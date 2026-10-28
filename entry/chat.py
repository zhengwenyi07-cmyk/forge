"""
entry/chat.py

交互对话模式。持续会话，每轮用户输入后 agent 继续工作，
history 跨轮保留，像 Claude Code 一样可以持续对话。

架构设计：
- ChatSession 持有 backend / registry / history，跨轮复用
- 每轮创建一个新 Task，但 history 通过 agent._inject_history() 延续
- EventLog 每轮独立（方便单轮审计），但统计累计显示
- 实时打印：每条 event 写入 log 后立刻 echo，不等跑完

用法：
    agent chat --repo /path/to/repo
    agent chat --repo . --model deepseek-chat

数据流（每轮对话）：
  CLI (entry/cli.py) → ChatSession.run_round(user_input)
    → 追加到 shared_history
    → Task(description=user_input, repo_path=fixed)
    → agent._pending_history = shared_history  (注入跨轮历史)
    → agent.run(task, log)                      (ReAct 循环)
    → log._append monkey-patch → live print     (实时 UI)
    → 结果写入 shared_history                    (跨轮保留)
    → 打印 "Round N · X steps · Y tokens · Zs"

对应简历"多轮对话历史跨轮传递"。
"""

from __future__ import annotations

# time.time()：run_round 中计时 elapsed = time.time() - t0
import time
# sys.path：_ROOT 路径添加到 sys.path 确保模块可 import
# sys.stdout.flush()：流式输出每个 token 后立即 flush
import sys
# Path：_ROOT = Path(__file__).parent.parent 定位项目根目录
from pathlib import Path
# Callable：用于类型注解（虽然 ChatSession.__init__ 中 confirm_callback 未标注类型）
from typing import Callable

# click：CLI 框架。
# click.echo()：跨平台终端输出（比 print 更好——自动处理换行和编码）
import click

# 项目根目录路径（entry/chat.py → entry/ → 项目根）
# 通过 sys.path.insert 确保 from 导入能找到模块
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ===========================================================================
# 彩色输出（复用 entry/cli.py 的风格）
# ANSI 转义序列：
# 32=绿、33=黄、31=红、36=青、1=粗体、2=暗色、35=品红
# 非 TTY 环境（管道/文件重定向）自动降级为纯文本
# ===========================================================================

def _c(t: str, code: str) -> str:
    """包裹 ANSI 转义码，非 TTY 时降级为纯文本。"""
    return f"\033[{code}m{t}\033[0m" if sys.stdout.isatty() else t

def green(t: str) -> str:  return _c(t, "32")    # 成功消息 / 系统状态
def yellow(t: str) -> str: return _c(t, "33")   # Reflection / 警告
def red(t: str) -> str:    return _c(t, "31")    # 错误消息
def cyan(t: str) -> str:   return _c(t, "36")    # Action / 工具名
def bold(t: str) -> str:   return _c(t, "1")     # 标题 / 关键信息
def dim(t: str) -> str:    return _c(t, "2")     # 次要信息 / 工具输出预览 / 流式 thought
def magenta(t: str) -> str: return _c(t, "35")   # 用户输入提示符 "you >"


# ===========================================================================
# 实时 event 打印
# 通过 monkey-patch EventLog._append() 实现"写入即实时显示"。
# 每条 Event 被写入 JSONL 文件后，立即被 _print_event_live 打印到终端。
# 这样用户不需要等 Agent.run() 完成就能看到每一步的输出。
#
# 打印策略：
# - ACTION：工具名 + 关键参数（cmd/path/pattern）
# - OBSERVATION：成功→✓，失败→✗ + 错误摘要
#   - 只读工具（file_read/file_view 等）不打印输出内容
#   - 其他工具（shell/test）打印前 20 行预览
# - REFLECTION：⟳ 标记 + 触发原因
# - TASK_COMPLETE：最终回答（仅当与流式打印的 thought 不同时）
# - TASK_FAILED：❌ 标记 + 原因
# ===========================================================================

def _print_event_live(event) -> None:
    """每条 event 写入 log 后立刻调用，实时显示。"""
    from agent.task import EventType
    etype = event.event_type
    p = event.payload

    # ── ACTION event：打印工具名 + 关键参数 ──────────────────────────────
    if etype == EventType.ACTION:
        step = p["step"]
        action = p["action"]
        thought = (action.get("thought") or "").strip()
        atype = action.get("action_type", "")
        tc = action.get("tool_call")

        # 流式模式：thought 已经被 stream_callback 实时打印出来了，
        # 非流式模式或 thought 为空时，在这里补打。
        # finish/give_up 的 thought = 回答内容，已在 TASK_COMPLETE 里显示，不重复。
        if thought and thought != "(no thought)" and atype not in ("finish", "give_up"):
            import sys
            sys.stdout.write("\n")   # 确保工具调用从新行开始
            sys.stdout.flush()

        if tc:
            # 缓存工具名，供下一个 observation event 使用
            _print_event_live._last_tool_name = tc['name']
            click.echo(cyan(f"  [{step}] {tc['name']}"), nl=False)
            # 打印关键参数（优先显示 cmd/path/pattern/symbol/message 这些最常用的）
            params = tc.get("params", {})
            key_param = (
                params.get("cmd")
                or params.get("path")
                or params.get("pattern")
                or params.get("symbol")
                or params.get("message")
                or ""
            )
            if key_param:
                short_param = str(key_param)[:60]
                suffix = "..." if len(str(key_param)) > 60 else ""
                click.echo(cyan(f"  {short_param}{suffix}"))
            else:
                click.echo()
        elif atype == "finish":
            click.echo(green(f"\n  [{step}] ✓ finish"))
            # 把 message 存到函数属性，供 TASK_COMPLETE event 打印
            _finish_message = action.get("message", "") or ""
            _print_event_live._pending_message = _finish_message
        elif atype == "give_up":
            click.echo(red(f"\n  [{step}] ✗ give_up"))

    # ── OBSERVATION event：显示执行结果 ──────────────────────────────────
    elif etype == EventType.OBSERVATION:
        obs = p["observation"]
        status = obs.get("status", "")
        output = (obs.get("output") or "").strip()
        error = obs.get("error")

        # 从上一条 action event 取工具名（_last_tool_name 由 ACTION 分支设置）
        tool_name = getattr(_print_event_live, "_last_tool_name", "")

        # 只读类工具：只显示 ✓ 或 ✗，不打印内容（内容已被模型读取，用户不需要看）
        SILENT_TOOLS = {"file_read", "file_view", "file_write", "find_files", "find_symbol"}
        silent = tool_name in SILENT_TOOLS

        if status == "success":
            if silent:
                click.echo(green("  ✓"))
            else:
                lines = output.splitlines()
                MAX_PREVIEW = 20
                preview = "\n".join(f"    {l}" for l in lines[:MAX_PREVIEW])
                if lines:
                    click.echo(green("  ✓") + dim(f"\n{preview}"))
                    if len(lines) > MAX_PREVIEW:
                        click.echo(dim(f"    ... ({len(lines)-MAX_PREVIEW} more lines)"))
                else:
                    click.echo(green("  ✓"))
        else:
            click.echo(red(f"  ✗ {error or output[:120]}"))

    # ── REFLECTION event：显示自省标记 ────────────────────────────────────
    elif etype == EventType.REFLECTION:
        reason = p.get("reason", "")
        click.echo(yellow(f"\n  ⟳ Reflection ({reason}) — reconsidering approach...\n"))

    # ── TASK_COMPLETE event：打印最终回答 ────────────────────────────────
    elif etype == EventType.TASK_COMPLETE:
        # 取出 finish action 存的 message
        message = getattr(_print_event_live, "_pending_message", "")
        _print_event_live._pending_message = ""

        if message:
            # 获取流式打印的 thought（存在 stream_callback 里）
            streamed = getattr(_print_event_live, "_streamed_thought", "").strip()
            msg_stripped = message.strip()

            if msg_stripped and msg_stripped != streamed:
                # thought 和 message 不同（如 Claude 的分开输出）→ 单独打印最终回答
                import sys
                sys.stdout.write("\n")
                sys.stdout.flush()
                click.echo(msg_stripped)
            # thought == message（如 DeepSeek flash）→ 已经流式打印过，不重复

    # ── TASK_FAILED event：显示失败原因 ──────────────────────────────────
    elif etype == EventType.TASK_FAILED:
        reason = p.get("reason", "")
        click.echo(red(bold(f"\n  ❌ Failed: {reason}")))


# ===========================================================================
# ChatSession — 跨轮持久化的会话状态
# 对应简历"多轮对话历史跨轮传递"——ChatSession 通过 _pending_history 注入，
# 使 Agent 能在多轮对话间保留对话历史。
#
# 关键设计：
# - 一个 Agent 实例跨所有轮次复用（非每轮新建）
# - _shared_history 在轮次间持久保留（通过 _pending_history 注入 agent.run）
# - EventLog 每轮独立（文件按 task_id 隔离），但统计累计到 session
# - 流式输出通过 monkey-patch EventLog._append 实现"写入即打印"
# ===========================================================================

class ChatSession:
    """
    持久化会话。跨多轮对话保留：
    - backend / registry（不变）
    - ConversationHistory（核心：让 agent 记得之前做了什么）
    - 累计 token / 步数统计
    - repo_map 缓存（换 repo 时自动失效）
    """

    def __init__(self, backend, registry, config, repo_path: str, log_dir: str, confirm_callback=None, task_registry=None, runtime=None, task_type="modify") -> None:
        from agent.core import Agent, AgentConfig
        from context.history import ConversationHistory

        self.repo_path = repo_path
        self.log_dir = log_dir
        self.config = config
        self.task_type = task_type
        self._confirm_callback = confirm_callback

        # ── 流式回调闭包 ──────────────────────────────────────────────
        # 使用列表而非简单布尔值，是因为闭包捕获的是引用而非值。
        # [False] → 在内部函数中可以通过 _stream_started[0] 修改。
        _stream_started = [False]      # 流式输出是否已经开始
        _thought_printed = [False]     # 是否已经打印过 thought（用于分隔）
        _streamed_buf = []              # 收集流式文本，用于和最终 message 比较

        def _thought_cb(text: str) -> None:
            """
            推理过程的流式回调。
            在 chat 模式中，thought 用暗色（dim）打印，表示模型在内部分析。
            仅推理模型（DeepSeek R1、Claude thinking）有内容。
            """
            import sys
            if not _stream_started[0]:
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            sys.stdout.write(dim(text))
            sys.stdout.flush()
            _thought_printed[0] = True

        def _stream_cb(text: str) -> None:
            """
            最终回答的流式回调。
            在 chat 模式中，最终回答用正常亮色打印。
            如果之前打印过 thought，先换两行作为视觉分隔。
            """
            import sys
            if not _stream_started[0]:
                sys.stdout.write("\r  ")
                sys.stdout.flush()
                _stream_started[0] = True
            elif _thought_printed[0]:
                # 之前打过 thought，先换两行作为分隔再打 message
                sys.stdout.write("\n\n")
                sys.stdout.flush()
                _thought_printed[0] = False  # 只换一次
            sys.stdout.write(text)
            sys.stdout.flush()
            _streamed_buf.append(text)
            _print_event_live._streamed_thought = "".join(_streamed_buf)

        agent_cfg = AgentConfig(
            max_steps=config.agent.max_steps,
            budget_tokens=config.agent.budget_tokens,
            # history_max_messages 设为 history_window * 2，
            # 因为一轮对话通常包含 1 条用户输入 + 多条 assistant 回复
            history_max_messages=config.context.history_window * 2,
            llm_max_retries=3,
            llm_retry_delay=1.0,
            stream=True,                # chat 模式默认启用流式输出
            stream_callback=_stream_cb,
            thought_callback=_thought_cb,
            confirm_dangerous=confirm_callback is not None,
            confirm_callback=confirm_callback,
        )
        self.agent = Agent(backend, registry, agent_cfg, task_registry=task_registry, runtime=runtime)
        # 共享历史：跨轮次复用，不随 EventLog 清空
        # max_messages = history_window * 2（同上理由）
        self._shared_history = ConversationHistory(
            max_messages=config.context.history_window * 2
        )

        # ── 累计统计（跨轮累加） ──────────────────────────────────────
        self.total_tokens = 0
        self.total_steps = 0
        self.round_count = 0

    def run_round(self, user_input: str) -> bool:
        """
        执行一轮对话。

        Args:
            user_input: 用户这轮的输入

        Returns:
            True 表示成功/正常结束，False 表示失败
        """
        from agent.core import AgentConfig
        from agent.event_log import EventLog
        from agent.task import Task
        from llm.base import LLMMessage

        self.round_count += 1

        # 把用户输入追加到共享 history（下一轮 LLM 调用时能看到此输入）
        self._shared_history.add(LLMMessage(role="user", content=user_input))

        # 构建这轮的 Task。
        # repo_path 固定（整个 session 不变），description 用用户当轮输入。
        # max_steps / budget_tokens 从 config 读取，每轮独立执行任务时按此步数/预算限制。
        task = Task(
            description=user_input,
            repo_path=self.repo_path,
            max_steps=self.config.agent.max_steps,
            budget_tokens=self.config.agent.budget_tokens,
            task_type=self.task_type,
        )

        # 把共享 history 注入 agent（替换它内部的 history）
        # 通过 monkey-patch _pending_history 实现跨轮续接
        self.agent._shared_history = self._shared_history

        t0 = time.time()
        with EventLog.create(task, log_dir=self.log_dir) as log:
            # 实时打印：每条 event 写入后立刻 echo（不等 agent.run() 跑完）
            result = self._run_with_live_print(task, log)

        elapsed = time.time() - t0
        self.total_tokens += result.total_tokens
        self.total_steps += result.steps_taken

        # 把 agent 这轮的最后回复追加到共享 history
        # 这样下一轮 agent 能看到自己上一轮说了什么。
        # 在 run_round 返回后，shared_history 中包含：
        #   [0] 第一条任务描述
        #   [1..N] 各轮的 user input + agent response
        #   [N+1] 当前轮 user input
        #   [N+2] 当前轮 agent response ← 刚追加的
        if result.summary:
            self._shared_history.add(LLMMessage(
                role="assistant",
                content=f"[Round {self.round_count} complete]\n{result.summary}",
            ))

        # 流式输出结束后打换行，重置 readline 的行状态（避免下一轮提示符位置错乱）
        import sys as _sys
        _sys.stdout.write("\n")
        _sys.stdout.flush()

        # 打印轮次统计（供 /stats 命令和视觉结束标记）
        click.echo(dim(
            f"  ─── Round {self.round_count} · "
            f"{result.steps_taken} steps · "
            f"{result.total_tokens:,} tokens · "
            f"{elapsed:.1f}s ───"
        ))

        return result.is_success() or result.status.value == "gave_up"

    def _run_with_live_print(self, task, log):
        """
        运行 agent，同时实时打印 event。

        因为 agent.run() 是同步的（跑完才返回），无法在 run() 执行中间插入 UI 更新。
        解决方案：monkey-patch EventLog._append 方法——在它写完 JSONL 到磁盘之后、
        返回之前，调用 _print_event_live() 实时打印到终端。
        这样用户不需要等整轮跑完就能看到每一步的输出。

        这个技巧在测试中也很有用——test_chat.py 中的 ChatSession 测试也由此观察到 Event 序列。
        """
        original_append = log._append

        def live_append(event):
            original_append(event)      # 写入 JSONL（磁盘持久化）
            _print_event_live(event)    # 实时打印（终端 UI）

        log._append = live_append

        # 把共享 history 传给 agent
        # agent.run() 会创建新的 ConversationHistory，
        # 我们需要在它初始化后注入 _shared_history
        return self._run_injecting_history(task, log)

    def _run_injecting_history(self, task, log):
        """
        运行 agent，并在第一步前把共享 history 注入进去。

        核心 trick：
        给 agent 设一个 _pending_history 属性，core.py:run() 中检查此属性——
        如果非空就复用（作为已有历史），否则创建一个新 ConversationHistory。

        这样做的好处：
        - 不污染 Agent.run() 的签名的（signature 保持不变）
        - ChatSession 多轮对话的核心实现——跨轮复用 history
        - 清除 _pending_history 后，单次 run 模式（CLI run）不受影响

        core.py 中对应的代码（第 119-128 行）：
            if hasattr(self, "_pending_history") and self._pending_history is not None:
                history = self._pending_history
            else:
                history = ConversationHistory(...)
        """
        from context.history import ConversationHistory
        from agent.prompt import build_task_prompt
        from llm.base import LLMMessage

        agent = self.agent

        # 注入共享 history
        agent._pending_history = self._shared_history

        result = agent.run(task, log)

        # 清除，避免影响下轮（下一轮会重新注入更新的 _shared_history）
        if hasattr(agent, "_pending_history"):
            del agent._pending_history

        return result

    def print_stats(self) -> None:
        """打印会话总统计。

        在 chat REPL 中通过 /stats 命令调用。
        显示数据：总轮次 / 总步数 / 总 token 数。
        """
        click.echo(bold(f"\n{'─'*50}"))
        click.echo(f"  Session stats:")
        click.echo(f"    Rounds  : {self.round_count}")
        click.echo(f"    Steps   : {self.total_steps}")
        click.echo(f"    Tokens  : {self.total_tokens:,}")
        click.echo(bold(f"{'─'*50}\n"))
