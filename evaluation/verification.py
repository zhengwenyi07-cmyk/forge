"""Bind cached harness reports to immutable evaluation inputs."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()


def bind_run(output: Path, run_id: str, predictions: list, dataset: list, harness_image: str, images: dict, timeout: int) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("run_id may contain only letters, digits, underscores and hyphens")
    ids = {p["instance_id"] for p in predictions}
    if not ids or len(ids) != len(predictions) or not ids <= {r["instance_id"] for r in dataset}:
        raise ValueError("Predictions need unique IDs present in the dataset")
    if len({p["model_name_or_path"] for p in predictions}) != 1:
        raise ValueError("A verification run must contain one model")
    if set(images) != ids or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in [harness_image, *images.values()]):
        raise ValueError("Evaluation requires an immutable image ID for the harness and every instance")
    inputs = {
        "predictions.json": json_bytes(sorted(predictions, key=lambda row: row["instance_id"])),
        "dataset.json": json_bytes(sorted(dataset, key=lambda row: row["instance_id"])),
        "images.json": json_bytes(images),
    }
    wrapper = Path(__file__).with_name("harness_entry.py").read_bytes()
    identity = {"schema_version": 1, "run_id": run_id, "harness_image": harness_image, "wrapper_sha256": sha256(wrapper), "timeout": timeout, "inputs": {name: sha256(data) for name, data in inputs.items()}}
    root = output / "inputs" / run_id
    binding = root / "binding.json"
    if binding.exists():
        if json.loads(binding.read_text(encoding="utf-8")) != identity:
            raise RuntimeError("Evaluation inputs changed. Use a new run ID or output directory; old scores cannot be reused.")
        for name, data in inputs.items():
            if not (root / name).exists() or (root / name).read_bytes() != data:
                raise RuntimeError(f"Bound evaluation input was modified: {name}")
        return root
    if (output / "logs" / "run_evaluation" / run_id).exists() or list(output.glob(f"*.{run_id}.json")):
        raise RuntimeError("Unbound legacy reports exist for this run ID. Use a new run ID or output directory.")
    root.mkdir(parents=True, exist_ok=True)
    for name, data in inputs.items():
        (root / name).write_bytes(data)
    binding.write_bytes(json_bytes(identity))
    return root


def validate_binding(output: Path, run_id: str):
    root = output / "inputs" / run_id
    binding = root / "binding.json"
    if not binding.is_file():
        raise RuntimeError("Report has no evaluation input binding; rerun with a new run ID")
    identity = json.loads(binding.read_text(encoding="utf-8"))
    for name in ("predictions.json", "dataset.json", "images.json"):
        if not (root / name).is_file() or sha256((root / name).read_bytes()) != identity["inputs"][name]:
            raise RuntimeError(f"Bound evaluation input was modified: {name}")
    return json.loads((root / "predictions.json").read_text(encoding="utf-8"))
