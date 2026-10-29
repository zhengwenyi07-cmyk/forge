"""
tools/file_tool.py

文件操作工具：
- file_read:   读取文件全部内容
- file_view:   分窗口查看文件（防止一次读爆上下文）
- file_write:  写入文件（全量覆盖）
- file_edit: 精确唯一替换
- apply_patch: 检查后应用 unified diff

设计原则：
- 支持双模式：本地直接 I/O（默认） / 容器 runtime.exec()（沙箱模式）
- 沙箱模式下，文件读写通过 docker exec 在容器内完成，路径无需映射
- 脚本与参数通过 stdin 传输，不进入宿主机命令行
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import Runtime


# 单次 file_read 最多返回的行数，超出提示用 file_view
MAX_READ_LINES = 500
# file_view 每窗口显示的行数
VIEW_WINDOW_LINES = 100
MAX_VIEW_CHARS = 20_000


def _file_operation(runtime, operation: str, params: dict) -> ToolResult:
    from tools import file_ops
    try:
        params = dict(params)
        if runtime is not None and operation in ("edit_file", "write_file"):
            root = getattr(runtime, "_workdir", None)
            if root is None and getattr(runtime, "repo_path", None):
                root = Path(runtime.repo_path).resolve()
            if root:
                params["workspace_root"] = str(root)
        if runtime is None:
            return ToolResult(success=True, output=getattr(file_ops, operation)(**params))
        source = Path(file_ops.__file__).read_text(encoding="utf-8")
        payload = json.dumps(params, ensure_ascii=True)
        script = source + "\nimport json\nprint(" + operation + "(**json.loads(" + repr(payload) + ")))\n"
        result = runtime.exec_python(script, timeout=10)
        return ToolResult(success=result.success, output=result.stdout, error=result.stderr if not result.success else None)
    except (OSError, ValueError, TypeError) as exc:
        return ToolResult(success=False, output="", error=str(exc))


# ===========================================================================
# FileReadTool — 读取文件
# ===========================================================================

class FileReadTool(BaseTool):
    read_only = True

    """
    读取文件内容。超过 MAX_READ_LINES 行时截断并提示。

    params:
        path (str): 文件路径（相对或绝对）

    Args:
        runtime: 传 runtime 时走容器（沙箱模式），None 则走本地直接 I/O
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            f"Read the contents of a file. "
            f"Files longer than {MAX_READ_LINES} lines will be truncated; "
            f"use file_view with line numbers to read specific sections."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (absolute or relative to repo root)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = params.get("path", "")
        result = _file_operation(self._runtime, "read_window", {"path": path, "start_line": 1, "window_size": MAX_READ_LINES})
        if not result.success:
            return result
        data = json.loads(result.output)
        return _format_file_content(path, data["lines"], MAX_READ_LINES, data["total"])




# ===========================================================================
# FileViewTool — 分页查看文件
# ===========================================================================

class FileViewTool(BaseTool):
    read_only = True

    """
    分窗口查看文件，每次返回 VIEW_WINDOW_LINES 行。

    params:
        path (str):       文件路径
        start_line (int): 从第几行开始（1-indexed，默认 1）

    Args:
        runtime: 传 runtime 时走容器，None 则走本地直接 I/O
    """

    def __init__(self, runtime: Runtime | None = None, max_view_lines: int = 100) -> None:
        self._runtime = runtime
        self._window = max(1, max_view_lines)

    @property
    def name(self) -> str:
        return "file_view"

    @property
    def description(self) -> str:
        return (
            f"View a specific section of a file, {VIEW_WINDOW_LINES} lines at a time. "
            f"Use start_line to scroll through large files. Lines are 1-indexed."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file",
                },
                "start_line": {
                    "type": "integer",
                    "description": f"First line to show (1-indexed, default 1)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = params.get("path", "")
        start_line = params.get("start_line", 1)
        if type(start_line) is not int or start_line < 1:
            return ToolResult(success=False, output="", error="start_line must be an integer >= 1")
        result = _file_operation(self._runtime, "read_window", {"path": path, "start_line": start_line, "window_size": self._window})
        if not result.success:
            return result
        data = json.loads(result.output)
        return _format_view(path, data["lines"], start_line, self._window, data["total"])




# ===========================================================================
# FileWriteTool — 写入文件
# ===========================================================================

class FileWriteTool(BaseTool):
    """
    写入文件（全量覆盖）。自动创建父目录。

    params:
        path (str):    文件路径
        content (str): 要写入的内容

    Args:
        runtime: 传 runtime 时走容器，None 则走本地直接 I/O
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file, replacing its entire contents. "
            "Parent directories are created automatically. "
            "Always read the file first before writing to avoid losing existing content."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = params.get("path", "")
        content = params.get("content", "")

        return _file_operation(self._runtime, "write_file", {"path": path, "content": content})




# ===========================================================================
# 内部辅助：格式化输出
# ===========================================================================

def _format_file_content(path: str, lines: list[str], max_lines: int, total: int | None = None) -> ToolResult:
    """格式化文件内容：加行号、截断提示。"""
    total = len(lines) if total is None else total
    truncated = total > max_lines
    display_lines = lines[:max_lines]

    numbered = "\n".join(
        f"{i + 1:4d} | {line}"
        for i, line in enumerate(display_lines)
    )

    suffix = ""
    if truncated:
        suffix = (
            f"\n... ({total - max_lines} more lines not shown) "
            f"Use file_view with start_line to read the rest."
        )

    return ToolResult(
        success=True,
        output=_bound_content(f"File: {path} ({total} lines total)\n{numbered}{suffix}"),
    )


def _format_view(
    path: str,
    window_lines: list[str],
    start_line: int,
    window_size: int,
    total_lines: int | None = None,
) -> ToolResult:
    """格式化分页查看输出。"""
    if total_lines is None:
        total_lines = start_line + len(window_lines) - 1
    if total_lines == 0:
        return ToolResult(success=True, output=f"File: {path} (0 lines). End of file.")

    assert len(window_lines) <= window_size, "file_view received more than one window"
    numbered = "\n".join(
        f"{start_line + i:4d} | {line}"
        for i, line in enumerate(window_lines)
    )

    end_line = start_line + len(window_lines) - 1
    nav = ""
    if end_line < total_lines:
        nav = (
            f"\n[Lines {start_line}–{end_line} of {total_lines}. "
            f"Next: file_view path={path} start_line={end_line + 1}]"
        )
    else:
        nav = f"\n[Lines {start_line}–{end_line} of {total_lines}. End of file.]"

    return ToolResult(success=True, output=_bound_content(numbered + nav))


def _bound_content(content: str) -> str:
    if len(content) <= MAX_VIEW_CHARS:
        return content
    return content[:MAX_VIEW_CHARS - 100] + "\n[Content truncated by character limit; request a smaller section.]"


class FileEditTool(BaseTool):
    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime

    @property
    def name(self):
        return "file_edit"

    @property
    def description(self):
        return "Precisely replace one unique old_string with new_string. Read the file first. Ambiguous or missing matches make no changes."

    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string", "minLength": 1},
            "new_string": {"type": "string"}, "expected_sha256": {"type": "string"},
        }, "required": ["path", "old_string", "new_string"]}

    def execute(self, params):
        return _file_operation(self._runtime, "edit_file", params)


class ApplyPatchTool(BaseTool):
    def __init__(self, runtime: Runtime | None = None, artifact_root=None) -> None:
        from tools.runtime import LocalRuntime
        self._runtime = runtime or LocalRuntime()
        self._artifact_root = Path(artifact_root or "logs/artifacts").resolve()

    @property
    def name(self):
        return "apply_patch"

    @property
    def description(self):
        return "Apply a unified git diff after git apply --check. Supply patch OR artifact_task_id to apply a durable child patch without retyping it. Paths are relative to the repository root; conflicts make no changes."

    @property
    def parameters_schema(self):
        return {"type": "object", "properties": {
            "patch": {"type": "string", "minLength": 1},
            "artifact_task_id": {"type": "string", "minLength": 1},
        }, "additionalProperties": False}

    def execute(self, params):
        import hashlib
        import re
        if bool(params.get("patch")) == bool(params.get("artifact_task_id")):
            return ToolResult(False, "", "Supply exactly one of patch or artifact_task_id")
        patch = params.get("patch")
        if patch is None:
            task_id = params["artifact_task_id"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                return ToolResult(False, "", "Invalid artifact task id")
            manifest = self._artifact_root / task_id / "result.json"
            if not manifest.resolve().is_relative_to(self._artifact_root):
                return ToolResult(False, "", "Artifact is outside the configured store")
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                patch = payload["result"]["patch"]
                if not patch or hashlib.sha256(patch.encode("utf-8")).hexdigest() != payload["patch_sha256"]:
                    return ToolResult(False, "", "Artifact patch checksum mismatch or empty patch")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return ToolResult(False, "", f"Cannot load child artifact: {exc}")
        encoded = base64.b64encode(patch.encode("utf-8")).decode("ascii")
        script = (
            "import base64,subprocess,sys\n"
            f"patch=base64.b64decode('{encoded}')\n"
            "for args in [['git','apply','--check','--whitespace=nowarn','-'],['git','apply','--whitespace=nowarn','-']]:\n"
            " r=subprocess.run(args,input=patch,capture_output=True)\n"
            " if r.returncode:\n"
            "  sys.stderr.buffer.write(r.stderr); sys.exit(r.returncode)\n"
            "print('Patch applied successfully')\n"
        )
        result = self._runtime.exec_python(script, timeout=30)
        return ToolResult(success=result.success, output=result.stdout, error=result.stderr if not result.success else None)
