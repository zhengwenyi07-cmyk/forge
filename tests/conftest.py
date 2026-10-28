import subprocess

import pytest


@pytest.fixture
def real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init")
    git("config", "user.name", "Tests")
    git("config", "user.email", "tests@example.invalid")
    git("config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (repo / "app.py").write_bytes(b"VALUE = 1\n")
    (repo / "test_app.py").write_text("from app import VALUE\ndef test_value():\n    assert VALUE == 2\n")
    git("add", ".")
    git("commit", "-m", "baseline")
    return repo
