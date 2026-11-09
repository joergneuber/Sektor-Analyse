from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
import ohlcv_quality_gate as gate


def _frame(dates):
    n = len(dates)
    return pd.DataFrame(
        {
            "Open": [100.0] * n,
            "High": [102.0] * n,
            "Low": [99.0] * n,
            "Close": [101.0] * n,
            "Volume": [1000.0] * n,
        },
        index=pd.DatetimeIndex(dates),
    )


def _install_calendar(monkeypatch, dates):
    monkeypatch.setattr(
        gate,
        "_calendar_sessions",
        lambda ticker, start, end: [
            d.date().isoformat()
            for d in dates
            if start <= d.date().isoformat() <= end
        ],
    )
    import historical_ohlcv

    monkeypatch.setattr(
        historical_ohlcv, "_calendar_name_for_ticker", lambda ticker: "XNYS"
    )
    monkeypatch.setattr(
        gate, "_last_completed_session_date",
        lambda ticker, as_of=None: dates[-1].date().isoformat(),
    )


def test_missing_quality_registry_fails_closed(monkeypatch, tmp_path):
    dates = pd.bdate_range("2026-10-01", periods=30)
    _install_calendar(monkeypatch, dates)
    gaps = tmp_path / "gaps.csv"
    gaps.write_text("ticker,missing_date,quality_class\n", encoding="utf-8")
    gate._load_registry.cache_clear()
    gate._load_gap_registry.cache_clear()

    expected_missing = dates[-7]
    checked, result = gate.apply_ohlcv_quality_gate(
        _frame(dates[-20:].delete(13)),
        "AAPL",
        registry_path=tmp_path / "missing-registry.csv",
        gap_registry_path=gaps,
        window_sessions=20,
    )

    assert result.quality_status == "INCOMPLETE"
    assert "QUALITY_REGISTRY_MISSING" in result.reason_codes
    assert expected_missing in checked.index
    assert checked["Close"].isna().all()


def test_missing_gap_registry_fails_closed(monkeypatch, tmp_path):
    dates = pd.bdate_range("2026-10-01", periods=30)
    _install_calendar(monkeypatch, dates)
    registry = tmp_path / "registry.csv"
    registry.write_text(
        "ticker,date,classification,usage_decision,approved_by,evidence_ref,rule_version,allowed_reason_codes\n",
        encoding="utf-8",
    )
    gate._load_registry.cache_clear()
    gate._load_gap_registry.cache_clear()

    checked, result = gate.apply_ohlcv_quality_gate(
        _frame(dates[-20:]),
        "AAPL",
        registry_path=registry,
        gap_registry_path=tmp_path / "missing-gaps.csv",
        window_sessions=20,
    )

    assert result.quality_status == "INCOMPLETE"
    assert "GAP_REGISTRY_MISSING" in result.reason_codes
    assert checked["Close"].isna().all()


def test_sqlite_history_uses_full_expected_session_axis():
    source = (ROOT / "historical_ohlcv.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "load_history")
    calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)]
    gate_call = next(
        call for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "apply_ohlcv_quality_gate"
    )
    kwargs = {kw.arg: kw.value for kw in gate_call.keywords}
    assert isinstance(kwargs.get("preserve_expected_sessions"), ast.Constant)
    assert kwargs["preserve_expected_sessions"].value is True
    assert isinstance(kwargs.get("full_history"), ast.Constant)
    assert kwargs["full_history"].value is True


def test_ad_hoc_alpaca_signal_fallback_is_quality_gated():
    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "analyze_a_setup")
    calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)]
    assert any(
        isinstance(call.func, ast.Name) and call.func.id == "quality_checked_frame"
        for call in calls
    )


def test_spy_benchmark_cache_key_is_quality_versioned():
    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    assert 'alpaca:SPY:close:ohlcv-quality-v1.4-split' in source


def test_exchange_cutoff_excludes_open_session_until_actual_close():
    before_close = gate._last_completed_session_date(
        "AAPL", as_of="2026-11-06T20:00:00Z"  # 15:00 New York
    )
    after_close = gate._last_completed_session_date(
        "AAPL", as_of="2026-11-06T22:00:00Z"  # 17:00 New York
    )
    assert before_close == "2026-11-05"
    assert after_close == "2026-11-06"


def test_negative_wti_price_requires_instrument_date_and_registry_approval(tmp_path):
    import csv

    dates = ["2020-04-17", "2020-04-20", "2020-04-21"]
    frame = pd.DataFrame(
        {
            "Open": [18.27, 17.73, -14.00],
            "High": [18.31, 17.85, 13.86],
            "Low": [18.00, -40.32, -16.74],
            "Close": [18.27, -37.63, 10.01],
            "Volume": [100000.0, 247947.0, 2288230.0],
        },
        index=pd.DatetimeIndex(dates),
    )
    registry = tmp_path / "registry.csv"
    gaps = tmp_path / "gaps.csv"
    gaps.write_text("ticker,missing_date,quality_class\n", encoding="utf-8")
    fields = [
        "ticker", "date", "open", "high", "low", "close", "classification",
        "usage_decision", "approved_by", "evidence_ref", "rule_version",
        "allowed_reason_codes",
    ]
    with registry.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for date, codes, open_, high, low, close in (
            ("2020-04-20", "LOW_NON_POSITIVE,CLOSE_NON_POSITIVE", 17.73, 17.85, -40.32, -37.63),
            ("2020-04-21", "OPEN_NON_POSITIVE,LOW_NON_POSITIVE", -14.00, 13.86, -16.74, 10.01),
        ):
            writer.writerow({
                "ticker": "CL=F", "date": date, "open": open_, "high": high,
                "low": low, "close": close, "classification": "SPECIAL_CASE",
                "usage_decision": "ALLOW_WITH_RULE", "approved_by": "Forensic review 2026-11-07",
                "evidence_ref": "https://www.cftc.gov/media/5296/InterimStaffReportNYMEX_WTICrudeOil/download",
                "rule_version": "WTI_NEGATIVE_SETTLEMENT_2020_V2",
                "allowed_reason_codes": codes,
            })
    gate._load_registry.cache_clear()
    gate._load_gap_registry.cache_clear()
    checked, result = gate.apply_ohlcv_quality_gate(
        frame, "CL=F", registry_path=registry, gap_registry_path=gaps,
        window_sessions=3, as_of="2020-04-21T22:00:00Z",
    )
    assert result.quality_status in {"PASS_WITH_EXCLUSIONS", "PASS"}
    assert pd.notna(checked.loc[pd.Timestamp("2020-04-20"), "Close"])
    assert pd.notna(checked.loc[pd.Timestamp("2020-04-21"), "Open"])
    assert pd.notna(checked.loc[pd.Timestamp("2020-04-21"), "Low"])
    assert pd.notna(checked.loc[pd.Timestamp("2020-04-21"), "Close"])

    # Null ist kein negativer Marktpreis und bleibt trotz gleichem Reason-Code gesperrt.
    zero_frame = frame.copy()
    zero_frame.loc[pd.Timestamp("2020-04-20"), "Low"] = 0.0
    zero_checked, zero_result = gate.apply_ohlcv_quality_gate(
        zero_frame, "CL=F", registry_path=registry, gap_registry_path=gaps,
        window_sessions=3, as_of="2020-04-21T22:00:00Z",
    )
    assert zero_result.quality_status == "INCOMPLETE"
    assert pd.isna(zero_checked.loc[pd.Timestamp("2020-04-20"), "Low"])

    # Ein anderer negativer Wert am freigegebenen Datum ist nicht automatisch legitim.
    changed_negative = frame.copy()
    changed_negative.loc[pd.Timestamp("2020-04-20"), "Low"] = -39.99
    changed_checked, changed_result = gate.apply_ohlcv_quality_gate(
        changed_negative, "CL=F", registry_path=registry, gap_registry_path=gaps,
        window_sessions=3, as_of="2020-04-21T22:00:00Z",
    )
    assert changed_result.quality_status == "INCOMPLETE"
    assert pd.isna(changed_checked.loc[pd.Timestamp("2020-04-20"), "Low"])

    # Die Sonderregel gilt nicht fuer andere Instrumente.
    other, other_result = gate.apply_ohlcv_quality_gate(
        frame, "GC=F", registry_path=registry, gap_registry_path=gaps,
        window_sessions=3, as_of="2020-04-21T22:00:00Z",
    )
    assert other_result.quality_status == "INCOMPLETE"
    assert pd.isna(other.loc[pd.Timestamp("2020-04-20"), "Close"])

    # Ein zusaetzlicher, nicht belegter Reason-Code darf nicht freigegeben werden.
    with registry.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writerow({
            "ticker": "CL=F", "date": "2020-04-20", "classification": "SPECIAL_CASE",
            "usage_decision": "ALLOW_WITH_RULE", "approved_by": "Forensic review 2026-11-07",
            "evidence_ref": "https://www.cftc.gov/media/5296/InterimStaffReportNYMEX_WTICrudeOil/download",
            "rule_version": "WTI_NEGATIVE_SETTLEMENT_2020_V2",
            "allowed_reason_codes": "LOW_NON_POSITIVE,CLOSE_NON_POSITIVE,HIGH_NON_POSITIVE",
        })
    gate._load_registry.cache_clear()
    tampered, tampered_result = gate.apply_ohlcv_quality_gate(
        frame, "CL=F", registry_path=registry, gap_registry_path=gaps,
        window_sessions=3, as_of="2020-04-21T22:00:00Z",
    )
    assert tampered_result.quality_status == "INCOMPLETE"
    assert pd.isna(tampered.loc[pd.Timestamp("2020-04-20"), "Close"])


def test_analyse_safe_rsi_does_not_turn_missing_close_into_neutral_rsi():
    import ast
    import numpy as np

    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_safe_rsi"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"pd": pd, "np": np}
    exec(compile(module, "analyse.py", "exec"), namespace)
    safe_rsi = namespace["get_safe_rsi"]

    close = pd.Series([100.0 + i for i in range(40)])
    close.iloc[25] = float("nan")
    frame = pd.DataFrame({"Close": close})
    result = safe_rsi(frame)
    assert pd.isna(result.iloc[25])
    assert pd.isna(result.iloc[26])
    assert pd.notna(result.iloc[27])

    short = safe_rsi(pd.DataFrame({"Close": [100.0, 101.0, 102.0]}))
    assert short.isna().all()


def test_partial_current_daily_bar_is_excluded_not_counted_as_missing():
    dates = pd.DatetimeIndex(["2026-11-05", "2026-11-06"])
    frame = _frame(dates)
    frame.loc[pd.Timestamp("2026-11-06"), ["Open", "High", "Low", "Close", "Volume"]] = float("nan")
    checked, result = gate.apply_ohlcv_quality_gate(
        frame, "AAPL", window_sessions=1,
        as_of="2026-11-06T20:00:00Z",  # before 16:00 New York close
    )
    assert result.quality_status == "PASS"
    assert pd.Timestamp("2026-11-06") not in checked.index
    assert checked.index[-1] == pd.Timestamp("2026-11-05")


def test_full_history_preserves_all_sessions_but_scores_only_recent_window(monkeypatch):
    dates = pd.bdate_range("2025-10-01", periods=260)
    monkeypatch.setattr(
        gate, "_calendar_sessions",
        lambda ticker, start, end: [
            d.date().isoformat() for d in dates
            if start <= d.date().isoformat() <= end
        ],
    )
    monkeypatch.setattr(
        gate, "_last_completed_session_date",
        lambda ticker, as_of=None: dates[-1].date().isoformat(),
    )
    frame = _frame(dates)
    frame.loc[dates[0], "High"] = frame.loc[dates[0], "Close"] - 1
    checked, result = gate.apply_ohlcv_quality_gate(
        frame, "AAPL", window_sessions=252, full_history=True,
    )
    assert len(checked) == 260
    assert pd.isna(checked.loc[dates[0], "Close"])
    assert result.expected_bars == 252
    assert result.excluded_bars == 0
    assert result.quality_status == "PASS"


def test_benchmark_price_lookup_does_not_skip_invalid_expected_session():
    import ast
    import numpy as np

    source = (ROOT / "performance_benchmark.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_price_on_or_after", "_price_on_or_before"}
    ]
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {"pd": pd, "np": np}
    exec(compile(module, "performance_benchmark.py", "exec"), namespace)
    series = pd.Series(
        [100.0, float("nan"), 102.0],
        index=pd.DatetimeIndex(["2026-11-05", "2026-11-06", "2026-11-09"]),
    )
    assert namespace["_price_on_or_after"](series, pd.Timestamp("2026-11-06").date()) == (None, None)
    assert namespace["_price_on_or_before"](series, pd.Timestamp("2026-11-06").date()) == (None, None)

    # A genuine non-session date may map to adjacent valid sessions.
    complete = pd.Series(
        [100.0, 101.0, 102.0],
        index=pd.DatetimeIndex(["2026-11-05", "2026-11-06", "2026-11-09"]),
    )
    assert namespace["_price_on_or_before"](complete, pd.Timestamp("2026-11-07").date())[0] == 101.0
    assert namespace["_price_on_or_after"](complete, pd.Timestamp("2026-11-07").date())[0] == 102.0


def test_analysis_performance_windows_keep_nan_sessions_and_eu_single_path_is_strict():
    import ast
    import numpy as np

    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_nur_abgeschlossene_tagesbalken",
        "get_rohstoff_performance_yf",
        "get_eu_benchmark_close",
        "get_perf_yf",
        "analyze_a_setup_eu",
    }
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    for name in names - {"analyze_a_setup_eu"}:
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dropna"
            for node in ast.walk(functions[name])
        )
    eu_calls = [
        node for node in ast.walk(functions["analyze_a_setup_eu"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "quality_checked_frame"
    ]
    assert any(
        any(kw.arg == "require_structure" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in call.keywords)
        for call in eu_calls
    )

    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_return_over_expected_sessions"
    )
    module = ast.Module(body=[helper], type_ignores=[])
    namespace = {"pd": pd, "np": np}
    exec(compile(module, "analyse.py", "exec"), namespace)
    valid = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    gap = valid.copy()
    gap.iloc[2] = float("nan")
    assert namespace["_return_over_expected_sessions"](valid, 5) is not None
    assert namespace["_return_over_expected_sessions"](gap, 5) is None


def test_ifeu_cutoff_uses_full_ice_trading_session_not_settlement_window():
    before_close = gate._last_completed_session_date(
        "BZ=F", as_of="2026-11-06T22:00:00Z"  # 22:00 London, before 23:00 close
    )
    after_close = gate._last_completed_session_date(
        "BZ=F", as_of="2026-11-06T23:30:00Z"
    )
    assert before_close == "2026-11-05"
    assert after_close == "2026-11-06"


def test_atr_accepts_negative_wti_only_with_approved_instrument_rule(tmp_path):
    import csv

    dates = pd.bdate_range("2020-03-20", periods=23)
    frame = pd.DataFrame(
        {
            "Open": [20.0] * 21 + [17.73, -14.00],
            "High": [22.0] * 21 + [17.85, 13.86],
            "Low": [18.0] * 21 + [-40.32, -16.74],
            "Close": [21.0] * 21 + [-37.63, 10.01],
        },
        index=dates,
    )
    registry = tmp_path / "registry.csv"
    fields = [
        "ticker", "date", "open", "high", "low", "close", "classification",
        "usage_decision", "approved_by", "evidence_ref", "rule_version",
        "allowed_reason_codes",
    ]
    with registry.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "ticker": "CL=F", "date": "2020-04-20", "open": 17.73,
            "high": 17.85, "low": -40.32, "close": -37.63,
            "classification": "SPECIAL_CASE", "usage_decision": "ALLOW_WITH_RULE",
            "approved_by": "Forensic review 2026-11-07",
            "evidence_ref": "https://www.eia.gov/todayinenergy/detail.php?id=43495",
            "rule_version": "WTI_NEGATIVE_SETTLEMENT_2020_V2",
            "allowed_reason_codes": "LOW_NON_POSITIVE,CLOSE_NON_POSITIVE",
        })
        writer.writerow({
            "ticker": "CL=F", "date": "2020-04-21", "open": -14.0,
            "high": 13.86, "low": -16.74, "close": 10.01,
            "classification": "SPECIAL_CASE", "usage_decision": "ALLOW_WITH_RULE",
            "approved_by": "Forensic review 2026-11-07",
            "evidence_ref": "https://www.cftc.gov/media/5296/InterimStaffReportNYMEX_WTICrudeOil/download",
            "rule_version": "WTI_NEGATIVE_SETTLEMENT_2020_V2",
            "allowed_reason_codes": "OPEN_NON_POSITIVE,LOW_NON_POSITIVE",
        })
    gate._load_registry.cache_clear()
    value, status = gate.calculate_valid_atr14(frame, ticker="CL=F", registry_path=registry)
    assert status == "PASS"
    assert value is not None and value > 0

    blocked_value, blocked_status = gate.calculate_valid_atr14(frame, ticker="GC=F", registry_path=registry)
    assert blocked_value is None
    assert blocked_status == "INCOMPLETE"


def test_closed_session_is_removed_from_denominator_and_unexpected_bar_is_masked(tmp_path):
    import csv

    gap_path = tmp_path / "gaps.csv"
    with gap_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "missing_date", "quality_class"])
        writer.writeheader()
        writer.writerow({"ticker": "AAPL", "missing_date": "2026-11-06", "quality_class": "CLOSED_SESSION"})
    frame = _frame(pd.DatetimeIndex(["2026-11-05", "2026-11-06"]))
    gate._load_gap_registry.cache_clear()
    checked, result = gate.apply_ohlcv_quality_gate(
        frame, "AAPL", gap_registry_path=gap_path, window_sessions=1,
        as_of="2026-11-06T22:00:00Z",
    )
    assert result.quality_status == "PASS"
    assert result.expected_bars == 1
    assert pd.isna(checked.loc[pd.Timestamp("2026-11-06"), "Close"])


def test_gap_unresolved_stays_in_expected_session_denominator(tmp_path):
    import csv

    gap_path = tmp_path / "gaps.csv"
    with gap_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "missing_date", "quality_class"])
        writer.writeheader()
        writer.writerow({"ticker": "AAPL", "missing_date": "2026-11-06", "quality_class": "GAP_UNRESOLVED"})
    frame = _frame(pd.DatetimeIndex(["2026-11-05"]))
    gate._load_gap_registry.cache_clear()
    checked, result = gate.apply_ohlcv_quality_gate(
        frame, "AAPL", gap_registry_path=gap_path, window_sessions=2,
        as_of="2026-11-06T22:00:00Z",
    )
    assert result.quality_status == "INCOMPLETE"
    assert result.expected_bars == 2
    assert result.excluded_bars == 1
    assert "MISSING_EXPECTED_SESSION" in result.reason_codes


def test_quality_gate_is_explicitly_daily_and_rejects_unsupported_timeframes():
    frame = _frame(pd.bdate_range("2026-10-01", periods=20))
    checked, result = gate.apply_ohlcv_quality_gate(
        frame, "AAPL", window_sessions=20, timeframe="1h",
    )
    assert checked.empty
    assert result.quality_status == "INCOMPLETE"
    assert result.timeframe == "1h"
    assert "UNSUPPORTED_TIMEFRAME" in result.reason_codes

    checked, result = gate.apply_ohlcv_quality_gate(
        frame, "AAPL", window_sessions=20, timeframe="1d",
        as_of="2026-10-29T23:59:00Z",
    )
    assert result.timeframe == "1d"


def test_setup_normalization_keeps_missing_rsi_missing_and_blocks_false_oversold():
    source = (ROOT / "analyse.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {"_normalisiere_setup_numerik", "update_status_logic"}
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in functions} == names
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {"pd": pd}
    exec(compile(module, "analyse.py", "exec"), namespace)

    frame = pd.DataFrame({
        "RSI": [float("nan"), "not-a-number"],
        "Vol_Ratio": [1.0, float("nan")],
        "Kurs": [100.0, 100.0],
        "TP1": [200.0, 200.0],
        "Pattern": ["Kein", "Kein"],
        "MACD_Trend": ["Neutral", "Neutral"],
        "Divergenz": ["Keine", "Keine"],
    }, index=["MISSING_RSI", "INVALID_RSI"])
    normalized = namespace["_normalisiere_setup_numerik"](frame)

    assert normalized["RSI"].isna().all()
    assert normalized["Vol_Ratio"].tolist() == [1.0, 0.0]
    for _, row in normalized.iterrows():
        status = namespace["update_status_logic"](row)
        assert status.iloc[0] == "ACHTUNG"
        assert "RSI nicht verfügbar" in status.iloc[1]

    without_rsi = namespace["_normalisiere_setup_numerik"](
        pd.DataFrame({"Kurs": [100.0], "TP1": [200.0]}, index=["NO_RSI_COLUMN"])
    )
    assert pd.isna(without_rsi["RSI"].iloc[0])
    status = namespace["update_status_logic"](without_rsi.iloc[0])
    assert status.iloc[0] == "ACHTUNG"
    assert "RSI nicht verfügbar" in status.iloc[1]

    # Kontrollfaelle: echte RSI-Werte und die bisherige TP1-Prioritaet bleiben erhalten.
    oversold = namespace["update_status_logic"](pd.Series({
        "RSI": 25.0, "Pattern": "Kein", "Vol_Ratio": 1.0,
        "MACD_Trend": "Neutral", "Kurs": 100.0, "TP1": 200.0,
        "Divergenz": "Keine",
    }))
    assert oversold.iloc[0] == "VALIDE"
    assert "überverkauft" in oversold.iloc[1]

    tp1_reached_without_rsi = namespace["update_status_logic"](pd.Series({
        "Pattern": "Kein", "Vol_Ratio": 1.0, "MACD_Trend": "Neutral",
        "Kurs": 210.0, "TP1": 200.0,
    }))
    assert tp1_reached_without_rsi.iloc[0] == "GELAUFEN"
