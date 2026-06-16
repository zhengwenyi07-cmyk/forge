"""
smoke_test.py

端到端冒烟测试，放在项目根目录运行：
    python smoke_test.py

测试内容：
1. 用真实 LLM（从 config/default.yaml 读配置）
2. 给 agent 一个简单任务：创建并运行一个 hello_agent.py 文件
3. 打印每一步的 action 和 observation
4. 最终打印 RunResult 和统计摘要

不依赖 SWE-bench，不需要真实 repo，适合快速验证：
- ✅ LLM API 联通（Key 有效、网络可达、模型响应正常）
- ✅ 12 个工具注册正确（从 build_registry() 输出可以看到全部工具列表）
- ✅ ReAct 主循环完整运行（观察 Thought → Action → Observation 循环）
- ✅ EventLog 写入和回放
- ✅ Agent 能自主完成编程任务（创建文件 → 运行验证 → 宣布完成）

注意：此脚本需要真实的 LLM API Key（不像 tests/ 目录下的测试文件那样使用 MockBackend）。
"""

from __future__ import annotations

# logging：输出 Agent 执行过程的 INFO 级别日志（LLM 调用、工具执行）
import logging
# sys：sys.path 路径注入 + sys.exit 退出码
import sys
# time：主流程计时 elapsed = time.time() - t0
import time
# Path：项目根目录路径 + 当前工作目录路径
from pathlib import Path

# yaml：解析 config/default.yaml 配置
import yaml

# 把项目根目录加入 sys.path（脚本放根目录时已经在，保险起见）
# 防止直接 python smoke_test.py 时找不到 agent/ 模块
sys.path.insert(0, str(Path(__file__).parent))

# Agent 核心：
#   Agent + AgentConfig → 主循环运行器
#   EventLog + summarize_run → 事件日志和统计
#   Task + EventType → 任务描述和事件类型
from agent.core import Agent, AgentConfig
from agent.event_log import EventLog, summarize_run
from agent.task import EventType, Task
# create_backend_from_config → 根据配置创建 LLM 后端
from llm.router import create_backend_from_config
# ToolRegistry → 工具注册表，注册 12 个工具
from tools.base import ToolRegistry
# 12 个工具：探索层（shell, search_text, find_files, find_symbol）
#              精读层（file_read, file_view）
#              编辑层（file_write）
#              验证层（test / PytestTool）
#              版本控制（git_status, git_diff, git_add, git_commit）
from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool
from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
from tools.shell_tool import ShellTool
from tools.test_tool import PytestTool

# ===========================================================================
# 日志：把 agent 每步都打印出来
# 使用 logging.INFO 级别，显示 LLM 调用、工具执行、Reflection 触发等关键事件
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("smoke_test")


# ===========================================================================
# 彩色打印辅助（终端支持时有颜色，不支持时降级）
# ANSI 转义序列：32=绿、33=黄、31=红、36=青、1=粗体
# 非 TTY（管道/文件重定向）自动降级为纯文本
# ===========================================================================

def _c(text: str, code: str) -> str:
    """包裹 ANSI 转义码，非 TTY 时降级为纯文本。"""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def green(t):  return _c(t, "32")    # 成功状态
def yellow(t): return _c(t, "33")   # Reflection 警告
def red(t):    return _c(t, "31")    # 错误/FAILED
def cyan(t):   return _c(t, "36")    # Action/Step 标识
def bold(t):   return _c(t, "1")     # 标题/关键信息


# ===========================================================================
# 读配置
# 从 config/default.yaml 读取 LLM 和 Agent 配置。
# 与 config/schema.py 的 load_config() 功能相似但不使用 dataclass 包装——
# 直接读取原始 dict，减少依赖。
# ===========================================================================

def load_config(path: str = "config/default.yaml") -> dict:
    """
    读取并解析配置文件，返回原始 dict。

    Args:
        path: YAML 配置文件的路径。默认使用 config/default.yaml

    Returns:
        解析后的配置 dict（直接由 yaml.safe_load 返回）

    Raises:
        SystemExit: 配置文件不存在时退出
    """
    config_path = Path(path)
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    # 显式指定 encoding="utf-8"（Windows 默认 GBK，不指定会出 UnicodeDecodeError）
    with open(config_path, encoding="utf-8") as f:
        raw = f.read()

    # 展开环境变量占位符 ${VAR}
    import os, re
    def replace_env(m):
        var = m.group(1)
        val = os.environ.get(var, "")
        if not val:
            logger.warning("Environment variable %s is not set", var)
        return val
    raw = re.sub(r"\$\{(\w+)\}", replace_env, raw)

    return yaml.safe_load(raw)


# ===========================================================================
# 组装工具注册表
# 注册 Agent 可调用的全部 12 个工具（与 entry/cli.py 中的 _build_registry 相同）。
# 不同点：此版本不传递 confirm_callback 和 runtime 参数，所有工具用默认设置。
# ===========================================================================

def build_registry() -> ToolRegistry:
    """
    构建并注册所有 12 个工具。

    返回的 ToolRegistry 会立刻在 UI 上打印工具列表：
        Tools    : shell, file_read, file_view, file_write, search_text,
                   find_files, find_symbol, test, git_status, git_diff,
                   git_add, git_commit
    """
    return (
        ToolRegistry()
        .register(ShellTool())
        .register(FileReadTool())
        .register(FileViewTool())
        .register(FileWriteTool())
        .register(SearchTextTool())
        .register(FindFilesTool())
        .register(FindSymbolTool())
        .register(PytestTool())
        .register(GitStatusTool())
        .register(GitDiffTool())
        .register(GitAddTool())
        .register(GitCommitTool())
    )


# ===========================================================================
# 实时打印 event log
# agent.run() 执行完毕后，通过 EventLog.replay() 回放所有事件并打印到终端。
# 与 entry/cli.py 的 run 子命令中的 _print_step 功能类似，但使用 print() 而非 click.echo()。
# ===========================================================================

def print_events_realtime(log: EventLog, last_count: int) -> int:
    """
    读取 event log，打印新增的 event。
    返回当前 event 总数（供下次调用时做增量打印）。

    Args:
        log:        已完成的 EventLog 实例
        last_count: 上次已打印的 event 数（0=全部打印）

    Returns:
        当前 event 总数（可传回给下一次调用做增量打印）

    注：当前实现是 Agent.run() 跑完后一次性打印所有 events（last_count=0）。
    如果后续需要真正的实时打印（一边跑一边打印），可以改成多线程调用此函数。
    """
    events = log.replay()
    for event in events[last_count:]:
        _print_event(event)
    return len(events)


def _print_event(event) -> None:
    """
    打印单条 event 到终端（包含彩色格式化）。
    根据 event_type 选择不同的打印格式。
    """
    etype = event.event_type
    payload = event.payload

    if etype == EventType.TASK_START:
        task = payload["task"]
        print(bold(f"\n{'='*60}"))
        print(bold(f"  TASK: {task['description']}"))
        print(bold(f"  REPO: {task['repo_path']}"))
        print(bold(f"{'='*60}\n"))

    elif etype == EventType.ACTION:
        step = payload["step"]
        action = payload["action"]
        thought = action.get("thought", "")
        atype = action.get("action_type", "")
        tc = action.get("tool_call")

        print(cyan(f"[Step {step}] Action: {atype}"))
        if thought:
            # 只打前 200 字，思考内容可能很长
            short_thought = thought[:200] + ("..." if len(thought) > 200 else "")
            print(f"  Thought: {short_thought}")
        if tc:
            print(f"  Tool:    {tc['name']}")
            params_str = str(tc['params'])
            if len(params_str) > 120:
                params_str = params_str[:120] + "..."
            print(f"  Params:  {params_str}")

    elif etype == EventType.OBSERVATION:
        obs = payload["observation"]
        status = obs.get("status", "")
        tool = obs.get("tool_name", "")
        output = obs.get("output", "")
        error = obs.get("error")

        if status == "success":
            print(green(f"  ✓ [{tool}] {status}"))
        else:
            print(red(f"  ✗ [{tool}] {status}"))
            if error:
                print(red(f"    Error: {error}"))

        # 打印输出前 10 行
        lines = output.splitlines()[:10]
        for line in lines:
            print(f"    {line}")
        if len(output.splitlines()) > 10:
            print(f"    ... ({len(output.splitlines()) - 10} more lines)")
        print()

    elif etype == EventType.REFLECTION:
        reason = payload.get("reason", "")
        print(yellow(f"\n  ⟳ REFLECTION triggered: {reason}\n"))

    elif etype == EventType.TASK_COMPLETE:
        summary = payload.get("summary", "")
        print(green(bold(f"\n{'='*60}")))
        print(green(bold(f"  ✓ TASK COMPLETE")))
        print(green(f"  {summary}"))
        print(green(bold(f"{'='*60}\n")))

    elif etype == EventType.TASK_FAILED:
        reason = payload.get("reason", "")
        print(red(bold(f"\n{'='*60}")))
        print(red(bold(f"  ✗ TASK FAILED")))
        print(red(f"  {reason}"))
        print(red(bold(f"{'='*60}\n")))


# ===========================================================================
# 主流程
# 步骤：读配置 → 创建 LLM backend → 组装工具 → 创建 Task → Agent.run()
#       → 打印 events → 打印统计 → 返回退出码
# 对应简历 "ReAct 循环" — 任务创建文件 → Agent 自主执行 → 打印每个 Action/Observation
# ===========================================================================

# 任务描述（硬编码）。
# Agent 需要理解"创建文件 → 写入内容 → 运行验证 → 确认输出正确"这个流程。
# 原来使用 /tmp/hello_agent.py（Linux 路径），已改为当前目录（跨平台兼容）。
TASK_DESCRIPTION = """\
Create a file named hello_agent.py in the current directory with the following content:

    print("Hello from coding agent!")

Then run it with `python hello_agent.py` and confirm the output says "Hello from coding agent!".
"""


def main():
    print(bold("\n🤖 Coding Agent — Smoke Test\n"))

    # ── 步骤 1：读配置 ────────────────────────────────────────────────
    # 从 config/default.yaml 读取 LLM provider / model / api_key 等配置
    config = load_config()
    llm_config = config.get("llm", {})
    agent_config_dict = config.get("agent", {})

    provider = llm_config.get("provider", "?")
    model = llm_config.get("model", "?")
    print(f"Provider : {provider}")
    print(f"Model    : {model}")
    print(f"Log dir  : {agent_config_dict.get('log_dir', './logs')}\n")

    # ── 步骤 2：创建 LLM backend ──────────────────────────────────────
    # 根据配置创建对应的 LLMBackend 实例（Authropia / OpenAI / DeepSeek / Groq / Ollama）
    # 如果 Key 无效或网络不通，在此步骤就会失败
    try:
        backend = create_backend_from_config(llm_config)
        print(green("✓ LLM backend created\n"))
    except Exception as e:
        print(red(f"✗ Failed to create LLM backend: {e}"))
        sys.exit(1)

    # ── 步骤 3：组装工具注册表 ────────────────────────────────────────
    # 注册 12 个工具，打印工具名列表（验证所有工具正确注册）
    registry = build_registry()
    print(f"Tools    : {', '.join(registry.tool_names)}\n")

    # ── 步骤 4：创建 Task ─────────────────────────────────────────────
    # 任务描述：在当前目录创建 hello_agent.py → 运行 → 验证输出
    # repo_path：当前工作目录（不是 forge-agent 项目目录本身）
    task = Task(
        description=TASK_DESCRIPTION,
        repo_path=str(Path.cwd()),
        max_steps=agent_config_dict.get("max_steps", 15),
        budget_tokens=agent_config_dict.get("budget_tokens", 80000),
    )

    # ── 步骤 5：创建 Agent ────────────────────────────────────────────
    # AgentConfig 只设置了 max_steps（其余都用默认值）
    agent_config = AgentConfig(
        max_steps=task.max_steps,
    )
    agent = Agent(backend, registry, agent_config)

    # ── 步骤 6：运行 Agent ────────────────────────────────────────────
    # 同步执行 Agent.run()，跑完后打印所有事件。
    # 如果后续需要真正的实时打印，可以改为多线程：agent.run() 跑在一个线程中，
    # 另一个线程间隔调用 print_events_realtime() 做增量打印。
    log_dir = agent_config_dict.get("log_dir", "./logs")
    print(bold("Starting agent...\n"))
    t0 = time.time()

    event_count = 0
    with EventLog.create(task, log_dir=log_dir) as log:
        print(f"Log file : {log.path}\n")

        # 用线程让主流程跑 agent，同时实时打印 event
        # 简单起见这里用单线程：run() 是同步的，所以我们在跑完后打印所有 event
        # 如果想真正"实时"打印，Day 6 加 streaming 时再改
        result = agent.run(task, log)
        print_events_realtime(log, 0)

    elapsed = time.time() - t0

    # ── 步骤 7：打印运行统计 ──────────────────────────────────────────
    print(bold("─" * 60))
    print(f"Status   : {green(result.status.value) if result.is_success() else red(result.status.value)}")
    print(f"Steps    : {result.steps_taken}")
    print(f"Tokens   : {result.total_tokens:,}")
    print(f"Time     : {elapsed:.1f}s")
    if result.error:
        print(f"Error    : {red(result.error)}")
    print(bold("─" * 60))

    # ── 步骤 8：Event 统计 ────────────────────────────────────────────
    # 使用 summarize_run() 对 EventLog 进行统计分析并打印摘要
    with EventLog.open_existing(log.path) as log2:
        stats = summarize_run(log2)
    print(f"\nEvent summary:")
    print(f"  Actions     : {stats['actions']}")
    print(f"  Reflections : {stats['reflections']}")
    print(f"  Tool calls  : {stats['tool_calls']}")
    print(f"  Final status: {stats['final_status']}")

    return 0 if result.is_success() else 1


if __name__ == "__main__":
    sys.exit(main())