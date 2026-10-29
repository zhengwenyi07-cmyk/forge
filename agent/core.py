"""
agent/core.py

ReAct 主循环。整个 agent 的大脑。

职责（只做这些，不做别的）：
- 维护对话历史，每轮组装 messages 调用 LLM
- 拿到 Action 后调用 ToolRegistry 执行
- 把 Action + Observation 写入 EventLog
- 检测三种终止/Reflection 触发条件
- 返回 RunResult

不负责：
- 任何 LLM 细节（交给 LLMBackend）
- 任何工具实现（交给 Tool）
- 上下文压缩（由 context/ 模块负责）

调用关系（入口 → Agent）：
  entry/cli.py / entry/chat.py / entry/github_issue.py
    → Agent(backend, registry, config)       ← 构建 Agent
      → Agent.run(task, log)                 ← 执行一次完整运行
        → _build_messages()                  ← 组装 LLM messages
        → _call_with_retry()                 ← LLM 调用（含重试）
        → _is_looping()                      ← 死循环检测
        → execute_tool()                     ← 执行工具
        → _format_*_for_history()            ← 结果回写
"""

from __future__ import annotations

# json：_format_action_for_history 中将参数字典序列化为 JSON 字符串
import json
# logging：每个 step 输出 LLM 调用、Action 类型、Reflection 触发等关键事件
import logging
import subprocess
# dataclass：AgentConfig 用 dataclass 承载配置，自动生成 __init__/__repr__
from dataclasses import dataclass

# EventLog：JSONL append-only 事件日志，记录每次运行的全部事件链
from agent.event_log import EventLog
# ConversationHistory：带首条保留的滑动窗口历史管理器
from context.history import ConversationHistory
# RepoMap：tree-sitter 仓库地图，注入 system prompt 提供全局代码结构
from context.repo_map import RepoMap
from agent.run_context import BudgetExceeded
# TokenBudget：按优先级分配各部分的 token 配额，超出时裁剪
from context.token_budget import TokenBudget
# Prompt 模板函数：
#   build_system_prompt → 渲染 system prompt（含 repo_summary 占位符）
#   build_task_prompt → 渲染首条 user 消息（任务描述 + issue URL）
#   reflection_no_edit / reflection_test_failed → Reflection 引导 prompt
from agent.prompt import (
    build_system_prompt,
    build_task_prompt,
    reflection_no_edit,
    reflection_test_failed,
)
# 所有数据模型：
#   Action / ActionType → LLM 决策输出
#   Observation / ObservationStatus → 工具执行结果
#   RunResult / RunStatus → 整次运行最终状态
#   Task / ToolCall → 任务输入 / 工具调用参数
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, ToolCall,
    TaskNotification,
)
# LLMBackend → 抽象基类（依赖倒置：Agent 不依赖具体实现）
# LLMMessage → 统一消息格式（system/user/assistant）
# LLMToolSchema → 工具 JSON Schema 描述
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
# ToolRegistry → 工具注册中心，按名称查找并执行已注册的工具
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    # ── 循环控制 ────────────────────────────────────────────────────────
    # 每轮最大步数，超出则熔断（对应 resume "三种自动熔断策略" 之一）
    max_steps: int = 40
    # 连续 N 步无文件写操作触发 Reflection（对应 resume "双重 Reflection" 之 B）
    reflection_no_edit_steps: int = 6
    # 连续 N 步相同 (tool, params) 元组判定死循环（对应 resume "死循环检测"）
    loop_detection_window: int = 3
    # 触发 Reflection 的工具名列表（这些工具返回 error 时注入反思 prompt）
    test_tool_names: tuple[str, ...] = ("test", "pytest")
    # ── 预算与历史 ─────────────────────────────────────────────────────
    # 整次运行的累计 token 上限；context_window 单独约束每次请求
    budget_tokens: int = 80_000
    context_window: int = 32_768
    max_output_tokens: int = 8192
    repo_map_budget: int = 4000
    repo_map_strategy: str = "personalized"
    budget_ledger: object = None
    # 历史最大消息条数（第一层裁剪：ConversationHistory 滑动窗口）
    history_max_messages: int = 40
    # ── 重试 ────────────────────────────────────────────────────────────
    # LLM 调用失败最大重试次数（不重试 401/403/400 等客户端错误）
    llm_max_retries: int = 3
    # 重试间隔（秒，指数退避：2s → 4s → 8s）
    llm_retry_delay: float = 2.0
    # ── 流式输出 ────────────────────────────────────────────────────────
    # 是否启用流式输出（True → backend.stream() 而非 complete()）
    stream: bool = False
    # 最终回答的流式回调（on_text 参数）
    stream_callback: object = None
    # 推理过程的流式回调（on_thought 参数，Chaude thinking/DeepSeek R1 专用）
    thought_callback: object = None
    # ── 安全确认 ────────────────────────────────────────────────────────
    # 是否对危险命令要求用户确认
    confirm_dangerous: bool = False
    # 确认回调（None=直接执行，terminal_confirm=等待用户 y/n，always_deny=拒绝）
    confirm_callback: object = None
    # ── 中断信号 ────────────────────────────────────────────────────────
    # 可选的停止检查回调：每步循环开始时调用，返回 True 时 Agent 优雅终止。
    # SubAgent 使用此回调接收父线程的中断信号。
    stop_check: object = None  # Callable[[], bool] | None
    message_source: object = None
    system_prompt_override: str | None = None



# ===========================================================================
# Agent — ReAct 主循环
# 最核心的类，遵循"依赖倒置"原则——只依赖抽象，不依赖具体实现。
# 两个依赖：
#   LLMBackend  → 生产用 Anthropic/OpenAI，测试用 MockBackend
#   ToolRegistry → 管理 12 个工具，新增工具无需改此文件
# ===========================================================================

class Agent:
    """
    ReAct 主循环实现。

    用法：
        agent = Agent(backend, registry, config)
        result = agent.run(task, log)

    生命周期：
        单次模式（CLI run）：每调用一次 run() 创建一个新 Agent 实例。
        多轮模式（CLI chat）：一个 Agent 实例跨轮复用，通过 _pending_history
                              注入共享 history 实现跨轮对话历史保留。
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
        task_registry: "TaskRegistry | None" = None,
        runtime=None,
    ) -> None:
        # LLM 后端实例（策略模式的具体策略，测试/生产可替换）
        self._backend = backend
        # 工具注册表（注册了 12 个 BaseTool 子类实例）
        self._registry = registry
        self._base_registry = registry
        # 多 Agent 任务注册中心（None = 单 Agent 模式，向后兼容）
        self._task_registry = task_registry
        self._runtime = runtime
        # 运行时配置（熔断/重试/流式/确认参数）
        self._cfg = config or AgentConfig()

    # ==================================================================
    # 公开接口 — run(task, log) → RunResult
    # 这是 Agent 唯一的对外接口。调用方构造 Task 和 EventLog 后传入。
    # ChatSession 模式通过 _pending_history 注入跨轮历史。
    # ==================================================================

    def run(self, task: Task, log: EventLog) -> RunResult:
        from agent.run_context import BudgetLedger, RunContext
        self._registry = self._base_registry.for_task(task.task_type)
        ledger = self._cfg.budget_ledger or BudgetLedger(task.budget_tokens)
        self.context = RunContext(task.task_id, task.repo_path, task.task_type, ledger)
        self._base_commit = None
        self._patch_error = None
        if "task" in self._registry and hasattr(self._registry.tools["task"], "set_run_context"):
            self._registry.tools["task"].set_run_context(self.context, self._cfg)
        try:
            result = self._run(task, log)
        except KeyboardInterrupt:
            result = RunResult(task.task_id, RunStatus.GAVE_UP, "Interrupted by user; partial artifacts preserved.", len(log.get_actions()))
            log.log_task_failed(steps=result.steps_taken, reason=result.summary)
        except Exception as exc:
            logger.exception("Run failed; preserving workspace evidence")
            result = RunResult(task.task_id, RunStatus.FAILED, str(exc), 0, error=str(exc))
        if self._cfg.budget_ledger is None:
            ledger.close()
            if self._task_registry is not None:
                try:
                    self._task_registry.shutdown()
                    self._check_child_completions(ConversationHistory(), log)
                except Exception as exc:
                    result.artifact_error = f"Child shutdown/finalization failed: {exc}"
            result.usage_complete = ledger.snapshot()["pending_requests"] == 0
            if not result.usage_complete:
                result.summary += "\nUsage is provisional: model requests are still settling in child agents."
        result.base_commit = getattr(self, "_base_commit", None)
        self.context.base_commit = result.base_commit
        if task.task_type == "modify":
            result.patch = result.patch or self._get_git_diff(task.repo_path)
        result.validation = list(self.context.validation)
        result.own_tokens = ledger.usage_for(task.task_id)
        result.total_tokens = result.own_tokens if self._cfg.budget_ledger else ledger.total
        if result.validation:
            result.validation_status = result.validation[-1].get("status") or ("passed" if result.validation[-1]["success"] else "failed")
        if task.task_type == "modify" and self._patch_error:
            result.artifact_error = self._patch_error
        if task.task_type == "read_only":
            result.patch = None
        try:
            self.context.save(result, log.path)
        except (OSError, ValueError, RuntimeError) as exc:
            result.artifact_error = str(exc)
        return result

    def _run(self, task: Task, log: EventLog) -> RunResult:
        """
        执行一次完整的 agent 运行（ReAct 主循环）。

        Args:
            task: 任务描述（含 repo_path / description / max_steps / budget_tokens / issue_url）
            log:  已初始化的 EventLog（每次运行一个独立 JSONL 文件）

        Returns:
            RunResult，包含最终状态、步数、token 消耗、git diff patch
        """
        # ── 初始化阶段 ────────────────────────────────────────────────
        # 记录 repo_path，供 _build_messages（system prompt 中的 repo 路径）
        # 和 _get_git_diff（相对任务开始时的固定提交）使用
        self._current_repo_path = task.repo_path
        self._current_task_type = task.task_type
        self._current_task_description = task.description
        from pathlib import Path
        self._patch_excludes = []
        try:
            log_path = Path(log.path).resolve()
            log_relative = log_path.relative_to(Path(task.repo_path).resolve())
            self._patch_excludes = [log_relative.parent.as_posix() if log_relative.parent != Path(".") else log_relative.as_posix()]
            self._patch_excludes.append((log_relative.parent / "artifacts").as_posix())
        except ValueError:
            pass
        self._base_commit = self._resolve_base_commit(task.repo_path)
        initial_patch = self._get_git_diff(task.repo_path) if task.task_type == "modify" else None
        tested_patch = None
        test_attempts = self.context.validation
        # 按 repo_path 隔离 repo_map 缓存，换 repo 时自动重建。
        # _repo_map_cache_key 与当前 task.repo_path 比较，
        # 不一致时删除缓存，下次 _build_messages 时重新构建。
        cache_key = task.repo_path
        if getattr(self, "_repo_map_cache_key", None) != cache_key:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache
            self._repo_map_cache_key = cache_key
        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)

        # 初始化对话历史：
        # - ChatSession 模式：_pending_history 非空 → 复用共享历史（跨轮续接）
        # - 单次 run 模式：创建新 ConversationHistory，任务描述作为首条 user 消息
        if hasattr(self, "_pending_history") and self._pending_history is not None:
            history = self._pending_history
        else:
            history = ConversationHistory(max_messages=self._cfg.history_max_messages)
            from agent.prompt import build_task_prompt
            history.add(LLMMessage(
                role="user",
                content=build_task_prompt(task.description, self._runtime._workdir if self._runtime and self._runtime.name.startswith("docker") else task.repo_path, task.issue_url),
            ))
        # 单次上下文窗口只负责消息裁剪；累计开销由共享 ledger 处理。
        token_budget = TokenBudget(total=self._cfg.context_window)
        # RepoMap：扫描 repo 提取符号，生成摘要字符串。
        # 文件内容、任务、近期路径或预算改变时更新选择。
        if not hasattr(self, "_repo_map") or str(self._repo_map._root) != str(Path(task.repo_path).resolve()):
            self._repo_map = RepoMap(task.repo_path)
        repo_map = self._repo_map
        repo_map.index.excluded_paths = set(self._patch_excludes)
        for name in ("find_symbol", "find_files", "search_text"):
            tool = self._registry.tools.get(name)
            if tool is not None:
                tool._repo_index = repo_map.index

        # ── ReAct 主循环计数器 ──────────────────────────────────────
        # 总 token 消耗（累加每次 LLMResponse.total_tokens）
        total_tokens = 0
        # 连续无文件编辑步数（触发 Reflection B：steps_without_edit >= 6）
        steps_without_edit = 0

        # ═══════════════════════════════════════════════════════════════════
        # ReAct 循环体（Observe → Think → Act）
        # for 循环上限为 task.max_steps（默认 40），超出返回 MAX_STEPS。
        # 每轮迭代：组装消息 → LLM 推理 → 解析 Action → 执行/终止
        # ═══════════════════════════════════════════════════════════════════
        for step in range(1, task.max_steps + 1):
            logger.debug("Step %d/%d", step, task.max_steps)
            if self.context.ledger.total >= self.context.ledger.limit or total_tokens >= task.budget_tokens:
                reason = f"Run token budget exhausted ({task.budget_tokens})"
                log.log_task_failed(steps=step - 1, reason=reason)
                return RunResult(task.task_id, RunStatus.GAVE_UP, reason, step - 1, total_tokens)

            # ── 阶段 -1：检查外部中断信号 ──────────────────────────
            # SubAgent 通过 stop_check 回调传入停止信号。
            # 返回 True 时 Agent 立即保存当前状态并优雅退出。
            if self._cfg.stop_check is not None:
                try:
                    if self._cfg.stop_check():
                        reason = "Stop signal received from parent."
                        logger.info(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens=total_tokens,
                        )
                except Exception:
                    pass  # 回调异常不应影响 Agent 正常运行

            # ── 阶段 0：检查子 Agent 完成状态 ──────────────────────
            # 每步开始时检查是否有后台子 Agent 已完成，
            # 如果有，将完成通知注入当前 history
            self._check_child_completions(history, log)
            if self._cfg.message_source is not None:
                for message in self._cfg.message_source():
                    text = f"[PARENT MESSAGE]\n{message.content}"
                    history.add(LLMMessage(role="user", content=text))
                    log.log_reflection(step=step, reason="parent_message", prompt=text)

            # ── 阶段 1：上下文组装 ────────────────────────────────────
            # _build_messages = system prompt（含 repo-map 摘要）+ 裁剪后的 history
            # _registry.get_schemas = 12 个工具的 JSON Schema
            messages = self._build_messages(history, token_budget, repo_map)
            tools = self._registry.get_schemas()

            # ── 阶段 1b：更新 TaskTool 父上下文 ───────────────────────
            # 将最新的 system prompt + history 同步到 TaskTool，
            # 确保后续 spawn 的子 Agent 能复制最新的父上下文
            self._update_task_tool_context(history)

            # ── 阶段 2：LLM 推理（带重试） ──────────────────────────
            try:
                response = self._call_with_retry(messages, tools)
            except BudgetExceeded as exc:
                log.log_task_failed(steps=step - 1, reason=str(exc))
                return RunResult(task.task_id, RunStatus.GAVE_UP, str(exc), step - 1)
            except Exception as exc:
                logger.error("LLM call failed at step %d after retries: %s", step, exc)
                log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.FAILED,
                    summary=f"LLM call failed: {exc}",
                    steps_taken=step,
                    total_tokens=total_tokens,
                    error=str(exc),
                )

            total_tokens += response.total_tokens
            action = response.action
            if self._cfg.stop_check and self._cfg.stop_check():
                reason = "Stop signal received during model request; returned action was not executed."
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(task.task_id, RunStatus.GAVE_UP, reason, step, total_tokens)

            # ── 阶段 2b：Action 写入 EventLog ───────────────────────
            # 记录 LLM 的完整原文（raw_content：含 thought + tool_call/message）
            log.log_action(step=step, action=action, raw_content=response.raw_content)
            logger.info("Step %d: %r", step, action)

            if action.action_type == ActionType.TOOL_CALL and action.tool_call:
                tc = action.tool_call
                if tc.name in ("finish", "give_up"):
                    key = "summary" if tc.name == "finish" else "reason"
                    if not isinstance(tc.params, dict) or not isinstance(tc.params.get(key), str) or not tc.params[key].strip():
                        history.add(LLMMessage(role="user", content=f"{tc.name} requires a nonempty {key}."))
                        continue
                    action = Action(ActionType.FINISH if tc.name == "finish" else ActionType.GIVE_UP, action.thought, message=tc.params[key])

            # ── 环节 A：死循环检测（第一种熔断） ─────────────────────
            # 比较最近 N 条 Action 的 (tool_name, params) 元组。
            # 元组完全一致 → 死循环 → 返回 GAVE_UP。
            # 对应 resume "结合连续相同 Action 死循环检测实现三种自动熔断策略"
            if self._is_looping(log):
                reason = f"Loop detected: same action repeated {self._cfg.loop_detection_window} times"
                logger.warning(reason)
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                )

            # ── 环节 B：终止 Action 判断 ─────────────────────────────
            # LLM 正常返回 FINISH → 提取 git diff patch → 返回 SUCCESS
            # 对应简历"替代黑盒式 AgentExecutor"——终止逻辑显式在循环中判断
            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
                blockers = []
                if self._task_registry is not None:
                    delivered = self._check_child_completions(history, log)
                    if delivered:
                        blockers.append("New child results arrived during your last action. Read the notifications and decide again before finishing.")
                    if self._task_registry.active_count():
                        blockers.append("Child agents are still running; wait for their results before finishing.")
                if task.task_type == "modify":
                    if not patch or patch == initial_patch:
                        blockers.append("Modification tasks require a new, nonempty diff. Existing changes do not count.")
                    if patch and tested_patch != patch and task.test_cmd:
                        before_test = patch
                        result = self._registry.execute_tool("test", {"command": task.test_cmd, "cwd": task.repo_path})
                        log.log_observation(step=step, observation=result.to_observation("test"))
                        if result.test_attempted:
                            patch = self._get_git_diff(task.repo_path)
                            tested_patch = patch if patch == before_test else None
                            test_attempts.append({"command": task.test_cmd, "success": result.success, "status": result.validation_status, "output": result.output, "error": result.error})
                            if not patch or patch == initial_patch:
                                blockers.append("The test changed or removed the task diff; review the changes and validate again.")
                    if not test_attempts or tested_patch != patch:
                        blockers.append("Attempt a targeted test using the test tool after the last change. Supply path or command, then call finish again.")
                if blockers:
                    text = "[COMPLETION BLOCKED]\n" + "\n".join(blockers)
                    log.log_reflection(step=step, reason="completion_gate", prompt=text)
                    history.add(LLMMessage(role="assistant", content=self._format_action_for_history(action)))
                    history.add(LLMMessage(role="user", content=text))
                    continue
                if test_attempts and not test_attempts[-1]["success"]:
                    summary += "\nValidation: targeted test was attempted but did not pass; see validation results."
                log.log_task_complete(steps=step, summary=summary)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.SUCCESS,
                    summary=summary,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    patch=patch,
                    validation=test_attempts,
                )

            # LLM 主动放弃 → 记录失败原因 → 返回 GAVE_UP
            if action.action_type == ActionType.GIVE_UP:
                reason = action.message or "Agent gave up."
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                )

            # ── 环节 C：TOOL_CALL — 执行工具 + 回写 + Reflection ──
            if action.action_type == ActionType.TOOL_CALL and action.tool_call:
                tc = action.tool_call
                before_test = self._get_git_diff(task.repo_path) if tc.name == "test" else None

                # [拦截] 部分模型（DeepSeek 等）把 "call finish" 当作 function calling
                # 目标，返回 tool_name="finish" 而非 finish_reason="stop"。
                # 此处拦截并转换为正确的终端 Action。
                # ── 阶段 3：Tool Calling ──
                # 通过 ToolRegistry 查找并执行工具。
                # 工具不存在时返回 error ToolResult（不抛异常），Agent 继续运行。
                # ToolResult → Observation：包装为 Agent 标准格式
                result = self._registry.execute_tool(tc.name, tc.params)
                self.context.observe(tc.name, tc.params, result, step)
                observation = result.to_observation(tc.name)
                if tc.name == "test" and result.test_attempted and (tc.params.get("path") or tc.params.get("command")):
                    after_test = self._get_git_diff(task.repo_path)
                    tested_patch = after_test if after_test == before_test else None
                    test_attempts.append({"params": tc.params, "success": result.success, "status": result.validation_status, "output": result.output, "error": result.error})

                # 追踪文件编辑步数：
                # file_write/file_edit/edit → 重置计数器
                # 其他工具（shell/read/search/git/test）→ 累加
                if result.success and tc.name in ("file_write", "file_edit", "apply_patch", "edit"):
                    steps_without_edit = 0
                else:
                    steps_without_edit += 1

                # 写入 Observation event 到 EventLog
                log.log_observation(step=step, observation=observation)

                # ── 阶段 4：结果回写 ──
                # Action 格式化为 assistant 消息（thought + tool_call params）
                # Observation 格式化为 user 消息（tool name + status + output）
                # 下一轮 LLM 调用时看到 history：上一步的 (Action, Observation) 对
                self._append_tool_history(history, action, observation)

                # ── 环节 D：Reflection 自省判断 ────────────────────
                # 对应 resume "双重 Reflection 自省机制"。
                # 不是暴力重试，而是注入引导性元认知 prompt。

                # 触发条件 A：测试失败 → 注入 REFLECTION_TEST_FAILED
                # 引导 LLM：(1) 读错误信息 (2) 反思最近编辑 (3) 确认是否需要更多上下文
                if (
                    tc.name in self._cfg.test_tool_names
                    and not observation.is_success()
                ):
                    reflect_prompt = reflection_test_failed()
                    log.log_reflection(
                        step=step,
                        reason="test_failed",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: test_failed at step %d", step)

                # 触发条件 B：连续 N 步无编辑 → 注入 REFLECTION_NO_EDIT
                # 防止无尽探索：追问"有足够上下文了吗？有→动手改；没有→精确说出缺什么"
                elif task.task_type == "modify" and self.context.phase in ("edit", "verify") and steps_without_edit >= self._cfg.reflection_no_edit_steps:
                    reflect_prompt = reflection_no_edit(steps_without_edit)
                    log.log_reflection(
                        step=step,
                        reason="no_edit",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    steps_without_edit = 0  # 重置计数，避免每步都触发
                    logger.debug("Reflection triggered: no_edit at step %d", step)

            # REFLECTION action type（预留）：LLM 主动反思。
            # 当前所有 Reflection 都由 core.py 代码注入（不是 LLM 主动返回的）
            elif action.action_type == ActionType.REFLECTION:
                history.add(LLMMessage(
                    role="assistant",
                    content=action.thought,
                ))
                history.add(LLMMessage(role="user", content="Continue with a tool call, or explicitly call finish/give_up. Plain text does not finish a task."))

        # ── 环节 E：步数上限熔断（第三种熔断策略） ──────────────────
        # for 循环自然结束（step > task.max_steps）→ 返回 MAX_STEPS。
        # 这是最基础的硬截断——前两种 Reflection 没触发时仍保证系统不无限循环。
        reason = f"Reached max_steps limit ({task.max_steps})"
        log.log_task_failed(steps=task.max_steps, reason=reason)
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.MAX_STEPS,
            summary=reason,
            steps_taken=task.max_steps,
            total_tokens=total_tokens,
        )

    # ==================================================================
    # 内部辅助方法
    # 以下方法由 run() 在 ReAct 循环各阶段调用，不对外暴露。
    # ==================================================================

    def _check_child_completions(
        self, history: ConversationHistory, log: EventLog
    ) -> int:
        """
        检查 TaskRegistry 中是否有新完成的子 Agent。

        每步 ReAct 循环开始时调用。发现已完成的子 Agent 后：
        1. 更新 TaskRegistry 中的任务状态
        2. 提取完成通知
        3. 以 user 消息格式注入父 Agent 对话历史

        父 Agent 在下一步 LLM 调用时就会看到这个通知。
        """
        if self._task_registry is None:
            return 0

        # 更新完成状态
        self._task_registry.poll()

        # 收取待处理通知
        notifications = self._task_registry.drain_notifications()
        if not notifications:
            return 0

        for notif in notifications:
            self.context.ledger.reconcile(notif.task_id, notif.total_tokens)
            self.context.child_results[notif.task_id] = {"task_id": notif.task_id, "status": notif.status.value,
                "base_commit": notif.base_commit, "artifact_path": notif.artifact_path, "summary": notif.summary[:500],
                "artifact_error": notif.artifact_error, "recovery_worktree": notif.recovery_worktree}
            status_icon = "SUCCESS" if notif.status.value == "completed" else "FAILED"
            notification_text = (
                f"[SUB-AGENT NOTIFICATION]\n"
                f"Task ID: {notif.task_id}\n"
                f"Type: {notif.agent_type.value}\n"
                f"Status: {status_icon}\n"
                f"Steps: {notif.steps_taken} | Tokens: {notif.total_tokens}\n"
                f"Summary: {notif.summary}\nBase: {notif.base_commit}\nArtifacts: {notif.artifact_path}\n"
                f"Artifact error: {notif.artifact_error}\nRecovery worktree: {notif.recovery_worktree}\n"
                + (f"Child patch preview (review validation; apply_patch artifact_task_id={notif.task_id}):\n{notif.patch[:8000]}\n" if notif.patch else "")
            )
            history.add(LLMMessage(role="user", content=notification_text))
            logger.info(
                "Sub-agent %s (%s) completed: %s",
                notif.task_id,
                notif.agent_type.value,
                notif.status.value,
            )
        return len(notifications)

    def _update_task_tool_context(self, history: ConversationHistory) -> None:
        """
        将当前 system prompt 和 history 同步到 TaskTool。

        使用 _build_messages 中缓存的 system prompt，避免重复生成。
        确保子 Agent 创建时复制的是父 Agent 最新的上下文。
        """
        if self._task_registry is None:
            return

        if "task" in self._registry:
            task_tool = self._registry._tools["task"]
            if hasattr(task_tool, "set_parent_context"):
                system_prompt = getattr(self, "_cached_system_prompt", "")
                task_tool.set_parent_context(history, system_prompt)

    def _build_messages(
        self,
        history: ConversationHistory,
        token_budget: TokenBudget,
        repo_map: RepoMap,
    ) -> list[LLMMessage]:
        """
        组装发给 LLM 的完整 messages，含 token 裁剪。

        这是 ReAct + Repo-map 融合的关键点（对应简历第二段）。
        组装后的 messages 结构：
          [0] system prompt（含工具列表 + repo-map 摘要）
          [1..N] 裁剪后的对话历史（首条任务描述保留 + 最近 N 条消息）
        """
        schemas = self._registry.get_schemas()

        # 缓存选择依赖索引版本、任务、近期路径、预算及算法。
        paths = self.context.relevant_paths if hasattr(self, "context") else []
        task_description = getattr(self, "_current_task_description", "")
        map_budget = min(self._cfg.repo_map_budget, self._cfg.context_window // 4)
        selection_key = repo_map.cache_key(task_description, paths, map_budget, self._cfg.repo_map_strategy)
        if not hasattr(self, "_repo_map_cache") or selection_key != getattr(self, "_repo_map_selection_key", None):
            self._repo_map_cache = repo_map.build(
                budget=map_budget, task=task_description, mentioned_paths=paths,
                strategy=self._cfg.repo_map_strategy,
            )
            self._repo_map_selection_key = selection_key

        # 渲染 system prompt：将 repo-map 摘要注入 {repo_summary} 占位符
        system_content = build_system_prompt(
            repo_path=self._runtime._workdir if self._runtime and self._runtime.name.startswith("docker") else getattr(self, "_current_repo_path", "."),
            tools=schemas,
            repo_summary=self._repo_map_cache,
        )
        if self._cfg.system_prompt_override:
            system_content = self._cfg.system_prompt_override + "\n\n" + system_content
        if getattr(self, "_current_task_type", "modify") == "read_only":
            system_content += "\nThis is a read-only analysis task. Report findings with finish; a diff and test attempt are not required."
        # 缓存 system prompt（供 _update_task_tool_context 复用，避免重复生成）
        self._cached_system_prompt = system_content
        self._cached_repo_summary = self._repo_map_cache

        # 第二层裁剪（Token 维度）：从最新到最旧贪婪选择，保留首条，丢弃插入提示。
        # 第一层已由 ConversationHistory 滑动窗口完成（条数维度）。
        from context.token_budget import estimate_tokens
        memory = self.context.memory() if hasattr(self, "context") else ""
        output_limit = getattr(self._backend, "_max_tokens", self._cfg.max_output_tokens)
        fixed_cost = estimate_tokens(system_content) + estimate_tokens(memory) + estimate_tokens(json.dumps([vars(s) for s in schemas])) + output_limit + 512
        history_limit = self._cfg.context_window - fixed_cost
        if history_limit < 128:
            raise ValueError("Context window is too small for instructions, tool schemas and output reserve; lower repo_map_budget/max_tokens or increase context_window")
        trimmed_history_dicts = token_budget.trim_history(history.to_dicts(), history_limit)

        # 组装完整 messages：system 放在第 0 位（永不裁剪）→ 裁剪后的 history
        messages = [LLMMessage(role="system", content=system_content)]
        for d in trimmed_history_dicts:
            messages.append(LLMMessage(**d))
        if memory:
            messages.append(LLMMessage(role="user", content=memory))
        return messages

    def _append_tool_history(self, history, action, observation):
        if self._backend.supports_function_calling:
            call = action.tool_call
            history.add_many([
                LLMMessage(role="assistant", content=action.thought or "", tool_calls=[{
                    "id": call.id, "type": "function", "function": {"name": call.name,
                    "arguments": json.dumps(call.params, ensure_ascii=False)}}]),
                LLMMessage(role="tool", content=self._format_observation_for_history(observation), tool_call_id=call.id),
            ])
        else:
            history.add_many([LLMMessage(role="assistant", content=self._format_action_for_history(action)),
                              LLMMessage(role="user", content=self._format_observation_for_history(observation))])

    def _format_action_for_history(self, action: Action) -> str:
        """
        把 Action 格式化为 assistant 消息，写入对话历史。
        下一轮 LLM 会看到："上次我做了什么决策"。
        """
        parts = [f"Thought: {action.thought}"]
        if action.tool_call:
            parts.append(f"Action: {action.tool_call.name}")
            parts.append(f"Params: {json.dumps(action.tool_call.params, ensure_ascii=False)}")
        elif action.message:
            parts.append(f"Message: {action.message}")
        return "\n".join(parts)

    def _format_observation_for_history(self, observation: Observation) -> str:
        """
        把 Observation 格式化为 user 消息，写入对话历史。
        下一轮 LLM 会看到："上次我调用工具后，看到了什么结果"。
        """
        status = "SUCCESS" if observation.is_success() else "ERROR"
        lines = [f"[Tool: {observation.tool_name} | {status}]"]
        if observation.output:
            lines.append(observation.output)
        if observation.error and not observation.is_success():
            lines.append(f"Error: {observation.error}")
        return "\n".join(lines)

    def _is_looping(self, log: EventLog) -> bool:
        """
        检测是否陷入死循环：最近 N 条 action 完全相同。
        比较 (tool_name, params) 元组（非字符串比较，区分"同工具不同参数"和"完全重复"）。

        对应简历"连续相同 Action 死循环检测（滑动窗口比对 tool+params 元组）"。
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        if len(actions) < n:
            return False

        recent = actions[-n:]
        # 只对 TOOL_CALL 类型做检测（FINISH/GIVE_UP 无需检测）
        if not all(a.action_type == ActionType.TOOL_CALL for a in recent):
            return False
        if not all(a.tool_call for a in recent):
            return False

        first = recent[0].tool_call
        # Waiting is bounded by task steps/tokens and may legitimately repeat
        # while a child is computing. It is not evidence of a tool-call loop.
        if first.name == "task_wait":
            return False
        return all(
            a.tool_call.name == first.name and a.tool_call.params == first.params
            for a in recent[1:]
        )

    def _call_with_retry(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ):
        """
        带指数退避重试的 LLM 调用。
        stream=True → 走 backend.stream()（边收 token 边实时打印）
        stream=False → 走 backend.complete()（一次性返回）
        不重试：401/403（认证失败）、400（参数错误）——这些重试也没用。
        """
        import time
        from context.token_budget import estimate_tokens
        ledger = self.context.ledger if hasattr(self, "context") else None
        estimated = estimate_tokens(json.dumps([m.to_dict() for m in messages], ensure_ascii=False)) + estimate_tokens(json.dumps([vars(t) for t in tools]))
        output_limit = getattr(self._backend, "_max_tokens", self._cfg.max_output_tokens)
        if hasattr(self._backend, "_input_tokens"):
            estimated = self._backend._input_tokens
            output_limit = self._backend._output_tokens
        delay = self._cfg.llm_retry_delay
        for attempt in range(1, self._cfg.llm_max_retries + 1):
            if self._cfg.stop_check and self._cfg.stop_check():
                raise BudgetExceeded("Run cancelled before model request")
            reservation = ledger.reserve(self.context.task_id, estimated + output_limit) if ledger else None
            try:
                if self._cfg.stream:
                    response = self._backend.stream(messages, tools, on_text=self._cfg.stream_callback,
                                                    on_thought=self._cfg.thought_callback)
                else:
                    response = self._backend.complete(messages, tools)
                if ledger:
                    ledger.settle(reservation, self.context.task_id, response.total_tokens)
                return response
            except BudgetExceeded:
                raise
            except Exception as exc:
                if attempt == self._cfg.llm_max_retries or any(k in str(exc).lower() for k in ("401", "403", "authentication", "invalid api key", "400", "bad request")):
                    raise
                logger.warning("LLM request failed; retry %s: %s", attempt, exc)
                if not self._cfg.stop_check:
                    time.sleep(delay)
                else:
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        if self._cfg.stop_check():
                            raise BudgetExceeded("Run cancelled during retry")
                        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                delay *= 2
            finally:
                if ledger:
                    ledger.release(reservation)

    def _resolve_base_commit(self, repo_path: str) -> str | None:
        from tools import workspace
        from pathlib import Path
        try:
            if self._runtime is None:
                return workspace.resolve_head(repo_path)
            script = Path(workspace.__file__).read_text(encoding="utf-8") + "\nprint(resolve_head())\n"
            result = self._runtime.exec_python(script, cwd=repo_path, timeout=10)
            return result.stdout.strip() if result.success else None
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None

    def _get_git_diff(self, repo_path: str) -> str | None:
        """
        抓取相对任务开始提交的 diff，包含运行中已提交的改动。
        FINISH action 处理时调用（第 179 行、210 行）。
        RunResult.patch 字段包含此 diff，调用方（如 GitHub Issue 入口）可将其作为 PR 代码变更。
        """
        from pathlib import Path
        from tools import workspace
        try:
            if self._runtime is None:
                diff = workspace.capture_patch(repo_path, getattr(self, "_patch_excludes", []), getattr(self, "_base_commit", None) or "HEAD")
            else:
                # JSON escapes CR/LF inside hunks so host text-mode pipes cannot
                # translate CRLF into CRCRLF and corrupt the durable patch.
                script = Path(workspace.__file__).read_text(encoding="utf-8") + f"\nimport json\nprint(json.dumps(capture_patch(exclude_paths={getattr(self, '_patch_excludes', [])!r}, base_ref={getattr(self, '_base_commit', None) or 'HEAD'!r}), ensure_ascii=True))\n"
                result = self._runtime.exec_python(script, cwd=repo_path, timeout=30)
                if not result.success:
                    self._patch_error = result.stderr or "Could not capture workspace patch"
                    return None
                diff = json.loads(result.stdout)
            self._patch_error = None
            return diff or None
        except Exception as exc:
            self._patch_error = str(exc)
            return None
