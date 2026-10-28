"""Verify predictions with the official SWE-bench harness inside Linux Docker."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def validate_reports(output: Path, run_id: str, instance_ids: set[str]) -> dict:
    reports = list(output.glob(f"*.{run_id}.json"))
    if len(reports) != 1:
        raise RuntimeError(f"Expected one official run report, found {len(reports)}")
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    if report.get("error_ids") or report.get("incomplete_ids"):
        raise RuntimeError("Harness infrastructure errors: " + json.dumps(report))
    completed = set(report.get("completed_ids", [])) | set(report.get("empty_patch_ids", []))
    if not instance_ids <= completed:
        raise RuntimeError(f"Missing evaluated instances: {sorted(instance_ids - completed)}")
    for log in (output / "logs" / "run_evaluation" / run_id).rglob("test_output.txt"):
        text = log.read_text(encoding="utf-8", errors="replace")
        if "$'\\r': command not found" in text or "invalid option" in text and "\\r" in text:
            raise RuntimeError(f"CRLF infrastructure failure in {log}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("swebench_lite.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="forge-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--image", default="forge-swebench:4.1.0")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
    ids = {p["instance_id"] for p in predictions}
    if not ids or len(ids) != len(predictions):
        parser.error("Predictions must be nonempty and instance IDs must be unique")
    (output / "predictions.json").write_bytes(json.dumps(predictions).encode("utf-8"))
    (output / "dataset.json").write_bytes(args.dataset.read_bytes())
    if args.build:
        subprocess.run(["docker", "build", "-f", "docker/Dockerfile.harness", "-t", args.image, "."], cwd=root, check=True)
    command = [
        "docker", "run", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{output}:/results", "-w", "/results", args.image,
        "python", "-m", "evaluation.harness_entry",
        "--dataset_name", "/results/dataset.json",
        "--predictions_path", "/results/predictions.json",
        "--run_id", args.run_id, "--max_workers", str(args.max_workers),
        "--timeout", str(args.timeout), "--report_dir", "/results",
    ]
    subprocess.run(command, check=True)
    report = validate_reports(output, args.run_id, ids)
    print(json.dumps({"run_id": args.run_id, "evaluated": len(ids), "resolved": report.get("resolved_ids", [])}, indent=2))


if __name__ == "__main__":
    main()
