from agent.core import Agent
from agent.event_log import EventLog
from agent.task import Action, ActionType, Task, ToolCall
from llm.base import MockBackend
from tools.base import ToolRegistry
from tools.file_tool import FileEditTool
from tools.test_tool import PytestTool


def test_invalid_test_params_cannot_satisfy_completion_gate(real_repo):
    script = [Action(ActionType.TOOL_CALL, "", ToolCall("file_edit", {"path": str(real_repo / "app.py"), "old_string": "VALUE = 1", "new_string": "VALUE = 2"})), Action(ActionType.TOOL_CALL, "", ToolCall("test", {"path": 12})), Action(ActionType.FINISH, "", message="done")]
    task = Task("fix value", str(real_repo), max_steps=5)
    registry = ToolRegistry().register(FileEditTool()).register(PytestTool())
    with EventLog.create(task, log_dir=str(real_repo.parent / "logs")) as log:
        result = Agent(MockBackend(script), registry).run(task, log)
        assert not result.is_success()
        assert any(e.payload.get("reason") == "completion_gate" for e in log.replay())


def test_cumulative_token_budget_stops_additional_calls(real_repo):
    backend = MockBackend([Action(ActionType.REFLECTION, "thinking"), Action(ActionType.FINISH, "", message="done")])
    task = Task("analyze", str(real_repo), budget_tokens=1, task_type="read_only")
    with EventLog.create(task, log_dir=str(real_repo.parent / "logs")) as log:
        result = Agent(backend, ToolRegistry()).run(task, log)
    assert not result.is_success()
    assert result.steps_taken == 1
    assert "token budget" in result.summary
