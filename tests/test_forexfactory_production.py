"""Regression tests for the production ForexFactory event-discovery layer."""
import datetime as dt
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("yfinance", types.SimpleNamespace())

import makro_szenario as m


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def test_normalizer_core_aliases():
    cases = {
        "ADP Weekly Employment Change": "ADP",
        "Non-Farm Employment Change": "NFP",
        "Core CPI y/y": "CPI",
        "PPI m/m": "PPI",
        "JOLTS Job Openings": "JOLTS",
        "PCE Price Index": "PCE",
        "Gross Domestic Product q/q": "GDP",
        "ISM Manufacturing PMI": "ISM",
        "Unemployment Claims": "JOBLESS_CLAIMS",
        "Federal Funds Rate": "FOMC",
        "FOMC Press Conference": "FOMC",
    }
    for title, expected in cases.items():
        actual = m._normalize_macro_event({"country": "USD", "title": title})
        _assert(actual == expected, f"{title}: expected {expected}, got {actual}")


def test_normalizer_false_positive_guards():
    _assert(m._normalize_macro_event({"country": "USD", "title": "ADP Employment Change"}) == "ADP",
            "ADP must not be classified as NFP")
    _assert(m._normalize_macro_event({"country": "USD", "title": "GDPNow"}) is None,
            "GDPNow must not be classified as GDP")
    _assert(m._normalize_macro_event({"country": "USD", "title": "CPI Expectations"}) is None,
            "CPI Expectations must not be classified as CPI")


def test_macro_focus_prefers_today():
    today = dt.date(2026, 9, 15)
    events = [
        {"country": "USD", "date": "2026-09-15", "title": "ADP Weekly Employment Change"},
        {"country": "USD", "date": "2026-09-16", "title": "Federal Funds Rate"},
    ]
    focus, candidates = m._macro_focus_from_calendar(today, events)
    _assert(focus is not None, "focus missing")
    _assert(focus["canonical"] == "ADP", "today's event must take precedence")
    _assert(focus["priority"] == "HIGH", "ADP project priority must be HIGH")
    _assert(focus["fed"] == "HIGH", "ADP Fed relevance must be HIGH")
    _assert(len(candidates) == 2, "candidate count incorrect")


def test_macro_focus_has_no_directional_signal():
    today = dt.date(2026, 9, 15)
    events = [{"country": "USD", "date": "2026-09-15", "title": "ADP Weekly Employment Change"}]
    focus, _ = m._macro_focus_from_calendar(today, events)
    _assert("signal" not in focus, "MACRO_FOCUS must not contain a trading signal")
    _assert("buy" not in str(focus).lower(), "MACRO_FOCUS must not contain buy")
    _assert("sell" not in str(focus).lower(), "MACRO_FOCUS must not contain sell")


def test_forexfactory_fetch_is_cached_for_same_day():
    today = dt.date(2026, 9, 15)
    payload = [{"country": "USD", "date": "2026-09-15", "title": "ADP Weekly Employment Change"}]
    with patch.object(m, "FOREXFACTORY_CACHE_FILE", ROOT / ".test_ff_cache.json"):
        cache_file = ROOT / ".test_ff_cache.json"
        try:
            response = types.SimpleNamespace(status_code=200, json=lambda: payload, raise_for_status=lambda: None)
            with patch.object(m.requests, "get", return_value=response) as mocked:
                first, status1 = m._forexfactory_events(today)
                second, status2 = m._forexfactory_events(today)
                _assert(first == payload and second == payload, "cache payload mismatch")
                _assert(status1 == "LIVE", "first call must be LIVE")
                _assert(status2 == "CACHE", "second same-day call must use CACHE")
                _assert(mocked.call_count == 1, "ForexFactory must be fetched only once per cache window")
        finally:
            cache_file.unlink(missing_ok=True)


def test_macro_events_snapshot_contains_focus(monkeypatch=None):
    today = dt.date(2026, 9, 15)
    payload = [{"country": "USD", "date": "2026-09-15", "title": "ADP Weekly Employment Change"}]
    with patch.object(m, "_forexfactory_events", return_value=(payload, "LIVE")), \
         patch.object(m, "_next_fomc_date", return_value=None), \
         patch.object(m.requests, "get", side_effect=RuntimeError("ECB disabled in unit test")):
        lines = m.macro_events_snapshot(today)
    text = "\n".join(lines)
    _assert("MACRO_FOCUS=ADP" in text, "production snapshot missing ADP focus")
    _assert("FOCUS_EVENT=ADP Weekly Employment Change" in text, "focus event missing")
    _assert("FED_RELEVANCE=HIGH" in text, "Fed relevance missing")
    _assert("WALL_STREET_RELEVANCE=HIGH" in text, "Wall Street relevance missing")


def test_upcoming_macro_events_does_not_duplicate_official_fomc():
    today = dt.date(2026, 9, 15)
    official_meeting = dt.date(2026, 9, 16)
    ff_events = [
        {"country": "USD", "date": "2026-09-16", "title": "Federal Funds Rate"},
        {"country": "USD", "date": "2026-09-16", "title": "FOMC Statement"},
        {"country": "USD", "date": "2026-09-16", "title": "FOMC Press Conference"},
    ]
    with patch.object(m, "_next_fomc_date", return_value=official_meeting), \
         patch.object(m.requests, "get", side_effect=RuntimeError("ECB disabled in unit test")):
        events = m._upcoming_macro_events(today, ff_events=ff_events, ff_status="LIVE")
    fomc = [e for e in events if e[0] == official_meeting and "FOMC" in e[1]]
    _assert(len(fomc) == 1, f"official FOMC must not be duplicated: {events}")
    _assert(fomc[0][1] == "FOMC-Zinsentscheid", f"unexpected FOMC entry: {fomc}")
    _assert(fomc[0][2].startswith("Federal Reserve"), "official FOMC entry must remain authoritative")


if __name__ == "__main__":
    tests = [
        test_normalizer_core_aliases,
        test_normalizer_false_positive_guards,
        test_macro_focus_prefers_today,
        test_macro_focus_has_no_directional_signal,
        test_forexfactory_fetch_is_cached_for_same_day,
        test_upcoming_macro_events_does_not_duplicate_official_fomc,
        test_macro_events_snapshot_contains_focus,
    ]
    for test in tests:
        test()
        print("PASS:", test.__name__)
    print(f"FOREXFACTORY_PRODUCTION_TESTS: {len(tests)}/{len(tests)} PASS")
