"""Capture staged, unstaged and new-file patches without modifying the index."""
from __future__ import annotations

import os
import subprocess


def resolve_head(repo_path: str = ".") -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, check=True)
    return result.stdout.decode("ascii").strip()


def capture_patch(repo_path: str = ".", exclude_paths=(), base_ref: str = "HEAD") -> str:
    def git(*args, codes=(0,)):
        proc = subprocess.run(["git", *args], cwd=repo_path, capture_output=True)
        if proc.returncode not in codes:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
        return proc.stdout

    pathspec = [".", *(f":(exclude,literal){p}" for p in exclude_paths)]
    patch = git("diff", "--binary", base_ref, "--", *pathspec)
    for name in git("ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec).split(b"\0"):
        if name:
            patch += git("diff", "--no-index", "--binary", "--", os.devnull, os.fsdecode(name), codes=(0, 1))
    return patch.decode("utf-8", errors="replace")
