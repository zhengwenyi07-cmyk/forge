"""
context/repo_map.py

Repo-map：把整个 repo 的结构压缩成一段摘要字符串，注入 system prompt。

核心思路（简化版 Aider repo-map）：
1. 用 tree-sitter 扫描源码文件，提取函数/类定义
2. 用正则 fallback 处理 tree-sitter 不支持或未安装的语言
3. 按"重要性"排序：顶层定义 > 方法，文件越小越可能是核心文件
4. 按 token 预算截取，生成摘要字符串

## 多语言支持

tree-sitter 每种语言需要单独安装语言包：

    pip install tree-sitter-python       # Python（必装）
    pip install tree-sitter-javascript   # JavaScript
    pip install tree-sitter-typescript   # TypeScript
    pip install tree-sitter-go           # Go
    pip install tree-sitter-rust         # Rust
    pip install tree-sitter-java         # Java

未安装的语言自动降级为正则解析，不报错。
新增语言只需在 _LANG_REGISTRY 里加一行。

调用关系（对应简历第二段"仓库地图生成与上下文注入"）：
  core.py:_build_messages() → RepoMap(task.repo_path)
    → RepoMap.build(budget=token_budget.default_plan().repo_map)
      → _scan() 递归遍历所有文件
        → _extract_symbols(content, filepath, ext)
          → _get_language(ext) → tree-sitter 或正则 fallback
      → 按 importance_score() 降序排列
      → 按 Token 预算截断
      → build_system_prompt(repo_summary=...) 注入 System Prompt

数据流（从文件到 prompt）：
  文件系统 → tree-sitter AST / 正则匹配
    → Symbol(name, kind, line, indent)
      → FileInfo(symbols sorted by importance)
        → 按 token 预算截断的格式化文本
          → build_system_prompt() 的 {repo_summary} 占位符
"""

from __future__ import annotations

# 正则 fallback：_extract_symbols_regex() 使用 _SYMBOL_RE 匹配 def/class/func/fn 关键字
import re
# dataclass：
#   Symbol(name, kind, line, file, indent) — 单个符号
#   FileInfo(path, size, symbols) — 单个文件的元信息
from dataclasses import dataclass, field
# Path：跨平台路径操作。_scan() 中用 rglob("*") 递归遍历，
# build() 中用 relative_to() 获取相对路径
from pathlib import Path

# ===========================================================================
# 语言注册表
# 格式：文件扩展名 → (pip 包名, 模块属性名)
# 运行时按需 import，失败时静默跳过，降级为正则
# 对应简历："支持 Python/JS/TS/Go/Rust/Java/C/C++/Ruby 共 8 种语言"
# 实际上支持 9 种扩展名（含 .tsx），核心实是 8 种语言
# ===========================================================================

# 扩展名 → (tree-sitter Python 包名, 导出属性名)
# 每个语言包通过 pip 安装，importlib.import_module() 动态导入。
# 未安装时 _get_language() 返回 None，调用方降级为正则。
# 新增语言只需在此字典加一行：
#   ".new_ext": ("tree_sitter_newlang", "language")
_LANG_REGISTRY: dict[str, tuple[str, str]] = {
    ".py":  ("tree_sitter_python",     "language"),       # Python
    ".js":  ("tree_sitter_javascript", "language"),        # JavaScript
    ".ts":  ("tree_sitter_typescript", "language_typescript"),  # TypeScript
    ".tsx": ("tree_sitter_typescript", "language_tsx"),        # TypeScript JSX
    ".go":  ("tree_sitter_go",         "language"),       # Go
    ".rs":  ("tree_sitter_rust",       "language"),       # Rust
    ".java":("tree_sitter_java",       "language"),       # Java
    ".cpp": ("tree_sitter_cpp",        "language"),       # C++
    ".c":   ("tree_sitter_c",          "language"),       # C
    ".rb":  ("tree_sitter_ruby",       "language"),       # Ruby
}

# AST 节点类型 → symbol kind 映射（各语言统一命名）。
# 在 _walk_tree() 中递归遍历 AST 时匹配这些节点类型。
# 匹配到的节点通过 child_by_field_name("name") 获取符号名。
# 对应简历："递归遍历 AST 提取函数、类、方法定义"
_FUNC_NODES: frozenset[str] = frozenset({
    "function_definition",       # Python, Go, C, C++
    "async_function_definition", # Python async def
    "function_declaration",      # JS, TS, Java
    "method_declaration",        # Java
    "method_definition",         # JS class method
    "function_item",             # Rust fn
    "arrow_function",            # JS arrow（跳过，通常是匿名的）
})
# class 相关的节点类型。匹配后提取符号名，kind 统一标记为 "class"。
_CLASS_NODES: frozenset[str] = frozenset({
    "class_definition",   # Python
    "class_declaration",  # JS, TS, Java
    "struct_item",        # Rust struct
    "impl_item",          # Rust impl
    "interface_declaration",  # TS/Java
})

# 跳过的目录（.gitignore 风格的目录名）。
# _scan() 中通过 "any(part in _SKIP_DIRS for part in path.parts)" 检查路径的每一级
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build",
})

# 正则 fallback：匹配常见语言的定义语句。
# 用于 tree-sitter 未安装或不支持的语言。
# 匹配范围较广——def/class/function/func/fn/pub fn/async def/async fn
# Java/JS 的修饰符（public/private/protected/static）也会匹配到，在后面被过滤掉
_SYMBOL_RE = re.compile(
    r"^[ \t]*(def|class|function|func|fn|pub fn|async fn|async def"
    r"|public|private|protected|static)\s+(\w+)",
    re.MULTILINE,
)

# 已加载的 tree-sitter Language 对象缓存（避免重复 import）。
# key=文件扩展名（如 ".py"），value=Language 对象或 None（未安装）。
# 模块级全局变量，整个 Python 进程内共享——同一个扩展名只需 import 一次。
_lang_cache: dict[str, object] = {}   # ext → Language or None


def _get_language(ext: str):
    """
    按文件扩展名获取 tree-sitter Language 对象。
    未安装时返回 None，调用方降级为正则。

    流程：
    1. 检查 _lang_cache 缓存：已加载直接返回
    2. 从 _LANG_REGISTRY 查找扩展名对应的 (package, attribute)
    3. 如未注册 → 缓存 None 并返回 None（调用方走正则 fallback）
    4. try: importlib.import_module(module_name) 动态导入语言包
    5. getattr(mod, attr_name) 获取语言工厂函数
    6. Language(lang_fn()) 创建 tree-sitter Language 对象
    7. 缓存并返回
    8. except: 任何异常（包未安装、导入失败等）→ 缓存 None → 返回 None

    设计要点（对应简历"运行时按需加载语法包，不可用时静默降级为正则匹配"）：
    - 按需加载：不 import 未使用的语言包
    - 静默降级：except 捕获所有异常，不报错
    - 全局缓存：_lang_cache 在每个 Python 进程内只 import 同一语言包一次
    """
    if ext in _lang_cache:
        return _lang_cache[ext]

    entry = _LANG_REGISTRY.get(ext)
    if entry is None:
        _lang_cache[ext] = None
        return None

    module_name, attr_name = entry
    try:
        import importlib
        from tree_sitter import Language
        mod = importlib.import_module(module_name)
        lang_fn = getattr(mod, attr_name)
        lang = Language(lang_fn())
        _lang_cache[ext] = lang
        return lang
    except Exception:
        _lang_cache[ext] = None
        return None


# ===========================================================================
# 数据类
# Symbol = 单个符号（函数 / 类 / 方法），FileInfo = 单个文件 + 其符号列表
# ===========================================================================

@dataclass
class Symbol:
    """
    一个提取出来的符号（函数或类定义）。

    对应简历"提取函数、类、方法定义及其行号与缩进级别"。

    缩进级别是关键——indent == 0 表示顶层定义（函数/类），indent > 0 表示类内部方法。
    这个区分在简历中被特别强调："区分顶层函数与类内部方法"。
    """
    name: str         # 符号名（如 "run"、"Agent"、"AgentConfig"）
    kind: str         # "function"（顶层函数）| "class"（类/结构体/接口）| "method"（方法）
    line: int         # 定义所在行号（1-indexed）
    file: Path        # 所属文件路径
    indent: int = 0   # 缩进列号（0 = 顶层，>0 = 嵌套在 class/impl 中的方法）

    @property
    def is_toplevel(self) -> bool:
        """
        是否是顶层定义（不是类内部方法）。
        tree-sitter 提供精确的缩进值（start_point[1]），
        正则 fallback 也通过计算代码行前导空格来估算缩进。
        """
        return self.indent == 0


@dataclass
class FileInfo:
    """一个文件的元信息和符号列表。"""
    path: Path          # 相对于 repo 根目录的路径
    size: int           # 文件大小（字节），用于重要性评分中的 size_penalty
    symbols: list[Symbol] = field(default_factory=list)  # 提取到的符号列表

    @property
    def rel_path(self) -> str:
        """返回相对路径字符串，用于 _format_file() 中生成文件标题。"""
        return str(self.path)

    def importance_score(self) -> float:
        """
        计算文件的重要性分数（用于排序：高优先级的文件排在前面）。

        公式：importance = 顶层符号数 - 文件大小 / 10000

        设计逻辑：
        - 顶层符号数高 → 该文件定义了大量公开接口（如 main.py、core.py、api.py），
          通常是理解项目的关键入口，优先展示。
        - 文件大小惩罚 → 大文件往往是自动生成的、或数据文件。上下文窗口有限，
          应该优先展示小而精的核心文件。
        - 数量级说明：10000 这个参数是经验值——一个 10KB 的文件需要 1 个顶层符号
          才能得正分，500B 的文件只需要 0.05 个符号就能得正分。

        对应简历："按'顶层符号数 - 文件大小/10000'评分排序"
        """
        top_level = sum(1 for s in self.symbols if s.is_toplevel)
        size_penalty = self.size / 10_000
        return top_level - size_penalty


# ===========================================================================
# RepoMap — 仓库地图生成器
# 整个 context/repo_map.py 的核心类。
# 被 core.py:_build_messages() 调用（每个 step 都尝试调用，但只有第一步实际执行扫描，
# 后续步骤通过 _repo_map_cache 复用结果）。
#
# 对应简历整段第二段"仓库地图生成与上下文注入"。
# ===========================================================================

class RepoMap:
    """
    扫描 repo，生成摘要字符串。

    用法：
        rm = RepoMap(repo_path="/path/to/repo")
        summary = rm.build(budget=8000)

    输出示例：
        agent/core.py (7 symbols):
          class AgentConfig (line 50)
          class Agent (line 72)
          function run (line 95)
            method _build_messages (line 275)
          ...
    """

    def __init__(self, repo_path: str | Path) -> None:
        # 将传入路径转为绝对路径（resolve() 解析符号链接和相对路径）
        self._root = Path(repo_path).resolve()

    def build(self, budget: int = 8000) -> str:
        """
        生成 repo-map 摘要字符串。

        Args:
            budget: Token 预算（字符预算 = budget * 4）。
                    默认 8000，对应 config/default.yaml 的 context.repo_map_budget。
                    在 core.py 中通过 token_budget.default_plan().repo_map 传入。

        Returns:
            格式化的 repo 摘要字符串，注入 System Prompt 的 {repo_summary} 占位符

        Pipeline（完整流程）：
        1. _scan() → 递归遍历所有文件，提取符号 → list[FileInfo]
        2. 按 importance_score() 降序排列
        3. 遍历排序后的文件列表，调用 _format_file() 逐文件格式化
        4. 累加字符数，超出 budget * 4 时截断，标记 "... (N more files not shown)"
        5. 返回 "\n".join(lines)
        """
        files = self._scan()
        if not files:
            return "(empty repository)"

        # 按重要性降序排列（顶层符号多 + 文件小 → 排前面）
        files.sort(key=lambda f: f.importance_score(), reverse=True)

        lines: list[str] = []
        char_count = 0
        max_chars = budget * 4  # 字符估算：1 token ≈ 4 个字符

        for fi in files:
            block = self._format_file(fi)
            if char_count + len(block) > max_chars:
                remaining = len(files) - files.index(fi)
                lines.append(f"... ({remaining} more files not shown)")
                break
            lines.append(block)
            char_count += len(block)

        return "\n".join(lines)

    def _scan(self) -> list[FileInfo]:
        """
        递归遍历 repo 目录，提取所有文件的符号信息。

        扫描规则：
        - 跳过 _SKIP_DIRS 中的目录（.git / __pycache__ / node_modules 等）
        - 跳过 >500KB 的大文件（太大会拉低 token 效率，且通常不是核心代码）
        - 对 _LANG_REGISTRY 中注册的语言，调用 _extract_symbols() 提取符号
        - 未注册的扩展名仍会加入结果（只是 symbols 为空）
          这样 LLM 能看到 repo 中有哪些文件，即使无法提取符号

        Returns:
            list[FileInfo]，每个文件一项（含 path / size / symbols）
        """
        results: list[FileInfo] = []
        for path in sorted(self._root.rglob("*")):
            # 跳过黑名单目录
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > 500_000:
                continue

            fi = FileInfo(path=path.relative_to(self._root), size=size)
            ext = path.suffix.lower()

            # 对已知语言提取符号。双重检查（先查注册表，再查硬编码列表）确保降级路径的完整性
            if ext in _LANG_REGISTRY or ext in {".py", ".js", ".ts", ".go", ".rs"}:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    fi.symbols = _extract_symbols(content, fi.path, ext)
                except OSError:
                    pass

            results.append(fi)
        return results

    def _format_file(self, fi: FileInfo) -> str:
        """
        将单个文件的符号列表格式化为易读的文本块。

        输出格式：
            agent/core.py (7 symbols):
              class AgentConfig (line 50)      ← "  " 前缀（2 空格）= 顶层定义
                function run (line 95)         ← "    " 前缀（4 空格）= 方法
        """
        sym_count = len(fi.symbols)
        header = f"{fi.rel_path}"
        if sym_count:
            header += f" ({sym_count} symbol{'s' if sym_count != 1 else ''})"

        if not fi.symbols:
            return header + "\n"

        sym_lines = [header + ":"]
        for sym in fi.symbols:
            # 缩进区分：顶层定义用 2 个空格，方法用 4 个空格
            prefix = "    " if not sym.is_toplevel else "  "
            sym_lines.append(f"{prefix}{sym.kind} {sym.name} (line {sym.line})")
        return "\n".join(sym_lines) + "\n"


# ===========================================================================
# 符号提取（对外暴露，供测试使用）
# 两个分支路径：
#   路径 1（主）：tree-sitter AST 遍历 — 精确，区分 function/method，错误容忍
#   路径 2（fallback）：正则匹配 — 无需安装，跨语言通用
# ===========================================================================

def _extract_symbols(content: str, filepath: Path, ext: str) -> list[Symbol]:
    """
    按扩展名选择解析方式：tree-sitter（如已安装）或正则 fallback。

    这是符号提取的统一入口。
    先尝试 tree-sitter（路径 1），不可用时走正则（路径 2）。
    对应简历"运行时按需加载语法包，不可用时静默降级为正则匹配"中的"降级"。
    """
    lang = _get_language(ext)
    if lang is not None:
        return _extract_with_treesitter(content, filepath, lang)
    return _extract_symbols_regex(content, filepath)


def _extract_with_treesitter(content: str, filepath: Path, lang) -> list[Symbol]:
    """
    用 tree-sitter 提取符号，失败时降级为正则。

    流程：
    1. 创建 Parser(lang)
    2. parser.parse(content.encode("utf-8")) 得到 AST
    3. _walk_tree(root_node) 递归遍历，收集 Symbol 列表
    4. 任何异常 → _extract_symbols_regex() 降级
    """
    try:
        from tree_sitter import Parser
        parser = Parser(lang)
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        return _walk_tree(tree.root_node, filepath)
    except Exception:
        return _extract_symbols_regex(content, filepath)


def _walk_tree(node, filepath: Path) -> list[Symbol]:
    """
    递归遍历 tree-sitter AST，提取函数和类定义。

    关键设计（对应简历"区分顶层函数与类内部方法"）：
    - 用 node.start_point[1]（起始列号）作为缩进级别：
      indent == 0 → 顶层定义（kind = "function" / "class"）
      indent > 0  → 嵌套在类内部（kind = "method"）
    - 用 child_by_field_name("name") 获取符号名：
      tree-sitter 的 field 查询比手动找索引更稳定
    - 用 node.start_point[0] + 1 计算行号（tree-sitter 的行是 0-indexed）

    节点类型匹配：
    - _FUNC_NODES：9 种函数/方法节点（跨语言统一）
    - _CLASS_NODES：5 种类/结构体/接口节点
    """
    results: list[Symbol] = []
    ntype = node.type

    if ntype in _FUNC_NODES and ntype != "arrow_function":
        name_node = node.child_by_field_name("name")
        if name_node:
            indent = node.start_point[1]
            kind = "method" if indent > 0 else "function"
            results.append(Symbol(
                name=name_node.text.decode("utf-8", errors="replace"),
                kind=kind,
                line=node.start_point[0] + 1,
                file=filepath,
                indent=indent,
            ))
    elif ntype in _CLASS_NODES:
        name_node = node.child_by_field_name("name")
        if name_node:
            indent = node.start_point[1]
            results.append(Symbol(
                name=name_node.text.decode("utf-8", errors="replace"),
                kind="class",
                line=node.start_point[0] + 1,
                file=filepath,
                indent=indent,
            ))

    for child in node.children:
        results.extend(_walk_tree(child, filepath))

    return results


# 保留原函数名供测试 import（tests/test_day5.py::TestExtractPythonSymbols 中使用此名）
def _extract_python_symbols(content: str, filepath: Path) -> list[Symbol]:
    """兼容旧接口，测试文件用此名调用。"""
    return _extract_symbols(content, filepath, ".py")


def _extract_symbols_regex(content: str, filepath: Path) -> list[Symbol]:
    """
    正则 fallback，支持多语言。
    用于 tree-sitter 未安装或不支持的语言。
    精度不如 tree-sitter（无法区分顶层函数和类方法中的 indent == 0 的特殊情况），
    但胜在零依赖、跨语言通用。

    正则逻辑：
    - 对每一行匹配 _SYMBOL_RE
    - 匹配到 def/class/function/func/fn... 关键字
    - 提取符号名（第 2 个 group）
    - 过滤掉 Java/JS 修饰符（public/private/static 等单独出现时不是定义）
    - 用行前导空格估算缩进（判断 method vs function vs class）
    """
    symbols: list[Symbol] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        m = _SYMBOL_RE.match(line)
        if not m:
            continue
        keyword = m.group(1)
        name = m.group(2)
        # 跳过 Java/JS 修饰符误匹配（public/private 后面跟的是类型，不是名字）
        if keyword in ("public", "private", "protected", "static"):
            continue
        indent = len(line) - len(line.lstrip())
        if keyword == "class":
            kind = "class"
        elif indent > 0:
            kind = "method"
        else:
            kind = "function"
        symbols.append(Symbol(
            name=name, kind=kind, line=lineno,
            file=filepath, indent=indent,
        ))
    return symbols