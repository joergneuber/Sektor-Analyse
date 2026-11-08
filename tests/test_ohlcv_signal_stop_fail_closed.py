from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "analyse.py"


def _extract_function(name: str):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    namespace = {"np": np}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


def test_missing_expected_session_in_ten_bar_stop_window_blocks_risk_and_crv():
    index = pd.bdate_range("2026-10-01", periods=20)
    lows = pd.Series(np.linspace(100.0, 119.0, len(index)), index=index)
    lows.iloc[-5] = np.nan  # an expected session remains present but invalid
    stop = lows.rolling(10).min().iloc[-1]

    assert pd.isna(stop)
    risiko, crv1 = _extract_function("_berechne_risiko_crv1_fail_closed")(
        entry=120.0, stop=stop, tp1=132.0
    )
    assert risiko is None
    assert crv1 is None


def test_finite_risk_and_crv1_keep_existing_calculation():
    calculate = _extract_function("_berechne_risiko_crv1_fail_closed")
    assert calculate(120.0, 110.0, 140.0) == (10.0, 2.0)
    assert calculate(120.0, 120.0, 140.0) == (None, None)
    assert calculate(120.0, 125.0, 140.0) == (None, None)


def test_nonfinite_entry_stop_or_tp1_is_rejected():
    calculate = _extract_function("_berechne_risiko_crv1_fail_closed")
    assert calculate(np.nan, 110.0, 140.0) == (None, None)
    assert calculate(120.0, np.nan, 140.0) == (None, None)
    assert calculate(120.0, 110.0, np.inf) == (None, None)


def test_missing_200_day_average_is_not_reported_as_trend_ok():
    status = _extract_function("_trend_status_200d")
    assert status(120.0, np.nan, 115.0) == "Daten unvollständig (WMA200/EMA200)"
    assert status(120.0, 125.0, 115.0) == "Unter WMA200/EMA200"
    assert status(120.0, 115.0, 110.0) == "OK"


def test_us_and_eu_entry_paths_both_use_finite_risk_and_explicit_trend_status():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"analyze_a_setup", "analyze_a_setup_eu"}
    }
    assert set(functions) == {"analyze_a_setup", "analyze_a_setup_eu"}

    for name, node in functions.items():
        calls = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_berechne_risiko_crv1_fail_closed"
        ]
        assert len(calls) == 1, f"{name} must use the finite risk/CRV guard exactly once"

        trend_calls = [
            call for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_trend_status_200d"
        ]
        assert len(trend_calls) == 1, f"{name} must report missing 200-day averages explicitly"

        pullback_stop_lines = [
            item for item in ast.walk(node)
            if isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "swing_low_stop" for target in item.targets)
        ]
        assert len(pullback_stop_lines) == 1, f"{name} must have one pullback stop calculation"
        assert ast.unparse(pullback_stop_lines[0].value) == "data['Low'].rolling(5).min().iloc[-1]"


def test_quality_gate_can_pass_isolated_recent_gap_but_stop_window_still_blocks(monkeypatch):
    import ohlcv_quality_gate as gate

    index = pd.bdate_range(end="2026-11-06", periods=252)
    session_dates = [stamp.date().isoformat() for stamp in index]
    monkeypatch.setattr(
        gate, "_calendar_sessions",
        lambda ticker, start, end: [d for d in session_dates if start <= d <= end],
    )
    monkeypatch.setattr(
        gate, "_last_completed_session_date",
        lambda ticker, as_of=None: session_dates[-1],
    )

    close = np.linspace(100.0, 200.0, len(index))
    frame = pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(len(index), 1000.0),
        },
        index=index,
    )
    gap_date = index[-5]
    frame.loc[gap_date, ["Open", "High", "Low", "Close", "Volume"]] = np.nan

    gated, quality = gate.apply_ohlcv_quality_gate(
        frame, "AAPL", window_sessions=252, require_structure=True,
        as_of="2026-11-07T12:00:00Z",
    )
    assert quality.quality_status == "PASS_WITH_EXCLUSIONS"
    assert quality.last_3_bars_valid
    assert pd.isna(gated.loc[gap_date, "Close"])

    stop = gated["Low"].rolling(10).min().iloc[-1]
    assert pd.isna(stop)
    # The former slice-min would silently ignore the missing session.
    assert np.isfinite(gated["Low"].iloc[-5:].min())
    pullback_stop = gated["Low"].rolling(5).min().iloc[-1]
    assert pd.isna(pullback_stop)
    assert _extract_function("_berechne_risiko_crv1_fail_closed")(
        entry=gated["Close"].iloc[-1], stop=stop, tp1=gated["Close"].iloc[-1] * 1.08
    ) == (None, None)


def test_gap_inside_200_session_window_marks_wma200_unavailable():
    close = pd.Series(np.linspace(100.0, 220.0, 220))
    close.iloc[-150] = np.nan
    wma200 = close.rolling(200).apply(
        lambda values: np.dot(values, np.arange(1, 201)) / np.sum(np.arange(1, 201)),
        raw=True,
    ).iloc[-1]
    assert pd.isna(wma200)
    assert _extract_function("_trend_status_200d")(220.0, wma200, 210.0) == (
        "Daten unvollständig (WMA200/EMA200)"
    )
