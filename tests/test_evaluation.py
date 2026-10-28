import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.harness_entry import install_copy_guard, normalize_script
from run_verification import validate_reports


def test_crlf_is_removed_before_container_copy(tmp_path):
    script = tmp_path / "eval.sh"
    script.write_bytes(b"#!/bin/bash\r\nset -e\r\necho ok\r\n")
    copied = []
    module = SimpleNamespace(copy_to_container=lambda c, s, d: copied.append(Path(s).read_bytes()))
    install_copy_guard(module)
    module.copy_to_container(None, script, Path("/eval.sh"))
    assert copied == [b"#!/bin/bash\nset -e\necho ok\n"]


def test_lf_guard_rejects_bare_cr_and_preserves_patch(tmp_path):
    script = tmp_path / "eval.sh"
    script.write_bytes(b"echo\rbad\n")
    with pytest.raises(ValueError, match="Bare CR"):
        normalize_script(script)
    patch = tmp_path / "fix.patch"
    patch.write_bytes(b"unchanged\r\n")
    normalize_script(patch)
    assert patch.read_bytes() == b"unchanged\r\n"


def test_infrastructure_failure_is_not_zero_score(tmp_path):
    report = tmp_path / "model.smoke.json"
    report.write_text(json.dumps({"completed_ids": [], "error_ids": ["a"]}))
    with pytest.raises(RuntimeError, match="infrastructure"):
        validate_reports(tmp_path, "smoke", {"a"})
    report.write_text(json.dumps({"completed_ids": ["a"], "resolved_ids": [], "error_ids": []}))
    assert validate_reports(tmp_path, "smoke", {"a"})["resolved_ids"] == []
