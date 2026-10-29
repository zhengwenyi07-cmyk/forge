"""
multi_agent/sub_agent.py

SubAgent 类 — 将现有 Agent 包装为受限于独立线程的子 Agent。

核心职责：
- 构建受限的 ToolRegistry（白名单/黑名单过滤）
- 复制父 Agent 的历史上下文用于 LLM 缓存前缀命中
- 在独立线程中运行 Agent.run()
- 通过 Mailbox 与父 Agent 通信

用法：
    sub = SubAgent(
        task_id="abc123",
        agent_type="explore",
        task=Task(...),
        parent_backend=backend,
        parent_registry=registry,
        parent_history=history,
        parent_system_prompt="...",
        allowed_tools=EXPLORE_TOOL_NAMES,
    )
    sub.start()               # 启动后台线程
    result = sub.wait(30)     # 阻塞等待（或超时返回 None）
"""

from __future__ import annotations

import threading
import time
import copy
from dataclasses import replace
from dataclasses import dataclass

from agent.core import Agent, AgentConfig
from agent.task import (
    Task, RunResult, RunStatus, Action, ActionType,
    Observation, ObservationStatus,
)
from agent.event_log import EventLog
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from tools.base import ToolRegistry
from tools.runtime import Runtime

from multi_agent.mailbox import Mailbox, Message
from multi_agent.blacklist import SUB_AGENT_BLACKLIST


# ============================================================================
# 受限注册表构建
# ============================================================================

def build_sub_agent_registry(
    parent_registry: ToolRegistry,
    allowed_tools: frozenset[str] | None,
    blacklist: frozenset[str] | None = None,
    runtime: Runtime | None = None,
) -> ToolRegistry:
    """
    从父 Agent 的注册表中构建子 Agent 的受限注册表。

    Args:
        parent_registry: 父 Agent 的 ToolRegistry
        allowed_tools: 白名单（None 表示使用父全部工具 - 黑名单）
        blacklist: 额外黑名单（默认使用 SUB_AGENT_BLACKLIST）
        runtime: 注入给子 Agent 工具的 Runtime 实例

    Returns:
        新的 ToolRegistry，只包含过滤后的工具
    """
    bl = blacklist or SUB_AGENT_BLACKLIST
    sub_registry = ToolRegistry(parent_registry.confirm_callback, parent_registry.read_only)

    for name, tool in parent_registry._tools.items():
        # 黑名单过滤（所有子 Agent 都不能用的工具）
        if name in bl:
            continue
        # 白名单过滤（如果指定了白名单，只注册白名单中的工具）
        if allowed_tools is not None and name not in allowed_tools:
            continue
        child_tool = copy.copy(tool)
        if runtime is not None and hasattr(child_tool, "_runtime"):
            child_tool._runtime = runtime
        sub_registry.register(child_tool)

    from tools.control_tool import FinishTool, GiveUpTool
    for tool in (FinishTool(), GiveUpTool()):
        if tool.name not in sub_registry:
            sub_registry.register(tool)

    return sub_registry


# ============================================================================
# SubAgent
# ============================================================================

class SubAgent:
    """
    在独立线程中运行 Agent 的子 Agent 包装器。

    Lifecycle:
        sub = SubAgent(...)
        sub.start()           # 启动后台线程（非 daemon，父需显式 stop）
        sub.wait(timeout=30)  # 阻塞直到完成或超时
        sub.is_done()         # 非阻塞检查
        sub.result            # 最终 RunResult（完成前为 None）
        sub.stop(timeout=10)  # 发送中断信号 + 等待线程结束（优雅终止）

    子 Agent 与父 Agent 的关键差异：
    - 受限的工具集（由 build_sub_agent_registry 生成）
    - 独立的 ConversationHistory（复制父历史前缀用于缓存）
    - 更小的 max_steps 和 budget_tokens
    - 不输出流式内容
    """

    def __init__(
        self,
        task_id: str,
        agent_type: str,
        task: Task,
        parent_backend: LLMBackend,
        parent_registry: ToolRegistry,
        parent_history: ConversationHistory,
        parent_system_prompt: str,
        sub_agent_system_prompt: str | None = None,
        allowed_tools: frozenset[str] | None = None,
        max_steps: int = 20,
        timeout: int = 300,
        runtime: Runtime | None = None,
        log_dir: str = "./logs",
    ) -> None:
        self.task_id = task_id
        self.agent_type = agent_type
        self._timeout = timeout
        self._task = replace(task, task_type="read_only") if agent_type in ("explore", "review") else task
        self._parent_registry = parent_registry
        self._allowed_tools = allowed_tools
        self._parent_runtime = runtime
        self._isolated_worktree = None
        self._child_runtime = None

        # 构建受限工具注册表
        self._registry = build_sub_agent_registry(
            parent_registry, allowed_tools, SUB_AGENT_BLACKLIST, runtime
        )

        # 复用父 Agent 的 LLM Backend（共享连接，最大化缓存命中）
        self._backend = parent_backend

        # 复制父 Agent 历史用于上下文前缀（缓存命中关键）
        # 子 Agent 的 history = system prompt + 最近父历史 + 任务描述
        system_prompt_to_use = sub_agent_system_prompt or parent_system_prompt
        self._history = self._build_child_history(
            parent_history, system_prompt_to_use
        )

        # Agent 配置（比父 Agent 更保守）
        self._config = AgentConfig(
            max_steps=max_steps,
            budget_tokens=min(task.budget_tokens, 40_000),  # 子 Agent 预算有限
            history_max_messages=40,
            stream=False,  # 子 Agent 不流式输出
        )

        # 通信
        self._mailbox = Mailbox(task_id)

        # 线程管理
        self._thread: threading.Thread | None = None
        self._result: RunResult | None = None
        self._error: str | None = None
        self._done = threading.Event()
        # 停止信号：父线程设置此事件，子 Agent 每步循环检查
        self._stop_event = threading.Event()

        # 备用引用
        self._config.system_prompt_override = system_prompt_to_use
        self._config.message_source = self._mailbox.poll_inbox
        self._log_dir = log_dir

    # ── 构建子 Agent 历史（缓存复制） ───────────────────────────────

    def _build_child_history(
        self,
        parent_history: ConversationHistory,
        system_prompt: str,
    ) -> ConversationHistory:
        """
        从父 Agent 历史复制上下文前缀。

        子 Agent 的历史结构：
          [0] system prompt（子 Agent 专用或复用父的）
          [1..N] 父 Agent 最近 N 条消息（用于 LLM API 请求前缀匹配 → 缓存命中）
          [N+1] 子 Agent 的任务描述
        """
        child_history = ConversationHistory(max_messages=40)
        # 首条：system prompt
        child_history.add(LLMMessage(role="user", content="[SUB-AGENT ASSIGNMENT]\n" + self._task.description))
        # 复制父 Agent 最近历史作为缓存前缀（最多 10 条）
        parent_msgs = parent_history.to_list() if parent_history else []
        recent = parent_msgs[-min(10, len(parent_msgs)):] if parent_msgs else []
        while recent and recent[0].tool_call_id:
            recent.pop(0)
        for msg in recent:
            if getattr(msg, "role", None) == "system":
                continue
            child_history.add(LLMMessage(**msg.to_dict()))
        return child_history

    # ── 生命周期管理 ───────────────────────────────────────────────

    def start(self) -> None:
        """
        启动子 Agent 后台线程。

        线程为非 daemon 模式：父进程/线程退出前必须显式调用 stop() 或
        TaskRegistry.shutdown()，否则会阻塞进程退出。
        这确保了子 Agent 的工作不会被静默丢弃。
        """
        self._thread = threading.Thread(
            target=self._run_agent_loop,
            daemon=False,  # 非 daemon：必须显式 stop，不会被强制 kill
            name=f"sub-agent-{self.task_id}",
        )
        self._thread.start()

    def _run_agent_loop(self) -> None:
        """
        在线程内运行 Agent ReAct 循环。

        捕获所有异常，确保 _done.set() 始终被调用（即使被外部 stop 中断）。
        """
        try:
            # 将 _stop_event 注入 Agent 配置，Agent 每步循环检查
            self._deadline = time.monotonic() + self._timeout
            self._config.stop_check = lambda: self._stop_event.is_set() or time.monotonic() >= self._deadline
            if self.agent_type == "fork":
                from multi_agent.worktree import IsolatedWorktree
                from tools.runtime import DockerRuntime, LocalRuntime
                self._isolated_worktree = IsolatedWorktree(self._task.repo_path).create()
                root = str(self._isolated_worktree.root)
                if isinstance(self._parent_runtime, DockerRuntime):
                    mounts, environment = self._isolated_worktree.docker_git_mount(self._parent_runtime._workdir)
                    self._child_runtime = self._parent_runtime.for_repo(root, mounts, environment)
                else:
                    self._child_runtime = LocalRuntime(repo_path=root)
                self._registry = build_sub_agent_registry(self._parent_registry, self._allowed_tools, SUB_AGENT_BLACKLIST, self._child_runtime)
                self._task = replace(self._task, repo_path=root)
                self._history.add(LLMMessage(role="user", content="Work only in your isolated repository: /workspace in Docker, otherwise " + root + ". Use relative paths. Return a patch; do not merge into the parent."))

            agent = Agent(self._backend, self._registry, self._config, runtime=self._child_runtime or self._parent_runtime)

            # 注入子 Agent 的独立 history（复用 ChatSession 的注入模式）
            agent._pending_history = self._history

            with EventLog.create(self._task, log_dir=self._log_dir) as log:
                self._result = agent.run(self._task, log)

        except Exception as exc:
            self._error = str(exc)
            self._result = RunResult(
                task_id=self.task_id,
                status=RunStatus.FAILED,
                summary=f"Sub-agent [{self.task_id}] crashed: {exc}",
                steps_taken=0,
                total_tokens=0,
                error=str(exc),
            )
        finally:
            try:
                if self._child_runtime:
                    self._child_runtime.cleanup()
                if self._isolated_worktree:
                    if self._result and self._result.artifact_error:
                        self._result.recovery_worktree = self._isolated_worktree.retain()
                    else:
                        self._isolated_worktree.cleanup()
            except Exception as exc:
                self._error = f"Worktree cleanup failed: {exc}"
            self._done.set()

    def request_stop(self):
        self._stop_event.set()

    def stop(self, timeout: float = 10.0) -> RunResult | None:
        """
        发送中断信号并等待子 Agent 线程结束（优雅终止）。

        Args:
            timeout: 等待线程 join 的最长时间（秒）

        Returns:
            RunResult 如果线程在超时前结束，None 如果超时仍需强制终止
        """
        if self._thread is None:
            return self._result

        # 1. 设置中断信号 → Agent 在下一步循环开始时捕获
        self._stop_event.set()

        # 2. 等待线程自行结束（Agent 检测到 stop_event 后退出循环）
        self._thread.join(timeout=timeout)

        if self._thread.is_alive():
            # 超时仍未结束 — 线程可能卡在 LLM API 调用中
            # 无法安全强制 kill（Python 线程无 terminate），
            # 但 _done 可能还未 set，调用方通过 is_done() 可感知
            return None

        return self._result

    def wait(self, timeout: float | None = None) -> RunResult | None:
        """
        阻塞等待子 Agent 完成。

        Args:
            timeout: 最长等待秒数（None → 使用子 Agent 的 timeout 配置）

        Returns:
            RunResult 如果完成，None 如果超时尚未完成
        """
        effective = min(timeout, self._timeout) if timeout else self._timeout
        if self._done.wait(timeout=effective):
            return self._result
        return None

    def is_done(self) -> bool:
        """非阻塞检查子 Agent 是否已完成。"""
        return self._done.is_set()

    # ── 父→子通信 ─────────────────────────────────────────────────

    def send_parent_message(self, content: str) -> None:
        """
        父 Agent 向此子 Agent 发送消息。

        消息放入 Mailbox inbox，子 Agent 在下一轮结束时读取。
        """
        msg = Message(
            sender="parent",
            recipient=self.task_id,
            content=content,
            msg_type="instruction",
        )
        self._mailbox.send_to_child(msg)

    # ── 属性 ───────────────────────────────────────────────────────

    @property
    def result(self) -> RunResult | None:
        return self._result

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def mailbox(self) -> Mailbox:
        return self._mailbox

    def __repr__(self) -> str:
        status = "done" if self.is_done() else "running"
        return (
            f"SubAgent(id={self.task_id!r}, type={self.agent_type!r}, "
            f"status={status})"
        )
