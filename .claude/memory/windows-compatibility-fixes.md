---
name: windows-compatibility-fixes
description: Forge Agent 在 Windows 上运行时遇到的编码和路径问题及修复方案
metadata:
  type: project
---

# Forge Agent — Windows 兼容性问题与修复

## 项目跨平台设计意图

项目通过 `--sandbox` 参数提供 Docker 沙箱模式，在 Windows/Mac 上通过 Linux 容器统一执行环境。不加 sandbox 时用 `LocalRuntime` 直接在宿主机执行。跨平台策略是"Docker 做抽象层"而非代码中大量 `if sys.platform` 分支。

## 已发现并修复的问题

### 1. 文件编码：`open()` 未指定 UTF-8

**文件**：`smoke_test.py:75`

**现象**：`UnicodeDecodeError: 'gbk' codec can't decode byte 0x96`

**原因**：Windows 上 Python 的 `open()` 默认编码跟随系统 locale（中文 Windows 为 GBK），而项目 YAML 文件为 UTF-8。

**修复**：`with open(config_path, encoding="utf-8") as f:`

**影响范围**：任何在 Windows 上直接 `open()` 读取项目文本文件的地方。核心代码（`config/schema.py:114`）已经正确写了 `encoding="utf-8"`，只有 `smoke_test.py` 遗漏了。

---

### 2. subprocess 编码：`text=True` 在 Windows 上走 GBK

**文件**：`tools/runtime.py`（3 处）、`agent/core.py`（1 处）、`entry/github_issue.py`（1 处）

**现象**：`subprocess.run` 的 `_readerthread` 线程报 `UnicodeDecodeError: 'gbk' codec can't decode`，虽然主流程不崩溃但后台异常日志异常丑陋。

**原因**：`text=True` 在 Python 3.x 上的行为是随平台 locale 选编码。Windows 上选 GBK，遇到子进程输出的 UTF-8 字符就崩。

**修复**：全部替换为 `encoding="utf-8", errors="replace"`。

```python
# 之前
subprocess.run(cmd, shell=True, capture_output=True, text=True, ...)

# 之后
subprocess.run(cmd, shell=True, capture_output=True, encoding="utf-8", errors="replace", ...)
```

**注意**：这个写法在所有平台（Linux/macOS/Windows）和所有 Python 3.x 版本上行为完全一致。

---

### 3. smoke_test 任务描述硬编码 Linux 路径

**文件**：`smoke_test.py:203-209`

**现象**：Agent 尝试执行 `mkdir -p /tmp` 和 `python /tmp/hello_agent.py`，在 Windows 上失败。

**修复**：改为跨平台任务——在**当前目录**创建文件并运行。

```python
# 之前
TASK_DESCRIPTION = """\
Create a file at /tmp/hello_agent.py ...
Then run it with `python /tmp/hello_agent.py` ...
"""

# 之后
TASK_DESCRIPTION = """\
Create a file named hello_agent.py in the current directory ...
Then run it with `python hello_agent.py` ...
"""
```

---

### 4. DeepSeek 模型将 `finish` 误解为工具调用（与 OS 无关）

**文件**：`agent/core.py:201-222`

**现象**：`Unknown tool 'finish'. Available tools: shell, file_read, ...`

**原因**：部分 LLM（DeepSeek 等）将 prompt 中的 "call finish" 字面理解为 function calling 目标，返回 `tool_name="finish"`。Agent 期望的是 `finish_reason="stop"` → `ActionType.FINISH`。

**修复**：在工具执行前加入拦截，将 `finish`/`give_up` tool call 直接映射为终端 Action。

---

## 已知但未修复（不影响功能）

| 问题 | 位置 | 影响 | 是否需修 |
|------|------|------|:--:|
| `test_timeout` 测试用 `sleep` 命令 | `tests/test_day3.py:201` | 该测试在 Windows 上失败 | 可标记 `skipIf(sys.platform=='win32')` |
| Docker 沙箱测试用 `sleep` | `tests/test_sandbox.py:82` | 同上 | 已有 `skipIf(not DOCKER_AVAILABLE)` |
| 部分测试路径用 `/tmp` 但不实际执行 | `tests/test_day1.py:34,86` | 只是字符串参数，不影响 | 否 |

## 设计启示

1. **永远显式指定 `encoding="utf-8"`** — 即使在 Linux 上 UTF-8 是默认值，这也能避免在 CI/Windows 上出问题
2. **`subprocess.run` 用 `encoding="utf-8", errors="replace"` 替代 `text=True`** — 行为在所有平台上一致
3. **Docker 是比 `if sys.platform` 更干净的跨平台方案** — 本项目证明了这一点：只需少量修复，核心架构完全不需要改动
