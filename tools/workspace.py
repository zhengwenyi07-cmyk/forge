"""Capture staged, unstaged and new-file patches without modifying the index."""
from __future__ import annotations

import os
import subprocess


def capture_patch(repo_path: str = ".") -> str:
    def git(*args, codes=(0,)):
        proc = subprocess.run(["git", *args], cwd=repo_path, capture_output=True)
        if proc.returncode not in codes:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
        return proc.stdout

    patch = git("diff", "--binary", "HEAD", "--")
    for name in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if name:
            patch += git("diff", "--no-index", "--binary", "--", os.devnull, os.fsdecode(name), codes=(0, 1))
    return patch.decode("utf-8", errors="replace")
