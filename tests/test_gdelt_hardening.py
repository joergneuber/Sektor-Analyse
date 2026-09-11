"""Regression tests for GDELT publication-lag and request-burst hardening."""
from __future__ import annotations
import datetime as dt
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "yfinance" not in sys.modules:
    sys.modules["yfinance"] = types.SimpleNamespace()
spec = importlib.util.spec_from_file_location("makro_szenario_under_test", ROOT / "makro_szenario.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

class FakeResponse:
    def __init__(self, status_code: int): self.status_code = status_code
    def raise_for_status(self):
        if self.status_code != 200: raise RuntimeError(f"HTTP {self.status_code}")

class TestGDELTHardening(unittest.TestCase):
    def test_gkg_anchor_steps_back_15_minutes_after_404(self):
        latest = dt.datetime(2026, 9, 10, 20, 30, tzinfo=dt.timezone.utc)
        seen = []
        def fake_get(url, **kwargs):
            seen.append(url)
            return FakeResponse(404 if url.endswith("203000.gkg.csv.zip") else 200)
        original = mod.requests.get
        mod.requests.get = fake_get
        try:
            anchor, response = mod._gdelt_gkg_find_available_anchor(latest)
            self.assertEqual(anchor, dt.datetime(2026, 9, 10, 20, 15, tzinfo=dt.timezone.utc))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(seen, [
                f"{mod.GDELT_GKG_BASE_URL}20260910203000.gkg.csv.zip",
                f"{mod.GDELT_GKG_BASE_URL}20260910201500.gkg.csv.zip",
            ])
        finally:
            mod.requests.get = original

    def test_gkg_anchor_rejects_future_lastupdate_timestamp(self):
        now = dt.datetime.now(dt.timezone.utc)
        current_slot = now.replace(
            minute=(now.minute // 15) * 15, second=0, microsecond=0
        )
        latest = current_slot + dt.timedelta(minutes=15)
        seen = []
        def fake_get(url, **kwargs):
            seen.append(url)
            return FakeResponse(200)
        original = mod.requests.get
        mod.requests.get = fake_get
        try:
            anchor, response = mod._gdelt_gkg_find_available_anchor(latest)
            self.assertEqual(anchor, current_slot)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                seen,
                [f"{mod.GDELT_GKG_BASE_URL}{current_slot.strftime('%Y%m%d%H%M%S')}.gkg.csv.zip"],
            )
            self.assertLessEqual(anchor, dt.datetime.now(dt.timezone.utc))
        finally:
            mod.requests.get = original

    def test_gkg_anchor_raises_after_full_lookback(self):
        latest = dt.datetime(2026, 9, 10, 20, 30, tzinfo=dt.timezone.utc)
        original = mod.requests.get
        mod.requests.get = lambda *a, **k: FakeResponse(404)
        try:
            with self.assertRaisesRegex(RuntimeError, "kein verfuegbarer 15-Minuten-Anker"):
                mod._gdelt_gkg_find_available_anchor(latest)
        finally:
            mod.requests.get = original

    def test_gkg_anchor_does_not_hide_non_404_http_errors(self):
        latest = dt.datetime(2026, 9, 10, 20, 30, tzinfo=dt.timezone.utc)
        original = mod.requests.get
        mod.requests.get = lambda *a, **k: FakeResponse(503)
        try:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                mod._gdelt_gkg_find_available_anchor(latest)
        finally:
            mod.requests.get = original

    def test_doc_concurrency_is_capped_at_two(self):
        self.assertEqual(mod.GDELT_DOC_MAX_WORKERS, 2)

if __name__ == "__main__":
    unittest.main()
