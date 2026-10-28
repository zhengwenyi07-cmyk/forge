"""Standalone, bounded search implementation for Linux tool execution."""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path


def search(operation, params):
    root = Path(params.get("path", "."))
    skip = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache", "dist", "build"}
    if not root.exists():
        raise ValueError(f"Path not found: {root}")
    def iter_files():
        if root.is_file():
            yield root
            return
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in skip and not d.endswith(".egg-info"))
            for name in sorted(files):
                yield Path(current) / name
    result = []
    pattern = params.get("pattern", "*")
    matcher = re.compile(pattern, 0 if params.get("case_sensitive", True) else re.I) if operation == "search_text" else None
    symbol = re.compile(r"^(\s*)(def|class)\s+(" + re.escape(params.get("symbol", "")) + r"\w*)\s*[:(]", re.M) if operation == "find_symbol" else None
    for path in iter_files():
        if not path.is_file() or any(p in skip or p.endswith(".egg-info") for p in path.parts):
            continue
        if operation == "find_files":
            if fnmatch.fnmatch(path.name, pattern) or path.match(pattern):
                result.append(str(path))
        elif operation == "search_text" and fnmatch.fnmatch(path.name, params.get("file_pattern", "*")):
            try:
                with path.open(encoding="utf-8", errors="replace") as stream:
                    for number, line in enumerate(stream, 1):
                        if matcher.search(line):
                            result.append(f"{path}:{number}:{line.rstrip()[:200]}")
                            if len(result) == 50:
                                return "\n".join(result)
            except OSError:
                continue
        elif operation == "find_symbol" and path.suffix == ".py":
            try:
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    match = symbol.match(line)
                    if match:
                        scope = "method" if match.group(1) else "top-level"
                        result.append(f"{path}:{number}: {match.group(2)} {match.group(3)} ({scope})")
            except OSError:
                continue
        if len(result) >= 50:
            break
    return "\n".join(result[:50]) or "No matches found."
