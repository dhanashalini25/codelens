from codelens.parsing import find_symbol_at_line, parse, parse_python

PYTHON_SOURCE = '''
import os


def simple(a, b):
    """Adds two numbers."""
    return a + b


def branchy(items, flag=False):
    total = 0
    for item in items:
        if item > 0 and flag:
            total += item
        elif item < 0:
            total -= item
        else:
            try:
                total += 1
            except ValueError:
                pass
    return total


class Service:
    """A service."""

    def __init__(self, name):
        self.name = name

    @property
    def label(self):
        return self.name.upper()
'''


def test_finds_functions_and_classes():
    symbols = parse_python(PYTHON_SOURCE)
    names = {s.qualified_name for s in symbols}
    assert "simple" in names
    assert "branchy" in names
    assert "Service" in names
    assert "Service.__init__" in names
    assert "Service.label" in names


def test_docstring_detection():
    symbols = {s.qualified_name: s for s in parse_python(PYTHON_SOURCE)}
    assert symbols["simple"].docstring is True
    assert symbols["branchy"].docstring is False
    assert symbols["Service"].docstring is True


def test_complexity_scales_with_branching():
    symbols = {s.qualified_name: s for s in parse_python(PYTHON_SOURCE)}
    assert symbols["simple"].complexity == 1
    assert symbols["branchy"].complexity > 4


def test_signature_and_decorators():
    symbols = {s.qualified_name: s for s in parse_python(PYTHON_SOURCE)}
    assert symbols["simple"].signature == "def simple(a, b)"
    assert "property" in symbols["Service.label"].decorators


def test_syntax_error_returns_empty_rather_than_raising():
    assert parse_python("def broken(:\n    pass") == []


def test_find_symbol_at_line_picks_innermost():
    symbols = parse_python(PYTHON_SOURCE)
    init = next(s for s in symbols if s.qualified_name == "Service.__init__")
    found = find_symbol_at_line(symbols, init.start_line + 1)
    assert found is not None
    assert found.qualified_name == "Service.__init__"


def test_generic_parser_handles_javascript():
    source = """
export function alpha(a) { return a; }
const beta = async (b) => b;
export class Gamma {}
"""
    names = {s.name for s in parse(source, "javascript")}
    assert {"alpha", "beta", "Gamma"} <= names


def test_unknown_language_yields_no_symbols():
    assert parse("some text", "cobol") == []
