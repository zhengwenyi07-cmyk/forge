# Forge Memory 面试讲解指南

这份文档用于把 Forge 的长期记忆系统讲清楚，而不是把组件名背一遍。建议先掌握“一分钟版本”，再沿着面试官追问逐层展开。

## 一句话定义

Forge Memory 是一个**面向 Coding Agent 的、版本感知的本地证据库**：它把可验证的历史任务结果保存为结构化 episode，在新任务中按仓库、代码指纹、路径、符号、词法相关性和测试证据检索少量线索，同时把所有历史内容隔离为不可信数据。

它不等于聊天历史，不等于 Repo-map，也不等于“把所有日志塞回 prompt”。

## 一分钟版本

> Forge 原来已经有 ConversationHistory、RunContext、EventLog、artifact 和 RepoIndex，但这些分别只解决当前窗口、当前任务、审计落盘和当前仓库结构，没有“新任务主动检索旧任务经验”的链路。
>
> 我把系统分成三层：RunContext 继续负责 working memory；SQLite checkpoint 负责同一 ChatSession 的跨进程恢复；SQLite + FTS5 负责跨任务 episodic memory。任务开始时只检索少量历史证据，按仓库和代码指纹过滤、按词法/路径/符号/验证/置信度/时间排序，并受独立 Token 预算约束。任务结束后不是让模型自由写记忆，而是由框架从 RunResult、测试和 artifact 生成结构化 episode。
>
> Repo-map 回答“当前代码是什么”，Memory 回答“以前发生过什么”。历史记忆永远以 user-role 的 `[UNTRUSTED HISTORICAL MEMORY]` 注入，敏感信息先净化，代码变化会造成 stale 或失效。第一版没有上向量数据库，也没有自动把单次经验升级成系统规则，因为当前规模下 SQLite FTS 更可解释、零模型成本，也更容易做版本和证据约束。

## 三分钟版本

### 1. 为什么需要记忆

Coding Agent 经常重复遇到同一仓库中的相似错误：同一测试约定、同一入口、同一种 CRLF 或运行时问题。如果完全不记忆，每次都从目录扫描开始，会重复花 Token 和步骤；但如果粗暴回放全部历史，又会把过期代码、失败尝试和提示注入带入新任务。

所以真正的问题不是“如何保存更多”，而是：**保存什么、什么时候可信、什么时候过期，以及最多给模型看多少。**

### 2. 原系统缺的是什么

- `ConversationHistory` 有滑窗和 `from_dicts()`，但生产入口没有持久化恢复。
- `ChatSession` 只在同一进程共享 `_shared_history`。
- `RunContext` 是单任务工作记忆，每次 `Agent.run()` 重建。
- `EventLog` 与 artifact 可审计但不会被新任务查询。
- `RepoIndex` 是当前代码事实，不包含历史 task outcome。
- 子 Agent 结果能回到父任务，但不会自动成为以后任务可检索的 episode。

因此 Forge 是“有历史文件，但没有长期记忆系统”。

### 3. 新系统如何工作

```text
新任务
  │
  ├─ 识别 repository_id + code_fingerprint
  ├─ SQLite/FTS5 召回同仓库候选
  ├─ 词法 + 路径 + 符号 + 指纹 + 测试 + 置信度 + 时间重排
  └─ 最多注入 memory.initial_token_budget
         │
         ▼
    ReAct / Repo retrieval / file tools / tests
         │
         ├─ Agent 可调用 memory_search / memory_get 继续按需查
         └─ finish / give_up / max_steps / failed
                 │
                 ▼
      框架从结果与证据生成 episode
      redact → validate → dedupe → transaction
```

### 4. 为什么是 SQLite + FTS5

Forge 当前是本地 Python Agent，不需要先部署向量服务。SQLite 提供事务、约束、索引、WAL、多进程恢复和单文件备份；FTS5 给出零模型调用、可解释的词法召回。代码任务还有很强的精确词：异常名、路径、函数、测试名，这些恰好适合 FTS。

embedding 的价值不是被否定，而是延后到“词法漏召回经过评测证明是瓶颈”之后再加。即使将来加入向量召回，也必须保留仓库、commit、路径、测试证据等结构化过滤。

### 5. 怎么防止记忆污染

- 自动写入者是框架，不是模型自由调用的 `memory_record`。
- 不保存完整对话、环境变量、完整 shell 输出或原始 HTTP/LLM 响应。
- API key、Bearer token、私钥、连接串在写入前净化。
- 指令型文本被标记和替换。
- 检索结果只能以 user-role 不可信数据进入上下文，不能成为 system message。
- 代码指纹不同的记忆标记 stale；相关文件全部消失会自动失效。
- 测试通过只对原代码指纹成立，stale 记忆必须重新验证。

## 深入技术版本

## 1. 记忆层次

### Working Memory

现有 `RunContext` 保存 phase、相关路径、近期工具证据、子 Agent 结果和验证结果；`ConversationHistory` 保存当前 ReAct 交换。它们高频、短生命周期、每步都可能变化，不应该每步写长期数据库。

### Conversational Memory

`ChatSession` 使用稳定 `session_id`，每轮结束后把受滑窗约束的消息、轮数、累计 token 和 step 原子写入 `session_checkpoints`。重启时按 `session_id + repository_id` 恢复。持久化并不表示全部发送给模型；恢复后仍经过原有历史裁剪和上下文预算。

### Episodic Memory

一条 episode 表示“某次任务发生了什么”：

- Issue/task 的安全摘录；
- finish/give_up/max_steps/failed；
- 修改、读取和历史命中的文件；
- 符号和检索信息；
- 定向测试状态；
- source artifact/log 的安全引用；
- repository、base commit、code fingerprint；
- confidence、trust、provenance 和 expiry。

第一版真正实现的长期记忆就是这一层。

### Semantic / Procedural / Preference Memory

- Semantic：多个 episode 支持的稳定事实，例如“这个仓库所有 parser 入口都通过 registry 注册”。
- Procedural：经过重复验证的策略，例如“修改生成文件前必须改 schema 并重新生成”。
- Preference：用户明确确认的格式、工具和工作流偏好。

第一版只在 schema/架构中保留类型，**不自动 consolidation**。单次成功不能自动变成永久规则，更不能自动改 system prompt。

## 2. 存储结构

```text
memory.sqlite3
├─ memories
│  ├─ identity: memory_id / repository_id / task_id
│  ├─ version: base_commit / code_fingerprint / schema_version
│  ├─ content: task_text / summary / evidence_json
│  ├─ links: related_files / related_symbols / source reference
│  ├─ quality: outcome / test_status / confidence / trust
│  └─ lifecycle: timestamps / expiry / invalidation / soft delete
├─ memory_fts
│  └─ summary / task / paths / symbols
├─ session_checkpoints
└─ memory_feedback
```

数据库默认在用户 state 目录，而不在目标代码仓库；自定义路径如果位于 workspace 内会被拒绝或明确降级。SQLite 使用 WAL、`busy_timeout`、短连接和 `BEGIN IMMEDIATE`；写入、去重和失效在事务里完成。运行中备份使用 SQLite backup API，不直接复制 WAL 文件。

## 3. 身份和版本

`repository_id` 优先由去凭据、规范化的 Git remote 计算；没有 remote 时退回规范化绝对路径哈希。这样同一仓库的不同 worktree/clone 可以共享作用域，同时不会把 remote token 保存进库。

`code_fingerprint` 由任务开始时的 HEAD、tracked dirty status 和 diff 形成。它表达“这条经验在哪个起始代码状态上发生”。完全相同指纹得高分；不同指纹仍可作为历史线索，但标记 stale，原测试通过结论不能直接复用。

这个策略是保守的：相同内容在重新提交后也可能变成 stale。它宁可要求重新验证，也不把旧测试结果误当作当前事实。未来可用 RepoIndex content version 增加内容等价判断。

## 4. 检索排名

第一阶段由 SQLite 结构化过滤和 FTS/LIKE 召回；第二阶段由 `MemoryManager` 重排：

```text
score = lexical×3.0
      + exact_fingerprint×2.5
      + path_overlap×2.0
      + symbol_overlap×1.5
      + verified_success×1.5
      + confidence×1.0
      + recency×0.5
      - stale×1.5
      - repeat×2.0
```

权重来自配置并随 query metrics 保存，不是隐藏常量。返回前再做 repository scope、失效、过期、文件存在性、条数和 Token 限制。

词法分数是第一版的可解释基线，不声称它已经等价于语义相似度。未来如果错误堆栈的自然语言表达变化造成明显漏召回，可以把 embedding 只用作候选召回信号，继续由版本和证据重排。

## 5. 写入和置信度

Agent 没有自动 `memory_record` 权限。任务结束后，框架从 `RunResult + RunContext + artifact` 生成候选：

```text
基础 confidence 0.25
+ 正常 finish 0.20
+ 有 patch 0.15
+ 定向测试通过 0.30
+ 其他已尝试验证 0.05
上限 1.0
```

这是可复现的证据权重，不是统计意义上的正确概率。`framework_verified` 只表示有当前任务验证证据，不表示官方 SWE-bench resolved。

失败 episode 也有价值：它能提示相似任务不要重复同一路径，但排序低、保留期可更短，并且不会被包装成成功经验。

## 6. Repo-map 与 Memory 如何协作

```text
               历史                          当前 checkout
        ┌────────────────┐               ┌────────────────┐
Issue ─>│ Memory retrieval│── path/symbol ─>│ RepoIndex/map  │
        │ what happened   │               │ what exists now │
        └────────────────┘               └───────┬────────┘
                                                 │
                                        file_view / tests
```

Memory 可以说“上次 `src/parser.py::parse` 与此错误相关”；RepoIndex 必须确认这个路径和符号现在还存在，Agent 再用 `file_view` 看当前内容。历史永远不能覆盖当前仓库事实。

## 7. 安全模型

系统把记忆视为一种持久化 RAG 数据，因此面临“持久化提示注入”：恶意 Issue 如果被保存，可能在未来任务再次出现。防线是：

1. 数据最小化：不保存原始全量对话。
2. 写入净化：secret regex、私钥块、连接串和指令型文本。
3. 来源和信任：每条记忆有 provenance/trust。
4. 角色隔离：历史只进 user-role 的 `[UNTRUSTED HISTORICAL MEMORY]`。
5. 工具隔离：memory tools 只能读当前 repository scope。
6. 当前事实优先：代码、RepoIndex 和测试覆盖历史说法。
7. 删除控制：普通 Agent 不能自主忘记或提升规则；用户 feedback 才能 obsolete/forget。

## 8. 成本和性能

- 无额外 LLM 调用，无 embedding 费用。
- 任务开始默认 600 token，单次工具默认 800 token。
- SQLite 候选有限制，工具 `limit` 最大 20；不会把完整库载入 prompt。
- 相同查询和相同 memory id 会记录重复，重复注入被降权。
- 每个操作短连接，适合 Forge 的低到中等并发；它不是面向数千并发服务的数据库设计。
- 每个 task 当前会做 Git identity/fingerprint 计算；大型 dirty diff 是已知成本点，可在后续用 RepoIndex version/mtime cache 优化。

## 9. 可观测性

每个 `RunResult.memory`/artifact 记录：

- search backend 与 store status；
- 初始 memory ids、stale 数、成功经验命中；
- query、返回 id、分数组成、Token、截断和重复；
- 检索/写入毫秒；
- 写入 memory id、是否去重；
- 历史文件与最终修改文件交集；
- 错误和降级状态。

指标不记录 API key、原始凭据或数据库连接信息。

## 10. 研究脉络怎么讲

- [Baddeley 的 Working Memory](https://pubmed.ncbi.nlm.nih.gov/1736359/)给出了短时存储与操作的概念边界；Forge 用 RunContext，而不是数据库模拟它。
- [Tulving 的 Episodic/Semantic 区分](https://garfield.library.upenn.edu/classics1987/A1987K827400001.pdf)解释了“某次任务经历”与“稳定知识”不能混写。
- [MemGPT](https://arxiv.org/abs/2310.08560)启发了有限上下文中的层级存储与按需调页，但 Forge 不需要重建 OS 式主循环。
- [LangGraph memory](https://langchain-ai.github.io/langgraph/concepts/memory/)明确区分 thread checkpoint 与 cross-thread store；Forge 采用同样的边界但不引入 graph runtime。
- [Reflexion](https://arxiv.org/abs/2303.11366)证明语言化反馈可以在后续尝试中复用；Forge 更保守，只让实际测试/artifact 决定信任。
- [Generative Agents](https://arxiv.org/abs/2304.03442)的 relevance/recency/importance 启发多信号排名与 consolidation，但 Forge 把代码版本和验证证据放在更高优先级。
- [Mem0](https://arxiv.org/abs/2504.19413)展示了抽取、合并和检索相对全上下文的成本收益；Forge 第一版避免其 LLM extraction/vector 依赖，先建立可解释基线。
- [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md)展示了可审计、可版本化的 Agent-owned memory；Forge 当前更适合结构化 SQLite episode，而不是第二个 Git 仓库。

## 常见面试追问与回答

### Q1：为什么不直接保存全部聊天？

全量聊天把 observation、失败尝试、秘密和外部指令一起永久化，Token 与历史线性增长，而且新任务需要的是经过筛选的证据，不是复演旧上下文。Forge 只保存受限 checkpoint 和结构化 episode。

### Q2：为什么不用向量数据库？

当前代码任务的关键线索通常是异常名、路径、函数、测试名，词法检索很强；SQLite FTS 零模型成本、可解释、事务和过滤完整。向量检索不能解决 commit 过期和测试可信度。先用 A/B 证明词法漏召回是瓶颈，再增加 embedding 作为候选信号。

### Q3：FTS 没有怎么办？

显式降级到参数化 LIKE，并记录 `search_backend=like`；不会静默假装 FTS 正常。能力下降可从 artifact 观察。

### Q4：模型会不会写入错误记忆？

第一版模型不能直接写。框架从 RunResult、RunContext 和验证记录生成 episode；模型 summary 仍标记为 observed，并经过净化。测试通过只对原 fingerprint 有效。

### Q5：成功经验与失败经验冲突怎么办？

都保留，不覆盖历史。相同 fingerprint、较新、验证更强、confidence 更高者排名靠前；旧记录显示 stale。用户可通过 feedback 标记 obsolete/forget。

### Q6：commit 一变，历史是不是全没用了？

不会删除。同仓库不同 fingerprint 的 episode仍可作为线索，但有 stale penalty，测试结论必须重跑；相关文件全部消失才自动失效。这在复用和安全之间取保守平衡。

### Q7：RepoIndex 已经有缓存，为什么还需要 Memory？

RepoIndex 缓存的是当前文件解析结果和依赖图；Memory 保存历史 task outcome 与验证。前者是 code state，后者是 experience state。生命周期和可信度不同，混在一起会污染当前事实。

### Q8：多 Agent 如何处理？

manager 可跨线程共享，但 task scope 使用 thread-local。子 Agent 写独立 episode，provenance 带 `parent_task_id` 和 `agent_type`；content hash 防止相同结果重复。父任务 artifact 仍保留 child reference。

### Q9：并发安全吗？

每次操作使用独立 SQLite connection，WAL + busy timeout，写操作用 `BEGIN IMMEDIATE`；schema migration、episode/FTS 写入、feedback/失效都在事务中。专项测试覆盖并发 40 条写入和跨进程重开。

### Q10：如何备份？

使用 SQLite backup API 获取一致快照，不在 WAL 活跃时直接复制主文件。数据库默认位于用户 state 目录，不进入 Git。

### Q11：如何证明它真的变强？

工程测试只能证明持久化、检索、安全和降级正确。能力需要冻结任务、同模型同预算 memory off/on 配对 A/B，比较 resolved、正确文件首次发现步骤、无关读取、token、耗时与负迁移。当前没有运行付费模型评测，因此不能声称 resolved 提升。

### Q12：最大的当前限制是什么？

没有 embedding；没有自动 semantic/procedural consolidation；指纹保守；符号删除尚未做完整 AST 级失效；ranking 权重未经真实 Coding Agent A/B 校准；SQLite 适合本地中低并发而非服务端大规模并发。

## 已实现、设计中、未验证

### 已实现并由离线测试验证

- ChatSession checkpoint 保存/恢复与敏感信息净化。
- SQLite schema、WAL、事务、FTS5/LIKE 路径和 backup API。
- 结构化 episodic memory、框架自动写入、幂等去重。
- repository scope、代码指纹 exact/stale、文件删除失效、expiry/feedback 数据模型。
- 词法/路径/符号/验证/置信度/时间/重复多信号重排。
- `memory_search` / `memory_get` 只读工具和 Token 截断。
- untrusted user-role 注入、secret redaction、prompt injection 净化。
- 子 Agent parent provenance、并发写入和 artifact metrics。

### 已设计但第一版未实现

- 从多个 episode 自动生成 Semantic Memory。
- 经人工/评测审核后提升 Procedural Memory。
- embedding/vector 混合召回。
- 后台 reflection/dreaming/consolidation job。
- AST 级符号重命名映射和内容等价 fingerprint。
- 用户友好的 memory feedback/forget CLI。

### 尚未经过真实模型评测的假设

- 历史经验能更早找到正确修改文件。
- 能减少无关文件读取、步骤和 Token。
- 能提高 SWE-bench resolved rate。
- 当前默认排序权重优于无记忆或其他权重。

## 最后一句收束

> 我没有把 Memory 做成“无限上下文”，而是把它做成一个有版本、有证据、有失效、有预算、有安全边界的历史检索层。Repo-map负责理解现在，Memory负责复用过去，最终仍由当前代码和测试裁决。
