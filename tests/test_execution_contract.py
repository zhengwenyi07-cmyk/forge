import subprocess
import sys

from config.schema import AppConfig
from entry.cli import _build_registry
from tools.file_tool import FileReadTool, FileWriteTool
from tools.git_tool import GitCommitTool
from tools.runtime import DockerRuntime, LocalRuntime, RunResult
from tools.shell_tool import _is_readonly


def test_large_unicode_payload_uses_stdin(tmp_path):
    runtime = LocalRuntime(str(tmp_path))
    text = "中文 $() ; ' \"\n" * 10000
    assert FileWriteTool(runtime).execute({"path": "large.txt", "content": text}).success
    assert (tmp_path / "large.txt").read_text(encoding="utf-8") == text


def test_git_message_is_literal(real_repo):
    (real_repo / "change.txt").write_text("change")
    subprocess.run(["git", "add", "--", "change.txt"], cwd=real_repo, check=True)
    message = "Fix $(touch audit-marker.txt); `echo literal`"
    result = GitCommitTool(LocalRuntime(str(real_repo))).execute({"message": message})
    assert result.success, result
    actual = subprocess.check_output(["git", "log", "-1", "--format=%s"], cwd=real_repo).decode().strip()
    assert actual == message
    assert not (real_repo / "audit-marker.txt").exists()


def test_read_only_enforced_at_registry(tmp_path):
    registry = _build_registry(AppConfig(), runtime=LocalRuntime(str(tmp_path))).for_task("read_only")
    for name, params in [("file_write", {"path": "marker", "content": "x"}),
                         ("shell", {"cmd": "echo x > marker"}),
                         ("test", {"command": "echo x > marker"})]:
        result = registry.execute_tool(name, params)
        assert not result.success
        assert "read-only" in result.error
    assert not (tmp_path / "marker").exists()
    assert "file_write" not in [s.name for s in registry.get_schemas()]


def test_confirmation_covers_test_and_git(tmp_path):
    calls = []
    registry = _build_registry(AppConfig(), confirm_callback=lambda command: calls.append(command) or False,
                               runtime=LocalRuntime(str(tmp_path)))
    assert not registry.execute_tool("test", {"command": "echo x > marker"}).success
    assert not registry.execute_tool("git_commit", {"message": "audit"}).success
    assert len(calls) == 2
    assert not (tmp_path / "marker").exists()


def test_compound_shell_does_not_inherit_read_only_prefix():
    for command in ["echo harmless && git commit -m audit", "echo x >> marker", "echo $(touch marker)",
                    "python -c 'open(\"marker\",\"w\")'", "find . -delete", "git branch renamed",
                    "sort a.txt -o a.txt", "git diff --output=marker", "env python write.py"]:
        assert not _is_readonly(command)


def test_docker_payload_is_not_in_commandline(tmp_path, monkeypatch):
    runtime = DockerRuntime(tmp_path)
    runtime._container_id = "recording-container"
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    monkeypatch.setattr(subprocess, "run", run)
    script = "print(" + repr("x" * 100000) + ")"
    assert runtime.exec_python(script).success
    argv, kwargs = calls[0]
    assert len(" ".join(argv)) < 1000
    assert kwargs["input"] == script
    assert "-i" in argv


def test_effective_tool_configuration(tmp_path):
    cfg = AppConfig()
    cfg.tools.file.max_view_lines = 2
    cfg.tools.shell.timeout = 1
    cfg.tools.shell.max_output_tokens = 50
    (tmp_path / "lines.txt").write_text("one\ntwo\nthree\n")
    registry = _build_registry(cfg, runtime=LocalRuntime(str(tmp_path)))
    view = registry.execute_tool("file_view", {"path": "lines.txt"})
    assert "two" in view.output and "three" not in view.output
    assert registry.tools["shell"]._timeout == 1
    assert registry.tools["shell"]._max_output_chars == 200


def test_file_writes_cannot_escape_scoped_runtime(tmp_path):
    from tools.file_tool import FileWriteTool
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n")
    tool = FileWriteTool(LocalRuntime(str(workspace)))
    result = tool.execute({"path": str(outside), "content": "VALUE = 2\n"})
    assert not result.success and "outside" in result.error
    assert outside.read_text() == "VALUE = 1\n"


def test_symlink_edit_preserves_link_and_checks_target(tmp_path):
    import pytest
    from tools.file_tool import FileEditTool
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.py"
    target.write_text("VALUE = 1\n")
    link = workspace / "alias.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    tool = FileEditTool(LocalRuntime(str(workspace)))
    assert tool.execute({"path": "alias.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}).success
    assert link.is_symlink() and target.read_text() == "VALUE = 2\n"
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 3\n")
    link.unlink()
    link.symlink_to(outside)
    result = tool.execute({"path": "alias.py", "old_string": "VALUE = 3", "new_string": "VALUE = 4"})
    assert not result.success and outside.read_text() == "VALUE = 3\n"
