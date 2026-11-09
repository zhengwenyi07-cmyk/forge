import json
from pathlib import Path

import pytest

from evaluation.suite import freeze, load_suite, select_tasks
from evaluation.summarize import paired_stats, summarize
from evaluation.run_ab import ProgressTracker, import_reusable_trial, write_json


def test_task_selection_is_balanced_deterministic_and_order_independent():
    rows = [{"instance_id": f"{repo}-{i}", "repo": repo} for repo in ["a", "b", "c"] for i in range(20)]
    a = select_tasks(rows, 30)
    b = select_tasks(list(reversed(rows)), 30)
    assert a == b
    assert len({r["instance_id"] for r in a}) == 30
    assert all(sum(r["repo"] == repo for r in a) == 10 for repo in ["a", "b", "c"])


def test_manifest_rejects_dataset_drift(tmp_path):
    rows = [{"instance_id": f"a-{i}", "repo": "a", "base_commit": str(i)} for i in range(30)]
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(rows))
    manifest = tmp_path / "manifest.json"
    freeze(dataset, manifest, 20)
    assert len(load_suite(dataset, manifest)[1]) == 20
    dataset.write_text(json.dumps(rows) + " ")
    assert len(load_suite(dataset, manifest)[1]) == 20
    rows[0]["base_commit"] = "changed"
    dataset.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="fingerprint"):
        load_suite(dataset, manifest)


def test_paired_statistics_use_discordant_pairs():
    result = paired_stats([True, False, False, True], [False, True, True, True])
    assert result["pro_minus_flash"] == .25
    assert result["pro_only_resolved"] == 2
    assert result["flash_only_resolved"] == 1
    assert result["mcnemar_exact_p"] == 1.0
    assert paired_stats([False] * 20, [False] * 20)["paired_bootstrap_95_ci"] == [0, 0]


def test_progress_tracker_is_durable_and_resumable(tmp_path):
    rows = [{"instance_id": "task-a"}, {"instance_id": "task-b"}]
    models = ["flash", "pro"]
    tracker = ProgressTracker(tmp_path, rows, models)
    tracker.update("flash", "task-a", "running", tokens=12)
    tracker.update("flash", "task-a", "success", tokens=34, seconds=1.5)
    saved = json.loads((tmp_path / "progress.json").read_text())
    assert saved["counts"]["success"] == 1
    assert "| flash | task-a | success | 34 | 1.5 |" in (tmp_path / "progress.md").read_text()
    resumed = ProgressTracker(tmp_path, rows, models)
    resumed.update("pro", "task-b", "gave_up", tokens=55)
    saved = json.loads((tmp_path / "progress.json").read_text())
    assert saved["counts"]["success"] == saved["counts"]["gave_up"] == 1


def test_write_json_replaces_complete_document(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"value": "old", "padding": "x" * 100})
    write_json(path, {"value": "new"})
    assert json.loads(path.read_text()) == {"value": "new"}
    assert not list(tmp_path.glob("*.tmp"))


def test_completed_trial_can_be_imported_with_provenance(tmp_path):
    source_root = tmp_path / "old"
    source = source_root / "flash" / "task-a"
    source.mkdir(parents=True)
    old_identity = {
        "model": "flash", "instance_id": "task-a", "base_commit": "abc",
        "code": {"commit": "old"},
        "settings": {"max_steps": 40, "budget_tokens": None, "max_tokens": 8192,
                     "temperature": 0, "multi_agent": False, "models": ["flash", "pro"]},
        "effective_config": {"endpoint": "https://example.invalid/v1"},
    }
    write_json(source / "result.json", {"identity": old_identity, "status": "max_steps",
                                         "tokens": 123, "seconds": 4})
    (source / "prediction.patch").write_text("diff --git a/a b/a\n")
    new_identity = dict(old_identity, code={"commit": "new"},
                        settings=dict(old_identity["settings"], models=["flash"]))
    target = tmp_path / "new" / "flash" / "task-a"
    target.mkdir(parents=True)

    imported = import_reusable_trial([source_root], target, "flash",
                                     {"instance_id": "task-a"}, new_identity)

    assert imported["status"] == "max_steps"
    assert imported["identity"] == new_identity
    assert imported["reused_from"]["original_identity"] == old_identity
    assert (target / "prediction.patch").read_text() == "diff --git a/a b/a\n"


def test_reusable_trial_rejects_effective_configuration_drift(tmp_path):
    source = tmp_path / "old" / "flash" / "task-a"
    source.mkdir(parents=True)
    identity = {
        "model": "flash", "instance_id": "task-a", "base_commit": "abc",
        "settings": {"max_steps": 40, "budget_tokens": None, "max_tokens": 8192,
                     "temperature": 0, "multi_agent": False},
        "effective_config": {"endpoint": "https://old.invalid/v1"},
    }
    write_json(source / "result.json", {"identity": identity, "status": "success"})
    (source / "prediction.patch").write_text("")
    target = tmp_path / "new"
    target.mkdir()
    changed = dict(identity, effective_config={"endpoint": "https://new.invalid/v1"})

    with pytest.raises(RuntimeError, match="configuration mismatch"):
        import_reusable_trial([tmp_path / "old"], target, "flash",
                              {"instance_id": "task-a"}, changed)


def test_summarize_supports_flash_only_effective_manifest(tmp_path, monkeypatch):
    tasks = [{"instance_id": "task-a"}, {"instance_id": "task-b"}]
    write_json(tmp_path / "manifest.json", {
        "tasks": tasks, "settings": {"models": ["flash", "pro"]},
    })
    write_json(tmp_path / "effective-manifest.json", {
        "tasks": tasks, "settings": {"models": ["flash"]},
    })
    write_json(tmp_path / "flash-predictions.json", [
        {"instance_id": "task-a", "model_patch": "diff", "model_name_or_path": "flash"},
        {"instance_id": "task-b", "model_patch": "", "model_name_or_path": "flash"},
    ])
    for instance_id, status, tokens, seconds, patch in [
        ("task-a", "success", 10, 2.5, "diff"),
        ("task-b", "max_steps", 20, 4.5, ""),
    ]:
        trial = tmp_path / "flash" / instance_id
        trial.mkdir(parents=True)
        write_json(trial / "result.json", {
            "status": status, "tokens": tokens, "seconds": seconds,
        })
        (trial / "prediction.patch").write_text(patch)
    monkeypatch.setattr(
        "evaluation.summarize.validate_reports",
        lambda *args, **kwargs: {"resolved_ids": ["task-a"]},
    )

    result = summarize(tmp_path)

    assert set(result["models"]) == {"flash"}
    assert "paired" not in result
    assert result["scope"].startswith("SWE-bench Lite frozen-suite evaluation")
    assert result["models"]["flash"] == {
        "resolved": 1,
        "resolved_rate": 0.5,
        "nonempty_patches": 1,
        "nonempty_patch_rate": 0.5,
        "tokens": 30,
        "total_seconds": 7.0,
        "median_seconds": 3.5,
        "agent_completed": 1,
        "status_counts": {"max_steps": 1, "success": 1},
        "resolved_ids": ["task-a"],
    }
