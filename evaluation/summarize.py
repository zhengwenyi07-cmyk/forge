"""Paired outcome statistics; generated patches never count as resolved tasks."""
from __future__ import annotations

import json
import math
import random
import statistics
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
    ids = [t["instance_id"] for t in manifest["tasks"]]
    models = manifest["settings"]["models"]
    summary = {"tasks": len(ids), "models": {}, "scope": "SWE-bench Lite paired experiment; no general model ranking implied"}
    resolved_by_model = []
    for model in models:
        report = validate_reports(output / model / "verification", "paired", set(ids))
        resolved = set(report["resolved_ids"])
        resolved_by_model.append([i in resolved for i in ids])
        records = [json.loads((output / model / i / "result.json").read_text()) for i in ids]
        if any(r["status"] == "infrastructure_error" for r in records):
            raise RuntimeError("Infrastructure errors invalidate the paired comparison")
        summary["models"][model] = {"resolved": len(resolved), "resolved_rate": len(resolved) / len(ids), "tokens": sum(r["tokens"] for r in records), "median_seconds": statistics.median(r["seconds"] for r in records), "agent_completed": sum(r["status"] == "success" for r in records), "resolved_ids": sorted(resolved)}
    summary["paired"] = paired_stats(*resolved_by_model)
    return summary
