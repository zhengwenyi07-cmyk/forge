"""Bounded, workspace-scoped queries over the shared repository index."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from context.graph_rank import pagerank
from context.map_builder import words
from context.token_budget import estimate_tokens
from tools.base import BaseTool, ToolResult


class RepoQueryTool(BaseTool):
    read_only = True
    OPERATIONS = {
        "repo_search": ("PRIMARY localization tool: search indexed paths, symbols and source terms before broad file or shell search", {"query": {"type": "string"}, "limit": {"type": "integer"}, "path_prefix": {"type": "string"}}),
        "repo_symbols": ("PRIMARY symbol locator: find class, function and method definitions before reading files", {"query": {"type": "string"}, "kind": {"type": "string"}, "limit": {"type": "integer"}}),
        "repo_related_files": ("Expand confirmed candidates through import/reference neighbors after repo_search or repo_symbols", {"paths": {"type": "array", "items": {"type": "string"}}, "direction": {"type": "string", "enum": ["incoming", "outgoing", "both"]}, "limit": {"type": "integer"}}),
        "repo_tree": ("Inspect a bounded local directory tree from the persistent repository index", {"path": {"type": "string"}, "depth": {"type": "integer"}}),
        "repo_map": ("Render a token-bounded local symbol map only for selected candidate files", {"paths": {"type": "array", "items": {"type": "string"}}, "token_budget": {"type": "integer"}}),
    }

    def __init__(self, operation: str, token_budget: int = 1000, weights=None):
        if operation not in self.OPERATIONS:
            raise ValueError(operation)
        self.operation = operation
        self.token_budget = token_budget
        self.weights = weights or {"path": 3.0, "symbol": 2.0, "text": 1.0, "pagerank": 1.0}
        self.index = None
        self.queries = []
        self._seen = set()
        self.task = ""
        self.confirmed_paths = []

    @property
    def name(self):
        return self.operation

    @property
    def description(self):
        return self.OPERATIONS[self.operation][0] + "; results are bounded and report truncation"

    @property
    def parameters_schema(self):
        required = ["query"] if self.operation in ("repo_search", "repo_symbols") else ["paths"] if self.operation in ("repo_related_files", "repo_map") else []
        return {"type": "object", "properties": self.OPERATIONS[self.operation][1], "required": required}

    def _path(self, raw):
        if not isinstance(raw, str) or not raw:
            raise ValueError("Expected a repository path")
        path = (self.index.root / raw).resolve()
        if not path.is_relative_to(self.index.root):
            raise ValueError("Path escapes workspace")
        return path.relative_to(self.index.root).as_posix()

    def execute(self, params):
        if self.index is None:
            return ToolResult(False, "", "Repository index unavailable")
        try:
            if len(json.dumps(params, ensure_ascii=False)) > 1600:
                raise ValueError("Query parameters exceed 1600 characters")
            try:
                self.index.refresh()
            except OSError as exc:
                return ToolResult(False, "", f"Repository index unavailable: {exc}")
            key = json.dumps([self.operation, params], sort_keys=True)
            if key in self._seen:
                self.queries.append({"operation": self.operation, "params": params, "duplicate": True})
                return ToolResult(True, json.dumps({"duplicate": True, "message": "Query already answered; change scope or terms"}))
            self._seen.add(key)
            records = self.index.files
            limit = max(1, min(int(params.get("limit", 10)), 30))
            candidates = []
            if self.operation in ("repo_search", "repo_symbols"):
                query = words(params["query"])
                task_terms = words(self.task)
                if not query:
                    raise ValueError("Query must contain searchable terms")
                prefix = self._path(params["path_prefix"]) if params.get("path_prefix") else "."
                seeds = {}
                for path, record in records.items():
                    if prefix != "." and not (path == prefix or path.startswith(prefix + "/")):
                        continue
                    path_score = len(query & words(path)) + 0.25 * len(task_terms & words(path))
                    matching = [d for d in record.definitions if query & words(d.qualified_name) and (self.operation != "repo_symbols" or not params.get("kind") or d.kind == params["kind"])]
                    symbol_score = len(matching)
                    lexical = path_score * self.weights["path"] + symbol_score * self.weights["symbol"]
                    if self.operation == "repo_search" and not lexical:
                        lexical = self.weights["text"] * len(query & record.terms)
                    if lexical:
                        seeds[path] = lexical
                        candidates.append((path, record, path_score, symbol_score, lexical, matching))
                for path in self.confirmed_paths:
                    try:
                        confirmed = self._path(path)
                    except ValueError:
                        continue
                    if confirmed in records:
                        seeds[confirmed] = seeds.get(confirmed, 0) + 5
                ranks = pagerank(self.index.edges, seeds) if candidates else {}
                candidates.sort(key=lambda c: (-(c[4] + self.weights["pagerank"] * ranks.get(c[0], 0) * len(records)), c[0]))
                rows = [{"path": p, "score": {"path": a, "symbol": b, "lexical": c, "pagerank": round(ranks.get(p, 0), 7), "total": round(c + self.weights["pagerank"] * ranks.get(p, 0) * len(records), 4)}, "symbols": [d.qualified_name for d in matches[:3]]} for p, _, a, b, c, matches in candidates]
            elif self.operation == "repo_related_files":
                if len(params["paths"]) > 20:
                    raise ValueError("At most 20 paths are allowed")
                paths = [self._path(p) for p in params["paths"]]
                if any(p not in records for p in paths):
                    raise ValueError("Path is not indexed")
                direction = params.get("direction", "both")
                if direction not in ("incoming", "outgoing", "both"):
                    raise ValueError("Invalid direction")
                related = Counter()
                for source in paths:
                    if direction in ("outgoing", "both"):
                        related.update(self.index.edges[source])
                    if direction in ("incoming", "both"):
                        related.update({p: edges[source] for p, edges in self.index.edges.items() if source in edges})
                rows = [{"path": p, "weight": round(weight, 4)} for p, weight in sorted(related.items(), key=lambda x: (-x[1], x[0])) if p not in paths]
            elif self.operation == "repo_tree":
                prefix = self._path(params.get("path", "."))
                depth = max(0, min(int(params.get("depth", 2)), 4))
                rows = [{"path": p} for p in records if (prefix == "." or p.startswith(prefix + "/") or p == prefix) and len(Path(p).parts) - (0 if prefix == "." else len(Path(prefix).parts)) <= depth]
            else:
                if len(params["paths"]) > 20:
                    raise ValueError("At most 20 paths are allowed")
                paths = [self._path(p) for p in params["paths"]]
                if any(p not in records for p in paths):
                    raise ValueError("Path is not indexed")
                rows = [{"path": p, "symbols": [{"name": d.qualified_name, "line": d.line, "signature": d.signature} for d in records[p].definitions[:20]]} for p in paths]
                limit = 20
            budget = min(self.token_budget, int(params.get("token_budget", self.token_budget)))
            if budget < 64:
                raise ValueError("token_budget must be at least 64")
            selected = []
            for row in rows[:limit]:
                if estimate_tokens(json.dumps(selected + [row], ensure_ascii=False)) > budget - 80:
                    break
                selected.append(row)
            truncated = len(selected) < len(rows)
            event = {"operation": self.operation, "candidates": selected, "total_candidates": len(rows), "truncated": truncated, "duplicate": False}
            while selected and estimate_tokens(json.dumps(event, ensure_ascii=False)) > budget:
                selected.pop()
                event["truncated"] = True
            if estimate_tokens(json.dumps(event, ensure_ascii=False)) > budget:
                raise ValueError("Result metadata exceeds token_budget")
            self.queries.append(dict(event, params=params))
            return ToolResult(True, json.dumps(event, ensure_ascii=False))
        except (ValueError, TypeError, OSError) as exc:
            return ToolResult(False, "", str(exc))
