import json

from context.repo_index import FileRecord, RepoIndex
from context.map_builder import RankedRepoMap
from tools.repo_query_tool import RepoQueryTool


def query(index, name, **params):
    tool = RepoQueryTool(name, token_budget=500)
    tool.index = index
    return tool, tool.execute(params)


def test_symbol_path_dependency_budget_duplicate_and_safety(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("from pkg.b import target\n\ndef caller():\n    return target()\n")
    (package / "b.py").write_text("def target():\n    return 42\n")
    (tmp_path / ".gitignore").write_text("secret.py\n")
    (tmp_path / "secret.py").write_text("def target_secret(): pass\n")
    index = RepoIndex(tmp_path)
    index.refresh()
    assert "secret.py" not in index.files
    tool, result = query(index, "repo_symbols", query="target", kind="function")
    assert result.success
    assert json.loads(result.output)["candidates"][0]["path"] == "pkg/b.py"
    assert json.loads(tool.execute({"query": "target", "kind": "function"}).output)["duplicate"]
    _, result = query(index, "repo_search", query="pkg/b.py", path_prefix="pkg")
    assert result.success
    _, result = query(index, "repo_related_files", paths=["pkg/a.py"], direction="outgoing")
    assert any(row["path"] == "pkg/b.py" for row in json.loads(result.output)["candidates"])
    _, result = query(index, "repo_tree", path="../")
    assert not result.success
    _, result = query(index, "repo_map", paths=["pkg/b.py"], token_budget=70)
    assert json.loads(result.output)["truncated"]


def test_overview_stays_small_and_cache_invalidates(tmp_path):
    (tmp_path / "main.py").write_text("def first(): pass\n")
    repo = RankedRepoMap(tmp_path)
    overview = repo.overview("first", budget=100)
    assert "main.py" in overview
    assert "def first" not in overview
    before = repo.index.version
    (tmp_path / "main.py").write_text("def second(): pass\n")
    repo.index.refresh()
    assert before != repo.index.version
    assert "main.py" in repo.overview("second", budget=100)


def test_large_index_is_not_embedded_in_overview(tmp_path):
    repo = RankedRepoMap(tmp_path)
    repo.index.files = {f"module{i:05d}.py": FileRecord(f"module{i:05d}.py", 0) for i in range(20000)}
    repo.index.edges = {path: {} for path in repo.index.files}
    repo.index.refresh = lambda: "synthetic-v1"
    overview = repo.overview("module12000", budget=200)
    assert len(overview) < 1000
    assert "module12000.py" in overview
    tool, result = query(repo.index, "repo_search", query="module12000", limit=10)
    assert result.success
    assert len(result.output) < 2500
    assert len(json.loads(result.output)["candidates"]) <= 10


def test_agent_finds_file_through_query_without_static_map(tmp_path):
    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.task import Action, ActionType, Task, ToolCall
    from config.schema import AppConfig
    from entry.cli import _build_registry
    from llm.base import MockBackend
    from tools.runtime import LocalRuntime

    (tmp_path / "target.py").write_text("def rare_locator(): pass\n")
    runtime = LocalRuntime(str(tmp_path))
    registry = _build_registry(AppConfig(), runtime=runtime)
    script = [Action(ActionType.TOOL_CALL, "", ToolCall("repo_symbols", {"query": "rare_locator"})),
              Action(ActionType.FINISH, "", message="Found target.py")]
    task = Task("Locate rare_locator", str(tmp_path), task_type="read_only")
    agent = Agent(MockBackend(script), registry, AgentConfig(repo_map_strategy="retrieval"), runtime=runtime)
    with EventLog.create(task, str(tmp_path / "logs")) as log:
        result = agent.run(task, log)
        observations = [e.payload["observation"]["output"] for e in log.replay() if e.event_type.value == "observation"]
    assert result.is_success()
    assert "target.py" in observations[0]
    assert "rare_locator" not in agent._cached_system_prompt
    assert result.retrieval["query_count"] == 1
