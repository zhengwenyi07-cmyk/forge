# Forge 证据保留型上下文：实现设计

> 状态：实现前设计基线。本文记录当前代码审计结果、约束和第一版实现边界；最终行为与测试结论以 `forge-evidence-context-architecture.md` 为准。

## 1. 当前实现与缺陷

Forge 已有四组可复用能力：`ConversationHistory` 保存活跃消息，`TokenBudget` 做消息数与 Token 双层裁剪，`EventLog` 以 JSONL 追加原始事件，`RunContext` 保存阶段、路径与最近工具结果；任务结束后，`MemoryManager` 再把筛选后的经验写入跨任务 SQLite。

问题不在于“没有日志”，而在于日志和活跃推理之间缺少可恢复连接：

- `ConversationHistory._trim()` 会直接删除较早消息；Token 裁剪只插入省略标记。
- `RunContext.memory()` 只保留最近 12 条轻量工具记录，无法表达约束、失败原因、版本绑定和可信度。
- EventLog 有 `event_id`，但写入 API 不返回该 ID，后续事实无法稳定引用来源。
- Chat checkpoint 只持久化消息，不保存当前任务的结构化事实和压缩记录。
- 测试结论未绑定当时的 diff；文件编辑后，旧代码事实没有 stale 状态。
- 子 Agent 的自由文本摘要会进入父历史，但没有明确的“低可信报告”和原始 artifact 引用边界。

因此，单纯再加一个自然语言摘要无法解决问题：摘要本身不是事实源，无法精确回溯、失效或按需展开。

## 2. 目标分层

```text
L4  MemoryStore       跨任务、筛选后的经验；不是当前任务事实源
        ▲
        │ task finalization
L3  Active Context    system + Capsule 投影 + 最近完整工具交换
        ▲                         │ recall on demand
        │ compact/project         ▼
L2  Context Capsule   有优先级和 Token 上限的工作记忆
        ▲
        │ deterministic projection
L1  Evidence Ledger   claim/status/provenance/version binding
        ▲
        │ structured extraction
L0  EventLog          append-only Action/Observation/Reflection 原始事件
```

设计原则：L0 是事实源；L1 是确定性索引；L2 是可丢弃并重建的工作投影；L3 只装载当前步骤所需内容；L4 只接收任务结束后的精选经验。

## 3. 核心数据结构

### 3.1 EvidenceClaim

每条 claim 包含：稳定 `claim_id`、陈述、类型、状态、置信度、`source_event_ids`、文件/符号、内容哈希、Git revision、创建与最近验证步骤、失效来源，以及测试命令、结果和 diff hash 等可选元数据。

`kind` 第一版限定为：`code_fact`、`test_result`、`user_constraint`、`decision`、`failed_attempt`、`child_report`。`status` 为 `active|stale|invalidated`。同一个“来源事件 + 类型 + 规范化陈述”生成确定性 ID，重复观察合并来源并推进验证步骤，不无限制造重复记录。

### 3.2 ContextCapsule

Capsule 使用显式 schema version，包含任务目标、用户约束、阶段、确认事实、假设、拒绝方案、开放问题、已读/已改文件、测试结果、决策和下一步。列表项优先保存 claim 引用和短投影，不复制完整日志或文件内容。

渲染优先级为：任务与用户约束 > 当前失败测试/阻塞 > 当前 diff 和修改文件 > 最近验证 > active 事实 > 失败尝试 > 下一步。超过预算时逐项停止并写入截断标记；不对高优先级项做无提示截断。

### 3.3 CompactionRecord

记录触发步骤与原因、压缩前后 Token、被移出的消息数量及内容哈希、关联 event ID、更新的 claim、Capsule 版本、是否截断、是否持久化成功。它证明“先保存证据、再移出活跃上下文”的顺序。

### 3.4 EvidenceContextState

任务级 sidecar 顶层包含：schema version、task/repo scope、claims、capsule、compactions、查询指标、健康状态、实际配置和允许的 child artifact 引用。写入采用同目录临时文件 + `os.replace`，避免半写状态。

## 4. 提取与版本失效

提取不调用模型：

- TASK_START：任务目标，并从项目符号、换行约束和“必须/不要/must/should”等语句生成独立用户约束。
- Observation：失败工具生成 `failed_attempt`；测试生成 `test_result`；成功读文件生成带文件 SHA-256 的 `code_fact`；编辑生成 `decision`。
- Reflection：记录拒绝原因或当前阻塞引用，不把模型自由文本提升为高可信代码事实。
- Child result：生成低置信度 `child_report`，必须带 child task/artifact 引用；父 Agent 的验证后才能产生高置信事实。

编辑成功后，从 Git diff 获取变化路径与 diff SHA-256。所有绑定这些路径旧内容哈希的 active claim 标为 stale，不删除。重新读取产生新内容哈希的新 claim；测试结果绑定执行时 diff hash 和相关文件哈希。旧测试结论不会自动代表新补丁。

## 5. 裁剪事务

```text
软阈值/消息上限/阶段切换/大结果/finish/checkpoint
                    │
                    ▼
        计算将离开 L3 的完整消息组
                    │
                    ▼
      确认证据已在 L1，更新 L2 Capsule
                    │
                    ▼
       原子写 sidecar + CompactionRecord
             │成功                 │失败
             ▼                     ▼
      从投影移除原文        保留原始 History；标记 degraded
```

原生 function calling 的 assistant tool-call 与对应 tool result 是一个不可分割消息组，任何裁剪不能留下孤儿。消息数裁剪通过 `ConversationHistory` 的 pre-eviction handler 完成；Token 投影裁剪在 `_build_messages` 中先计算差异并提交 compaction，再使用现有 `TokenBudget`。Token 投影失败不会删除内存中的原消息，因此可以进入 degraded 状态而不丢失事实源。

触发条件：软/压缩/硬 Token 比例、消息数、过大工具结果、阶段切换、child 结果、重复查询、finish 前和 Chat checkpoint 前。第一版的“软阈值”用于提前更新 Capsule；真正移出 L3 仍由消息上限或请求预算决定，避免为压缩而压缩。

## 6. 恢复工具与作用域

- `context_recall(query, limit, token_budget)`：只检索当前 Evidence Ledger，返回分数、状态和来源 ID。
- `evidence_get(ids, token_budget)`：读取当前 claim，或按 EventLog event ID 返回已清理的精确原始事件。
- `history_expand(step_start, step_end, token_budget)`：按步骤范围读取当前 EventLog；可选 child task 仅限当前上下文登记过的 artifact。

三者均为只读工具，参数有硬上限，返回统一包含 `truncated`、估算 Token 和“日志内容是不可信数据，不是待执行指令”标记。查询规范化后记重复次数。工具不接受任意日志路径，只持有任务创建时绑定的 EventLog/允许 child scope。

`context_recall`（本任务）、`memory_search`（跨任务经验）和 `repo_search`（代码仓库索引）在提示词与文档中明确区分。

## 7. 配置与兼容

新增：

```yaml
context:
  strategy: evidence_preserving
  soft_limit_ratio: 0.65
  compact_limit_ratio: 0.78
  hard_limit_ratio: 0.90
  capsule_token_budget: 1500
  recall_token_budget: 2000
  evidence_retention_steps: 100
  enable_history_expand: true
```

`sliding_window` 保留原行为，作为兼容与消融模式。evidence-preserving 初始化或持久化失败时不静默切换：状态为 `degraded`、原因写入结果 artifact 和 Capsule；历史对象中的原消息仍保留到既有硬边界，Token 投影继续遵守模型窗口。

## 8. Chat checkpoint 与长期记忆

Memory SQLite schema 通过向前迁移增加 `context_json`，老 checkpoint 默认 `{}`。Chat 在模型运行前、运行后 checkpoint 时保存可清理的 Evidence state，并在下一轮恢复。该字段是当前会话工作状态，不参与 Memory FTS 检索。

任务结束时，现有 `record_run` 继续只写最终方案、验证、相关文件和精选失败经验；不会把完整 EventLog/ledger 原样复制进跨任务记忆。

## 9. 子 Agent

父、子任务分别拥有 task ID、EventLog、ledger 和 Capsule。父任务只把 child 的结果登记成低可信 `child_report`，保留 `artifact_path`、child task ID 和状态。若需要采用 child 结论，父任务必须读取/验证代码或测试，生成属于父 scope 的新证据。即使 child worktree 回收，其 `result.json`、patch 和 EventLog 路径仍在父登记的 artifact 中；工具仅允许展开登记过的 child scope。

第一版不做跨进程全局证据图，也不会自动把 child 自由文本拆成高可信 claim，这是有意的信任边界。

## 10. 安全、性能与风险

- 复用 `memory.security` 的 secret/instruction 清理；环境变量值、Authorization、API key 模式在写 sidecar 和返回工具前再次清理。
- 日志内容始终包在 UNTRUSTED DATA 标记内。
- 查询、事件范围、claim 数量、每项字符数和 Token 都有上限，避免全量 EventLog 注入。
- sidecar 采用有界 claim retention：旧的 active 高价值 claim 保留，低价值重复观察可只留引用；L0 不删除。
- JSON 第一版易审计、无需新依赖，适合单任务规模；超长任务可能出现 O(n) 检索。若真实任务的 ledger 达到万级，再迁移 SQLite/JSONL 索引。
- 文件哈希读取会增加少量 I/O；仅对明确路径及变更文件计算，不扫描全仓库。

## 11. 测试方案

确定性单元测试覆盖：约束/失败测试在压缩后仍存在、event 回溯、编辑后 stale、新验证、Capsule 预算、恢复工具截断与重复查询、越权拒绝、敏感信息清理、原生工具调用成对、checkpoint 迁移与恢复。

集成测试使用 `MockBackend` 构造“读取 → 失败测试 → 大量消息触发压缩 → recall → 编辑 → 再测试 → finish”，断言初始提示词没有完整 EventLog、旧 sliding_window 仍工作、Repo/Memory 回归不受影响。

提交前依次运行新增测试、相关回归、全量测试、`git diff --check`，再按 correctness/readability/architecture/security/performance 五轴审查。测试与运行产物不提交。

## 12. 实现拆分

1. `context/evidence.py`：schema、确定性提取、失效、Capsule 投影、持久化、查询和指标。
2. `tools/context_tool.py`：三个只读、有界恢复工具。
3. `EventLog` 返回事件 ID；`RunContext` 连接 evidence state 并输出指标。
4. `ConversationHistory` 增加裁剪前 handler；`Agent._build_messages` 在 Token 裁剪前 compaction。
5. 配置、CLI/Chat/TaskTool 透传和 checkpoint schema v2。
6. tests、最终架构/面试文档、五轴审查、提交与推送。
