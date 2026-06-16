# Forge Agent — Windows 环境配置详细流程

> 本文档是从 OPERATION_GUIDE.md 中独立出来的环境配置详细流程，
> 覆盖 Windows 10/11 上从零搭建 Forge Agent 开发/运行环境的每一步。

---

## 目录

- [A.1 基础环境检查](#a1-基础环境检查)
- [A.2 克隆项目并创建虚拟环境](#a2-克隆项目并创建虚拟环境)
- [A.3 安装项目依赖](#a3-安装项目依赖)
- [A.4 安装多语言 tree-sitter（可选）](#a4-安装多语言-tree-sitter可选)
- [A.5 Docker Desktop 安装与验证](#a5-docker-desktop-安装与验证)
- [A.6 API Key 配置](#a6-api-key-配置)
- [A.7 验证环境完整可用](#a7-验证环境完整可用)
- [A.8 依赖安装失败时的分步方案](#a8-依赖安装失败时的分步方案)
- [A.9 常见环境问题排查](#a9-常见环境问题排查)
- [A.10 Windows 与 Linux 环境差异总结](#a10-windows-与-linux-环境差异总结)

---

## A.1 基础环境检查

### Python 版本确认

项目要求 **Python 3.11+**。打开 CMD：

```cmd
python --version
```

应输出 `Python 3.11.x` 或更高。

如果输出 3.10 或更低，或者提示"python 不是内部命令"：
1. 访问 https://www.python.org/downloads/
2. 下载 Python 3.11.x（64-bit Windows installer）
3. 运行安装程序，**务必勾选底部的 "Add Python to PATH"**
4. 安装完成后重新打开 CMD 验证

### Git 确认

Agent 内置了 `git_status`、`git_diff`、`git_add`、`git_commit` 四个 Git 工具，且 Agent 完成任务时会执行 `git diff HEAD` 生成 patch。因此 Git 是必须的：

```cmd
git --version
```

应输出 `git version 2.x.x`。

如果没有：
1. 访问 https://git-scm.com/download/win
2. 下载 64-bit Git for Windows Setup
3. 安装时关键选项：
   - **"Adjusting your PATH environment"** → 选择 **"Git from the command line and also from 3rd-party software"**
   - 其余保持默认
4. 安装完成后重新打开 CMD 验证

### 验证 bash 可用（可选但推荐）

Git for Windows 自带 `bash.exe`，项目在非 Docker 模式下如果 LLM 生成了 Unix 命令，可以通过 bash 执行：

```cmd
bash --version
```

如果输出版本号 → bash 在 PATH 中，没问题。
如果失败 → 将 `C:\Program Files\Git\bin` 加入系统 PATH 环境变量。

---

## A.2 克隆项目并创建虚拟环境

```cmd
:: 进入工作目录
cd /d D:\fianll-test\forge

:: 如果还没有克隆项目
git clone <repo-url> forge-agent
cd forge-agent

:: 创建 Python 3.11 虚拟环境
python -m venv .venv

:: 激活虚拟环境
.venv\Scripts\activate
```

终端提示符前出现 `(.venv)` 表示虚拟环境已激活。

**每次重新打开 CMD 时需要重新激活**：

```cmd
cd /d D:\fianll-test\forge\forge-agent
.venv\Scripts\activate
```

---

## A.3 安装项目依赖

### 标准安装

```cmd
:: 升级 pip（避免安装时报版本过旧）
python -m pip install --upgrade pip

:: 安装核心依赖 + 开发依赖（pytest）
pip install -e ".[dev]"

:: 验证 CLI 入口可用
agent --help
```

应输出 CLI 帮助信息。

### 各依赖用途说明

| 依赖 | 用途 | 是否必装 |
|------|------|:--:|
| `anthropic >= 0.30.0` | Claude API 原生 SDK，支持 tool_use 和流式 | ✅ 核心 |
| `openai >= 1.30.0` | OpenAI-compatible SDK，覆盖 DeepSeek/Groq/Ollama | ✅ 核心 |
| `click >= 8.1.0` | CLI 框架 | ✅ 核心 |
| `rich >= 13.0.0` | 终端彩色输出 | ✅ 核心 |
| `pyyaml >= 6.0` | 配置文件解析 | ✅ 核心 |
| `tree-sitter >= 0.22.0` | AST 解析引擎（repo-map 核心依赖） | ✅ 核心 |
| `tree-sitter-python >= 0.22.0` | Python 语言语法包 | ✅ 核心 |
| `PyGithub >= 2.3.0` | GitHub API 客户端（Issue 拉取 + PR 创建） | ✅ 核心 |
| `gitpython >= 3.1.0` | Git 操作封装 | ✅ 核心 |
| `pytest >= 8.0.0` | 测试框架 | ✅ 开发 |
| `pytest-cov >= 5.0.0` | 测试覆盖率 | ✅ 开发 |
| `tiktoken >= 0.7.0` | 精确 token 计数 | 🟡 可选（不可用时自动降级） |
| `tree-sitter-javascript` 等 | 更多语言的语法解析 | 🟡 可选（未安装自动降级为正则） |

---

## A.4 安装多语言 tree-sitter（可选）

默认只安装了 Python 语言包。如需其他语言的精确符号提取：

```cmd
pip install tree-sitter-javascript tree-sitter-typescript
pip install tree-sitter-go tree-sitter-rust tree-sitter-java
pip install tree-sitter-cpp tree-sitter-c tree-sitter-ruby
```

> **不装也没关系**：`context/repo_map.py` 在 tree-sitter 不可用时会自动降级为正则匹配
> （`_extract_symbols_regex`），不会报错，只是符号识别的精确度稍低（无法区分顶层函数
> 和类内部方法）。

---

## A.5 Docker Desktop 安装与验证

Docker 是 `--sandbox` 模式的前提。不使用沙箱模式可以跳过本节。

### 为什么 Windows 用户需要 Docker

项目在 `--sandbox` 模式下的所有命令都在 Linux 容器内执行。容器内 `bash`、`grep`、
`sleep`、`tail` 等 Unix 命令全部可用。这意味着即使用户的宿主机是 Windows，加了
`--sandbox` 后 Agent 的行为与 Linux 上完全一致。这是项目设计的跨平台策略——
**用 Docker 做 OS 抽象层**。

### 安装步骤

1. 访问 https://www.docker.com/products/docker-desktop/
2. 下载 **Docker Desktop for Windows**（x86_64 或 ARM64，视 CPU 而定）
3. 运行安装程序，使用默认选项（WSL 2 backend）
4. 安装完成后**重启电脑**（Docker Desktop 首次启动需要）
5. 从开始菜单启动 Docker Desktop
6. 等待系统托盘图标变为绿色（约 30 秒–1 分钟）

### 验证 Docker 可用

```cmd
:: 版本检查
docker --version
:: 应输出：Docker version 2x.x.x

:: 引擎状态检查
docker info
:: 应输出大量配置信息，不报错

:: 简单功能验证
docker run --rm hello-world
:: 应输出 "Hello from Docker!" 和一段欢迎信息
```

### 确认 Linux 容器模式

```cmd
docker info | findstr "OSType"
```

应输出 `OSType: linux`。

如果不是（显示 `windows`）：
- 在系统托盘 Docker 图标上右键
- 取消勾选 "Switch to Windows containers..."
- Docker Desktop 会提示重启

### 提前拉取沙箱镜像（节省首次使用等待时间）

```cmd
:: 项目使用 python:3.11-slim 镜像（约 150MB）
docker pull python:3.11-slim
```

---

## A.6 API Key 配置

### 方式一：环境变量（推荐）

```cmd
:: 当前 CMD 窗口有效
set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx

:: 永久设置（系统级，以管理员身份运行 CMD）
setx DEEPSEEK_API_KEY "sk-xxxxxxxxxxxxxxxx"

:: 验证
echo %DEEPSEEK_API_KEY%
```

> 注意：`setx` 设置后**当前 CMD 窗口不会立即生效**，需重新打开一个新 CMD 窗口。

### 方式二：修改配置文件（适合单机开发）

编辑 `config\default.yaml`，将 `api_key` 字段直接填写：

```yaml
llm:
  provider: deepseek
  model: deepseek-v4-flash
  api_key: sk-xxxxxxxxxxxxxxxx          # 直接填，不用 ${} 了
  base_url: https://api.deepseek.com
```

> ⚠️ 注意安全：直接填写密钥的配置文件**不要提交到 git**。

### 各 Provider 的 Key 获取地址

| Provider | 获取地址 | 环境变量名 | 备注 |
|----------|---------|-----------|------|
| DeepSeek | https://platform.deepseek.com/api_keys | `DEEPSEEK_API_KEY` | 性价比高，推荐 |
| Anthropic | https://console.anthropic.com/settings/keys | `ANTHROPIC_API_KEY` | Claude 模型 |
| OpenAI | https://platform.openai.com/api-keys | `OPENAI_API_KEY` | GPT 系列 |
| Groq | https://console.groq.com/keys | `GROQ_API_KEY` | 开源模型推理，速度极快 |
| Ollama | 本地运行 `ollama pull llama3` | 不需要 | 本地运行，免费 |

---

## A.7 验证环境完整可用

### 第一步：全量测试套件（2 秒）

```cmd
pytest -v --tb=line 2>&1 | findstr /C:"passed" /C:"failed" /C:"skipped"
```

期望输出：`376 passed, 7 skipped`

> Windows 上可能有 1-3 个测试因 `sleep` 命令等 Unix 工具缺失而失败或 skip。
> 这些是测试用例本身的 Linux 假设，不影响 Agent 核心功能。

### 第二步：冒烟测试（约 15 秒，需要真实 LLM）

```cmd
python smoke_test.py
```

期望输出：

```
🤖 Coding Agent — Smoke Test

Provider : deepseek
Model    : deepseek-v4-flash

✓ LLM backend created

Tools    : shell, file_read, file_view, file_write, search_text,
           find_files, find_symbol, test, git_status, git_diff,
           git_add, git_commit

  TASK: Create a file named hello_agent.py ...

[Step 1] Action: tool_call
  ✓ [file_write] success

[Step 2] Action: tool_call
  ✓ [shell] success

✓ COMPLETE

Status   : SUCCESS
Steps    : 3-6
Tokens   : ~3,000-8,000
Time     : ~10-30s
```

**如果这一步通过，说明**：
- ✅ LLM API 联通
- ✅ 12 个工具全部正确注册
- ✅ ReAct 主循环完整运行（Thought → Action → Observation → 循环 → FINISH）
- ✅ Agent 能自主完成编程任务

### 第三步：验证 Docker 沙箱（需要 Docker Desktop 运行中）

```cmd
:: 确认 Docker 在运行
docker info >nul 2>&1 && echo Docker OK || echo Docker 未运行

:: 用沙箱模式跑一个简单任务
agent run --task "echo Hello from Docker sandbox" --sandbox --max-steps 3
```

期望输出：

```
  Sandbox: Docker (python:3.11-slim)

[Step 1] Action: tool_call
  ✓ [shell] success
    Hello from Docker sandbox

✓ COMPLETE
```

---

## A.8 依赖安装失败时的分步方案

### 问题：`pip install -e ".[dev]"` 报 `Microsoft Visual C++ 14.0 is required`

这是因为 `tree-sitter` 系列包需要 C 编译器来编译 C 扩展。

**方案一：安装 C 编译工具链**

1. 访问 https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022
2. 下载 **Build Tools for Visual Studio 2022**
3. 安装时勾选 **"使用 C++ 的桌面开发"**（Desktop development with C++）
4. 安装完成后重新打开 CMD，重新运行 `pip install -e ".[dev]"`

**方案二：分步安装，跳过需要编译的包**

```cmd
:: 第一步：纯 Python 依赖（不需要 C 编译器）
pip install anthropic openai click rich pyyaml PyGithub gitpython

:: 第二步：开发依赖
pip install pytest pytest-cov

:: 第三步：以可编辑模式安装项目本身，但不额外拉依赖
pip install -e . --no-deps

:: 第四步：单独尝试 tree-sitter（失败只影响 repo-map 的多语言精度）
pip install tree-sitter tree-sitter-python

:: 第五步：验证
agent --help
pytest -v --tb=line 2>&1 | findstr /C:"passed"
```

> tree-sitter 安装失败不影响 Agent 核心功能——repo-map 会自动降级为正则匹配。
> 只是符号提取的精度稍低（无法精确区分顶层函数和类方法）。

### 问题：`pip install tiktoken` 失败

tiktoken 是可选依赖，安装失败不影响任何功能——token 计数会自动降级为 `字符数 // 4`。

---

## A.9 常见环境问题排查

| 症状 | 可能原因 | 排查步骤 |
|------|---------|---------|
| `python: command not found` | Python 未安装或不在 PATH | 重装 Python 3.11+，务必勾选 "Add to PATH" |
| `pip: command not found` | pip 不在 PATH | `python -m pip --version` |
| `(.venv)` 没有出现 | 未激活虚拟环境 | `.venv\Scripts\activate` |
| `agent: command not found` | 未安装项目 CLI | `pip install -e .` |
| `error: Microsoft Visual C++ 14.0 is required` | 缺少 C 编译工具链 | 见 A.8 分步安装方案 |
| `DEEPSEEK_API_KEY not set` | 环境变量未配置 | `echo %DEEPSEEK_API_KEY%` 检查 |
| smoke_test 调用 LLM 后卡住不动 | 网络不通 / Key 无效 / 模型慢 | 加 `--verbose` 看日志；检查代理/防火墙 |
| `UnicodeDecodeError: 'gbk'` | 旧版代码编码问题 | **已修复**——确认有 `encoding="utf-8"` |
| `Unknown tool 'finish'` | DeepSeek 模型 function calling 行为 | **已修复**——`agent/core.py` 已加入拦截 |
| pytest 报 "no module named 'agent'" | 工作目录不在项目根 | `cd /d D:\fianll-test\forge\forge-agent && pip install -e .` |
| CMD 输出中文乱码 | 代码页不是 UTF-8 | `chcp 65001` |
| Docker Desktop 启动后一直转圈 | WSL 2 未正确安装 | 管理员 PowerShell：`wsl --install`，然后重启 |
| `docker: error during connect` | Docker Desktop 未运行 | 从开始菜单启动，等图标变绿 |
| Docker 容器内 bind mount 为空 | Windows 文件共享未配置 | Docker Desktop → Settings → Resources → File Sharing → 添加项目目录 |
| `pytest` 部分测试 skip | 正常 | Windows 上 Unix 相关测试会自动 skip |

---

## A.10 Windows 与 Linux 环境差异总结

| 组件 | Linux 行为 | Windows 行为 | Windows 如何对齐 |
|------|-----------|-------------|----------------|
| `LocalRuntime.exec()` | `shell=True` → `/bin/sh` | `shell=True` → `cmd.exe` | 加 `--sandbox` 用 Docker Linux 容器 |
| `subprocess` 编码 | `text=True` 默认 UTF-8 | `text=True` 默认 GBK | **已修复**：全部改为 `encoding="utf-8"` |
| `open()` 默认编码 | UTF-8 | GBK | **已修复**：显式 `encoding="utf-8"` |
| Unix 命令（`grep`/`sleep`/`tail`/`cat`） | 原生可用 | 不存在 | Docker 沙箱内可用；非沙箱建议装 Git Bash |
| 路径分隔符 | `/` | `\` | Python `pathlib.Path` 自动处理 |
| Docker 安装方式 | `apt install docker.io` | Docker Desktop | 行为一致，容器内部始终是 Linux |
| 虚拟环境路径 | `.venv/bin/activate` | `.venv\Scripts\activate` | 功能一致 |
| 环境变量设置 | `export KEY=val` | `set KEY=val` | 功能一致 |
| `findstr` vs `grep` | `grep` 可用 | CMD 用 `findstr` | 本文档全部使用 `findstr` |
| Python `subprocess.run` | `bash -c` | `cmd /c` | `shell=True` 自动选择 |

### 核心结论

- **不加 `--sandbox`** → Windows 上 Agent 的基本功能可用（文件读写、Python 搜索、pytest、Git），但 LLM 可能生成的 Unix shell 命令（如 `grep -r`、`ls -la`）会在 `cmd.exe` 下失败。大部分项目内置了 Python 原生的搜索工具（`search_text`、`find_files`、`find_symbol`），可以补充。
- **加 `--sandbox`** → Agent 所有行为与 Linux 完全一致。**推荐 Windows 用户始终使用 `--sandbox` 模式**进行重要操作。

---

> **配置完成后**，返回 `OPERATION_GUIDE.md` 继续验证简历中的技术点。
