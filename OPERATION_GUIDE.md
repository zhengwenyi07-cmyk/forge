# Forge Agent — 完整体验操作指南（Windows CMD）

> **环境**：Windows 11 · Python 3.11 · CMD 终端  
> **目标**：从零搭建环境 → 逐条验证 `resume-project-interview-prep.md` 中简历描述的每一项技术点  
> **预计耗时**：首次完整走完约 40 分钟（含安装），后续快速验证约 10 分钟

---

## 目录

1. [环境安装](#1-环境安装)
2. [快速验证（5 分钟）](#2-快速验证5-分钟)
3. [简历第 1 段验证：Agent 架构与自省机制](#3-简历第-1-段验证agent-架构与自省机制)
4. [简历第 2 段验证：工具链抽象与多模型适配](#4-简历第-2-段验证工具链抽象与多模型适配)
5. [简历第 3 段验证：上下文管理与可测试性工程](#5-简历第-3-段验证上下文管理与可测试性工程)
6. [简历逐句对照验证清单](#6-简历逐句对照验证清单)
7. [常见问题速查](#7-常见问题速查)

---

## 1. 环境安装

### 1.1 确认 Python 版本

```cmd
python --version
```

必须输出 `Python 3.11.x`。如果不是，从 https://www.python.org/downloads/ 下载 Python 3.11 安装，**勾选 "Add Python to PATH"**。

### 1.2 确认 Git 已安装

```cmd
git --version
```

如果没有，从 https://git-scm.com/download/win 下载安装。Agent 内置的 `git_status`/`git_diff`/`git_add`/`git_commit` 工具依赖 `git` 命令。

### 1.3 进入项目、创建虚拟环境

```cmd
cd /d D:\fianll-test\forge\forge-agent

:: 创建 Python 3.11 虚拟环境（如果还没有）
python -m venv .venv

:: 激活虚拟环境
.venv\Scripts\activate
```

终端提示符前出现 `(.venv)` 表示成功。

### 1.4 安装项目依赖

```cmd
:: 升级 pip（避免安装时报版本过旧）
python -m pip install --upgrade pip

:: 安装核心 + 开发依赖
pip install -e ".[dev]"

:: 验证
agent --help
```

如果 `pip install -e ".[dev]"` 因 tree-sitter 编译失败（报 `Microsoft Visual C++ 14.0 is required`），分步安装：

```cmd
:: 跳过需要编译的包，先装纯 Python 依赖
pip install anthropic openai click rich pyyaml PyGithub gitpython
pip install pytest pytest-cov
pip install -e . --no-deps

:: tree-sitter 单独尝试（需要 C 编译器，失败也不影响核心功能）
pip install tree-sitter tree-sitter-python
```

### 1.5 配置 API Key

```cmd
:: 方式一：环境变量（推荐，仅当前终端有效）
set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx

:: 方式二：永久设置（以管理员身份运行 CMD）
setx DEEPSEEK_API_KEY "sk-xxxxxxxxxxxxxxxx"
```

> **Key 获取地址**：https://platform.deepseek.com/api_keys  
> 其他 Provider 的 Key 获取地址见 `USAGE.md` 第 2 节。

### 1.6 环境验证

```cmd
:: 确认 key 已设置
echo %DEEPSEEK_API_KEY%

:: 确认 agent 命令可用
agent --help
```

> **以上 6 步完成后即可进入验证流程。以下所有操作默认已在项目根目录且虚拟环境已激活。**

---

## 2. 快速验证（5 分钟）

> 如果你只有 5 分钟，跑完这 4 条命令就能覆盖简历 80% 的技术点。

### 2.1 全量测试：验证 MockBackend 架构（约 20 秒）

```cmd
pytest -v --tb=line 2>&1 | findstr /C:"passed" /C:"failed" /C:"skipped"
```

**期望看到**：`376 passed, 7 skipped in ~20s`

**验证了什么**：
- ✅ 简历第 3 段「376 个测试在约 20 秒内覆盖 Agent 全部 6 种终止路径、2 种 Reflection 触发条件」
- ✅ `MockBackend` 以预设 Action 脚本替代真实 LLM，零网络零延迟

### 2.2 冒烟测试：观察 ReAct 完整循环（约 15 秒）

```cmd
python smoke_test.py
```

**观察输出中的关键标记**：

```
[Step 1] Action: tool_call     ← LLM 决定调用工具（Act）
  Thought: ...                 ← LLM 的推理过程（Think）
  ✓ [file_write] success       ← 工具执行结果（Observe）

[Step 2] Action: tool_call
  ✓ [shell] success

[Step 3] Action: finish        ← Agent 宣布完成
✓ COMPLETE                      ← 主循环正常终止
```

**验证了什么**：
- ✅ 简历第 1 段「ReAct 范式构建自主编程 Agent 主循环」（观察 Thought → Action → Observation 三步）
- ✅ 简历第 2 段「12 个工具统一为 `execute(params) → ToolResult` 接口」（输出中列出了全部 12 个工具名）
- ✅ 6 种终止路径之一：FINISH

### 2.3 查看 repo-map 生成（2 秒）

```cmd
python -c "from context.repo_map import RepoMap; rm = RepoMap('.'); print(rm.build(budget=2000)[:1500])"
```

**期望看到**：类似以下的结构化符号摘要

```
agent/core.py (7 symbols):
  class AgentConfig (line 50)
  class Agent (line 72)
  function run (line 95)
    method _build_messages (line 275)
...
```

**验证了什么**：
- ✅ 简历第 3 段「tree-sitter 符号提取方案，生成结构化仓库地图」
- ✅ 精确区分了 `class`（顶层）vs `method`（缩进 > 0，类内部方法）

### 2.4 验证 Token 预算分配（1 秒）

```cmd
python -c "from context.token_budget import TokenBudget; b = TokenBudget(80000); p = b.default_plan(); print(f'System:{p.system_core} RepoMap:{p.repo_map} History:{p.history} Observation:{p.observation} Reserve:{p.reserve}')"
```

**期望输出**类似：`System:6800 RepoMap:10200 History:34000 Observation:17000 Reserve:12000`

**验证了什么**：
- ✅ 简历第 3 段「基于优先级的 Token 预算分配器（System 10% → Repo-map 15% → History 50% → Observation 25%）」

---

## 3. 简历第 1 段验证：Agent 架构与自省机制

> 简历原文：*基于 SWE-agent 的 ReAct 范式构建自主编程 Agent 主循环…设计双重 Reflection 自省机制…三种自动熔断策略*

### 3.1 阅读主循环源码（3 分钟）

```cmd
:: 验证主循环位置和结构
findstr /N "def run" agent\core.py
:: 应输出：95:    def run(self, task: Task, log: EventLog) -> RunResult:

findstr /N "_build_messages\|_call_with_retry\|execute_tool\|_is_looping" agent\core.py
```

打开 `agent\core.py`，阅读第 95-269 行的 `run()` 方法。你应该能识别这个结构：

```
for step in range(1, max_steps+1):
    _build_messages()        → 组装 LLM 输入（含 repo-map）
    _call_with_retry()       → 调 LLM（带指数退避重试）
    _is_looping()            → 死循环检测
    检查 FINISH / GIVE_UP    → 终端判断
    execute_tool()           → 执行工具
    写入 history             → Observation 回写
    检查 Reflection          → 自省触发
```

**验证了什么**：ReAct 循环的 6 个阶段全部可定位到具体代码行。

### 3.2 通过测试验证 Reflection 触发（2 秒）

```cmd
pytest tests/test_day2.py -v -k "reflection" --tb=line
```

**期望**：`TestReflectionTestFailed` 和 `TestReflectionNoEdit` 类下所有测试 PASSED。

**查看 Reflection 源码**：

```cmd
findstr /N "reflection_test_failed\|reflection_no_edit\|_is_looping" agent\core.py agent\prompt.py
```

**验证了什么**：
- ✅ 双重 Reflection：测试失败触发（`agent/core.py:228`）+ 连续 N 步无编辑触发（`agent/core.py:241`）
- ✅ Reflection prompt 不是"重试"，而是引导元认知（`agent/prompt.py:96-123`）

### 3.3 通过测试验证死循环检测（2 秒）

```cmd
pytest tests/test_day2.py -v -k "loop" --tb=line
```

**期望**：`TestLoopDetection` 下所有测试 PASSED。

**验证了什么**：
- ✅ 简历第 1 段「连续相同 Action 死循环检测（滑动窗口比对）」
- ✅ 比较的是 `(tool_name, params)` 元组，而非字符串

### 3.4 验证 6 种终止路径（2 秒）

```cmd
pytest tests/test_day2.py -v -k "finish or give_up or max_steps or loop or llm_error or unknown" --tb=line
```

**期望**：约 15-20 个测试全部 PASSED，覆盖：

| 路径 | 对应测试类 | 验证的终止条件 |
|------|----------|--------------|
| `SUCCESS` | `TestAgentFinish` | Agent 正常完成任务 |
| `GAVE_UP` | `TestAgentGiveUp` | Agent 主动放弃 |
| `MAX_STEPS` | `TestAgentMaxSteps` | 超出步数上限熔断 |
| `GAVE_UP` | `TestLoopDetection` | 死循环检测熔断 |
| `FAILED` | `TestLLMError` | LLM 调用异常 |
| `SUCCESS` | `TestUnknownTool` | 工具不存在时优雅降级 |

**验证了什么**：简历「覆盖 Agent 全部 6 种终止路径」。

---

## 4. 简历第 2 段验证：工具链抽象与多模型适配

> 简历原文：*基于策略模式设计 BaseTool 抽象基类与 ToolRegistry 注册中心…12 个工具…多 LLM 后端统一层*

### 4.1 理解 BaseTool 接口（2 分钟）

```cmd
:: 查看抽象基类定义
findstr /N "class BaseTool\|def name\|def description\|def parameters_schema\|def execute\|def to_llm_schema" tools\base.py
```

打开 `tools\base.py` 第 53-101 行，确认 BaseTool 的四个抽象成员 + 一个模板方法：

- `name` / `description` / `parameters_schema` → 为 LLM 生成 function calling JSON Schema
- `execute(params) → ToolResult` → 实际执行逻辑
- `to_llm_schema()` → 模板方法，汇总前三者

**验证了什么**：策略模式——所有工具共享同一接口，Agent 只依赖抽象。

### 4.2 理解 ToolRegistry 开放-封闭原则（2 分钟）

```cmd
:: 查看注册表实现
findstr /N "class ToolRegistry\|def register\|def execute_tool\|def get_schemas" tools\base.py
```

```cmd
:: 验证工具不存在时优雅降级
pytest tests/test_day2.py -v -k "unknown_tool" --tb=line
```

**验证了什么**：
- ✅ `register()` 支持链式调用，新增工具只需注册一行
- ✅ `execute_tool()` 两层保护：不存在返回 error（不抛异常）；内部异常被 try/except 捕获

### 4.3 查验 12 个工具（2 分钟）

```cmd
:: 列出所有工具文件
dir tools\*.py /b

:: 列出工具注册位置
findstr /C:".register(" entry\cli.py
```

**期望**：输出 12 行 `.register(...)`，对应 12 个工具：

| # | 工具名 | 文件 | 认知阶段 |
|---|--------|------|---------|
| 1 | `shell` | `tools/shell_tool.py` | 探索 |
| 2 | `file_read` | `tools/file_tool.py` | 精读 |
| 3 | `file_view` | `tools/file_tool.py` | 精读 |
| 4 | `file_write` | `tools/file_tool.py` | 编辑 |
| 5 | `search_text` | `tools/search_tool.py` | 探索 |
| 6 | `find_files` | `tools/search_tool.py` | 探索 |
| 7 | `find_symbol` | `tools/search_tool.py` | 探索 |
| 8 | `test` | `tools/test_tool.py` | 验证 |
| 9 | `git_status` | `tools/git_tool.py` | 版本控制 |
| 10 | `git_diff` | `tools/git_tool.py` | 版本控制 |
| 11 | `git_add` | `tools/git_tool.py` | 版本控制 |
| 12 | `git_commit` | `tools/git_tool.py` | 版本控制 |

**验证了什么**：简历「12 个工具（Shell 执行、文件分页读写、正则/AST 符号搜索、Pytest 结构化解析、Git 四步操作）」。

### 4.4 验证多 LLM 后端适配层（2 分钟）

```cmd
:: 查看 Router 支持的 provider 列表
findstr /N "_PROVIDER_BASE_URLS\|_ENV_KEY_MAP" llm\router.py
```

**期望**：5 个 provider（anthropic、openai、deepseek、groq、ollama）。

```cmd
:: 验证 Router 测试
pytest tests/test_day4.py -v -k "router" --tb=line
```

**验证了什么**：简历「一行配置切换五种后端」。

### 4.5 验证 R1 文本解析 fallback（2 分钟）

```cmd
:: 查看不支持 function calling 的模型列表
findstr /N "_NO_FUNCTION_CALLING\|supports_function_calling\|_complete_text_only\|_parse_text_response" llm\openai_compat.py
```

```cmd
:: 运行文本 fallback 测试
pytest tests/test_day4.py -v -k "text or fallback" --tb=line
```

**期望**：`TestTextFallback` 下所有测试 PASSED。

**验证了什么**：简历「针对 DeepSeek R1 等不支持 function calling 的模型设计工具描述注入 System Prompt → JSON 文本正则解析的降级方案」。

### 4.6 验证 shell 工具的四层安全（30 秒）

```cmd
pytest tests/test_day3.py tests/test_confirm.py -v -k "block or readonly or confirm" --tb=line 2>&1 | findstr /C:"PASSED" /C:"FAILED"
```

**验证了什么**：三层安全（黑名单硬拦截 → 只读白名单 → 确认回调）正常工作。

---

## 5. 简历第 3 段验证：上下文管理与可测试性工程

> 简历原文：*基于优先级的 Token 预算分配器…双层上下文裁剪…tree-sitter 仓库地图…MockBackend 测试架构*

### 5.1 理解 TokenBudget 优先级分配（2 分钟）

```cmd
:: 运行并阅读输出
python -c "from context.token_budget import TokenBudget; b = TokenBudget(80000); p = b.default_plan(); print('=== Token 预算分配 ==='); print(f'总预算:    {p.total:>8,} tokens'); print(f'安全预留:  {p.reserve:>8,} ({p.reserve/p.total*100:.0f}%%)'); print(f'System:    {p.system_core:>8,} ({p.system_core/(p.total-p.reserve)*100:.0f}%%)'); print(f'RepoMap:   {p.repo_map:>8,} ({p.repo_map/(p.total-p.reserve)*100:.0f}%%)'); print(f'History:   {p.history:>8,} ({p.history/(p.total-p.reserve)*100:.0f}%%)'); print(f'Observation:{p.observation:>8,} ({p.observation/(p.total-p.reserve)*100:.0f}%%)')"
```

**期望输出**：

```
=== Token 预算分配 ===
总预算:      80,000 tokens
安全预留:    12,000 (15%)
System:       6,800 (10%)
RepoMap:     10,200 (15%)
History:     34,000 (50%)
Observation: 17,000 (25%)
```

**验证了什么**：简历「Token 预算分配器（System 10% → Repo-map 15% → History 50% → Observation 25%）」。

### 5.2 验证双层裁剪（2 秒）

```cmd
pytest tests/test_day5.py -v -k "token or trim or history" --tb=line
```

**验证了什么**：
- ✅ ConversationHistory 滑动窗口：保留首条 + 超出 `max_messages` 时从旧丢弃
- ✅ TokenBudget 二次裁剪：从旧到新贪婪选择，插入截断提示

### 5.3 验证 tree-sitter repo-map（2 秒）

```cmd
pytest tests/test_day5.py -v -k "repo_map or symbol" --tb=line
```

```cmd
:: 查看多语言注册表
findstr /N "_LANG_REGISTRY\|_FUNC_NODES\|_CLASS_NODES" context\repo_map.py
```

**期望**：`_LANG_REGISTRY` 包含 9 种语言扩展名（`.py`/`.js`/`.ts`/`.tsx`/`.go`/`.rs`/`.java`/`.cpp`/`.c`/`.rb`）。

**验证了什么**：简历「覆盖 Python/JS/TS/Go/Rust/Java/C/C++/Ruby 共 8 种语言的结构化仓库地图」。

### 5.4 验证 tiktoken fallback（2 秒）

```cmd
python -c "from context.token_budget import estimate_tokens, is_tiktoken_available; print('tiktoken available:', is_tiktoken_available()); print('estimate_tokens(\"hello world\"):', estimate_tokens('hello world')); print('estimate_tokens(100 chars):', estimate_tokens('x'*100))"
```

**如果没装 tiktoken**：输出 `tiktoken available: False`，但仍输出合理的估算值——这就是 fallback 机制在运作。

**验证了什么**：简历「tiktoken fallback：优先精确计数，不可用时字符数 // 4 估算」。

### 5.5 深入理解 MockBackend（5 分钟）

这是简历中最核心的技术亮点，值得仔细验证。

```cmd
:: 1. 查看 MockBackend 源码（只有 70 行，值得通读）
findstr /N "class MockBackend\|def complete\|self._script\|self._index\|call_count\|received_messages" llm\base.py
```

打开 `llm\base.py` 第 133-201 行，理解这个核心结构：

```
MockBackend._script    = [Action1, Action2, ...]   ← 预设剧本
MockBackend._index     = 0                          ← 播放指针
MockBackend.complete() → 返回 _script[_index]      ← 按剧本返回
MockBackend.received_messages                       ← 记录收到的所有 LLM 请求
```

```cmd
:: 2. 观察一个使用 MockBackend 的测试用例
findstr /N "MockBackend\|mock_backend\|make_finish_action\|make_tool_call_action" tests\test_day2.py
```

```cmd
:: 3. 统计 MockBackend 在所有测试中的使用频率
findstr /C:"MockBackend" tests\*.py
```

**你会看到**：MockBackend 被 6 个测试文件使用，覆盖了 Agent 核心逻辑、ChatSession、CLI、流式、集成等所有需要 LLM 的场景。

```cmd
:: 4. 验证 MockBackend 本身的行为正确
pytest tests/test_day2.py -v -k "MockBackend" --tb=line
```

**验证了什么**：
- ✅ 简历「基于依赖倒置原则设计 MockBackend——以预设 Action 脚本队列替代真实 LLM API 调用」
- ✅ 简历「将非确定性的 LLM 行为转化为完全确定的输入/输出序列」
- ✅ 简历「配合 NoopTool、FailingTool 实现 376 个测试在约 20 秒内完成」

### 5.6 理解为什么测试这么快（理解即可，无需操作）

| 因素 | 真实 LLM | MockBackend |
|------|---------|-------------|
| 单次调用延迟 | 500ms – 10s+ | ~0ms（纯内存） |
| 376 测试 × 3 步的 LLM 时间 | ~30-60 分钟 | ~0 秒 |
| 网络依赖 | 需要 | 不需要 |
| API 费用 | 每次调用计费 | $0 |
| 确定性 | 非确定性 | 100% 确定 |

测试中剩下的 ~20 秒全部是真实的文件 I/O、subprocess 和 git 操作——MockBackend 把唯一慢且非确定的部分替换掉了。

---

## 6. 简历逐句对照验证清单

> 按简历原文顺序，每条标注验证命令和预期结果。

### 第 1 段：Agent 架构与自省机制设计

| # | 简历声明 | 验证命令 | 预期结果 | 用时 |
|---|---------|---------|---------|:--:|
| 1 | 基于 SWE-agent 的 ReAct 范式构建主循环 | `python smoke_test.py` | 观察 [Step 1] Thought → Action → Observation | 15s |
| 2 | 观察注入、LLM 推理、Tool Calling、结果回写拆分 | `findstr /N "_build_messages\|execute_tool\|_format.*history" agent\core.py` | 定位到每一阶段的代码行 | 30s |
| 3 | 双重 Reflection：测试失败触发 | `pytest tests/test_day2.py -k "reflection and test_failed" -v` | PASSED | 3s |
| 4 | 双重 Reflection：连续无编辑触发 | `pytest tests/test_day2.py -k "reflection and no_edit" -v` | PASSED | 3s |
| 5 | 死循环检测（滑动窗口比对） | `pytest tests/test_day2.py -k "loop" -v` | PASSED | 3s |

### 第 2 段：工具链抽象与多模型适配

| # | 简历声明 | 验证命令 | 预期结果 | 用时 |
|---|---------|---------|---------|:--:|
| 6 | BaseTool 抽象基类 + 策略模式 | `findstr /N "class BaseTool" tools\base.py` | 定位到第 53 行 | 10s |
| 7 | ToolRegistry 注册中心 | `findstr /N "class ToolRegistry" tools\base.py` | 定位到第 108 行 | 10s |
| 8 | 12 个工具统一接口 | `findstr /C:".register(" entry\cli.py` | 12 行 .register() | 10s |
| 9 | 新增工具无需修改主循环 | `pytest tests/test_day2.py -k "unknown_tool" -v` | Agent 不崩溃，优雅降级 | 3s |
| 10 | 多 LLM 后端适配器模式 | `findstr /N "_PROVIDER_BASE_URLS" llm\router.py` | 5 个 provider | 10s |
| 11 | 一行配置切换五种后端 | 查看 `config\default.yaml` | provider: deepseek → 改一个词即可切换 | 10s |
| 12 | R1 文本解析降级方案 | `pytest tests/test_day4.py -k "text" -v` | TestTextFallback 全 PASSED | 3s |

### 第 3 段：上下文管理与可测试性工程

| # | 简历声明 | 验证命令 | 预期结果 | 用时 |
|---|---------|---------|---------|:--:|
| 13 | Token 预算 10/15/50/25 | 见 2.4 节命令 | 输出正确的百分比分配 | 2s |
| 14 | 双层上下文裁剪 | `pytest tests/test_day5.py -k "trim or history" -v` | PASSED | 3s |
| 15 | tree-sitter 8 语言 repo-map | `findstr /N "_LANG_REGISTRY" context\repo_map.py` | 9+ 种扩展名映射 | 10s |
| 16 | 符号按重要性排序 | `python -c "..." ` (见 2.3 节) | class/function/method 分层输出 | 2s |
| 17 | MockBackend 替代真实 LLM | `findstr /N "class MockBackend" llm\base.py` | 定位到第 133 行 | 10s |
| 18 | 376 个测试约 20 秒 | `pytest -v --tb=line 2>&1 | findstr "passed"` | `376 passed` + 时间 ~20s | 20s |
| 19 | 覆盖 6 种终止路径 | `pytest tests/test_day2.py -k "finish or give_up or max_steps or loop or llm_error or unknown"` | 全部 PASSED | 3s |
| 20 | 覆盖 2 种 Reflection | `pytest tests/test_day2.py -k "reflection"` | 全部 PASSED | 3s |
| 21 | 多轮对话历史跨轮传递 | `pytest tests/test_chat.py -k "history or round" -v` | PASSED | 3s |

---

## 7. 常见问题速查

| 症状 | 解决 |
|------|------|
| `agent: command not found` | `pip install -e .` 或检查 `.venv\Scripts` 是否在 PATH |
| `DEEPSEEK_API_KEY not set` | `set DEEPSEEK_API_KEY=sk-xxx` |
| smoke_test 卡住不动 | 网络问题或 API Key 无效。先跑 `pytest` 确认测试套件正常，再排查网络 |
| pytest 报 "no module named 'agent'" | 确保在 `forge-agent` 根目录下执行，且已 `pip install -e .` |
| `UnicodeDecodeError: 'gbk'` | **已修复**。如仍出现，确认 `smoke_test.py` 第 75 行有 `encoding="utf-8"` |
| `Unknown tool 'finish'` | **已修复**。如仍出现，确认 `agent\core.py` 第 208 行有 `if tc.name == "finish":` |
| CMD 中文乱码 | `chcp 65001` 切换到 UTF-8 代码页 |
| `pip install` 报 C++ 编译错误 | 见 1.4 节的分步安装方案 |
| Docker 相关测试 skip | 正常——`test_sandbox.py` 在无 Docker 环境自动 skip |

---

> **完成以上所有验证后**，你就有了充分的证据支持简历中的每一项技术声明。面试时可以自信地说："我不仅实现了这些功能，而且每一项都有对应的测试和验证命令可以当场演示。"
