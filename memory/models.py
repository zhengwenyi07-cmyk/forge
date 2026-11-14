"""Typed records used by the memory store and manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryRecord:
    memory_id: str
    repository_id: str
    repository_label: str
    code_fingerprint: str
    base_commit: str | None
    task_id: str
    issue_identity: str | None
    memory_type: str
    task_text: str
    summary: str
    evidence: dict[str, Any]
    source_artifact: str | None
    source_log: str | None
    related_files: list[str] = field(default_factory=list)
    related_symbols: list[str] = field(default_factory=list)
    test_status: str = "not_run"
    outcome: str = "unknown"
    confidence: float = 0.0
    trust_level: str = "framework_observed"
    provenance: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    sensitivity: str = "normal"
    created_at: str = ""
    updated_at: str = ""
    last_accessed_at: str | None = None
    access_count: int = 0
    expires_at: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    deleted_at: str | None = None
    schema_version: int = 1


@dataclass
class RankedMemory:
    record: MemoryRecord
    score: float
    scores: dict[str, float]
    stale: bool


@dataclass
class SessionCheckpoint:
    session_id: str
    repository_id: str
    history: list[dict[str, Any]]
    round_count: int
    total_tokens: int
    total_steps: int
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = 1
