from codelens.rules import analyze, dedupe, scan_python, scan_text


def categories(findings):
    return {f.category for f in findings}


def titles(findings):
    return " | ".join(f.title for f in findings)


def test_detects_bare_except():
    source = "try:\n    x = 1\nexcept:\n    raise\n"
    findings = scan_python("a.py", source)
    assert any("Bare except" in f.title for f in findings)


def test_detects_swallowed_exception():
    source = "try:\n    x = 1\nexcept ValueError:\n    pass\n"
    findings = scan_python("a.py", source)
    assert any("silently swallowed" in f.title for f in findings)


def test_detects_mutable_default_argument():
    source = "def f(items=[]):\n    items.append(1)\n    return items\n"
    findings = scan_python("a.py", source)
    assert any("Mutable default" in f.title for f in findings)
    assert any(f.severity == "high" for f in findings)


def test_detects_eval_as_critical():
    findings = scan_python("a.py", "def f(s):\n    return eval(s)\n")
    assert any(f.severity == "critical" and "eval" in f.title for f in findings)


def test_detects_shell_true():
    source = "import subprocess\ndef f(cmd):\n    subprocess.run(cmd, shell=True)\n"
    findings = scan_python("a.py", source)
    assert any("shell=True" in f.title for f in findings)


def test_detects_comparison_to_none():
    findings = scan_python("a.py", "def f(x):\n    return x == None\n")
    assert any("None" in f.title for f in findings)


def test_syntax_error_is_reported_not_swallowed():
    findings = scan_python("a.py", "def broken(:\n    pass\n")
    assert len(findings) == 1
    assert findings[0].category == "correctness"
    assert findings[0].severity == "high"


def test_detects_hardcoded_api_key():
    source = 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n'
    findings = scan_text("config.py", source)
    assert any(f.severity == "critical" and f.category == "security" for f in findings)


def test_detects_todo_comment():
    findings = scan_text("a.py", "# TODO: handle the empty case\n")
    assert any(f.title == "TODO comment" for f in findings)


def test_ignores_clean_code():
    source = (
        "def add(a, b):\n"
        '    """Add two numbers."""\n'
        "    return a + b\n"
    )
    findings = analyze("a.py", source, "python")
    assert findings == []


def test_only_lines_restricts_scope():
    """A review should not report pre-existing issues the change did not touch."""
    source = "def f(x):\n    return x == None\n\n\ndef g(y):\n    return y == None\n"
    everything = scan_python("a.py", source)
    restricted = scan_python("a.py", source, only_lines=[2])
    assert len(everything) == 2
    assert len(restricted) == 1
    assert restricted[0].line == 2


def test_complexity_threshold_flags_branchy_functions():
    branches = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(14))
    source = f"def f(x):\n{branches}\n    return None\n"
    findings = scan_python("a.py", source)
    assert any("complexity" in f.title for f in findings)


def test_dedupe_keeps_the_most_severe_report():
    from codelens.rules import Finding

    low = Finding("a.py", 1, "low", "style", "same", "d")
    high = Finding("a.py", 1, "high", "style", "same", "d")
    assert dedupe([low, high])[0].severity == "high"
    assert len(dedupe([low, high])) == 1


def test_long_class_is_not_reported_as_a_long_function():
    """A 90-line class is a normal class. Only functions are flagged at 80."""
    body = "\n".join(f"    attribute_{i} = {i}" for i in range(90))
    source = f"class Big:\n{body}\n"
    findings = scan_python("a.py", source)
    assert not any("lines long" in f.title for f in findings)


def test_long_function_is_still_reported_with_the_right_noun():
    body = "\n".join(f"    x{i} = {i}" for i in range(90))
    source = f"def big():\n{body}\n"
    findings = scan_python("a.py", source)
    long_findings = [f for f in findings if "lines long" in f.title]
    assert long_findings
    assert long_findings[0].title.startswith("function big is")


def test_bom_prefixed_source_still_parses():
    """Windows editors write a UTF-8 BOM; U+FEFF must not break the parser."""
    source = "﻿" + 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n'
    findings = analyze("a.py", source, "python")
    assert not any("does not parse" in f.title for f in findings)
    assert any(f.severity == "critical" for f in findings)
