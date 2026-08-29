"""
Tests for codepilot/core/conflict_protocol.py

Covers:
  - parse_blocks: happy path, fuzzy markers, missing path, missing sep/close
  - apply_block: write/create, exact edit, indentation-stripped fallback,
    first+last anchor, progressive interior expansion, context scoring,
    multi-block same file, SEARCH-not-found error, ambiguous error
  - format_parse_error / format_apply_error: message content checks
"""

import unittest

from codepilot.core.conflict_protocol import (
    BlockOperation,
    ConflictProtocolError,
    ParseError,
    apply_block,
    format_apply_error,
    format_parse_error,
    parse_blocks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block(path: str, search: str, replace: str) -> str:
    """Build a syntactically correct block string."""
    return (
        f"{path}\n"
        f"<<<<<<< SEARCH\n"
        f"{search}"
        f"=======\n"
        f"{replace}"
        f">>>>>>> REPLACE\n"
    )


# ---------------------------------------------------------------------------
# parse_blocks — happy path
# ---------------------------------------------------------------------------

class TestParseBlocksHappyPath(unittest.TestCase):

    def test_single_edit_block_parsed(self):
        text = _block("src/main.py", "def hello():\n    print('world')\n", "def hello():\n    print('universe')\n")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1)
        self.assertEqual(len(errors), 0)
        op = ops[0]
        self.assertEqual(op.path, "src/main.py")
        self.assertIn("world", op.search_text)
        self.assertIn("universe", op.replace_text)
        self.assertFalse(op.is_creation)

    def test_write_block_empty_search_is_creation(self):
        text = _block("src/new.py", "", "x = 1\n")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1)
        self.assertEqual(len(errors), 0)
        self.assertTrue(ops[0].is_creation)

    def test_multiple_blocks_parsed_in_order(self):
        text = (
            _block("a.py", "x = 1\n", "x = 2\n")
            + "\nSome natural language here.\n\n"
            + _block("b.py", "y = 1\n", "y = 99\n")
        )
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0].path, "a.py")
        self.assertEqual(ops[1].path, "b.py")

    def test_natural_text_outside_blocks_ignored(self):
        text = "I will now edit the file.\n\n" + _block("f.py", "a = 1\n", "a = 2\n") + "\nDone.\n"
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].path, "f.py")


# ---------------------------------------------------------------------------
# parse_blocks — fuzzy marker tolerance
# ---------------------------------------------------------------------------

class TestParseBlocksFuzzyMarkers(unittest.TestCase):

    def _make_block(self, open_m, sep, close_m, path="f.py"):
        return (
            f"{path}\n"
            f"{open_m}\n"
            f"old line\n"
            f"{sep}\n"
            f"new line\n"
            f"{close_m}\n"
        )

    def test_5_angle_open_accepted(self):
        text = self._make_block("<<<<< SEARCH", "=======", ">>>>>>> REPLACE")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1, errors)

    def test_9_angle_open_accepted(self):
        text = self._make_block("<<<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1, errors)

    def test_search_suffix_variant_SRCH(self):
        text = self._make_block("<<<<<<< SRCH", "=======", ">>>>>>> REPLACE")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1, errors)

    def test_replace_suffix_variant_RPLACE(self):
        text = self._make_block("<<<<<<< SEARCH", "=======", ">>>>>>> RPLACE")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1, errors)

    def test_separator_with_more_equals(self):
        text = self._make_block("<<<<<<< SEARCH", "============", ">>>>>>> REPLACE")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1, errors)

    def test_lowercase_suffix_accepted(self):
        text = self._make_block("<<<<<<< search", "=======", ">>>>>>> replace")
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 1, errors)


# ---------------------------------------------------------------------------
# parse_blocks — error cases
# ---------------------------------------------------------------------------

class TestParseBlocksErrors(unittest.TestCase):

    def test_missing_separator_returns_parse_error(self):
        # No ======= line
        text = "f.py\n<<<<<<< SEARCH\nold\n>>>>>>> REPLACE\n"
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 0)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ParseError)
        self.assertIn("separator", errors[0].reason.lower())

    def test_missing_close_marker_returns_parse_error(self):
        text = "f.py\n<<<<<<< SEARCH\nold\n=======\nnew\n"
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 0)
        self.assertEqual(len(errors), 1)
        self.assertIn("REPLACE", errors[0].reason)

    def test_missing_path_returns_parse_error(self):
        # Block starts at the very beginning of text with no preceding non-blank line
        text = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 0)
        self.assertEqual(len(errors), 1)
        self.assertIn("path", errors[0].reason.lower())

    def test_good_and_bad_block_both_returned(self):
        good = _block("good.py", "x = 1\n", "x = 2\n")
        bad = "bad.py\n<<<<<<< SEARCH\nold\n>>>>>>> REPLACE\n"  # missing sep
        ops, errors = parse_blocks(good + bad)
        self.assertEqual(len(ops), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(ops[0].path, "good.py")


# ---------------------------------------------------------------------------
# apply_block — write / create
# ---------------------------------------------------------------------------

class TestApplyBlockCreate(unittest.TestCase):

    def test_empty_search_overwrites_existing_content(self):
        op = parse_blocks(_block("f.py", "", "brand new\n"))[0][0]
        result = apply_block(op, "old content\n")
        self.assertEqual(result, "brand new\n")

    def test_empty_search_creates_new_content(self):
        op = parse_blocks(_block("f.py", "", "def main():\n    pass\n"))[0][0]
        result = apply_block(op, "")
        self.assertEqual(result, "def main():\n    pass\n")

    def test_trailing_newline_added_if_missing(self):
        op = parse_blocks("f.py\n<<<<<<< SEARCH\n=======\nno newline\n>>>>>>> REPLACE\n")[0][0]
        result = apply_block(op, "")
        self.assertTrue(result.endswith("\n"))


# ---------------------------------------------------------------------------
# apply_block — exact edit
# ---------------------------------------------------------------------------

class TestApplyBlockEdit(unittest.TestCase):

    def test_simple_single_line_replacement(self):
        file_content = "x = 1\ny = 2\n"
        op = parse_blocks(_block("f.py", "x = 1\n", "x = 99\n"))[0][0]
        result = apply_block(op, file_content)
        self.assertIn("x = 99", result)
        self.assertIn("y = 2", result)
        self.assertNotIn("x = 1", result)

    def test_multi_line_replacement(self):
        file_content = "def hello():\n    print('world')\n    return None\n"
        op = parse_blocks(_block(
            "f.py",
            "def hello():\n    print('world')\n",
            "def hello():\n    print('universe')\n",
        ))[0][0]
        result = apply_block(op, file_content)
        self.assertIn("universe", result)
        self.assertNotIn("world", result)
        self.assertIn("return None", result)

    def test_search_not_in_file_raises_error(self):
        op = parse_blocks(_block("f.py", "nonexistent line\n", "replacement\n"))[0][0]
        with self.assertRaises(ConflictProtocolError) as ctx:
            apply_block(op, "x = 1\ny = 2\n")
        self.assertIn("not found", str(ctx.exception).lower())

    def test_search_not_found_error_is_actionable(self):
        op = parse_blocks(_block("f.py", "fake content\n", "new\n"))[0][0]
        try:
            apply_block(op, "actual content\n")
        except ConflictProtocolError as exc:
            msg = format_apply_error(op, exc)
            self.assertIn("view_file", msg)
            self.assertIn("f.py", msg)


# ---------------------------------------------------------------------------
# apply_block — indentation-stripped fallback (Pass 2)
# ---------------------------------------------------------------------------

class TestApplyBlockIndentStrip(unittest.TestCase):

    def test_wrong_indentation_in_search_falls_back_to_stripped_match(self):
        file_content = (
            "def process():\n"
            "    result = compute()\n"
            "    return result\n"
        )
        # Model emits SEARCH with wrong leading whitespace (2 spaces instead of 4)
        op = parse_blocks(_block(
            "f.py",
            "  result = compute()\n",   # wrong indent
            "    result = compute() * 2\n",
        ))[0][0]
        result = apply_block(op, file_content)
        self.assertIn("compute() * 2", result)
        self.assertIn("return result", result)

    def test_no_indentation_in_search_matches_indented_file_line(self):
        file_content = "class Foo:\n    def bar(self):\n        pass\n"
        op = parse_blocks(_block(
            "f.py",
            "def bar(self):\n",   # no indent
            "    def bar(self, x=1):\n",
        ))[0][0]
        result = apply_block(op, file_content)
        self.assertIn("x=1", result)


# ---------------------------------------------------------------------------
# apply_block — first+last anchor + progressive interior expansion
# ---------------------------------------------------------------------------

class TestApplyBlockAnchorExpansion(unittest.TestCase):

    def test_first_last_anchor_unique_region(self):
        file_content = (
            "header\n"
            "start_marker\n"
            "middle_line\n"
            "end_marker\n"
            "footer\n"
        )
        op = parse_blocks(_block(
            "f.py",
            "start_marker\nmiddle_line\nend_marker\n",
            "REPLACED\n",
        ))[0][0]
        result = apply_block(op, file_content)
        self.assertIn("REPLACED", result)
        self.assertIn("header", result)
        self.assertIn("footer", result)
        self.assertNotIn("start_marker", result)
        self.assertNotIn("middle_line", result)

    def test_ambiguous_first_last_uses_more_interior(self):
        # File has two identical first+last pairs; interior line disambiguates.
        block1 = "START\nUNIQUE_A\nEND\n"
        block2 = "START\nUNIQUE_B\nEND\n"
        file_content = block1 + block2
        op = parse_blocks(_block(
            "f.py",
            "START\nUNIQUE_A\nEND\n",
            "REPLACED\n",
        ))[0][0]
        result = apply_block(op, file_content)
        self.assertIn("REPLACED", result)
        self.assertIn("UNIQUE_B", result)
        self.assertNotIn("UNIQUE_A", result)

    def test_fully_ambiguous_raises_conflict_error(self):
        block = "START\nSAME\nEND\n"
        file_content = block + block
        op = parse_blocks(_block("f.py", "START\nSAME\nEND\n", "NEW\n"))[0][0]
        with self.assertRaises(ConflictProtocolError) as ctx:
            apply_block(op, file_content)
        self.assertIn("ambiguous", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# apply_block — ambiguity is always an immediate error (no scoring bypass)
# ---------------------------------------------------------------------------

class TestApplyBlockAmbiguityIsImmediate(unittest.TestCase):

    def test_two_identical_regions_raise_ambiguous(self):
        # Two functions with identical bodies. No tie-breaking — LLM must
        # add more lines to SEARCH to disambiguate.
        file_content = (
            "def process_data():\n"
            "    x = 1\n"
            "    return x\n"
            "\n"
            "def process_other():\n"
            "    x = 1\n"
            "    return x\n"
        )
        op = parse_blocks(_block("f.py", "    x = 1\n", "    x = 99\n"))[0][0]
        with self.assertRaises(ConflictProtocolError) as ctx:
            apply_block(op, file_content)
        self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_ambiguity_error_tells_llm_to_add_lines(self):
        file_content = "    return True\n" * 2
        op = parse_blocks(_block("f.py", "    return True\n", "    return False\n"))[0][0]
        try:
            apply_block(op, file_content)
            self.fail("Expected ConflictProtocolError")
        except ConflictProtocolError as exc:
            msg = str(exc)
            self.assertIn("ambiguous", msg.lower())
            self.assertIn("surrounding lines", msg)


# ---------------------------------------------------------------------------
# apply_block — multi-block same file (simulated sequential apply)
# ---------------------------------------------------------------------------

class TestMultiBlockSameFile(unittest.TestCase):

    def test_two_edits_applied_sequentially(self):
        file_content = "alpha\nbeta\ngamma\n"

        text = (
            _block("f.py", "alpha\n", "ALPHA\n")
            + _block("f.py", "gamma\n", "GAMMA\n")
        )
        ops, errors = parse_blocks(text)
        self.assertEqual(len(ops), 2)
        self.assertEqual(len(errors), 0)

        working = file_content
        for op in ops:
            working = apply_block(op, working)

        self.assertIn("ALPHA", working)
        self.assertIn("beta", working)
        self.assertIn("GAMMA", working)
        self.assertNotIn("alpha", working)
        self.assertNotIn("gamma", working)


# ---------------------------------------------------------------------------
# Feedback message formatting
# ---------------------------------------------------------------------------

class TestFeedbackMessages(unittest.TestCase):

    def test_parse_error_message_includes_position_and_reason(self):
        err = ParseError(position=3, path="x.py", reason="missing separator")
        msg = format_parse_error(err)
        self.assertIn("#3", msg)
        self.assertIn("missing separator", msg)
        self.assertIn("SEARCH", msg)

    def test_apply_error_not_found_references_view_file(self):
        op = parse_blocks(_block("x.py", "bad\n", "good\n"))[0][0]
        exc = ConflictProtocolError("SEARCH block for 'x.py' was not found in the file.")
        msg = format_apply_error(op, exc)
        self.assertIn("view_file", msg)

    def test_apply_error_ambiguous_references_more_lines(self):
        op = parse_blocks(_block("x.py", "dupe\n", "good\n"))[0][0]
        exc = ConflictProtocolError("SEARCH block for 'x.py' is ambiguous.")
        msg = format_apply_error(op, exc)
        self.assertIn("unique", msg.lower())


# ---------------------------------------------------------------------------
# BlockOperation dataclass properties
# ---------------------------------------------------------------------------

class TestBlockOperationProperties(unittest.TestCase):

    def test_is_creation_true_for_empty_search(self):
        op = parse_blocks(_block("f.py", "", "content\n"))[0][0]
        self.assertTrue(op.is_creation)

    def test_is_creation_false_for_nonempty_search(self):
        op = parse_blocks(_block("f.py", "old\n", "new\n"))[0][0]
        self.assertFalse(op.is_creation)

    def test_source_contains_path_and_markers(self):
        op = parse_blocks(_block("src/app.py", "old\n", "new\n"))[0][0]
        self.assertIn("src/app.py", op.source)
        self.assertIn("SEARCH", op.source)
        self.assertIn("REPLACE", op.source)


if __name__ == "__main__":
    unittest.main()
