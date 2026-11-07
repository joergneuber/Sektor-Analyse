from __future__ import annotations

import csv
import hashlib
import sqlite3
from contextlib import closing
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ohlcv_quality_gate as gate
from ohlcv_quality_gate import apply_ohlcv_quality_gate, calculate_valid_atr14


def _sessions(ticker="AAPL", n=20, end="2026-11-05"):
    import historical_ohlcv as ho
    calendar = ho._calendar_name_for_ticker(ticker)
    end_ts = pd.Timestamp(end)
    start = (end_ts - pd.Timedelta(days=n * 3 + 20)).date().isoformat()
    dates = ho._calendar_session_dates(calendar, start, end_ts.date().isoformat())
    dates = sorted(ho._expected_daily_bar_dates(calendar, dates))
    return pd.DatetimeIndex(pd.to_datetime(dates[-n:]))


def _frame(index):
    values = [100.0 + i for i in range(len(index))]
    return pd.DataFrame({
        "Open": values,
        "High": [x + 2 for x in values],
        "Low": [x - 2 for x in values],
        "Close": [x + 1 for x in values],
        "Volume": [1000] * len(values),
    }, index=index)



def _apply_gate(frame, ticker, **kwargs):
    # Deterministic historical fixture: evaluate immediately after the last
    # fixture session, before any later exchange session can enter the window.
    as_of = pd.Timestamp(frame.index.max()).tz_localize("UTC") + pd.Timedelta(days=1, hours=12)
    kwargs.setdefault("as_of", as_of)
    return apply_ohlcv_quality_gate(frame, ticker, **kwargs)

def test_20_session_window_accepts_exactly_95_percent_with_one_exclusion():
    idx = _sessions(n=20)
    frame = _frame(idx)
    frame.loc[idx[6], "High"] = frame.loc[idx[6], "Close"] - 1
    gated, result = _apply_gate(frame, "AAPL", window_sessions=20)
    assert result.quality_status == "PASS_WITH_EXCLUSIONS"
    assert result.valid_bars == 19
    assert result.valid_ratio == 0.95
    assert pd.isna(gated.loc[idx[6], "Close"])
    assert gated.attrs["ohlcv_quality_exclusions"]


def test_50_session_window_accepts_two_separated_exclusions():
    idx = _sessions(n=50)
    frame = _frame(idx)
    for pos in (8, 31):
        frame.loc[idx[pos], "High"] = frame.loc[idx[pos], "Close"] - 1
    _, result = _apply_gate(frame, "AAPL", window_sessions=50)
    assert result.quality_status == "PASS_WITH_EXCLUSIONS"
    assert result.valid_bars == 48
    assert result.valid_ratio == 0.96
    assert result.max_consecutive_exclusions == 1


def test_two_consecutive_exclusions_fail_closed():
    idx = _sessions(n=20)
    frame = _frame(idx)
    frame.loc[idx[7:9], "High"] = frame.loc[idx[7:9], "Close"] - 1
    _, result = _apply_gate(frame, "AAPL", window_sessions=20)
    assert result.quality_status == "INCOMPLETE"
    assert result.max_consecutive_exclusions == 2
    assert "CONSECUTIVE_EXCLUSIONS_ABOVE_1" in result.reason_codes


def test_latest_expected_bar_must_be_valid():
    idx = _sessions(n=20)
    frame = _frame(idx)
    frame.loc[idx[-1], "High"] = frame.loc[idx[-1], "Close"] - 1
    _, result = _apply_gate(frame, "AAPL", window_sessions=20)
    assert result.quality_status == "INCOMPLETE"
    assert not result.latest_bar_valid


def test_last_three_sessions_are_required_for_structure():
    idx = _sessions(n=20)
    frame = _frame(idx)
    frame.loc[idx[-2], "High"] = frame.loc[idx[-2], "Close"] - 1
    _, result = _apply_gate(
        frame, "AAPL", window_sessions=20, require_structure=True
    )
    assert result.quality_status == "INCOMPLETE"
    assert not result.last_3_bars_valid
    assert "LAST_3_EXPECTED_BARS_INVALID" in result.reason_codes


def test_registry_confirmed_invalid_masks_exact_date():
    idx = _sessions("ABBNY", 20, "2026-11-05")
    assert pd.Timestamp("2026-11-05") in idx
    frame = _frame(idx)
    gated, result = _apply_gate(frame, "ABBNY", window_sessions=20)
    assert pd.isna(gated.loc[pd.Timestamp("2026-11-05"), "Close"])
    assert "REGISTRY_CONFIRMED_INVALID" in result.reason_codes


def test_documented_gap_registry_inserts_nan_without_time_compression():
    gap_path = ROOT / "ohlcv_quality_gap_registry.csv"
    with gap_path.open("r", encoding="utf-8-sig", newline="") as handle:
        record = next(csv.DictReader(handle))
    ticker = record["ticker"]
    gap_date = pd.Timestamp(record["missing_date"])
    idx_all = _sessions(ticker, 22, (gap_date + pd.Timedelta(days=20)).date().isoformat())
    if gap_date not in idx_all:
        idx_all = idx_all.union(pd.DatetimeIndex([gap_date])).sort_values()
    idx = idx_all.delete(idx_all.get_loc(gap_date))
    frame = _frame(idx)
    gated, result = _apply_gate(frame, ticker, window_sessions=20)
    assert gap_date in gated.index
    assert pd.isna(gated.loc[gap_date, "Close"])
    assert "MISSING_EXPECTED_SESSION" in result.reason_codes


def test_special_case_requires_approval_evidence_rule_and_explicit_reason_codes(tmp_path):
    idx = _sessions("GC=F", n=20)
    bad_date = idx[-2].date().isoformat()
    frame = _frame(idx)
    frame.loc[idx[-2], "High"] = frame.loc[idx[-2], "Close"] - 0.5
    registry = tmp_path / "special.csv"
    fields = ["ticker", "date", "classification", "usage_decision", "approved_by",
              "evidence_ref", "rule_version", "allowed_reason_codes"]
    with registry.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "ticker": "GC=F", "date": bad_date, "classification": "SPECIAL_CASE",
            "usage_decision": "ALLOW_WITH_RULE", "approved_by": "", "evidence_ref": "",
            "rule_version": "", "allowed_reason_codes": "HIGH_BELOW_OPEN_OR_CLOSE",
        })
    gated, result = _apply_gate(frame, "GC=F", registry_path=registry, window_sessions=20)
    assert pd.isna(gated.loc[idx[-2], "Close"])
    assert "SPECIAL_CASE_NOT_APPROVED_OR_EVIDENCED" in result.reason_codes

    # A non-empty but unknown rule version is not an approval.
    with registry.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "ticker": "GC=F", "date": bad_date, "classification": "SPECIAL_CASE",
            "usage_decision": "ALLOW_WITH_RULE", "approved_by": "Projektfreigabe",
            "evidence_ref": "evidence://approved-test", "rule_version": "1.0",
            "allowed_reason_codes": "HIGH_BELOW_OPEN_OR_CLOSE",
        })
    from ohlcv_quality_gate import _load_registry
    _load_registry.cache_clear()
    gated, result = _apply_gate(frame, "GC=F", registry_path=registry, window_sessions=20)
    assert pd.isna(gated.loc[idx[-2], "Close"])
    assert "SPECIAL_CASE_NOT_APPROVED_OR_EVIDENCED" in result.reason_codes

    with registry.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "ticker": "GC=F", "date": bad_date, "classification": "SPECIAL_CASE",
            "usage_decision": "ALLOW_WITH_RULE", "approved_by": "Projektfreigabe",
            "evidence_ref": "evidence://approved-test", "rule_version": "CME_SETTLEMENT_CLOSE_OUTSIDE_RANGE_V1",
            "allowed_reason_codes": "HIGH_BELOW_OPEN_OR_CLOSE",
        })
    from ohlcv_quality_gate import _load_registry
    _load_registry.cache_clear()
    gated, result = _apply_gate(frame, "GC=F", registry_path=registry, window_sessions=20)
    assert pd.notna(gated.loc[idx[-2], "Close"])
    assert result.excluded_bars == 0
    assert gated.attrs["ohlcv_quality_special_cases"]

    # The same rule/version cannot be reused for an unrelated instrument.
    other, other_result = _apply_gate(
        frame, "AAPL", registry_path=registry, window_sessions=20
    )
    assert pd.isna(other.loc[idx[-2], "Close"])
    assert other_result.excluded_bars == 1


def test_unknown_calendar_fails_closed():
    idx = _sessions(n=20)
    frame = _frame(idx)
    _, result = _apply_gate(frame, "UNKNOWN.VERYBAD", window_sessions=20)
    assert result.quality_status == "INCOMPLETE"
    assert "CALENDAR_UNVERIFIED_OR_UNAVAILABLE" in result.reason_codes


def test_atr14_uses_valid_true_ranges_and_immediate_previous_close():
    idx = _sessions(n=21)
    frame = _frame(idx)
    atr, status = calculate_valid_atr14(frame, ticker="AAPL")
    assert status == "PASS" and atr is not None and atr > 0

    frame.loc[idx[3], "Close"] = float("nan")
    atr, status = calculate_valid_atr14(frame, ticker="AAPL")
    assert status == "PASS" and atr is not None and atr > 0

    frame.loc[idx[-2], "Close"] = float("nan")
    atr, status = calculate_valid_atr14(frame, ticker="AAPL")
    assert atr is None and status == "INCOMPLETE"

    frame = _frame(idx)
    frame.loc[idx[3:9], ["Open", "High", "Low", "Close"]] = float("nan")
    atr, status = calculate_valid_atr14(frame, ticker="AAPL")
    assert atr is None and status == "INCOMPLETE"

    # Removing a row from the input index must not compress expected sessions.
    frame = _frame(idx).drop(index=idx[-2])
    atr, status = calculate_valid_atr14(frame, ticker="AAPL")
    assert atr is None and status == "INCOMPLETE"


def test_historical_loader_masks_confirmed_invalid_without_mutating_sqlite(tmp_path, monkeypatch):
    import historical_ohlcv as ho
    monkeypatch.setattr(gate, '_last_completed_session_date', lambda ticker, as_of=None: '2026-11-05')
    db = tmp_path / "historical_ohlcv.sqlite"
    ho.init_db(db)
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            conn.execute(
                """INSERT INTO bars
                   (ticker,date,open,high,low,close,adj_close,volume,source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("ABBNY", "2026-11-05", 84.59, 84.59, 81.48, 81.36, 81.36, 46733, "test"),
            )
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    loaded = ho.load_history("ABBNY", db)
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert len(loaded) == 1
    assert pd.isna(loaded.iloc[0]["Open"])
    assert pd.isna(loaded.iloc[0]["Close"])
    assert before == after
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_performance_return_does_not_bridge_missing_expected_session():
    from quellenwert_vertrag import four_week_return
    closes = [100.0 + i for i in range(21)]
    closes[10] = float("nan")
    assert four_week_return(closes, sessions=20) is None


def test_performance_benchmark_preserves_pass_with_exclusions(monkeypatch):
    monkeypatch.setattr(gate, '_last_completed_session_date', lambda ticker, as_of=None: '2026-11-05')
    import importlib
    import types
    import sys
    fake_yf = types.SimpleNamespace(download=lambda *args, **kwargs: frame)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    import performance_benchmark as benchmark
    benchmark = importlib.reload(benchmark)
    idx = _sessions(n=20)
    frame = _frame(idx)
    frame.loc[idx[6], ["Open", "High", "Low", "Close", "Volume"]] = float("nan")
    monkeypatch.setattr(benchmark.yf, "download", lambda *args, **kwargs: frame)
    result = benchmark._download_close("AAPL", idx[0].date(), idx[-1].date())
    assert len(result) == 20
    assert pd.isna(result.loc[idx[6]])
    assert result.attrs["ohlcv_quality"]["quality_status"] == "PASS_WITH_EXCLUSIONS"


def test_performance_benchmark_blocks_consecutive_gap_window(monkeypatch):
    monkeypatch.setattr(gate, '_last_completed_session_date', lambda ticker, as_of=None: '2026-11-05')
    import importlib
    import types
    import sys
    fake_yf = types.SimpleNamespace(download=lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    import performance_benchmark as benchmark
    benchmark = importlib.reload(benchmark)
    idx = _sessions(n=20)
    frame = _frame(idx)
    frame.loc[idx[6:8], ["Open", "High", "Low", "Close", "Volume"]] = float("nan")
    monkeypatch.setattr(benchmark.yf, "download", lambda *args, **kwargs: frame)
    result = benchmark._download_close("AAPL", idx[0].date(), idx[-1].date())
    assert result.empty
    assert result.attrs["ohlcv_quality"]["quality_status"] == "INCOMPLETE"


def test_missing_quality_registry_fails_closed(tmp_path):
    idx = _sessions(n=20)
    frame = _frame(idx)
    _, result = _apply_gate(
        frame, "AAPL", registry_path=tmp_path / "missing_registry.csv", window_sessions=20
    )
    assert result.quality_status == "INCOMPLETE"
    assert "QUALITY_REGISTRY_MISSING" in result.reason_codes
