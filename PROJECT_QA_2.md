# Forge Agent 项目疑问解答（第二期）

> 共 8 个问题，覆盖沙箱机制、设计模式、版本控制、项目使用方式。

---

## Q1：在 Docker 沙箱中只能执行 `shell` / `pytest` / `git` 命令吗？

**不是。** Docker 沙箱中可以执行**任意 bash 命令**——实际上沙箱给你的是一个完整的 Linux 容器。

### 沙箱容器里的环境

启动沙箱时用的镜像是 `python:3.11-slim`，里面预装了：

- Python 3.11 + pip
- Git
- 常用 Linux 工具（bash、ls、cat、grep、find、sed、awk、curl 等）
- 你声明的 dependencies（通过 `setup_cmds` 在容器启动时安装）

### 为什么说"只"支持这三个？

因为你的 Agent 有 12 个工具：

| 工具 | 是否经过 Runtime | 是否进沙箱 |
|------|-----------------|-----------|
| `shell` | ✅ 是 | ✅ 进沙箱 |
| `test`（pytest） | ✅ 是 | ✅ 进沙箱 |
| `git_status` / `git_diff` / `git_add` / `git_commit` | ✅ 是 | ✅ 进沙箱 |
| `file_read` / `file_view` / `file_write` | ❌ 否 | ❌ 不进沙箱 |
| `search_text` / `find_files` / `find_symbol` | ❌ 否 | ❌ 不进沙箱 |

**关键理解**：文件读写和搜索工具**不经过 Runtime**，它们直接用 Python 的 `open()` / `Path.read_text()` 操作文件。但因为 repo 目录通过 bind mount 挂载进了容器，所以：

- 宿主机上 Agent 写文件 → 容器里 `shell` 执行 `python xxx.py` 能读到最新版本
- 容器里 `git commit` 提交 → 宿主机上 git 仓库也同步变化

### `shell` 工具可以执行任意命令

```python
# tools/shell_tool.py——shell 工具接收任意 cmd 字符串
agent.run() → LLM 决定调 shell → shell.execute({"cmd": "any bash command"}) → Runtime.exec(cmd)
```

例如 Agent 在沙箱中可以执行：

```e
pip install requests        # 安装依赖
python -c "print('hello')"  # 执行 Python 代码
curl https://example.com    # 发网络请求（如果开了网络）
cat /etc/os-release         # 读系统信息
```

所以**不是只能用这三个工具**，而是"所有需要执行命令的操作都走这三个工具，这三个工具再通过 Runtime 转发到沙箱"。

---

## Q2：`--sandbox` 的 5 步流程是自动的还是需要手动操作？

**全部自动，不需要任何手动操作。** 你只需要在命令中加 `--sandbox` 一个参数。

```bash
# 你只需要做这一步
agent run --task "运行所有测试" --sandbox
```

### 系统内部自动完成的 5 步

```python
# 步骤 1：CLI 读取 --sandbox 标志
# entry/cli.py:240
runtime = create_runtime(sandbox=True, repo_path=str(repo_path)) if sandbox else None

# 步骤 2：create_runtime 检测到 sandbox=True，创建 DockerRuntime
# tools/runtime.py:398-404
if not sandbox:
    return LocalRuntime()
runtime = DockerRuntime(repo_path=repo_path, image=image)

# 步骤 3：DockerRuntime 被注入到所有需要执行命令的工具
# entry/cli.py:76-87
ShellTool(runtime=runtime)
PytestTool(runtime=runtime)
GitStatusTool(runtime=runtime)
# ... 等等

# 步骤 4：Agent 运行，当 LLM 决定调 shell 时
# tools/shell_tool.py → self._runtime.exec(cmd)
# DockerRuntime.exec() 首次调用时自动启动容器

# 步骤 5：容器启动后，每条命令通过 docker exec 在容器里执行
```

### 一句话总结

> 你只敲了 `--sandbox`，系统自动完成：创建 DockerRuntime → 注入工具 → 懒启动容器 → 所有命令在容器执行 → 会话结束自动清理。你全程不需要和 Docker 交互。

---

## Q3：策略模式 + 依赖注入的详细解释

### 先理解问题本身

项目的核心需求：Agent 需要在**不同环境**下执行命令（本地 / Docker 沙箱），但 Agent 和工具的代码**不应该**为每种环境写一份。

### 策略模式（Strategy Pattern）

**定义**：定义一系列算法（策略），把它们封装起来，让它们可以互相替换。策略模式让算法的变化独立于使用算法的客户端。

**本项目中的应用**：

```python
# Runtime 是"策略"的抽象定义
class Runtime(ABC):
    @abstractmethod
    def exec(self, cmd, cwd, timeout) -> RunResult: ...

# 策略 1：本地执行
class LocalRuntime(Runtime):
    def exec(self, cmd, cwd, timeout):
        return subprocess.run(cmd, shell=True, ...)

# 策略 2：Docker 执行
class DockerRuntime(Runtime):
    def exec(self, cmd, cwd, timeout):
        return subprocess.run(["docker", "exec", ...])
```

两个 `exec()` 方法——一个跑在本机，一个跑在容器里。但对外暴露的接口一模一样。

### 依赖注入（Dependency Injection）

**定义**：不从类内部创建依赖对象，而是从外部"注入"进来。这样类不负责创建它的依赖，只负责使用。

**反面例子（不使用依赖注入）**：

```python
# ❌ ShellTool 自己决定用什么 Runtime——不灵活
class ShellTool:
    def __init__(self):
        self._runtime = LocalRuntime()  # 写死了！没法切换成 Docker

    def execute(self, params):
        return self._runtime.exec(params["cmd"])
```

**本项目（使用依赖注入）**：

```python
# ✅ Runtime 从外部传入——灵活
class ShellTool:
    def __init__(self, runtime=None):
        self._runtime = runtime or LocalRuntime()  # 接收注入，同时提供默认值

    def execute(self, params):
        return self._runtime.exec(params["cmd"])

# 使用者决定传什么 Runtime
tool_local  = ShellTool()                    # 默认 LocalRuntime
tool_docker = ShellTool(runtime=DockerRuntime(...))  # 注入 Docker 策略
```

### 两者结合的效果

```python
# 创建阶段：CLI 根据 --sandbox 决定用什么策略
runtime = create_runtime(sandbox=True, repo_path=repo_path)
#         ^^^^^^^^^^^^^^                                ^
#         策略模式：根据参数选择具体策略                   依赖注入：创建时确定依赖

# 注入阶段：把策略注入到每个需要它的工具
ShellTool(runtime=runtime)       # 注入！
PytestTool(runtime=runtime)      # 注入！
GitStatusTool(runtime=runtime)   # 注入！

# 使用阶段：工具内部完全不知道、也不关心使用哪个 Runtime
self._runtime.exec(cmd)  # ← 一行代码，适配所有执行环境
```

### 类比帮助理解

| 概念 | 日常类比 |
|------|---------|
| **策略模式** | 你有一把通用螺丝刀手柄（接口），可以插十字头、一字头、内六角头（不同策略），插哪个头取决于拧什么螺丝（场景） |
| **依赖注入** | 螺丝刀不自带批头——你从工具箱里挑一个批头**塞进去**（注入），而不是螺丝刀自己生产批头 |
| **两者结合** | 你拿通用手柄，根据螺丝类型从工具箱挑对应批头，塞进去，开始拧 |

---

## Q4：`--sandbox` 决定走本地还是沙箱，原理是什么？

### 你的理解完全正确！

```python
# entry/cli.py:240
runtime = create_runtime(sandbox=sandbox, ...) if sandbox else None
#         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#         sandbox=True  → create_runtime(...) → DockerRuntime
#         sandbox=False → None → 工具走默认 LocalRuntime
```

### 两条路径的完整对比

**路径 A：加了 `--sandbox`**

```
CLI 解析 --sandbox
  → create_runtime(sandbox=True) → DockerRuntime
  → ShellTool(runtime=DockerRuntime)      ← 工具里存的是 DockerRuntime
  → Agent 运行时，工具执行命令
  → self._runtime.exec(cmd)
  → DockerRuntime.exec() → docker exec ...  ← 命令在容器里跑！
```

**路径 B：没加 `--sandbox`**

```
CLI 解析 → sandbox=False
  → runtime = None
  → ShellTool(runtime=None)
  → ShellTool.__init__ 里: self._runtime = None or LocalRuntime()
  →                                      ^^^^^^^^^^^^^^^^^^^^^^^^
  →                                      用了默认的 LocalRuntime！
  → Agent 运行时，工具执行命令
  → self._runtime.exec(cmd)
  → LocalRuntime.exec() → subprocess.run(...)  ← 命令在本机跑！
```

### 关键代码

```python
# tools/shell_tool.py:119
self._runtime = runtime or LocalRuntime()
#               ^^^^^^^    ^^^^^^^^^^^^^^^
#               有注入就用注入的   没注入就用默认本地

# tools/runtime.py:380-399
def create_runtime(sandbox=False, ...):
    if not sandbox:
        return LocalRuntime()              # 不加 --sandbox → 本地
    return DockerRuntime(repo_path=...)    # 加了 --sandbox → Docker
```

### 一句话总结

`--sandbox` 是一个开关：开了 → 创建 DockerRuntime 注入工具 → 命令在容器执行；没开 → runtime=None → 工具用默认 LocalRuntime → 命令在本机执行。

---

## Q5：沙箱只针对调用工具有用吗？读取操作不进沙箱？

### 你的理解是对的！

沙箱**只对需要执行外部命令**的工具起作用。读文件、搜索文件这些操作不进沙箱。

### 具体分类

| 工具 | 实现方式 | 是否进沙箱 | 原因 |
|------|---------|-----------|------|
| `file_read` | `path.read_text(encoding="utf-8")` | ❌ | Python 直接读文件，不走 Runtime |
| `file_view` | `path.read_text(...).splitlines()` | ❌ | 同上 |
| `file_write` | `path.write_text(content)` | ❌ | 同上 |
| `search_text` | `filepath.read_text(...)` + 正则 | ❌ | 同上 |
| `find_files` | `path.rglob(glob_pattern)` | ❌ | 同上 |
| `find_symbol` | `filepath.read_text(...)` + 正则 | ❌ | 同上 |
| `shell` | `Runtime.exec(cmd)` | ✅ | 执行任意命令，必须进沙箱 |
| `test`（pytest） | `Runtime.exec("pytest ...")` | ✅ | 执行 pytest 命令 |
| `git_status` | `Runtime.exec("git status")` | ✅ | 执行 git 命令 |
| `git_diff` | `Runtime.exec("git diff")` | ✅ | 同上 |
| `git_add` | `Runtime.exec("git add ...")` | ✅ | 同上 |
| `git_commit` | `Runtime.exec("git commit ...")` | ✅ | 同上 |

### 那沙箱模式下文件读写怎么办？

因为 Docker 容器通过 **bind mount** 挂载了 repo 目录：

```python
# 宿主机目录 → 容器内目录（实时双向同步）
# D:\fianll-test\forge\forge-agent → /workspace
```

所以读取操作虽然在宿主机上进行，结果和"在沙箱里读"完全一样——因为宿主机和容器看到的文件内容是**同一个文件**。

**举个例子**：

```
[Step 1] Agent 调 file_write: 写 "hello()" → src/main.py
         ← 在宿主机上写的，但容器里 /workspace/src/main.py 已经更新了（bind mount）

[Step 2] Agent 调 shell: python src/main.py
         ← DockerRuntime.exec() → docker exec ... bash -c "python src/main.py"
         ← 容器里读到的就是刚刚宿主机写的文件 ✓
```

---

## Q6：依赖倒置原则的详细解释

### 什么是依赖倒置原则（DIP）

**定义**：高层模块不应该依赖于低层模块，两者都应该依赖于抽象。抽象不应该依赖于细节，细节应该依赖于抽象。

"倒置"的意思是：传统的依赖方向是「高层 → 低层」；依赖倒置后变成「高层 → 抽象 ← 低层」，箭头方向反了。

### 传统做法（❌ 违反 DIP）

```python
# agent/core.py——Agent 直接依赖具体实现
class Agent:
    def __init__(self):
        from llm.anthropic_backend import AnthropicBackend
        self._backend = AnthropicBackend(api_key="sk-xxx")  # 写死了！

    def run(self):
        response = self._backend.complete(messages)  # 只能用 Claude
```

问题：
1. 换模型 = 改 Agent 代码
2. 想测试 = 必须调真实 API（慢、花钱）
3. 想加新 provider = 改 core.py

### 本项目的做法（✅ 遵循 DIP）

**第 1 步：定义抽象接口**

```python
# llm/base.py:68-101
class LLMBackend(ABC):
    """
    所有 LLM backend 必须实现的接口。
    Agent Core 只依赖这个抽象，永不 import 具体 SDK。
    """

    @abstractmethod
    def complete(self, messages, tools) -> LLMResponse: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...
```

**第 2 步：具体实现依赖这个抽象**

```python
# llm/anthropic_backend.py
class AnthropicBackend(LLMBackend):   # ← 依赖 LLMBackend 抽象
    def complete(self, messages, tools):
        # 调 Anthropic SDK...

# llm/openai_compat.py
class OpenAICompatBackend(LLMBackend):  # ← 同样依赖 LLMBackend 抽象
    def complete(self, messages, tools):
        # 调 OpenAI SDK...

# llm/base.py
class MockBackend(LLMBackend):          # ← 测试用，也依赖 LLMBackend 抽象
    def complete(self, messages, tools):
        return self._script[self._index]  # 按剧本返回
```

**第 3 步：Agent 只依赖抽象**

```python
# agent/core.py:81-87
class Agent:
    def __init__(self, backend: LLMBackend, registry, config):
        self._backend = backend   # ← 类型注解是 LLMBackend，不是具体类
```

**第 4 步：由外部（Router/CLI）决定注入哪个具体实现**

```python
# llm/router.py——依赖注入的地方
def create_backend(provider, model, api_key):
    if provider == "anthropic":
        return AnthropicBackend(model, api_key)       # 策略 A
    return OpenAICompatBackend(model, api_key, ...)   # 策略 B

# entry/cli.py——最终组装
backend = create_backend_from_config(config)   # 出：LLMBackend 的具体子类
agent = Agent(backend, registry, config)       # Agent 收到的只是 LLMBackend
```

### 依赖方向图示

```
传统依赖（❌）：
  Agent ──→ AnthropicBackend   ← Agent 依赖具体实现
  Agent ──→ OpenAIBackend      ← 每加一个后端都要改 Agent

依赖倒置（✅）：
  Agent ──→ LLMBackend (抽象) ←── AnthropicBackend (具体实现)
                               ←── OpenAICompatBackend (具体实现)
                               ←── MockBackend (测试实现)
  
  Agent 只认识 LLMBackend
  任何实现了 LLMBackend 接口的东西都能塞给 Agent
```

### 最大的好处：MockBackend 能无缝替换

```python
# 生产环境
backend = AnthropicBackend(model="claude-sonnet-4-5", api_key="sk-xxx")
agent = Agent(backend, registry)  # ← Agent 完全不知道 backend 的类型
# agent.run() → 调真实 Claude API

# 测试环境（同样的 Agent 代码，一行不改）
script = [Action(FINISH, "Done")]
backend = MockBackend(script)     # ← 只需要实现 LLMBackend 接口
agent = Agent(backend, registry)  # ← 完全相同的 Agent 构造
# agent.run() → 不调 API，按剧本返回，毫秒级完成
```

**Agent 代码整个文件（agent/core.py）里没有任何 `import anthropic` 或 `import openai`。**

这就是依赖倒置的价值：换后端不用改 Agent 代码，加新后端只需写一个新类实现 `LLMBackend`。

---

## Q7：Git 工作区详解 + 回退范围

### Git 的三个区域

```
┌──────────────┐    git add   ┌──────────────┐   git commit  ┌──────────────┐
│   工作区      │  ──────────→ │   暂存区      │ ────────────→ │   本地仓库     │
│  (Working    │              │  (Staging    │               │  (Local      │
│   Directory) │              │   Area)      │               │   Repository)│
└──────────────┘              └──────────────┘               └──────────────┘
      ↑                            ↑                              ↑
  你看到的文件                  git add 之后的文件              git commit 之后的快照
  Agent 直接修改这里            (准备提交的"购物车")            (永久保存的"存档")
```

**本项目的关键设计**：Agent 的 `file_write` 工具只修改**工作区**，不会自动 `git add` 或 `git commit`。除非 LLM 显式决定调 `git_add` / `git_commit` 工具。

### 这意味着什么

```bash
# Agent 完成修改后，你的 repo 状态：
git status
# 输出类似：
#   modified:   src/main.py
#   modified:   tests/test_main.py
# （文件改变了，但还没 add 也没 commit，都在工作区）
```

### 回退能回多少步？

**可以回退任意多步**，不限制回退步数。因为你回退的是整个「工作区 + 暂存区」相对于「最后一次 commit」的**全部差异**。

```bash
# ====== 查看 agent 改了什么 ======
git diff                         # 工作区 vs 最后一次 commit 的差异
git diff --stat                  # 只看改了哪些文件

# ====== 撤销方式 1：撤销所有修改 ======
git checkout -- .                # 所有文件回到最后一次 commit 的状态
# 不管 agent 改了多少个文件、改了多少次，一次性全部撤销

# ====== 撤销方式 2：撤销特定文件 ======
git checkout -- src/main.py      # 只撤销这一个文件

# ====== 撤销方式 3：查看修改后决定是否保留 ======
git diff src/main.py             # 先看看 agent 改了什么
git add src/main.py              # 满意 → 暂存
git checkout -- tests/           # 测试文件改得不好 → 撤销
```

### 回退原理详解

```bash
# Agent 执行了 5 轮修改：
# 第 1 轮：改 main.py
# 第 2 轮：改 utils.py
# 第 3 轮：再次改 main.py（因为第一次改得不对）
# 第 4 轮：改 tests/test_main.py
# 第 5 轮：改 utils.py（微调）

# 此时工作区状态：4 个文件的最终版本（都在工作区）
# git 不关心"改了多少次"，只关心"当前工作区 vs 最后一次 commit"

git checkout -- .
# ↑ 这会把所有 4 个文件恢复到 agent 修改之前的状态
# 不是"撤销最后一步"，而是"撤销从最后一次 commit 以来的所有修改"
```

**关键理解**：

> Agent 连续改了 40 步，每一步都是覆盖写入工作区文件。Git 不追踪"每一步"——git 只追踪"当前文件 vs 最后一次 commit"。所以 `git checkout -- .` 是一个**全量撤销**操作，撤销的是从 commit 以来累积的全部修改，不限制步数。

---

## Q8：本项目如何使用？能否在别的项目里用它来修改？

### 本项目与 Claude Code 的关系

是的，本项目**和 Claude Code 一样在终端使用**。它就是一个命令行工具，通过 `pip install -e .` 安装后，你会在终端获得 `agent` 命令。

### 在别的项目里使用本项目

**方式 1：指定 `--repo` 参数（推荐）**

```bash
# 你在 forge-agent 目录下，但想让它修改另一个项目
cd D:\fianll-test\forge\forge-agent
agent run --repo D:\my-project --task "修复 tests/test_api.py 里的失败测试"

# chat 模式也一样
agent chat --repo D:\my-project
```

Agent 会在 `D:\my-project` 目录里探索代码、修改文件、运行测试——跟你在那个项目目录里操作一样。

**方式 2：先 cd 过去，再运行**

```bash
cd D:\my-project
agent run --task "重构 src/database.py，把 15 个方法的类拆成两个类"
# --repo 默认是当前目录
```

**方式 3：从任意位置，用绝对路径**

```bash
# 不管当前在哪个目录
agent run --repo /absolute/path/to/any/project --task "你的任务"
```

### 完整示例：用 forge-agent 去修复另一个项目

假设你有另一个项目 `D:\projects\my-web-app`，测试文件 `tests/test_login.py` 报错：

```bash
# 1. 进入 forge-agent 目录
cd D:\fianll-test\forge\forge-agent

# 2. 激活虚拟环境
.venv\Scripts\activate

# 3. 确保 API Key 已设置
set DEEPSEEK_API_KEY=sk-xxx

# 4. 用 chat 模式交互式修复
agent chat --repo D:\projects\my-web-app
# 输入：tests/test_login.py 的 test_empty_password 失败了，帮我修

# 5. 或者用 run 模式一次性修复
agent run --repo D:\projects\my-web-app \
    --task "修复 tests/test_login.py::test_empty_password，输入空密码应该返回 400 而不是 500"

# 6. 去 my-web-app 目录查看 agent 改了什么
cd D:\projects\my-web-app
git diff

# 7. 满意就 commit，不满意就撤销
git add . && git commit -m "fix: login returns 400 on empty password"
# 或者
git checkout -- .
```

### 工作原理

```
你在终端输入：
  agent run --repo D:\projects\my-web-app --task "修 bug"

系统内部：
  1. CLI 解析参数: repo_path = "D:\projects\my-web-app"
  2. 创建 Task(description="修 bug", repo_path="D:\projects\my-web-app")
  3. RepoMap("D:\projects\my-web-app").build()  ← 扫描 my-web-app 的代码结构
  4. Agent 在 my-web-app 目录内运行：
     - file_read / file_write → 读写 my-web-app 的文件
     - shell("pytest") → 在 my-web-app 目录下运行测试
     - git_status / git_diff → 查看 my-web-app 的 git 状态
  5. 返回 RunResult
```

**结论**：本项目是一个**通用的**编程 Agent。它不绑定特定项目——你把它指向哪个 repo，它就在哪个 repo 里工作。在 forge-agent 目录下 `agent chat --repo /path/to/other-project` 就能让它修改任何你指定的项目。