"""Verify predictions with the official SWE-bench harness inside Linux Docker."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from evaluation.verification import bind_run, json_bytes, validate_binding


def validate_reports(output: Path, run_id: str, instance_ids: set[str], expected_predictions: list | None = None) -> dict:
    predictions = validate_binding(output, run_id)
    if expected_predictions is not None and json_bytes(sorted(predictions, key=lambda row: row["instance_id"])) != json_bytes(sorted(expected_predictions, key=lambda row: row["instance_id"])):
        raise RuntimeError("Predictions differ from the evaluated input binding")
    if {p["instance_id"] for p in predictions} != instance_ids:
        raise RuntimeError("Report instance IDs differ from the bound evaluation inputs")
    reports = list(output.glob(f"*.{run_id}.json"))
    if len(reports) != 1:
        raise RuntimeError(f"Expected one official run report, found {len(reports)}")
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    if report.get("error_ids") or report.get("incomplete_ids"):
        raise RuntimeError("Harness infrastructure errors: " + json.dumps(report))
    completed = set(report.get("completed_ids", [])) | set(report.get("empty_patch_ids", []))
    if not instance_ids <= completed:
        raise RuntimeError(f"Missing evaluated instances: {sorted(instance_ids - completed)}")
    for prediction in predictions:
        if prediction["model_patch"].strip():
            model = prediction["model_name_or_path"].replace("/", "__")
            log = output / "logs" / "run_evaluation" / run_id / model / prediction["instance_id"]
            if not (log / "test_output.txt").is_file() or not (log / "report.json").is_file():
                raise RuntimeError(f"Missing per-instance test evidence: {prediction['instance_id']}")
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
    parser.add_argument("--image-lock", type=Path, help="Map instance IDs to immutable Docker image IDs")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
    ids = {p["instance_id"] for p in predictions}
    if not ids or len(ids) != len(predictions):
        parser.error("Predictions must be nonempty and instance IDs must be unique")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if args.build:
        subprocess.run(["docker", "build", "-f", "docker/Dockerfile.harness", "-t", args.image, "."], cwd=root, check=True)
    def image_id(image):
        found = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, text=True)
        if found.returncode:
            subprocess.run(["docker", "pull", image], check=True)
            return subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
        return found.stdout.strip()
    harness_image = image_id(args.image)
    if args.image_lock:
        lock = json.loads(args.image_lock.read_text(encoding="utf-8"))
        images = {instance_id: image_id(lock[instance_id]) for instance_id in ids}
    else:
        with tempfile.TemporaryDirectory(prefix="forge-specs-") as temp:
            spec_dir = Path(temp)
            (spec_dir / "dataset.json").write_bytes(json_bytes([row for row in dataset if row["instance_id"] in ids]))
            subprocess.run(["docker", "run", "--rm", "-v", f"{spec_dir}:/suite", harness_image, "python", "-m", "evaluation.build_specs", "/suite/dataset.json", "/suite/specs.json"], check=True)
            specs = json.loads((spec_dir / "specs.json").read_text(encoding="utf-8"))
            images = {instance_id: image_id(specs[instance_id]["image"]) for instance_id in ids}
    inputs = bind_run(output, args.run_id, predictions, dataset, harness_image, images, args.timeout)
    container_inputs = "/results/" + inputs.relative_to(output).as_posix()
    command = [
        "docker", "run", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{output}:/results", "-w", "/results", harness_image,
        "python", "-m", "evaluation.harness_entry",
        "--image-lock", container_inputs + "/images.json",
        "--dataset_name", container_inputs + "/dataset.json",
        "--predictions_path", container_inputs + "/predictions.json",
        "--run_id", args.run_id, "--max_workers", str(args.max_workers),
        "--timeout", str(args.timeout), "--report_dir", "/results",
    ]
    subprocess.run(command, check=True)
    report = validate_reports(output, args.run_id, ids)
    print(json.dumps({"run_id": args.run_id, "evaluated": len(ids), "resolved": report.get("resolved_ids", [])}, indent=2))


if __name__ == "__main__":
    main()
