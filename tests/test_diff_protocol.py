import unittest

from codepilot.core.diff_protocol import DiffProtocolError, apply_operation, parse_operations


class DiffProtocolTests(unittest.TestCase):
    def test_hunk_ranges_are_ignored_and_context_indentation_is_preserved(self):
        operation = parse_operations("""diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -400,2 +900,3 @@
     answer = 1
+    enabled = True
     return answer
""")[0]
        self.assertEqual(
            "        answer = 1\n    enabled = True\n        return answer\n",
            apply_operation(operation, "        answer = 1\n        return answer\n", True),
        )

    def test_ambiguous_match_is_rejected(self):
        operation = parse_operations("""diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-TIMEOUT = 10
+TIMEOUT = 20
""")[0]
        with self.assertRaisesRegex(DiffProtocolError, "found 2 matches"):
            apply_operation(operation, "TIMEOUT = 10\nTIMEOUT = 10\n", True)

    def test_pure_addition_rewrites_file(self):
        operation = parse_operations("""diff --git a/a.py b/a.py
--- /dev/null
+++ b/a.py
@@ -0,0 +1,2 @@
+new = 1
+print(new)
""")[0]
        self.assertEqual("new = 1\nprint(new)\n", apply_operation(operation, "old\n", True))

    def test_multiple_hunks_apply_to_evolving_content(self):
        operation = parse_operations("""diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-one
+ONE
@@ -50 +50 @@
-three
+THREE
""")[0]
        self.assertEqual("ONE\ntwo\nTHREE\n", apply_operation(operation, "one\ntwo\nthree\n", True))


if __name__ == "__main__":
    unittest.main()
