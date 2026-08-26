"""Parses a single Python file's source into definitions, call sites, and
imports, using tree-sitter. Stateless — no repo is checked out or cached
anywhere; `pr_context.py` calls this on whatever file content it just
fetched from the GitHub API, one file at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_languages import get_language, get_parser

QUERY_PATH = Path(__file__).parent / "queries" / "python.scm"


@dataclass
class Symbol:
    name: str
    kind: str  # "function" | "class"
    start_line: int  # 1-indexed, inclusive
    end_line: int
    bases: list[str] = field(default_factory=list)


@dataclass
class CallSite:
    name: str
    line: int


@dataclass
class Import:
    raw: str    # dotted module path, or a bare submodule name for `from . import x`
    level: int  # 0 = absolute import, 1+ = number of leading dots


@dataclass
class ParsedFile:
    symbols: list[Symbol]
    calls: list[CallSite]
    imports: list[Import]

    def symbol_at(self, line: int) -> Symbol | None:
        """The innermost function/class whose range contains `line`, if any."""
        best: Symbol | None = None
        for symbol in self.symbols:
            if symbol.start_line <= line <= symbol.end_line:
                if best is None or (symbol.end_line - symbol.start_line) < (best.end_line - best.start_line):
                    best = symbol
        return best


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


_parser = None
_query = None


def _load():
    global _parser, _query
    if _parser is None:
        _parser = get_parser("python")
        _query = get_language("python").query(QUERY_PATH.read_text(encoding="utf-8"))
    return _parser, _query


def _extract_definitions(query, root, source: bytes) -> list[Symbol]:
    by_id: dict[int, Symbol] = {}
    order: list[int] = []
    for _pattern_index, caps in query.matches(root):
        if "def.function" in caps:
            node = caps["def.function"]
            name_node = caps.get("def.function.name")
            if name_node is None:
                continue
            key = node.id
            if key not in by_id:
                by_id[key] = Symbol(
                    name=_text(name_node, source), kind="function",
                    start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                )
                order.append(key)
        elif "def.class" in caps:
            node = caps["def.class"]
            name_node = caps.get("def.class.name")
            if name_node is None:
                continue
            key = node.id
            if key not in by_id:
                by_id[key] = Symbol(
                    name=_text(name_node, source), kind="class",
                    start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                )
                order.append(key)
            base_node = caps.get("def.class.base")
            if base_node is not None:
                by_id[key].bases.append(_text(base_node, source))
    return [by_id[key] for key in order]


def _extract_calls(query, root, source: bytes) -> list[CallSite]:
    calls: list[CallSite] = []
    seen: set[tuple[int, int]] = set()
    for _pattern_index, caps in query.matches(root):
        call_node = caps.get("call")
        name_node = caps.get("call.name")
        if call_node is None or name_node is None:
            continue
        dedup_key = (call_node.id, name_node.id)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        calls.append(CallSite(name=_text(name_node, source), line=call_node.start_point[0] + 1))
    return calls


def _extract_imports(root, source: bytes) -> list[Import]:
    imports: list[Import] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    imports.append(Import(raw=_text(child, source), level=0))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    if name_node is not None:
                        imports.append(Import(raw=_text(name_node, source), level=0))
        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            if module_node is not None:
                if module_node.type == "dotted_name":
                    imports.append(Import(raw=_text(module_node, source), level=0))
                elif module_node.type == "relative_import":
                    raw = _text(module_node, source)
                    level = len(raw) - len(raw.lstrip("."))
                    rest = raw[level:]
                    if rest:
                        imports.append(Import(raw=rest, level=level))
                    else:
                        # `from . import x` — x is a submodule of the resolved package.
                        for name_node in node.children_by_field_name("name"):
                            if name_node.type == "dotted_name":
                                imports.append(Import(raw=_text(name_node, source), level=level))
        stack.extend(node.children)
    return imports


def parse_python_source(source: str) -> ParsedFile:
    parser, query = _load()
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    return ParsedFile(
        symbols=_extract_definitions(query, tree.root_node, source_bytes),
        calls=_extract_calls(query, tree.root_node, source_bytes),
        imports=_extract_imports(tree.root_node, source_bytes),
    )


def resolve_python_import_paths(imp: Import, importing_file: str) -> list[str]:
    """Candidate repo-relative paths `imp` might point to, most-likely first.
    Existence isn't checked here (there's no local filesystem to check against) —
    the caller fetches each candidate from GitHub and uses whichever one exists.
    """
    rel = Path(*imp.raw.split("."))

    if imp.level > 0:
        base_dir = Path(importing_file).parent
        for _ in range(imp.level - 1):
            base_dir = base_dir.parent
        return [
            (base_dir / rel).with_suffix(".py").as_posix(),
            (base_dir / rel / "__init__.py").as_posix(),
        ]

    # Absolute import — try both a flat layout and a common src/-layout.
    return [
        rel.with_suffix(".py").as_posix(),
        (rel / "__init__.py").as_posix(),
        f"src/{rel.with_suffix('.py').as_posix()}",
        f"src/{(rel / '__init__.py').as_posix()}",
    ]
