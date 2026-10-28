"""
tools/task_tool.py

TaskTool — 父 Agent 调用来 spawn 子 Agent 的统一工具。

这是 multi-agent 子系统的唯一用户可见入口。通过 type 参数路由到
explore / review / fork 三种子 Agent 类型。

设计原则：
- 单个工具 + type 参数，不把每种子 Agent 做成独立工具
- 短任务（≤30s）阻塞等待，类似普通工具调用
- 长任务自动转后台，子完成后通知父 Agent

用法：
    tool = TaskTool(
        backend=llm_backend,
        registry=tool_registry,
        task_registry=task_registry,
        repo_path="/path/to/repo",
    )
    result = tool.execute({
        "type": "explore",
        "task": "Find the authentication module",
    })
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import Runtime
from context.history import ConversationHistory
from llm.base import LLMBackend
from tools.base import ToolRegistry as ToolReg
from agent.task import Task

from multi_agent.sub_agent import SubAgent
from multi_agent.task_registry import TaskRegistry
from multi_agent.task_prompts import (
    EXPLORE_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    FORK_SYSTEM_PROMPT_PREFIX,
)
from multi_agent.blacklist import (
    EXPLORE_TOOL_NAMES,
    REVIEW_TOOL_NAMES,
)


# 短任务最大阻塞时间（超过则转后台）
SHORT_TASK_TIMEOUT = 30  # 秒


class TaskTool(BaseTool):
    """
    Spawn 子 Agent 执行 explore / review / fork 任务。

    ┌──────────┬──────────┬──────────────┬──────────┐
    │ type     │ 工具集    │ system prompt │ 默认模式  │
    ├──────────┼──────────┼──────────────┼──────────┤
    │ explore  │ 只读探索  │ 内置探索 prompt│ wait=True │
    │ review   │ 只读+lint │ 内置审查 prompt│ wait=True │
    │ fork     │ 父-黑名单  │ 父 prompt+前缀 │ wait=False│
    └──────────┴──────────┴──────────────┴──────────┘

    params:
        type (str):      "explore" | "review" | "fork"
        task (str):      子 Agent 的任务描述
        context (str):   补充上下文（代码片段、文件路径等，可选）
        wait (bool):     是否阻塞等待完成（默认：explore/review=true, fork=false）
        max_steps (int): 最大步数（默认 20）
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolReg,
        task_registry: TaskRegistry,
        repo_path: str,
        runtime: Runtime | None = None,
        log_dir: str = "./logs",
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._task_registry = task_registry
        self._repo_path = repo_path
        self._runtime = runtime
        self._log_dir = log_dir

        # 父 Agent 上下文引用（由 Agent.run() 每步更新）
        self._parent_history: ConversationHistory | None = None
        self._parent_system_prompt: str = ""

    # ── 父 Agent 上下文注入（由 Agent.run() 在每步前调用） ──────────

    def set_parent_context(
        self,
        history: ConversationHistory,
        system_prompt: str,
    ) -> None:
        """
        更新父 Agent 上下文引用。

        每次工具执行前由 Agent.run() 调用，确保子 Agent 复制最新的父上下文。
        """
        self._parent_history = history
        self._parent_system_prompt = system_prompt

    # ── BaseTool 接口 ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return (
            "Spawn a sub-agent to independently explore code, review changes, "
            "or execute parallel work. Sub-agents run in background threads "
            "and cannot spawn their own sub-agents. "
            "Types:\n"
            "- 'explore': Read-only codebase exploration (search, read, find symbols). "
            "Blocks until complete (up to 30s).\n"
            "- 'review': Code review with read-only tools + shell for linters/tests. "
            "Blocks until complete (up to 30s).\n"
            "- 'fork': Dynamic parallel work with same tools as parent (minus blacklist). "
            "Runs in background and notifies when done."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["explore", "review", "fork"],
                    "description": "Type of sub-agent to spawn: "
                                   "'explore' for code exploration, "
                                   "'review' for code review, "
                                   "'fork' for dynamic parallel work.",
                },
                "task": {
                    "type": "string",
                    "description": "Detailed task description for the sub-agent. "
                                   "Be specific — what should it find/check/review?",
                },
                "context": {
                    "type": "string",
                    "description": "Additional context: relevant file paths, "
                                   "code snippets, search terms, etc.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Whether to block until the sub-agent completes. "
                                   "Default: true for explore/review, false for fork.",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Maximum steps for the sub-agent (default: 20).",
                },
            },
            "required": ["type", "task"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        agent_type = params["type"]
        task_desc = params["task"]
        context = params.get("context", "")
        wait = params.get("wait", agent_type in ("explore", "review"))
        max_steps = int(params.get("max_steps", 20))

        # 组装完整任务描述
        full_desc = task_desc
        if context:
            full_desc = f"{task_desc}\n\n## Additional Context\n{context}"

        # ── 按类型路由 ─────────────────────────────────────────────
        if agent_type == "explore":
            allowed_tools = EXPLORE_TOOL_NAMES
            sub_prompt = EXPLORE_SYSTEM_PROMPT
            timeout = 300
        elif agent_type == "review":
            allowed_tools = REVIEW_TOOL_NAMES
            sub_prompt = REVIEW_SYSTEM_PROMPT
            timeout = 300
        elif agent_type == "fork":
            allowed_tools = None  # 父全部工具 - 黑名单
            sub_prompt = self._build_fork_prompt()
            timeout = 600
        else:
            return ToolResult(
                success=False, output="",
                error=(
                    f"Unknown sub-agent type: '{agent_type}'. "
                    f"Use 'explore', 'review', or 'fork'."
                ),
            )

        # ── 构建 Task + SubAgent ───────────────────────────────────
        task_id = str(uuid.uuid4())[:8]
        sub_task = Task(
            task_id=task_id,
            description=full_desc,
            repo_path=self._repo_path,
            max_steps=max_steps,
            budget_tokens=40_000,
        )

        # 获取父 Agent 的当前上下文（用于子 Agent 缓存命中）
        parent_history = self._parent_history or ConversationHistory(max_messages=20)
        parent_system_prompt = self._parent_system_prompt or ""

        sub_agent = SubAgent(
            task_id=task_id,
            agent_type=agent_type,
            task=sub_task,
            parent_backend=self._backend,
            parent_registry=self._registry,
            parent_history=parent_history,
            parent_system_prompt=parent_system_prompt,
            sub_agent_system_prompt=sub_prompt,
            allowed_tools=allowed_tools,
            max_steps=max_steps,
            timeout=timeout,
            runtime=self._runtime,
            log_dir=self._log_dir,
        )

        # 注册并关联
        bg_task = self._task_registry.spawn(sub_agent, agent_type)

        # ── 阻塞模式 ───────────────────────────────────────────────
        if wait:
            result = sub_agent.wait(timeout=SHORT_TASK_TIMEOUT)
            if result is None:
                # 超过短任务阈值，转后台
                return ToolResult(
                    success=True,
                    output=(
                        f"Sub-agent [{task_id}] ({agent_type}) is still working "
                        f"in the background. It will notify you when done.\n"
                        f"Task: {task_desc[:120]}"
                    ),
                )
            return self._format_completed_result(task_id, agent_type, result)

        # ── 后台模式 ───────────────────────────────────────────────
        return ToolResult(
            success=True,
            output=(
                f"Sub-agent [{task_id}] ({agent_type}) started in background.\n"
                f"Task: {task_desc[:120]}\n"
                f"It will notify you when complete."
            ),
        )

    # ── 内部辅助 ──────────────────────────────────────────────────

    def _format_completed_result(
        self, task_id: str, agent_type: str, result
    ) -> ToolResult:
        """格式化已完成的子 Agent 结果为 ToolResult。"""
        if result.is_success():
            return ToolResult(
                success=True,
                output=(
                    f"## Sub-agent [{task_id}] ({agent_type}) — COMPLETED\n"
                    f"Steps: {result.steps_taken} | Tokens: {result.total_tokens}\n\n"
                    f"{result.summary}"
                ),
            )
        else:
            return ToolResult(
                success=False,
                output=(
                    f"## Sub-agent [{task_id}] ({agent_type}) — FAILED\n"
                    f"Status: {result.status.value}\n"
                    f"{result.summary}"
                ),
                error=result.error or result.summary,
            )

    def _build_fork_prompt(self) -> str:
        """Fork 子 Agent：在父 system prompt 前追加 fork 模式前缀。"""
        parent_prompt = self._parent_system_prompt
        if not parent_prompt:
            return FORK_SYSTEM_PROMPT_PREFIX
        return FORK_SYSTEM_PROMPT_PREFIX + "\n" + parent_prompt

    def __repr__(self) -> str:
        active = self._task_registry.active_count() if self._task_registry else 0
        return f"TaskTool(active_children={active})"
