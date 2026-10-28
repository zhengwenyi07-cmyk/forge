"""Private-index snapshots and detached worktrees; preserve the parent's index."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


class IsolatedWorktree:
    def __init__(self, repo_path: str):
        self.parent = Path(repo_path).resolve()
        self._temp = tempfile.TemporaryDirectory(prefix="forge-child-")
        self.root = Path(self._temp.name) / "repo"
        self.base_commit = None

    def _git(self, *args, env=None, data=None):
        result = subprocess.run(["git", *args], cwd=self.parent, env=env, input=data, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout.decode("utf-8").strip()

    def create(self):
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(self._temp.name) / "index"), "GIT_AUTHOR_NAME": "Forge Agent", "GIT_AUTHOR_EMAIL": "agent@forge.dev", "GIT_COMMITTER_NAME": "Forge Agent", "GIT_COMMITTER_EMAIL": "agent@forge.dev"}
        try:
            self._git("read-tree", "HEAD", env=env)
            self._git("add", "-A", "--", ".", env=env)
            tree = self._git("write-tree", env=env)
            self.base_commit = self._git("commit-tree", tree, "-p", "HEAD", env=env, data=b"Forge isolated working tree snapshot\n")
            self._git("worktree", "add", "--detach", str(self.root), self.base_commit)
        except Exception:
            self.cleanup()
            raise
        return self

    def docker_git_mount(self, workdir="/workspace"):
        common = Path(self._git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        gitdir = (self.root / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
        relative = Path(gitdir).relative_to(common).as_posix()
        return [(str(common), "/forge-git")], {"GIT_DIR": f"/forge-git/{relative}", "GIT_WORK_TREE": workdir}

    def cleanup(self):
        expected = (Path(self._temp.name) / "repo").absolute()
        if self.root.absolute() != expected or not expected.is_relative_to(Path(self._temp.name).absolute()):
            raise RuntimeError("Refusing cleanup outside the allocated child workspace")
        if self.root.exists():
            result = subprocess.run(["git", "worktree", "remove", "--force", str(self.root)], cwd=self.parent, capture_output=True)
            if result.returncode:
                raise RuntimeError("Worktree retained for recovery: " + result.stderr.decode("utf-8", errors="replace"))
        self._temp.cleanup()
