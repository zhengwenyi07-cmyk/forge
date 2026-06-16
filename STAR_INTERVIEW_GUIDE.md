# Forge Agent — STAR 法则面试深度讲解

> 本文档按 STAR 法则（Situation → Task → Action → Result）逐句拆解简历项目描述，
> 每句话背后的"是什么 / 为什么 / 怎么做 / 怎么验证"全部讲透。
> 专为 Agent 开发岗位面试准备。

---

## 目录

1. [STAR 总述：这个项目的全局叙事](#1-star-总述这个项目的全局叙事)
2. [第一段：Agent 架构与自省机制](#2-第一段agent-架构与自省机制)
   - [Situation & Task：为什么要自己写 Agent 主循环](#21-situation--task为什么要自己写-agent-主循环)
   - [Action：ReAct 循环的四阶段设计](#22-actionreact-循环的四阶段设计)
   - [Action：双重 Reflection 自省机制](#23-action双重-reflection-自省机制)
   - [Action：死循环检测与滑动窗口比对](#24-action死循环检测与滑动窗口比对)
   - [Result：三种熔断策略 vs LangChain 的硬截断](#25-result三种熔断策略-vs-langchain-的硬截断)
3. [第二段：仓库地图生成与上下文注入](#3-第二段仓库地图生成与上下文注入)
   - [Situation & Task：Agent 探索代码库的视野盲区](#31-situation--taskagent-探索代码库的视野盲区)
   - [Action：tree-sitter 多语言符号提取](#32-actiontree-sitter-多语言符号提取)
   - [Action：从 AST 到重要性评分到预算截断的完整 Pipeline](#33-action从-ast-到重要性评分到预算截断的完整-pipeline)
   - [Action：将 repo-map 注入 ReAct 循环](#34-action将-repo-map-注入-react-循环)
   - [Result：每一步决策都有全局地图](#35-result每一步决策都有全局地图)
4. [第三段：上下文管理与可测试性工程](#4-第三段上下文管理与可测试性工程)
   - [Situation & Task：两个工程难题](#41-situation--task两个工程难题)
   - [Action：Token 预算 + 双层裁剪的上下文管理](#42-actiontoken-预算--双层裁剪的上下文管理)
   - [Action：MockBackend——如何测试非确定性 AI 系统](#43-actionmockbackend如何测试非确定性-ai-系统)
   - [Action：376 个测试的构成与设计逻辑](#44-action376-个测试的构成与设计逻辑)
   - [Action：EventLog 可观测性设计](#45-actioneventlog-可观测性设计)
   - [Result：每段声明都有精确的测试覆盖](#46-result每段声明都有精确的测试覆盖)
5. [附录：简历每句 → STAR 自查清单](#5-附录简历每句--star-自查清单)

---

## 1. STAR 总述：这个项目的全局叙事

**Situation（背景）**：当前主流的 AI Agent 开发框架（LangChain、AutoGPT）提供了封装好的黑盒循环，
但存在三个核心痛点：(1) 循环逻辑不透明，出错时难以定位根因；(2) Agent 陷入低效行为时只有硬截断
一种保护手段；(3) LLM 的非确定性使得系统难以进行确定性的单元测试。

**Task（目标）**：从零构建一个架构清晰、行为可观测、逻辑可测试的自主编程 Agent，
将 ReAct 循环的每个阶段显式化，并引入自省机制和 MockBackend 测试策略。

**Action（行动）**：实现了三个核心模块：
1. **Agent 主循环 + 双重 Reflection**：将 `_build_messages()`→`_call_with_retry()`→`execute_tool()`→写回
   拆分为独立阶段，在循环中嵌入三种自省/熔断策略
2. **Repo-map 上下文注入**：借鉴 Aider 的 tree-sitter 路线，独立实现了 8 语言符号提取 +
   重要性排序 + Token 预算截断的完整 pipeline
3. **MockBackend 测试架构**：用依赖倒置将 LLM 调用抽象为接口，测试环境注入
   预设 Action 脚本，376 个测试 20 秒完成

**Result（成果）**：Agent 的 6 种终止路径、2 种 Reflection 条件、4 种上下文裁剪策略、
多轮对话历史传递全部有精确的自动化测试覆盖。测试套件零外部依赖，可在任何环境瞬间运行。

---

## 2. 第一段：Agent 架构与自省机制

### 2.1 Situation & Task：为什么要自己写 Agent 主循环

**Situation**：我研究了 SWE-agent（Princeton NLP 的自主编程 Agent）和 LangChain 的 `AgentExecutor`。
它们的核心循环本质上都是 `while step < max_steps: call_llm(); exec_tool()`。这有两个问题：

- **不透明**：LLM 为什么选择了某个工具？Observation 是怎么被注入下一轮决策的？所有逻辑藏在框架内部，
  出了问题只能翻源码。
- **保护单一**：唯一的熔断机制是 `max_steps`（步数硬上限）。这种一刀切的方式要么在 Agent 还在
  有效工作时就终止，要么等它彻底跑飞了才发现——死了 15 步才发现它一直在重复同一个操作。

**Task**：设计一个自主编程 Agent 的主循环，要求：
1. 循环的每一个阶段都是显式的、可定位的函数
2. 有比 `max_steps` 更智能的熔断机制
3. 每一步的决策和执行都有结构化的事件记录

### 2.2 Action：ReAct 循环的四阶段设计

我将 SWE-agent 的 ReAct（Reasoning + Acting）范式拆成了四个独立函数，全部在 `agent/core.py` 中：

```
for step in range(1, max_steps + 1):
    ┌─────────────────────────────────────────────┐
    │ 阶段 1: _build_messages()                   │
    │ 组装 LLM 输入 = system prompt               │
    │   + repo-map 摘要 + 裁剪后的对话历史         │
    │   + 可用工具列表                             │
    ├─────────────────────────────────────────────┤
    │ 阶段 2: _call_with_retry()                  │
    │ 调用 LLM（带指数退避重试，最多 3 次）        │
    │ 不重试 401/403/400 等客户端错误             │
    │ 返回 LLMResponse（含 Action）                │
    ├─────────────────────────────────────────────┤
    │ 阶段 3: execute_tool()                      │
    │ 拿到 LLM 的 Action → 查 ToolRegistry        │
    │ → 执行工具 → 得到 Observation                │
    ├─────────────────────────────────────────────┤
    │ 阶段 4: _format_*_for_history()             │
    │ 将 Action + Observation 格式化为对话消息     │
    │ 写入 ConversationHistory（供下一轮使用）     │
    └─────────────────────────────────────────────┘
```

**为什么这样设计**：每个阶段是可独立测试的。比如我可以在不启动整个 Agent 的情况下单独测
`_build_messages()` 是否正确注入了 repo-map、`_call_with_retry()` 在 401 错误时是否正确
跳过了重试。这与 LangChain 把逻辑全塞在 `_take_next_step()` 里的做法有本质区别。

**代码位置**：`agent/core.py:95-269`

```cmd
:: 验证：四个阶段都有独立方法
python qfind.py "agent/core.py" "_build_messages"
python qfind.py "agent/core.py" "_call_with_retry"
python qfind.py "agent/core.py" "execute_tool"
python qfind.py "agent/core.py" "_format_action_for_history"
```

### 2.3 Action：双重 Reflection 自省机制

这是相对于 SWE-agent 和 LangChain 的**原创设计**。Reflection 的灵感来自 Daniel Kahneman 的
"系统 2 思维"——当 Agent（系统 1，快速直觉）陷入局部最优时，系统主动注入反思提示，
强制进入元认知模式（系统 2，慢速分析）。

#### 触发条件 A：测试失败（`agent/core.py:228-238`）

**为什么需要**：LLM 面对测试失败时，天然倾向于"再试一次相同策略"。比如它改了一行代码后测试失败，
下一轮的 thought 经常是 "Let me try again" 然后重复同样的修改。如果不干预，可能连试 8-10 步。

**怎么做的**：当 LLM 调用名为 `test` 或 `pytest` 的工具且返回 `status=ERROR` 时，系统自动
向对话历史注入一段引导性 prompt（`agent/prompt.py:96-103`）：

```
[REFLECTION] The tests just failed. Before your next action, consider:
1. Read the full error message above carefully — what is the root cause?
2. Is your last edit correct? Did it introduce a new bug?
3. Do you need to look at more context before editing again?

Be specific about what you will do differently. What is your next action?
```

**要点**：这不是简单的"失败了重来"，而是引导 LLM 做三件事：(a) 读错误信息 → (b) 反思最近的编辑
→ (c) 判断是否需要更多上下文。这在 prompt 设计上叫"引导性元认知（guided metacognition）"。

#### 触发条件 B：连续 N 步无文件编辑（`agent/core.py:241-251`）

**为什么需要**：Agent 有时会陷入"无尽探索"模式——一直 `file_read`、`search_text`、`find_files`，
但不敢动手改代码。如果不管它，可能探索 15 步然后被 `max_steps` 硬截断。

**触发机制**：维护一个 `steps_without_edit` 计数器——每次 `TOOL_CALL` 时，如果工具名是
`file_write`（或 `file_edit`、`edit`）就重置为 0，否则 +1。当计数器 >= `reflection_no_edit_steps`
（默认 6）时注入 `REFLECTION_NO_EDIT` prompt：

```
[REFLECTION] You have taken 6 steps without editing any file.
You may be stuck in an exploration loop. Consider:
1. Do you have enough context to make a change now?
2. If yes — make the edit
3. If no — identify exactly what you still need, get it in one targeted step, then edit

What specific action will move the task forward?
```

**如何验证 Reflection 确实生效**：`TestReflectionTestFailed` 和 `TestReflectionNoEdit` 测试类中，
我不仅验证了 Reflection event 被写入 EventLog，还验证了**下一次 LLM 调用收到的 messages 中确实
包含了 REFLECTION 字样**（`tests/test_day2.py:387-414`）。这证明了 Reflection prompt 不是
"记录但不注入"的花瓶功能。

```cmd
:: 验证：Reflection 测试全部通过
pytest tests/test_day2.py -v -k "TestReflectionTestFailed or TestReflectionNoEdit" --tb=line
```

### 2.4 Action：死循环检测与滑动窗口比对

**为什么需要元组比对而不是字符串比对**：如果 LLM 两次调用 `shell`，第一次 `{"cmd": "pytest tests/"}`，
第二次 `{"cmd": "ls -la"}`，这是正常的多步操作；但如果连续 3 次调用
`shell, {"cmd": "echo hello"}`，这是在死循环。用字符串比对（"tool=shell"）会误杀前者，
用 `(tool_name, params)` 元组比对能精确区分。

**实现**（`agent/core.py:352-369`）：
```python
def _is_looping(self, log: EventLog) -> bool:
    n = self._cfg.loop_detection_window  # 默认 3
    actions = log.get_actions()
    if len(actions) < n:
        return False

    recent = actions[-n:]  # 取最近 N 条
    # 只对 TOOL_CALL 类型检测
    if not all(a.action_type == ActionType.TOOL_CALL and a.tool_call for a in recent):
        return False

    first = recent[0].tool_call
    return all(
        a.tool_call.name == first.name and a.tool_call.params == first.params
        for a in recent[1:]
    )
```

**测试如何验证**：`TestLoopDetection` 测试类包含两个 Case：
- `test_loop_detected_gives_up`：连续 3 步相同的 `(shell, {"cmd": "echo hi"})` → 正确触发熔断
- `test_different_actions_not_detected_as_loop`：`shell/pytest` → `file_read/foo.py` → `shell/pytest`
  交替出现 → 不误判为循环（因为 params 不同）

### 2.5 Result：三种熔断策略 vs LangChain 的硬截断

| 熔断策略 | 触发条件 | 行为 | 对应代码 |
|---------|---------|------|---------|
| Reflection A（测试失败） | test 工具返回 error | 注入引导性反思 prompt，LLM 自行调整 | `agent/core.py:228-238` |
| Reflection B（无编辑） | `steps_without_edit >= 6` | 注入方向反思 prompt，追问缺什么信息 | `agent/core.py:241-251` |
| 死循环熔断 | 连续 3 步 `(tool, params)` 完全一致 | 立即终止，返回 `GAVE_UP` | `agent/core.py:352-369` |
| 步数上限 | `step > max_steps` | 立即终止，返回 `MAX_STEPS` | `agent/core.py:260-269` |

对比 LangChain 的 `max_iterations`（只有步数上限一种），这形成了"软引导 + 硬截断"的分层保护。

---

## 3. 第二段：仓库地图生成与上下文注入

### 3.1 Situation & Task：Agent 探索代码库的视野盲区

**Situation**：SWE-agent 的 ReAct 循环中，Agent 在第一轮只知道"任务描述"和"工具列表"。
它的代码库认知完全靠一步一步的 `find_files` → `file_read` → `search_text` 来建立。
对于一个 200 个文件的项目，光是"知道有哪些主要模块"就需要 5-8 步探索。

Aider（Paul Gauthier 的结对编程工具）在 2023 年提出了 repo-map 方案：用 tree-sitter 在启动时
扫描整个仓库，生成一个"代码地图"注入系统 prompt，让 LLM 从第一轮就有全局视野。

**Task**：把 Aider 的 repo-map 思路集成到 ReAct 循环中，但不依赖 Aider 的具体实现。
要求：(1) 支持多种语言 (2) tree-sitter 不可用时优雅降级 (3) 按重要性排序
(4) 有 Token 预算控制 (5) 在 ReAct 循环的消息组装阶段注入。

### 3.2 Action：tree-sitter 多语言符号提取

**多语言注册表**（`context/repo_map.py:39-50`）：

```python
_LANG_REGISTRY: dict[str, tuple[str, str]] = {
    ".py":   ("tree_sitter_python",     "language"),
    ".js":   ("tree_sitter_javascript", "language"),
    ".ts":   ("tree_sitter_typescript", "language_typescript"),
    ".tsx":  ("tree_sitter_typescript", "language_tsx"),
    ".go":   ("tree_sitter_go",         "language"),
    ".rs":   ("tree_sitter_rust",       "language"),
    ".java": ("tree_sitter_java",       "language"),
    ".cpp":  ("tree_sitter_cpp",        "language"),
    ".c":    ("tree_sitter_c",          "language"),
    ".rb":   ("tree_sitter_ruby",       "language"),
}
```

**为什么是这个设计**：(1) 新增语言只需加一行字典 (2) 运行时按需 `importlib.import_module`，
不安装不报错，静默降级为正则 (3) 每种语言映射到其 Python 包名和导出属性名，
一个 `_get_language()` 函数统一处理所有语言。

**运行时按需加载 + 降级**（`context/repo_map.py:87-111`）：

```python
def _get_language(ext: str):
    if ext in _lang_cache:
        return _lang_cache[ext]  # 已加载，直接返回缓存

    entry = _LANG_REGISTRY.get(ext)
    if entry is None:
        _lang_cache[ext] = None  # 不支持的语言
        return None

    module_name, attr_name = entry
    try:
        import importlib
        from tree_sitter import Language
        mod = importlib.import_module(module_name)
        lang_fn = getattr(mod, attr_name)
        lang = Language(lang_fn())
        _lang_cache[ext] = lang
        return lang
    except Exception:
        _lang_cache[ext] = None  # 安装失败 → 静默降级
        return None
```

上层调用 `_extract_symbols()` 在拿到 `None` 时自动走 `_extract_symbols_regex()` 正则路径。

### 3.3 Action：从 AST 到重要性评分到预算截断的完整 Pipeline

整个 pipeline 在 `RepoMap.build()` 中串联：

```
RepoMap.build(budget=8000)
  │
  ├─ _scan()
  │   ├─ 递归遍历所有文件（跳过 .git/__pycache__/node_modules 等）
  │   ├─ 跳过 >500KB 的大文件
  │   └─ 对每个源文件调用 _extract_symbols()
  │       ├─ tree-sitter 可用 → _walk_tree() 递归遍历 AST
  │       │   ├─ 匹配 _FUNC_NODES（9 种函数/方法节点类型）
  │       │   ├─ 匹配 _CLASS_NODES（5 种类/结构体/接口节点类型）
  │       │   └─ indent = node.start_point[1]（缩进列号）
  │       └─ tree-sitter 不可用 → _extract_symbols_regex() 正则匹配
  │           └─ 正则: r"^[ \t]*(def|class|function|func|fn|...)\s+(\w+)"
  │
  ├─ 按重要性降序排列
  │   └─ importance_score() = 顶层符号数 - 文件大小 / 10000
  │       例: 一个小型 main.py (500B, 3 个顶层函数) = 3 - 0.05 = 2.95 分
  │           一个大型 data.py (50KB, 0 个符号) = 0 - 5.0 = -5 分
  │           → main.py 排在前面，data.py 排在后面甚至被截断
  │
  ├─ 按 Token 预算截断
  │   └─ max_chars = budget * 4（Token → 字符估算）
  │      逐个加入文件摘要，超出时标记 "... (N more files not shown)"
  │
  └─ 输出格式化字符串
      └─ 按文件分行，缩进表示嵌套层级
```

**重要性评分为什么这么设计**：

顶层符号数高 → 说明文件定义了大量公开接口（如 `main.py`、`api.py`、`core.py`），这些通常是理解项目的关键入口。

文件大小惩罚 → 大文件往往是自动生成的（如 `migrations/`）、数据文件、或者过于庞大的模块。
LLM 的上下文窗口有限，应该优先展示"小而精"的核心文件。

公式 `top_level - size/10000` 让这两个因素在同一量级竞争——一个 10KB 的文件需要 1 个顶层符号才能得正分，
而一个 500B 的文件只需要 0.05 个符号就能得正分。参数 10000 是从实验中凭经验选的，
不是拍脑袋凑出来的——我们验证了在 200+ 个文件的项目中，排名靠前的确实是 `main.py`、`core.py`、`app.py` 这类核心模块。

**为什么用缩进区分 function vs method**：

正则能匹配 `def foo()` 但无法知道它在 `class Bar:` 内部还是顶层。
tree-sitter 的 AST 提供 `node.start_point[1]`（起始列号）。
indent == 0 → 顶层函数（`function`），indent > 0 → 类内部方法（`method`）。
这个区分为 LLM 提供了关键的语义信息——"`run()` 是顶层入口，`_build_messages()` 是 Agent 类的内部方法"。

### 3.4 Action：将 repo-map 注入 ReAct 循环

**注入位置**：`agent/core.py:_build_messages()` 方法（第 275 行）。

这是 ReAct 与 repo-map **融合的关键点**。在每一步 LLM 调用的消息组装阶段：

```python
def _build_messages(self, history, token_budget, repo_map):
    # 1. 生成 repo-map（带缓存：整个 run 只构建一次）
    if not hasattr(self, "_repo_map_cache"):
        self._repo_map_cache = repo_map.build(
            budget=token_budget.default_plan().repo_map
        )

    # 2. 渲染 system prompt（repo-map 注入 {repo_summary} 占位符）
    system_content = build_system_prompt(
        repo_path="...",
        tools=schemas,
        repo_summary=self._repo_map_cache,  # ← repo-map 在这里注入
    )

    # 3. 裁剪历史 + 组装完整 messages
    trimmed_history = token_budget.trim_history(history, budget)
    messages = [LLMMessage(role="system", content=system_content)]
    messages += [LLMMessage(role=d["role"], content=d["content"]) for d in trimmed_history]
    return messages
```

**System Prompt 中的占位符**（`agent/prompt.py:45-51`）：

```
## Repository
Path: {repo_path}
{repo_summary}           ← repo-map 注入位置

## Available tools
{tool_descriptions}
```

**缓存策略**：repo-map 在同一次 `run()` 内只构建一次。跨 repo 切换时自动失效（`_repo_map_cache_key` 比对）。
这与 Aider 的"每次编辑后增量更新"不同——Aider 是结对编程工具，用户频繁小修改需要即时反馈；
而 Agent 是"给定 Issue → 产出 Patch"，整个过程中的代码结构不太会发生根本性变化。
这是一个**场景驱动的工程权衡**，不是"因为实现简单"而省略。

### 3.5 Result：每一步决策都有全局地图

融合后的效果：Agent 在第一步就能从 system prompt 看到类似这样的信息：

```
agent/core.py (7 symbols):
  class AgentConfig (line 50)
  class Agent (line 72)
  function run (line 95)
    method _build_messages (line 275)
    method _is_looping (line 352)
    method _call_with_retry (line 375)
    method _get_git_diff (line 420)

llm/base.py (5 symbols):
  class LLMBackend (line 68)
  class MockBackend (line 133)
  class StreamingMixin (line 249)

tools/base.py (6 symbols):
  class BaseTool (line 53)
  class ToolRegistry (line 108)
  class NoopTool (line 176)
  class FailingTool (line 212)
```

Agent 不需要先 `find_files *.py`（50 个结果）→ `file_read main.py`（太长截断）→ `search_text def`
（200 个匹配）……来建立基本认知。repo-map 在第一步就把全仓库的符号级结构"免费"给了 LLM。
这 5-8 步的探索成本被**一步**替代了。

---

## 4. 第三段：上下文管理与可测试性工程

### 4.1 Situation & Task：两个工程难题

**难题 1——上下文窗口管理**：LLM 的上下文窗口有硬上限（8K-128K token），而 Agent 在运行中会产生
大量历史消息（system prompt + 任务描述 + 多步 Action/Observation + Reflection prompt + 工具输出）。
简单的 `messages[-N:]` 截断会丢掉最关键的**首条任务描述**和**最近的错误信息**。

**难题 2——非确定性 AI 系统的测试**：Agent 的核心逻辑（状态转换、Reflection 触发、死循环检测）
依赖于 LLM 的输出，但 LLM 是非确定性的（即使 temperature=0），且每次 API 调用有 1-10 秒延迟和费用。
如果每个测试都要调真实 LLM，376 个测试需要 30-60 分钟，根本无法做 TDD。

**Task**：(1) 设计一个"知道什么该留、什么该丢"的上下文裁剪系统
(2) 设计一个测试架构，让 Agent 的所有逻辑路径可以被确定性地、瞬时地验证。

### 4.2 Action：Token 预算 + 双层裁剪的上下文管理

**第一层：ConversationHistory 滑动窗口（条数维度）**

`context/history.py:81-88`：

```
策略：保留 index 0（任务描述，永不丢弃）+ 最近 (max_messages - 1) 条消息
超出时从 index 1 开始弹出旧消息（任务描述永远在 index 0，不会被 pop(0)）
```

**为什么任务描述永不丢弃**：Agent 在运行 20 步后，原始的"修复 test_parser.py 中失败的测试"这条
消息可能已经被裁剪掉了。如果 LLM 不记得原始任务，它可能会做出与任务无关的修改。

**第二层：TokenBudget 二次裁剪（Token 数量维度）**

`context/token_budget.py:144-184`：

```
策略：
1. 计算每条消息的 token 估算值
2. 保留第一条（任务描述）
3. 从最新消息开始向前贪婪选择——每选一条，从剩余预算中扣除其 token 数
4. 预算耗尽后停止，丢弃的消息数量记录在一条截断提示中：
   "[N earlier messages were truncated to fit context window]"
```

**为什么是"从新到旧贪婪选择"而不是"从旧到新"**：最近的 Observation 对 Agent 下一步决策最重要——
它刚执行了哪个工具、看到了什么结果。如果优先保留旧消息，Agent 可能在关键时刻丢失了"上一步的
测试输出显示 AssertionError line 42"这条关键信息。

**优先级预算分配**：

| 部分 | 预算比例 | 优先级 | 裁剪策略 |
|------|:--:|:--:|---------|
| System 指令 | 10% | 最高 | 永不裁剪 |
| Repo-map | 15% | 高 | 超出时缩减文件数量 |
| History | 50% | 中 | 从旧到新贪婪裁剪 |
| Observation | 25% | 中高 | 截断单条超长输出 |
| 安全预留 | 15% | — | 缓冲余量 |

**tiktoken fallback**：优先 `tiktoken.get_encoding("cl100k_base")` 精确计数；
如果 tiktoken 未安装（网络受限、离线环境），降级为 `len(text) // 4` 字符估算。
对于 Token 预算控制这种"粗粒度"任务，<15% 的误差完全可接受。

### 4.3 Action：MockBackend——如何测试非确定性 AI 系统

**核心洞察**：Agent 的核心逻辑（状态转换、Reflection 触发、死循环检测、Token 裁剪）
不依赖于 LLM 的"智能"，而依赖于 LLM **返回了什么**。只要 MockBackend 返回的 `LLMResponse`
与真实 Backend 格式完全一致，Agent 的行为就是完全确定的。

**依赖倒置设计**：

```
Agent 依赖 LLMBackend（抽象接口），不是 AnthropicBackend（具体实现）
         ↓
    ┌────────────┐     ┌──────────────────┐
    │ 生产环境    │     │ 测试环境           │
    │ Agent      │     │ Agent            │
    │   ↓        │     │   ↓              │
    │ Anthropic..│     │ MockBackend      │
    │ Real LLM   │     │ Script Queue     │
    └────────────┘     └──────────────────┘
```

**MockBackend 实现**（`llm/base.py:133-201`）：

```python
class MockBackend(LLMBackend):
    def __init__(self, script: list[Action], input_tokens=100, output_tokens=50):
        self._script = script      # 预设 Action 列表（"剧本"）
        self._index = 0            # 当前"播放头"
        self.call_count = 0        # 被调用了几次（供断言）
        self.received_messages = []  # 每次收到的完整消息列表（供断言）

    def complete(self, messages, tools) -> LLMResponse:
        self.call_count += 1
        self.received_messages.append(messages)  # 保存请求，事后可审计

        if self._index < len(self._script):
            action = self._script[self._index]  # 按剧本返回
            self._index += 1
        else:
            action = Action(GIVE_UP, "Script exhausted.")  # 剧本用尽，安全兜底

        return LLMResponse(action=action, input_tokens=..., output_tokens=...)
```

**MockBackend 解决了什么**：

| 对比维度 | 真实 LLM | MockBackend |
|---------|---------|-------------|
| 单次调用 | 1-10 秒（网络+推理） | ~0ms（纯内存） |
| 确定性 | temperature=0 仍有细微差异 | 100% 确定 |
| 路径控制 | 无法精确控制 LLM 选哪个工具 | 可精确测试每条代码路径 |
| 费用 | 每次调用计费 | $0 |
| 并发 | 受 API rate limit | 无限 |

**配合 NoopTool 和 FailingTool 实现精准测试**：

- **NoopTool**（`tools/base.py:176`）：`execute()` 始终返回 `ToolResult(success=True, output="ok")`，
  记录每次调用的 `call_count` 和 `last_params`。用于一般的 Agent 流程测试。
- **FailingTool**（`tools/base.py:212`）：`execute()` 始终返回 `ToolResult(success=False, error="AssertionError")`，
  命名为 `test` 后注册，可**精确触发** Reflection A（测试失败反射）。

### 4.4 Action：376 个测试的构成与设计逻辑

**测试分布**：

| 测试文件 | 测试数（约） | 覆盖范围 | Mock 策略 |
|---------|:--:|---------|----------|
| `test_day1.py` | 30 | 数据模型序列化、EventLog 写入/replay | 无 mock（纯数据） |
| `test_day2.py` | 50 | **Agent 6 种终止路径 + 2 种 Reflection + 死循环** | MockBackend + NoopTool/FailingTool |
| `test_day3.py` | 60 | 工具层（文件/Shell/搜索/pytest/Git）| 真实文件系统 + subprocess |
| `test_day4.py` | 50 | LLM Router + Backend 格式解析 + Prompt 模板 | `unittest.mock.patch` SDK |
| `test_day5.py` | 55 | RepoMap + TokenBudget + History + 集成 | MockBackend + 真实文件 |
| `test_day6.py` | 35 | Config + CLI（Click test runner）| MockBackend + patch Config |
| `test_day7.py` | 40 | 重试逻辑 + git diff + 缓存隔离 + 端到端 | MockBackend + 真实 git |
| `test_stream.py` | 25 | 流式输出 + StreamCallback | MockBackend + 自定义 stream() |
| `test_sandbox.py` | 30 | LocalRuntime + DockerRuntime | 真实 subprocess + mock docker |
| `test_chat.py` | 20 | ChatSession 多轮对话 + History 跨轮 | MockBackend |
| `test_confirm.py` | 25 | Shell 安全层 + 确认回调 | 真实 ShellTool + callback |

**我是怎么想到这些测试的——测试设计方法论**：

1. **状态机覆盖**：Agent 主循环是一个有限状态机。我枚举了所有可能的终止状态
   （`RunStatus` 枚举中的 SUCCESS/FAILED/MAX_STEPS/GAVE_UP），每个状态写一个测试类。
   这不是"我先写代码再写测试"，而是**先定义状态机，再为每个状态写测试，最后写实现代码**——
   标准的 State-Driven Testing。

2. **边界条件枚举**：对于 Reflection，我枚举了两种触发条件（test_failed / no_edit），
   每种写两个 Case：一个验证 event 被记录，一个验证 prompt 被注入到下一轮 messages。
   对于死循环，写了正向（检测到循环）和反向（不同操作不误判）两个 Case。

3. **分层 Mock**：核心逻辑（Agent.run、ReAct）用 MockBackend 完全模拟；
   工具层（Shell、File）用真实文件系统和 subprocess——工具的价值就在于真实执行；
   LLM SDK 层用 `unittest.mock.patch`——验证格式转换正确性。

4. **集成测试**：`test_day5.py` 和 `test_day7.py` 有 10+ 个集成测试，
   验证 repo_map + token_budget + reflection + retry 全部串联正常。
   这是关键——单元测试只保证每个模块对，集成测试保证它们**一起对**。

**为什么 376 个测试能在 20 秒内完成**：

真实 LLM API 调用 1-10 秒/次，376 个测试 × 平均 3 步 × 2s（保守估计）= **约 38 分钟**。
MockBackend 的 `complete()` 是纯内存操作（~微秒级），376 个测试的 LLM 调用时间 ≈ 0 秒。
剩下的 20 秒全部是真实的文件 I/O、subprocess 和 git 操作。

```
376 个测试总耗时 ≈ 20 秒
├── MockBackend 调用（1128+ 次 LLM 模拟）：~0.01 秒
├── 文件 I/O（读取/写入/搜索）：~10 秒
├── subprocess（Shell/pytest/Git 工具测试）：~8 秒
└── git 操作（init/commit/diff）：~2 秒
```

### 4.5 Action：EventLog 可观测性设计

**为什么需要 EventLog**：Agent 的运行是"看不见的"——LLM 每次推理的内容、工具执行的结果、
Reflection 是否触发——如果不记录，出了问题只能猜想。

**设计**（`agent/event_log.py:32-264`）：

```
EventLog（JSONL append-only）
├── 每条 Event 写入后立即 flush()  → 崩溃不丢数据
├── 文件命名：{task_id}_{timestamp}.jsonl → 多次运行不覆盖
├── Event 类型：TASK_START / ACTION / OBSERVATION / REFLECTION / TASK_COMPLETE / TASK_FAILED
├── Action Event 包含：step + thought + tool_call + raw_content（LLM 原始输出）
├── replay()  → 完整回放全部事件
└── summarize_run() → 统计 actions/reflections/tool_calls/final_status
```

**设计要点**：
- `append-only`：只增不删，每次写入是追加一行 JSON——避免并发问题，格式极简
- `flush()` 策略：每条写入后立即 `self._file.flush()`——可能多耗 1ms，但**绝不因崩溃丢最后一条事件**
- 每条 Event 带 `event_id`（8 位 UUID），支持精确引用
- `raw_content` 字段保留 LLM 的完整原始输出——事后调试的关键

### 4.6 Result：每段声明都有精确的测试覆盖

**6 种终止路径的完整测试覆盖**：

| # | 终止路径 | RunResult.status | 测试类 | Mock LLM 返回什么？ |
|---|---------|-----------------|--------|-------------------|
| 1 | 正常完成 | `SUCCESS` | `TestAgentFinish` | `Action(FINISH, message="Done")` |
| 2 | 主动放弃 | `GAVE_UP` | `TestAgentGiveUp` | `Action(GIVE_UP, message="Too complex")` |
| 3 | 步数上限 | `MAX_STEPS` | `TestAgentMaxSteps` | 10 个 TOOL_CALL，但 `max_steps=3` |
| 4 | 死循环熔断 | `GAVE_UP` | `TestLoopDetection` | 重复 20 次的同一 TOOL_CALL |
| 5 | LLM 崩溃 | `FAILED` | `TestLLMError` | 继承 MockBackend，`complete()` 直接 `raise ConnectionError` |
| 6 | 工具不存在 | `SUCCESS` | `TestUnknownTool` | TOOL_CALL 一个不存在的工具名，Agent 应优雅降级 |

**2 种 Reflection 的完整测试覆盖**：

| # | 触发条件 | 测试类 | 如何伪造触发 |
|---|---------|--------|-------------|
| 1 | 测试失败 | `TestReflectionTestFailed` | FailingTool（命名为 `test`），`execute()` 返回 error |
| 2 | 连续无编辑 | `TestReflectionNoEdit` | 连续 6 步 NoopTool（非 `file_write`），`steps_without_edit` 累加到 6 |

```cmd
:: 一口气验证所有
pytest tests/test_day2.py -v -k "finish or give_up or max_steps or loop or reflection or llm_error or unknown" --tb=line
```

---

## 5. 附录：简历每句 → STAR 自查清单

面试前用这张表逐句过一遍，确保每句都能用 STAR 逻辑展开：

| # | 简历声明 | S（为什么需要） | T（设计目标） | A（怎么做的） | R（怎么验证） |
|---|---------|---------------|-------------|------------|------------|
| 1 | ReAct 主循环替代黑盒 AgentExecutor | 框架循环不透明，出错难定位 | 每个阶段显式化、可独立测试 | 拆分为 4 个独立方法 | 4 个方法都有独立测试覆盖 |
| 2 | 双重 Reflection 自省机制 | LLM 面对失败/探索死胡同时缺乏自省 | 软引导而非硬截断 | 注入引导性元认知 prompt | `TestReflectionTestFailed` + `TestReflectionNoEdit` 均 PASSED |
| 3 | 死循环检测 | LLM 有时会重复完全相同操作 | 精确区分"同工具不同参数"vs"完全重复" | `(tool, params)` 元组比对，非字符串 | `TestLoopDetection` 正向 + 反向均 PASSED |
| 4 | tree-sitter 多语言符号提取 | 正则无法区分顶层函数 vs 类方法，跨语言需要用不同工具 | 多语言 AST 级精度 | `_LANG_REGISTRY` 注册表 + 懒加载 + 正则降级 | `test_day5.py` repo_map 测试全 PASSED |
| 5 | 重要性评分排序 | LLM 上下文有限，不能展示所有文件 | 优先展示小而核心的文件 | `top_level - size/10000` 评分 | `test_repo_map_*.py` 验证了排序正确性 |
| 6 | 注入 System Prompt | Agent 第一步就需要全局视野 | 与 ReAct 循环无缝融合 | `_build_messages()` 中注入 | 集成测试验证 LLM 收到的 system msg 含 repo-map |
| 7 | Token 预算双层裁剪 | `messages[-N:]` 会丢掉任务描述和错误信息 | 有优先级的智能截断 | 条数维度 + Token 维度双层 | `trim_history` 测试保留首条 + 保留最近 |
| 8 | MockBackend | LLM 非确定性导致测试不可靠 | 将不确定性隔离在接口层之外 | 依赖倒置 + Action 脚本队列 | 376 个测试 20 秒完成，零失败 |
| 9 | 376 个测试 20 秒 | 每次调真实 LLM 跑不完测试 | 全覆盖 + 秒级反馈 | 10 个测试文件按状态机/边界/集成分层 | `pytest` 全量 = `376 passed in ~20s` |
| 10 | EventLog JSONL | Agent 运行不可见，需审计 | 结构化、可回放、防崩溃 | append-only + flush + replay + summarize | `test_day1.py` log 读写测试全 PASSED |

---

> **面试技巧**：当面试官追问某个技术细节时，不要背诵简历，而是先说 **Situation**（当时遇到的问题），
> 再说 **Action**（我的解决方案 + 关键代码位置），最后说 **Result**（测试怎么验证的）。
> STAR 叙事比"我用了 XX 技术"更有说服力——因为面试官听到的是**你在什么场景下做了什么工程决策**。
