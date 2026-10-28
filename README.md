# Forge Agent

自主编程智能体。给它一个任务描述，它会自己探索代码库、修改文件、运行测试，直到完成。

支持 **Claude、DeepSeek、OpenAI、Groq、Ollama** 多种模型，内置流式输出、Docker 沙箱、GitHub Issue 自动修复。

---

## 快速开始

```bash
# 安装
git clone <repo-url> && cd coding-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 首先构建 Linux Runtime（需要启动 Docker 的 Linux 引擎）
docker build -f docker/Dockerfile.runtime -t forge-runtime:py311 .

# 本地窗口配置 MiMo；密钥保存在被 Git 忽略的 config/mimo.local.yaml
python -m entry.configure_mimo
# 无图形界面的环境可以设置 MIMO_API_KEY，并使用 config/default.yaml

# 验证
python -m evaluation.smoke_agent  # 两个真实 MiMo 模型的小任务验证

# 使用
cd your-project
agent chat
```

修改类任务只有在产生新 diff、且最后一次修改后使用 `test` 工具尝试定向测试，才能调用 `finish` 完成。
测试失败会明确记录在结果中。只读分析可以使用 `agent run --task-type read_only --task "解释项目结构"`。
`file_edit` 用于唯一文本替换，`apply_patch` 接受标准 unified diff；大型现有文件禁止 `file_write` 全量覆盖。
默认命令在 Linux Docker 中执行；`--no-sandbox` 仅支持 Linux/WSL。

修复说明、恢复方式和 Flash/Pro 对照评测见 [docs/architecture-repair.md](docs/architecture-repair.md)。

---

## 使用方式

### chat 模式（推荐）

持续对话，每轮历史保留，最接近 Claude Code 的体验：

```bash
agent chat                            # 当前目录
agent chat --repo /path/to/project   # 指定目录
agent chat --model deepseek-v4-pro   # 切换模型
agent chat --sandbox                  # Docker 沙箱
```

对话内命令：`/exit` 退出、`/stats` 查看统计、`/clear` 清空历史、`/help` 帮助

### run 模式

一次性任务，适合明确的批处理场景：

```bash
agent run --task "修复所有 failing 的测试"
agent run --task-file task.txt           # 从文件读任务
agent run --task "..." --confirm         # 危险命令需确认
agent run --task "..." --sandbox         # Docker 沙箱
```

### GitHub Issue 自动修复

```bash
export GITHUB_TOKEN=ghp_xxx
python -m entry.github_issue \
    --repo owner/repo --issue 42 --local-path /tmp/myrepo
```

自动拉取 Issue → 运行 agent → 提交 PR。

---

## 配置

编辑 `config/default.yaml`：

```yaml
llm:
  provider: deepseek                      # anthropic | openai | deepseek | groq | ollama
  model: deepseek-v4-flash
  api_key: ${DEEPSEEK_API_KEY}            # 从环境变量读取
  base_url: https://api.deepseek.com      # OpenAI-compatible 时填写，anthropic 留空

agent:
  max_steps: 40           # 每轮最大步数
  budget_tokens: 80000    # token 预算

context:
  repo_map_budget: 8000   # repo-map 注入量
  history_window: 20      # 保留历史轮数
```

---

## 项目结构

```
coding-agent/
├── agent/              # 核心：ReAct 主循环、事件日志、数据结构
│   ├── core.py         # Agent 类，驱动整个运行循环
│   ├── task.py         # Task / Action / Observation / RunResult 数据类
│   ├── event_log.py    # JSONL append-only 事件流，支持回放
│   └── prompt.py       # System prompt 模板
│
├── llm/                # LLM 后端
│   ├── base.py         # LLMBackend 抽象基类，含默认 stream()
│   ├── anthropic_backend.py   # Claude 原生（tool_use + 流式）
│   ├── openai_compat.py       # OpenAI / DeepSeek / Groq / Ollama
│   └── router.py       # 按配置选择 backend
│
├── tools/              # 工具层（agent 可调用的操作）
│   ├── base.py         # BaseTool + ToolRegistry
│   ├── file_tool.py    # 文件读写查看
│   ├── shell_tool.py   # Shell 执行（四层安全防护）
│   ├── search_tool.py  # 文本搜索 / 文件查找 / 符号定位
│   ├── test_tool.py    # pytest 执行 + 结构化结果解析
│   ├── git_tool.py     # git status / diff / add / commit
│   └── runtime.py      # LocalRuntime / DockerRuntime
│
├── context/            # 上下文管理
│   ├── repo_map.py     # tree-sitter 多语言符号提取，生成 repo 摘要
│   ├── token_budget.py # Token 预算分配与裁剪
│   └── history.py      # 对话历史滑动窗口
│
├── entry/              # 入口层
│   ├── cli.py          # Click CLI（run / chat / log 子命令）
│   ├── chat.py         # ChatSession，跨轮持久化历史
│   └── github_issue.py # GitHub Issue → PR 自动化
│
├── config/
│   ├── default.yaml    # 默认配置
│   └── schema.py       # 配置加载与校验
│
├── tests/              # 376 个测试，覆盖所有模块
├── smoke_test.py       # 端到端联通验证
├── quicksort_task.py   # 示例任务脚本
└── USAGE.md            # 完整使用教程
```

---

## 核心特性

**多模型支持**
- Anthropic Claude（原生 tool_use）
- OpenAI、DeepSeek、Groq、Ollama（OpenAI-compatible）
- DeepSeek R1 等不支持 function calling 的模型走文本解析 fallback
- 配置文件一行切换，或 `--model` 参数临时覆盖

**多语言 Repo-map**
用 tree-sitter 精确提取符号（函数、类、方法），生成 repo 摘要注入 system prompt，
支持 Python / JavaScript / TypeScript / Go / Rust / Java / C++ / C / Ruby。

**流式输出**
模型 thought 逐 token 实时打印，工具调用实时显示，体验接近 Claude Code。

**安全机制（三层）**
- 硬拦截黑名单：`rm -rf /`、`mkfs` 等永不执行
- 只读白名单：`ls`、`grep`、`git status`、`pytest` 等直接执行
- 写操作确认：`--confirm` 模式下 `git commit`、`pip install` 等需 y/n 确认

**Docker 沙箱**
默认启用 `--sandbox`，所有命令在预先构建的 `forge-runtime:py311` Linux 容器里执行，
repo 通过 bind mount 双向同步，默认断网。

**Reflection 机制**
- 测试失败 → 自动触发反思 prompt，重新分析错误原因
- 连续 6 步无文件修改 → 触发反思，防止探索死循环
- 连续 3 步相同操作 → 判定死循环，自动终止

**事件日志**
每次运行生成 JSONL 日志，记录所有 action / observation / reflection，
支持完整回放和统计分析。

---

## 安全说明

`--confirm` 模式（`run`）和 `chat` 模式默认对写操作要求确认，执行前显示：

```
  ⚠  Agent wants to run:
     $ git commit -m "fix parser bug"
  Allow? [y/N]
```

`--sandbox` 在 Docker 容器中执行命令，目标仓库通过 bind mount 同步修改。

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest                     # 全量；最近验证结果见 docs/repair-results.md
pytest tests/test_day3.py  # 单个文件

# 可选：更多语言的 tree-sitter 支持
pip install tree-sitter-javascript tree-sitter-typescript \
            tree-sitter-go tree-sitter-rust tree-sitter-java

# 可选：精确 token 计数
pip install tiktoken
```

---

## 命令参考

```bash
# chat
agent chat [--repo PATH] [--model MODEL] [--sandbox] [-v]

# run
agent run --task TEXT [--repo PATH] [--task-file FILE]
          [--model MODEL] [--confirm] [--sandbox] [--no-stream] [-v]

# log
agent log list [--dir DIR]
agent log show LOG_FILE

# github issue
python -m entry.github_issue \
    -r owner/repo -i ISSUE_NUM -l LOCAL_PATH [--no-pr] [-v]
```

详细用法见 [USAGE.md](USAGE.md)。
