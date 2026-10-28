"""Freeze a repository-balanced task set before seeing model outcomes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def select_tasks(dataset: list[dict], count: int, seed: int = 20261028) -> list[dict]:
    if not 20 <= count <= 50:
        raise ValueError("The paired suite must contain 20 to 50 tasks")
    groups = {}
    for row in dataset:
        groups.setdefault(row["repo"], []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: digest(f"{seed}:{row['instance_id']}".encode()))
    selected = []
    while len(selected) < count:
        added = False
        for repo in sorted(groups):
            if groups[repo] and len(selected) < count:
                selected.append(groups[repo].pop(0))
                added = True
        if not added:
            raise ValueError("Not enough dataset tasks")
    return selected


def freeze(dataset_path: Path, manifest_path: Path, count=30):
    raw = dataset_path.read_bytes()
    rows = select_tasks(json.loads(raw), count)
    manifest = {"schema_version": 1, "seed": 20261028, "dataset_sha256": digest(raw), "tasks": [{k: r[k] for k in ("instance_id", "repo", "base_commit")} for r in rows], "settings": {"max_steps": 40, "budget_tokens": 80000, "max_tokens": 8192, "temperature": 0, "multi_agent": False, "models": ["mimo-v2.6-flash", "mimo-v2.6-pro"]}}
    manifest_path.write_bytes((json.dumps(manifest, indent=2) + "\n").encode())
    return manifest


def load_suite(dataset_path: Path, manifest_path: Path):
    raw = dataset_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if digest(raw) != manifest["dataset_sha256"]:
        raise ValueError("Dataset fingerprint differs from the frozen manifest")
    by_id = {r["instance_id"]: r for r in json.loads(raw)}
    rows = []
    for task in manifest["tasks"]:
        row = by_id[task["instance_id"]]
        if any(row[k] != task[k] for k in ("repo", "base_commit")):
            raise ValueError("Task metadata differs from the frozen manifest")
        rows.append(row)
    return manifest, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("swebench_lite.json"))
    parser.add_argument("--manifest", type=Path, default=Path("evaluation/swebench30.json"))
    parser.add_argument("--count", type=int, default=30)
    args = parser.parse_args()
    if args.manifest.exists():
        parser.error("Manifest already exists; choose a new path to avoid changing a frozen experiment")
    freeze(args.dataset, args.manifest, args.count)


if __name__ == "__main__":
    main()
