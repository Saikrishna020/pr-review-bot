"""Unit tests for code_graph.py's tree-sitter parsing — no network, no
GitHub API, just source text in and structured data out.
"""

from code_graph import Import, parse_python_source, resolve_python_import_paths


def test_parses_function_and_class_definitions():
    source = """
def foo():
    return 1


class Bar:
    def method(self):
        return 2
"""
    parsed = parse_python_source(source)
    names = {(s.name, s.kind) for s in parsed.symbols}
    assert ("foo", "function") in names
    assert ("Bar", "class") in names
    assert ("method", "function") in names


def test_class_bases_bare_and_dotted_and_multiple():
    source = """
import io

class Bare(Base):
    pass

class Dotted(io.TextIOWrapper):
    pass

class Multiple(A, B, C):
    pass
"""
    parsed = parse_python_source(source)
    bases_by_name = {s.name: s.bases for s in parsed.symbols if s.kind == "class"}
    assert bases_by_name["Bare"] == ["Base"]
    # dotted bases (module.Class) capture the trailing name, since that's what
    # name-based hierarchy lookups match against elsewhere in the tool
    assert bases_by_name["Dotted"] == ["TextIOWrapper"]
    assert bases_by_name["Multiple"] == ["A", "B", "C"]


def test_call_sites_plain_and_attribute():
    source = """
def caller():
    helper()
    module.helper()
    obj.method().chained()
"""
    parsed = parse_python_source(source)
    call_names = [c.name for c in parsed.calls]
    assert call_names.count("helper") == 2  # plain call + attribute call both count
    assert "chained" in call_names


def test_symbol_at_picks_innermost_enclosing_symbol():
    source = """class Outer:
    def method(self):
        return 1
"""
    parsed = parse_python_source(source)
    # line 2 ("def method...") is inside both Outer and method — the
    # innermost (method) should win, not the enclosing class.
    symbol = parsed.symbol_at(2)
    assert symbol is not None
    assert symbol.name == "method"


def test_imports_cover_absolute_relative_and_bare_relative_forms():
    source = """
import os
import os.path as op
from foo.bar import baz
from . import sibling
from .utils import helper
from ..pkg import thing
"""
    parsed = parse_python_source(source)
    imports = {(i.raw, i.level) for i in parsed.imports}
    assert ("os", 0) in imports
    assert ("os.path", 0) in imports          # `as op` doesn't change the real dotted path
    assert ("foo.bar", 0) in imports
    assert ("sibling", 1) in imports          # bare `from . import sibling` -> submodule of current package
    assert ("utils", 1) in imports
    assert ("pkg", 2) in imports


def test_resolve_absolute_import_never_emits_backslashes():
    # Regression test: these strings become GitHub API paths, not local
    # filesystem paths. resolve_python_import_paths used to build them with
    # plain str(Path(...)), which is backslash-joined on Windows and silently
    # broke every absolute-import resolution (candidates just always 404'd).
    candidates = resolve_python_import_paths(Import(raw="click._compat", level=0), "some/file.py")
    assert "src/click/_compat.py" in candidates
    assert all("\\" not in c for c in candidates)


def test_from_package_import_module_offers_the_submodule_path():
    # SCRUM-8: `from package import module` used to resolve only to
    # package.py / package/__init__.py, so a file importing a submodule this
    # way was demoted from a confirmed caller to a name-only "possible" match.
    imp = parse_python_source("from package import module\n").imports[0]
    candidates = resolve_python_import_paths(imp, "caller.py")
    assert "package/module.py" in candidates
    assert "src/package/module.py" in candidates


def test_submodule_candidates_come_after_module_candidates():
    # Order is load-bearing: _resolve_import takes the first candidate that
    # exists, so module-level paths must stay ahead of submodule guesses or
    # ordinary attribute imports would start resolving to non-existent files
    # (and cost an extra API call each).
    imp = parse_python_source("from package import module\n").imports[0]
    candidates = resolve_python_import_paths(imp, "caller.py")
    assert candidates.index("package.py") < candidates.index("package/module.py")
    assert candidates.index("package/__init__.py") < candidates.index("package/module.py")


def test_confirmed_caller_detection_matches_a_submodule_import():
    # Mirrors what _confirm_usage does: a file is a confirmed caller of
    # symbol_file if any resolved candidate equals it.
    parsed = parse_python_source("from package import module\n\nmodule.func()\n")
    symbol_file = "package/module.py"
    resolved = [
        candidate
        for imp in parsed.imports
        for candidate in resolve_python_import_paths(imp, "caller.py")
    ]
    assert symbol_file in resolved


def test_subpackage_import_offers_the_package_init_path():
    # An imported name may be a directory, not a single file. The rest of
    # resolve_python_import_paths always tries both X.py and X/__init__.py,
    # so the submodule candidates have to as well.
    imp = parse_python_source("from package import subpkg\n").imports[0]
    candidates = resolve_python_import_paths(imp, "caller.py")
    assert "package/subpkg.py" in candidates
    assert "package/subpkg/__init__.py" in candidates


def test_aliased_submodule_import_resolves_to_the_original_name():
    imp = parse_python_source("from package import module as m\n").imports[0]
    assert imp.names == ["module"]
    assert "package/module.py" in resolve_python_import_paths(imp, "caller.py")


def test_star_import_contributes_no_submodule_names():
    imp = parse_python_source("from package import *\n").imports[0]
    assert imp.names == []
    assert resolve_python_import_paths(imp, "caller.py") == [
        "package.py", "package/__init__.py", "src/package.py", "src/package/__init__.py",
    ]


def test_plain_dotted_import_is_unchanged():
    imp = parse_python_source("import package.module\n").imports[0]
    assert imp.names == []
    assert resolve_python_import_paths(imp, "caller.py") == [
        "package/module.py", "package/module/__init__.py",
        "src/package/module.py", "src/package/module/__init__.py",
    ]


def test_relative_submodule_import_resolves_within_the_package():
    imp = parse_python_source("from .mod import helper\n").imports[0]
    candidates = resolve_python_import_paths(imp, "pkg/caller.py")
    assert candidates.index("pkg/mod.py") < candidates.index("pkg/mod/helper.py")


def test_resolve_relative_import_walks_up_by_level():
    # `from . import x` inside package/sub/module.py -> package/sub/x.py
    candidates = resolve_python_import_paths(Import(raw="x", level=1), "package/sub/module.py")
    assert "package/sub/x.py" in candidates

    # `from .. import x` inside package/sub/module.py -> package/x.py
    candidates = resolve_python_import_paths(Import(raw="x", level=2), "package/sub/module.py")
    assert "package/x.py" in candidates
