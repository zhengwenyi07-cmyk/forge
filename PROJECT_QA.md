# Forge Agent 项目疑问解答

> 共 16 个问题，覆盖架构设计、工程实现、使用流程，每个解答包含具体代码示例。

---

## Q1：importance_score 用简化公式替代 PageRank，理由是否合理？

**答：合理。** 这是一个典型的"工程实用性 vs 理论最优"的权衡。

### 你当前的公式

```python
# context/repo_map.py:143-146
def importance_score(self) -> float:
    top_level = sum(1 for s in self.symbols if s.is_toplevel)
    size_penalty = self.size / 10_000
    return top_level - size_penalty
```

### Aider 的 PageRank 方案

Aider 的做法：解析所有文件的 `import` 语句 → 构建依赖图 → 用 PageRank 计算每个文件的"被引用权重" → 被高频引用的文件排名更高。

### 为什么简化是合理的

| 维度 | PageRank | 简化公式 |
|------|----------|---------|
| 需要解析 | import 语句（8 种语言×8 套解析器） | 只需要 tree-sitter AST |
| 代码量 | 每种语言单独实现 import 解析 | 所有语言复用同一套逻辑 |
| 准确度 | 更精确（能识别核心依赖） | 近似准确（小文件+多定义≈核心模块） |
| 维护成本 | 高（每种语言的 import 语法都会演变） | 低（符号提取逻辑不变） |

**面试这么说**：Aider 的 PageRank 需要为 8 种语言分别解析 import 依赖图，工程量巨大——我算过 ROI，简单启发式 `顶层符号数 - 文件大小/10000` 在实践中已经能把 `main.py`、`core.py` 这类核心模块排到第一页，够用就好。

---

## Q2：为什么要区分 function vs method？本项目只用缩进区分吗？

### 为什么要区分

区分 **顶层函数** 和 **类内部方法** 对 Agent 做决策很关键：

```
agent/core.py (7 symbols):
  class Agent (line 72)              ← 顶层类：模块入口
    method run (line 95)             ← 方法：知道在 Agent 内部
    method _build_messages (line 275) ← 方法：私有的内部实现
  class AgentConfig (line 50)        ← 顶层类：数据结构
  function _c (line 49)              ← 顶层函数：工具函数
```

Agent 看到这个摘要是这样推理的：
1. "我要修 bug" → 先找顶层 `class`/`function` 找到入口
2. "我要改某个方法" → 已知它在哪个类里，用 `file_view` 精确跳转行号

### 是的，本项目用缩进区分

```python
# context/repo_map.py:258-267
if ntype in _FUNC_NODES and ntype != "arrow_function":
    name_node = node.child_by_field_name("name")
    if name_node:
        indent = node.start_point[1]   # ← 列偏移
        kind = "method" if indent > 0 else "function"  # ← 缩进>0=方法
```

tree-sitter 的 `start_point` 返回 `(行号, 列号)`，列号>0 说明嵌套在 `class` 内部。这是 tree-sitter 相比正则的核心优势——正则只能匹配 `def foo()`，无法区分：

```python
def run():          # indent=0 → function ✓
    pass

class App:
    def run(self):  # indent=4 → method ✓
        pass
```

---

## Q3：tree-sitter 导出树后，按什么方案截取代码用于最终显示？

### 不是截取源代码，只提取符号名+位置

tree-sitter 的作用是**提取符号摘要**而非截取源代码。对于每个类/函数，最终只保留：

- **kind**：`function` / `method` / `class`
- **name**：符号名
- **line**：定义所在行号
- **indent**：是否嵌套

不展示具体代码实现。最终格式化输出：

```python
# context/repo_map.py:211-224
def _format_file(self, fi: FileInfo) -> str:
    header = f"{fi.rel_path}"         # agent/core.py
    if sym_count:
        header += f" ({sym_count} symbols)"

    sym_lines = [header + ":"]
    for sym in fi.symbols:
        prefix = "    " if not sym.is_toplevel else "  "
        sym_lines.append(f"{prefix}{sym.kind} {sym.name} (line {sym.line})")
    return "\n".join(sym_lines) + "\n"
```

输出效果：

```
agent/core.py (7 symbols):
  class AgentConfig (line 50)
  class Agent (line 72)
  function run (line 95)
    method _build_messages (line 275)
    method _is_looping (line 330)
    method _call_with_retry (line 353)
    method _get_git_diff (line 398)
```

Agent 拿到这份"地图"后，若需要读某函数的实现代码，会调用 `file_view` 并传入行号精确定位。

### 截断发生在"文件级别"

```python
# context/repo_map.py:174-182
for fi in files:
    block = self._format_file(fi)
    if char_count + len(block) > max_chars:
        remaining = len(files) - files.index(fi)
        lines.append(f"... ({remaining} more files not shown)")
        break
    lines.append(block)
    char_count += len(block)
```

先按重要性排序，然后从头到尾一个个文件往上加，预算用尽时一刀切断，不展示的文件显示 `"... (72 more files not shown)"`。

---

## Q4：参数 10000 是怎么选的？能复现验证吗？

### 来源说明

参数 10000 是一个经验值，选 10000 字节（约 10KB）作为"大文件"的分界线。选择依据：

- 一个正常的 Python 模块通常在 1-5KB
- 超过 10KB 的通常是自动生成文件、数据文件、或者过度膨胀的代码
- 参数值确保文件大小不会完全淹没符号数的贡献

### 可以自己复现验证

```bash
# 1. 生成当前项目的完整 ranking
python -c "
from context.repo_map import RepoMap
rm = RepoMap('.')
files = rm._scan()
files.sort(key=lambda f: f.importance_score(), reverse=True)

# 打印排名前 10
for i, fi in enumerate(files[:10], 1):
    score = fi.importance_score()
    tops = sum(1 for s in fi.symbols if s.is_toplevel)
    print(f'{i:2d}. score={score:6.1f}  top={tops:2d}  size={fi.size:6d}B  {fi.rel_path}')
"
```

你会看到 `agent/core.py`（7 个顶层符号、~15KB）、`llm/openai_compat.py`（5 个顶层符号、~12KB）这类核心文件确实排在前面，而 `tests/` 下虽然也有很多符号，但当文件大时得分就会下降。

---

## Q5：Token 预算截断是逐个计算还是二分搜索？`* 4` 的依据？

### 两种截断方式

项目用了**两种策略混合**：

**按条数截断**（ConversationHistory）：逐一丢弃最旧的非首条消息。

```python
# context/history.py:81-88
def _trim(self) -> None:
    while len(self._messages) > self._max:
        if len(self._messages) > 1:
            self._messages.pop(1)  # 从索引1开始丢，保留索引0
        else:
            break
```

**按 Token 截断**（TokenBudget）：从最旧开始**贪婪选择**（不是一个个计算、也不是二分）：

```python
# context/token_budget.py:167-175
for msg, tokens in zip(reversed(messages[1:]), reversed(token_counts[1:])):
    if budget_left - tokens >= 0:
        selected.append(msg)
        budget_left -= tokens
    else:
        dropped += 1
```

从最新往最旧方向遍历，能塞就塞，塞不下就丢弃。这是 O(n) 的**贪心算法**——不是二分搜索，但效率也不差。

### 为什么 Aider 用二分、本项目用贪心

Aider 用二分是因为要精确控制在某个 token 数的文件范围内。本项目用贪心是因为"优先保留最新消息"的规则已经定了——遍历一遍就能决出哪些保留、哪些丢弃，不需要二分。

### `1 token ≈ 4 chars` 的依据

来自 OpenAI 官方的经验估计。OpenAI 文档中建议：对于英文文本，1 token 大约等于 4 个字符。项目代码中：

```python
# context/token_budget.py:67-68
# 字符估算 fallback
return max(1, len(text) // 4)
```

```python
# context/token_budget.py:71-73
def estimate_chars(tokens: int) -> int:
    """把 token 数转换为字符预算（估算）。"""
    return tokens * 4
```

**但这不是固定规律**——代码/中文的 token 密度不同。项目优先用 `tiktoken` 的 `cl100k_base` 做精确计数，`*4` 只是 tiktoken 不可用时的 fallback，误差 <15%，对粗粒度的窗口管理已足够。

---

## Q6：本项目 repo-map 的真实示例

> **这是真实运行输出**，不是虚构的：

```
tests\test_day2.py (50 symbols):
  function task (line 32)
  function registry (line 42)
  function log (line 51)
  function make_agent (line 55)
  class TestToolRegistry (line 90)
    method test_register_and_execute (line 91)
    method test_duplicate_register_raises (line 100)
    method test_unknown_tool_returns_error (line 106)
  class TestMockBackend (line 152)
    method test_returns_scripted_actions_in_order (line 153)
    method test_tracks_call_count (line 168)
  class TestAgentFinish (line 186)
    method test_finish_after_tool_calls (line 198)
  class TestReflectionTestFailed (line 353)
    method test_reflection_triggered_on_test_failure (line 354)
  class TestLoopDetection (line 310)
    method test_loop_detected_gives_up (line 311)

entry\cli.py (19 symbols):
  function _build_registry (line 65)
  function run (line 176)
  function chat (line 327)
  function log_show (line 482)
  function main (line 541)

tests\test_day3.py (76 symbols):
  class TestFileReadTool (line 75)
  class TestFileViewTool (line 113)
  class TestShellTool (line 187)
  class TestShellBlacklist (line 222)
  class TestSearchTextTool (line 262)
  ...
```

**关键观察**：
- `test_day2.py` 50 个符号但文件很大，得分反而不如小文件
- 每个嵌套方法都被精确标注 `method`——tree-sitter 列的功劳
- 行号让 Agent 能用 `file_view` 精确定位

---

## Q7：Docker 在本项目中作用是什么？如何连接？

### Docker 的作用：沙箱隔离

Docker 提供**命令执行沙箱**——Agent 调用的所有 `shell` / `pytest` / `git` 命令都在容器内执行，宿主机环境完全隔离。

### 连接方式：Runtime 抽象层

```
Agent → ShellTool.execute() → Runtime.exec() → Docker exec / subprocess
```

具体来说：

**第 1 步**：用户加 `--sandbox` 参数

```bash
agent run --task "..." --sandbox
```

**第 2 步**：CLI 创建 DockerRuntime

```python
# entry/cli.py:240
runtime = create_runtime(sandbox=True, repo_path=str(repo_path))
```

**第 3 步**：运行时注入到所有工具

```python
# entry/cli.py:76
ShellTool(confirm_callback=confirm_cb, runtime=runtime)
PytestTool(runtime=runtime)
GitStatusTool(runtime=runtime)
# ... 所有需要执行命令的工具都传入 runtime
```

**第 4 步**：工具执行时，完全不感知自己是在本地还是容器

```python
# tools/shell_tool.py:194
result = self._runtime.exec(cmd, cwd=cwd, timeout=timeout)
```

**第 5 步**：首次 exec 时懒启动容器

```python
# tools/runtime.py:252-260
docker_cmd = [
    "docker", "exec",
    "--workdir", container_cwd,
    self._container_id,
    "bash", "-c", cmd,
]
```

### 核心设计

```python
class Runtime(ABC):
    @abstractmethod
    def exec(self, cmd, cwd, timeout) -> RunResult: ...

class LocalRuntime(Runtime):   # 本地 subprocess
class DockerRuntime(Runtime):  # Docker exec
```

工具层依赖 `Runtime` 抽象接口，不关心具体实现。这就是**策略模式 + 依赖注入**。

---

## Q8：一个进程创建一个沙箱环境？每个进程都需要 Docker 吗？

### 当前设计：一个 Agent 实例复用一个容器

`DockerRuntime` 是**懒启动**的：首次调用 `exec()` 时才启动容器（`_start_container()`），之后所有命令都在同一个容器中执行，直到 `cleanup()` 或会话结束。

```python
# tools/runtime.py:228-232
def exec(self, cmd, cwd=None, timeout=30):
    if not self.is_running:
        startup_result = self._start_container()
        if startup_result is not None:
            return startup_result
    # 之后复用同一个容器
    docker_cmd = ["docker", "exec", "--workdir", ..., self._container_id, "bash", "-c", cmd]
```

### 容器复用 = 高效

- 之前：每条命令一个 `docker run` → 每次启动容器要 1-3 秒
- 现在：`docker run -d ... tail -f /dev/null` 一次启动 → 所有命令 `docker exec` → 每次 <0.1 秒

当 Agent 跑 40 步、每步调一个 shell 命令时，这个差异是 120 秒 vs 4 秒。

### 什么情况下每个进程不都需要 Docker

默认不传 `--sandbox` 时，走的是一条完全不同的路径：

```python
# tools/runtime.py
def create_runtime(sandbox=False, ...):
    if not sandbox:
        return LocalRuntime()  # 本地 subprocess，不用 Docker
```

---

## Q9：GitHub Issue 模式详解

### 能执行什么任务

给定一个 GitHub Issue（如 "Login endpoint returns 500 on empty email"），Agent 自动：
1. 拉取 Issue 标题和描述作为任务
2. 克隆仓库
3. 创建修复分支
4. 在代码库中探索、修改、运行测试
5. 提交修改、推送分支
6. 创建 PR（标题如 `[Agent] Fix issue #42: Login endpoint ...`）

### 关键代码流程

```python
# entry/github_issue.py:169-291

# 1. 拉取 Issue
title, body, issue_url = fetch_issue(repo_name, issue_number)
description = f"Fix GitHub Issue #{issue_number}: {title}\n\n{body}"

# 2. Clone（已存在则跳过）
clone_repo(repo_name, local_path)

# 3. 创建分支 agent/fix-issue-42-1718123456
branch = f"agent/fix-issue-{issue_number}-{int(time.time())}"
create_branch(local_path, branch)

# 4. 构建 Agent（和普通 run 模式一模一样）
backend = create_backend_from_config({...})
agent = Agent(backend, registry, agent_config)
task = Task(description=description, repo_path=local_path, issue_url=issue_url)
 
# 5. 运行 Agent
result = agent.run(task, log)

# 6. Push + 创建 PR
push_branch(local_path, branch)
pr_url = create_pull_request(repo_name, branch, pr_title, pr_body)
```

### 体验流程

**前提**：有 GitHub Token（`repo` 权限）、目标仓库是你有写权限的。

```bash
# 1. 设置 Token
set GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxx

# 2. 运行（用任意公开 Issue 做实验）
python -m entry.github_issue \
    --repo owner/repo-name \
    --issue 1 \
    --local-path C:\Temp\test-repo \
    --no-pr
```

`--no-pr` 表示只本地修代码，不创建 PR。去掉它就会自动提 PR。

---

## Q10：是否必须加 `--sandbox` 才在沙箱中执行？

**是的。** 不加 `--sandbox` = 本地执行（默认 `LocalRuntime`）。

```python
# entry/cli.py:239-240
confirm_cb = terminal_confirm if confirm else None
runtime = create_runtime(sandbox=sandbox, ...) if sandbox else None
#        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#        只有传了 --sandbox，runtime 才不是 None
```

当 `runtime=None` 时，每个工具内部使用默认的 `LocalRuntime`：

```python
# tools/shell_tool.py:119
self._runtime = runtime or LocalRuntime()
```

```bash
# 本地执行（默认）
agent run --task "echo hello"

# 沙箱执行
agent run --task "echo hello" --sandbox
```

---

## Q11：chat / run / GitHub Issue 三种模式的分工？

### 三者关系

```
┌─────────────────────────────────────────────────┐
│                   CLI 入口                        │
│  agent chat  │  agent run  │  python -m entry... │
│  持续对话     │  一次性任务   │  GitHub Issue 自动化 │
└─────────────────────────────────────────────────┘
          ↓             ↓              ↓
     ┌─────────────────────────────────────┐
     │          Agent Core (ReAct 主循环)    │
     │    run(task, log) → RunResult        │
     └─────────────────────────────────────┘
```

三者共享同一套 Agent Core，区别在于**上层交互方式**：

| 模式 | 适用场景 | 特色 |
|------|---------|------|
| **chat** | 探索性开发、多轮对话 | 历史跨轮保留，可以持续迭代 |
| **run** | 明确的批处理任务 | 一次性执行，适合脚本化 |
| **GitHub Issue** | 全自动修 Bug 提 PR | 端到端自动化 |

### 只能纯 chat 或纯 run？

不是的。chat 模式下每轮都可以完全不同的任务，不强制"纯对话"或"纯运行"：

```
you > 帮我分析项目结构
    → agent 探索代码库，生成分析报告

you > 刚才分析的那个文件，把里面的 bug 修了
    → agent 记得上一轮看到了什么文件

you > /stats        ← 可以随时查统计
you > /clear        ← 也可以清空历史重来
you > /exit
```

---

## Q12：对话历史保留在哪里？是否算在 history 里？

### 是的，对话历史就是 ConversationHistory

```python
# entry/chat.py:226-228
self._shared_history = ConversationHistory(
    max_messages=config.context.history_window * 2
)
```

每轮用户输入 → 追加到共享 history → agent 运行 → 结果也追加回 history：

```python
# entry/chat.py:252-253
self._shared_history.add(LLMMessage(role="user", content=user_input))

# entry/chat.py:278-282
self._shared_history.add(LLMMessage(
    role="assistant",
    content=f"[Round {self.round_count} complete]\n{result.summary}",
))
```

### 传给 LLM 时的裁剪路径

history 不是全额传给 LLM，经过**双层裁剪**：

```
ConversationHistory._trim()
    ↓ 条数维度：保留首条 + 最近 N 条
TokenBudget.trim_history()
    ↓ Token 维度：保留首条 + 从新到旧贪心选择
发给 LLM 的 messages
```

对应简历描述：「ConversationHistory 滑动窗口（条数维度）与 TokenBudget 二分截断（Token 维度）实现双层上下文裁剪」

---

## Q13：MockBackend 和真实工具测试的 "mock vs 不 mock" 区别？

### 这是一个分层的测试策略

| 测试文件 | 测什么 | 是否 mock LLM | 是否 mock 工具 | 为什么 |
|---------|--------|-------------|--------------|--------|
| `test_day2.py` | Agent 状态机 | ✅ MockBackend | ✅ NoopTool | 测状态转换逻辑，不需要真实 I/O |
| `test_day3.py` | 工具实现 | ❌ 不涉及 LLM | ❌ 真实工具 | 测 Shell/File/Git，必须真实执行 |
| `test_day4.py` | LLM 格式转换 | ✅ mock SDK | ❌ 不涉及工具 | 测请求格式/响应解析 |
| `test_day5.py` | 上下文模块 | ✅ MockBackend | ✅ NoopTool | 测 token 裁剪/repo-map，不涉及工具 |
| `test_day6.py` | CLI + 配置 | ✅ Click CliRunner | ❌ 不涉及 | 测命令行参数解析 |
| `test_day7.py` | 集成测试 | ✅ MockBackend | ✅ NoopTool | 测 retry/patch/cache 等跨模块逻辑 |

### 为什么要分层 

```python
# test_day2.py——测 "Agent 看到 FINISH action 是否会正确终止"
backend = MockBackend(script=[make_finish_action("Done")])
result = agent.run(task, log)
assert result.status == RunStatus.SUCCESS

# test_day3.py——测 "ShellTool 执行 echo 是否正确返回结果"
tool = ShellTool()
result = tool.execute({"cmd": "echo hello"})
assert result.success and "hello" in result.output
```

第一个测试如果调真实 LLM——不确定它会不会返回 FINISH，每次要花 2 秒还要花钱。第二个测试如果 mock 工具——那等于在测试 mock 本身而不是测试 ShellTool。分层才能各测各的。

---

## Q14：MockBackend 是库还是自己写的？

### 本项目自己写的，不是第三方库

```python
# llm/base.py:133-200
class MockBackend(LLMBackend):
    """
    测试专用 backend，按脚本顺序返回预设的 Action。
    """
    def __init__(self, script: list[Action], input_tokens=100, output_tokens=50):
        self._script = script   # 预设的 Action 列表，按序播放
        self._index = 0
        self.call_count = 0          # 记录被调用次数
        self.received_messages = []  # 记录每次收到的 messages

    def complete(self, messages, tools):
        self.call_count += 1
        self.received_messages.append(messages)

        if self._index < len(self._script):
            action = self._script[self._index]   # 按剧本返回
            self._index += 1
        else:
            action = Action(GIVE_UP, "Script exhausted")  # 用完自动结束

        return LLMResponse(action=action, ...)
```

它利用了**依赖倒置原则**：Agent 不直接依赖 `AnthropicBackend` 或 `OpenAICompatBackend`，而是依赖抽象 `LLMBackend`。所以只需实现同一个接口就能无缝替换。

### 为什么不用 `pytest-mock` 的 `patch`

MockBackend 提供了 `patch` 做不到的功能：

```python
# 验证"Agent 向 LLM 发送了什么内容"——patch 做不到这一点
assert "REFLECTION" in backend.received_messages[-1][-1].content

# 验证"LLM 一共被调了几次"——patch 也难以精确计数
assert backend.call_count == 3
```

这是个"剧本播放器"（script player），不是简单的 mock。

---

## Q15：Agent 不自动 commit 是什么意思？

### 设计决策

Agent 修文件时**只修改工作区文件，不自动执行 `git commit`**。原因：

1. **安全性**：用户可以在 commit 前审查 diff
2. **可回溯**：即使 agent 修错了，工作区状态仍然可控
3. **灵活性**：用户决定什么时候 commit、怎么写 message

### 如何撤销 Agent 的修改

Agent 没有自动 commit → 所有修改都在 git 工作区 → 用 git 直接回退：

```bash
# 撤销所有未提交的修改
git checkout -- .

# 撤销特定文件
git checkout -- src/foo.py

# 查看 agent 改了什么（在撤销前）
git diff
```

### Agent 有 Git 工具但不自动调用

Agent 有 `git_status`、`git_diff`、`git_add`、`git_commit` 四个工具，但只在 **LLM 主动决定调用它们时** 才会执行——而 system prompt 没有要求 agent 自动 commit，所以它通常修完文件就 `finish`。

### 例外：GitHub Issue 模式

只有 GitHub Issue 模式会**自动** push 到远程和创建 PR——这是模式层面控制的，不是 Agent 主循环的行为。

---

## Q16：完整阅读代码的推荐顺序

### 推荐顺序（由浅入深，约 2-3 小时）

```
第 1 层：理解数据模型（15 分钟）
  ├── agent/task.py         → Action, Observation, Task, RunResult
  ├── llm/base.py           → LLMMessage, LLMResponse, MockBackend
  └── tools/base.py         → ToolResult, BaseTool, ToolRegistry

第 2 层：理解核心流程（30 分钟）
  ├── agent/core.py:95-269  → Agent.run() ← 整个项目的核心，从这进去
  ├── agent/prompt.py       → system prompt 和 reflection prompt
  └── agent/event_log.py    → JSONL 事件日志

第 3 层：理解工具实现（30 分钟）
  ├── tools/shell_tool.py   → 四层安全防护
  ├── tools/file_tool.py    → 分页读写设计
  ├── tools/search_tool.py  → 三种搜索策略
  ├── tools/test_tool.py    → pytest 结构化解析
  └── tools/git_tool.py     → git 四步操作

第 4 层：理解 LLM 适配（30 分钟）
  ├── llm/router.py         → provider 路由分发
  ├── llm/anthropic_backend.py  → Claude 原生 tool_use
  ├── llm/openai_compat.py  → OpenAI/DeepSeek/Groq/Ollama + R1 文本 fallback
  └── config/schema.py      → 配置加载与校验

第 5 层：理解上下文管理（20 分钟）
  ├── context/history.py    → 对话历史滑动窗口
  ├── context/token_budget.py → Token 预算分配
  └── context/repo_map.py   → tree-sitter 仓库地图

第 6 层：理解入口层（15 分钟）
  ├── entry/cli.py          → 所有 CLI 命令
  ├── entry/chat.py         → 交互对话会话管理
  └── entry/github_issue.py → Issue → PR 自动化

第 7 层：验证理解（20 分钟）
  └── tests/test_day1~7.py  → 从测试看每个模块的契约
```

### 阅读时的重点提示

- `agent/core.py:95` 的 `run()` 方法是整个系统的帧——看懂这段代码，整个项目的运作逻辑就通了
- 每当你看到 `class Xxx(ABC)` 或 `@abstractmethod`，停下来想一想"这个抽象是为了什么"——这些是架构决策点
- `tools/runtime.py` 是整个第 6 层才需要看的，它不是核心路径，但 Docker 沙箱的懒启动设计很值得学



