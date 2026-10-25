"""Regression tests for persistent, throttled company-name resolution."""
import json
import os
import re
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "analyse.py"


class FakeYahoo:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result or {"longName": "Example Corporation"}
        self.error = error

    def Ticker(self, ticker):
        outer = self
        class FakeTicker:
            @property
            def info(self):
                outer.calls += 1
                if outer.error:
                    raise outer.error
                return outer.result
        return FakeTicker()


def load_lookup(cache_path, yahoo):
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("# Lokale Namen haben Vorrang")
    end = source.index("\ndef get_analyst_target(", start)
    namespace = {
        "os": os, "json": json, "time": time, "re": re,
        "lru_cache": lru_cache, "threading": threading, "yf": yahoo, "__file__": str(SOURCE),
    }
    os.environ["FIRMA_NAME_CACHE_PATH"] = str(cache_path)
    os.environ["FIRMA_NAME_MIN_INTERVAL_SECONDS"] = "0"
    os.environ["FIRMA_NAME_FAILURE_TTL_SECONDS"] = "86400"
    exec(compile(source[start:end], str(SOURCE), "exec"), namespace)
    namespace["_FIRMA_NAME_LOCAL_INDEX"] = {}
    return namespace


class FirmaNameCacheTests(unittest.TestCase):
    def test_success_is_persisted_and_second_process_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "names.json"
            yahoo = FakeYahoo({"longName": "Example Corporation"})
            first = load_lookup(cache, yahoo)
            self.assertEqual(first["_hole_firma_name"]("EXM"), "Example Corporation")
            self.assertTrue(cache.exists())
            second_yahoo = FakeYahoo(error=RuntimeError("should not be called"))
            second = load_lookup(cache, second_yahoo)
            self.assertEqual(second["_hole_firma_name"]("EXM"), "Example Corporation")
            self.assertEqual(second_yahoo.calls, 0)

    def test_negative_cache_prevents_immediate_repeat_after_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "names.json"
            yahoo = FakeYahoo(error=RuntimeError("Too Many Requests"))
            lookup = load_lookup(cache, yahoo)
            self.assertEqual(lookup["_hole_firma_name"]("NKE"), "NKE")
            self.assertEqual(lookup["_hole_firma_name"]("NKE"), "NKE")
            self.assertEqual(yahoo.calls, 1)
            saved = json.loads(cache.read_text(encoding="utf-8"))
            self.assertTrue(saved["NKE"]["missing"])
            self.assertIn("Too Many Requests", saved["NKE"]["last_error"])

    def test_local_universe_name_has_priority_over_yahoo(self):
        with tempfile.TemporaryDirectory() as tmp:
            yahoo = FakeYahoo(error=RuntimeError("should not be called"))
            lookup = load_lookup(Path(tmp) / "names.json", yahoo)
            lookup["_FIRMA_NAME_LOCAL_INDEX"] = {"NKE": "Nike, Inc."}
            self.assertEqual(lookup["_hole_firma_name"]("NKE"), "Nike, Inc.")
            self.assertEqual(yahoo.calls, 0)


if __name__ == "__main__":
    unittest.main()
