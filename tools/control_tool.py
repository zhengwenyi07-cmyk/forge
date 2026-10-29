"""Terminal actions advertised to models; Agent applies the completion gate."""
from tools.base import BaseTool, ToolResult


class FinishTool(BaseTool):
    read_only = True

    name = "finish"
    description = "Request completion with an accurate summary. Modified tasks require a new diff and a targeted test attempt after the last change."
    parameters_schema = {"type": "object", "properties": {"summary": {"type": "string", "minLength": 1}}, "required": ["summary"]}

    def execute(self, params):
        return ToolResult(success=True, output=params["summary"])


class GiveUpTool(BaseTool):
    read_only = True

    name = "give_up"
    description = "Stop when unable to complete the task, explaining the blocker."
    parameters_schema = {"type": "object", "properties": {"reason": {"type": "string", "minLength": 1}}, "required": ["reason"]}

    def execute(self, params):
        return ToolResult(success=True, output=params["reason"])
