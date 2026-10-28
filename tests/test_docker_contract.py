import subprocess

import pytest

from config.schema import AppConfig
from entry.cli import _build_registry
from tools.runtime import DockerRuntime
from tools.file_tool import FileViewTool, FileEditTool, FileWriteTool
from tools.search_tool import SearchTextTool, FindFilesTool, FindSymbolTool


def docker_ready():
    try:
        return subprocess.run(["docker", "image", "inspect", "forge-runtime:py311"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not docker_ready(), reason="Build forge-runtime:py311 to run Linux contract tests")


def test_docker_search_view_and_precise_edit(tmp_path):
    name = "quoted ' file.py"
    path = tmp_path / name
    path.write_bytes(b"def target():\r\n    return 1\r\n" + b"# padding\r\n" * 200)
    with DockerRuntime(str(tmp_path)) as runtime:
        assert runtime.preflight().success
        result = FileViewTool(runtime).execute({"path": name, "start_line": 2})
        assert result.success and "2 |     return 1" in result.output
        assert "102 |" not in result.output
        assert SearchTextTool(runtime).execute({"pattern": "return 1"}).success
        assert "return 1" in SearchTextTool(runtime).execute({"pattern": "return 1"}).output
        assert name in FindFilesTool(runtime).execute({"pattern": "*.py"}).output
        assert "def target" in FindSymbolTool(runtime).execute({"symbol": "target"}).output
        assert FileEditTool(runtime).execute({"path": name, "old_string": "return 1", "new_string": "return 2"}).success
        assert b"return 2\r\n" in path.read_bytes()
        path.write_bytes(b"line\n" * 501)
        assert not FileWriteTool(runtime).execute({"path": name, "content": "truncated"}).success


def test_fork_with_docker_mounts_the_correct_git_metadata(real_repo):
    from agent.task import Task, Action, ActionType, ToolCall
    from context.history import ConversationHistory
    from llm.base import MockBackend
    from multi_agent.sub_agent import SubAgent
    runtime = DockerRuntime(str(real_repo))
    registry = _build_registry(AppConfig(), runtime=runtime)
    script = [Action(ActionType.TOOL_CALL, "", ToolCall("file_edit", {"path": "app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"})), Action(ActionType.TOOL_CALL, "", ToolCall("test", {"path": "test_app.py"})), Action(ActionType.FINISH, "", message="fixed")]
    child = SubAgent("docker-fork", "fork", Task("Fix value", str(real_repo)), MockBackend(script), registry, ConversationHistory(), "parent", runtime=runtime, log_dir=str(real_repo.parent / "logs"))
    child.start()
    result = child.wait(45)
    assert result is not None and result.is_success(), child.error
    assert "+VALUE = 2" in result.patch
    assert (real_repo / "app.py").read_bytes() == b"VALUE = 1\n"
    assert result.validation[-1]["success"]
