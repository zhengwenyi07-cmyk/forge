import subprocess

from agent.task import Task, Action, ActionType, ToolCall
from context.history import ConversationHistory
from llm.base import MockBackend
from multi_agent.sub_agent import SubAgent
from multi_agent.worktree import IsolatedWorktree
from tools.base import ToolRegistry
from tools.file_tool import FileEditTool
from tools.test_tool import PytestTool


def test_snapshot_preserves_parent_dirty_state_and_index(real_repo):
    (real_repo / "app.py").write_text("VALUE = 3\n")
    (real_repo / "untracked.py").write_text("keep = True\n")
    index = (real_repo / ".git" / "index").read_bytes()
    tree = IsolatedWorktree(str(real_repo)).create()
    try:
        assert (tree.root / "app.py").read_text() == "VALUE = 3\n"
        assert (tree.root / "untracked.py").exists()
        (tree.root / "app.py").write_text("VALUE = 2\n")
        assert (real_repo / "app.py").read_text() == "VALUE = 3\n"
        assert (real_repo / ".git" / "index").read_bytes() == index
    finally:
        tree.cleanup()
    assert not tree.root.exists()


def test_child_receives_assignment_and_parent_message(real_repo):
    seen = []
    class RecordingBackend(MockBackend):
        def complete(self, messages, tools):
            seen.extend(m.content for m in messages)
            return super().complete(messages, tools)
    child = SubAgent("assignment", "explore", Task("Find precise VALUE definition", str(real_repo)), RecordingBackend([Action(ActionType.FINISH, "", message="found")]), ToolRegistry(), ConversationHistory(), "parent", sub_agent_system_prompt="EXPLORE INSTRUCTIONS", log_dir=str(real_repo.parent / "logs"))
    child.send_parent_message("Also inspect its tests")
    child.start()
    assert child.wait(10).is_success()
    assert any("Find precise VALUE definition" in text for text in seen)
    assert any("Also inspect its tests" in text for text in seen)
    assert any("EXPLORE INSTRUCTIONS" in text for text in seen)
    assert not child.mailbox.has_pending_inbox


def test_two_forks_return_independent_patches(real_repo):
    children = []
    for i, value in enumerate([2, 4]):
        script = [Action(ActionType.TOOL_CALL, "", ToolCall("file_edit", {"path": "app.py", "old_string": "VALUE = 1", "new_string": f"VALUE = {value}"})), Action(ActionType.TOOL_CALL, "", ToolCall("test", {"path": "test_app.py"})), Action(ActionType.FINISH, "", message=f"VALUE {value}")]
        child = SubAgent(f"fork-{i}", "fork", Task("Fix value", str(real_repo)), MockBackend(script), ToolRegistry().register(FileEditTool()).register(PytestTool()), ConversationHistory(), "parent", log_dir=str(real_repo.parent / "logs"))
        children.append(child)
        child.start()
    results = [child.wait(30) for child in children]
    assert all(r is not None and r.is_success() for r in results)
    assert "+VALUE = 2" in results[0].patch
    assert "+VALUE = 4" in results[1].patch
    assert (real_repo / "app.py").read_text() == "VALUE = 1\n"
    assert all(not child._isolated_worktree.root.exists() for child in children)
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=real_repo) == b""
