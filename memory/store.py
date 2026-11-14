"""Transactional SQLite/FTS5 persistence for Forge memory."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from memory.models import MemoryRecord, SessionCheckpoint


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_memory_path() -> Path:
    override = os.environ.get("FORGE_MEMORY_PATH")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        root = Path(os.environ["XDG_STATE_HOME"])
    else:
        root = Path.home() / ".local" / "state"
    return root / "Forge" / "memory" / "memory.sqlite3"


class MemoryStore:
    """Short-lived connections plus WAL make one store safe across threads/processes."""

    def __init__(self, path: str | Path | None = None, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_memory_path()
        self.busy_timeout_ms = busy_timeout_ms
        self._init_lock = threading.Lock()
        self._initialized = False
        self.search_backend = "unknown"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    if version > SCHEMA_VERSION:
                        raise RuntimeError(f"Memory schema {version} is newer than supported {SCHEMA_VERSION}")
                    if version < 1:
                        self._migrate_v1(conn)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                try:
                    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, summary, task_text, paths, symbols)")
                    self.search_backend = "fts5"
                    memory_count = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
                    fts_count = conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
                    if memory_count != fts_count:
                        conn.execute("DELETE FROM memory_fts")
                        conn.execute(
                            """INSERT INTO memory_fts(memory_id, summary, task_text, paths, symbols)
                            SELECT memory_id, summary, task_text, related_files_json, related_symbols_json FROM memories"""
                        )
                except sqlite3.OperationalError:
                    self.search_backend = "like"
            self._initialized = True

    @staticmethod
    def _migrate_v1(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                repository_label TEXT NOT NULL,
                code_fingerprint TEXT NOT NULL,
                base_commit TEXT,
                task_id TEXT NOT NULL,
                issue_identity TEXT,
                memory_type TEXT NOT NULL,
                task_text TEXT NOT NULL,
                summary TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                source_artifact TEXT,
                source_log TEXT,
                related_files_json TEXT NOT NULL,
                related_symbols_json TEXT NOT NULL,
                test_status TEXT NOT NULL,
                outcome TEXT NOT NULL,
                confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                trust_level TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                invalidated_at TEXT,
                invalidation_reason TEXT,
                deleted_at TEXT,
                schema_version INTEGER NOT NULL,
                UNIQUE(repository_id, code_fingerprint, content_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_repo_updated ON memories(repository_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memory_repo_commit ON memories(repository_id, code_fingerprint);
            CREATE INDEX IF NOT EXISTS idx_memory_state ON memories(repository_id, invalidated_at, deleted_at, expires_at);
            CREATE TABLE IF NOT EXISTS session_checkpoints (
                session_id TEXT NOT NULL,
                repository_id TEXT NOT NULL,
                history_json TEXT NOT NULL,
                round_count INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                total_steps INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                PRIMARY KEY(session_id, repository_id)
            );
            CREATE TABLE IF NOT EXISTS memory_feedback (
                feedback_id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL REFERENCES memories(memory_id),
                verdict TEXT NOT NULL,
                reason TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"], repository_id=row["repository_id"],
            repository_label=row["repository_label"], code_fingerprint=row["code_fingerprint"],
            base_commit=row["base_commit"], task_id=row["task_id"], issue_identity=row["issue_identity"],
            memory_type=row["memory_type"], task_text=row["task_text"], summary=row["summary"],
            evidence=json.loads(row["evidence_json"]), source_artifact=row["source_artifact"],
            source_log=row["source_log"], related_files=json.loads(row["related_files_json"]),
            related_symbols=json.loads(row["related_symbols_json"]), test_status=row["test_status"],
            outcome=row["outcome"], confidence=float(row["confidence"]), trust_level=row["trust_level"],
            provenance=json.loads(row["provenance_json"]), content_hash=row["content_hash"],
            sensitivity=row["sensitivity"], created_at=row["created_at"], updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"], access_count=int(row["access_count"]),
            expires_at=row["expires_at"], invalidated_at=row["invalidated_at"],
            invalidation_reason=row["invalidation_reason"], deleted_at=row["deleted_at"],
            schema_version=int(row["schema_version"]),
        )

    def upsert_memory(self, record: MemoryRecord) -> tuple[str, bool]:
        now = utc_now()
        record.created_at = record.created_at or now
        record.updated_at = now
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT memory_id FROM memories WHERE repository_id=? AND code_fingerprint=? AND content_hash=?",
                (record.repository_id, record.code_fingerprint, record.content_hash),
            ).fetchone()
            if existing:
                memory_id = existing["memory_id"]
                conn.execute(
                    "UPDATE memories SET updated_at=?, access_count=access_count+1 WHERE memory_id=?",
                    (now, memory_id),
                )
                return memory_id, True
            values = (
                record.memory_id, record.repository_id, record.repository_label, record.code_fingerprint,
                record.base_commit, record.task_id, record.issue_identity, record.memory_type,
                record.task_text, record.summary, json.dumps(record.evidence, ensure_ascii=False),
                record.source_artifact, record.source_log, json.dumps(record.related_files, ensure_ascii=False),
                json.dumps(record.related_symbols, ensure_ascii=False), record.test_status, record.outcome,
                record.confidence, record.trust_level, json.dumps(record.provenance, ensure_ascii=False),
                record.content_hash, record.sensitivity, record.created_at, record.updated_at,
                record.last_accessed_at, record.access_count, record.expires_at, record.invalidated_at,
                record.invalidation_reason, record.deleted_at, record.schema_version,
            )
            conn.execute(
                "INSERT INTO memories VALUES (" + ",".join("?" for _ in values) + ")",
                values,
            )
            if self.search_backend == "fts5":
                conn.execute(
                    "INSERT INTO memory_fts(memory_id, summary, task_text, paths, symbols) VALUES(?,?,?,?,?)",
                    (record.memory_id, record.summary, record.task_text, " ".join(record.related_files), " ".join(record.related_symbols)),
                )
            return record.memory_id, False

    def get_memory(self, memory_id: str, repository_id: str | None = None) -> MemoryRecord | None:
        self.initialize()
        with self._connect() as conn:
            params: list[object] = [memory_id]
            sql = "SELECT * FROM memories WHERE memory_id=? AND deleted_at IS NULL"
            if repository_id:
                sql += " AND repository_id=?"
                params.append(repository_id)
            row = conn.execute(sql, params).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE memories SET last_accessed_at=?, access_count=access_count+1 WHERE memory_id=?",
                (utc_now(), memory_id),
            )
            return self._row_to_record(row)

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z0-9_.:/-]{1,63}|[\u4e00-\u9fff]{2,}", query)))[:24]

    def search_candidates(
        self, repository_id: str, query: str, *, outcomes: tuple[str, ...] = (), limit: int = 50,
    ) -> list[MemoryRecord]:
        self.initialize()
        terms = self._query_terms(query)
        now = utc_now()
        with self._connect() as conn:
            params: list[object] = [repository_id, now]
            state = "m.repository_id=? AND m.deleted_at IS NULL AND m.invalidated_at IS NULL AND (m.expires_at IS NULL OR m.expires_at>?)"
            if outcomes:
                state += " AND m.outcome IN (" + ",".join("?" for _ in outcomes) + ")"
                params.extend(outcomes)
            if terms and self.search_backend == "fts5":
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                sql = f"SELECT m.* FROM memory_fts f JOIN memories m ON m.memory_id=f.memory_id WHERE {state} AND memory_fts MATCH ? ORDER BY bm25(memory_fts), m.updated_at DESC LIMIT ?"
                params.extend((match, limit))
            elif terms:
                haystack = "lower(m.summary || ' ' || m.task_text || ' ' || m.related_files_json || ' ' || m.related_symbols_json)"
                predicate = " OR ".join(f"{haystack} LIKE ?" for _ in terms)
                sql = f"SELECT m.* FROM memories m WHERE {state} AND ({predicate}) ORDER BY m.updated_at DESC LIMIT ?"
                params.extend([f"%{term.lower()}%" for term in terms])
                params.append(limit)
            else:
                sql = f"SELECT m.* FROM memories m WHERE {state} ORDER BY m.updated_at DESC LIMIT ?"
                params.append(limit)
            return [self._row_to_record(row) for row in conn.execute(sql, params).fetchall()]

    def invalidate(self, memory_id: str, reason: str) -> bool:
        with self.transaction() as conn:
            changed = conn.execute(
                "UPDATE memories SET invalidated_at=?, invalidation_reason=?, updated_at=? WHERE memory_id=? AND invalidated_at IS NULL",
                (utc_now(), reason[:500], utc_now(), memory_id),
            ).rowcount
            return bool(changed)

    def invalidate_many(self, memory_ids: list[str], reason: str) -> int:
        ids = list(dict.fromkeys(memory_ids))
        if not ids:
            return 0
        now = utc_now()
        with self.transaction() as conn:
            before = conn.total_changes
            conn.executemany(
                "UPDATE memories SET invalidated_at=?, invalidation_reason=?, updated_at=? WHERE memory_id=? AND invalidated_at IS NULL",
                [(now, reason[:500], now, memory_id) for memory_id in ids],
            )
            return conn.total_changes - before

    def add_feedback(self, memory_id: str, verdict: str, reason: str = "", source: str = "user") -> None:
        if verdict not in {"useful", "correct", "obsolete", "forget"}:
            raise ValueError("Unknown memory feedback verdict")
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (memory_id,)).fetchone():
                raise KeyError(memory_id)
            conn.execute(
                "INSERT INTO memory_feedback VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, memory_id, verdict, reason[:1000], source, utc_now()),
            )
            if verdict == "obsolete":
                conn.execute(
                    "UPDATE memories SET invalidated_at=?, invalidation_reason='user marked obsolete', updated_at=? WHERE memory_id=?",
                    (utc_now(), utc_now(), memory_id),
                )
            elif verdict == "forget":
                conn.execute("UPDATE memories SET deleted_at=?, updated_at=? WHERE memory_id=?", (utc_now(), utc_now(), memory_id))

    def save_session(self, checkpoint: SessionCheckpoint) -> None:
        now = utc_now()
        checkpoint.created_at = checkpoint.created_at or now
        checkpoint.updated_at = now
        payload = json.dumps(checkpoint.history, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO session_checkpoints VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id, repository_id) DO UPDATE SET
                history_json=excluded.history_json, round_count=excluded.round_count,
                total_tokens=excluded.total_tokens, total_steps=excluded.total_steps,
                updated_at=excluded.updated_at, schema_version=excluded.schema_version""",
                (checkpoint.session_id, checkpoint.repository_id, payload, checkpoint.round_count,
                 checkpoint.total_tokens, checkpoint.total_steps, checkpoint.created_at,
                 checkpoint.updated_at, checkpoint.schema_version),
            )

    def load_session(self, session_id: str, repository_id: str) -> SessionCheckpoint | None:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_checkpoints WHERE session_id=? AND repository_id=?",
                (session_id, repository_id),
            ).fetchone()
        if not row:
            return None
        history = json.loads(row["history_json"])
        if not isinstance(history, list):
            raise ValueError("Invalid session checkpoint history")
        return SessionCheckpoint(
            session_id=row["session_id"], repository_id=row["repository_id"], history=history,
            round_count=int(row["round_count"]), total_tokens=int(row["total_tokens"]),
            total_steps=int(row["total_steps"]), created_at=row["created_at"], updated_at=row["updated_at"],
            schema_version=int(row["schema_version"]),
        )

    def backup(self, destination: str | Path) -> Path:
        self.initialize()
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        return target
