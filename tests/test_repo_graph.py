import subprocess

from context.graph_rank import pagerank
from context.repo_map import RepoMap
from context.token_budget import estimate_tokens


def test_weighted_pagerank_and_dangling_nodes():
    ranks = pagerank({"caller": {"core": 4}, "core": {}, "isolated": {}})
    assert ranks["core"] > ranks["caller"]
    assert abs(sum(ranks.values()) - 1) < 1e-8
    assert pagerank({}) == {}


def test_task_seed_retains_rare_boundary(tmp_path):
    (tmp_path / "hub.py").write_text("def common(): pass\n")
    (tmp_path / "caller.py").write_text("from hub import common\ncommon()\n")
    (tmp_path / "rare_boundary.py").write_text("def special_case(value: str): return value\n")
    repo = RepoMap(tmp_path)
    text = repo.build(100, task="Fix rare_boundary.py special_case")
    assert text.startswith("rare_boundary.py")
    assert "special_case(value: str)" in text
    assert repo.index.edges["caller.py"]["hub.py"] > 0


def test_oversized_file_does_not_empty_map(tmp_path):
    (tmp_path / "a_many.py").write_text("".join(f"def function_{i}(): pass\n" for i in range(100)))
    (tmp_path / "b_target.py").write_text("def target(): pass\n")
    text = RepoMap(tmp_path).build(100, task="b_target.py target")
    assert "b_target.py" in text and "def target" in text
    assert estimate_tokens(text) <= 100


def test_git_ignored_files_excluded_but_new_source_visible(real_repo):
    (real_repo / ".gitignore").write_text("ignored/\n")
    (real_repo / "ignored").mkdir()
    (real_repo / "ignored" / "noise.py").write_text("def noise(): pass\n")
    (real_repo / "new_source.py").write_text("def new_feature(): pass\n")
    repo = RepoMap(real_repo)
    repo.build(1000)
    assert "ignored/noise.py" not in repo.index.files
    assert "new_source.py" in repo.index.files


def test_non_git_ignore_and_incremental_updates(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "noise.py").write_text("def noise(): pass\n")
    source = tmp_path / "mod.py"
    source.write_text("def original(): pass\n")
    repo = RepoMap(tmp_path)
    first = repo.cache_key()
    assert "original" in repo.build(1000)
    count = repo.index.parse_count
    assert repo.cache_key() == first
    assert repo.index.parse_count == count
    source.write_text("def replaced(): pass\n")
    assert repo.cache_key() != first
    assert "replaced" in repo.build(1000) and "original" not in repo.build(1000)
    assert "ignored/noise.py" not in repo.index.files
    source.unlink()
    repo.build(1000)
    assert "mod.py" not in repo.index.files


def test_qualified_methods_and_relative_imports(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "api.py").write_text("from .worker import Worker\nWorker()\n")
    (package / "worker.py").write_text("class Worker:\n    async def run(self, value: int) -> str: return str(value)\n")
    repo = RepoMap(tmp_path)
    text = repo.build(1000, task="pkg/worker.py run")
    assert "async def Worker.run(self, value: int) -> str" in text
    assert repo.index.edges["pkg/api.py"]["pkg/worker.py"] > 0
    assert "\\" not in text


def test_all_ranking_modes_respect_budget(tmp_path):
    for i in range(10):
        (tmp_path / f"module_{i}.py").write_text(f"def callable_{i}(value: int): return value\n")
    repo = RepoMap(tmp_path)
    for strategy in ("personalized", "global", "lexical"):
        for budget in (10, 50, 300):
            assert estimate_tokens(repo.build(budget, task="module_9.py", strategy=strategy)) <= budget


def test_large_file_retains_metadata_without_parsing(tmp_path):
    (tmp_path / "large.py").write_text("# filler\n" * 70000)
    repo = RepoMap(tmp_path)
    assert "large.py" in repo.build(100, task="large.py")
    assert repo.index.files["large.py"].parse_status == "large_file_metadata"


def test_dotted_import_without_alias_links_submodule(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "api.py").write_text("def run(): pass\n")
    (tmp_path / "caller.py").write_text("import pkg.api\npkg.api.run()\n")
    repo = RepoMap(tmp_path)
    repo.build(500)
    assert repo.index.edges["caller.py"]["pkg/api.py"] > 0


def test_offline_labels_do_not_enter_retrieval_query(tmp_path, monkeypatch):
    from evaluation.repo_map_probe import compare
    calls = []
    original = RepoMap.build
    def build(self, *args, **kwargs):
        calls.append(kwargs.get("task"))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(RepoMap, "build", build)
    (tmp_path / "oracle.py").write_text("def compute(): pass\n")
    report = compare(tmp_path, "Diagnose a problem", 100, ["oracle.py"])
    assert calls == ["Diagnose a problem"] * 4
    assert report["results"]["personalized"]["recall"]["1"] == 1
    assert report["results"]["legacy"]["recall"]["1"] == 1


def test_search_tools_and_map_share_definitions_and_ignore_rules(real_repo):
    from agent.core import Agent
    from agent.event_log import EventLog
    from agent.task import Task, Action, ActionType, ToolCall
    from config.schema import AppConfig
    from entry.cli import _build_registry
    from llm.base import MockBackend
    from tools.runtime import LocalRuntime
    with (real_repo / ".gitignore").open("a") as stream:
        stream.write("ignored/\n")
    (real_repo / "ignored").mkdir()
    (real_repo / "ignored/noise.py").write_text("def hidden(): pass\nSECRET_MARKER = 1\n")
    (real_repo / "worker.py").write_text("class Worker:\n    async def run(self): pass\n")
    runtime = LocalRuntime(str(real_repo))
    registry = _build_registry(AppConfig(), runtime=runtime)
    script = [Action(ActionType.TOOL_CALL, "", ToolCall(name, params)) for name, params in (
        ("find_symbol", {"symbol": "Worker.run"}), ("find_files", {"pattern": "*.py"}),
        ("search_text", {"pattern": "SECRET_MARKER"}))]
    script.append(Action(ActionType.FINISH, "", message="Inspected"))
    task = Task("Inspect async worker", str(real_repo), task_type="read_only")
    agent = Agent(MockBackend(script), registry, runtime=runtime)
    with EventLog.create(task, str(real_repo.parent / "logs")) as log:
        assert agent.run(task, log).is_success()
        observations = [e.payload["observation"]["output"] for e in log.replay() if e.event_type.value == "observation"]
    assert "async def Worker.run" in observations[0]
    assert "worker.py" in observations[1] and "noise.py" not in observations[1]
    assert observations[2].strip() == "No matches found."
    assert registry.tools["find_symbol"]._repo_index is agent._repo_map.index
