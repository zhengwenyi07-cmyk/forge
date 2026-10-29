"""
tools/search_tool.py

代码搜索工具，三个 action：
- search_text:   在文件内容中搜索字符串（grep 风格）
- find_files:    按文件名 pattern 查找文件
- find_symbol:   使用共享 RepoIndex 查找限定名称和签名；独立使用时保留兼容回退

设计原则：
- 本地模式（无 runtime）：直接用 Python 遍历文件系统
- 容器模式（有 runtime）：搜索脚本经 stdin 在容器内执行
- 结果数量上限防止返回太多内容爆上下文
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import Runtime


MAX_RESULTS = 50        # 单次搜索最多返回的结果数
MAX_LINE_LENGTH = 200   # 单行超长时截断显示


def _index_scope(tool, search_path):
    index = tool._repo_index
    workdir = getattr(tool._runtime, "_workdir", None)
    normalized = search_path.replace("\\", "/")
    if workdir and (normalized == workdir or normalized.startswith(workdir + "/")):
        normalized = normalized[len(workdir):].lstrip("/") or "."
    target = (index.root / normalized).resolve()
    if not target.is_relative_to(index.root):
        raise ValueError("Search path must stay inside the task repository")
    if not target.exists():
        raise ValueError(f"Path not found: {search_path}")
    scope = target.relative_to(index.root).as_posix()
    index.refresh()
    return [(path, record) for path, record in sorted(index.files.items())
            if scope == "." or path == scope or path.startswith(scope + "/")]

# 搜索时跳过的目录
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "*.egg-info",
})


# ===========================================================================
# 辅助函数
# ===========================================================================

def _iter_files(root: Path, glob_pattern: str):
    """
    递归遍历目录，跳过 _SKIP_DIRS，按 glob_pattern 过滤文件名。
    """
    if root.is_file():
        yield root
        return

    for filepath in sorted(root.rglob(glob_pattern)):
        if any(part in _SKIP_DIRS for part in filepath.parts):
            continue
        if filepath.is_file():
            yield filepath


# ===========================================================================
# SearchTextTool — 文本搜索
# ===========================================================================

class SearchTextTool(BaseTool):
    read_only = True

    """
    在 repo 文件中搜索文本，返回匹配行及其上下文。

    params:
        pattern (str):    搜索字符串（支持正则）
        path (str):       搜索范围（文件或目录，默认当前目录）
        file_pattern (str): 只搜索匹配的文件名（如 "*.py"，默认所有文件）
        case_sensitive (bool): 是否区分大小写（默认 True）

    Args:
        runtime: 传 runtime 时走容器，None 则走本地直接 I/O
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "search_text"

    @property
    def description(self) -> str:
        return (
            "Search for a text pattern (regex supported) in files. "
            "Returns matching lines with file path and line number. "
            f"Returns at most {MAX_RESULTS} matches."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: current directory)",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py'). Default: all files",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive search (default true)",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        raw_pattern = params.get("pattern", "")
        search_path = params.get("path", ".")
        file_pattern = params.get("file_pattern", "*")
        case_sensitive = params.get("case_sensitive", True)

        if hasattr(self, "_repo_index"):
            try:
                files = [p for p, _ in _index_scope(self, search_path)]
                args = {"pattern": raw_pattern, "path": search_path, "file_pattern": file_pattern,
                        "case_sensitive": case_sensitive, "_files": files}
                if self._runtime:
                    return _runtime_search(self._runtime, "search_text", args)
                from tools.search_ops import search
                args["_files"] = [str(self._repo_index.root / p) for p in files]
                args["path"] = str(self._repo_index.root)
                return ToolResult(True, search("search_text", args))
            except (OSError, ValueError) as exc:
                return ToolResult(False, "", str(exc))

        if self._runtime:
            return self._execute_via_runtime(
                raw_pattern, search_path, file_pattern, case_sensitive,
            )
        return self._execute_local(
            raw_pattern, search_path, file_pattern, case_sensitive,
        )

    def _execute_local(self, pattern, search_path, file_pattern, case_sensitive):
        """本地模式：直接遍历文件系统。"""
        p = Path(search_path)
        flags = 0 if case_sensitive else re.IGNORECASE

        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(success=False, output="", error=f"Invalid regex: {e}")

        if not p.exists():
            return ToolResult(success=False, output="", error=f"Path not found: {search_path}")

        matches: list[str] = []
        for filepath in _iter_files(p, file_pattern):
            if len(matches) >= MAX_RESULTS:
                break
            try:
                for lineno, line in enumerate(
                    filepath.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if regex.search(line):
                        display_line = line[:MAX_LINE_LENGTH]
                        if len(line) > MAX_LINE_LENGTH:
                            display_line += " ..."
                        matches.append(f"{filepath}:{lineno}: {display_line}")
                        if len(matches) >= MAX_RESULTS:
                            break
            except OSError:
                continue

        return _format_search_results(matches, raw_pattern=pattern)

    def _execute_via_runtime(self, pattern, search_path, file_pattern, case_sensitive):
        return _runtime_search(self._runtime, "search_text", {"pattern": pattern, "path": search_path, "file_pattern": file_pattern, "case_sensitive": case_sensitive})


# ===========================================================================
# FindFilesTool — 文件名搜索
# ===========================================================================

class FindFilesTool(BaseTool):
    read_only = True

    """
    按文件名 pattern 查找文件。

    params:
        pattern (str): glob 风格的文件名 pattern（如 "*.py", "test_*.py"）
        path (str):    搜索根目录（默认当前目录）

    Args:
        runtime: 传 runtime 时走容器，None 则走本地直接 I/O
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return (
            "Find files by name pattern (glob style). "
            "Example: pattern='test_*.py' finds all test files. "
            f"Returns at most {MAX_RESULTS} results."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern for file names (e.g. '*.py', 'conftest.py')",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        pattern = params.get("pattern", "")
        search_path = params.get("path", ".")

        if hasattr(self, "_repo_index"):
            try:
                matches = [p for p, _ in _index_scope(self, search_path)
                           if fnmatch.fnmatch(Path(p).name, pattern) or Path(p).match(pattern)]
                return ToolResult(True, "\n".join(matches[:MAX_RESULTS]) or f"No files found matching '{pattern}'")
            except (OSError, ValueError) as exc:
                return ToolResult(False, "", str(exc))

        if self._runtime:
            return self._execute_via_runtime(pattern, search_path)
        return self._execute_local(pattern, search_path)

    def _execute_local(self, pattern, search_path):
        """本地模式：用 Path.rglob 遍历。"""
        p = Path(search_path)
        if not p.exists():
            return ToolResult(success=False, output="", error=f"Path not found: {search_path}")

        results: list[str] = []
        for filepath in _iter_files(p, pattern):
            results.append(str(filepath))
            if len(results) >= MAX_RESULTS:
                break

        if not results:
            return ToolResult(
                success=True, output=f"No files found matching '{pattern}' in {search_path}",
            )

        suffix = ""
        if len(results) == MAX_RESULTS:
            suffix = f"\n[Showing first {MAX_RESULTS} results]"

        return ToolResult(success=True, output="\n".join(results) + suffix)

    def _execute_via_runtime(self, pattern, search_path):
        return _runtime_search(self._runtime, "find_files", {"pattern": pattern, "path": search_path})


# ===========================================================================
# FindSymbolTool — 符号查找
# ===========================================================================

class FindSymbolTool(BaseTool):
    read_only = True

    """
    在 Python 文件中查找函数/类定义。
    用正则匹配 def / class 语句，Day 5 可替换为 tree-sitter 精确实现。

    params:
        symbol (str): 函数名或类名（支持部分匹配）
        path (str):   搜索根目录（默认当前目录）

    Args:
        runtime: 传 runtime 时走容器，None 则走本地直接 I/O
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime

    @property
    def name(self) -> str:
        return "find_symbol"

    @property
    def description(self) -> str:
        return (
            "Find function, class and method definitions, including qualified names such as Agent.run. "
            "Uses the shared repository index during agent runs; returns paths, line numbers and signatures. "
            "Supports partial name matching."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Function or class name to find (partial match supported)",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
            "required": ["symbol"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        symbol = params.get("symbol", "")
        search_path = params.get("path", ".")

        if not symbol:
            return ToolResult(success=False, output="", error="symbol is required")

        if hasattr(self, "_repo_index"):
            try:
                matches = [f"{path}:{definition.line}: {definition.signature} ({definition.kind})"
                           for path, record in _index_scope(self, search_path) for definition in record.definitions
                           if symbol in definition.qualified_name]
                return ToolResult(True, "\n".join(matches[:MAX_RESULTS]) or f"No definition found for '{symbol}'")
            except (OSError, ValueError) as exc:
                return ToolResult(False, "", str(exc))

        if self._runtime:
            return self._execute_via_runtime(symbol, search_path)
        return self._execute_local(symbol, search_path)

    def _execute_local(self, symbol, search_path):
        """本地模式：直接遍历 .py 文件。"""
        p = Path(search_path)
        if not p.exists():
            return ToolResult(success=False, output="", error=f"Path not found: {search_path}")

        pattern = re.compile(
            rf"^(\s*)(def|class)\s+({re.escape(symbol)}\w*)\s*[:(]",
            re.MULTILINE,
        )

        matches: list[str] = []
        for filepath in _iter_files(p, "*.py"):
            if len(matches) >= MAX_RESULTS:
                break
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
                for m in pattern.finditer(content):
                    lineno = content[: m.start()].count("\n") + 1
                    kind = m.group(2)
                    name = m.group(3)
                    indent = len(m.group(1))
                    scope = "method" if indent > 0 else "top-level"
                    matches.append(f"{filepath}:{lineno}: {kind} {name} ({scope})")
                    if len(matches) >= MAX_RESULTS:
                        break
            except OSError:
                continue

        if not matches:
            return ToolResult(success=True, output=f"No definition found for '{symbol}'")

        return ToolResult(success=True, output="\n".join(matches))

    def _execute_via_runtime(self, symbol, search_path):
        return _runtime_search(self._runtime, "find_symbol", {"symbol": symbol, "path": search_path})


# ===========================================================================
# 内部辅助
# ===========================================================================

def _format_search_results(matches: list[str], raw_pattern: str = "") -> ToolResult:
    """格式化搜索结果，加尾部计数提示。"""
    if not matches:
        return ToolResult(success=True, output=f"No matches found for '{raw_pattern}'")

    suffix = f"\n[Showing {len(matches)} matches]"
    if len(matches) == MAX_RESULTS:
        suffix = f"\n[Showing first {MAX_RESULTS} matches, there may be more]"

    return ToolResult(success=True, output="\n".join(matches) + suffix)


def _runtime_search(runtime, operation, params):
    from tools import search_ops
    source = Path(search_ops.__file__).read_text(encoding="utf-8")
    script = source + "\nimport json\nprint(search(" + repr(operation) + ", json.loads(" + repr(json.dumps(params)) + ")))\n"
    result = runtime.exec_python(script, timeout=30)
    return ToolResult(success=result.success, output=result.stdout, error=result.stderr if not result.success else None)
