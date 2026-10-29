"""Incremental repository definitions and a conservative static reference graph."""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", "dist", "build"}
SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".cpp", ".c", ".rb"}


@dataclass
class Definition:
    name: str
    qualified_name: str
    kind: str
    line: int
    signature: str


@dataclass
class FileRecord:
    path: str
    size: int
    definitions: list[Definition] = field(default_factory=list)
    imports: dict = field(default_factory=dict)
    references: Counter = field(default_factory=Counter)
    attributes: Counter = field(default_factory=Counter)
    parse_status: str = "metadata"


def repository_files(root: Path):
    """Git owns ignore semantics; untracked, non-ignored source is included."""
    try:
        proc = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                              cwd=root, capture_output=True, timeout=15)
        if proc.returncode == 0:
            names = sorted(set(os.fsdecode(n) for n in proc.stdout.split(b"\0") if n))
            return [root / name for name in names if not set(Path(name).parts) & SKIP_DIRS]
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Non-Git directories: support nested ignore patterns and directory pruning.
    files = []
    def walk(folder, inherited):
        patterns = list(inherited)
        ignore = folder / ".gitignore"
        if ignore.is_file():
            for line in ignore.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip() and not line.startswith("#"):
                    patterns.append((folder, line.strip()))
        def ignored(path):
            excluded = False
            for base, raw in patterns:
                negated = raw.startswith("!")
                pattern = raw.lstrip("!").lstrip("/").rstrip("/")
                relative = path.relative_to(base).as_posix()
                match = fnmatch.fnmatchcase(relative, pattern) if "/" in pattern or raw.lstrip("!").startswith("/") else any(fnmatch.fnmatchcase(p, pattern) for p in Path(relative).parts)
                if match:
                    excluded = not negated
            return excluded
        for path in sorted(folder.iterdir()):
            if path.name in SKIP_DIRS or ignored(path):
                continue
            if path.is_dir() and not path.is_symlink():
                walk(path, patterns)
            elif path.is_file():
                files.append(path)
    if root.is_dir():
        walk(root, [])
    return files


def module_name(path):
    parts = list(Path(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def parse_python(content, record):
    tree = ast.parse(content)
    def definitions(node, parents=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = ".".join((*parents, child.name))
                if isinstance(child, ast.ClassDef):
                    signature = "class " + child.name + "(" + ", ".join(ast.unparse(b) for b in child.bases) + ")"
                    kind = "class"
                else:
                    kind = "method" if parents else "function"
                    signature = ("async " if isinstance(child, ast.AsyncFunctionDef) else "") + "def " + qualified + "(" + ast.unparse(child.args) + ")"
                    if child.returns:
                        signature += " -> " + ast.unparse(child.returns)
                record.definitions.append(Definition(child.name, qualified, kind, child.lineno, signature[:300]))
                definitions(child, (*parents, child.name))
            else:
                definitions(child, parents)
    definitions(tree)
    package = module_name(record.path).split(".")
    if not record.path.endswith("__init__.py"):
        package.pop()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record.imports[alias.asname or alias.name.split(".")[0]] = (alias.name, None)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[:len(package) - node.level + 1] if node.level else []
            mod = ".".join([*prefix, *([node.module] if node.module else [])])
            for alias in node.names:
                if alias.name != "*":
                    record.imports[alias.asname or alias.name] = (mod, alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            record.references[node.id] += 1
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            record.attributes[(node.value.id, node.attr)] += 1
    record.parse_status = "python_ast"


class RepoIndex:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files = {}
        self.edges = {}
        self.version = ""
        self._cache = {}
        self.parse_count = 0
        self.excluded_paths = set()

    def refresh(self):
        records, keys = {}, []
        for absolute in repository_files(self.root):
            try:
                if not absolute.is_file() or not absolute.resolve().is_relative_to(self.root):
                    continue
                path = absolute.relative_to(self.root).as_posix()
                if any(path == p or path.startswith(p + "/") for p in self.excluded_paths):
                    continue
                size = absolute.stat().st_size
                is_source = absolute.suffix.lower() in SOURCE_EXTENSIONS and size <= 500_000
                content = absolute.read_bytes() if is_source else b""
                fingerprint = hashlib.sha256(content).hexdigest() if is_source else str(size)
                key = (path, fingerprint)
                record = self._cache.get(key)
                if record is None:
                    record = FileRecord(path, size)
                    if is_source:
                        self._parse(content.decode("utf-8-sig", errors="replace"), record)
                    elif size > 500_000:
                        record.parse_status = "large_file_metadata"
                    self._cache[key] = record
                records[path] = record
                keys.append(key)
            except (OSError, ValueError):
                continue
        version = hashlib.sha256(repr(keys).encode()).hexdigest()
        if version != self.version:
            self.files = records
            self.edges = self._graph(records)
            self.version = version
            self._cache = {key: self._cache[key] for key in keys}
        return self.version

    def _parse(self, content, record):
        self.parse_count += 1
        if record.path.endswith(".py"):
            try:
                parse_python(content, record)
                return
            except (SyntaxError, ValueError, RecursionError):
                pass
        from context.repo_map import _extract_symbols
        record.definitions = [Definition(s.name, s.name, s.kind, s.line, f"{s.kind} {s.name}")
                              for s in _extract_symbols(content, Path(record.path), Path(record.path).suffix)]
        record.parse_status = "definitions_only"

    @staticmethod
    def _graph(records):
        modules = {module_name(p): p for p in records if p.endswith(".py")}
        modules.update({name[4:]: path for name, path in list(modules.items()) if name.startswith("src.")})
        definitions = defaultdict(set)
        for path, record in records.items():
            for definition in record.definitions:
                definitions[definition.name].add(path)
        edges = {path: Counter() for path in records}
        for path, record in records.items():
            for alias, (mod, symbol) in record.imports.items():
                # `from pkg import module` can refer to a submodule, not an attribute.
                target = modules.get(mod + "." + symbol) if symbol else None
                target = target or modules.get(mod)
                if target and target != path:
                    edges[path][target] += 1 + (record.references[alias] ** 0.5)
            local = {d.name for d in record.definitions}
            for name, count in record.references.items():
                if name in record.imports or name in local or len(name) < 4 or name.startswith("_"):
                    continue
                targets = definitions.get(name, set())
                if len(targets) == 1:
                    target = next(iter(targets))
                    if target != path:
                        # Unqualified matching is deliberately weaker than imports.
                        edges[path][target] += 0.1 * count ** 0.5
        return edges
