import json

import pytest

from evaluation.harness_entry import install_image_lock
from evaluation.verification import bind_run
from run_verification import validate_reports

IMAGE = "sha256:" + "1" * 64
INSTANCE_IMAGE = "sha256:" + "2" * 64


def inputs():
    return [{"instance_id": "a", "model_name_or_path": "model", "model_patch": "diff original"}], [{"instance_id": "a", "base_commit": "base"}]


def bind(output, predictions=None, dataset=None, image=IMAGE, instance_image=INSTANCE_IMAGE):
    original_predictions, original_dataset = inputs()
    return bind_run(output, "paired", predictions or original_predictions, dataset or original_dataset, image, {"a": instance_image}, 1800)


@pytest.mark.parametrize("changed", ["patch", "dataset", "harness", "instance"])
def test_changed_inputs_cannot_reuse_cached_scores(tmp_path, changed):
    bind(tmp_path)
    predictions, dataset = inputs()
    kwargs = {}
    if changed == "patch": predictions[0]["model_patch"] = "INVALID PATCH"
    if changed == "dataset": dataset[0]["base_commit"] = "different"
    if changed == "harness": kwargs["image"] = "sha256:" + "3" * 64
    if changed == "instance": kwargs["instance_image"] = "sha256:" + "4" * 64
    with pytest.raises(RuntimeError, match="inputs changed"):
        bind(tmp_path, predictions, dataset, **kwargs)
    assert json.loads((tmp_path / "inputs/paired/predictions.json").read_text())[0]["model_patch"] == "diff original"


def test_same_inputs_resume_and_reordered_json_is_equivalent(tmp_path):
    root = bind(tmp_path)
    assert bind(tmp_path) == root
    assert (root / "binding.json").is_file()


def test_legacy_reports_without_binding_are_rejected(tmp_path):
    (tmp_path / "model.paired.json").write_text(json.dumps({"completed_ids": ["a"], "resolved_ids": ["a"]}))
    with pytest.raises(RuntimeError, match="Unbound legacy"):
        bind(tmp_path)
    with pytest.raises(RuntimeError, match="no evaluation input binding"):
        validate_reports(tmp_path, "paired", {"a"})


def test_tampered_inputs_and_missing_test_evidence_are_rejected(tmp_path):
    root = bind(tmp_path)
    (tmp_path / "model.paired.json").write_text(json.dumps({"completed_ids": ["a"], "resolved_ids": ["a"]}))
    with pytest.raises(RuntimeError, match="per-instance test evidence"):
        validate_reports(tmp_path, "paired", {"a"})
    log = tmp_path / "logs/run_evaluation/paired/model/a"
    log.mkdir(parents=True)
    (log / "test_output.txt").write_text("tests ran")
    (log / "report.json").write_text(json.dumps({"a": {"resolved": True}}))
    assert validate_reports(tmp_path, "paired", {"a"})["resolved_ids"] == ["a"]
    with pytest.raises(RuntimeError, match="Predictions differ"):
        validate_reports(tmp_path, "paired", {"a"}, expected_predictions=[{"instance_id": "a", "model_name_or_path": "model", "model_patch": "WRONG"}])
    (root / "predictions.json").write_text("[]")
    with pytest.raises(RuntimeError, match="modified"):
        validate_reports(tmp_path, "paired", {"a"})


def test_instance_image_lock_is_required_for_every_test_spec():
    class Spec:
        instance_id = "a"
    install_image_lock(Spec, {"a": INSTANCE_IMAGE})
    assert Spec().instance_image_key == INSTANCE_IMAGE
    Spec.instance_id = "b"
    with pytest.raises(RuntimeError, match="Missing immutable image"):
        _ = Spec().instance_image_key
