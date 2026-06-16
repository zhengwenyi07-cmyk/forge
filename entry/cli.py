"""
entry/cli.py

命令行入口。
这是 Agent 的三大入口之一（另两个是 entry/chat.py 和 entry/github_issue.py）。

用法：
    # 直接传任务描述
    python -m entry.cli run --repo /path/to/repo --task "Fix the failing test"

    # 从文件读任务描述
    python -m entry.cli run --repo . --task-file task.txt

    # 覆盖模型
    python -m entry.cli run --repo . --task "fix it" --model deepseek-chat

    # 查看 event log 统计
    python -m entry.cli log show logs/abc123_20240101_120000.jsonl

安装为命令行工具后（pyproject.toml 里配置了 scripts）：
    agent run --repo . --task "fix it"

三个子命令：
  agent run    — 一次性任务，Agent 跑完后退出
  agent chat  — 持续对话模式，跨轮保留历史
  agent log   — 查看 JSONL 事件日志（show / list）
"""

from __future__ import annotations

# logging：Agent 内部日志（LLM 调用、Reflection 触发、错误信息等）
# verbose 模式输出到 stderr
import logging
# sys：sys.path 路径注入 + sys.exit 退出码
import sys
# time：run 子命令计时 elapsed = time.time() - t0
import time
# Path：repo 路径解析 + task-file 读取 + pyproject.toml 路径检查
from pathlib import Path

# Click CLI 框架。提供 @click.group / @click.command / @click.option / @click.pass_context
import click

# 把项目根加入 path（直接跑脚本时需要，避免 "ModuleNotFoundError: No module named 'agent'"）
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# load_config：加载 YAML 配置文件为 AppConfig dataclass
# merge_cli_overrides：CLI 参数（--model / --provider / --max-steps）覆盖 config 值
from config.schema import load_config, merge_cli_overrides   # noqa: E402
# create_backend_from_config：根据配置字典创建 LLMBackend 实例
from llm.router import create_backend_from_config            # noqa: E402

# 模块级 import（供测试中的 Monkey-patch 使用，保持 import 路径一致）

from config.schema import load_config, merge_cli_overrides  # noqa: E402
from llm.router import create_backend_from_config           # noqa: E402


# ===========================================================================
# 辅助：彩色输出
# ANSI 转义序列：32=绿、33=黄、31=红、36=青、1=粗体、2=暗色、35=品红
# 非 TTY（管道/文件重定向）自动降级为纯文本，不输出转义码
# ===========================================================================

def _c(text: str, code: str) -> str:
    """包裹 ANSI 转义码，非 TTY 时降级为纯文本。"""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def green(t: str) -> str:  return _c(t, "32")    # 成功/COMPLETE 状态
def yellow(t: str) -> str: return _c(t, "33")   # Reflection 警告
def red(t: str) -> str:    return _c(t, "31")    # 错误/FAILED 状态
def cyan(t: str) -> str:   return _c(t, "36")    # Action/Step 标识
def bold(t: str) -> str:   return _c(t, "1")     # 标题/关键信息
def dim(t: str) -> str:    return _c(t, "2")     # 次要信息/工具输出预览
def magenta(t: str) -> str: return _c(t, "35")   # 用户输入提示符


# ===========================================================================
# 构建 agent 各组件
# 工厂函数：根据配置创建 ToolRegistry（12 个工具全部在此注册）。
# 调用方（CLI run / CLI chat）传入 confirm_callback 和 runtime 两个可选参数：
#   confirm_callback=None → 危险命令不确认直接执行（run 模式默认）
#   confirm_callback=terminal_confirm → 需要用户 y/n 确认（chat 模式 + --confirm）
#   runtime=None → 本地 subprocess 执行
#   runtime=DockerRuntime → 容器内执行（--sandbox）
# ===========================================================================

def _build_registry(cfg, confirm_callback=None, runtime=None):
    """
    根据配置组装工具注册表。

    注册的 12 个工具按职责分层（对应简历"12 个工具统一接口"）：
    探索层：   shell, search_text, find_files, find_symbol
    精读层：   file_read, file_view
    编辑层：   file_write
    验证层：   test（PytestTool）
    版本控制： git_status, git_diff, git_add, git_commit

    Args:
        runtime: 传 DockerRuntime 时所有工具走容器执行（沙箱模式）
                 None 时全部在宿主机本地执行
    """
    from tools.base import ToolRegistry
    from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool

    return (
        ToolRegistry()
        .register(ShellTool(confirm_callback=confirm_callback, runtime=runtime))
        .register(FileReadTool(runtime=runtime))
        .register(FileViewTool(runtime=runtime))
        .register(FileWriteTool(runtime=runtime))
        .register(SearchTextTool(runtime=runtime))
        .register(FindFilesTool(runtime=runtime))
        .register(FindSymbolTool(runtime=runtime))
        .register(PytestTool(runtime=runtime))
        .register(GitStatusTool(runtime=runtime))
        .register(GitDiffTool(runtime=runtime))
        .register(GitAddTool(runtime=runtime))
        .register(GitCommitTool(runtime=runtime))
    )


def _print_step(event) -> None:
    """
    实时打印单条 event。
    run 子命令的 Agent.run() 执行完毕后，通过 log.replay() 遍历所有事件，
    对每条 event 调用此函数打印到终端。

    与 chat 模式的 _print_event_live 的区别：
    - 此函数在 run() 执行完毕后统一打印（一次性输出全部 events）
    - chat 模式的 _print_event_live 通过 monkey-patch 在 run() 执行期间实时打印
    """
    from agent.task import EventType
    etype = event.event_type
    payload = event.payload

    if etype == EventType.TASK_START:
        task = payload["task"]
        click.echo(bold(f"\n{'─'*60}"))
        click.echo(bold(f"  Task : {task['description'][:80]}"))
        click.echo(bold(f"  Repo : {task['repo_path']}"))
        click.echo(bold(f"{'─'*60}\n"))

    elif etype == EventType.ACTION:
        step = payload["step"]
        action = payload["action"]
        thought = action.get("thought", "")[:160]
        atype = action.get("action_type", "")
        tc = action.get("tool_call")
        click.echo(cyan(f"[Step {step}] {atype}"))
        if thought:
            click.echo(dim(f"  ↳ {thought}"))
        if tc:
            params_str = str(tc["params"])[:100]
            click.echo(f"  Tool: {tc['name']}  params: {params_str}")

    elif etype == EventType.OBSERVATION:
        obs = payload["observation"]
        status = obs.get("status", "")
        tool = obs.get("tool_name", "")
        output = obs.get("output", "")
        if status == "success":
            click.echo(green(f"  ✓ [{tool}]"))
        else:
            click.echo(red(f"  ✗ [{tool}] {obs.get('error', '')}"))
        # 打印前 5 行输出
        for line in output.splitlines()[:5]:
            click.echo(dim(f"    {line}"))
        if len(output.splitlines()) > 5:
            click.echo(dim(f"    ... ({len(output.splitlines())-5} more lines)"))
        click.echo()

    elif etype == EventType.REFLECTION:
        click.echo(yellow(f"\n  ⟳ Reflection: {payload.get('reason', '')}\n"))

    elif etype == EventType.TASK_COMPLETE:
        click.echo(green(bold(f"\n✓ COMPLETE: {payload.get('summary', '')}\n")))

    elif etype == EventType.TASK_FAILED:
        click.echo(red(bold(f"\n✗ FAILED: {payload.get('reason', '')}\n")))


# ===========================================================================
# CLI 主命令组
# Click 三层结构：
#   agent (group)
#     ├── run     (command) — 一次性任务
#     ├── chat    (command) — 持续对话
#     └── log     (group)
#           ├── show (command) — 查看日志详情
#           └── list (command) — 列出日志文件
# ===========================================================================

@click.group()
@click.option(
    "--config", "-c",
    default=None,
    help="Path to config YAML file (default: config/default.yaml)",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Coding Agent — autonomous code editing and bug fixing."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# ===========================================================================
# run 子命令
# "agent run --repo . --task 'fix tests'" 的入口。
# 流程：读取配置 → 创建 backend → 组装 registry → 创建 Agent → Agent.run()
# → 打印 events → 输出 RunResult 统计
# ===========================================================================

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--task", "-t", default=None, help="Task description (natural language)")
@click.option("--task-file", "-f", default=None, help="Read task description from file")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--max-steps", default=None, type=int, help="Override max steps")
@click.option("--stream", "-s", is_flag=True, default=True, help="Enable streaming output (default: on)")
@click.option("--confirm", is_flag=True, default=False, help="Ask confirmation before running dangerous shell commands")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def run(
    ctx: click.Context,
    repo: str,
    task: str | None,
    task_file: str | None,
    model: str | None,
    provider: str | None,
    max_steps: int | None,
    stream: bool,
    confirm: bool,
    sandbox: bool,
    verbose: bool,
) -> None:
    """
    Run the coding agent on a repository.

    这是 Agent 的"一次性任务"模式。
    流程：加载配置 → 创建 LLMBackend → 组装 12 个工具 → 创建 Agent → Agent.run()
    → 打印 Events → 输出统计。

    --sandbox：Docker 沙箱模式，所有 shell 命令在容器内执行
    --confirm：危险命令需用户 y/n 确认
    --stream：流式输出（默认开启）
    """
    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # 加载配置
    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(
        config, provider=provider, model=model, max_steps=max_steps
    )

    # 解析任务描述
    if task_file:
        description = Path(task_file).read_text(encoding="utf-8").strip()
    elif task:
        description = task
    else:
        click.echo(red("Error: provide --task or --task-file"), err=True)
        sys.exit(1)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    # 打印运行信息
    click.echo(bold(f"\n🤖 Coding Agent"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(f"  Max steps: {config.agent.max_steps}\n")

    # 构建各组件
    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model":    config.llm.model,
            "api_key":  config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    confirm_cb = terminal_confirm if confirm else None
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
    registry = _build_registry(config, confirm_callback=confirm_cb, runtime=runtime)

    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog, summarize_run
    from agent.task import Task
    try:
        from context.token_budget import is_tiktoken_available
    except ImportError:
        is_tiktoken_available = lambda: False

    # 流式回调：最终回答正常亮色
    def _stream_cb(text: str) -> None:
        import sys
        sys.stdout.write(text)
        sys.stdout.flush()

    # 推理回调：思考过程 dim 暗色
    def _thought_cb(text: str) -> None:
        import sys
        sys.stdout.write(dim(text))
        sys.stdout.flush()

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        history_max_messages=config.context.history_window * 2,
        stream=stream,
        stream_callback=_stream_cb if stream else None,
        thought_callback=_thought_cb if stream else None,
        confirm_dangerous=confirm,
        confirm_callback=confirm_cb,
    )
    agent = Agent(backend, registry, agent_config)

    task_obj = Task(
        description=description,
        repo_path="/workspace" if sandbox else str(repo_path),
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    if verbose:
        click.echo(dim(
            f"  tiktoken: {'yes' if is_tiktoken_available() else 'no (char estimate)'}\n"
        ))

    # 运行
    t0 = time.time()
    try:
        with EventLog.create(task_obj, log_dir=config.agent.log_dir) as log:
            click.echo(dim(f"  Log: {log.path}\n"))
            result = agent.run(task_obj, log)
            # 打印所有 events
            for event in log.replay():
                _print_step(event)
    finally:
        if runtime:
            runtime.cleanup()
            click.echo(dim("  Sandbox cleaned up."))

    elapsed = time.time() - t0

    # 打印结果
    click.echo(bold("─" * 60))
    status_str = green("SUCCESS") if result.is_success() else red(result.status.value.upper())
    click.echo(f"Status  : {status_str}")
    click.echo(f"Steps   : {result.steps_taken}")
    click.echo(f"Tokens  : {result.total_tokens:,}")
    click.echo(f"Time    : {elapsed:.1f}s")
    if result.error:
        click.echo(red(f"Error   : {result.error}"))
    click.echo(bold("─" * 60) + "\n")

    sys.exit(0 if result.is_success() else 1)



# ===========================================================================
# chat 子命令 — 交互对话模式
# "agent chat --repo . " 的入口。
# 与 run 模式的核心区别：
# - 创建 ChatSession 而非直接创建 Agent（ChatSession 内部管理 Agent 实例）
# - ChatSession 跨轮复用 _shared_history
# - 流式输出默认开启（not configurable）
# - 危险命令确认默认开启（使用 terminal_confirm）
# - 提供 REPL 命令：/exit /stats /clear /help
# ===========================================================================

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--max-steps", default=None, type=int, help="Max steps per round")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def chat(
    ctx: click.Context,
    repo: str,
    model: str | None,
    provider: str | None,
    max_steps: int | None,
    sandbox: bool,
    verbose: bool,
) -> None:
    """
    Interactive chat mode — continuous conversation with the agent.

    体验接近 Claude Code：每轮输入 → Agent 自动探索/修改/验证 → 下一轮历史保留。
    支持内置命令：/exit /stats /clear /help
    """
    import logging
    from entry.chat import ChatSession

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(config, provider=provider, model=model, max_steps=max_steps)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    try:
        backend = create_backend_from_config({
            "provider":   config.llm.provider,
            "model":      config.llm.model,
            "api_key":    config.llm.api_key or None,
            "base_url":   config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    registry = _build_registry(config)
    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
        registry = _build_registry(config, confirm_callback=terminal_confirm, runtime=runtime)
    session = ChatSession(
        backend=backend,
        registry=registry,
        config=config,
        repo_path="/workspace" if sandbox else str(repo_path),
        log_dir=config.agent.log_dir,
        confirm_callback=terminal_confirm,   # chat 模式默认开启确认
    )

    # 欢迎信息
    click.echo(bold(f"\n🤖 Coding Agent — Chat Mode"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(dim(f"  Type your task. Commands: /exit /stats /clear /help\n"))

    # 启用行编辑：退格、方向键、Ctrl+A/E、历史记录（↑↓）
    try:
        import readline as _rl
        import sys as _sys
        # 检测后端：libedit（某些 Linux/macOS）还是 GNU readline
        _is_libedit = "libedit" in getattr(_rl, "__doc__", "") or (
            hasattr(_rl, "parse_and_bind") and _sys.platform == "darwin"
        )
        # 更可靠的检测：尝试 libedit 特有的绑定语法
        try:
            _rl.parse_and_bind("bind -e")   # libedit 启用 Emacs 模式
            _is_libedit = True
        except Exception:
            _is_libedit = False

        if _is_libedit:
            _rl.parse_and_bind("bind -e")           # Emacs 模式：Ctrl+A/E/K 等
            _rl.parse_and_bind("bind ^I rl_complete")  # Tab 补全
        else:
            _rl.parse_and_bind("set editing-mode emacs")  # GNU readline Emacs 模式
            _rl.parse_and_bind("tab: complete")

        _rl.set_history_length(500)   # 历史记录最多 500 条
    except ImportError:
        pass  # Windows 没有 readline，降级为普通 input

    # 主 REPL 循环
    try:
        while True:
            try:
                # 清理当前行（流式输出后 readline 不知道屏幕上有残留字符）
                # \r 回到行首，\033[2K 清除整行，然后显示提示符
                sys.stdout.write("\r\033[2K")
                sys.stdout.flush()
                user_input = input(magenta("you") + " > ").strip()
            except EOFError:
                click.echo()
                break
            except KeyboardInterrupt:
                click.echo()
                break

            if not user_input:
                continue

            # 内置命令
            if user_input.startswith("/"):
                cmd = user_input.lower()
                if cmd in ("/exit", "/quit", "/q"):
                    break
                elif cmd == "/stats":
                    session.print_stats()
                elif cmd == "/clear":
                    session._shared_history.clear_except_first()
                    click.echo(dim("  History cleared (kept initial context)."))
                elif cmd == "/help":
                    click.echo(dim(
                        "  Commands:\n"
                        "    /exit   — quit\n"
                        "    /stats  — show session statistics\n"
                        "    /clear  — clear conversation history\n"
                        "    /help   — show this help\n"
                        "  Anything else is sent to the agent."
                    ))
                else:
                    click.echo(dim(f"  Unknown command: {user_input}. Type /help for help."))
                continue

            # 运行一轮 agent
            click.echo(dim(f"\n  Agent working..."))
            try:
                session.run_round(user_input)
            except KeyboardInterrupt:
                click.echo(yellow("\n  Interrupted. Type /exit to quit or continue with a new task."))
            except Exception as e:
                click.echo(red(f"\n  Error: {e}"))
                if verbose:
                    import traceback
                    traceback.print_exc()

    finally:
        if runtime:
            runtime.cleanup()
            click.echo(dim("  Sandbox cleaned up."))

    session.print_stats()
    click.echo(dim("  Bye!\n"))


# ===========================================================================
# log 子命令组
# "agent log show LOG_FILE" / "agent log list" 的入口。
# 用于查看 Agent 运行后的 JSONL 事件日志和统计摘要。
# 不需要 API Key 或网络——直接在本地日志文件上操作。
# ===========================================================================

@cli.group()
def log() -> None:
    """Inspect event logs."""


@log.command("show")
@click.argument("log_file")
def log_show(log_file: str) -> None:
    """
    Show a summary of an event log file.

    读取 JSONL 日志文件：按时间顺序列出所有事件，末尾显示统计摘要。
    统计摘要由 EventLog.summarize_run() 生成。
    """
    from agent.event_log import EventLog, summarize_run

    path = Path(log_file)
    if not path.exists():
        click.echo(red(f"File not found: {path}"), err=True)
        sys.exit(1)

    with EventLog.open_existing(path) as elog:
        events = elog.replay()
        stats = summarize_run(elog)

    click.echo(bold(f"\nEvent Log: {path.name}"))
    click.echo(f"  Total events : {stats['total_events']}")
    click.echo(f"  Actions      : {stats['actions']}")
    click.echo(f"  Reflections  : {stats['reflections']}")
    click.echo(f"  Tool calls   : {stats['tool_calls']}")
    click.echo(f"  Final status : {stats['final_status']}\n")

    click.echo(bold("Events:"))
    for event in events:
        ts = event.timestamp[11:19]   # HH:MM:SS
        etype = event.event_type.value
        detail = ""
        if event.event_type.value == "action":
            tc = event.payload.get("action", {}).get("tool_call")
            detail = f"  tool={tc['name']}" if tc else ""
        elif event.event_type.value == "observation":
            obs = event.payload.get("observation", {})
            detail = f"  status={obs.get('status')}"
        click.echo(f"  {ts}  {etype:<16}{detail}")


@log.command("list")
@click.option("--dir", "log_dir", default="./logs", help="Log directory")
def log_list(log_dir: str) -> None:
    """List all event log files.

    按修改时间倒序排列，显示文件名和文件大小。
    """
    log_path = Path(log_dir)
    if not log_path.exists():
        click.echo(f"Log directory not found: {log_path}")
        return

    files = sorted(log_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No log files found.")
        return

    click.echo(bold(f"\nLog files in {log_path}:\n"))
    for f in files:
        size_kb = f.stat().st_size / 1024
        click.echo(f"  {f.name}  ({size_kb:.1f} KB)")
    click.echo()


# ===========================================================================
# 入口
# 由 pyproject.toml 配置的 [project.scripts] 入口点调用：
#   agent = "entry.cli:main"
# 也可直接运行 python -m entry.cli run --task "fix it"
# ===========================================================================

def main() -> None:
    """CLI 入口函数。调用 click group 的主命令分发。"""
    cli(obj={})


if __name__ == "__main__":
    main()