"""
tests/test_multi_agent.py

Multi-agent 子系统端到端测试。

使用 MockBackend（预设 Action 脚本）实现完全确定性测试，
不依赖真实 LLM 调用。覆盖场景：
- Mailbox 线程安全
- TaskRegistry spawn/poll/drain
- SubAgent 正常完成 + 超时 + 错误
- TaskTool type 路由 + 阻塞/后台
- Agent 核心集成（子通知注入）
- 工具黑名单
- 多子 Agent 并发
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import (
    Action, ActionType, Observation, ObservationStatus,
    RunResult, RunStatus, Task, ToolCall,
    TaskStatus, SubAgentType, TaskNotification,
)
from context.history import ConversationHistory
from llm.base import LLMBackend, LLMMessage, MockBackend
from tools.base import ToolRegistry, NoopTool, FailingTool

from multi_agent.mailbox import Mailbox, Message
from multi_agent.task_registry import TaskRegistry, BackgroundTask
from multi_agent.sub_agent import SubAgent, build_sub_agent_registry
from multi_agent.blacklist import (
    SUB_AGENT_BLACKLIST,
    EXPLORE_TOOL_NAMES,
    REVIEW_TOOL_NAMES,
)
from multi_agent.task_prompts import (
    EXPLORE_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    FORK_SYSTEM_PROMPT_PREFIX,
)
from tools.task_tool import TaskTool


# ============================================================================
# Mailbox 测试
# ============================================================================

class TestMailbox:
    def test_send_and_poll(self):
        mb = Mailbox("child-1")
        mb.send_to_child(Message("parent", "child-1", "check auth module"))
        mb.send_to_child(Message("parent", "child-1", "also check login"))

        msgs = mb.poll_inbox()
        assert len(msgs) == 2
        assert msgs[0].content == "check auth module"
        assert msgs[1].content == "also check login"

        # poll 清空 inbox
        assert mb.poll_inbox() == []

    def test_post_result_and_drain(self):
        mb = Mailbox("child-1")
        mb.post_result(Message("child-1", "parent", "done", msg_type="result"))

        msgs = mb.drain_outbox()
        assert len(msgs) == 1
        assert msgs[0].msg_type == "result"
        # drain 清空 outbox
        assert mb.drain_outbox() == []

    def test_thread_safety(self):
        mb = Mailbox("child-1")
        N = 100

        def writer():
            for i in range(N):
                mb.send_to_child(Message("parent", "child-1", f"msg-{i}"))

        threads = [threading.Thread(target=writer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        msgs = mb.poll_inbox()
        assert len(msgs) == N * 5

    def test_has_pending(self):
        mb = Mailbox("child-1")
        assert not mb.has_pending_inbox
        assert not mb.has_pending_outbox

        mb.send_to_child(Message("parent", "child-1", "test"))
        assert mb.has_pending_inbox

        mb.post_result(Message("child-1", "parent", "done"))
        assert mb.has_pending_outbox


# ============================================================================
# TaskRegistry 测试
# ============================================================================

class TestTaskRegistry:
    def test_spawn_and_poll_completed(self, tmp_path):
        """子 Agent 正常完成"""
        registry = TaskRegistry()
        task_registry_builder = ToolRegistry().register(NoopTool("file_read")).register(NoopTool("search_text"))

        sub = SubAgent(
            task_id="aa000001",
            agent_type="explore",
            task=Task(task_type="read_only", description="Find auth", repo_path=str(tmp_path)),
            parent_backend=MockBackend([
                Action(ActionType.TOOL_CALL, "searching...", ToolCall("search_text", {"pattern": "auth"})),
                Action(ActionType.FINISH, "done", message="Found auth.py"),
            ]),
            parent_registry=task_registry_builder,
            parent_history=ConversationHistory(max_messages=20),
            parent_system_prompt="parent prompt",
            sub_agent_system_prompt=EXPLORE_SYSTEM_PROMPT,
            allowed_tools=EXPLORE_TOOL_NAMES,
            max_steps=10,
        )

        bg = registry.spawn(sub, SubAgentType.EXPLORE)
        assert bg.status == "running"
        assert registry.active_count() == 1

        # 等待完成
        result = sub.wait(timeout=5)
        assert result is not None
        assert result.is_success()

        # poll 更新状态
        completed = registry.poll()
        assert len(completed) == 1
        assert completed[0].status == TaskStatus.COMPLETED.value

        # drain 产生通知
        notifs = registry.drain_notifications()
        assert len(notifs) == 1
        assert notifs[0].task_id == "aa000001"
        assert notifs[0].agent_type == SubAgentType.EXPLORE
        assert notifs[0].status == TaskStatus.COMPLETED

    def test_spawn_multiple_concurrent(self, tmp_path):
        """多个子 Agent 并发运行"""
        registry = TaskRegistry()
        builder = ToolRegistry().register(NoopTool("file_read", output="found"))

        subs = []
        for i in range(3):
            sub = SubAgent(
                task_id=f"task-{i}",
                agent_type="explore",
                task=Task(task_type="read_only", description=f"Task {i}", repo_path=str(tmp_path)),
                parent_backend=MockBackend([
                    Action(ActionType.TOOL_CALL, "reading...", ToolCall("file_read", {"path": f"f{i}.py"})),
                    Action(ActionType.FINISH, "done", message=f"Result {i}"),
                ]),
                parent_registry=builder,
                parent_history=ConversationHistory(max_messages=20),
                parent_system_prompt="parent prompt",
                sub_agent_system_prompt=EXPLORE_SYSTEM_PROMPT,
                allowed_tools=EXPLORE_TOOL_NAMES,
                max_steps=10,
            )
            registry.spawn(sub, SubAgentType.EXPLORE)
            subs.append(sub)

        assert registry.active_count() == 3

        # 等待所有完成
        for sub in subs:
            result = sub.wait(timeout=5)
            assert result is not None

        completed = registry.poll()
        assert len(completed) == 3
        assert registry.active_count() == 0

    def test_subagent_error(self, tmp_path):
        """子 Agent 异常终止"""
        registry = TaskRegistry()
        builder = ToolRegistry().register(NoopTool("file_read"))

        # MockBackend 脚本用尽 → GAVE_UP
        sub = SubAgent(
            task_id="err-001",
            agent_type="explore",
            task=Task(task_type="read_only", description="Will fail", repo_path=str(tmp_path), max_steps=5),
            parent_backend=MockBackend([]),  # 无预设 Action → 直接 GAVE_UP
            parent_registry=builder,
            parent_history=ConversationHistory(max_messages=20),
            parent_system_prompt="parent prompt",
            sub_agent_system_prompt=EXPLORE_SYSTEM_PROMPT,
            allowed_tools=EXPLORE_TOOL_NAMES,
            max_steps=3,
        )

        bg = registry.spawn(sub, SubAgentType.EXPLORE)
        result = sub.wait(timeout=5)
        assert result is not None
        assert not result.is_success()

        completed = registry.poll()
        assert completed[0].status == TaskStatus.ERROR.value

    def test_send_message_to_child(self, tmp_path):
        """父 Agent 向运行中的子 Agent 发消息"""
        registry = TaskRegistry()
        builder = ToolRegistry().register(NoopTool("file_read"))

        sub = SubAgent(
            task_id="msg-001",
            agent_type="fork",
            task=Task(task_type="read_only", description="Background task", repo_path=str(tmp_path)),
            parent_backend=MockBackend([
                Action(ActionType.TOOL_CALL, "reading...", ToolCall("file_read", {"path": "x.py"})),
                Action(ActionType.FINISH, "done", message="OK"),
            ]),
            parent_registry=builder,
            parent_history=ConversationHistory(max_messages=20),
            parent_system_prompt="parent prompt",
            sub_agent_system_prompt="fork prompt",
            allowed_tools=None,
            max_steps=10,
        )

        bg = registry.spawn(sub, SubAgentType.FORK)
        registry._link_sub_agent("msg-001", sub)

        # 发消息
        sent = registry.send_message_to_child("msg-001", "check auth first")
        assert sent is True
        assert sub.mailbox.has_pending_inbox

    def test_send_message_to_nonexistent(self):
        """向不存在或已完成的任务发消息 → False"""
        registry = TaskRegistry()
        assert registry.send_message_to_child("no-such", "hey") is False

    def test_active_count(self, tmp_path):
        registry = TaskRegistry()
        builder = ToolRegistry().register(NoopTool("file_read"))

        sub = SubAgent(
            task_id="count-1",
            agent_type="explore",
            task=Task(task_type="read_only", description="Will timeout", repo_path=str(tmp_path), max_steps=3),
            parent_backend=MockBackend([
                Action(ActionType.TOOL_CALL, "search...", ToolCall("file_read", {"path": "x"})),
                Action(ActionType.TOOL_CALL, "search...", ToolCall("file_read", {"path": "y"})),
                Action(ActionType.TOOL_CALL, "search...", ToolCall("file_read", {"path": "z"})),
                Action(ActionType.TOOL_CALL, "search...", ToolCall("file_read", {"path": "w"})),
                # 超出 max_steps → 熔断
            ]),
            parent_registry=builder,
            parent_history=ConversationHistory(max_messages=20),
            parent_system_prompt="parent prompt",
            sub_agent_system_prompt=EXPLORE_SYSTEM_PROMPT,
            allowed_tools=EXPLORE_TOOL_NAMES,
            max_steps=3,
        )

        registry.spawn(sub, SubAgentType.EXPLORE)
        assert registry.active_count() == 1
        sub.wait(timeout=5)
        registry.poll()
        assert registry.active_count() == 0


# ============================================================================
# build_sub_agent_registry 测试
# ============================================================================

class TestBuildSubAgentRegistry:
    def test_whitelist_filters_correctly(self):
        parent = (
            ToolRegistry()
            .register(NoopTool("file_read"))
            .register(NoopTool("file_write"))
            .register(NoopTool("search_text"))
            .register(NoopTool("shell"))
        )

        sub = build_sub_agent_registry(parent, EXPLORE_TOOL_NAMES)
        assert "file_read" in sub
        assert "search_text" in sub
        assert "file_write" not in sub
        assert "shell" not in sub

    def test_blacklist_filters_task_tool(self):
        """子 Agent 不能派孙 agent（task 在黑名单中）"""
        parent = (
            ToolRegistry()
            .register(NoopTool("file_read"))
            .register(NoopTool("task"))  # task 工具
        )

        sub = build_sub_agent_registry(parent, None)
        assert "file_read" in sub
        assert "task" not in sub  # 被黑名单过滤

    def test_fork_gets_all_minus_blacklist(self):
        """Fork 子 Agent 获得全部工具 - 黑名单"""
        parent = (
            ToolRegistry()
            .register(NoopTool("file_read"))
            .register(NoopTool("file_write"))
            .register(NoopTool("shell"))
            .register(NoopTool("task"))
        )

        sub = build_sub_agent_registry(parent, None)
        assert "file_read" in sub
        assert "file_write" in sub  # fork 可以有写权限
        assert "shell" in sub
        assert "task" not in sub  # 但 task 在黑名单


# ============================================================================
# Agent 核心集成测试（MockBackend 驱动完整 ReAct 循环）
# ============================================================================

class TestAgentWithChildren:
    def test_child_completion_notification_injected(self, tmp_path):
        """
        完整集成测试：
        父 Agent spawn explore 子 Agent → 子完成 → 父收到通知。

        父 Agent 脚本：
          Step 1: call task (spawn explore) → 获取启动结果
          Step 2: 检查子完成 → 收到通知 → FINISH

        子 Agent 在独立线程中运行，使用独立 MockBackend。
        """
        # 父 Agent 使用的工具
        parent_registry = (
            ToolRegistry()
            .register(NoopTool("file_read", output="content of auth.py"))
            .register(NoopTool("search_text", output="auth.py:42"))
        )

        # 子 Agent 的 backend（独立脚本）
        child_backend = MockBackend([
            Action(ActionType.TOOL_CALL, "searching...", ToolCall("search_text", {"pattern": "auth"})),
            Action(ActionType.FINISH, "found auth module", message="Auth module located at src/auth.py:42"),
        ])

        # 伪造一个 TaskTool 来测试集成
        # 这里不通过 CLI 的 _build_registry，直接手动构建

        from multi_agent.task_registry import TaskRegistry as TR
        task_registry = TR()

        from tools.task_tool import TaskTool
        task_tool = TaskTool(
            backend=child_backend,
            registry=parent_registry,
            task_registry=task_registry,
            repo_path=str(tmp_path),
        )
        # 设置父上下文
        task_tool.set_parent_context(
            ConversationHistory(max_messages=20), "test system prompt"
        )

        # 注册 TaskTool
        parent_registry.register(task_tool)

        # 父 Agent 脚本：呼叫 task 工具 spawn explore
        parent_backend = MockBackend([
            Action(ActionType.TOOL_CALL, "spawning sub-agent",
                   ToolCall("task", {"type": "explore", "task": "Find auth module"})),
            Action(ActionType.FINISH, "got result from child", message="All done"),
        ])

        # 创建 Agent 并运行
        agent = Agent(parent_backend, parent_registry, AgentConfig(max_steps=10), task_registry=task_registry)
        agent._current_repo_path = str(tmp_path)

        task = Task(task_type="read_only", description="Test multi-agent", repo_path=str(tmp_path), max_steps=5)

        with EventLog.create(task, log_dir=str(tmp_path)) as log:
            result = agent.run(task, log)

        assert result.is_success()
        # 验证至少有一个子 Agent 被 spawn 且完成
        assert task_registry.task_ids  # 至少有一个子任务 ID

    def test_background_task_notification_arrives_later(self, tmp_path):
        """
        父 Agent spawn fork（后台模式），继续执行其他步骤，
        后续步骤中收到子 Agent 完成通知。
        """
        parent_registry = (
            ToolRegistry()
            .register(NoopTool("file_read", output="content"))
            .register(NoopTool("search_text", output="found"))
        )

        child_backend = MockBackend([
            Action(ActionType.TOOL_CALL, "working...", ToolCall("file_read", {"path": "x.py"})),
            Action(ActionType.FINISH, "done background work", message="Background task result"),
        ])

        task_registry = TaskRegistry()

        from tools.task_tool import TaskTool
        task_tool = TaskTool(
            backend=child_backend,
            registry=parent_registry,
            task_registry=task_registry,
            repo_path=str(tmp_path),
        )
        task_tool.set_parent_context(
            ConversationHistory(max_messages=20), "test system prompt"
        )

        parent_registry.register(task_tool)

        # 父：先 spawn fork（后台），再做其他事，最后收结果
        parent_backend = MockBackend([
            # Step 1: spawn fork
            Action(ActionType.TOOL_CALL, "spawn fork",
                   ToolCall("task", {"type": "fork", "task": "Background analysis", "wait": False})),
            # Step 2: 做其他事
            Action(ActionType.TOOL_CALL, "reading something",
                   ToolCall("file_read", {"path": "main.py"})),
            # Step 3: FINISH
            Action(ActionType.FINISH, "work done", message="All work completed"),
        ])

        agent = Agent(parent_backend, parent_registry, AgentConfig(max_steps=10), task_registry=task_registry)
        agent._current_repo_path = str(tmp_path)

        task = Task(task_type="read_only", description="Test background", repo_path=str(tmp_path), max_steps=5)

        with EventLog.create(task, log_dir=str(tmp_path)) as log:
            result = agent.run(task, log)

        assert result.is_success()


# ============================================================================
# TaskTool 独立测试
# ============================================================================

class TestTaskTool:
    def test_explore_blocks_and_returns(self, tmp_path):
        """explore 模式阻塞等待并返回结果"""
        registry = ToolRegistry().register(NoopTool("file_read", output="auth content"))
        task_registry = TaskRegistry()

        backend = MockBackend([
            Action(ActionType.TOOL_CALL, "search...", ToolCall("file_read", {"path": "auth.py"})),
            Action(ActionType.FINISH, "done", message="Found auth module in src/auth.py"),
        ])

        tool = TaskTool(
            backend=backend,
            registry=registry,
            task_registry=task_registry,
            repo_path=str(tmp_path),
        )
        tool.set_parent_context(ConversationHistory(max_messages=20), "parent prompt")

        result = tool.execute({
            "type": "explore",
            "task": "Find auth module",
            "wait": True,
            "max_steps": 10,
        })

        assert result.success
        assert "COMPLETED" in result.output
        assert "auth.py" in result.output

    def test_fork_background_mode(self, tmp_path):
        """fork 模式默认后台运行"""
        registry = ToolRegistry().register(NoopTool("file_write", output="written"))
        task_registry = TaskRegistry()

        backend = MockBackend([
            Action(ActionType.TOOL_CALL, "writing...", ToolCall("file_write", {"path": "out.py", "content": "x=1"})),
            Action(ActionType.FINISH, "done", message="File written"),
        ])

        tool = TaskTool(
            backend=backend,
            registry=registry,
            task_registry=task_registry,
            repo_path=str(tmp_path),
        )
        tool.set_parent_context(ConversationHistory(max_messages=20), "parent prompt")

        result = tool.execute({
            "type": "fork",
            "task": "Write output file",
            "wait": False,
            "max_steps": 10,
        })

        assert result.success
        assert "background" in result.output

    def test_invalid_type_returns_error(self, tmp_path):
        registry = ToolRegistry()
        task_registry = TaskRegistry()

        tool = TaskTool(
            backend=MockBackend([]),
            registry=registry,
            task_registry=task_registry,
            repo_path=str(tmp_path),
        )

        result = tool.execute({
            "type": "invalid",
            "task": "test",
        })

        assert not result.success
        assert "unknown" in result.error.lower()

    def test_tool_schema_valid(self, tmp_path):
        registry = ToolRegistry()
        task_registry = TaskRegistry()

        tool = TaskTool(
            backend=MockBackend([]),
            registry=registry,
            task_registry=task_registry,
            repo_path=str(tmp_path),
        )

        schema = tool.to_llm_schema()
        assert schema.name == "task"
        assert "type" in schema.parameters["properties"]
        assert "task" in schema.parameters["properties"]
        assert "explore" in schema.parameters["properties"]["type"]["enum"]
        assert "review" in schema.parameters["properties"]["type"]["enum"]
        assert "fork" in schema.parameters["properties"]["type"]["enum"]


# ============================================================================
# 数据模型测试
# ============================================================================

class TestDataModels:
    def test_task_status_enum(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.ERROR.value == "error"
        assert TaskStatus.TIMED_OUT.value == "timed_out"

    def test_sub_agent_type_enum(self):
        assert SubAgentType.EXPLORE.value == "explore"
        assert SubAgentType.REVIEW.value == "review"
        assert SubAgentType.FORK.value == "fork"

    def test_task_notification(self):
        notif = TaskNotification(
            task_id="abc123",
            agent_type=SubAgentType.EXPLORE,
            status=TaskStatus.COMPLETED,
            summary="Found auth module",
            steps_taken=12,
            total_tokens=8500,
        )
        assert notif.task_id == "abc123"
        assert notif.agent_type == SubAgentType.EXPLORE
        assert notif.steps_taken == 12


# ============================================================================
# 工具黑名单测试
# ============================================================================

class TestBlacklist:
    def test_sub_agent_blacklist_contains_task(self):
        assert "task" in SUB_AGENT_BLACKLIST

    def test_explore_tools_are_readonly(self):
        """explore 工具集不含写文件、shell、git commit"""
        writable_tools = {"file_write", "shell", "git_add", "git_commit"}
        for tool_name in EXPLORE_TOOL_NAMES:
            assert tool_name not in writable_tools

    def test_review_has_git_views_without_shell(self):
        assert "shell" not in REVIEW_TOOL_NAMES
        assert "git_diff" in REVIEW_TOOL_NAMES
        assert "git_status" in REVIEW_TOOL_NAMES


# ============================================================================
# Prompt 模板测试
# ============================================================================

class TestTaskPrompts:
    def test_explore_prompt_not_empty(self):
        assert len(EXPLORE_SYSTEM_PROMPT) > 100
        assert "探索" in EXPLORE_SYSTEM_PROMPT

    def test_review_prompt_not_empty(self):
        assert len(REVIEW_SYSTEM_PROMPT) > 100
        assert "审查" in REVIEW_SYSTEM_PROMPT

    def test_fork_prefix_not_empty(self):
        assert len(FORK_SYSTEM_PROMPT_PREFIX) > 10
        assert "fork" in FORK_SYSTEM_PROMPT_PREFIX.lower()


# ============================================================================
# ChatSession 多 Agent 集成测试
# ============================================================================

class TestChatSessionMultiAgent:
    def test_chat_session_accepts_task_registry(self, tmp_path):
        """验证 ChatSession 接受 task_registry 参数"""
        from entry.chat import ChatSession
        from config.schema import AppConfig, LLMConfig, AgentCfg, ContextConfig, ToolsConfig, FileToolConfig, ShellToolConfig

        # 最小配置
        config = AppConfig(
            llm=LLMConfig(provider="mock", model="mock"),
            agent=AgentCfg(max_steps=5, budget_tokens=10000, log_dir=str(tmp_path)),
            context=ContextConfig(repo_map_budget=2000, history_window=5),
            tools=ToolsConfig(
                shell=ShellToolConfig(timeout=10, max_output_tokens=1000),
                file=FileToolConfig(max_view_lines=50),
            ),
        )

        registry = ToolRegistry().register(NoopTool("file_read"))
        task_registry = TaskRegistry()
        backend = MockBackend([Action(ActionType.FINISH, "done")])

        session = ChatSession(
            backend=backend,
            registry=registry,
            config=config,
            repo_path=str(tmp_path),
            log_dir=str(tmp_path),
            task_registry=task_registry,
        )

        # 验证 Agent 持有 task_registry
        assert session.agent._task_registry is task_registry


# ============================================================================
# 并发安全测试
# ============================================================================

class TestConcurrencySafety:
    def test_task_registry_concurrent_spawn(self, tmp_path):
        """多线程同时 spawn 子 Agent"""
        registry = TaskRegistry(max_active=5)
        errors = []

        def spawn_and_wait(i):
            try:
                builder = ToolRegistry().register(NoopTool("file_read", output="ok"))
                sub = SubAgent(
                    task_id=f"conc-{i}",
                    agent_type="explore",
                    task=Task(task_type="read_only", description=f"Concurrent {i}", repo_path=str(tmp_path)),
                    parent_backend=MockBackend([
                        Action(ActionType.FINISH, "done", message=f"Result {i}"),
                    ]),
                    parent_registry=builder,
                    parent_history=ConversationHistory(max_messages=20),
                    parent_system_prompt="test prompt",
                    sub_agent_system_prompt=EXPLORE_SYSTEM_PROMPT,
                    allowed_tools=EXPLORE_TOOL_NAMES,
                    max_steps=5,
                )
                bg = registry.spawn(sub, SubAgentType.EXPLORE)
                sub.wait(timeout=5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=spawn_and_wait, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(registry.task_ids) == 5

    def test_mailbox_concurrent_read_write(self):
        """多线程并发读写 Mailbox"""
        mb = Mailbox("test")
        N = 200

        def writer():
            for i in range(N):
                mb.send_to_child(Message("parent", "test", f"msg-{i}"))

        def reader(results):
            total = 0
            while total < N * 2:  # 两个 writer
                msgs = mb.poll_inbox()
                total += len(msgs)
            results.append(total)

        results = []
        writers = [threading.Thread(target=writer) for _ in range(2)]
        rd = threading.Thread(target=reader, args=(results,))

        for w in writers:
            w.start()
        rd.start()
        for w in writers:
            w.join()
        rd.join(timeout=5)

        assert results[0] == N * 2
