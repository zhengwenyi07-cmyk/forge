from unittest.mock import MagicMock

from agent.task import RunResult, RunStatus
from config.schema import AppConfig
from llm.base import MockBackend
from tools.runtime import RunResult as RuntimeResult


def test_issue_entry_uses_linux_runtime_and_cleans_it_up(tmp_path, monkeypatch):
    import entry.github_issue as entry
    runtime = MagicMock()
    runtime.preflight.return_value = RuntimeResult(0, "ready", "")
    seen = []
    monkeypatch.setattr(entry, "fetch_issue", lambda *a: ("Fix bug", "description", "https://example.invalid/1"))
    monkeypatch.setattr(entry, "clone_repo", lambda *a: None)
    monkeypatch.setattr(entry, "create_branch", lambda *a: None)
    cfg = AppConfig()
    cfg.agent.log_dir = str(tmp_path / "logs")
    monkeypatch.setattr("config.schema.load_config", lambda *a: cfg)
    monkeypatch.setattr("llm.router.create_backend_from_config", lambda *a: MockBackend([]))
    def create(**kwargs):
        seen.append(kwargs)
        return runtime
    monkeypatch.setattr("tools.runtime.create_runtime", create)
    monkeypatch.setattr("agent.core.Agent.run", lambda *a: RunResult("x", RunStatus.GAVE_UP, "stop", 1))
    assert entry.run_on_issue("owner/repo", 1, str(tmp_path), create_pr=False) == 1
    assert seen == [{"sandbox": True, "repo_path": str(tmp_path), **cfg.runtime.options()}]
    runtime.cleanup.assert_called_once()
