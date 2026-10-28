"""
multi_agent/task_registry.py

父 Agent 的任务注册中心（TaskRegistry）。

功能：
- spawn(sub_agent, agent_type) — 启动子 Agent 并登记
- poll() — 检查是否有新完成的子任务
- drain_notifications() — 收取待处理的完成通知
- send_message_to_child() — 向运行中的子 Agent 发消息

父 Agent 在自己的 ReAct 循环每步开始时调用 _check_child_completions()，
该方法内部调用 poll() + drain_notifications()，
将完成通知注入对话历史。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from agent.task import RunResult, TaskStatus, TaskNotification, SubAgentType


@dataclass
class BackgroundTask:
    """跟踪一个后台子 Agent 的运行状态。"""
    task_id: str                                # 子 agent 的 task_id
    agent_type: SubAgentType                    # explore / review / fork
    status: str = "running"                     # "running" | "completed" | "error" | "timed_out"
    started_at: float = field(                  # time.time() 启动时间戳
        default_factory=time.time
    )
    result: RunResult | None = None             # 完成后的结果（None 表示尚未完成）
    error_message: str | None = None            # 异常错误信息


class TaskRegistry:
    """
    父 Agent 持有的全局任务注册表。

    所有 spawn 出来的子 Agent 都在这里登记。
    线程安全，支持多子 Agent 并发。

    用法：
        registry = TaskRegistry()
        registry.spawn(sub_agent, SubAgentType.EXPLORE)
        # ... 在 Agent.run() 循环中 ...
        completed = registry.poll()
        for notif in registry.drain_notifications():
            # 注入父 Agent 对话历史
            pass
    """

    def __init__(self, max_active: int = 3) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._pending_notifications: list[BackgroundTask] = []
        self._max_active = max_active

    # ── 子 Agent 生命周期管理 ──────────────────────────────────────

    def spawn(self, sub_agent, agent_type: SubAgentType) -> BackgroundTask:
        """
        启动一个子 Agent 并注册到本表。

        Args:
            sub_agent: SubAgent 实例（包含 task_id, thread 等）
            agent_type: explore / review / fork

        Returns:
            BackgroundTask 记录对象
        """
        task = BackgroundTask(
            task_id=sub_agent.task_id,
            agent_type=agent_type,
            status="running",
        )
        # 直接关联 SubAgent 实例
        task._sub_agent = sub_agent
        with self._lock:
            if sum(t.status == "running" for t in self._tasks.values()) >= self._max_active:
                raise RuntimeError(f"Sub-agent limit reached ({self._max_active}); wait for running tasks.")
            self._tasks[task.task_id] = task
        # 启动子 Agent 线程
        sub_agent.start()
        return task

    # ── 轮询完成状态 ───────────────────────────────────────────────

    def poll(self) -> list[BackgroundTask]:
        """
        检查所有 running 任务，发现已完成的则更新状态。

        返回本轮新完成的任务列表。
        幂等：已完成的任务不会重复出现在返回列表中。
        """
        completed: list[BackgroundTask] = []
        with self._lock:
            for task in self._tasks.values():
                if task.status != "running":
                    continue
                agent = task._sub_agent  # 由 spawn 时通过 setattr 关联
                if agent is not None and agent.is_done():
                    result = agent.result
                    if result is not None:
                        task.result = result
                        if result.is_success():
                            task.status = TaskStatus.COMPLETED.value
                        elif result.status.value == "max_steps":
                            task.status = TaskStatus.TIMED_OUT.value
                        else:
                            task.status = TaskStatus.ERROR.value
                    else:
                        task.status = TaskStatus.ERROR.value
                        task.error_message = agent.error
                    completed.append(task)
                    self._pending_notifications.append(task)
        return completed

    # ── 通知收取 ──────────────────────────────────────────────────

    def drain_notifications(self) -> list[TaskNotification]:
        """
        收取并清空待处理的通知队列，转换为 TaskNotification 列表。

        父 Agent 在每步循环中调用此方法，将返回的通知注入对话历史。
        """
        notifications: list[TaskNotification] = []
        with self._lock:
            for task in self._pending_notifications:
                # 兼容字符串和枚举两种 agent_type
                at = task.agent_type
                if isinstance(at, SubAgentType):
                    agent_type = at
                else:
                    try:
                        agent_type = SubAgentType(at)
                    except ValueError:
                        agent_type = SubAgentType.FORK  # fallback

                notif = TaskNotification(
                    task_id=task.task_id,
                    agent_type=agent_type,
                    status=TaskStatus(task.status),
                    summary=(
                        task.result.summary
                        if task.result
                        else (task.error_message or "Unknown error")
                    ),
                    steps_taken=task.result.steps_taken if task.result else 0,
                    total_tokens=task.result.total_tokens if task.result else 0,
                    patch=task.result.patch if task.result else None,
                )
                notifications.append(notif)
            self._pending_notifications.clear()
        return notifications

    # ── 向子 Agent 发消息 ──────────────────────────────────────────

    def send_message_to_child(self, task_id: str, content: str) -> bool:
        """
        父 Agent 向运行中的子 Agent 发送消息。

        Returns:
            True 如果消息成功投递，False 如果子 Agent 不存在或已结束。
        """
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None or task.status != "running":
            return False
        agent = task._sub_agent
        if agent is not None:
            agent.send_parent_message(content)
            return True
        return False

    # ── 优雅关闭 ──────────────────────────────────────────────────

    def shutdown(self, timeout: float = 10.0) -> list[BackgroundTask]:
        """
        停止所有运行中的子 Agent 并等待线程结束。

        流程：
        1. 向所有 running 子 Agent 发送 stop 信号
        2. 等待每个子线程 join（最多 timeout 秒）
        3. 收集所有结果（包括被中断后保存的部分结果）
        4. 将未通知的结果放入通知队列

        Args:
            timeout: 每个子 Agent 的最大等待时间（秒）

        Returns:
            所有子任务的最终 BackgroundTask 列表
        """
        running: list[BackgroundTask] = []
        with self._lock:
            for task in self._tasks.values():
                if task.status == "running":
                    running.append(task)

        # 向所有运行中的子 Agent 发送停止信号并等待
        for task in running:
            agent = task._sub_agent
            if agent is not None:
                agent.stop(timeout=timeout)

        # 拉取最终状态（包括被中断后的结果）
        self.poll()

        # 返回所有任务状态
        with self._lock:
            return list(self._tasks.values())

    # ── 查询方法 ──────────────────────────────────────────────────

    def get(self, task_id: str) -> BackgroundTask | None:
        """按 task_id 查找后台任务。"""
        with self._lock:
            return self._tasks.get(task_id)

    def active_count(self) -> int:
        """当前仍在运行中的子 Agent 数量。"""
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == "running")

    @property
    def task_ids(self) -> list[str]:
        """所有已注册任务的 ID 列表。"""
        with self._lock:
            return list(self._tasks.keys())

    # ── 内部方法：关联 SubAgent 实例到 BackgroundTask ───────────────

    def _link_sub_agent(self, task_id: str, sub_agent) -> None:
        """
        将 SubAgent 实例关联到 BackgroundTask 记录。
        在 SubAgent.start() 之前由 TaskTool 调用。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task._sub_agent = sub_agent
