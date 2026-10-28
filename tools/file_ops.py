"""Filesystem operations shared verbatim by the host and Linux runtime."""
from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
from pathlib import Path

MAX_OVERWRITE_BYTES = 32_000
MAX_OVERWRITE_LINES = 500


def atomic_write(path: Path, data: bytes) -> None:
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


def write_file(path: str, content: str) -> str:
    target = Path(path)
    if target.exists():
        old = target.read_bytes()
        if len(old) > MAX_OVERWRITE_BYTES or len(old.splitlines()) > MAX_OVERWRITE_LINES:
            raise ValueError("Full overwrite blocked for large existing file; use file_edit or apply_patch.")
        if len(old.splitlines()) > 50 and len(content.splitlines()) < len(old.splitlines()) / 2:
            raise ValueError("Full overwrite would delete over half the file; use file_edit or apply_patch.")
    atomic_write(target, content.encode("utf-8"))
    return f"Written {len(content.splitlines())} lines to {path}"


def edit_file(path: str, old_string: str, new_string: str, expected_sha256: str | None = None) -> str:
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
    atomic_write(target, data)
    diff = "".join(difflib.unified_diff(normalized.splitlines(True), after.splitlines(True), fromfile=path, tofile=path))
    return f"Edited {path}. sha256={hashlib.sha256(data).hexdigest()}\n{diff[:8000]}"
