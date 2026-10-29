import json
from pathlib import Path

import pytest

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.run_context import BudgetExceeded, BudgetLedger
from agent.task import Action, ActionType, Task, ToolCall
from context.history import ConversationHistory
from context.token_budget import TokenBudget
from llm.base import LLMMessage, MockBackend
from multi_agent.sub_agent import SubAgent
from tools.base import NoopTool, ToolRegistry
from tools.file_tool import FileEditTool


def test_budget_reservations_and_duplicate_delivery():
    ledger = BudgetLedger(1000)
    parent = ledger.reserve("parent", 600)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("child", 500)
    ledger.settle(parent, "parent", 200)
    child = ledger.reserve("child", 500)
    ledger.settle(child, "child", 450)
    ledger.settle(child, "child", 450)
    ledger.reconcile("child", 450)
    ledger.reconcile("child", 450)
    assert ledger.total == 650


def test_failed_fork_patch_survives_worktree_cleanup(real_repo, tmp_path):
    import subprocess
    source = real_repo / "partial.py"
    source.write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "partial.py"], cwd=real_repo, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=real_repo, check=True, capture_output=True)
    backend = MockBackend([Action(ActionType.TOOL_CALL, "edit", ToolCall("file_edit", {
        "path": "partial.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}))])
    child = SubAgent("partial-task", "fork", Task("Change VALUE", str(real_repo), max_steps=1),
                     backend, ToolRegistry().register(FileEditTool()), ConversationHistory(), "parent",
                     max_steps=1, log_dir=str(tmp_path / "logs"))
    child.start()
    result = child.wait(30)
    assert result is not None and result.status.value == "max_steps"
    assert result.patch and "+VALUE = 2" in result.patch
    assert result.artifact_path and Path(result.artifact_path).is_file()
    assert json.loads(Path(result.artifact_path).read_text(encoding="utf-8"))["result"]["patch"] == result.patch
    assert not child._isolated_worktree.root.exists()
    assert source.read_text() == "VALUE = 1\n"
    from tools.file_tool import ApplyPatchTool
    from tools.runtime import LocalRuntime
    apply = ApplyPatchTool(LocalRuntime(str(real_repo)), Path(result.artifact_path).parents[1])
    applied = apply.execute({"artifact_task_id": result.task_id})
    assert applied.success, applied.error
    assert source.read_text() == "VALUE = 2\n"
    assert not apply.execute({"artifact_task_id": "../../outside"}).success
    payload = json.loads(Path(result.artifact_path).read_text(encoding="utf-8"))
    payload["result"]["patch"] += "tampered"
    Path(result.artifact_path).write_text(json.dumps(payload), encoding="utf-8")
    assert "checksum" in apply.execute({"artifact_task_id": result.task_id}).error


def test_registry_child_patch_uses_custom_log_directory(real_repo, tmp_path):
    from config.schema import AppConfig
    from entry.cli import _build_registry
    from multi_agent.task_registry import TaskRegistry
    from tools.runtime import LocalRuntime

    cfg = AppConfig()
    cfg.agent.log_dir = str(tmp_path / "custom logs")
    children = TaskRegistry()
    backend = MockBackend([Action(ActionType.TOOL_CALL, "edit", ToolCall("file_edit", {
        "path": "app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}))])
    registry = _build_registry(cfg, runtime=LocalRuntime(str(real_repo)),
                               task_registry=children, backend=backend, repo_path=str(real_repo))
    try:
        delivery = registry.execute_tool("task", {
            "type": "fork", "task": "Change VALUE", "wait": True, "max_steps": 1,
        })
        assert delivery.artifacts
        task_id = delivery.artifacts[0]["task_id"]
        manifest = Path(cfg.agent.log_dir) / "artifacts" / task_id / "result.json"
        assert manifest.is_file(), delivery
        assert (real_repo / "app.py").read_bytes() == b"VALUE = 1\n"
        children.poll()
        result = children.get(task_id).result
        assert result is not None and result.patch
        applied = registry.execute_tool("apply_patch", {"artifact_task_id": task_id})
        assert applied.success, applied.error
        assert (real_repo / "app.py").read_bytes() == b"VALUE = 2\n"
    finally:
        children.shutdown()


def test_context_budget_independent_from_run_limit(tmp_path):
    (tmp_path / "a.py").write_text("def old(): pass\n")
    backend = MockBackend([Action(ActionType.FINISH, "", message="done")])
    agent = Agent(backend, ToolRegistry(), AgentConfig(context_window=20000, repo_map_budget=123))
    task = Task("Inspect a.py", str(tmp_path), budget_tokens=500, task_type="read_only")
    with EventLog.create(task, str(tmp_path / "logs")) as log:
        result = agent.run(task, log)
    assert result.is_success()
    assert agent._repo_map.last_diagnostics["estimated_tokens"] <= 123
    assert result.total_tokens == result.own_tokens == 150


def test_native_history_remains_paired_after_trimming():
    history = ConversationHistory(max_messages=5)
    history.add(LLMMessage("user", "task"))
    for i in range(4):
        call = {"id": str(i), "type": "function", "function": {"name": "read", "arguments": "{}"}}
        history.add_many([LLMMessage("assistant", "", tool_calls=[call]), LLMMessage("tool", "large output " * 100, tool_call_id=str(i))])
    for limit in (100, 500, 2000):
        messages = TokenBudget().trim_history(history.to_dicts(), limit)
        pending = set()
        for message in messages:
            if message.get("tool_calls"):
                pending.update(c["id"] for c in message["tool_calls"])
            if message.get("tool_call_id"):
                assert message["tool_call_id"] in pending
                pending.remove(message["tool_call_id"])
        assert not pending


def test_same_agent_refreshes_map_on_new_task(tmp_path):
    source = tmp_path / "mod.py"
    source.write_text("def before(): pass\n")
    backend = MockBackend([Action(ActionType.FINISH, "", message="done")] * 2)
    agent = Agent(backend, ToolRegistry())
    for name in ("before", "after"):
        source.write_text(f"def {name}(): pass\n")
        task = Task(f"Inspect {name}", str(tmp_path), task_type="read_only")
        with EventLog.create(task, str(tmp_path / "logs")) as log:
            assert agent.run(task, log).is_success()
        assert name in agent._repo_map_cache


def test_native_tool_ids_reach_next_request_and_both_adapters(tmp_path):
    from llm.anthropic_backend import _to_anthropic_messages
    from llm.openai_compat import _to_openai_messages

    class RecordingBackend(MockBackend):
        def complete(self, messages, tools):
            if self.call_count:
                assistant = next(m for m in messages if m.tool_calls)
                tool_result = next(m for m in messages if m.tool_call_id)
                assert assistant.tool_calls[0]["id"] == tool_result.tool_call_id == "provider-call-42"
                assert _to_openai_messages([assistant, tool_result])[1]["tool_call_id"] == "provider-call-42"
                anthropic = _to_anthropic_messages([assistant, tool_result])
                assert anthropic[0]["content"][-1]["id"] == anthropic[1]["content"][0]["tool_use_id"]
            return super().complete(messages, tools)

    backend = RecordingBackend([
        Action(ActionType.TOOL_CALL, "Read", ToolCall("noop", {}, id="provider-call-42")),
        Action(ActionType.FINISH, "", message="Inspected"),
    ])
    task = Task("Inspect", str(tmp_path), task_type="read_only")
    with EventLog.create(task, str(tmp_path / "logs")) as log:
        assert Agent(backend, ToolRegistry().register(NoopTool())).run(task, log).is_success()
    assert backend.call_count == 2


def test_interrupt_preserves_patch_and_result(real_repo, tmp_path):
    class InterruptBackend(MockBackend):
        def complete(self, messages, tools):
            if self.call_count:
                raise KeyboardInterrupt()
            return super().complete(messages, tools)
    backend = InterruptBackend([Action(ActionType.TOOL_CALL, "", ToolCall("file_edit", {
        "path": str(real_repo / "app.py"), "old_string": "VALUE = 1", "new_string": "VALUE = 7"}))])
    task = Task("Edit", str(real_repo))
    with EventLog.create(task, str(tmp_path / "logs")) as log:
        result = Agent(backend, ToolRegistry().register(FileEditTool())).run(task, log)
    assert result.status.value == "gave_up" and "+VALUE = 7" in result.patch
    assert Path(result.artifact_path).is_file()


def test_native_trim_preserves_task_if_full_exchange_fits():
    messages = [{"role": "user", "content": "Important requirement " * 100},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "read", "arguments": "{}"}}]},
                {"role": "tool", "content": "result", "tool_call_id": "a"}]
    assert TokenBudget().trim_history(messages, 10000) == messages


def test_cancel_during_request_prevents_returned_edit(real_repo, tmp_path):
    import threading
    cancelled = threading.Event()
    class CancelBackend(MockBackend):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            cancelled.set()
            return response
    backend = CancelBackend([Action(ActionType.TOOL_CALL, "", ToolCall("file_edit", {
        "path": str(real_repo / "app.py"), "old_string": "VALUE = 1", "new_string": "VALUE = 9"}))])
    task = Task("Edit", str(real_repo))
    with EventLog.create(task, str(tmp_path / "logs")) as log:
        result = Agent(backend, ToolRegistry().register(FileEditTool()), AgentConfig(stop_check=cancelled.is_set)).run(task, log)
    assert not result.is_success() and result.total_tokens == 150
    assert (real_repo / "app.py").read_bytes() == b"VALUE = 1\n"


def test_parent_shutdown_accounts_child_usage(tmp_path):
    import time
    import threading
    from multi_agent.task_registry import TaskRegistry
    from tools.task_tool import TaskTool
    child_request_started = threading.Event()
    class SlowBackend(MockBackend):
        def complete(self, messages, tools):
            child_request_started.set()
            time.sleep(0.1)
            return super().complete(messages, tools)
    class ParentBackend(MockBackend):
        def complete(self, messages, tools):
            if self.call_count == 1:
                assert child_request_started.wait(5)
            return super().complete(messages, tools)
    children = TaskRegistry()
    registry = ToolRegistry()
    registry.register(TaskTool(SlowBackend([Action(ActionType.FINISH, "", message="child finding")]),
                               registry, children, str(tmp_path), log_dir=str(tmp_path / "logs")))
    parent = ParentBackend([
        Action(ActionType.TOOL_CALL, "", ToolCall("task", {"type": "explore", "task": "Inspect", "wait": False})),
        Action(ActionType.GIVE_UP, "", message="Stop parent"),
    ])
    task = Task("Analyze", str(tmp_path), task_type="read_only")
    with EventLog.create(task, str(tmp_path / "logs")) as log:
        result = Agent(parent, registry, task_registry=children).run(task, log)
    assert children.active_count() == 0 and result.usage_complete
    child = children.get(children.task_ids[0]).result
    assert result.total_tokens == result.own_tokens + child.own_tokens
    assert result.total_tokens == 450


def test_failed_artifact_save_retains_child_workspace(real_repo, tmp_path, monkeypatch):
    from agent.run_context import RunContext
    def fail_save(*args):
        raise OSError("simulated disk write failure")
    monkeypatch.setattr(RunContext, "save", fail_save)
    backend = MockBackend([Action(ActionType.TOOL_CALL, "", ToolCall("file_edit", {
        "path": "app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 8"}))])
    child = SubAgent("retain-task", "fork", Task("Edit", str(real_repo), max_steps=1), backend,
                     ToolRegistry().register(FileEditTool()), ConversationHistory(), "parent",
                     max_steps=1, log_dir=str(tmp_path / "logs"))
    child.start()
    result = child.wait(30)
    try:
        assert result.artifact_error == "simulated disk write failure"
        assert Path(result.recovery_worktree).is_dir()
        assert (Path(result.recovery_worktree) / "app.py").read_bytes() == b"VALUE = 8\n"
    finally:
        child._isolated_worktree.cleanup()
