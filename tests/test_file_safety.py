import hashlib
import re

import pytest

from tools.file_tool import FileViewTool, FileEditTool, FileWriteTool, MAX_VIEW_CHARS


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_view_pagination_content_and_numbers(tmp_path, newline):
    path = tmp_path / "quoted ' name.py"
    path.write_bytes(newline.join(f"line-{i}".encode() for i in range(1, 251)))
    result = FileViewTool().execute({"path": str(path), "start_line": 101})
    lines = re.findall(r"^\s*(\d+) \| (.*)$", result.output, flags=re.M)
    assert result.success
    assert lines == [(str(i), f"line-{i}") for i in range(101, 201)]
    assert "of 250" in result.output


@pytest.mark.parametrize("start", [0, -1, True, "10", 999])
def test_invalid_window_returns_error(tmp_path, start):
    p = tmp_path / "f"
    p.write_text("one\ntwo\n")
    assert not FileViewTool().execute({"path": str(p), "start_line": start}).success


def test_long_line_is_bounded(tmp_path):
    p = tmp_path / "large"
    p.write_text("x" * 1_000_000)
    assert len(FileViewTool().execute({"path": str(p)}).output) <= MAX_VIEW_CHARS


def test_precise_edit_preserves_crlf_and_other_content(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    result = FileEditTool().execute({"path": str(p), "old_string": "beta\ngamma", "new_string": "B\ngamma"})
    assert result.success
    assert p.read_bytes() == b"alpha\r\nB\r\ngamma\r\n"


@pytest.mark.parametrize("old", ["missing", "same", ""])
def test_non_unique_edit_changes_nothing(tmp_path, old):
    p = tmp_path / "f"
    original = b"same\nsame\n"
    p.write_bytes(original)
    assert not FileEditTool().execute({"path": str(p), "old_string": old, "new_string": "new"}).success
    assert p.read_bytes() == original


def test_stale_hash_and_large_overwrite_are_rejected(tmp_path):
    p = tmp_path / "f"
    original = b"line\n" * 501
    p.write_bytes(original)
    assert not FileWriteTool().execute({"path": str(p), "content": "oops"}).success
    assert not FileEditTool().execute({"path": str(p), "old_string": "line", "new_string": "new", "expected_sha256": hashlib.sha256(b"stale").hexdigest()}).success
    assert p.read_bytes() == original
