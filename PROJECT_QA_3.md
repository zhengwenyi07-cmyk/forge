# Forge Agent 项目疑问解答（第三期）

> 共 9 个问题，覆盖运行机制、工具详解、零基础使用全流程

---

## Q1：TASK_FAILED 是 LLM 判断的还是代码自动检测的？

**答：全部由代码自动检测，LLM 不参与"失败"的判定。**

### 四种失败场景，全部由代码检测

```python
# agent/core.py:135-269 中的 Agent.run() 方法

# ─── 场景 1：死循环检测（代码自动判断）───
# agent/core.py:164-174
if self._is_looping(log):
    reason = f"Loop detected: same action repeated {self._cfg.loop_detection_window} times"
    log.log_task_failed(steps=step, reason=reason)
    return RunResult(status=GAVE_UP, ...)

# ─── 场景 2：LLM 主动放弃（代码解析 LLM 输出中的 GIVE_UP action）───
# agent/core.py:190-199
if action.action_type == ActionType.GIVE_UP:
    reason = action.message or "Agent gave up."
    log.log_task_failed(steps=step, reason=reason)
    return RunResult(status=GAVE_UP, ...)

# ─── 场景 3：超过步数上限（代码自动判断）───
# agent/core.py:260-269
reason = f"Reached max_steps limit ({task.max_steps})"
log.log_task_failed(steps=task.max_steps, reason=reason)
return RunResult(status=MAX_STEPS, ...)

# ─── 场景 4：LLM API 调用异常（代码捕获异常）───
# agent/core.py:143-154
try:
    response = self._call_with_retry(messages, tools)
except Exception as exc:
    log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
    return RunResult(status=FAILED, ...)
```

### 总结

| TASK_FAILED 原因 | 谁判断的 | 如何判断 |
|-----------------|---------|---------|
| `max_steps` | 代码 | `step >= task.max_steps` 自动截断 |
| `loop_detected` | 代码 | 最近 N 条 `(tool_name, params)` 完全相同 |
| `Agent gave up` | LLM 决定，代码解析 | LLM 返回 `action_type == GIVE_UP` |
| `LLM error` | 代码 | LLM 调用抛异常，重试耗尽后仍失败 |

**关键区分**：`GIVE_UP` 是 **LLM 主动投降**（"我搞不定了"），`TASK_FAILED` 是 **代码自动熔断**（"你太慢了/你在死循环/LLM 挂了"）。

---

## Q2：`shell` 工具的作用是什么？

`shell` 工具是 Agent 的"万能执行器"——它让 LLM 可以在终端上执行任何 bash 命令。

### 具体能做什么

```python
# LLM 决定调用 shell，传入这些参数
{"cmd": "ls -la"}                    # 查看文件列表
{"cmd": "cat src/main.py"}           # 查看文件内容（不用 file_read 时）
{"cmd": "pytest tests/"}            # 运行测试
{"cmd": "python utils.py"}           # 执行 Python 脚本
{"cmd": "pip install requests"}      # 安装依赖
{"cmd": "git log --oneline -5"}      # 查看 git 历史
{"cmd": "grep -r 'def foo' src/"}    # 搜索文本（不用 search_text 时）
```

### 代码实现

```python
# tools/shell_tool.py:99-205
class ShellTool(BaseTool):
    @property
    def name(self) -> str:
        return "shell"

    def execute(self, params):
        cmd = params.get("cmd", "")
        # 四层安全防护（见下方）
        return self._run(cmd, timeout, cwd)
```

### 三层安全防护

```python
# 层 1：黑名单（永不执行）
_BLOCKED_PATTERNS = ("rm -rf /", "mkfs", "dd if=", ...)

# 层 2：白名单（直接执行，不询问）
_READONLY_PREFIXES = ("ls", "cat", "grep", "pytest", "git status", ...)

# 层 3：写操作确认（仅在 --confirm 或 chat 模式下）
_CONFIRM_KEYWORDS = ("pip install", "git commit", "docker", "sudo", ...)
```

### 为什么 LLM 需要 `shell`？

因为 `file_read`（读文件）、`search_text`（搜文本）、`test`（跑测试）这些专用工具虽然有更方便的结构化接口，但 LLM 有时需要执行不在工具列表里的命令——`shell` 就是兜底方案。

---

## Q3：`file_read` / `file_view` / `file_write` 有什么区别？

| 工具 | 功能 | 每次读/写多少 | 主要用途 |
|------|------|-------------|---------|
| `file_read` | 读取文件，超过 500 行截断 | 最多 500 行 | 了解文件内容 |
| `file_view` | 分页查看文件 | 每次 100 行 | 逐页阅读大文件 |
| `file_write` | 写入/覆盖文件 | 全量覆盖 | 修改代码 |

### file_read（全量读）

```python
# tools/file_tool.py:64-104
def execute(self, params):
    lines = path.read_text().splitlines()
    total = len(lines)
    display_lines = lines[:MAX_READ_LINES]  # MAX_READ_LINES = 500
    # 加行号，超出500行提示用 file_view 翻页
    return ToolResult(success=True, output=numbered + suffix)
```

**输出示例**：
```
File: src/main.py (1200 lines total)
   1 | import os
   2 | import sys
   ...
 500 | def run():
... (700 more lines not shown) Use file_view with start_line to read the rest.
```

### file_view（分页读）

```python
# tools/file_tool.py:144-180
def execute(self, params):
    start_line = params.get("start_line", 1)
    window = lines[start_line-1 : start_line-1 + VIEW_WINDOW_LINES]
    # VIEW_WINDOW_LINES = 100
    # 翻页提示：
    nav = f"\n[Lines {start_line}–{end_line} of {total}. Next: file_view path={path} start_line={end_line+1}]"
    return ToolResult(success=True, output=numbered + nav)
```

**输出示例**：
```
 501 |     result = process(data)
 502 |     ...
 600 |     return result

[Lines 501–600 of 1200. Next: file_view path=src/main.py start_line=601]
```

Agent 可以像人类一样"翻页"阅读。

### file_write（全量覆盖写入）

```python
# tools/file_tool.py:221-235
def execute(self, params):
    path.write_text(content, encoding="utf-8")
    return ToolResult(success=True, output=f"Written {N} lines to {path}")
```

**关键设计**：file_write 不是"diff 编辑"而是"全量覆盖"。LLM 需要读取文件 → 在 thought 中思考修改 → 写出**完整**的新文件内容。这简化了 LLM 的编辑模型（不用考虑 diff 格式）。

### 三者的协作流程

```
Agent 要修 src/main.py 里的一个 bug：

Step 1: file_read("src/main.py")   ← 了解全部内容（最多 500 行）
Step 2: file_view("src/main.py", start_line=501)  ← 如果文件超长，翻页读后续
Step 3: file_write("src/main.py", content="完整新内容")  ← 全量覆盖写入
Step 4: shell("pytest")            ← 运行测试验证
```

---

## Q4：Docker bind mount 详细解释

### bind mount 是什么

**bind mount** 是 Docker 的一种数据挂载方式：把宿主机上的一个目录（或文件）直接"映射"到容器内的一个路径。

```
宿主机                              Docker 容器
─────────────────────────────────────────────────
D:\my-project              →      /workspace
  ├── src/                          ├── src/
  │   └── main.py                   │   └── main.py  ← 同一个文件
  ├── tests/                        ├── tests/
  │   └── test_main.py             │   └── test_main.py  ← 同一个文件
  └── README.md                     └── README.md  ← 同一个文件
```

### 本项目中 bind mount 的代码

```python
# tools/runtime.py:320-323
run_args = [
    "docker", "run",
    "-v", f"{self._repo_path}:{CONTAINER_WORKDIR}",  # ← 这就是 bind mount！
    #  宿主机路径    :   容器内路径
    #  如 D:\project :   /workspace
]
```

### 核心特性

**实时双向同步**：

- 宿主机上 `file_write` 写的文件 → 容器内 `shell` 执行立刻看到 ✅
- 容器内 `git commit` 提交 → 宿主机 git 仓库立刻更新 ✅
- **不需要手动复制文件**，bind mount 是操作系统层面的文件共享

### 一个完整例子

```bash
# 你的项目在 D:\projects\my-app
agent run --task "修 bug" --sandbox

# 系统内部：
# 1. 启动容器时：docker run -v D:\projects\my-app:/workspace ...
# 2. Agent 的 file_write("src/main.py", "修正的代码")
#    → 实际写入了 D:\projects\my-app\src\main.py（宿主机）
#    → 但因为 bind mount，容器里读 /workspace/src/main.py 也是新版本
# 3. Agent 的 shell("python -m pytest")
#    → 在容器内执行 docker exec ... bash -c "python -m pytest"
#    → pytest 读到的 /workspace/src/main.py 就是刚才 file_write 写的新版本
# 4. 运行结束后，去 D:\projects\my-app\src\main.py 看到的是修改后的版本
```

### 和 cp 的区别

```bash
# ❌ 不用 cp（要手动复制，慢且麻烦）
docker cp host_file container:/workspace/  # 手动复制进去
docker cp container:/workspace/ host_file  # 手动复制出来

# ✅ 用 bind mount（自动同步，不需要额外操作）
docker run -v host_dir:/workspace ...  # 启动时设好，之后自动同步
```

---

## Q5：`search_text` / `find_files` / `find_symbol` 有什么区别？

| 工具 | 搜什么 | 怎么搜 | 返回什么 |
|------|--------|-------|---------|
| `search_text` | 文件**内容** | 正则匹配每行 | 文件:行号: 匹配行 |
| `find_files` | 文件**名称** | glob 文件名模式 | 文件路径列表 |
| `find_symbol` | Python 定义 | 正则匹配 `def`/`class` | 文件:行号: 类型 名字 |

### search_text（内容搜索）

```python
# tools/search_tool.py:82-130
def execute(self, params):
    for filepath in _iter_files(search_path, file_pattern):
        for lineno, line in enumerate(filepath.read_text().splitlines()):
            if regex.search(line):  # 正则匹配
                matches.append(f"{filepath}:{lineno}: {line}")
```

**用途**：找到某段代码/字符串出现在哪些地方
```python
# LLM 想找所有用到 "parse_date" 的地方：
search_text(pattern="parse_date")
# 返回：
# src/parser.py:42: result = parse_date(input_str)
# src/utils.py:18: from parser import parse_date
# tests/test_parser.py:5: def test_parse_date():
```

### find_files（文件名搜索）

```python
# tools/search_tool.py:171-199
def execute(self, params):
    for filepath in _iter_files(search_path, pattern):  # glob 匹配
        results.append(str(filepath))
```

**用途**：找到符合某种命名规则的文件
```python
# LLM 想找到所有测试文件：
find_files(pattern="test_*.py")
# 返回：
# tests/test_parser.py
# tests/test_api.py
# tests/test_auth.py
```

### find_symbol（符号搜索）

```python
# tools/search_tool.py:241-280
def execute(self, params):
    pattern = re.compile(rf"^(\s*)(def|class)\s+({symbol}\w*)\s*[:(]")
    for filepath in _iter_files(search_path, "*.py"):
        for m in pattern.finditer(content):
            kind = m.group(2)  # def / class
            scope = "method" if indent > 0 else "top-level"
            matches.append(f"{filepath}:{lineno}: {kind} {name} ({scope})")
```

**用途**：找到某个函数/类在哪个文件定义
```python
# LLM 想找到 "validate" 相关的函数/类定义：
find_symbol(symbol="validate")
# 返回：
# src/validator.py:15: class Validator (top-level)
# src/validator.py:42: def validate_email (method)
# src/api.py:10: def validate_token (top-level)
```

### 选择合适的搜索

```
问题                          用哪个
─────────────────────────────────────────
"find all files named config*.py"  → find_files
"where is the validate function?"   → find_symbol
"which files use the deprecated API" → search_text
"find all test files"               → find_files
"find the Database class definition" → find_symbol
```

---

## Q6：当前项目的"记忆系统"详细解释

**本项目没有"记忆系统"。** 这一点很重要——Agent 的"记忆"就是**对话历史（ConversationHistory）**，没有独立的记忆模块或向量数据库。

### 所谓"记忆"到底是什么

项目中的"记忆"由三个层层递进的机制构成：

```
┌─────────────────────────────────────────┐
│             第 1 层：本轮对话历史          │
│  Agent 主循环内，每一步的 Action +        │
│  Observation 都追加到 messages，           │
│  下一轮 LLM 调用能看到之前所有步骤         │
├─────────────────────────────────────────┤
│             第 2 层：跨轮共享历史          │
│  ChatSession 持有一个 ConversationHistory， │
│  用户每轮的输入和 Agent 每轮的总结都追加到它│
│  下一轮 Agent 能记得之前说过什么            │
├─────────────────────────────────────────┤
│             第 3 层：repo-map              │
│  整个仓库的符号结构注入 system prompt，     │
│  让 Agent 从一开始就知道项目结构            │
└─────────────────────────────────────────┘
```

### 第 1 层：本轮对话历史

```python
# agent/core.py:216-223
# 每步的 Action 和 Observation 都追加到 history
history.add(LLMMessage(role="assistant", content=self._format_action_for_history(action)))
history.add(LLMMessage(role="user", content=self._format_observation_for_history(observation)))
```

所以如果 Agent 连续运行 10 步，LLM 在第 11 步能看到前面 10 步的完整记录。

### 第 2 层：跨轮共享历史（ChatSession）

```python
# entry/chat.py:225-228
# ChatSession 持有一个"共享"history
self._shared_history = ConversationHistory(max_messages=config.context.history_window * 2)

# 每轮用户输入 → 追加到共享 history
# agent/core.py:278-282
self._shared_history.add(LLMMessage(role="user", content=user_input))

# Agent 运行完后，结果也追加到共享 history
self._shared_history.add(LLMMessage(role="assistant", content=f"[Round {self.round_count} complete]\n{result.summary}"))
```

所以你可以这样聊天：

```
you > 帮我看看 src/main.py 有什么问题
    → Agent 读文件，发现一个 bug

you > 把 bug 修了
    → Agent 记得上一轮看到了什么文件、什么 bug，直接改

you > 再跑一下测试
    → Agent 知道修改了什么，跑测试验证
```

### 第 3 层：repo-map

```python
# context/repo_map.py:165-185
# 在 system prompt 中注入整个仓库的符号摘要
# Agent 不需要"记忆"项目结构——它从一开始就知道了
```

### 这里为什么没有"记忆系统"

传统"记忆系统"（如 LangChain 的 Memory）会把对话历史存储到向量数据库，下一次会话时通过相似度检索找回。本项目不做这个，因为：

1. **场景不需要**——Agent 是一次性修复任务（run 模式）或持续对话（chat 模式），不需要隔天回来还记得
2. **简化架构**——ConversationHistory 的滑动窗口 + TokenBudget 裁剪已经解决了"记太多 vs 记太少"的矛盾
3. **ChatSession 就是记忆**——跨轮对话的历史仍然在内存里，只是不持久化

---

## Q7：零基础完整体验 chat + run + git 功能

> 以下是从零开始的逐步操作流程，假设你已经在 `D:\fianll-test\forge\forge-agent` 目录下。

### 第 0 步：确认环境

```cmd
:: 打开 CMD 终端，执行以下命令确认就绪

:: 1. 确认你在正确的目录
cd D:\fianll-test\forge\forge-agent

:: 2. 确认虚拟环境已激活（提示符前有 (.venv)）
.venv\Scripts\activate

:: 3. 确认 API Key 已设置
set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx

:: 3.1 验证 key 设置成功（会显示你刚设的 key）
echo %DEEPSEEK_API_KEY%

:: 4. 确认一切正常
python --version   ← 必须显示 Python 3.11.x
agent --help       ← 必须显示命令帮助
```

> 如果第 4 步有问题，说明尚未安装依赖，执行 `pip install -e ".[dev]"`。

---

### 第 1 步：体验 run 模式（一次性任务）

run 模式适合"一次性明确任务"。Agent 拿到任务后执行完毕即退出。

#### 1.1 最简单的 run 命令

```cmd
agent run --task "列出当前目录下所有的 Python 文件" --max-steps 5
```

你会看到：
```
🤖 Coding Agent
  Provider : deepseek
  Model    : deepseek-v4-flash
  Repo     : D:\fianll-test\forge\forge-agent
  Max steps: 5

[Step 1] shell  ls -la
  ✓
  ...

[Step 2] find_files  *.py
  ✓
  agent/core.py
  ...

[Step 3] finish
✓ COMPLETE: ...
```

**看到这步说明**：你的环境已完全就绪，Agent 可以通过 LLM 执行任务了 ✅

#### 1.2 让 Agent 创建一个测试文件

```cmd
agent run --task "在 D:\fianll-test\forge\forge-agent\hallo.py 创建一个文件，写入 print('hello from agent!')，然后用 Python 运行它验证" --max-steps 8
```

**预期结果**：Agent 会创建文件并运行，控制台输出 `hello from agent!`。

#### 1.3 查看 Agent 修改了什么（git diff）

```cmd
git diff
```

你会看到 Agent 创建的 `hallo.py` 文件的内容。

#### 1.4 可选：撤销 Agent 的修改

```cmd
:: 删除 Agent 创建的文件
del hallo.py

:: 或者如果动了其他文件
git checkout -- .
```

---

### 第 2 步：体验 chat 模式（持续对话）

chat 模式适合"多轮对话、边改边聊"的场景。

#### 2.1 启动 chat 模式

```cmd
agent chat
```

你会看到：
```
🤖 Coding Agent — Chat Mode
  Provider : deepseek
  Model    : deepseek-v4-flash
  Repo     : D:\fianll-test\forge\forge-agent
  Type your task. Commands: /exit /stats /clear /help

you >
```

#### 2.2 第一轮对话：了解项目

在 `you >` 提示符后输入：

```
帮我看看这个项目有哪些模块，分别负责什么
```

Agent 会逐步探索代码库，输出类似：

```
  Agent working...
  （流式输出推理过程）

  [1] shell  ls -la
  ✓

  [2] find_files  *.py
  ✓
  ...

  [3] file_read  agent/core.py
  ✓

  ─── Round 1 · 3 steps · 1,234 tokens · 5.2s ───
```

#### 2.3 第二轮对话：上下文延续

```
看看 tools/ 目录下有哪些工具文件，分别是什么用途
```

Agent 记得上一轮看到的结构（`_shared_history` 中的跨轮记忆），会继续探索 tools/ 目录。

#### 2.4 第三轮对话：基于上下文修改代码

```
在 agent/core.py 的第 95 行的 run 方法定义前，加一行注释 # Main ReAct loop entry
```

Agent 会读文件 → 找到第 95 行 → 写入修改后的内容 → 验证。

#### 2.5 查看统计

```
/stats
```

显示：
```
  Session stats:
    Rounds  : 3
    Steps   : 8
    Tokens  : 5,234
```

#### 2.6 退出 chat

```
/exit
```

---

### 第 3 步：体验 git 功能

Agent 有 4 个 git 工具，你可以让 Agent 自主管理 git 操作。

#### 3.1 在 chat 中让 Agent 使用 git

```cmd
agent chat
```

**输入**：
```
创建一个 demo.py 文件，写入 x = "test git"
然后执行 git status 和 git diff 查看状态，最后用 git add 和 git commit 提交
commit message 写 "add demo file"
```

你会看到 Agent 连续调用 git 工具：

```
  [1] file_write  demo.py
  ✓  Written 1 lines

  [2] git_status
  ✓  ## main...origin/main
     ?? demo.py

  [3] git_add
  ✓  Staged: .

  [4] git_commit  add demo file
  ✓  [main 1234567] add demo file

  [5] finish
  ✓  Task complete
```

#### 3.2 验证提交成功

退出 chat 后，在终端执行：

```cmd
git log --oneline -3
```

你会看到刚刚 Agent 提交的记录。

#### 3.3 撤销最后的 commit（如果想")

```cmd
git reset --soft HEAD~1    ← 撤销 commit，但保留文件修改在工作区
del demo.py                ← 删除 Agent 创建的文件
```

---

## Q8：项目从 0 到 1 的搭建故事

### 这个问题没有标准答案

这个项目是一个**个人项目**，每个开发者搭建它的路径都不同。但根据项目代码的结构和注释，可以推断出最可能的搭建顺序：

### 第一阶段：先想清楚要做什么（1 天）

```
我想要做的东西：
- 一个 AI 编程助手，给它一个任务（"修复这个 bug"），它能自己探索代码、修改文件、跑测试
- 不是 ChatBot，是 Agent——会调工具、会自主决策
- 能在终端跑，像 Claude Code 一样
```

### 第二阶段：搭建核心骨架（第 1-3 天）

**第 1 步：定义数据模型（`agent/task.py`）**

最先做的是思考"Agent 运行时需要哪些数据流"：

```python
# 1. 输入：Task（任务描述）
# 2. LLM 输出：Action（做决策） → TOOL_CALL / FINISH / GIVE_UP
# 3. 工具执行返回：Observation（结果）
# 4. 最终产出：RunResult（状态 + patch）
# 5. 日志：Event（每一步的记录）
```

于是定义了 `task.py` 里的 5 个核心 dataclass。

**第 2 步：定义抽象接口（`llm/base.py`、`tools/base.py`）**

先写抽象类，不写具体实现：

```python
class LLMBackend(ABC):
    @abstractmethod
    def complete(self, messages, tools) -> LLMResponse: ...

class BaseTool(ABC):
    @abstractmethod
    def execute(self, params) -> ToolResult: ...
```

**为什么先写抽象？** 因为 Agent 主循环 `core.py` 需要依赖它们——先定接口再写实现，"面向接口编程"。

**第 3 步：用 MockBackend 驱动 Agent 主循环（`agent/core.py` + `llm/base.py`）**

这里是关键决策：**不先接真实 LLM，先用 MockBackend**。

```python
# 构建最简单的 Agent 循环
script = [Action(TOOL_CALL, ...), Action(FINISH, "Done")]
backend = MockBackend(script)
agent = Agent(backend, registry)
result = agent.run(task, log)
assert result.status == SUCCESS  # 验证主循环能跑通
```

这一步走完时，Agent 的主循环骨架就成型了。

### 第三阶段：填工具实现（第 4-7 天）

按"Agent 最需要什么"的优先级顺序实现工具：

```
第 1 批：shell + file_read + file_write
         → Agent 能"看代码"和"改代码"了

第 2 批：search_text + find_files + find_symbol
         → Agent 能搜索代码了

第 3 批：test
         → Agent 能跑测试验证了

第 4 批：git_status + git_diff + git_add + git_commit
         → Agent 能管理自己的修改了

第 5 批：file_view
         → 大文件也能看了
```

### 第四阶段：接真实 LLM（第 8-10 天）

```python
# llm/router.py
if provider == "anthropic":
    return AnthropicBackend(...)
elif provider == "openai" or "deepseek" or ...:
    return OpenAICompatBackend(...)
```

**走的弯路**：DeepSeek R1 不支持 function calling，所以做了第 135-166 行的文本解析 fallback。

### 第五阶段：上下文管理（第 11-12 天）

引入了：
- `context/history.py`：滑动窗口，防止历史无限增长
- `context/token_budget.py`：按优先级分配 token 预算
- `context/repo_map.py`：仓库地图，让 LLM 了解项目结构

### 第六阶段：入口层 + 安全机制（第 13-15 天）

- `entry/cli.py`：Click CLI，支持 `agent run` / `agent chat` / `agent log`
- `entry/chat.py`：持续对话，跨轮历史传递
- `entry/github_issue.py`：Issue → PR 自动化
- `tools/shell_tool.py` 的三层安全防护 + `tools/runtime.py` 的 Docker 沙箱

### 第七阶段：测试覆盖（贯穿始终）

不是最后才写测试，而是**每写完一个模块就写对应的测试**：

```
写完 task.py  → test_day1.py（数据类序列化/反序列化）
写完 core.py  → test_day2.py（MockBackend 驱动 6 种路径）
写完 工具     → test_day3.py（真实文件系统测试）
写完 LLM     → test_day4.py（mock SDK 验证请求格式）
写完 上下文   → test_day5.py（token 裁剪/repo-map 集成）
写完 CLI     → test_day6.py（Click CliRunner）
写完 集成     → test_day7.py（retry/patch/跨模块）
```

**376 个测试就是这样逐模块积累出来的。**

### 走过的弯路（从代码中可以看出来的）

| 弯路 | 代码证据 | 后来的改进 |
|------|---------|-----------|
| 最开始直接用真实 LLM 开发，每步等 2 秒 | `llm/base.py` 里 `MockBackend` 是后来加的 | 所有核心逻辑先上 MockBackend 验证 |
| 所有工具代码写在一起 | `tools/base.py` 的 `BaseTool` 抽象 + `ToolRegistry` | 策略模式解耦 |
| 历史直接传所有 messages 给 LLM | `context/history.py` + `context/token_budget.py` | 双层裁剪 |
| chat 模式每轮历史互相隔离 | `entry/chat.py` 的 `_shared_history` | 跨轮共享历史 |
| 每个工具自己调 subprocess | `tools/runtime.py` 的 `Runtime` 抽象 | 依赖注入 |

---

## Q9：零基础完整体验 Docker 沙箱 + GitHub Issue

### 前提说明

**Docker 沙箱**和 **GitHub Issue** 是两个独立功能，可以分别体验。但都需要一些前置条件。

---

### 体验 Docker 沙箱

#### 前置准备

```cmd
:: 1. 确认 Docker Desktop 已安装且运行中
docker --version
:: 输出类似：Docker version 27.x.x

:: 2. 如果 Docker Desktop 没运行，在开始菜单搜索 "Docker Desktop" 打开
:: 3. 等 Docker 图标（右下角或 Dock 上）变成稳定状态（不闪烁）
```

#### 体验步骤

**Step 1：不带沙箱执行（对比用）**

```cmd
agent run --task "运行 python --version 查看 Python 版本" --max-steps 3
```

记录下输出的 Python 版本（比如 3.11.x）。

**Step 2：带沙箱执行**

```cmd
agent run --task "运行 python --version 查看 Python 版本" --max-steps 3 --sandbox
```

你会看到：

```
  Sandbox: Docker (python:3.11-slim)
  ...
  [1] shell  python --version
  ✓
    Python 3.11.x
```

首次执行时 Docker 需要拉取 `python:3.11-slim` 镜像（约 150MB），稍等片刻。

**Step 3：在沙箱中执行复杂命令**

```cmd
agent run --task "使用 Python 列出 /workspace 目录下的所有 Python 文件" --max-steps 5 --sandbox
```

注意 `/workspace` 就是你的项目目录（通过 bind mount 挂载的），容器里能看到。

**Step 4：验证隔离性**

```cmd
agent run --task "运行 python --version；然后执行 pip list 看看装了什么" --max-steps 4 --sandbox
```

容器里的包列表和宿主机不同——这是隔离的表现。

#### Docker 沙箱常见问题

| 问题 | 解决 |
|------|------|
| `docker: command not found` | 安装 Docker Desktop |
| `Docker is not available` | 启动 Docker Desktop |
| 首次拉镜像慢 | 取决于网速，只有第一次需要拉取 |
| `--network none` 导致 curl 失败 | 默认行为——沙箱断网，更安全 |

---

### 体验 GitHub Issue 模式

#### 前置准备

**Step 1：创建 GitHub Token**

1. 打开浏览器，登录 GitHub
2. 点击右上角头像 → Settings → **Developer settings**（左下角）
3. 点击 **Personal access tokens** → **Tokens (classic)**
4. 点击 **Generate new token** → **Generate new token (classic)**
5. 在 **Note** 填 `forge-agent-test`
6. 在 **Select scopes** 勾选 **repo**（全选 repo 下的所有权限）
7. 滑到页面底部，点击 **Generate token**
8. 复制生成的 token（类似 `ghp_xxxxxxxxxxxxxxxxxxxx`）——**关掉页面后就看不到了**

**Step 2：设置 Token**

```cmd
set GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

#### 体验流程

**Step 1：找一个公开仓库测试**

找一个你自己有写权限的仓库，创建一个 Issue（或者找一个已有的 Issue）。

用你的 GitHub 个人仓库（比如 `你的用户名/hello-world`）新建一个 Issue：
1. 进入 https://github.com/你的用户名/hello-world
2. 点击 **Issues** → **New issue**
3. 标题填：`Add a simple test`
4. 内容填：`Add a test for the hello function that checks it returns the expected string`
5. 点击 **Submit new issue**
6. 记下 Issue 编号（比如 #1）

**Step 2：先 clone 仓库到本地**

```cmd
:: 克隆仓库到本地 C:\Temp 目录
cd C:\Temp
git clone https://github.com/你的用户名/hello-world.git
```

**Step 3：运行 GitHub Issue 模式**

```cmd
cd D:\fianll-test\forge\forge-agent

:: 使用 --no-pr 跳过 PR 创建
python -m entry.github_issue ^
    --repo 你的用户名/hello-world ^
    --issue 1 ^
    --local-path C:\Temp\hello-world ^
    --no-pr
```

> `^` 是 CMD 的换行符。如果输错了，可以全部写在一行。

你会看到类似输出：

```
Fetching issue #1 from 你的用户名/hello-world ...
  Title: Add a simple test

Running agent on issue #1 ...
  Status : success
  Steps  : 8
  Tokens : 12,345
  Time   : 35.2s
```

**Step 4：验证 Agent 改了什么**

```cmd
cd C:\Temp\hello-world
git diff
git log --oneline -2
```

你会看到 Agent 在 `agent/fix-issue-1-xxxxxxxx` 分支上提交的修改。

**Step 5：可选——创建 PR**

不带 `--no-pr` 参数（会帮你提 PR）：

```cmd
cd D:\fianll-test\forge\forge-agent
python -m entry.github_issue ^
    --repo 你的用户名/hello-world ^
    --issue 1 ^
    --local-path C:\Temp\hello-world
```

成功后你会看到 PR 的 URL。打开浏览器，登录 GitHub，就能看到 Agent 自动创建的 PR。

#### GitHub Issue 模式完整流程图

```
你操作                       系统自动完成
══════════════════════════════════════════════════════════════
1. 创建 GitHub Token         -
2. 设置 set GITHUB_TOKEN=... -
3. 运行 python -m entry...   -
                              ↓
4. -                        5. 使用 Token 连接 GitHub API
                            6. 拉取 Issue #1 的标题和描述
                            7. Clone 仓库到本地路径
                            8. 创建新分支 agent/fix-issue-1-时间戳
                            9. 构建 Agent（LLM Backend + 12 个工具）
                            10. Agent 运行：
                                - 探索代码仓库
                                - 理解 Issue 描述
                                - 修改文件实现功能
                                - 运行测试验证
                            11. Agent 完成
                            12. 推送到 GitHub（--no-pr 时跳过）
                            13. 创建 PR（--no-pr 时跳过）
                              ↓
14. 查看 PR，审查修改           -
15. 合并 PR 或关闭              -
```

#### 常见问题

| 问题 | 解决 |
|------|------|
| `GITHUB_TOKEN not set` | 运行 `set GITHUB_TOKEN=ghp_xxx` |
| `git clone failed` | 确认仓库地址正确：`你的用户名/仓库名` |
| `403: Bad credentials` | Token 权限不足，检查是否勾选了 `repo` |
| Agent 修改的内容不对 | Issue 描述越具体效果越好 |
| `--no-pr` 是什么 | 跳过 PR 创建，只本地修代码，适合先预览 |
