"""Run-wide accounting, evidence and durable finalization shared by entrypoints."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class BudgetLedger:
    """Reserve concurrent requests before dispatch; settle each request exactly once."""

    def __init__(self, limit: int):
        self.limit = limit
        self._lock = threading.Lock()
        self._pending = {}
        self._calls = {}
        self._closed = False

    @property
    def total(self):
        with self._lock:
            return sum(tokens for owner, tokens in self._calls.values())

    def usage_for(self, owner):
        with self._lock:
            return sum(tokens for who, tokens in self._calls.values() if who == owner)

    def reserve(self, owner, estimate):
        with self._lock:
            if self._closed:
                raise BudgetExceeded("Run is closing; no further model requests are permitted")
            used = sum(tokens for who, tokens in self._calls.values())
            if used + sum(self._pending.values()) + estimate > self.limit:
                raise BudgetExceeded(f"Run token budget cannot accommodate next request ({used}/{self.limit}; requested {estimate})")
            key = uuid.uuid4().hex
            self._pending[key] = estimate
            return key

    def close(self):
        with self._lock:
            self._closed = True

    def snapshot(self):
        with self._lock:
            return {"limit": self.limit, "settled_tokens": sum(n for _, n in self._calls.values()),
                    "pending_requests": len(self._pending), "reserved_tokens": sum(self._pending.values()),
                    "closed": self._closed}

    def settle(self, key, owner, tokens):
        with self._lock:
            if key not in self._calls:
                self._pending.pop(key, None)
                self._calls[key] = (owner, max(0, tokens))

    def release(self, key):
        with self._lock:
            self._pending.pop(key, None)

    def reconcile(self, owner, tokens):
        """Import external child usage without counting delivered results twice."""
        with self._lock:
            recorded = sum(n for who, n in self._calls.values() if who == owner)
            if tokens > recorded:
                self._calls[f"external:{owner}:{tokens}"] = (owner, tokens - recorded)


@dataclass
class RunContext:
    task_id: str
    repo_path: str
    task_type: str
    ledger: BudgetLedger
    base_commit: str | None = None
    phase: str = "inspect"
    relevant_paths: list[str] = field(default_factory=list)
    validation: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    changed: bool = False
    child_results: dict = field(default_factory=dict)

    def observe(self, tool, params, result, step):
        paths = list(params.get("paths", [])) + ([params["path"]] if params.get("path") else [])
        if result.success and tool in ("search_text", "find_symbol"):
            paths += re.findall(r"^(.+?):\d+:", result.output, flags=re.MULTILINE)[:16]
        for path in paths:
            if path not in self.relevant_paths:
                self.relevant_paths.append(path)
        self.relevant_paths = self.relevant_paths[-16:]
        if result.success and tool in ("file_write", "file_edit", "apply_patch", "edit"):
            self.phase, self.changed = "edit", True
        elif tool == "test" and result.test_attempted:
            self.phase = "verify" if self.changed else "reproduce"
        elif tool in ("search_text", "find_files", "find_symbol", "file_view", "file_read"):
            self.phase = "localize" if self.phase == "inspect" else self.phase
        self.evidence.append({"step": step, "tool": tool, "success": result.success,
                              "paths": paths, "error": (result.error or "")[:200]})
        self.evidence = self.evidence[-12:]
        for artifact in result.artifacts:
            self.child_results[artifact["task_id"]] = artifact

    def memory(self):
        return "[VERIFIED RUN STATE]\n" + json.dumps({
            "phase": self.phase, "related_paths": self.relevant_paths,
            "recent_tool_evidence": self.evidence,
            "child_results": list(self.child_results.values())[-8:],
            "last_validation": [{"success": v["success"], "params": v.get("params"),
                                  "command": v.get("command")} for v in self.validation[-2:]],
        }, ensure_ascii=False)

    def save(self, result, log_path):
        folder = Path(log_path).resolve().parent / "artifacts" / self.task_id
        folder.mkdir(parents=True, exist_ok=True)
        result.artifact_path = str(folder / "result.json")
        payload = {"result": result.to_dict(), "base_commit": self.base_commit,
                   "repo_path": self.repo_path, "task_type": self.task_type, "event_log": str(Path(log_path).resolve()),
                   "state": json.loads(self.memory().split("\n", 1)[1]), "budget": self.ledger.snapshot()}
        if result.patch:
            (folder / "changes.patch").write_bytes(result.patch.encode("utf-8"))
            payload["patch_sha256"] = hashlib.sha256(result.patch.encode("utf-8")).hexdigest()
        temporary = folder / (uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, result.artifact_path)
