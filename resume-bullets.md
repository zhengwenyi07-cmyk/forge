# Forge Agent — 简历项目描述（多版本）

> 以下提供 6 个版本的简历项目描述，按不同侧重点组织。
> 每个版本三段式结构（架构 → 工具/适配 → 工程化），可直接嵌入简历「项目经历」栏。

---

## 版本选择指南

| 版本 | 适用岗位 | 侧重点 |
|------|---------|--------|
| V1 标准版 | 通用 Agent 开发岗 | 均衡，面面俱到 |
| V2 架构深度版 | Agent 架构师 / 资深岗 | ReAct 循环、Reflection、设计模式 |
| V3 工程化版 | Agent 工程化 / 测试开发 | MockBackend、376 测试、CI/CD |
| V4 模型适配版 | LLM 应用开发 / 推理优化 | 多模型适配、R1 降级、token 管理 |
| V5 上下文与检索版 | RAG / 上下文工程岗 | repo-map、TokenBudget、tree-sitter |
| V6 精简版 | 简历空间有限时 | 每段一句话，保留关键词密度 |

---

## V1 标准版（推荐）

> 适用：大多数 Agent 开发岗位。均衡覆盖架构、工具链、测试三个维度。

** Agent 架构与自省机制设计**：基于 SWE-agent 的 ReAct 范式从零构建自主编程 Agent 主循环，将上下文组装、LLM 推理、Tool Calling、结果回写拆分为独立阶段，替代黑盒式 AgentExecutor；设计双重 Reflection 自省机制——测试失败时自动注入错误分析提示引导 LLM 反思根因、连续 N 步无文件修改时触发方向反思，结合连续相同 Action 死循环检测（滑动窗口比对 `(tool, params)` 元组）实现三种自动熔断策略，防止 Agent 陷入局部最优或无效循环。

** 工具链抽象与多模型统一适配**：基于策略模式设计 `BaseTool` 抽象基类与 `ToolRegistry` 注册中心，将 Shell 安全执行、文件分页读写、正则/AST 符号搜索、Pytest 结构化解析、Git 四步操作共 12 个工具统一为 `execute(params) → ToolResult` 接口，新增工具零侵入主循环；基于适配器模式构建多 LLM 后端统一层，覆盖 Claude 原生 tool_use、OpenAI function calling 及 DeepSeek R1 等不支持 function calling 模型的文本解析降级路径，实现一行配置切换五种后端。

** 上下文管理、沙箱安全与可测试性工程**：设计基于优先级的 Token 预算分配器（System 10% → Repo-map 15% → History 50% → Observation 25%），结合滑动窗口与二分截断实现双层上下文裁剪；借鉴 Aider 的 tree-sitter 符号提取方案，实现 8 语言结构化仓库地图生成并按重要性排序注入 System Prompt；设计 Local/Docker 双层 Runtime 抽象，Docker 沙箱默认断网、Bind Mount 双向同步。测试架构上，基于依赖倒置设计 MockBackend 以预设 Action 脚本替代真实 LLM 调用，将非确定性 LLM 行为转化为确定性输入/输出序列，实现 376 个测试在约 20 秒内覆盖全部 6 种终止路径与 2 种 Reflection 条件。

---

## V2 架构深度版

> 适用：Agent 架构师、资深开发岗。强调设计决策、循环结构、自省机制。

** ReAct 主循环与自省熔断体系**：基于 SWE-agent 范式构建 Agent 主循环，将 Observe → Think → Act 三段式严格分离——`_build_messages()` 注入 repo-map 与裁剪后历史作为上下文、`_call_with_retry()` 调用 LLM 获取 Action、`execute_tool()` 执行工具并回写 Observation，全程通过 EventLog 以 JSONL append-only 格式实时记录；在循环中嵌入自省层——LLM 调用的测试工具返回失败时自动注入 REELECTION_TEST_FAILED 引导 LLM 分析根因、连续 6 步无文件编辑时注入 REFLECTION_NO_EDIT 防止无尽探索、连续 3 步 `(tool, params)` 元组完全一致判定死循环熔断，三种策略形成"软引导 + 硬截断"的分层保护，比 LangChain 的 `max_iterations` 单一熔断更智能。

** 工具系统与 Runtime 抽象层**：以策略模式抽象 `BaseTool` 基类——`name`/`description`/`parameters_schema` 为 LLM 提供 JSON Schema、`execute()` 执行实际逻辑，`ToolRegistry` 以注册表模式管理全部 12 个工具，新增工具仅需继承基类并注册一行，主循环无感知；设计 `Runtime` 抽象层将命令执行与工具解耦——`LocalRuntime` 直调 subprocess、`DockerRuntime` 懒启动容器通过 `docker exec` 执行，工具通过依赖注入接收 Runtime，ShellTool 在 Runtime 之上叠加三层安全：黑名单硬拦截（`rm -rf /` 等永不执行）→ 只读白名单免确认 → 写操作回调确认。

** 依赖倒置与可测试性架构**：严格遵循依赖倒置原则——Agent 核心只依赖 `LLMBackend` 与 `BaseTool` 两个抽象接口，不感知具体模型或工具实现。基于此设计 MockBackend 测试策略：维护预设 Action 脚本队列，`complete()` 按序返回，配合 `call_count` 与 `received_messages` 实现精确断言。将不确定性隔离在 LLM 层之外，Agent 状态机、Reflection 触发、死循环检测、Token 裁剪、多轮对话历史传递等全部逻辑转化为确定性单元测试。ChatSession 通过 `_pending_history` 注入实现跨轮历史复用，支持多轮对话的完整测试覆盖。

---

## V3 工程化版

> 适用：Agent 工程化 / 测试开发 / 平台工程岗。强调 MockBackend、测试策略、确定性工程。

** Agent 测试架构与 MockBackend 设计**：针对 AI Agent 系统因 LLM 非确定性而难以进行确定性测试的核心难题，基于依赖倒置原则设计 MockBackend——以预设 Action 脚本队列替代真实 LLM API 调用，每次 `complete()` 按剧本返回下一个 Action 并记录 `call_count` 与 `received_messages`。配合 NoopTool（始终返回成功）与 FailingTool（始终返回失败），实现 Agent 全部逻辑路径的确定性覆盖。376 个测试在约 20 秒内完成——如果将 MockBackend 替换为真实 LLM，相同测试量需 30-60 分钟且产生 API 费用。

** 全路径测试覆盖与 CI 就绪**：通过 MockBackend 实现对 Agent 6 种终止路径（SUCCESS / GAVE_UP / MAX_STEPS / LOOP_DETECTED / LLM_ERROR / UNKNOWN_TOOL）的精确单元测试，每种路径对应独立测试类；2 种 Reflection 触发条件（test_failed / no_edit）通过 FailingTool 和连续 NoopTool 调用精确还原；多轮对话历史跨轮传递、流式输出 callback、指数退避重试（含不重试 4xx 错误的逻辑）均有独立测试覆盖。测试套件零外部依赖（无网络、无 API Key）、完全确定性、支持 `pytest -n auto` 并行执行。

** 安全执行与可观测性**：Shell 工具设计黑名单硬拦截（正则匹配永不执行）→ 只读白名单（`ls`/`grep`/`pytest` 等直接执行）→ 确认回调（`rm`/`git commit`/`curl` 等等待用户 y/n）三层安全模型，`--confirm` 模式对写操作要求确认，Docker 沙箱模式默认断网 + Bind Mount 双向同步。EventLog 以 JSONL append-only 格式记录每次运行的全部事件（含 LLM 原始输出），每条写入后立即 flush 防崩溃丢失，支持 `replay()` 完整回放与 `summarize_run()` 统计，为 Agent 行为审计和调试提供结构化数据基础。

---

## V4 模型适配版

> 适用：LLM 应用开发 / 推理优化 / MLOps 岗。强调多模型适配、function calling 降级、token 管理。

** ReAct Agent 主循环与多模型推理**：基于 SWE-agent 范式构建 ReAct 主循环，实现 Observe → Think → Act 三段式推理。循环中动态注入 tree-sitter 生成的 repo-map 作为全局视野，使 LLM 在每一步决策时同时具备环境反馈与代码库结构信息。通过 EventLog 以 JSONL 格式记录完整推理链（含 LLM 原始输出与 token 消耗），支持离线回放与行为分析。

** 多 LLM 后端统一适配与降级策略**：基于策略模式设计 `LLMBackend` 抽象层——`AnthropicBackend` 处理 Claude 原生 tool_use 格式（system prompt 独立传递、tool_result 以 content block 回传），`OpenAICompatBackend` 以单一类覆盖 OpenAI/DeepSeek/Groq/Ollama 四种后端的 function calling 路径，Router 工厂函数按配置字符串选择具体策略。针对 DeepSeek R1 等不支持 function calling 的模型，设计文本解析降级路径：将工具 JSON Schema 注入 System Prompt → 要求模型输出 `{"tool": "xxx", "params": {...}}` 格式 → 正则提取 JSON 块解析为 Action，使 Agent 在不支持 function calling 的强推理模型上仍可完整运行。

** Token 预算管理与上下文压缩**：设计基于优先级的 Token 预算分配器——System 指令 10%（永不裁剪）→ Repo-map 15%（按符号重要性排序后截断）→ History 50%（从旧到新贪婪裁剪，保留任务描述首条）→ Observation 25%，15% 总预算作为安全预留。结合 ConversationHistory 滑动窗口（条数维度，保留首条）与 TokenBudget 二分截断（Token 维度，超出插入 `[N earlier messages truncated]` 提示）实现双层压缩。优先使用 tiktoken 的 `cl100k_base` 编码做精确计数，环境受限时自动降级为字符数 // 4 估算（误差 <15%）。采用指数退避重试（间隔翻倍，最多 3 次），对 401/403/400 等客户端错误不重试。

---

## V5 上下文与检索版

> 适用：RAG / 上下文工程 / AI Infra 岗。强调 repo-map、token 管理、tree-sitter。

** Agent 上下文架构设计**：构建 ReAct 循环中的多层上下文注入系统——System Prompt 包含角色指令、工具能力描述与动态生成的仓库结构地图；History 注入经双层裁剪的对话历史（首条任务描述永久保留 + 最近 N 轮 Observation）；Observation 注入工具执行的实时反馈。各部分通过基于优先级的 Token 预算分配器协调——System 10%、Repo-map 15%、History 50%、Observation 25%，15% 总预算预留为安全边界，裁剪时从低优先级逐级缩减。

** 仓库地图（Repo-map）生成与注入**：借鉴 Aider 的 repo-map 方案，基于 tree-sitter 实现多语言代码仓库的结构化符号提取——通过 `_LANG_REGISTRY` 注册表支持 Python/JavaScript/TypeScript/Go/Rust/Java/C/C++/Ruby 共 8 种语言，运行时按需 `importlib.import_module` 加载语法包，不可用时静默降级为正则匹配。递归遍历 AST 提取函数、类、方法定义及其行号与缩进级别（区分 `class`、顶层 `function`、内部 `method`），按"顶层符号数 - 文件大小/10000"评分排序后按 Token 预算截断注入 System Prompt，使 LLM 在探索前即获得代码库的全局符号视图。

** 多轮对话记忆与工程落地**：设计 ConversationHistory 滑动窗口——`max_messages` 条数限制保留首条 + 最近消息，超出时从索引 1 处丢弃旧消息；TokenBudget 提供基于 Token 数量的二次裁剪，从最新消息贪婪选择直到预算耗尽，丢弃消息插入截断提示。支持跨轮复用（ChatSession 通过 `_pending_history` 注入），多轮对话间历史保留。EventLog 以 JSONL append-only 格式持久化完整上下文流转，支持历史回溯与行为审计。

---

## V6 精简版

> 适用：简历空间紧张、只需一段的情况。保留关键词密度。

** Agent 架构与自省机制**：基于 SWE-agent 的 ReAct 范式构建自主编程 Agent 主循环，设计双重 Reflection 自省机制（测试失败触发 + 连续无编辑触发）与死循环检测实现三种自动熔断，替代传统 `max_iterations` 硬截断。

** 工具链抽象与多模型适配**：基于策略模式设计 BaseTool 抽象层，12 个工具（Shell/文件/搜索/测试/Git）统一接口、零侵入扩展；多 LLM 后端适配层覆盖 5 种模型，针对不支持 function calling 的模型设计文本解析降级方案。

** 上下文管理与确定性测试**：Token 预算优先级分配 + 双层裁剪；tree-sitter 8 语言 repo-map 注入 System Prompt；基于依赖倒置设计 MockBackend 将非确定性 LLM 行为转化为确定性测试序列，376 个测试 20 秒覆盖全部终止路径。

---

## 附：关键词速查表

> 面试前 10 分钟快速过一遍，确保能随口展开任意关键词。

| 关键词 | 一句话解释 | 对应代码位置 |
|--------|-----------|-------------|
| ReAct 循环 | Observe → Think → Act 三段式，每步决策基于上一步 Observation | `agent/core.py:95-269` |
| Reflection | 测试失败/连续无编辑时注入元认知 prompt，不是简单重试 | `agent/core.py:228-251` |
| 死循环检测 | 比较最近 N 条 `(tool, params)` 元组是否完全一致 | `agent/core.py:330-351` |
| BaseTool | 4 个抽象成员：name/description/schema/execute | `tools/base.py:53-101` |
| ToolRegistry | 链式注册 + execute_tool 查找 + get_schemas 汇总 | `tools/base.py:108-169` |
| MockBackend | 预设 Action 队列替代 LLM，脚本用完自动 GIVE_UP | `llm/base.py:133-201` |
| TokenBudget | 10/15/50/25 优先级分配 + 两层裁剪 | `context/token_budget.py:105-185` |
| RepoMap | tree-sitter 8 语言符号提取 + 重要性排序 + 预算截断 | `context/repo_map.py:153-224` |
| 文本 fallback | R1 不支持 function calling → tool desc 注入 prompt → 正则解析 JSON | `llm/openai_compat.py:252-355` |
| 三层安全 | 黑名单硬拦截 → 白名单免确认 → 回调确认 | `tools/shell_tool.py:34-92` |
| Runtime 抽象 | LocalRuntime (subprocess) / DockerRuntime (docker exec) | `tools/runtime.py:69-110` |
| EventLog | JSONL append-only，每条 flush，支持 replay + summarize | `agent/event_log.py:32-264` |
| 376 测试 20 秒 | MockBackend 消除 LLM 延迟和不确定性 | 所有 tests/*.py |
| 6 种终止路径 | SUCCESS/GAVE_UP/MAX_STEPS/LOOP/LLM_ERROR/UNKNOWN_TOOL | `tests/test_day2.py` |
