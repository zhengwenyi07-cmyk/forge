"""Deterministic, model-free recall benchmark for Forge long-term memory."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.task import Task
from memory.identity import identify_repository
from memory.manager import DEFAULT_WEIGHTS, MemoryManager
from memory.models import MemoryRecord
from memory.store import MemoryStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).with_name("memory_recall_manifest.json")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> tuple[dict[str, Any], str]:
    source = Path(path)
    raw = source.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported memory recall manifest schema")
    query_ids = [row["query_id"] for row in manifest["queries"]]
    memory_ids = [row["memory_id"] for row in manifest["memories"]]
    if len(query_ids) != len(set(query_ids)) or len(memory_ids) != len(set(memory_ids)):
        raise ValueError("Manifest ids must be unique")
    return manifest, hashlib.sha256(raw).hexdigest()


def ablation_weights() -> dict[str, dict[str, float]]:
    zero = {key: 0.0 for key in DEFAULT_WEIGHTS}
    lexical = dict(zero, lexical=DEFAULT_WEIGHTS["lexical"])
    lexical_path = dict(lexical, path=DEFAULT_WEIGHTS["path"])
    lexical_path_symbol = dict(lexical_path, symbol=DEFAULT_WEIGHTS["symbol"])
    no_fingerprint = dict(DEFAULT_WEIGHTS, fingerprint=0.0, stale=0.0)
    no_validation = dict(DEFAULT_WEIGHTS, validation=0.0)
    return {
        "lexical_only": lexical,
        "lexical_path": lexical_path,
        "lexical_path_symbol": lexical_path_symbol,
        "full_no_fingerprint": no_fingerprint,
        "full_no_validation": no_validation,
        "full": dict(DEFAULT_WEIGHTS),
    }


def _prepare_repositories(manifest: dict[str, Any], output: Path) -> dict[str, Path]:
    workspaces = output / "workspaces"
    roots: dict[str, Path] = {}
    for name, spec in manifest["repositories"].items():
        root = workspaces / name
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "remote.origin.url", f"benchmark://{name}"], cwd=root, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
        roots[name] = root.resolve()
        for relative, content in spec["files"].items():
            target = (root / relative).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError(f"Manifest path escapes workspace: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    return roots


def _record_from_manifest(raw: dict[str, Any], roots: dict[str, Path]) -> MemoryRecord:
    identity = identify_repository(roots[raw["repository"]])
    fingerprint = identity.code_fingerprint if raw["fingerprint"] == "current" else hashlib.sha256(
        f"benchmark:{raw['fingerprint']}".encode()
    ).hexdigest()
    return MemoryRecord(
        memory_id=raw["memory_id"], repository_id=identity.repository_id,
        repository_label=identity.label, code_fingerprint=fingerprint,
        base_commit=identity.base_commit, task_id=f"fixture-{raw['memory_id'][-3:]}",
        issue_identity=None, memory_type="episodic", task_text=raw["task"],
        summary=raw["summary"], evidence=raw.get("evidence", {}),
        source_artifact=None, source_log=None,
        related_files=list(raw.get("related_files", [])),
        related_symbols=list(raw.get("related_symbols", [])),
        test_status=raw["test_status"], outcome=raw["outcome"],
        confidence=float(raw["confidence"]),
        trust_level="framework_verified" if raw["test_status"] == "passed" else "framework_observed",
        provenance={"generator": "memory-recall-benchmark", "fixture": True},
        content_hash=raw["content_hash"], sensitivity="normal",
    )


def _seed_store(store: MemoryStore, manifest: dict[str, Any], roots: dict[str, Path]) -> list[dict[str, Any]]:
    insertions = []
    for raw in manifest["memories"]:
        record = _record_from_manifest(raw, roots)
        stored_id, deduplicated = store.upsert_memory(record)
        insertions.append({
            "requested_id": raw["memory_id"], "stored_id": stored_id,
            "deduplicated": deduplicated, "duplicate_of": raw.get("duplicate_of"),
        })
    return insertions


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _evaluate_query(
    query: dict[str, Any], manager: MemoryManager, store: MemoryStore,
    roots: dict[str, Path], token_budget: int,
) -> dict[str, Any]:
    repo_root = roots[query["repository"]]
    manager.start_task(Task(
        description=f"__memory_recall_scope__ {query['query_id']}",
        repo_path=str(repo_root), task_id=f"bench-{query['query_id']}", task_type="read_only",
    ))
    ranking_paths = query.get("paths", []) if manager.weights["path"] > 0 else []
    ranking_symbols = query.get("symbols", []) if manager.weights["symbol"] > 0 else []
    started = time.perf_counter()
    ranked = manager.search(
        query["query"], paths=ranking_paths, symbols=ranking_symbols, limit=5,
    )
    rendered, returned_tokens, truncated = manager.render_results(ranked, token_budget)
    latency_ms = (time.perf_counter() - started) * 1000
    delivered = [item for item in ranked if item.record.memory_id in rendered]
    returned_ids = [item.record.memory_id for item in delivered]
    candidate_ids = [item.record.memory_id for item in ranked]
    expected = list(query["expected_memory_ids"])
    expected_set = set(expected)
    should_invalidate = bool(query["should_invalidate"])
    first_rank = next((index for index, memory_id in enumerate(returned_ids, 1) if memory_id in expected_set), None)
    recalls: dict[str, float | None] = {}
    precisions: dict[str, float | None] = {}
    for k in (1, 3, 5):
        if should_invalidate:
            recalls[str(k)] = None
            precisions[str(k)] = None
        else:
            hits = len(expected_set & set(returned_ids[:k]))
            recalls[str(k)] = hits / len(expected_set)
            precisions[str(k)] = hits / k
    returned_files = {path for item in delivered for path in item.record.related_files}
    returned_symbols = {symbol for item in delivered for symbol in item.record.related_symbols}
    identity = identify_repository(repo_root)
    cross_repo = sum(item.record.repository_id != identity.repository_id for item in delivered)
    stale_predictions = {
        item.record.memory_id: item.stale for item in delivered if item.record.memory_id in expected_set
    }
    invalidation_states = {}
    for memory_id in expected:
        record = store.get_memory(memory_id, identity.repository_id)
        invalidation_states[memory_id] = bool(record and record.invalidated_at)
    if should_invalidate:
        invalidation_correct = all(invalidation_states.values()) and not (expected_set & set(returned_ids))
        stale_correct = None
    else:
        invalidation_correct = not any(invalidation_states.values())
        stale_correct = all(
            memory_id in stale_predictions and stale_predictions[memory_id] == bool(query["should_stale"])
            for memory_id in expected
        )
    content_hashes = [item.record.content_hash for item in delivered]
    duplicate_count = (len(returned_ids) - len(set(returned_ids))) + (len(content_hashes) - len(set(content_hashes)))
    file_hit = None if should_invalidate else bool(set(query["correct_files"]) & returned_files)
    symbol_hit = None if should_invalidate else bool(set(query["correct_symbols"]) & returned_symbols)
    return {
        "query_id": query["query_id"], "scenario": query["scenario"], "query": query["query"],
        "paths": query.get("paths", []), "symbols": query.get("symbols", []),
        "ranking_paths": ranking_paths, "ranking_symbols": ranking_symbols,
        "expected_memory_ids": expected, "candidate_ids": candidate_ids,
        "returned_ids": returned_ids, "first_relevant_rank": first_rank,
        "recall_at": recalls, "precision_at": precisions,
        "reciprocal_rank": 0.0 if should_invalidate or first_rank is None else 1.0 / first_rank,
        "correct_file_hit": file_hit, "correct_symbol_hit": symbol_hit,
        "cross_repository_results": cross_repo,
        "expected_stale": query["should_stale"], "stale_predictions": stale_predictions,
        "stale_correct": stale_correct, "should_invalidate": should_invalidate,
        "invalidation_states": invalidation_states, "invalidation_correct": invalidation_correct,
        "duplicate_result_count": duplicate_count,
        "latency_ms": round(latency_ms, 4), "returned_tokens": returned_tokens,
        "truncated": truncated, "rendered_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        "scores": {item.record.memory_id: {"total": item.score, **item.scores} for item in ranked},
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [row for row in rows if not row["should_invalidate"]]
    returned_count = sum(len(row["returned_ids"]) for row in rows)
    summary: dict[str, Any] = {}
    for k in (1, 3, 5):
        summary[f"recall_at_{k}"] = round(_mean([row["recall_at"][str(k)] for row in positives]), 6)
        summary[f"precision_at_{k}"] = round(_mean([row["precision_at"][str(k)] for row in positives]), 6)
    summary.update({
        "mrr": round(_mean([row["reciprocal_rank"] for row in positives]), 6),
        "correct_file_hit_rate": round(_mean([float(row["correct_file_hit"]) for row in positives]), 6),
        "correct_symbol_hit_rate": round(_mean([float(row["correct_symbol_hit"]) for row in positives]), 6),
        "cross_repository_error_rate": round(
            sum(row["cross_repository_results"] for row in rows) / max(1, returned_count), 6
        ),
        "stale_identification_rate": round(_mean([float(row["stale_correct"]) for row in positives]), 6),
        "invalidation_accuracy": round(_mean([float(row["invalidation_correct"]) for row in rows]), 6),
        "duplicate_result_rate": round(
            sum(row["duplicate_result_count"] for row in rows) / max(1, returned_count), 6
        ),
        "average_latency_ms": round(_mean([row["latency_ms"] for row in rows]), 4),
        "p95_latency_ms": round(_p95([row["latency_ms"] for row in rows]), 4),
        "average_returned_tokens": round(_mean([row["returned_tokens"] for row in rows]), 4),
        "truncation_rate": round(_mean([float(row["truncated"]) for row in rows]), 6),
        "positive_queries": len(positives), "invalidation_queries": len(rows) - len(positives),
        "query_count": len(rows),
    })
    return summary


def _error_cases(rows: list[dict[str, Any]], lexical_rows: list[dict[str, Any]] | None = None) -> dict[str, list[str]]:
    errors = {
        "no_recall": [], "ranked_below_one": [], "wrong_repository": [],
        "stale_misclassified": [], "failure_over_success": [], "lexical_overdependence": [],
    }
    for row in rows:
        if not row["should_invalidate"] and row["first_relevant_rank"] is None:
            errors["no_recall"].append(row["query_id"])
        if row["first_relevant_rank"] and row["first_relevant_rank"] > 1:
            errors["ranked_below_one"].append(row["query_id"])
        if row["cross_repository_results"]:
            errors["wrong_repository"].append(row["query_id"])
        if row["stale_correct"] is False:
            errors["stale_misclassified"].append(row["query_id"])
        if row["scenario"] == "success_failure_conflict" and row["first_relevant_rank"] != 1:
            errors["failure_over_success"].append(row["query_id"])
    if lexical_rows is not None:
        by_id = {row["query_id"]: row for row in lexical_rows}
        for row in rows:
            lexical = by_id[row["query_id"]]
            if row["scenario"] == "lexically_different_semantics" and lexical["first_relevant_rank"] != 1 and row["first_relevant_rank"] == 1:
                errors["lexical_overdependence"].append(row["query_id"])
    return errors


def _render_report(result: dict[str, Any]) -> str:
    lines = [
        "# Forge Memory Recall Benchmark", "",
        f"- Benchmark: `{result['benchmark_id']}`",
        f"- Manifest SHA-256: `{result['manifest_sha256']}`",
        f"- Generated: `{result['generated_at']}`",
        "- Model/API calls: `0`", "",
        "## Ablation summary", "",
        "| Ablation | R@1 | R@3 | R@5 | P@1 | P@3 | P@5 | MRR | File | Symbol | Stale | Invalid | Cross-repo | Duplicate | Avg ms | P95 ms | Avg tokens | Trunc |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, run in result["ablations"].items():
        s = run["summary"]
        lines.append(
            f"| {name} | {s['recall_at_1']:.3f} | {s['recall_at_3']:.3f} | {s['recall_at_5']:.3f} | "
            f"{s['precision_at_1']:.3f} | {s['precision_at_3']:.3f} | {s['precision_at_5']:.3f} | {s['mrr']:.3f} | "
            f"{s['correct_file_hit_rate']:.3f} | {s['correct_symbol_hit_rate']:.3f} | {s['stale_identification_rate']:.3f} | "
            f"{s['invalidation_accuracy']:.3f} | {s['cross_repository_error_rate']:.3f} | {s['duplicate_result_rate']:.3f} | "
            f"{s['average_latency_ms']:.3f} | {s['p95_latency_ms']:.3f} | {s['average_returned_tokens']:.1f} | {s['truncation_rate']:.3f} |"
        )
    for name, run in result["ablations"].items():
        lines.extend([
            "", f"## Per-query: `{name}`", "",
            "| Query | Scenario | Expected | Returned | Rank | File | Symbol | Stale | Invalid | Dup | ms | tokens | trunc |",
            "| --- | --- | --- | --- | ---: | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ])
        for row in run["queries"]:
            lines.append(
                f"| {row['query_id']} | {row['scenario']} | {', '.join(row['expected_memory_ids'])} | "
                f"{', '.join(row['returned_ids']) or '—'} | {row['first_relevant_rank'] or '—'} | "
                f"{row['correct_file_hit']} | {row['correct_symbol_hit']} | {row['stale_correct']} | "
                f"{row['invalidation_correct']} | {row['duplicate_result_count']} | {row['latency_ms']:.3f} | "
                f"{row['returned_tokens']} | {row['truncated']} |"
            )
    lines.extend(["", "## Full-ranking error analysis", ""])
    for category, query_ids in result["error_analysis"].items():
        lines.append(f"- **{category}**: {', '.join(query_ids) if query_ids else 'none'}")
    lines.extend([
        "", "## Interpretation", "",
        "Metrics exclude the deleted-file negative query from recall, precision, MRR, file and symbol rates; it is evaluated by invalidation accuracy.",
        "Precision@k uses denominator k. Recall is calculated from the budget-delivered rows, not hidden database candidates.",
        "Latency is an offline directional measurement over this small synthetic set and is not a production load test.",
    ])
    return "\n".join(lines) + "\n"


def run_benchmark(
    manifest_path: str | Path, output_dir: str | Path,
    selected_ablations: list[str] | None = None,
) -> dict[str, Any]:
    manifest, manifest_sha = load_manifest(manifest_path)
    output = Path(output_dir).resolve()
    if (output / "results.json").exists():
        raise FileExistsError(f"Refusing to overwrite existing benchmark output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    roots = _prepare_repositories(manifest, output)
    all_weights = ablation_weights()
    names = selected_ablations or list(all_weights)
    unknown = sorted(set(names) - set(all_weights))
    if unknown:
        raise ValueError(f"Unknown ablations: {', '.join(unknown)}")
    result: dict[str, Any] = {
        "schema_version": 1, "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": manifest_sha, "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "model_calls": 0, "ablations": {},
    }
    for name in names:
        db = output / "databases" / f"{name}.memory.sqlite3"
        store = MemoryStore(db)
        insertions = _seed_store(store, manifest, roots)
        rows = []
        for query in manifest["queries"]:
            manager = MemoryManager(
                store, initial_token_budget=600,
                search_token_budget=int(manifest["output_token_budget"]),
                max_results=5, weights=all_weights[name],
            )
            rows.append(_evaluate_query(
                query, manager, store, roots, int(manifest["output_token_budget"]),
            ))
        result["ablations"][name] = {
            "weights": all_weights[name], "insertions": insertions,
            "summary": _summarize(rows), "queries": rows,
        }
    if "full" in result["ablations"]:
        lexical = result["ablations"].get("lexical_only", {}).get("queries")
        result["error_analysis"] = _error_cases(result["ablations"]["full"]["queries"], lexical)
    else:
        result["error_analysis"] = {}
    _atomic_json(output / "results.json", result)
    _atomic_text(output / "report.md", _render_report(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ablation", action="append", choices=list(ablation_weights()), dest="ablations")
    args = parser.parse_args()
    result = run_benchmark(args.manifest, args.output, args.ablations)
    full = result["ablations"].get("full")
    if full:
        summary = full["summary"]
        print(json.dumps({
            "recall_at_1": summary["recall_at_1"], "recall_at_5": summary["recall_at_5"],
            "mrr": summary["mrr"], "average_latency_ms": summary["average_latency_ms"],
            "output": str(args.output.resolve()),
        }, ensure_ascii=False))
    else:
        print(json.dumps({"output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
