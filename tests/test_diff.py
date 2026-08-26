from codelens.diff import parse_unified_diff

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,7 +10,9 @@ def handler(request):
     data = request.json()
-    return process(data)
+    if not data:
+        return None
+    return process(data, strict=True)

 def other():
     pass
diff --git a/README.md b/README.md
new file mode 100644
--- /dev/null
+++ b/README.md
@@ -0,0 +1,2 @@
+# Title
+Some text.
"""


def test_parses_every_file_in_the_diff():
    files = parse_unified_diff(DIFF)
    assert [f.path for f in files] == ["src/app.py", "README.md"]


def test_counts_added_and_removed_lines():
    app = parse_unified_diff(DIFF)[0]
    assert app.added == 3
    assert app.removed == 1


def test_new_file_is_marked_added():
    readme = parse_unified_diff(DIFF)[1]
    assert readme.status == "added"
    assert readme.added == 2


def test_added_line_numbers_are_absolute_not_relative():
    """A finding must point at a real line in the new file, not an offset."""
    app = parse_unified_diff(DIFF)[0]
    # The hunk starts at new-file line 10. One context line sits at 10, the
    # removed line consumes no new-file number, so the additions are 11-13.
    assert app.changed_line_numbers == [11, 12, 13]


def test_empty_diff_yields_nothing():
    assert parse_unified_diff("") == []


def test_render_truncates_to_budget():
    app = parse_unified_diff(DIFF)[0]
    rendered = app.render(max_chars=40)
    assert "truncated" in rendered
    assert len(rendered) < 120
