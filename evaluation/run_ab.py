"""Run paired Flash/Pro trials from a frozen manifest and official instance images."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Task
from config.schema import load_config
from entry.cli import _build_registry
from evaluation.suite import load_suite, digest
from llm.openai_compat import OpenAICompatBackend
from tools.runtime import DockerRuntime
from tools.workspace import capture_patch


def write_json(path, value):
    path.write_bytes((json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))


def code_fingerprint(root: Path):
    git = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    files = {}
    for directory in ("agent", "tools", "context", "llm", "entry", "multi_agent", "evaluation", "config"):
        for path in sorted((root / directory).glob("*.py")):
            files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return {"commit": git, "source_sha256": digest(json.dumps(files, sort_keys=True).encode())}


def run_trial(row, model, cfg, settings, image, repos_dir, output, fingerprint):
    trial = output / model / row["instance_id"]
    trial.mkdir(parents=True, exist_ok=True)
    result_file = trial / "result.json"
    effective = {"context_window": cfg.context.context_window, "repo_map_budget": cfg.context.repo_map_budget,
                 "repo_map_strategy": cfg.context.repo_map_strategy, "endpoint": cfg.llm.base_url,
                 "tools": {"shell_timeout": cfg.tools.shell.timeout, "shell_output": cfg.tools.shell.max_output_tokens,
                           "file_view_lines": cfg.tools.file.max_view_lines}}
    identity = {"model": model, "instance_id": row["instance_id"], "base_commit": row["base_commit"], "image": image, "code": fingerprint, "settings": settings, "effective_config": effective}
    if result_file.exists():
        saved = json.loads(result_file.read_text())
        if saved["identity"] != identity:
            raise RuntimeError(f"Resume fingerprint mismatch: {trial}")
        if saved["status"] != "infrastructure_error":
            return saved
    start = time.monotonic()
    record = {"identity": identity, "status": "infrastructure_error", "patch": "", "tokens": 0, "seconds": 0}
    with tempfile.TemporaryDirectory(prefix="repo-", dir=trial) as temp:
        repo = Path(temp) / "checkout"
        source = repos_dir / row["repo"].replace("/", "_")
        if (source / ".git").exists():
            subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--no-checkout", "--no-hardlinks", str(source), str(repo)], check=True, capture_output=True)
        else:
            # Official images contain the source at base_commit; avoid downloading
            # another full history when a local repository cache is unavailable.
            container = subprocess.check_output(["docker", "create", image], text=True).strip()
            try:
                repo.mkdir()
                subprocess.run(["docker", "cp", f"{container}:/testbed/.", str(repo)], check=True, capture_output=True)
            finally:
                subprocess.run(["docker", "rm", container], check=True, capture_output=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "--detach", row["base_commit"]], cwd=repo, check=True, capture_output=True)
        base_commit = row["base_commit"]
        if capture_patch(str(repo)):
            raise RuntimeError("Trial must start with a clean checkout at base_commit")
        runtime = DockerRuntime(str(repo), image=image, setup_cmds=["git config --global safe.directory /testbed"])
        runtime._workdir = "/testbed"
        runtime._command_prefix = "source /opt/miniconda3/bin/activate testbed"
        runtime._helper_python = "/opt/miniconda3/bin/python"
        try:
            check = runtime.preflight()
            if not check.success:
                raise RuntimeError(check.stderr)
            task = Task(description=row["problem_statement"], repo_path=str(repo), max_steps=settings["max_steps"], budget_tokens=settings["budget_tokens"])
            backend = OpenAICompatBackend(model=model, api_key=cfg.llm.api_key, base_url=cfg.llm.base_url,
                                          headers=cfg.llm.headers, max_tokens=settings["max_tokens"])
            # Single-agent mode fixes the architecture and excludes scheduling differences.
            registry = _build_registry(cfg, runtime=runtime)
            agent = Agent(backend, registry, AgentConfig(budget_tokens=settings["budget_tokens"], history_max_messages=40,
                          context_window=cfg.context.context_window, repo_map_budget=cfg.context.repo_map_budget,
                          repo_map_strategy=cfg.context.repo_map_strategy, max_output_tokens=settings["max_tokens"], stream=False), runtime=runtime)
            with EventLog.create(task, log_dir=str(trial / "logs")) as log:
                result = agent.run(task, log)
            record.update(status="infrastructure_error" if result.error else result.status.value, tokens=result.total_tokens, validation=result.validation, summary=result.summary.replace(cfg.llm.api_key, "[REDACTED]"), patch=capture_patch(str(repo), base_ref=base_commit))
        except Exception as exc:
            # Errors may contain server-provided request details: redact the configured secret.
            record["error"] = str(exc).replace(cfg.llm.api_key, "[REDACTED]")
        finally:
            runtime.cleanup()
    record["seconds"] = round(time.monotonic() - start, 3)
    (trial / "prediction.patch").write_bytes(record.pop("patch").encode("utf-8"))
    write_json(result_file, record)
    print(f"{model} {row['instance_id']}: {record['status']} ({record['seconds']}s)", flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/mimo.local.yaml"))
    parser.add_argument("--dataset", type=Path, default=Path("swebench_lite.json"))
    parser.add_argument("--manifest", type=Path, default=Path("evaluation/swebench30.json"))
    parser.add_argument("--repos-dir", type=Path, default=Path("evaluation/runs/repos"), help="Optional repository cache; otherwise extract the exact source from the official image")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--harness-image", default="forge-swebench:4.1.0")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--map-strategy", choices=("legacy", "lexical", "global", "personalized"), help="Fix one map strategy for both models; use separate output directories for map ablations")
    parser.add_argument("--repo-map-budget", type=int, help="Fix the same map token budget for both models")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--preflight-only", action="store_true", help="Pull images and run the gold smoke without calling either model")
    parser.add_argument("--skip-gold-smoke", action="store_true", help="Only use after a successful gold smoke for these exact images")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest, rows = load_suite(args.dataset, args.manifest)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_bytes = args.manifest.read_bytes()
    if (output / "manifest.json").exists() and (output / "manifest.json").read_bytes() != manifest_bytes:
        raise RuntimeError("Output already belongs to another manifest")
    (output / "manifest.json").write_bytes(manifest_bytes)
    write_json(output / "dataset.json", rows)
    subprocess.run(["docker", "run", "--rm", "-v", f"{output}:/suite", args.harness_image, "python", "-m", "evaluation.build_specs", "/suite/dataset.json", "/suite/specs.json"], check=True)
    specs = json.loads((output / "specs.json").read_text())
    if args.prepare_only:
        print(f"Frozen {len(rows)} tasks; official specs written to {output}")
        return
    # Pull before making any paid model calls. Record immutable image IDs.
    def pull_image(row):
        image = specs[row["instance_id"]]["image"]
        existing = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
        if existing.returncode:
            for attempt in range(3):
                try:
                    subprocess.run(["docker", "pull", image], check=True, timeout=600)
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
        print(f"Image ready: {row['instance_id']} {image_id}", flush=True)
        return row["instance_id"], image_id
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        images = dict(pool.map(pull_image, rows))
    write_json(output / "images.json", images)
    if not args.skip_gold_smoke:
        smoke = rows[:5]
        write_json(output / "gold-dataset.json", smoke)
        write_json(output / "gold-predictions.json", [{"instance_id": r["instance_id"], "model_patch": r["patch"], "model_name_or_path": "gold"} for r in smoke])
        write_json(output / "gold-images.json", {r["instance_id"]: images[r["instance_id"]] for r in smoke})
        subprocess.run([sys.executable, str(root / "run_verification.py"), "--predictions", str(output / "gold-predictions.json"), "--dataset", str(output / "gold-dataset.json"), "--output", str(output / "gold"), "--run-id", "gold-smoke", "--image", args.harness_image, "--image-lock", str(output / "gold-images.json")], check=True)
        from run_verification import validate_reports
        report = validate_reports(output / "gold", "gold-smoke", {r["instance_id"] for r in smoke})
        if set(report["resolved_ids"]) != {r["instance_id"] for r in smoke}:
            raise RuntimeError("Gold smoke did not solve all five tasks; infrastructure must be fixed before A/B")
    if args.preflight_only:
        print("All official images and gold smoke passed; ready for paired model calls.")
        return
    cfg = load_config(args.config)
    if args.map_strategy is not None:
        cfg.context.repo_map_strategy = args.map_strategy
    if args.repo_map_budget is not None:
        if not 0 <= args.repo_map_budget < cfg.context.context_window:
            parser.error("--repo-map-budget must be nonnegative and smaller than context_window")
        cfg.context.repo_map_budget = args.repo_map_budget
    if not cfg.llm.api_key:
        raise RuntimeError("MiMo key is missing; run python -m entry.configure_mimo")
    fingerprint = code_fingerprint(root)
    if (output / "code.json").exists() and json.loads((output / "code.json").read_text()) != fingerprint:
        raise RuntimeError("Code changed since this experiment started; use a new output directory")
    write_json(output / "code.json", fingerprint)
    settings = manifest["settings"]

    def paired(index_row):
        index, row = index_row
        models = settings["models"] if index % 2 == 0 else list(reversed(settings["models"]))
        return [run_trial(row, model, cfg, settings, images[row["instance_id"]], args.repos_dir.resolve(), output, fingerprint) for model in models]

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(paired, enumerate(rows)))
    for model in settings["models"]:
        predictions = []
        records = []
        for row in rows:
            trial = output / model / row["instance_id"]
            record = json.loads((trial / "result.json").read_text())
            if record["status"] == "infrastructure_error":
                raise RuntimeError("A/B has infrastructure errors; resolve them before scoring")
            records.append(record)
            predictions.append({"instance_id": row["instance_id"], "model_patch": (trial / "prediction.patch").read_text(encoding="utf-8"), "model_name_or_path": model})
        write_json(output / f"{model}-predictions.json", predictions)
        subprocess.run([sys.executable, str(root / "run_verification.py"), "--predictions", str(output / f"{model}-predictions.json"), "--dataset", str(output / "dataset.json"), "--output", str(output / model / "verification"), "--run-id", "paired", "--image", args.harness_image, "--image-lock", str(output / "images.json")], check=True)
    from evaluation.summarize import summarize
    write_json(output / "summary.json", summarize(output))


if __name__ == "__main__":
    main()
