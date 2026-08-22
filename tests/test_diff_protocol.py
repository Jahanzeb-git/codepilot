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
""")[0][0]
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
""")[0][0]
        with self.assertRaisesRegex(DiffProtocolError, "ambiguous"):
            apply_operation(operation, "TIMEOUT = 10\nTIMEOUT = 10\n", True)

    def test_pure_addition_rewrites_file(self):
        operation = parse_operations("""diff --git a/a.py b/a.py
--- /dev/null
+++ b/a.py
@@ -0,0 +1,2 @@
+new = 1
+print(new)
""")[0][0]
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
""")[0][0]
        self.assertEqual("ONE\ntwo\nTHREE\n", apply_operation(operation, "one\ntwo\nthree\n", True))


class HardAnchorSoftContextTests(unittest.TestCase):
    """Tests for the Phase 1 (Hard Anchors + Soft Context Scoring) algorithm."""

    # ── Scenario A: unique hard anchors, context line is missing ──────────────

    def test_missing_context_line_still_matches_unique_remove_sequence(self):
        """Phase 1 finds the region via - lines alone; missing context is fine."""
        file_content = (
            "def process_data(data):\n"
            "    if not data:\n"
            "        print('Error: No data provided')\n"
            "        logger.error('Data is missing')\n"
            "        return None\n"
            "    result = transform(data)\n"  # <-- model omitted this context line
            "    return result\n"
        )
        # Diff omits the 'result = transform(data)' context line
        operation = parse_operations(
            "diff --git a/utils.py b/utils.py\n"
            "--- a/utils.py\n"
            "+++ b/utils.py\n"
            "@@ -1,7 +1,5 @@\n"
            " def process_data(data):\n"
            "     if not data:\n"
            "-        print('Error: No data provided')\n"
            "-        logger.error('Data is missing')\n"
            "-        return None\n"
            "+        raise ValueError('Data is missing')\n"
            "     return result\n"
        )[0][0]
        result = apply_operation(operation, file_content, True)
        self.assertIn("raise ValueError", result)
        self.assertNotIn("print('Error", result)
        self.assertNotIn("logger.error", result)
        self.assertIn("result = transform(data)", result)

    # ── Scenario B: ambiguous hard anchors, context disambiguates ─────────────

    def test_context_scoring_selects_correct_region_when_removes_are_duplicated(self):
        """The exact scenario from the algorithm proposal: two identical remove-blocks,
        context scoring picks the right one."""
        file_content = (
            "def process_data(data):\n"
            "    if not data:\n"
            "        print('Error: No data provided')\n"
            "        logger.error('Data is missing')\n"
            "        return None\n"
            "    result = transform(data)\n"
            "    return result\n"
            "\n"
            "def process_other(other):\n"
            "    if not other:\n"
            "        print('Error: No data provided')\n"
            "        logger.error('Data is missing')\n"
            "        return None\n"
            "    return transform(other)\n"
        )
        # Context line 'def process_data(data):' uniquely identifies the first block
        operation = parse_operations(
            "diff --git a/utils.py b/utils.py\n"
            "--- a/utils.py\n"
            "+++ b/utils.py\n"
            "@@ -1,7 +1,5 @@\n"
            " def process_data(data):\n"
            "     if not data:\n"
            "-        print('Error: No data provided')\n"
            "-        logger.error('Data is missing')\n"
            "-        return None\n"
            "+        raise ValueError('Data is missing')\n"
            "     return result\n"
        )[0][0]
        result = apply_operation(operation, file_content, True)
        # First block replaced
        lines = result.splitlines()
        self.assertIn("        raise ValueError('Data is missing')", lines)
        # Second block untouched
        self.assertIn("        print('Error: No data provided')", lines)

    # ── Scenario B tie: context score ties → reject with useful message ───────

    def test_ambiguous_removes_with_tied_context_score_are_rejected(self):
        """Two identical blocks with identical surrounding context → tie → error."""
        file_content = (
            "def a():\n"
            "    x = 1\n"
            "    return x\n"
            "\n"
            "def b():\n"
            "    x = 1\n"
            "    return x\n"
        )
        operation = parse_operations(
            "diff --git a/f.py b/f.py\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-    x = 1\n"
            "+    x = 99\n"
        )[0][0]
        with self.assertRaisesRegex(DiffProtocolError, "ambiguous"):
            apply_operation(operation, file_content, True)

    # ── Scenario C: hard anchors not in file → precise error ─────────────────

    def test_remove_lines_not_in_file_raises_precise_error(self):
        """If - lines don't exist in the file, the error says to fix them."""
        operation = parse_operations(
            "diff --git a/f.py b/f.py\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1 +1 @@\n"
            "-hallucinated_line = True\n"
            "+real_line = True\n"
        )[0][0]
        with self.assertRaisesRegex(DiffProtocolError, "not found"):
            apply_operation(operation, "real_line = False\n", True)

    # ── Pure insertion (zero - lines, context locates position) ───────────────

    def test_pure_insertion_appends_after_context_line(self):
        """A hunk with only context and + lines inserts without needing - lines."""
        file_content = "TIMEOUT = 10\nDEBUG = False\n"
        operation = parse_operations(
            "diff --git a/cfg.py b/cfg.py\n"
            "--- a/cfg.py\n"
            "+++ b/cfg.py\n"
            "@@ -1,2 +1,3 @@\n"
            " TIMEOUT = 10\n"
            "+LOG_LEVEL = 'INFO'\n"
            " DEBUG = False\n"
        )[0][0]
        result = apply_operation(operation, file_content, True)
        self.assertIn("LOG_LEVEL = 'INFO'", result)
        self.assertIn("TIMEOUT = 10", result)
        self.assertIn("DEBUG = False", result)

    # ── Many-to-one replacement (3 - lines replaced by 1 + line) ─────────────

    def test_many_to_one_replacement(self):
        """N remove lines replaced by M add lines (N≠M) works correctly."""
        file_content = (
            "def f():\n"
            "    if not data:\n"
            "        print('a')\n"
            "        log('b')\n"
            "        return None\n"
            "    return data\n"
        )
        operation = parse_operations(
            "diff --git a/f.py b/f.py\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,6 +1,4 @@\n"
            " def f():\n"
            "     if not data:\n"
            "-        print('a')\n"
            "-        log('b')\n"
            "-        return None\n"
            "+        raise ValueError('bad')\n"
            "     return data\n"
        )[0][0]
        result = apply_operation(operation, file_content, True)
        self.assertIn("raise ValueError('bad')", result)
        self.assertNotIn("print('a')", result)
        self.assertNotIn("log('b')", result)
        self.assertIn("return data", result)


if __name__ == "__main__":
    unittest.main()
