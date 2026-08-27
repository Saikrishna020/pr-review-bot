"""Parses a single Python file's source into definitions, call sites, and
imports, using tree-sitter. Stateless — no repo is checked out or cached
anywhere; `pr_context.py` calls this on whatever file content it just
fetched from the GitHub API, one file at a time.
"""

from __future__ import annotations

import threading
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
    # Names bound by a `from X import a, b` statement. Syntax alone can't say
    # whether these are attributes of X or submodules of it, so resolution
    # tries them as submodules too — see resolve_python_import_paths.
    names: list[str] = field(default_factory=list)


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


# One parser per thread, not one shared globally. `parse_python_source` is
# called concurrently from pr_context's thread pool, and a tree-sitter Parser
# is not safe to call .parse() on from multiple threads at once — sharing one
# is a data race, not just a lock-contention issue. Thread-local storage also
# removes the race on lazy initialisation itself.
_local = threading.local()


def _load():
    parser = getattr(_local, "parser", None)
    if parser is None:
        parser = get_parser("python")
        query = get_language("python").query(QUERY_PATH.read_text(encoding="utf-8"))
        _local.parser = parser
        _local.query = query
    return parser, _local.query


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


def _imported_names(from_import_node, source: bytes) -> list[str]:
    """The names bound by a `from X import a, b as c` statement.

    Aliases resolve to the original name, since that's what identifies the
    file on disk. `import *` binds no nameable submodule, so it contributes
    nothing.
    """
    names: list[str] = []
    for name_node in from_import_node.children_by_field_name("name"):
        if name_node.type == "dotted_name":
            names.append(_text(name_node, source))
        elif name_node.type == "aliased_import":
            original = name_node.child_by_field_name("name")
            if original is not None:
                names.append(_text(original, source))
    return names


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
                imported_names = _imported_names(node, source)
                if module_node.type == "dotted_name":
                    imports.append(Import(raw=_text(module_node, source), level=0, names=imported_names))
                elif module_node.type == "relative_import":
                    raw = _text(module_node, source)
                    level = len(raw) - len(raw.lstrip("."))
                    rest = raw[level:]
                    if rest:
                        imports.append(Import(raw=rest, level=level, names=imported_names))
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
        candidates = [
            (base_dir / rel).with_suffix(".py").as_posix(),
            (base_dir / rel / "__init__.py").as_posix(),
        ]
        package_dirs = [base_dir / rel]
    else:
        # Absolute import — try both a flat layout and a common src/-layout.
        candidates = [
            rel.with_suffix(".py").as_posix(),
            (rel / "__init__.py").as_posix(),
            f"src/{rel.with_suffix('.py').as_posix()}",
            f"src/{(rel / '__init__.py').as_posix()}",
        ]
        package_dirs = [rel, Path("src") / rel]

    # `from package import module` binds a name that syntax alone can't
    # classify: `module` may be an attribute defined inside package/__init__.py,
    # or a separate package/module.py file. Offer the submodule paths too, so a
    # file importing a submodule this way is recognised as a real caller of it
    # rather than being demoted to an unconfirmed name match.
    #
    # These go last deliberately. Callers that take the first candidate that
    # exists (_resolve_import) keep their current behaviour and never fetch
    # these unless every module-level candidate missed, so this adds no API
    # calls in the common case. Callers that check membership across all
    # candidates (_confirm_usage) get the extra precision for free.
    # Both forms, since the imported name may be a single-file module
    # (name.py) or a subpackage directory (name/__init__.py) — matching how
    # the module-level candidates above already try each.
    candidates.extend(
        path
        for package_dir in package_dirs
        for name in imp.names
        for path in (
            (package_dir / name).with_suffix(".py").as_posix(),
            (package_dir / name / "__init__.py").as_posix(),
        )
    )
    return candidates
