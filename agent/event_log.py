"""
agent/event_log.py

Append-only JSONL 事件日志。
整个 agent 运行过程的完整记录，支持：
- 实时写入（每条 event 立刻 flush 到磁盘）
- 确定性回放（replay 还原完整事件序列）
- 按 task_id 隔离（每次运行一个独立文件）
- 人类可读（JSONL 格式，可直接 cat / tail -f）

设计原则：
- 只增不改，写入后永不修改历史记录
- 每条写入后立即 flush，崩溃不丢最近事件
- 文件命名带 timestamp，多次运行不覆盖

调用关系（事件流转）：
  Agent.run() → log.log_task_start(task)   → [TASK_START]
              → log.log_action(...)        → [ACTION]
              → log.log_observation(...)   → [OBSERVATION]
              → log.log_reflection(...)    → [REFLECTION]
              → log.log_task_complete(...) → [TASK_COMPLETE]
              → log.log_task_failed(...)   → [TASK_FAILED]
  summarize_run(log) → 读取 log 文件，统计 actions/reflections/tool_calls 的数量

对应简历第三段：
  "EventLog 以 JSONL append-only 格式记录完整推理链，每条写入后立即 flush，
   支持 replay() 回放与 summarize_run() 统计"
"""

from __future__ import annotations

# JSON 序列化/反序列化：
#   json.dumps(event.to_dict()) → 写入 JSONL 的每一行
#   json.loads(line) → replay() / iter_events() 读取回放
import json
# File I/O 和路径操作：
#   os.makedirs() → log_dir 目录自动创建
import os
# ISO 8601 时间戳：
#   datetime.now(timezone.utc).isoformat() → Event.timestamp
#   在 config/default.yaml 用于日志文件名 {task_id}_{timestamp}.jsonl
from datetime import datetime, timezone
# Path 操作：
#   log_path = Path(log_dir) / f"{task_id}_{timestamp}.jsonl"
#   支持跨平台路径分隔符
from pathlib import Path
# Iterator 类型注解：
#   iter_events() -> Iterator[Event]（惰性读取，大文件友好）
from typing import Iterator

# 数据类引用（来自 agent/task.py）：
#   Event / EventType：写入 EventLog 的原子单位
#   Task：log_task_start 需要 task.to_dict() 做 payload
#   Action：log_action 需要 action.to_dict() 做 payload
#   Observation：log_observation 需要 observation.to_dict() 做 payload
from agent.task import Event, EventType, Task, Action, Observation


# ===========================================================================
# EventLog — append-only JSONL 事件日志
# 整个 Agent 运行过程的完整记录。每次 Agent.run() 创建一个独立的 JSONL 文件。
# 核心文件操作模式：以追加（"a"）模式打开，每条写入后立即 flush()。
#
# 文件路径格式：./logs/{task_id}_{timestamp}.jsonl
# 示例：abc12345_20250611_143022.jsonl
#
# 对应简历第三段（面试常考）：
#   "EventLog 以 JSONL append-only 格式记录完整推理链"
#   "每条写入后立即 flush，崩溃不丢最近一条数据"
#   "支持 replay() 回放与 summarize_run() 统计"
# ===========================================================================

class EventLog:
    """
    JSONL 格式的 append-only 事件日志。

    用法：
        log = EventLog.create(task, log_dir="./logs")
        log.log_task_start(task)
        log.log_action(step=1, action=action)
        log.log_observation(step=1, observation=obs)
        log.close()

    文件路径格式：
        {log_dir}/{task_id}_{timestamp}.jsonl
    """

    def __init__(self, path: Path) -> None:
        # JSONL 文件路径（完整路径，含文件名）
        self._path = path
        # 以追加写入模式打开的文件句柄。
        # encoding="utf-8"：确保中文注释/输出在 JSONL 中不会编码损坏
        self._file = open(path, "a", encoding="utf-8")  # append mode

    # ==================================================================
    # 工厂方法
    # ==================================================================

    @classmethod
    def create(cls, task: Task, log_dir: str = "./logs") -> "EventLog":
        """
        为一次新运行创建 EventLog。
        目录不存在时自动创建。

        Args:
            task:    当前运行的 Task（用 task_id 和 timestamp 命名日志文件）
            log_dir: 日志目录路径，不存在时自动创建（mkdir -p 行为）

        Returns:
            新创建的 EventLog 实例

        文件命名格式保证：
        - 两次运行即使同一 task_id，timestamp 不同也不会覆盖
        - 文件名可排序（timestamp 在前），ls -t 可直接看最新日志
        """
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{task.task_id}_{timestamp}.jsonl"
        return cls(log_path / filename)

    @classmethod
    def open_existing(cls, path: str | Path) -> "EventLog":
        """
        打开已有的 EventLog 文件（用于追加写入，如断点续跑）。
        以追加模式打开现有文件，不会覆盖已有内容。

        Args:
            path: 已有 JSONL 文件的路径

        Returns:
            已追加打开的 EventLog 实例
        """
        return cls(Path(path))

    # ==================================================================
    # 写入方法（每种 EventType 一个语义化方法）
    # 所有写入方法都调用底层的 _append()，确保统一的序列化和 flush 策略。
    # ==================================================================

    def log_task_start(self, task: Task) -> None:
        """
        任务开始。
        写入 Agent 运行的第一个 Event，记录完整的 Task 输入信息。

        Args:
            task: Task 对象，to_dict() 后包含 description / repo_path / max_steps 等
        """
        self._append(Event(
            event_type=EventType.TASK_START,
            task_id=task.task_id,
            payload={"task": task.to_dict()},
        ))

    def log_action(self, step: int, action: Action, raw_content: str = "") -> None:
        """
        Agent 的每一步决策。

        Args:
            step:        当前步数（1-indexed），对应 ReAct 循环的迭代次数
            action:      Action 对象，包含 action_type / thought / tool_call / message
            raw_content: 模型返回的完整原始文本（调试用途）。
                         包含 LLM 的完整推理链和 SDK 返回的原始响应。
                         对于流式模式，此字段可能为空或摘要。
        """
        self._append(Event(
            event_type=EventType.ACTION,
            task_id=self._current_task_id,
            payload={
                "step":        step,
                "action":      action.to_dict(),
                "raw_content": raw_content,  # 模型原始输出，含完整推理链
            },
        ))

    def log_observation(self, step: int, observation: Observation) -> None:
        """
        工具执行结果。

        Args:
            step:        当前步数（与对应的 log_action 的 step 值一致）
            observation: Observation 对象，包含 status / output / tool_name / error
        """
        self._append(Event(
            event_type=EventType.OBSERVATION,
            task_id=self._current_task_id,
            payload={
                "step":        step,
                "observation": observation.to_dict(),
            },
        ))

    def log_reflection(self, step: int, reason: str, prompt: str) -> None:
        """
        触发 Reflection 时记录。

        Args:
            step:   触发 Reflection 时的步数
            reason: 触发原因（"test_failed" / "no_edit"）
                    core.py 中通过 log.log_reflection(reason="test_failed") 等调用
            prompt: 注入 LLM 的 reflection prompt 内容。
                    用于事后分析"Agent 被注入了什么提示来引导方向调整"
        """
        self._append(Event(
            event_type=EventType.REFLECTION,
            task_id=self._current_task_id,
            payload={
                "step":   step,
                "reason": reason,
                "prompt": prompt,
            },
        ))

    def log_task_complete(self, steps: int, summary: str) -> None:
        """
        任务成功完成。

        Args:
            steps:   总步数
            summary: 完成摘要，通常来自 Action.message（LLM 的总结）
        """
        self._append(Event(
            event_type=EventType.TASK_COMPLETE,
            task_id=self._current_task_id,
            payload={
                "steps":   steps,
                "summary": summary,
            },
        ))

    def log_task_failed(self, steps: int, reason: str) -> None:
        """
        任务失败或被熔断。

        Args:
            steps:  触发失败时的步数
            reason: 失败原因，可能是：
                    - "Loop detected: same action repeated N times"
                    - "LLM error: ..."
                    - "Reached max_steps limit (N)"
                    - MockBackend 脚本用尽时的自动 GIVE_UP
        """
        self._append(Event(
            event_type=EventType.TASK_FAILED,
            task_id=self._current_task_id,
            payload={
                "steps":  steps,
                "reason": reason,
            },
        ))

    # ==================================================================
    # 读取方法
    # 从已写入的 JSONL 文件还原 Event 序列。
    # 文件关闭后（Agent.run() 结束后）仍可调用——读取重新打开文件，不影响已关闭的句柄。
    # ==================================================================

    def replay(self) -> list[Event]:
        """
        从头读取所有 event，还原完整事件序列。
        用于调试、断点续跑分析。文件关闭后仍可调用。

        Returns:
            全部 Event 的时间顺序列表（与写入顺序一致）

        注意：
        - 返回 list[Event]（一次性加载到内存），适合小到中等大小的日志文件
        - 对于超大日志，使用 iter_events() 惰性迭代避免 OOM
        """
        if not self._file.closed:
            self._file.flush()
        events: list[Event] = []
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                events.append(Event(
                    event_id=raw["event_id"],
                    event_type=EventType(raw["event_type"]),
                    task_id=raw["task_id"],
                    timestamp=raw["timestamp"],
                    payload=raw["payload"],
                ))
        return events

    def iter_events(self) -> Iterator[Event]:
        """
        惰性迭代所有 event，适合大文件。文件关闭后仍可调用。

        Yields:
            Event 对象（每次 yield 一条），不一次性加载全部到内存

        使用场景：
        - get_actions() 中调用此方法筛选 ACTION 类型的事件
        - 处理大型日志文件（如 10 万+ 行）时比 replay() 更省内存
        """
        if not self._file.closed:
            self._file.flush()
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                yield Event(
                    event_id=raw["event_id"],
                    event_type=EventType(raw["event_type"]),
                    task_id=raw["task_id"],
                    timestamp=raw["timestamp"],
                    payload=raw["payload"],
                )

    def get_actions(self) -> list[Action]:
        """
        从 event log 提取所有 Action，用于循环检测。
        （连续相同 action 时触发熔断）

        被 core.py:_is_looping() 调用（第 358 行）。
        遍历所有 event，筛选 ACTION 类型的，还原为 Action 对象列表。

        恢复逻辑：
        - 从 JSON 字典重新构造 ToolCall(name, params)
        - 从 JSON 字典重新构造 Action(action_type, thought, tool_call, message)
        """
        from agent.task import ActionType, ToolCall

        actions: list[Action] = []
        for event in self.iter_events():
            if event.event_type != EventType.ACTION:
                continue
            raw_action = event.payload["action"]
            raw_tc = raw_action.get("tool_call")
            tool_call = None
            if raw_tc:
                tool_call = ToolCall(
                    name=raw_tc["name"],
                    params=raw_tc["params"],
                )
            actions.append(Action(
                action_type=ActionType(raw_action["action_type"]),
                thought=raw_action["thought"],
                tool_call=tool_call,
                message=raw_action.get("message"),
            ))
        return actions

    # ==================================================================
    # 属性
    # ==================================================================

    @property
    def path(self) -> Path:
        """返回 JSONL 文件的完整路径（用于在 CLI 输出和调试中显示）。"""
        return self._path

    @property
    def _current_task_id(self) -> str:
        """
        从文件名中提取 task_id（8位前缀）。

        文件名格式：{task_id}_{timestamp}.jsonl
        例如：abc12345_20250611_143022.jsonl → stem = "abc12345_20250611_143022"
        提取 split("_")[0] = "abc12345"
        """
        return self._path.stem.split("_")[0]

    # ==================================================================
    # 内部方法
    # ==================================================================

    def _append(self, event: Event) -> None:
        """
        写入一条 event。
        每次写入后立即 flush，确保崩溃不丢数据。

        这是 EventLog 唯一写入入口。所有 log_*() 方法最终都调此方法。
        设计要点：
        - append-only：只追加不修改，写入后永不改动
        - immediate flush：每条写入后立即 sync 到磁盘
          （频繁 flush 有性能开销，但 Agent 的 event 频率很低——每步 2 次写入，
           所以 flush 开销可忽略不计，而数据安全性收益巨大）
        - ensure_ascii=False：保留非 ASCII 字符（如中文），不转义为 \\uXXXX 形式
        """
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        """
        显式关闭文件。通常在 Agent.run() 结束时调用。

        EventLog 也支持上下文管理器（with 语句）：
            with EventLog.create(task) as log:
                ...
            # 退出 with 块时自动 close()
        """
        if not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self) -> "EventLog":
        """支持 with 语句，进入上下文管理器时返回 self。"""
        return self

    def __exit__(self, *_) -> None:
        """退出 with 块时自动关闭文件句柄。"""
        self.close()

    def __repr__(self) -> str:
        return f"EventLog(path={self._path})"


# ===========================================================================
# 辅助：从已完成的 log 生成摘要统计
# 不在 Agent 主流程中使用（不依赖 EventLog 实例）。
# 在 CLI 命令 "agent log show LOG_FILE" 中被调用。
# 也在测试用例（test_day1.py::TestSummarizeRun）中验证其正确性。
# ===========================================================================

def summarize_run(log: EventLog) -> dict:
    """
    读取一次完整运行的 event log，返回统计摘要。
    用于 Day 7 的分析脚本，不在 agent 主流程里使用。

    Args:
        log: 已完成的 EventLog 实例（文件可能已关闭，replay() 仍可读）

    Returns:
        包含以下键的统计字典：
        - total_events:    Event 总数
        - actions:         ACTION 类型的 Event 数量（= 总步数减去 Reflection 步数）
        - reflections:     REFLECTION 类型的 Event 数量
        - tool_calls:      {tool_name: count} — 每种工具被调用了多少次
        - observations_ok:  成功的 Observation 数
        - observations_err: 失败的 Observation 数
        - final_status:    运行最终状态（"task_complete" / "task_failed"）或 None

    CLI 输出示例：
        Total events : 18
        Actions      : 6
        Reflections  : 1
        Tool calls   : {'shell': 2, 'file_read': 1, 'file_write': 1, 'test': 2}
        Final status : task_complete
    """
    events = log.replay()

    stats = {
        "total_events":    len(events),
        "actions":         0,
        "reflections":     0,
        "tool_calls":      {},   # tool_name -> count
        "observations_ok": 0,
        "observations_err": 0,
        "final_status":    None,
    }

    for event in events:
        # ACTION → 累加 actions 计数，提取 tool_call 名称
        if event.event_type == EventType.ACTION:
            stats["actions"] += 1
            tc = event.payload["action"].get("tool_call")
            if tc:
                name = tc["name"]
                stats["tool_calls"][name] = stats["tool_calls"].get(name, 0) + 1

        # OBSERVATION → 统计成功/失败次数
        elif event.event_type == EventType.OBSERVATION:
            obs = event.payload["observation"]
            if obs["status"] == "success":
                stats["observations_ok"] += 1
            else:
                stats["observations_err"] += 1

        # REFLECTION → 累加反思次数
        elif event.event_type == EventType.REFLECTION:
            stats["reflections"] += 1

        # TASK_COMPLETE / TASK_FAILED → 记录最终状态（取最后一个）
        elif event.event_type in (EventType.TASK_COMPLETE, EventType.TASK_FAILED):
            stats["final_status"] = event.event_type.value

    return stats