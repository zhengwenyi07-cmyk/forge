import subprocess
from unittest.mock import patch

from click.testing import CliRunner

from agent.core import Agent
from agent.event_log import EventLog
from agent.task import Task, Action, ActionType, ToolCall, RunResult, RunStatus, SubAgentType
from config.schema import AppConfig
from context.token_budget import TokenBudget, estimate_tokens
from entry.cli import cli
from llm.base import MockBackend
from multi_agent.task_registry import TaskRegistry
from tools.base import NoopTool, ToolRegistry
from tools.file_tool import FileEditTool
from tools.git_tool import GitAddTool, GitCommitTool
from tools.runtime import LocalRuntime
from tools.test_tool import PytestTool


def call(name, **params):
    return Action(ActionType.TOOL_CALL, "", ToolCall(name, params))


def test_commit_does_not_erase_task_diff_or_test_evidence(real_repo):
    runtime = LocalRuntime(str(real_repo))
    registry = ToolRegistry().register(FileEditTool(runtime)).register(PytestTool(runtime)).register(GitAddTool(runtime)).register(GitCommitTool(runtime))
    script = [call("file_edit", path="app.py", old_string="VALUE = 1", new_string="VALUE = 2"), call("test", path="test_app.py"), call("git_add"), call("git_commit", message="Fix value"), call("finish", summary="fixed and tested")]
    task = Task("Fix and commit value", str(real_repo))
    with EventLog.create(task, log_dir=str(real_repo.parent / "logs")) as log:
        result = Agent(MockBackend(script), registry, runtime=runtime).run(task, log)
    assert result.is_success()
    assert "+VALUE = 2" in result.patch
    assert result.validation[-1]["success"]
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=real_repo) == b""


def test_test_that_mutates_code_requires_another_test(real_repo):
    runtime = LocalRuntime(str(real_repo))
    script = [call("file_edit", path="app.py", old_string="VALUE = 1", new_string="VALUE = 2"), call("test", command="python -c \"from pathlib import Path; Path('app.py').write_text('VALUE = 99')\""), call("finish", summary="premature"), call("test", path="test_app.py"), call("finish", summary="test failed")]
    registry = ToolRegistry().register(FileEditTool(runtime)).register(PytestTool(runtime))
    task = Task("Fix", str(real_repo))
    with EventLog.create(task, log_dir=str(real_repo.parent / "logs")) as log:
        result = Agent(MockBackend(script), registry, runtime=runtime).run(task, log)
        assert result.steps_taken == 5
        assert any(e.payload.get("reason") == "completion_gate" for e in log.replay())


def test_automatic_test_cannot_remove_diff_and_then_finish(real_repo):
    runtime = LocalRuntime(str(real_repo))
    task = Task("Fix", str(real_repo), test_cmd="git checkout -- app.py")
    registry = ToolRegistry().register(FileEditTool(runtime)).register(PytestTool(runtime))
    with EventLog.create(task, log_dir=str(real_repo.parent / "logs")) as log:
        result = Agent(MockBackend([call("file_edit", path="app.py", old_string="VALUE = 1", new_string="VALUE = 2"), call("finish", summary="done")]), registry, runtime=runtime).run(task, log)
    assert not result.is_success()


def test_new_child_result_must_be_seen_before_parent_finishes(tmp_path):
    class Child:
        task_id = "child"
        done = False
        result = RunResult("child", RunStatus.SUCCESS, "Essential child finding", 1)
        error = None
        def start(self): pass
        def is_done(self): return self.done
    child = Child()
    tasks = TaskRegistry()
    tasks.spawn(child, SubAgentType.EXPLORE)
    seen = []
    class Backend(MockBackend):
        def complete(self, messages, tools):
            seen.append([m.content for m in messages])
            child.done = True
            return super().complete(messages, tools)
    script = [call("finish", summary="premature"), call("finish", summary="incorporated finding")]
    task = Task("Analyze", str(tmp_path), task_type="read_only")
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = Agent(Backend(script), ToolRegistry(), task_registry=tasks).run(task, log)
    assert result.is_success() and result.steps_taken == 2
    assert not any("Essential child finding" in text for text in seen[0])
    assert any("Essential child finding" in text for text in seen[1])
    assert tasks.drain_notifications() == []


def test_repeated_bounded_waits_are_allowed(tmp_path):
    task = Task("Wait", str(tmp_path), task_type="read_only")
    tool = NoopTool("task_wait")
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = Agent(MockBackend([call("task_wait", timeout=1)] * 4 + [call("finish", summary="done")]), ToolRegistry().register(tool)).run(task, log)
    assert result.is_success() and tool.call_count == 4


def test_local_chat_injects_the_selected_repository_runtime(tmp_path):
    cfg = AppConfig()
    runtime = LocalRuntime(str(tmp_path))
    observed = {}
    class Session:
        def __init__(self, **kwargs):
            observed.update(kwargs)
        def print_stats(self): pass
    with patch("entry.cli.load_config", return_value=cfg), patch("entry.cli.create_backend_from_config", return_value=MockBackend([])), patch("tools.runtime.create_runtime", return_value=runtime), patch("entry.chat.ChatSession", Session):
        result = CliRunner().invoke(cli, ["chat", "--repo", str(tmp_path), "--no-sandbox"], input="/exit\n")
    assert result.exit_code == 0, result.output
    assert observed["runtime"] is runtime
    assert all(getattr(tool, "_runtime", runtime) is runtime for tool in observed["registry"].tools.values())


def test_latest_observation_keeps_head_tail_and_fits_budget():
    messages = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "old action"}, {"role": "user", "content": "LATEST_TOOL_RESULT\n" + "x" * 20000 + "\nFINAL_ERROR"}]
    trimmed = TokenBudget().trim_history(messages, 150)
    assert "LATEST_TOOL_RESULT" in trimmed[-1]["content"]
    assert "FINAL_ERROR" in trimmed[-1]["content"]
    assert sum(estimate_tokens(m["content"]) for m in trimmed) <= 150


def test_large_initial_task_cannot_crowd_out_latest_result():
    messages = [{"role": "user", "content": "TASK " + "x" * 20000}, {"role": "user", "content": "critical result"}]
    trimmed = TokenBudget().trim_history(messages, 100)
    assert trimmed[-1]["content"] == "critical result"
    assert sum(estimate_tokens(m["content"]) for m in trimmed) <= 100
