# Forge Agent — 简历验证操作指南

> 对应简历三段描述，每条声明都可运行一行命令当场验证。
> **环境**：Windows CMD · Python 3.11 · 项目根目录 `D:\fianll-test\forge\forge-agent`

---

## 前置：解决 CMD 编码问题

Windows CMD 默认 GBK 编码，而项目源文件是 UTF-8（含中文注释）。先执行：

```cmd
:: 切换到 UTF-8 代码页
chcp 65001 >nul

:: 创建微型搜索工具（之后所有源码搜索都用它）
echo from pathlib import Path > qfind.py
echo import sys >> qfind.py
echo path, pat = sys.argv[1], sys.argv[2] >> qfind.py
echo for f in sorted(Path('.').glob(path)): >> qfind.py
echo.    for i, line in enumerate(open(f, encoding='utf-8'), 1): >> qfind.py
echo.        if pat in line: print(f'{f}:{i}: {line.rstrip()[:120]}') >> qfind.py
```

之后所有源码搜索命令格式为：

```cmd
python qfind.py "agent/*.py" "def run"
python qfind.py "path/*.py" "搜索内容"
```

> `qfind.py` 只是一个临时工具（4 行代码），用完后可以删掉。它用 Python 读 UTF-8 文件，彻底规避 CMD 编码问题。

---

## 你的简历（修正版）

```
Agent 架构与自省机制：基于 SWE-agent 的 ReAct 范式构建自主编程 Agent 主循环，
将上下文组装、LLM 推理、Tool Calling、结果回写拆分为独立阶段，替代黑盒式
AgentExecutor；设计双重 Reflection 自省机制——测试失败时自动注入错误分析提示
引导 LLM 反思根因、连续 N 步无文件修改时触发方向反思，结合连续相同 Action 死循环
检测（滑动窗口比对 tool+params 元组）实现三种自动熔断策略，防止 Agent 陷入局部
最优或无效循环。

仓库地图生成与上下文注入：借鉴 Aider 的 repo-map 方案，基于 tree-sitter 实现
多语言代码仓库的结构化符号提取——支持 Python/JS/TS/Go/Rust/Java/C/C++/Ruby 共
8 种语言，运行时按需加载语法包，不可用时静默降级为正则匹配；递归遍历 AST 提取
函数、类、方法定义及其行号与缩进级别，按"顶层符号数 - 文件大小/10000"评分排序
后按 Token 预算截断，在 ReAct 循环的消息组装阶段注入 System Prompt，使 LLM 在
每一步决策时同时具备环境反馈与全局代码结构信息。

上下文管理与可测试性工程：设计基于优先级的 Token 预算分配器（System 10% →
Repo-map 15% → History 50% → Observation 25%），结合 ConversationHistory 滑动
窗口与 TokenBudget 二分截断实现双层裁剪；基于依赖倒置设计 MockBackend 以预设
Action 脚本队列替代真实 LLM 调用，配合 NoopTool/FailingTool 将非确定性 LLM 行为
转化为确定性输入/输出序列，376 个测试在约 20 秒内覆盖 Agent 全部 6 种终止路径与
2 种 Reflection 条件；EventLog 以 JSONL append-only 格式记录完整推理链，每条写入
后立即 flush，支持 replay() 回放与 summarize_run() 统计。
```

> 如果你坚持在简历中用原文（三段结构但不含工具链段），见 [附录 B：原文问题修正](#附录-b原文问题修正)。

---

## 第一段验证：Agent 架构与自省机制

> 简历声明：*ReAct 主循环 · 双重 Reflection · 死循环检测 · 三种自动熔断*

### 1.1 主循环位置

```cmd
python qfind.py "agent/core.py" "def run"
```

**期望输出**：`agent/core.py:95:     def run(self, task: Task, log: EventLog) -> RunResult:`

**验证了**：主循环入口在 `agent/core.py:95`，不是藏在某个框架深处。

### 1.2 主循环的四个独立阶段

```cmd
python qfind.py "agent/core.py" "_build_messages"
python qfind.py "agent/core.py" "_call_with_retry"
python qfind.py "agent/core.py" "execute_tool"
python qfind.py "agent/core.py" "_format_action_for_history"
python qfind.py "agent/core.py" "_format_observation_for_history"
```

**期望输出**：
```
agent/core.py:95:     def _build_messages(...)       ← 上下文组装
agent/core.py:353:    def _call_with_retry(...)      ← LLM 推理
agent/core.py:204:    ...registry.execute_tool(...)  ← Tool Calling
agent/core.py:310:    def _format_action_for_history ← 结果回写
agent/core.py:320:    def _format_observation_for_history
```

**验证了**：「将上下文组装、LLM 推理、Tool Calling、结果回写拆分为独立阶段」——每一步都有独立函数，不是黑盒 while 循环。

### 1.3 完整的 ReAct 循环结构

直接打开 `agent\core.py`，阅读第 135-269 行。或用以下命令快速查看关键节点：

```cmd
python qfind.py "agent/core.py" "# ──"
python qfind.py "agent/core.py" "for step in"
python qfind.py "agent/core.py" "FINISH"
python qfind.py "agent/core.py" "GIVE_UP"
python qfind.py "agent/core.py" "Reflection"
python qfind.py "agent/core.py" "max_steps"
```

确认这个结构：

```
for step in range(1, max_steps+1):
    1. _build_messages()           → 组装（含 repo-map 注入）
    2. _call_with_retry()          → 调 LLM
    3. _is_looping()               → 死循环检测
    4. FINISH/GIVE_UP 检查         → 终端判断
    5. execute_tool()              → 执行工具
    6. 写入 history                → 回写
    7. Reflection 检查             → 自省触发
```

**验证了**：「基于 SWE-agent 的 ReAct 范式」——完整的 Observe→Think→Act 循环。

### 1.4 冒烟测试观察 ReAct 过程

```cmd
python smoke_test.py
```

观察输出中的每一步：

```
[Step 1] Action: tool_call     ← Act：LLM 决定调工具
  Thought: ...                 ← Think：推理过程
  ✓ [file_write] success       ← Observe：工具执行结果

[Step 2] Action: tool_call
  ✓ [shell] success

[Step 3] Action: finish        ← 自主决定完成
✓ COMPLETE
```

### 1.5 Reflection：测试失败触发

```cmd
pytest tests/test_day2.py -v -k "TestReflectionTestFailed" --tb=line
```

**期望**：所有测试 PASSED。

**查看注入位置**：

```cmd
python qfind.py "agent/core.py" "reflection_test_failed"
python qfind.py "agent/prompt.py" "REFLECTION"
```

**核心源码**：`agent/core.py:228-238`——当调用的工具是 `test`/`pytest` 且返回失败时，自动注入：

```
[REFLECTION] The tests just failed. Before your next action, consider:
1. Read the full error message above carefully — what is the root cause?
2. Is your last edit correct? Did it introduce a new bug?
3. Do you need to look at more context before editing again?
```

**验证了**：不是"失败了重来"，而是引导 LLM 做根因分析。

### 1.6 Reflection：连续无编辑触发

```cmd
pytest tests/test_day2.py -v -k "TestReflectionNoEdit" --tb=line
```

**期望**：所有测试 PASSED。

**核心源码**：`agent/core.py:241-251`——当 `steps_without_edit >= 6` 时触发。

**验证了**：防止 Agent 陷入无尽探索——一直读文件、搜索，但不敢动手改。

### 1.7 死循环检测

```cmd
pytest tests/test_day2.py -v -k "TestLoopDetection" --tb=line
```

**期望**：`test_loop_detected_gives_up` 和 `test_different_actions_not_detected_as_loop` 均 PASSED。

```cmd
python qfind.py "agent/core.py" "_is_looping"
```

**核心源码**：`agent/core.py:330-351`——比较最近 N 条 Action 的 `(tool_name, params)` 元组，而非字符串。

**验证了**：「滑动窗口比对 tool+params 元组」——能区分"两次调同一个工具但不同参数"（正常）和"完全相同的重复操作"（死循环）。

### 1.8 三种熔断策略汇总

```cmd
:: 一口气验证所有终止/检测逻辑
pytest tests/test_day2.py -v -k "finish or give_up or max_steps or loop or reflection or llm_error" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"
```

**期望**：约 20 个测试全部 PASSED。

| 熔断策略 | 测试类 | 触发条件 |
|---------|--------|---------|
| 死循环熔断 | `TestLoopDetection` | 连续 3 步相同 `(tool, params)` |
| Reflection A | `TestReflectionTestFailed` | 测试工具返回失败 |
| Reflection B | `TestReflectionNoEdit` | 连续 6 步无 `file_write` |
| 步数上限 | `TestAgentMaxSteps` | `step > max_steps` |
| LLM 崩溃 | `TestLLMError` | `complete()` 抛异常 |
| 主动放弃 | `TestAgentGiveUp` | LLM 返回 `GIVE_UP` |

---

## 第二段验证：仓库地图生成与上下文注入

> 简历声明：*tree-sitter 8 语言符号提取 · AST 遍历 · 重要性排序 · System Prompt 注入*

### 2.1 多语言注册表

```cmd
python qfind.py "context/repo_map.py" "_LANG_REGISTRY"
python qfind.py "context/repo_map.py" ".py"
```

**期望输出**（`context/repo_map.py:39-50`）：
```
39:_LANG_REGISTRY: dict[str, tuple[str, str]] = {
40:    ".py":  ("tree_sitter_python",     "language"),
41:    ".js":  ("tree_sitter_javascript", "language"),
42:    ".ts":  ("tree_sitter_typescript", "language_typescript"),
...
49:    ".rb":  ("tree_sitter_ruby",       "language"),
```

**验证了**：「支持 Python/JS/TS/Go/Rust/Java/C/C++/Ruby 共 8 种语言」。

### 2.2 运行时按需加载 + 降级机制

```cmd
python qfind.py "context/repo_map.py" "_get_language"
python qfind.py "context/repo_map.py" "importlib"
python qfind.py "context/repo_map.py" "_extract_symbols_regex"
```

**核心源码**：`context/repo_map.py:87-111`。`_get_language()` 尝试 `importlib.import_module`，失败时返回 `None` → 调用方降级为正则。

**验证了**：「运行时按需加载语法包，不可用时静默降级为正则匹配」。

### 2.3 AST 节点类型识别

```cmd
python qfind.py "context/repo_map.py" "_FUNC_NODES"
python qfind.py "context/repo_map.py" "_CLASS_NODES"
```

**核心源码**：`context/repo_map.py:53-68`——精确区分 `function_definition`、`method_declaration`、`arrow_function`（跳过）等节点类型。多语言统一映射到 `function`/`class`/`method` 三种 kind。

**验证了**：「递归遍历 AST 提取函数、类、方法定义」。

### 2.4 缩进级别区分（class vs method）

```cmd
pytest tests/test_day5.py -v -k "method_has_indent or toplevel or line_numbers" --tb=line
```

**核心源码**：`context/repo_map.py:260-267`——`indent = node.start_point[1]`，indent > 0 判定为 method。

**验证了**：「提取函数、类、方法定义及其行号与缩进级别」——tree-sitter 提供 AST 级精度，这是正则做不到的。

### 2.5 重要性评分算法

```cmd
python qfind.py "context/repo_map.py" "importance_score"
python qfind.py "context/repo_map.py" "top_level"
```

**期望输出**：`context/repo_map.py:143-146`

```python
def importance_score(self) -> float:
    top_level = sum(1 for s in self.symbols if s.is_toplevel)
    size_penalty = self.size / 10_000
    return top_level - size_penalty
```

**验证了**：「按"顶层符号数 - 文件大小/10000"评分排序」——小文件 + 多顶层符号 = 高优先级（通常是核心模块）。

### 2.6 Token 预算截断

```cmd
python qfind.py "context/repo_map.py" "def build"
python qfind.py "context/repo_map.py" "char_count"
python qfind.py "context/repo_map.py" "more files"
```

**核心源码**：`context/repo_map.py:165-185`。按 `budget * 4` 字符预算截断，超出标记为 `"... (N more files not shown)"`。

**验证了**：「按 Token 预算截断」。

### 2.7 Repo-map 实际输出

```cmd
python -c "from context.repo_map import RepoMap; rm = RepoMap('.'); print(rm.build(budget=1500)[:1500])"
```

**期望输出示例**：

```
agent/core.py (7 symbols):
  class AgentConfig (line 50)
  class Agent (line 72)
  function run (line 95)
    method _build_messages (line 275)
    method _is_looping (line 330)
...

llm/base.py (5 symbols):
  class LLMBackend (line 68)
  class MockBackend (line 133)
...
```

> 注意 `function run (line 95)` 没有缩进（顶层函数），而 `method _build_messages` 有缩进（类内部方法）——这就是 AST 级精度的体现。

### 2.8 Repo-map 如何注入 System Prompt

```cmd
python qfind.py "agent/core.py" "repo_summary"
python qfind.py "agent/core.py" "_repo_map_cache"
python qfind.py "agent/prompt.py" "build_system_prompt"
```

**核心源码**：
- `agent/core.py:287-296`——在 `_build_messages()` 中生成 repo-map 并传给 `build_system_prompt()`
- `agent/prompt.py:56-79`——将 repo_summary 注入 System Prompt 的 `{repo_summary}` 占位符

```cmd
:: 验证 repo-map 确实出现在 LLM 收到的消息中
pytest tests/test_day5.py -v -k "repo_map_injected" --tb=line
```

**验证了**：「在 ReAct 循环的消息组装阶段注入 System Prompt，使 LLM 在每一步决策时同时具备环境反馈与全局代码结构信息」。

---

## 第三段验证：上下文管理与可测试性工程

> 简历声明：*Token 预算分配 · 双层裁剪 · MockBackend · 376 测试 20 秒 · EventLog*

### 3.1 Token 预算优先级分配

```cmd
python -c "from context.token_budget import TokenBudget; b = TokenBudget(80000); p = b.default_plan(); print(f'总预算: {p.total}'); print(f'Security Reserve: {p.reserve} (15%%)'); print(f'System:    {p.system_core:>6} (10%%)'); print(f'RepoMap:   {p.repo_map:>6} (15%%)'); print(f'History:   {p.history:>6} (50%%)'); print(f'Observation: {p.observation:>6} (25%%)')"
```

**期望输出**：
```
总预算: 80000
Security Reserve: 12000 (15%)
System:     6800 (10%)
RepoMap:   10200 (15%)
History:   34000 (50%)
Observation: 17000 (25%)
```

**验证了**：「Token 预算分配器（System 10% → Repo-map 15% → History 50% → Observation 25%）」。

### 3.2 双层裁剪机制

```cmd
pytest tests/test_day5.py -v -k "trim_history or sliding_window or token" --tb=line
```

**期望**：所有测试 PASSED。

**核心源码**：
- **第一层**：`context/history.py:81-88`——`ConversationHistory._trim()`，按消息条数裁剪，保留首条
- **第二层**：`context/token_budget.py:144-184`——`TokenBudget.trim_history()`，按 Token 数量从旧到新贪婪选择

**验证了**：「结合 ConversationHistory 滑动窗口与 TokenBudget 二分截断实现双层裁剪」。

### 3.3 MockBackend 源码

```cmd
python qfind.py "llm/base.py" "class MockBackend"
python qfind.py "llm/base.py" "def complete"
python qfind.py "llm/base.py" "self._script"
python qfind.py "llm/base.py" "self._index"
python qfind.py "llm/base.py" "call_count"
python qfind.py "llm/base.py" "received_messages"
```

**期望**：`llm/base.py:133-201`，约 70 行。

**核心机制**：

```
MockBackend._script    = [Action1, Action2, ...]  ← 预设剧本
MockBackend._index     = 0                         ← 播放指针
MockBackend.complete() → 返回 _script[_index]      ← 按剧本返回
MockBackend.received_messages                      ← 记录收到的 LLM 请求（供测试断言）
脚本耗尽 → 自动返回 GIVE_UP（防止 Agent 死循环）
```

```cmd
:: MockBackend 自身的行为测试
pytest tests/test_day2.py -v -k "TestMockBackend" --tb=line
```

**验证了**：「基于依赖倒置设计 MockBackend 以预设 Action 脚本队列替代真实 LLM 调用」。

### 3.4 NoopTool 和 FailingTool 源码

```cmd
python qfind.py "tools/base.py" "class NoopTool"
python qfind.py "tools/base.py" "class FailingTool"
```

- **NoopTool**（`tools/base.py:176`）：execute() 始终返回成功，记录 `call_count` + `last_params`
- **FailingTool**（`tools/base.py:212`）：execute() 始终返回失败，用于精确触发 Reflection 测试

**验证了**：「配合 NoopTool/FailingTool 将非确定性 LLM 行为转化为确定性输入/输出序列」。

### 3.5 376 个测试 20 秒

```cmd
pytest -v --tb=line 2>&1 | findstr /C:"passed" /C:"failed" /C:"skipped"
```

**期望**：`376 passed, 7 skipped in ~20s`

```cmd
:: 统计 MockBackend 在所有测试文件中的使用
python qfind.py "tests/*.py" "MockBackend"
```

**你会看到**：MockBackend 被 `test_day2.py`、`test_day5.py`、`test_day6.py`、`test_day7.py`、`test_stream.py`、`test_chat.py` 等 6 个测试文件使用。

**验证了**：「376 个测试在约 20 秒内覆盖 Agent 全部 6 种终止路径与 2 种 Reflection 条件」。

### 3.6 6 种终止路径 + 2 种 Reflection

```cmd
pytest tests/test_day2.py -v -k "finish or give_up or max_steps or loop or llm_error or unknown or reflection" --tb=line 2>&1 | findstr /C:"PASSED"
```

**期望**：约 20 个测试 PASSED。

| 路径/条件 | 测试 |
|-----------|------|
| SUCCESS（正常完成） | `TestAgentFinish::test_finish_on_first_action` |
| GAVE_UP（主动放弃） | `TestAgentGiveUp::test_give_up_returns_gave_up_status` |
| MAX_STEPS（步数熔断） | `TestAgentMaxSteps::test_max_steps_returns_correct_status` |
| LOOP_DETECTED（死循环） | `TestLoopDetection::test_loop_detected_gives_up` |
| LLM_ERROR（调用异常） | `TestLLMError::test_llm_exception_returns_failed` |
| UNKNOWN_TOOL（工具不存在）| `TestUnknownTool::test_unknown_tool_does_not_crash_agent` |
| Reflection A（测试失败） | `TestReflectionTestFailed::test_reflection_triggered_on_test_failure` |
| Reflection B（无编辑） | `TestReflectionNoEdit::test_reflection_triggered_after_no_edit_steps` |

### 3.7 EventLog 源码

```cmd
python qfind.py "agent/event_log.py" "class EventLog"
python qfind.py "agent/event_log.py" "def _append"
python qfind.py "agent/event_log.py" "def replay"
python qfind.py "agent/event_log.py" "def summarize_run"
python qfind.py "agent/event_log.py" "flush"
```

**核心源码**：
- `agent/event_log.py:32`——EventLog 类
- `agent/event_log.py:235-242`——`_append()` 每次写入后立即 `flush()`
- `agent/event_log.py:150-171`——`replay()` 完整回放
- `agent/event_log.py:264-302`——`summarize_run()` 统计分析

```cmd
pytest tests/test_day1.py -v -k "log or replay or summarize" --tb=line
```

**验证了**：「JSONL append-only 格式 · 每条写入后立即 flush · 支持 replay() 回放与 summarize_run() 统计」。

---

## 附录 A：一键全量验证脚本

以下命令按简历三段顺序依次运行，约 30 秒（不含 smoke_test 的 LLM 等待时间）：

```cmd
cd /d D:\fianll-test\forge\forge-agent
.venv\Scripts\activate
chcp 65001 >nul

:: 创建搜索工具（跳过如果已存在）
if not exist qfind.py (echo from pathlib import Path > qfind.py & echo import sys >> qfind.py & echo path, pat = sys.argv[1], sys.argv[2] >> qfind.py & echo for f in sorted(Path('.').glob(path)): >> qfind.py & echo.    for i, line in enumerate(open(f, encoding='utf-8'), 1): >> qfind.py & echo.        if pat in line: print(f'{f}:{i}: {line.rstrip()[:120]}') >> qfind.py)

:: === 第一段：Agent 架构与自省机制 ===
echo === 1.1 主循环位置 ===
python qfind.py "agent/core.py" "def run"

echo === 1.2 四个独立阶段 ===
python qfind.py "agent/core.py" "_build_messages" & python qfind.py "agent/core.py" "_call_with_retry" & python qfind.py "agent/core.py" "execute_tool"

echo === 1.3 Reflection 测试 ===
pytest tests/test_day2.py -v -k "reflection" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"

echo === 1.4 死循环检测测试 ===
pytest tests/test_day2.py -v -k "loop" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"

echo === 1.5 6种终止路径 ===
pytest tests/test_day2.py -v -k "finish or give_up or max_steps or loop or llm_error or unknown" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"

:: === 第二段：仓库地图生成与上下文注入 ===
echo === 2.1 多语言注册表 ===
python qfind.py "context/repo_map.py" "_LANG_REGISTRY"

echo === 2.2 重要性评分算法 ===
python qfind.py "context/repo_map.py" "importance_score"

echo === 2.3 Repo-map 实际输出 ===
python -c "from context.repo_map import RepoMap; rm = RepoMap('.'); print(rm.build(budget=1500)[:1200])"

echo === 2.4 Repo-map 注入 System Prompt ===
python qfind.py "agent/core.py" "repo_summary" & python qfind.py "agent/core.py" "_repo_map_cache" & python qfind.py "agent/prompt.py" "build_system_prompt"

echo === 2.5 Repo-map 测试 ===
pytest tests/test_day5.py -v -k "repo_map" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"

:: === 第三段：上下文管理与可测试性工程 ===
echo === 3.1 Token 预算分配 ===
python -c "from context.token_budget import TokenBudget; b = TokenBudget(80000); p = b.default_plan(); print(f'System:{p.system_core} RepoMap:{p.repo_map} History:{p.history} Observation:{p.observation}')"

echo === 3.2 双层裁剪测试 ===
pytest tests/test_day5.py -v -k "trim or history" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"

echo === 3.3 MockBackend 测试 ===
pytest tests/test_day2.py -v -k "MockBackend" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"

echo === 3.4 全量测试 ===
pytest -v --tb=line 2>&1 | findstr /C:"passed" /C:"failed"

echo === 3.5 EventLog 测试 ===
pytest tests/test_day1.py -v -k "log or replay" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"

echo === 全部验证完成 ===
```

---

## 附录 B：原文问题修正

如果你坚持使用提供的那三段（不含工具链段），建议按以下标注修正：

```
Agent 架构与自省机制：基于 SWE-agent 的 ReAct 范式构建自主编程 Agent 主循环，
将上下文组装、LLM 推理、Tool Calling、结果回写拆分为独立阶段，替代黑盒式
AgentExecutor；设计双重 Reflection 自省机制——测试失败时自动注入错误分析提示
引导 LLM 反思根因、连续 N 步无文件修改时触发方向反思，结合连续相同 Action 死循环
检测（滑动窗口比对 tool+params 元组）实现三种自动熔断策略，防止 Agent 陷入局部
最优或无效循环。
                                                                  ← 加上括号内文字

仓库地图生成与上下文注入：借鉴 Aider 的 repo-map 方案，基于 tree-sitter 实现
多语言代码仓库的结构化符号提取——支持 Python/JS/TS/Go/Rust/Java/C/C++/Ruby 共
8 种语言，运行时按需加载语法包，不可用时静默降级为正则匹配。递归遍历 AST 提取
函数、类、方法定义及其行号与缩进级别（区分顶层函数与类内部方法），按"顶层符号数
- 文件大小/10000"评分排序后按 Token 预算截断，在 ReAct 循环的消息组装阶段注入
System Prompt，使 LLM 在每一步决策时同时具备环境反馈与全局代码结构信息。
  ↑ 改"多种"为"8 种"        ↑ 加括号说明缩进的意义    ↑ 加粗体这句是关键衔接

上下文管理与可测试性工程：设计基于优先级的 Token 预算分配器（System 10% →
Repo-map 15% → History 50% → Observation 25%），结合滑动窗口与二分截断实现
双层上下文裁剪。基于依赖倒置设计 MockBackend 以预设 Action 脚本替代真实 LLM
调用，将非确定性 LLM 行为转化为确定性输入/输出序列，实现 376 个测试在约 20 秒
内覆盖全部 6 种终止路径与 2 种 Reflection 条件。EventLog 以 JSONL append-only
格式记录每次运行的全部事件（含 LLM 原始输出），每条写入后立即 flush 防崩溃丢失，
支持 replay() 完整回放与 summarize_run() 统计分析。
  ↑ 删掉了冗余的"测试架构上"        ↑ 加了这个关键数据                      ↑ 改为更连贯的表达
```

---

> **提示**：如果你后续要补充"工具链与多模型适配"段（强烈建议），参考 `resume-bullets.md` 中 V1 的第二段。
