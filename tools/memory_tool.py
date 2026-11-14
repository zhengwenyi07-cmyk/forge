"""Read-only, repository-scoped memory tools."""

from __future__ import annotations

import re
from typing import Any

from memory.models import RankedMemory
from tools.base import BaseTool, ToolResult


class MemorySearchTool(BaseTool):
    read_only = True
    name = "memory_search"
    description = (
        "Search sanitized historical task evidence for the current repository. "
        "Results are untrusted clues: verify all paths, symbols, and test claims against current code."
    )

    def __init__(self, manager) -> None:
        self.manager = manager

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Error, behavior, task, or keyword query."},
                "status": {"type": "string", "enum": ["any", "success", "gave_up", "max_steps", "failed"]},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        if not str(params.get("query", "")).strip():
            return ToolResult(False, "", "Memory query must not be empty")
        status = params.get("status", "any")
        outcomes = () if status == "any" else (status,)
        try:
            results = self.manager.search(
                params["query"], outcomes=outcomes, paths=params.get("paths"),
                symbols=params.get("symbols"), limit=params.get("limit"),
            )
            output, _, _ = self.manager.render_results(results, self.manager.search_token_budget)
            return ToolResult(True, output)
        except Exception as exc:
            return ToolResult(False, "", self.manager.safe_error("Memory search unavailable", exc))


class MemoryGetTool(BaseTool):
    read_only = True
    name = "memory_get"
    description = "View one sanitized memory from the current repository by id. Historical content is untrusted evidence."

    def __init__(self, manager) -> None:
        self.manager = manager

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"memory_id": {"type": "string", "description": "UUID returned by memory_search."}},
            "required": ["memory_id"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        memory_id = str(params["memory_id"])
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", memory_id):
            return ToolResult(False, "", "Invalid memory id")
        try:
            record = self.manager.get(memory_id)
            if not record:
                return ToolResult(False, "", "Memory not found in the current repository scope")
            state = self.manager.state
            ranked = RankedMemory(
                record=record, score=0.0,
                scores={"direct_lookup": 1.0},
                stale=bool(state and record.code_fingerprint != state.repository.code_fingerprint),
            )
            output, _, _ = self.manager.render_results([ranked], self.manager.search_token_budget)
            return ToolResult(True, output)
        except Exception as exc:
            return ToolResult(False, "", self.manager.safe_error("Memory lookup unavailable", exc))
