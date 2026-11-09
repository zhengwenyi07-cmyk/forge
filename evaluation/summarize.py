"""Paired outcome statistics; generated patches never count as resolved tasks."""
from __future__ import annotations

import json
import math
import random
import statistics
from collections import Counter
from pathlib import Path

from run_verification import validate_reports


def paired_stats(a: list[bool], b: list[bool]):
    if len(a) != len(b) or not a:
        raise ValueError("Paired results must have the same nonzero length")
    differences = [int(y) - int(x) for x, y in zip(a, b)]
    b_wins = differences.count(1)
    a_wins = differences.count(-1)
    discordant = a_wins + b_wins
    p_value = min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(a_wins, b_wins) + 1)) / 2 ** discordant) if discordant else 1.0
    rng = random.Random(20261028)
    bootstrap = sorted(sum(rng.choices(differences, k=len(a))) / len(a) for _ in range(10000))
    return {"pro_minus_flash": sum(differences) / len(a), "paired_bootstrap_95_ci": [bootstrap[249], bootstrap[9749]], "pro_only_resolved": b_wins, "flash_only_resolved": a_wins, "mcnemar_exact_p": p_value}


def summarize(output: Path):
    manifest = json.loads((output / "manifest.json").read_text())
    effective_manifest = output / "effective-manifest.json"
    if effective_manifest.exists():
        manifest = json.loads(effective_manifest.read_text())
    ids = [t["instance_id"] for t in manifest["tasks"]]
    models = manifest["settings"]["models"]
    scope = "SWE-bench Lite paired experiment" if len(models) == 2 else "SWE-bench Lite frozen-suite evaluation"
    summary = {"tasks": len(ids), "models": {}, "scope": f"{scope}; no general model ranking implied"}
    resolved_by_model = []
    for model in models:
        predictions = json.loads((output / f"{model}-predictions.json").read_text(encoding="utf-8"))
        report = validate_reports(output / model / "verification", "paired", set(ids), expected_predictions=predictions)
        resolved = set(report["resolved_ids"])
        resolved_by_model.append([i in resolved for i in ids])
        records = [json.loads((output / model / i / "result.json").read_text()) for i in ids]
        if any(r["status"] == "infrastructure_error" for r in records):
            raise RuntimeError("Infrastructure errors invalidate the paired comparison")
        patches = [(output / model / i / "prediction.patch").read_text(encoding="utf-8") for i in ids]
        status_counts = dict(sorted(Counter(r["status"] for r in records).items()))
        summary["models"][model] = {
            "resolved": len(resolved),
            "resolved_rate": len(resolved) / len(ids),
            "nonempty_patches": sum(bool(p.strip()) for p in patches),
            "nonempty_patch_rate": sum(bool(p.strip()) for p in patches) / len(ids),
            "tokens": sum(r["tokens"] for r in records),
            "total_seconds": sum(r["seconds"] for r in records),
            "median_seconds": statistics.median(r["seconds"] for r in records),
            "agent_completed": sum(r["status"] == "success" for r in records),
            "status_counts": status_counts,
            "resolved_ids": sorted(resolved),
        }
    if len(resolved_by_model) == 2:
        summary["paired"] = paired_stats(*resolved_by_model)
    elif len(resolved_by_model) > 2:
        raise ValueError("Paired statistics require exactly two selected models")
    return summary
