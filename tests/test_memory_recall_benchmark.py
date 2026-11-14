from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.memory_recall_benchmark import DEFAULT_MANIFEST, load_manifest, run_benchmark


def test_frozen_manifest_has_complete_ground_truth():
    manifest, digest = load_manifest(DEFAULT_MANIFEST)

    assert manifest["benchmark_id"] == "forge-memory-recall-v1"
    assert len(digest) == 64
    assert len(manifest["queries"]) == 12
    assert {
        "similar_issue", "error_message", "file_path", "qualified_symbol",
        "success_failure_conflict", "same_repo_different_commit",
        "cross_repository_interference", "deleted_file_invalidation",
        "duplicate_memory", "chinese_query", "lexically_different_semantics",
        "stale_identification",
    } == {row["scenario"] for row in manifest["queries"]}
    for row in manifest["queries"]:
        assert row["expected_memory_ids"]
        assert "correct_files" in row and "correct_symbols" in row
        assert isinstance(row["should_stale"], bool)
        assert isinstance(row["should_invalidate"], bool)


def test_full_recall_regression_and_ablation(tmp_path: Path):
    output = tmp_path / "memory-recall"
    result = run_benchmark(
        DEFAULT_MANIFEST, output, selected_ablations=["lexical_only", "full"],
    )
    lexical = result["ablations"]["lexical_only"]["summary"]
    full = result["ablations"]["full"]["summary"]

    assert result["model_calls"] == 0
    assert full["recall_at_1"] >= 0.90
    assert full["recall_at_3"] == 1.0
    assert full["recall_at_5"] == 1.0
    assert full["mrr"] >= 0.95
    assert full["correct_file_hit_rate"] == 1.0
    assert full["correct_symbol_hit_rate"] == 1.0
    assert full["cross_repository_error_rate"] == 0.0
    assert full["stale_identification_rate"] == 1.0
    assert full["invalidation_accuracy"] == 1.0
    assert full["duplicate_result_rate"] == 0.0
    assert full["recall_at_1"] > lexical["recall_at_1"]
    assert full["mrr"] > lexical["mrr"]
    assert len(result["ablations"]["full"]["queries"]) == 12
    assert (output / "results.json").is_file()
    assert (output / "report.md").is_file()

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        run_benchmark(DEFAULT_MANIFEST, output, selected_ablations=["full"])
