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


def test_resolve_relative_import_walks_up_by_level():
    # `from . import x` inside package/sub/module.py -> package/sub/x.py
    candidates = resolve_python_import_paths(Import(raw="x", level=1), "package/sub/module.py")
    assert "package/sub/x.py" in candidates

    # `from .. import x` inside package/sub/module.py -> package/x.py
    candidates = resolve_python_import_paths(Import(raw="x", level=2), "package/sub/module.py")
    assert "package/x.py" in candidates
