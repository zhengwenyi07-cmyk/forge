import json
from pathlib import Path

import pytest

from evaluation.suite import freeze, load_suite, select_tasks
from evaluation.summarize import paired_stats


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
    with pytest.raises(ValueError, match="fingerprint"):
        load_suite(dataset, manifest)


def test_paired_statistics_use_discordant_pairs():
    result = paired_stats([True, False, False, True], [False, True, True, True])
    assert result["pro_minus_flash"] == .25
    assert result["pro_only_resolved"] == 2
    assert result["flash_only_resolved"] == 1
    assert result["mcnemar_exact_p"] == 1.0
    assert paired_stats([False] * 20, [False] * 20)["paired_bootstrap_95_ci"] == [0, 0]
