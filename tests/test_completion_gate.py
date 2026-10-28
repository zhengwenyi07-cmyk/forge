from agent.core import Agent
from agent.event_log import EventLog
from agent.task import Action, ActionType, Task, ToolCall
from llm.base import MockBackend
from tools.base import ToolRegistry
from tools.control_tool import FinishTool
from tools.file_tool import FileEditTool, ApplyPatchTool
from tools.test_tool import PytestTool
from tools.runtime import LocalRuntime


def call(name, **params):
    return Action(ActionType.TOOL_CALL, "", ToolCall(name, params))


def run(repo, actions, **kwargs):
    registry = ToolRegistry().register(FileEditTool()).register(FinishTool()).register(PytestTool()).register(ApplyPatchTool())
    task = Task(description="Fix VALUE", repo_path=str(repo), max_steps=12, **kwargs)
    with EventLog.create(task, log_dir=str(repo.parent / "logs")) as log:
        result = Agent(MockBackend(actions), registry).run(task, log)
        return result, log.replay()


def test_finish_cannot_claim_existing_dirty_patch(real_repo):
    (real_repo / "app.py").write_text("VALUE = 2\n")
    result, events = run(real_repo, [call("finish", summary="done")])
    assert not result.is_success()
    assert any("Existing changes do not count" in str(e.payload) for e in events)


def test_edits_require_a_targeted_test_after_last_change(real_repo):
    p = str(real_repo / "app.py")
    actions = [
        call("file_edit", path=p, old_string="VALUE = 1", new_string="VALUE = 3"),
        call("finish", summary="premature"),
        call("test", path=str(real_repo / "test_app.py"), cwd=str(real_repo)),
        call("file_edit", path=p, old_string="VALUE = 3", new_string="VALUE = 2"),
        call("finish", summary="still premature"),
        call("test", path=str(real_repo / "test_app.py"), cwd=str(real_repo)),
        call("finish", summary="Fixed and tested"),
    ]
    result, events = run(real_repo, actions)
    assert result.is_success()
    assert result.steps_taken == 7
    assert [a["success"] for a in result.validation] == [False, True]
    assert "VALUE = 2" in result.patch
    assert sum(e.payload.get("reason") == "completion_gate" for e in events) == 2


def test_task_test_command_is_attempted_automatically(real_repo):
    import sys
    import subprocess
    command = subprocess.list2cmdline([sys.executable, "-m", "pytest", str(real_repo / "test_app.py"), "-q"])
    result, _ = run(real_repo, [call("file_edit", path=str(real_repo / "app.py"), old_string="VALUE = 1", new_string="VALUE = 2"), call("finish", summary="done")], test_cmd=command)
    assert result.is_success()
    assert len(result.validation) == 1


def test_new_files_are_included_without_staging(real_repo):
    from tools.workspace import capture_patch
    (real_repo / "new.py").write_text("answer = 42\n")
    patch = capture_patch(str(real_repo))
    assert "new.py" in patch and "+answer = 42" in patch


def test_apply_patch_checks_all_files_before_mutating(real_repo):
    runtime = LocalRuntime(repo_path=str(real_repo))
    patch = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    tool = ApplyPatchTool(runtime)
    invalid = patch + "diff --git a/missing.py b/missing.py\n--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert not tool.execute({"patch": invalid}).success
    assert (real_repo / "app.py").read_text() == "VALUE = 1\n"
    assert tool.execute({"patch": patch}).success
    assert (real_repo / "app.py").read_text() == "VALUE = 2\n"
