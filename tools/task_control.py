"""Bounded waiting and instruction delivery for running child agents."""
from tools.base import BaseTool, ToolResult


class TaskWaitTool(BaseTool):
    name = "task_wait"
    description = "Wait up to 30 seconds for running sub-agents. Completed results and patches are injected on the next turn."
    parameters_schema = {"type": "object", "properties": {"timeout": {"type": "integer", "minimum": 1, "maximum": 30}}}

    def __init__(self, task_registry):
        self._task_registry = task_registry

    def execute(self, params):
        import time
        deadline = time.monotonic() + params.get("timeout", 30)
        while time.monotonic() < deadline:
            completed = self._task_registry.poll()
            if completed or not self._task_registry.active_count():
                break
            time.sleep(0.05)
        return ToolResult(True, f"Running sub-agents: {self._task_registry.active_count()}. Check notifications for completed results.")


class TaskMessageTool(BaseTool):
    name = "task_message"
    description = "Send an instruction to a running child agent; it reads the message before its next model call."
    parameters_schema = {"type": "object", "properties": {"task_id": {"type": "string"}, "message": {"type": "string", "minLength": 1}}, "required": ["task_id", "message"]}

    def __init__(self, task_registry):
        self._task_registry = task_registry

    def execute(self, params):
        sent = self._task_registry.send_message_to_child(params["task_id"], params["message"])
        return ToolResult(sent, "Instruction delivered." if sent else "Child not found or already completed.")
