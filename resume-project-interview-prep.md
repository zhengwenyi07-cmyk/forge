# Forge Agent — 简历项目 & 面试准备

---

## 一、简历项目描述

> 以下三段按"Agent 架构 → 工具链与模型适配 → 工程化与测试"的递进结构组织，可直接嵌入简历的「项目经历」栏。

---

** Agent 架构与自省机制设计**：基于 SWE-agent 的 ReAct 范式构建自主编程 Agent 主循环，将观察注入、LLM 推理、Tool Calling、结果回写拆分为独立阶段，替代黑盒式 AgentExecutor；设计双重 Reflection 自省机制——测试失败时自动注入错误分析提示、连续 N 步无文件编辑时触发方向反思，并结合连续相同 Action 死循环检测（滑动窗口比对）实现三种自动熔断策略，防止 Agent 在探索中陷入局部最优或无效循环。

** 工具链抽象与多模型适配**：基于策略模式设计 BaseTool 抽象基类与 ToolRegistry 注册中心，将 12 个工具（Shell 执行、文件分页读写、正则/AST 符号搜索、Pytest 结构化解析、Git 四步操作）统一为 `execute(params) → ToolResult` 接口，新增工具无需修改主循环代码；基于适配器模式构建多 LLM 后端统一层，覆盖 Claude 原生 tool_use、OpenAI/DeepSeek/Groq/Ollama 的 function calling 路径，并针对 DeepSeek R1 等不支持 function calling 的模型设计「工具描述注入 System Prompt → JSON 文本正则解析」的降级方案，实现一行配置切换五种后端。

** 上下文管理与可测试性工程**：设计基于优先级的 Token 预算分配器（System 10% → Repo-map 15% → History 50% → Observation 25%），结合 ConversationHistory 滑动窗口（条数维度）与 TokenBudget 二分截断（Token 维度）实现双层上下文裁剪；借鉴 Aider 的 tree-sitter 符号提取方案，实现覆盖 Python/JS/TS/Go/Rust/Java/C/C++/Ruby 共 8 种语言的结构化仓库地图生成，按符号重要性与文件大小排序后注入 System Prompt。**测试架构上**，基于依赖倒置原则设计 MockBackend——以预设 Action 脚本队列替代真实 LLM API 调用，将非确定性的 LLM 行为转化为完全确定的输入/输出序列，配合 NoopTool、FailingTool 及 Click CliRunner，实现 376 个测试在约 20 秒内覆盖 Agent 全部 6 种终止路径、2 种 Reflection 触发条件及多轮对话历史跨轮传递，解决了非确定性 AI 系统难以进行确定性单元测试的工程难题。

---

## 二、逐条深度讲解

### 第一条：Agent 架构与自省机制设计

**"基于 SWE-agent 的 ReAct 范式构建自主编程 Agent 主循环"**

这条说的核心技术决策是：为什么选择 ReAct 而不是 Plan-Execute 或纯 Chain-of-Thought？

- **ReAct（Reasoning + Acting）** 的核心理念是让 LLM 在每一步都输出一个 `(Thought, Action)` 对——先思考"我现在应该做什么"，再执行具体操作，然后观察结果进入下一轮。这和人类解决复杂编程问题的过程高度一致：看代码 → 思考原因 → 修一处 → 跑测试 → 看结果 → 继续修。
- 对比 Plan-Execute 范式：Plan-Execute 先生成完整计划再执行，但编程任务中计划往往需要根据测试结果动态调整，ReAct 的"边做边想"更合适。
- 具体实现在 `agent/core.py:95-269`，核心循环结构是 `_build_messages() → _call_with_retry() → 解析 Action → execute_tool() → 写入 history → 检测终止/Reflection → 下一轮`。

**"双重 Reflection 自省机制"**

这是项目相对于 SWE-agent 和 Aider 的**原创设计**。Reflection 的本质是"系统 2 思维"（Daniel Kahneman 的术语）——当 Agent 陷入局部最优时，系统主动注入反思提示，强制 LLM 切换到元认知模式。

- **触发条件 A（测试失败）**：`agent/core.py:228-238`。当 LLM 调用 `test` 或 `pytest` 工具且返回失败时，系统注入 `REFLECTION_TEST_FAILED` 这个 prompt。这个 prompt 不是简单的"你失败了再来一次"，而是引导 LLM 做三件事：(1) 仔细读错误信息 (2) 确认最近一次编辑是否正确 (3) 是否需要更多上下文。这避免了 LLM 反复尝试同一错误修复策略。
- **触发条件 B（连续 N 步无编辑）**：`agent/core.py:241-251`。当 `steps_without_edit` 超过阈值（默认 6 步）时触发。这防止 Agent 陷入"无尽探索"模式——一直读文件、搜索，但不敢动手改。Reflection prompt 追问："你已经有足够上下文了吗？如果有，动手改；如果没有，精确说出还需要什么，一步拿到，然后改。"
- **死循环检测**：`agent/core.py:330-351`。比较最近 N 条 Action 的 `(tool_name, params)` 元组。注意是比较 **元组** 而非字符串——这可以区分"两次调同一个工具但不同参数"和"完全相同的重复操作"。只有后者才是死循环。

**面试时你可以这样讲**：

> "ReAct 循环本身不是创新——SWE-agent 和 LangChain 都有。我做的改进是在循环中嵌入了一个'自我监控层'。LLM 在连续探索或反复失败时，天然倾向于重复相似操作，这是 Transformer 的自回归特性决定的。我设计了两种触发条件和一个定量检测器，在 LLM 可能陷入局部最优时主动注入元认知 prompt。这比 LangChain 的 `max_iterations` 硬截断更智能——不是在 LLM 可能还在有效工作时就强行终止，也不是等它彻底跑飞了才发现。"

---

### 第二条：工具链抽象与多模型适配

**"BaseTool 抽象基类 + ToolRegistry 注册中心"**

这是项目架构中最能体现**设计模式功力**的部分。

- **BaseTool**（`tools/base.py:53`）定义了四个必须实现的成员：`name`、`description`、`parameters_schema`、`execute()`。其中前三个是为 LLM 服务的（生成 function calling 的 JSON Schema），最后一个是实际执行逻辑。
- **ToolRegistry**（`tools/base.py:108`）是注册表模式——所有工具在一个地方注册，`Agent` 通过 `execute_tool(name, params)` 调用，完全不感知具体工具的存在。这实现了**开放-封闭原则**：新增工具只需实现基类、在注册表加一行，主循环零改动。
- **设计亮点**：`execute_tool()` 做了两层错误保护——工具不存在时返回 error ToolResult（不抛异常），工具内部异常也被 try/except 捕获（`tools/base.py:144-152`）。这意味着即使 LLM "幻觉"出一个不存在的工具，Agent 也不会崩溃，而是看到错误信息后在下一轮尝试其他方法。

**"12 个工具的设计逻辑"**

这些工具并非随意堆砌，而是按照编程任务的认知阶段分层：

| 认知阶段 | 对应工具 | 设计意图 |
|---------|---------|---------|
| 探索 | `shell`、`find_files`、`search_text`、`find_symbol` | 让 LLM 理解代码结构 |
| 精读 | `file_read`、`file_view` | 分页机制防止一次性灌入 5000 行 |
| 编辑 | `file_write` | 全量覆盖而非 diff 编辑，简化 LLM 的编辑模型 |
| 验证 | `test`（PytestTool） | 结构化解析输出，成功只返统计行，失败返失败列表 |
| 版本控制 | `git_status`、`git_diff`、`git_add`、`git_commit` | Agent 自主管理修改历史 |

其中 `file_view` 是很巧妙的设计——大文件先被 `file_read` 截断到 500 行，截断处提示"用 file_view 分页阅读"。`file_view` 每次返回 100 行窗口，并在末尾附上导航指令（`Next: file_view path=... start_line=201`），Agent 可以像人类一样"翻页"阅读。

**"多 LLM 后端的统一适配层"**

这是项目中**策略模式 + 适配器模式**的典型应用：

- **策略模式**：`LLMBackend` 是抽象策略接口，`AnthropicBackend` 和 `OpenAICompatBackend` 是具体策略。`Agent` 只依赖抽象，Router 负责根据配置选择策略。切换模型不需要改 Agent 代码。
- **适配器模式**：`_to_anthropic_messages()` 和 `_to_openai_messages()` 将统一的 `LLMMessage` 转换为各家 API 的格式。Anthropic 的 system prompt 要单独传（不在 messages 数组里），tool result 用 `tool_result` content block；OpenAI 用 `role="tool"` + `tool_call_id`。这些差异被适配器完全屏蔽。
- **文本解析 fallback**（`llm/openai_compat.py:252-355`）：这是为 DeepSeek R1 等不支持 function calling 的模型设计的。核心思路是：在 system prompt 末尾注入工具描述和格式要求（`{"tool": "xxx", "params": {...}}`），然后用正则从 LLM 文本输出中提取 JSON 块。这展示了你在 LLM 能力边界上的工程创造力。

**面试时你可以这样讲**：

> "我做 LLM 后端层的核心理念是：**Agent 不应该感知它用的是哪个模型**。所以我定义了一个统一的 `LLMResponse` 数据类——不管底层是 Anthropic 的 `tool_use` block 还是 OpenAI 的 `tool_calls`，最终都解析成 `Action(thought=..., tool_call=...)` 这个结构。更有意思的是针对 DeepSeek R1 这种不支持 function calling 的模型，我做了一个文本降级方案：把工具描述注入 system prompt，让模型输出 JSON，然后用正则把 JSON 抠出来。这个方案很 hacky，但在 R1 这种强推理模型上效果出奇地好——因为 R1 本来就更擅长遵循文本格式指令。"

---

### 第三条：上下文管理与可测试性工程

**"基于优先级的 Token 预算分配器"**

这是 Agent 开发中**最被低估的难点**。很多 Agent 项目简单地用 `messages[-N:]` 截断，但这会丢掉可能最关键的首条任务描述或是最近的错误信息。

- **优先级队列**：`context/token_budget.py:119-130`。15% 预留作为安全边际，剩下的按优先级分配——System 指令（最低弹性，不裁剪）→ Repo-map → History（最大份额，最有弹性）→ Observation。
- **双层裁剪**：先按**消息条数**（`ConversationHistory._trim()`，保留首条 + 最近 N 条），再按 **Token 数量**（`TokenBudget.trim_history()`，从旧到新贪婪选择）。两个维度互补——条数裁剪防止历史无限增长，Token 裁剪防止单条巨长 observation 撑爆窗口。
- **tiktoken fallback**：优先使用 `tiktoken` 的 `cl100k_base` 做精确计数；环境受限时降级为 `字符数 // 4`。误差 <15%，用于预算控制已足够。这里展示了你对"工程中优雅降级"的理解。

**"tree-sitter 仓库地图生成"**

- **技术选型**：为什么用 tree-sitter 而不是正则？正则能匹配 `def foo()` 但无法处理嵌套、装饰器、异步函数。tree-sitter 给你的是真实 AST，能精确区分 `def method(self)`（在 class 内部，indent > 0）和 `def top_level()`（indent == 0）。
- **多语言注册表**：`context/repo_map.py:39-50` 的 `_LANG_REGISTRY`。运行时按需 `importlib.import_module`，未安装的语言静默降级为正则。新增语言只需加一行字典。
- **重要性评分**（`context/repo_map.py:143-146`）：`评分 = 顶层符号数 - 文件大小/10000`。小文件 + 多顶层符号 = 高优先级，合理——这些通常是核心模块（如 `main.py`、`api.py`），而大文件往往是自动生成或数据文件。
- **与 Aider 的差异**：Aider 在每次编辑后增量更新 repo-map，Forge Agent 在整个 run 内缓存一次。这不是偷懒，而是针对不同场景的设计权衡——Aider 的结对编程场景中频繁小改需要即时反馈，而 Agent 的"给定 Issue→产出 Patch"场景中探索范围通常不超出初始地图。

**"MockBackend 测试架构"**

这是整个项目**最值得在面试中展开讲的技术决策**。

核心矛盾：**Agent 系统的核心逻辑（状态转换、错误处理、Reflection 触发）依赖于 LLM 的输出，但 LLM 是非确定性的（即使 temperature=0），且调用有延迟和费用。如何测试？**

- **解决思路**：依赖倒置。`Agent` 依赖抽象 `LLMBackend` 而非具体实现。生产环境注入 `AnthropicBackend`（调真实 API），测试环境注入 `MockBackend`（按剧本返回）。
- **MockBackend 实现**（`llm/base.py:133-201`）：维护一个 `script: list[Action]` 队列和一个 `_index` 播放指针。每次 `complete()` 返回队列中的下一个 Action，脚本用尽自动返回 `GIVE_UP`。同时记录 `call_count` 和 `received_messages` 供测试断言。
- **为什么 376 个测试能在 20 秒内跑完**：真实 LLM 每次调用 1-10 秒，MockBackend 是纯内存操作（~微秒级）。376 个测试 × 平均 3 步 × 0 秒 LLM 延迟 = 只有文件 I/O 和 subprocess 的时间。如果没有 MockBackend，376 个测试至少需要 30-60 分钟。
- **可以精确测试什么**：
  - Agent 看到 `FINISH` action 时是否正确返回 `RunResult(status=SUCCESS, patch=...)`？
  - 连续 3 步相同的 `(shell, {"cmd": "echo hi"})` 是否触发死循环熔断？
  - 测试失败后下一次 LLM 调用收到的 messages 中是否包含 `REFLECTION` 字样？
  - LLM 返回一个不存在的工具名时，Agent 是崩溃还是优雅降级？

**面试时你可以这样讲**：

> "测试 AI Agent 最难的地方在于：核心逻辑的正确性依赖于 LLM 的输出，但 LLM 输出是非确定性的。我的解决方案是做了一个架构上的依赖倒置——Agent 不直接调 LLM，而是依赖一个 `LLMBackend` 抽象接口。测试时注入 `MockBackend`，它是一个'剧本播放器'：你给它预设一个 Action 列表，它按队列顺序逐个返回，就像 puppeteer 操控木偶一样操控 Agent 的行为路径。这样一来，Agent 的 6 种终止状态、2 种 Reflection 触发、死循环检测、多轮对话的历史传递 —— 全都可以被转化为'给定输入序列 → 验证输出序列'的确定性单元测试。376 个测试跑完只需要约 20 秒，没有任何 API 费用。这也是为什么我对 Agent 系统的可测试性特别关注——如果你每次改一行代码都要等几分钟让真实 LLM 跑一遍，开发效率会低到不可接受。"

---

## 三、面试官高频追问 & 回答策略

### Q1："你说融合了 SWE-agent 和 Aider，具体怎么融合的？"

**考察点**：是否真正理解两个项目的核心机制，还是只看了表面。

**回答策略**：
> "融合点在 Agent 主循环的 `_build_messages()` 方法。SWE-agent 的贡献是 ReAct 循环结构——每一步组装 `system + history + tools` → 调 LLM → 拿到 Action → 执行 → 观察结果 → 写入历史 → 下一轮。Aider 的贡献是 repo-map——用 tree-sitter 扫描整个仓库的符号（函数/类/方法），生成一个结构化的仓库摘要。我把 repo-map 的生成放在 `_build_messages()` 里，注入 system prompt 的 `{repo_summary}` 占位符。效果是：Agent 每一步做决策时，不仅能看到对话历史（ReAct），还能看到整个仓库的符号级结构（repo-map）。这解决了纯 ReAct 的一个盲区——LLM 在探索代码库时如果不知道有哪些文件、有哪些函数，就容易做无效的 `find_files` 和 `cat` 操作。有了 repo-map 作为'地图'，探索效率会高很多。"

### Q2："MockBackend 听起来就是写死的 if-else，有什么技术含量？"

**考察点**：是否理解 Mock/Stub/Fake 的区别，以及为什么 Mock 在 AI 系统中比传统系统中更重要。

**回答策略**：
> "MockBackend 的技术价值不在实现复杂度，而在架构决策。第一个决策是**依赖倒置**：让 Agent 依赖抽象 `LLMBackend` 而非具体的 `AnthropicBackend`。这是面向对象设计的基本原则，但在 AI 项目中很少被严格执行——很多人直接在 Agent 里 `import openai` 然后调 API。第二个决策是**认识到非确定性是 AI 系统测试的元问题**：传统软件中你可以 mock 数据库、mock 网络请求，但 LLM 的输出即使 temperature=0 也可能因为模型更新而变化。MockBackend 把'LLM 会返回什么'这个唯一的不确定因素变成完全可控的输入，剩下所有逻辑——状态转换、Reflection 触发、死循环检测、token 裁剪——都变成可精确断言的了。第三个决策是**测试性能**：没有 MockBackend，376 个测试每次都要调真实 LLM，按每步 2 秒算，跑一遍要 30-60 分钟，根本不可能做 TDD。"
>
> "如果你问'为什么不用 pytest-mock 直接 patch 掉 complete 方法'——可以，但 MockBackend 额外提供了 `call_count`（验证 LLM 被调了几次）和 `received_messages`（验证每次调用时 Agent 给 LLM 发了什么），这些是简单的 `return_value` 做不到的。"

### Q3："你的 Reflection 机制和 LangChain 的 Agent 有什么不同？"

**考察点**：是否了解主流框架的设计缺陷，能否做技术批判。

**回答策略**：
> "LangChain 的 AgentExecutor 本质上是 `while step < max_iterations: call_llm(); execute_tool()`。它只有'步数硬上限'这一种熔断。这在简单场景下够用，但有两个问题：第一，LLM 可能在第一步就陷入死循环（反复调同一个工具同样的参数），但 max_iterations 设得高的话，要等 20 步才终止，浪费大量 token；第二，LLM 可能在有效工作中途被 `max_iterations` 粗暴截断。"
>
> "我的 Reflection 机制是多维度的软熔断。测试失败时，不是简单地'失败了重来'，而是注入一个引导性的 prompt——'请仔细读错误信息，反思你最近的编辑是否正确，确认是否需要更多上下文'。连续探索不编辑时，不是直接终止，而是追问'你已经看了 6 步了，到底还缺什么？精确说出来，一步拿到，然后动手'。这两种方式都是在 LLM 可能低效但还没有完全跑飞的时候，给它一个'元认知干预'，让它自己调整方向。加上死循环检测（连续 N 步相同 Action 直接熔断），形成了'软引导 + 硬截断'的分层保护。"

### Q4："repo-map 生成用的是 tree-sitter，为什么不直接用 grep 或者 LLM 自己探索？"

**考察点**：对 repo-map 价值的技术理解。

**回答策略**：
> "Grep 和 LLM 自主探索的问题是**效率**和**视野盲区**。假设一个项目有 200 个文件，Agent 要找一个特定函数。纯探索路径：`find_files *.py`（50 个结果）→ `file_read main.py`（太长截断）→ `search_text pattern='def'`（200 个匹配）→ ... 至少 5-8 步才能建立基本认识。而 repo-map 在第一步就把所有符号（函数/类/方法，带行号和所属文件）压缩成一段 8000 token 的摘要注入 system prompt，Agent 从一开始就知道有哪些模块、每个模块有哪些入口。这 5-8 步的探索被一步替代了。"
>
> "Tree-sitter 相比正则的优势在于 AST 级别的精确性。正则在 Python 里能匹配 `def foo()`，但对于 `@decorator\ndef foo()` 可能误判，对于 class 内部的 `def method(self)` 无法区分是顶层函数还是方法。Tree-sitter 给你的是完整的 AST——你能拿到节点的类型（`function_definition` vs `method_definition`）、缩进深度（indent=0 是顶层，>0 是嵌套）、行号。这些精确信息让 Agent 知道'哪个是模块入口函数，哪个是类的内部方法'，做决策时更精准。"

### Q5："你的上下文管理方案有哪些局限性？"

**考察点**：是否有技术自省能力，能否诚实地分析自己设计的弱点。

**回答策略**：
> "有几个明显的局限。第一，目前 repo-map 只缓存一次，如果 Agent 在运行中创建了新文件或大幅重构了代码，repo-map 不会更新。Aider 通过在每次编辑后增量更新来解决这个问题，但我没有做——主要因为我们场景中 Agent 是'修复一个特定 issue'，不太会大规模重构导致地图完全过时。第二，Token 预算分配的比例（10/15/50/25）是基于经验的硬编码，没有根据不同任务动态调整——比如探索型任务可能需要更多 repo_map 预算，修复型任务可能需要更多 history 预算。第三，history 裁剪是从旧到新贪婪选择的，但如果中间有一条消息包含了关键的错误信息，它可能被裁剪掉而保留了一条不那么重要但更新的消息。可以改进为基于信息密度的裁剪——保留包含错误堆栈、测试失败摘要等高信息量消息，优先丢弃纯探索的 ls/grep 输出。"
>
> "还有一点，目前所有的 token 计数都基于 tiktoken 的 `cl100k_base` 编码，这主要是为 OpenAI 优化的。对于 DeepSeek 或 Claude 模型，实际的 token 化方式可能略有不同，导致预算控制有一定误差。不过对于窗口管理这种'粗粒度'任务来说，这个误差是可以容忍的。"

### Q6："如果你要把这个项目变成生产级的，你会做什么？"

**考察点**：工程成熟度意识。

**回答策略**：
> "有三个方向。第一个是**可靠性**。目前 LLM 调用失败的重试只处理网络错误，但如果 LLM 返回了格式错误的 JSON（function calling 的 arguments 解析失败），目前的处理比较粗糙。应该加一层**输出校验**——解析失败的响应不要直接丢弃，而是把错误信息反馈给 LLM 让它重试（类似 OpenAI 的 `tool_choice` 的 `required` 模式）。"
>
> "第二个是**可观测性**。目前 EventLog 是 JSONL 文件，这对单机调试够用但生产环境不够。应该把 event stream 接入结构化日志系统（如 OpenTelemetry），每条 event 带上 trace_id。这样当 Agent 在某个用户的任务上表现异常时，可以回溯完整的推理链。"
>
> "第三个是**安全**。目前的三层安全（黑名单/白名单/确认）足够防止明显的破坏性操作，但没有做**行为异常检测**——比如 Agent 突然开始大量修改与任务无关的文件、或者执行的 shell 命令数量和复杂度突然飙升。可以考虑给 Agent 的操作空间加一个'安全预算'——每个工具调用都有一个风险评分，累计超过阈值时暂停并要求用户确认。"

### Q7："你提到借鉴了 Aider 的 repo-map，但你的实现更简单。简化了什么？为什么？"

**考察点**：是否理解简化背后的工程权衡。

**回答策略**：
> "Aider 的 repo-map 有几个我做简化或省略的特性。第一，Aider 使用了一个基于 PageRank 的排序算法——文件之间的 import 关系被建模为图，被高频引用的文件排名更高。我用的是一种更简单的启发式——`顶层符号数 - 文件大小/10000`。这个简化的原因是 PageRank 需要解析 import 语句并构建依赖图，对于 8 种语言要做 8 种解析，ROI 太低。简单启发式在实践中效果够用——小文件且多顶层符号的通常是核心模块。"
>
> "第二，Aider 在每次文件修改后增量更新 repo-map——只重新解析被修改的文件和引用它的文件。我选择在整个 run 内缓存一次。这个取舍基于场景差异：Aider 是结对编程工具，用户可能连续做 20 个小修改，每次都需要最新的地图；而 Agent 是'给定一个 issue，产出 patch'，整个过程中代码结构不太会发生根本性变化。如果未来 Agent 的 step 数上限从 40 提升到 200，这个缓存策略可能需要重新考虑。"

---

## 四、面试话术速记卡

| 你要展示的能力 | 关键词 | 怎么自然引出 |
|-------------|--------|------------|
| Agent 架构设计 | ReAct、状态机、Reflection | "我设计的主循环不是一个黑盒 while 循环，而是有明确状态转换的..." |
| 设计模式 | 策略模式、依赖倒置、适配器 | "我故意让 Agent 只依赖抽象接口，这样..." |
| LLM 能力边界 | function calling 兼容性、文本 fallback | "DeepSeek R1 不支持 function calling，我的处理方式是..." |
| 测试工程 | MockBackend、确定性测试 | "AI 系统测试的核心矛盾是 LLM 的非确定性，我的解决方案是..." |
| 上下文管理 | Token 预算、优先级裁剪 | "上下文窗口不是简单的 FIFO 截断，而是一个预算分配问题..." |
| 工程权衡 | 简化 vs 完善、场景适配 | "Aider 的 PageRank 排序我没做，因为..." |

---

> **核心建议**：面试中不要被动等待提问，而是在讲完项目后主动说——"这个项目我最想展开讲的有两个点：一是 MockBackend 的测试架构，它解决了非确定性 AI 系统的测试难题；二是 Reflection 机制，它是比简单 max_steps 截断更智能的 Agent 行为控制方案。你想先听哪个？"——这展示了你的技术判断力和叙事掌控力。
