"""Repository identity and code-fingerprint derivation."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from memory.security import safe_label


@dataclass(frozen=True)
class RepositoryIdentity:
    repository_id: str
    label: str
    base_commit: str | None
    code_fingerprint: str


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _normalize_remote(remote: str) -> str:
    value = remote.strip().replace("\\", "/")
    if "://" in value:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = f":{parts.port}" if parts.port else ""
        value = urlunsplit((parts.scheme.lower(), host + port, parts.path, "", ""))
    elif "@" in value and ":" in value:
        # git@host:owner/repo.git -> host/owner/repo.git
        value = value.split("@", 1)[1].replace(":", "/", 1)
    return value.removesuffix("/").removesuffix(".git").lower()


def identify_repository(repo_path: str | Path, *, include_worktree: bool = True) -> RepositoryIdentity:
    root = Path(repo_path).resolve()
    remote = _git(root, "config", "--get", "remote.origin.url")
    normalized = _normalize_remote(remote) if remote else root.as_posix().casefold()
    label = safe_label(normalized)
    repository_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    head = _git(root, "rev-parse", "HEAD")
    status = (_git(root, "status", "--porcelain=v1", "--untracked-files=no") or "") if include_worktree else ""
    diff = (_git(root, "diff", "--binary", "--no-ext-diff") or "") if include_worktree else ""
    material = "\0".join((head or "no-head", status, diff))
    fingerprint = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()
    return RepositoryIdentity(repository_id, label, head, fingerprint)
