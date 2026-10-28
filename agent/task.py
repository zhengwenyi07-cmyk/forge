"""
agent/task.py

整个项目的数据地基。定义 agent 运行过程中所有核心概念：
Task / ToolCall / Action / Observation / Event / RunResult

设计原则：
- 全部使用 dataclass，类型安全，IDE 友好
- 每个类都可以 asdict() 序列化为 JSON，供 EventLog 使用
- 枚举类型用 str enum，序列化后人类可读

数据流路径（从上往下读）：
  CLI / GitHub Issue ──→ Task ──→ Agent.run()
                                       │
                               ┌───────┴────────┐
                               │  LLM 返回 Action │
                               │  (TOOL_CALL /    │
                               │   FINISH /       │
                               │   GIVE_UP /      │
                               │   REFLECTION)    │
                               └───────┬────────┘
                                       │
                               ToolCall ──→ Tool.execute()
                                       │
                               Observation(status, output)
                                       │
                               RunResult(status, patch)
"""

# ---------------------------------------------------------------------------
# 导入：仅在需要时启用前向引用（PEP 604），不改变运行时行为
# ---------------------------------------------------------------------------
from __future__ import annotations

# 生成唯一 ID：task_id 和 event_id 都用 uuid4 的 8 位前缀
import uuid

# dataclass：自动生成 __init__ / __repr__ / __eq__，field 提供默认值工厂
# asdict()：递归将 dataclass 转换为 dict，供 json.dumps 序列化
from dataclasses import dataclass, field, asdict

# UTC 时间戳，所有 Event 统一使用
from datetime import datetime, timezone

# 继承 str 的枚举（str, Enum）：值本身是字符串，直接可 json.dumps
from enum import Enum

# 类型注解：运行时无开销，IDE 和 mypy/pyright 可用
from typing import Any


# ============================================================================
# 枚举部分（Enums）
# 所有枚举都继承 (str, Enum)——这意味着枚举成员可以直接当字符串用，
# json.dumps() / yaml.safe_load() 都不需要额外转换。
# ============================================================================

class EventType(str, Enum):
    """Event log 中的事件类型，str enum 序列化后直接是字符串。"""
    # 任务开始：记录 Task 详情（描述、repo 路径、max_steps 等）
    TASK_START      = "task_start"
    # Agent 做了一次决策（TOOL_CALL / FINISH / GIVE_UP / REFLECTION）
    ACTION          = "action"
    # 工具执行结果（status + output + error）
    OBSERVATION     = "observation"
    # Agent 触发了自省反思，记录了 reason + 注入的 prompt 内容
    REFLECTION      = "reflection"
    # Agent 宣布任务成功完成（含 summary）
    TASK_COMPLETE   = "task_complete"
    # Agent 任务失败或被熔断（含 reason：max_steps / loop_detected / llm_error）
    TASK_FAILED     = "task_failed"


class ActionType(str, Enum):
    """Agent 可以执行的 action 类型。"""
    # LLM 决定调用一个工具（shell / file_read / file_write / test / git_* 等）
    # 需要与 ToolRegistry 中注册的工具名匹配
    TOOL_CALL   = "tool_call"
    # 系统触发了 Reflection 自省（非 LLM 主动产生的 action，由 core.py 注入）
    REFLECTION  = "reflection"
    # Agent 判断任务已完成，主动调用 finish 并附上总结
    # 对应 RunResult.status == SUCCESS
    FINISH      = "finish"
    # Agent 判断任务超出能力范围，主动放弃
    # 对应 RunResult.status == GAVE_UP；也用于 MockBackend 脚本用尽时的安全兜底
    GIVE_UP     = "give_up"


class ObservationStatus(str, Enum):
    """工具执行结果的状态。"""
    # 工具正常执行，返回了预期输出（exit code == 0 / 文件写入成功 / 搜索完成等）
    SUCCESS = "success"
    # 工具执行失败（exit code != 0 / 文件不存在 / pytest 有失败 Case 等）
    ERROR   = "error"
    # 工具执行超时（超过 tools.shell.timeout 或 PYTEST_TIMEOUT 阈值）
    TIMEOUT = "timeout"


class RunStatus(str, Enum):
    """整次 agent 运行的最终状态。"""
    # Agent 成功完成（LLM 返回了 FINISH action，或 finish/give_up 拦截生效）
    SUCCESS     = "success"
    # Agent 因错误崩溃（LLM 调用全部重试后仍失败 / 未捕获异常）
    FAILED      = "failed"
    # 达到 step 上限自动熔断（step >= task.max_steps）
    MAX_STEPS   = "max_steps"
    # Agent 主动放弃 / 死循环检测熔断 / MockBackend 脚本用尽
    GAVE_UP     = "gave_up"


# ============================================================================
# 多 Agent 相关枚举和数据类
# 用于 multi_agent 子系统，定义子 agent 生命周期和父-子通信数据结构
# ============================================================================

class TaskStatus(str, Enum):
    """子 Agent 的生命周期状态。"""
    PENDING     = "pending"       # 已创建，尚未启动
    RUNNING     = "running"       # 正在执行中
    COMPLETED   = "completed"     # 正常完成（RunResult.status == SUCCESS）
    ERROR       = "error"         # 异常终止（抛出未捕获异常）
    TIMED_OUT   = "timed_out"     # 达到 max_steps 或超时熔断


class SubAgentType(str, Enum):
    """预定义的子 Agent 类型。"""
    EXPLORE = "explore"       # 只读代码探索
    REVIEW  = "review"        # 代码审查
    FORK    = "fork"          # 动态并行任务，复用父 agent 上下文


@dataclass
class TaskNotification:
    """
    子 Agent 完成后的通知，注入父 Agent 对话历史。

    由 TaskRegistry.drain_notifications() 产出，
    Agent._check_child_completions() 将其序列化后作为 user 消息注入。
    """
    task_id: str                    # 子 agent 的 task_id
    agent_type: SubAgentType        # explore / review / fork
    status: TaskStatus              # completed / error / timed_out
    summary: str                    # 结果摘要 / 错误信息
    steps_taken: int = 0            # 子 agent 消耗的步数
    total_tokens: int = 0           # 子 agent 消耗的 token 数
    patch: str | None = None


# ============================================================================
# Task — 输入
# 这是 Agent 运行的"起点"：用户（或 CLI / GitHub Issue 入口）构造一个 Task，
# 传给 Agent.run()，Agent 主循环据此开始工作。
# ============================================================================

@dataclass
class Task:
    """
    一次 agent 运行的输入描述。
    由 entry 层（CLI / GitHub Issue）构造，传给 Agent.run()。
    """
    # ── 必填字段 ────────────────────────────────────────────────────────
    # 任务描述，自然语言。
    # 例如："修复 tests/test_parser.py 中失败的测试"
    # 在 ChatSession 中，每轮用户输入就是一个新的 Task.description
    description: str
    # 目标 repo 的本地路径。
    # Agent 所有的文件读写、git 操作都在这个路径下执行。
    # 也用于 RepoMap 扫描的范围和 Runtime 的 bind mount
    repo_path: str

    # ── 可选字段 ────────────────────────────────────────────────────────
    # 8 位 UUID 前缀，用于 EventLog 文件名和日志追踪。
    # 默认自动生成，调用方也可以传自定义值（测试中使用固定值便于断言）
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    # GitHub issue URL。
    # 由 entry/github_issue.py 在拉取 issue 后填入，
    # build_task_prompt() 会在任务描述中追加 "## GitHub Issue\nURL: {url}" 段
    issue_url: str | None = None
    # 运行测试的命令。
    # 预留字段，当前未在主循环中使用。后续可用于 Reflection 自动运行测试
    test_cmd: str | None = None
    # 最大循环步数，超出则熔断，返回 RunResult(status=MAX_STEPS)。
    # 默认 40，可在 config/default.yaml 的 agent.max_steps 中修改
    max_steps: int = 40
    # 整次运行的 token 预算（包含 system + repo_map + history + observation + 15% 预留）。
    # 默认 80_000，对应 TokenBudget.default_plan() 的分配基准
    budget_tokens: int = 80_000
    # Explicit contract: analysis tasks can finish without a patch or tests.
    task_type: str = "modify"

    def __post_init__(self):
        if self.task_type not in ("modify", "read_only"):
            raise ValueError("task_type must be modify or read_only")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return f"Task(id={self.task_id!r}, desc={self.description[:60]!r})"


# ============================================================================
# ToolCall — Action 的载体
# 嵌套在 Action 内部，不单独出现在 EventLog 中（总是随 Action 一起出现）。
# LLMBackend 把 LLM 原始响应解析为 Action（内含 ToolCall 或终结消息）。
# ============================================================================

@dataclass
class ToolCall:
    """
    agent 决定调用某个工具时的具体参数。
    嵌套在 Action 里，不单独写入 EventLog。
    """
    # 工具名称，必须与 BaseTool.name 和 ToolRegistry 中的注册名一致。
    # 例如："shell", "file_read", "file_view", "file_write",
    #       "search_text", "find_files", "find_symbol",
    #       "test", "git_status", "git_diff", "git_add", "git_commit"
    name: str
    # 工具参数，由各 Tool 的 parameters_schema 定义 JSON Schema 约束。
    # 例如：shell → {"cmd": "pytest tests/"}, file_read → {"path": "src/main.py"}
    # ToolRegistry.execute_tool() 接收这个 dict 直接传给对应 Tool.execute(params)
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================================
# Action — Agent 的决策输出
# 这是 ReAct 循环中"LLM 返回 → Agent 执行"的关键中转站。
# 数据流：LLMBackend.complete() → LLMResponse.action → Agent.run() 识别类型 → 执行或终止。
# ============================================================================

@dataclass
class Action:
    """
    LLM 每轮输出的决策。
    由 LLMBackend 解析返回，Agent Core 拿到后执行。
    """
    # Action 类型：决定 Agent 接下来怎么做。
    # - TOOL_CALL：调用某个工具，需要看 tool_call 字段
    # - FINISH / GIVE_UP：终止循环，message 字段包含总结或原因
    # - REFLECTION：预留，当前由 core.py 代码注入而非 LLM 返回
    action_type: ActionType
    # LLM 的推理过程，调试关键，必须保留。
    # 在 Anthropic 原生 tool_use 中，thought 来自 text content block；
    # 在 OpenAI function calling 中，thought 来自 assistant message 的 content；
    # 在 DeepSeek R1 文本 fallback 中，thought 是整个原始输出。
    # MockBackend 的 preset script 中也可自定义 thought（用于断言验证）。
    thought: str
    # action_type == TOOL_CALL 时非空，包含工具名和参数。
    # action_type == FINISH / GIVE_UP 时必为 None。
    tool_call: ToolCall | None = None
    # action_type == FINISH 时：任务完成的总结，会作为 RunResult.summary 返回
    # action_type == GIVE_UP 时：放弃的原因说明
    # 也用于 finish/give_up 拦截（agent/core.py:208-221）：当 LLM 将 finish/give_up
    #   当作 function calling 目标（如 DeepSeek 的行为）时，从此字段提取总结/原因
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "action_type": self.action_type.value,
            "thought": self.thought,
            "message": self.message,
            "tool_call": self.tool_call.to_dict() if self.tool_call else None,
        }
        return d

    def is_terminal(self) -> bool:
        """是否是终止 action（不再需要执行工具）。"""
        return self.action_type in (ActionType.FINISH, ActionType.GIVE_UP)

    def __repr__(self) -> str:
        if self.tool_call:
            return f"Action({self.action_type.value}, tool={self.tool_call.name})"
        return f"Action({self.action_type.value})"


# ============================================================================
# Observation — 工具执行结果
# ToolRegistry.execute_tool() 返回 ToolResult，Agent 将其包装为 Observation，
# 写入 EventLog 并注入下一轮 LLM 调用的 history。
# ============================================================================

@dataclass
class Observation:
    """
    工具执行后返回给 agent 的结果。
    output 字段会被注入下一轮 LLM 的上下文。
    """
    # 执行结果状态。
    # SUCCESS：工具正常执行（exit code 0 / 文件写入成功 / 搜索完成等）
    # ERROR：工具执行出错（exit code != 0 / 文件不存在 / 测试断言失败等）
    # TIMEOUT：工具超时（shell 命令超过 timeout 参数 / pytest 超时）
    status: ObservationStatus
    # 工具输出的文本内容，已由工具自身做截断处理。
    # - ShellTool：保留头部 60% + 尾部 40%，最长 8000 字符
    # - FileReadTool：最多 500 行
    # - PytestTool：最长 6000 字符，失败时自动提取 FAILED 列表 + short traceback
    # 这个字符串会被 _format_observation_for_history() 格式化后注入下一轮 LLM
    output: str
    # 来自哪个工具。与 ToolCall.name 对应。
    # 用于 Reflection 判断（tc.name in test_tool_names → 测试失败触发 Reflection）
    # 也用于死循环检测的元组比对 (tool_name, params)
    tool_name: str
    # 这条 observation 消耗的 token 数（估算）。
    # 当前由 ToolResult.to_observation() 默认传 0，后续可加精确估算
    tokens_used: int = 0
    # status == ERROR 时的错误信息。
    # 内容取决于工具实现：
    # - ShellTool：exit code + 截断后的 stderr
    # - FileReadTool：文件不存在的描述
    # - PytestTool：pytest exit code
    # - ToolRegistry 未知工具：可用工具列表
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_success(self) -> bool:
        return self.status == ObservationStatus.SUCCESS

    def __repr__(self) -> str:
        return (
            f"Observation(tool={self.tool_name}, "
            f"status={self.status.value}, "
            f"len={len(self.output)})"
        )


# ============================================================================
# Event — 写入 EventLog 的统一单元
# Agent 运行过程中产生的每一条记录（任务开始 / 决策 / 工具执行结果 / 反思 /
# 任务完成或失败）都封装为 Event，通过 EventLog._append() 追加写入 JSONL 文件。
# ============================================================================

@dataclass
class Event:
    """
    EventLog 中的一条记录。
    所有信息统一封装在这里，append-only 写入 JSONL 文件。

    payload 的内容取决于 event_type：
    - TASK_START:    {"task": Task.to_dict()}
    - ACTION:        {"step": int, "action": Action.to_dict()}
    - OBSERVATION:   {"step": int, "observation": Observation.to_dict()}
    - REFLECTION:    {"step": int, "reason": str, "prompt": str}
    - TASK_COMPLETE: {"steps": int, "summary": str}
    - TASK_FAILED:   {"steps": int, "reason": str}
    """
    # 事件类型，决定 payload 的解析方式。
    # 取值：task_start / action / observation / reflection / task_complete / task_failed
    event_type: EventType
    # 所属任务的 8 位 ID（Task.task_id），一条 JSONL 文件内所有 Event 共享同一 task_id
    task_id: str
    # 具体事件内容。结构取决于 event_type（见 class docstring）。
    # - TASK_START："task" → Task.to_dict()
    # - ACTION：    "step" + "action" → Action.to_dict() + "raw_content" → LLM 原始输出
    # - OBSERVATION："step" + "observation" → Observation.to_dict()
    # - REFLECTION： "step" + "reason"（触发原因）+ "prompt"（注入的反思 prompt）
    # - TASK_COMPLETE："steps" + "summary"
    # - TASK_FAILED：  "steps" + "reason"
    payload: dict[str, Any]
    # ISO 8601 格式的 UTC 时间戳，由创建 Event 时自动生成。
    # 格式示例："2026-06-11T14:30:22.123456+00:00"
    # 用于日志排序和回放
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # 8 位 UUID 前缀，每条 Event 唯一。
    # 用于 debug 时精确引用某条事件
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id":   self.event_id,
            "event_type": self.event_type.value,
            "task_id":    self.task_id,
            "timestamp":  self.timestamp,
            "payload":    self.payload,
        }


# ============================================================================
# RunResult — 整次运行的最终结果
# Agent.run() 的最终产出。成功时包含 git diff 格式的 patch，
# 失败时包含错误原因和步数统计。
# chat 模式中 RunResult 被累积到 ChatSession.total_steps / total_tokens 做跨轮统计。
# ============================================================================

@dataclass
class RunResult:
    """
    Agent.run() 的返回值。
    包含最终状态、patch 内容（如有）、统计信息。
    """
    task_id: str
    # 最终运行状态。
    # - SUCCESS → LLM 返回了 FINISH，或 finish/give_up 拦截生效
    # - GAVE_UP → LLM 返回 GIVE_UP / 死循环检测熔断 / MockBackend 脚本用尽
    # - MAX_STEPS → step > task.max_steps
    # - FAILED → LLM 调用重试耗尽后仍失败
    status: RunStatus
    # 人类可读的结果摘要。
    # SUCCESS 时：Action.message（LLM 的总结）
    # GAVE_UP 时：Action.message 或死循环检测的 reason
    # MAX_STEPS 时：固定 "Reached max_steps limit ({N})"
    # FAILED 时：错误信息
    summary: str
    steps_taken: int
    # 整次运行消耗的 token 总数（input + output）。
    # 累加每一步 LLMResponse.total_tokens。MockBackend 返回固定的 input=100, output=50
    total_tokens: int = 0
    # git diff HEAD 格式的修改内容（仅 status == SUCCESS 时可能有内容）。
    # Agent.run() 在返回前调用 _get_git_diff() 抓取。
    # 如果 repo 中没有修改或 git 命令失败，为 None
    patch: str | None = None
    validation: list[dict[str, Any]] = field(default_factory=list)
    # status == FAILED 时的底层错误信息（如 LLM 调用的具体异常文本）
    error: str | None = None

    def is_success(self) -> bool:
        return self.status == RunStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return (
            f"RunResult(status={self.status.value}, "
            f"steps={self.steps_taken}, "
            f"tokens={self.total_tokens})"
        )
