"""
multi_agent/mailbox.py

线程安全的消息队列，用于父 Agent 与子 Agent 之间的异步通信。

设计：
- 每个子 Agent 有一个 Mailbox
- 父 Agent 通过 inbox 向子发送指令（send + check 模式）
- 子 Agent 通过 outbox 向父发送完成通知
- 所有操作受 threading.Lock 保护

用法：
    mb = Mailbox("abc123")
    mb.send_to_child(Message("parent", "abc123", "priority: look at auth module first"))
    # ... 子 Agent 一轮结束后 ...
    new_msgs = mb.poll_inbox()  # 取出所有新消息
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Message:
    """
    父 Agent 与子 Agent 之间的一条消息。

    sender / recipient 可以是 "parent" 或子 agent 的 task_id。
    """
    sender: str             # "parent" | child_id
    recipient: str          # child_id | "parent"
    content: str            # 消息正文
    msg_type: str = "instruction"   # "instruction" | "notification" | "result"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Mailbox:
    """
    线程安全的消息队列。

    - inbox：父 Agent → 子 Agent（父写入，子读取并清空）
    - outbox：子 Agent → 父 Agent（子写入，父通过 TaskRegistry 收集）

    子 Agent 每轮工具调用结束后 poll_inbox()，
    有新消息则注入为 user 消息进入下一轮 LLM 调用。
    """

    def __init__(self, owner_id: str) -> None:
        self._owner_id = owner_id
        self._inbox: list[Message] = []        # 来自父 Agent 的消息
        self._outbox: list[Message] = []       # 发给父 Agent 的消息
        self._lock = threading.Lock()

    # ── 父 Agent 操作 ──────────────────────────────────────────────

    def send_to_child(self, msg: Message) -> None:
        """父 Agent 向此子 Agent 发送一条消息。"""
        with self._lock:
            self._inbox.append(msg)

    def drain_outbox(self) -> list[Message]:
        """父 Agent（通过 TaskRegistry）收取子 Agent 的完成通知。"""
        with self._lock:
            msgs = self._outbox[:]
            self._outbox.clear()
            return msgs

    # ── 子 Agent 操作 ──────────────────────────────────────────────

    def poll_inbox(self) -> list[Message]:
        """
        子 Agent 检查是否有来自父 Agent 的新消息。
        取出后清空 inbox，消息将注入子 Agent 的对话历史。
        """
        with self._lock:
            msgs = self._inbox[:]
            self._inbox.clear()
            return msgs

    def post_result(self, msg: Message) -> None:
        """子 Agent 将完成结果放入 outbox，等待父 Agent 收取。"""
        with self._lock:
            self._outbox.append(msg)

    # ── 便利属性 ──────────────────────────────────────────────────

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def has_pending_inbox(self) -> bool:
        with self._lock:
            return len(self._inbox) > 0

    @property
    def has_pending_outbox(self) -> bool:
        with self._lock:
            return len(self._outbox) > 0
