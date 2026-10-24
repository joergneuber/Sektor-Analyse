"""Regression tests for Trading-Radar status/upload gates.

These tests deliberately exercise local validation only; they do not contact
Google Drive, SMTP, Gemini, or GitHub Actions.
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "upload_to_drive.py"
spec = importlib.util.spec_from_file_location("upload_to_drive_under_test", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TradingRadarGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.report = Path(self.temp.name) / "Trading_Radar(2026-10-22).txt"
        self.report.write_text("1. Trading-Radar\\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def check(self, *, status="SUCCESS", fachlich="FINAL", check="success",
              skip=False, path=None):
        return module.validierter_trading_radar_report(
            status, fachlich, str(path or self.report), check, skip_upload=skip
        )

    def test_final_report_accepted(self):
        self.assertEqual(self.check(), str(self.report))

    def test_non_final_report_blocked_from_publication(self):
        self.assertIsNone(self.check(fachlich="NICHT_FINAL"))

    def test_bad_process_status_rejected(self):
        self.assertIsNone(self.check(status="FAILED"))

    def test_bad_report_check_rejected(self):
        self.assertIsNone(self.check(check="failure"))

    def test_upload_skip_rejected(self):
        self.assertIsNone(self.check(skip=True))

    def test_missing_report_rejected(self):
        self.assertIsNone(self.check(path=self.report.parent / "missing.txt"))

    def test_empty_report_rejected(self):
        empty = self.report.parent / "empty.txt"
        empty.write_text("", encoding="utf-8")
        self.assertIsNone(self.check(path=empty))

    def test_bad_filename_rejected(self):
        bad = self.report.parent / "Auswertung(2026-10-22).txt"
        bad.write_text("report", encoding="utf-8")
        self.assertIsNone(self.check(path=bad))


if __name__ == "__main__":
    unittest.main()
