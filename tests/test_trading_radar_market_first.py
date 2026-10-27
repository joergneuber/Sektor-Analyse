from datetime import datetime, timezone, timedelta
import pytest
from trading_radar_market_first import normalize_event, deduplicate_events, classify_ohlcv_row, summarize_ohlcv_status


def test_event_missing_time_timezone_marked_once_and_not_verified():
    event = normalize_event({"name": "FOMC", "date": "2026-10-28", "source": "Fed",
                             "time": "NICHT VERFÜGBAR", "timezone": "NICHT VERFÜGBAR"})
    assert event["time"] is None
    assert event["timezone"] is None
    assert event["time_verified"] is False
    assert event["status"] == "READY"
    assert event["missing_required"] == []


def test_event_duplicates_are_collapsed():
    rows = [{"name": "CPI", "date": "2026-11-12", "source": "BLS"},
            {"name": "CPI", "date": "2026-11-12", "source": "BLS"}]
    assert len(deduplicate_events(rows)) == 1


def test_event_missing_required_fields_are_explicit():
    row = normalize_event({"name": "CPI"})
    assert row["status"] == "INCOMPLETE"
    assert row["missing_required"] == ["date", "source"]


def test_ohlcv_missing_stale_invalid_and_valid_are_separate():
    cutoff = datetime(2026, 10, 27, tzinfo=timezone.utc)
    base = {"open": 10, "high": 12, "low": 9, "close": 11, "volume": 100,
            "timestamp": "2026-10-26T00:00:00+00:00"}
    assert classify_ohlcv_row({**base, "close": None}, cutoff=cutoff) == "MISSING"
    assert classify_ohlcv_row({**base, "timestamp": "2026-09-01T00:00:00+00:00"}, cutoff=cutoff) == "STALE"
    assert classify_ohlcv_row({**base, "high": 8}, cutoff=cutoff) == "INVALID"
    assert classify_ohlcv_row(base, cutoff=cutoff) == "VALID"


def test_ohlcv_summary_counts_only_known_states():
    assert summarize_ohlcv_status(["VALID", "MISSING", "STALE", "INVALID"]) == {
        "INVALID": 1, "MISSING": 1, "STALE": 1, "VALID": 1, "TOTAL": 4}
    with pytest.raises(ValueError):
        summarize_ohlcv_status(["UNKNOWN"])
