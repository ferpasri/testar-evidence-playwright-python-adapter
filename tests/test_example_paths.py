"""The example reserves unique trace directories and preserves explicit paths."""

from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from examples.parabank_denied_loan import create_trace_directory, output_directory, safe_label


class ExamplePathTests(unittest.TestCase):
    def test_repeated_timestamp_preserves_existing_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            instant = datetime(2026, 10, 22, 15, 30, 12, 123456)
            first = create_trace_directory(parent, "parabank", "denied-loan", instant)
            marker = first / "evidence.jsonl"
            marker.write_text("original evidence", encoding="utf-8")
            second = create_trace_directory(parent, "parabank", "denied-loan", instant)
            self.assertEqual(first.name, "parabank_denied-loan_trace-2026-10-22_15-30-12-123")
            self.assertEqual(second.name, first.name + "-2")
            self.assertEqual(marker.read_text(encoding="utf-8"), "original evidence")
            self.assertTrue(second.is_dir())

    def test_supplied_path_remains_exact_and_is_not_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            requested = Path(temporary) / "my-loan-evidence"
            self.assertEqual(output_directory(requested), requested)
            self.assertFalse(requested.exists())

    def test_labels_are_safe_and_bounded(self):
        self.assertEqual(safe_label("../customer portal", "sut"), "customer-portal")
        self.assertEqual(safe_label("////", "sut"), "sut")
        self.assertEqual(safe_label("x" * 80, "sut"), "x" * 32)


if __name__ == "__main__":
    unittest.main()
