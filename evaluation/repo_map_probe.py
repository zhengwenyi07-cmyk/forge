"""Compare map strategies offline; expected files are scoring labels only."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from context.repo_map import RepoMap
from context.token_budget import estimate_tokens


def compare(repo_path, task, budget=4000, expected_files=()):
    labels = {str(p).replace("\\", "/") for p in expected_files}
    results = {}
    for strategy in ("legacy", "lexical", "global", "personalized"):
        # Independent, cold indexes measure construction as well as rendering.
        repo_map = RepoMap(repo_path)
        start = time.perf_counter()
        text = repo_map.build(budget=budget, task=task, strategy=strategy)
        seconds = time.perf_counter() - start
        files = [line.rstrip(":").replace("\\", "/") for line in text.splitlines()
                 if line and not line[0].isspace() and not line.startswith(("...", "("))]
        if strategy == "legacy":
            files = [re.sub(r" \(\d+ symbols?\)$", "", path) for path in files]
        record = {"seconds": round(seconds, 4), "estimated_tokens": estimate_tokens(text),
                  "budget_exceeded": estimate_tokens(text) > budget, "files": files,
                  "map": text, "diagnostics": repo_map.last_diagnostics}
        if labels:
            record["recall"] = {str(k): len(labels & set(files[:k])) / len(labels) for k in (1, 5, 10)}
            record["included_recall"] = len(labels & set(files)) / len(labels)
            record["first_relevant_rank"] = next((i + 1 for i, p in enumerate(files) if p in labels), None)
        results[strategy] = record
    return {"repo": str(Path(repo_path).resolve()), "task": task, "budget": budget,
            "labels": sorted(labels), "results": results,
            "note": "Offline retrieval proxy only; this does not measure resolved rate."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--task", required=True)
    parser.add_argument("--budget", type=int, default=4000)
    parser.add_argument("--expected", action="append", default=[], help="Optional offline relevance label; never added to the map query")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.budget < 1:
        parser.error("--budget must be positive")
    result = compare(args.repo, args.task, args.budget, args.expected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    for strategy, record in result["results"].items():
        print(strategy, "tokens=", record["estimated_tokens"], "first_relevant_rank=", record.get("first_relevant_rank"), "seconds=", record["seconds"])


if __name__ == "__main__":
    main()
