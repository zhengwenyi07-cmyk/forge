"""Filesystem operations shared verbatim by the host and Linux runtime."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

MAX_OVERWRITE_BYTES = 32_000
MAX_OVERWRITE_LINES = 500


def read_window(path: str, start_line: int, window_size: int) -> str:
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"File not found or not a regular file: {path}")
    selected = []
    total = 0
    remaining = 20_000
    with target.open(encoding="utf-8", errors="replace") as stream:
        for total, line in enumerate(stream, 1):
            if start_line <= total < start_line + window_size:
                text = line.rstrip("\r\n")
                limit = min(remaining, 4_000)
                selected.append(text if len(text) <= limit else text[:limit] + " [line truncated]")
                remaining = max(0, remaining - min(len(text), limit))
    if start_line > max(1, total):
        raise ValueError(f"start_line {start_line} exceeds {total} lines")
    return json.dumps({"total": total, "lines": selected}, ensure_ascii=True)


def atomic_write(path: Path, data: bytes, workspace_root: str | None = None) -> None:
    # Match read semantics: edit the referent, preserving the symlink itself.
    path = path.resolve()
    if workspace_root and not path.is_relative_to(Path(workspace_root).resolve()):
        raise ValueError("Write target resolves outside the task workspace")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, temp = tempfile.mkstemp(prefix=".forge-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_file(path: str, content: str, workspace_root: str | None = None) -> str:
    target = Path(path)
    if target.exists():
        old = target.read_bytes()
        if len(old) > MAX_OVERWRITE_BYTES or len(old.splitlines()) > MAX_OVERWRITE_LINES:
            raise ValueError("Full overwrite blocked for large existing file; use file_edit or apply_patch.")
        if len(old.splitlines()) > 50 and len(content.splitlines()) < len(old.splitlines()) / 2:
            raise ValueError("Full overwrite would delete over half the file; use file_edit or apply_patch.")
    atomic_write(target, content.encode("utf-8"), workspace_root)
    return f"Written {len(content.splitlines())} lines to {path}"


def edit_file(path: str, old_string: str, new_string: str, expected_sha256: str | None = None,
              workspace_root: str | None = None) -> str:
    if not old_string:
        raise ValueError("old_string must not be empty")
    target = Path(path)
    before_bytes = target.read_bytes()
    if b"\x00" in before_bytes:
        raise ValueError("Binary files cannot be edited")
    if expected_sha256 and hashlib.sha256(before_bytes).hexdigest() != expected_sha256:
        raise ValueError("Stale content: expected_sha256 does not match")
    before = before_bytes.decode("utf-8")
    # Match normalized LF text but preserve the original file's CRLF convention.
    newline = "\r\n" if b"\r\n" in before_bytes else "\n"
    normalized = before.replace("\r\n", "\n")
    old_string = old_string.replace("\r\n", "\n")
    new_string = new_string.replace("\r\n", "\n")
    count = normalized.count(old_string)
    if count != 1:
        raise ValueError(f"Expected exactly one match for old_string; found {count}. No changes made.")
    after = normalized.replace(old_string, new_string, 1)
    if after == normalized:
        raise ValueError("Edit makes no change")
    data = after.replace("\n", newline).encode("utf-8")
    if target.read_bytes() != before_bytes:
        raise ValueError("File changed while preparing edit; retry after reading it")
    atomic_write(target, data, workspace_root)
    diff = "".join(difflib.unified_diff(normalized.splitlines(True), after.splitlines(True), fromfile=path, tofile=path))
    return f"Edited {path}. sha256={hashlib.sha256(data).hexdigest()}\n{diff[:8000]}"
