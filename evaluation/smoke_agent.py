"""Exercise both real models against a tiny edit/test/finish task before SWE-bench."""
import json
import subprocess
import tempfile
from pathlib import Path

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Task
from config.schema import load_config
from entry.cli import _build_registry
from llm.openai_compat import OpenAICompatBackend
from tools.runtime import create_runtime


def main():
    cfg = load_config()
    output = Path("evaluation/runs/live-smoke").resolve()
    output.mkdir(parents=True, exist_ok=True)
    for model in ("mimo-v2.6-flash", "mimo-v2.6-pro"):
        with tempfile.TemporaryDirectory(prefix="smoke-", dir=output) as temp:
            repo = Path(temp)
            (repo / "app.py").write_bytes(b"def add(a, b):\n    return a - b\n")
            (repo / "test_app.py").write_bytes(b"from app import add\ndef test_add():\n    assert add(2, 3) == 5\n")
            (repo / ".gitignore").write_bytes(b"__pycache__/\n.pytest_cache/\n")
            for args in [["init"], ["config", "user.name", "Smoke"], ["config", "user.email", "smoke@example.invalid"], ["config", "core.autocrlf", "false"], ["add", "."], ["commit", "-m", "baseline"]]:
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            runtime = create_runtime(repo_path=str(repo))
            try:
                assert runtime.preflight().success
                backend = OpenAICompatBackend(model, cfg.llm.api_key, cfg.llm.base_url, max_tokens=8192)
                registry = _build_registry(cfg, runtime=runtime)
                task = Task("Fix add in app.py so it returns the sum. Run test_app.py using the test tool, then finish with a summary.", str(repo), max_steps=12)
                with EventLog.create(task, log_dir=str(output / model)) as log:
                    result = Agent(backend, registry, AgentConfig(stream=False), runtime=runtime).run(task, log)
                (output / f"{model}.json").write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
                print(model, result.status.value, "steps", result.steps_taken, "tokens", result.total_tokens, "tested", [a["success"] for a in result.validation], flush=True)
                if not result.is_success() or not result.validation[-1]["success"]:
                    raise RuntimeError("Real-model smoke did not pass; inspect the event log before benchmarking")
            finally:
                runtime.cleanup()


if __name__ == "__main__":
    main()
