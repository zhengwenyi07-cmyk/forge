"""
multi_agent/__init__.py

Multi-agent 子系统：为 forge-agent 提供并行子 agent 能力。

核心类：
- SubAgent      — 包装 Agent 运行在独立线程，带受限工具集
- TaskRegistry  — 父 agent 全局任务注册表
- Mailbox       — 线程安全消息队列
- TaskTool      — 父 agent 调用的统一入口工具

用法：
    from multi_agent import SubAgent, TaskRegistry, Mailbox
"""

from multi_agent.sub_agent import SubAgent, build_sub_agent_registry
from multi_agent.task_registry import TaskRegistry, BackgroundTask
from multi_agent.mailbox import Mailbox, Message

__all__ = [
    "SubAgent",
    "build_sub_agent_registry",
    "TaskRegistry",
    "BackgroundTask",
    "Mailbox",
    "Message",
]
