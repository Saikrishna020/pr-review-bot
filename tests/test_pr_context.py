"""Unit tests for pr_context.py's diff hunk parser — pure text-in,
structured-data-out, no network calls.
"""

from pr_context import parse_changed_files

SIMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,4 +1,5 @@
 def foo():
-    return 1
+    x = 2
+    return x

 def bar():
"""

MULTI_HUNK_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
-def foo():
+def foo(x):
     return x
@@ -10,2 +10,3 @@
 def bar():
+    print("added")
     return 1
"""

MULTI_FILE_DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
 def a():
+    pass
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1,1 +1,2 @@
 def b():
+    pass
"""

DELETED_FILE_DIFF = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    pass
"""


def test_added_lines_recorded_at_correct_new_file_line_numbers():
    files = parse_changed_files(SIMPLE_DIFF)
    assert len(files) == 1
    assert files[0].path == "foo.py"
    # hunk starts at new-file line 1; "def foo():" (context) is line 1,
    # so the two added lines land at 2 and 3.
    assert files[0].changed_lines == {2, 3}


def test_removed_lines_do_not_advance_new_file_line_count():
    diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,2 @@
 def foo():
-    unused = 1
     return 2
"""
    files = parse_changed_files(diff)
    # nothing was added, so there should be no changed lines at all
    assert files[0].changed_lines == set()


def test_multiple_hunks_in_one_file_are_merged():
    files = parse_changed_files(MULTI_HUNK_DIFF)
    assert len(files) == 1
    assert files[0].changed_lines == {1, 11}


def test_multiple_files_are_tracked_separately():
    files = parse_changed_files(MULTI_FILE_DIFF)
    paths = {f.path: f.changed_lines for f in files}
    assert paths == {"a.py": {2}, "b.py": {2}}


def test_deleted_file_produces_no_changed_file_entry():
    files = parse_changed_files(DELETED_FILE_DIFF)
    assert files == []


def test_b_prefix_is_stripped_from_path():
    files = parse_changed_files(SIMPLE_DIFF)
    assert files[0].path == "foo.py"  # not "b/foo.py"


def test_blank_context_line_still_advances_line_count():
    # A blank source line appears as a context line with no visible content —
    # sometimes as " " (a lone space), sometimes as a truly empty string if
    # trailing whitespace got stripped upstream. Either way it's one real
    # line, and line numbers after it must not drift.
    diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,4 +1,5 @@
 def foo():
+    pass

 def bar():
+    pass
"""
    files = parse_changed_files(diff)
    assert files[0].changed_lines == {2, 5}
