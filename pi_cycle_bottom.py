"""Bitcoin Pi-Cycle-Bottom information module.

TradingDigits variant used here:
- 150-day EMA (blue)
- 471-day SMA * 0.745 (reference)

Both confirmed cross directions are detected:
- 150 EMA crosses UP through the reference -> BUY information
- 150 EMA crosses DOWN through the reference -> SELL information

Only completed BTC daily candles are used.
The feature is informational only and never changes CRV, setup scores,
filters, trade decisions, or intraday logic.

Weekend handling:
BTC trades 24/7 while the main analysis runs Monday-Friday. The module
therefore searches the completed daily history for a newly occurring cross
rather than only comparing "today" to "yesterday". A small persistent state
file is used when the workspace survives between runs. If it does not
survive (e.g. a fresh GitHub runner), the fallback uses the previous
analysis/business-day window so a Friday/Saturday/Sunday cross can still be
reported on Monday without repeating it on Tuesday.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd


PI_FAST_EMA = 150
PI_SLOW_SMA = 471
PI_SLOW_MULTIPLIER = 0.745

STATE_FILE = Path(__file__).resolve().parent / "pi_cycle_bottom_state.json"


def _completed_btc_daily_bars(hist: pd.DataFrame) -> pd.DataFrame:
    """Return completed BTC daily candles only."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return pd.DataFrame()

    data = hist.copy()
    data = data.dropna(subset=["Close"])
    if data.empty:
        return data

    idx = pd.to_datetime(data.index, errors="coerce")
    valid = ~idx.isna()
    data = data.loc[valid].copy()
    idx = idx[valid]

    if getattr(idx, "tz", None) is not None:
        idx_utc = idx.tz_convert("UTC")
    else:
        idx_utc = idx.tz_localize("UTC")

    data.index = idx_utc

    # The current UTC day is still forming and must not be used.
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    data = data[data.index.date < today_utc]

    return data


def _load_state() -> dict[str, Any]:
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as f:
                value = json.load(f)
            return value if isinstance(value, dict) else {}
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(STATE_FILE)
    except Exception:
        # State persistence must never stop the main analysis.
        pass


def _business_days_back(days: int = 1) -> dt.date:
    """Previous Mon-Fri business day relative to today."""
    d = dt.datetime.now(dt.timezone.utc).date()
    count = 0
    while count < days:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d


def calculate_pi_cycle_bottom(hist: pd.DataFrame) -> dict[str, Any]:
    """Calculate the indicator and emit at most one new confirmed event."""
    data = _completed_btc_daily_bars(hist)

    minimum = PI_SLOW_SMA + 5
    if len(data) < minimum:
        return {
            "ok": False,
            "message": "Bitcoin Pi-Cycle Bottom: Daten unvollständig",
            "signal": False,
        }

    close = pd.to_numeric(data["Close"], errors="coerce").dropna()
    if len(close) < minimum:
        return {
            "ok": False,
            "message": "Bitcoin Pi-Cycle Bottom: Daten unvollständig",
            "signal": False,
        }

    ema150 = close.ewm(
        span=PI_FAST_EMA,
        adjust=False,
        min_periods=PI_FAST_EMA,
    ).mean()

    sma471 = close.rolling(
        PI_SLOW_SMA,
        min_periods=PI_SLOW_SMA,
    ).mean()

    reference = sma471 * PI_SLOW_MULTIPLIER

    values = pd.concat(
        [
            close.rename("Close"),
            ema150.rename("EMA150"),
            reference.rename("Reference"),
        ],
        axis=1,
    ).dropna()

    if len(values) < 2:
        return {
            "ok": False,
            "message": "Bitcoin Pi-Cycle Bottom: Daten unvollständig",
            "signal": False,
        }

    prev_diff = values["EMA150"].shift(1) - values["Reference"].shift(1)
    curr_diff = values["EMA150"] - values["Reference"]

    values["cross_up"] = (prev_diff <= 0) & (curr_diff > 0)
    values["cross_down"] = (prev_diff >= 0) & (curr_diff < 0)

    events = values[values["cross_up"] | values["cross_down"]].copy()

    if events.empty:
        return {
            "ok": True,
            "signal": False,
            "message": "Bitcoin Pi-Cycle Bottom: kein neuer Cross.",
            "date": values.index[-1].date(),
            "close": float(values.iloc[-1]["Close"]),
            "ema150": float(values.iloc[-1]["EMA150"]),
            "sma471_x0745": float(values.iloc[-1]["Reference"]),
        }

    state = _load_state()
    last_reported = state.get("last_reported_cross_date")

    # Preferred path: persistent state. This catches weekend events on Monday
    # and prevents the same event from being repeated on later weekdays.
    if last_reported:
        try:
            last_date = pd.Timestamp(last_reported, tz="UTC")
            events_new = events[events.index > last_date]
        except Exception:
            events_new = events
    else:
        # Fresh runner fallback: only consider the previous business-day
        # window. On Monday this covers Friday + Saturday + Sunday; on Tue-Fri
        # it covers the immediately preceding business day. This avoids
        # generating an ancient historical signal on first deployment.
        cutoff = pd.Timestamp(_business_days_back(1), tz="UTC")
        events_new = events[events.index.normalize() >= cutoff.normalize()]

    if events_new.empty:
        return {
            "ok": True,
            "signal": False,
            "message": "Bitcoin Pi-Cycle Bottom: kein neuer Cross.",
            "date": values.index[-1].date(),
            "close": float(values.iloc[-1]["Close"]),
            "ema150": float(values.iloc[-1]["EMA150"]),
            "sma471_x0745": float(values.iloc[-1]["Reference"]),
        }

    # If several crosses happened between runs, report the newest one.
    event_idx = events_new.index[-1]
    event = events_new.iloc[-1]

    signal = "BUY" if bool(event["cross_up"]) else "SELL"
    event_date = event_idx.date()

    state["last_reported_cross_date"] = str(event_date)
    state["last_signal"] = signal
    _save_state(state)

    if signal == "BUY":
        message = (
            "🟦 BITCOIN PI-CYCLE BOTTOM → KAUFSIGNAL: "
            f"150-EMA kreuzt 471-SMA × {PI_SLOW_MULTIPLIER:.3f} "
            f"von unten nach oben. Cross-Datum: "
            f"{event_date.strftime('%d.%m.%Y')} "
            f"(BTC {float(event['Close']):,.0f} USD)."
        )
    else:
        message = (
            "🔻 BITCOIN PI-CYCLE BOTTOM → VERKAUFSSIGNAL: "
            f"150-EMA kreuzt 471-SMA × {PI_SLOW_MULTIPLIER:.3f} "
            f"von oben nach unten. Cross-Datum: "
            f"{event_date.strftime('%d.%m.%Y')} "
            f"(BTC {float(event['Close']):,.0f} USD)."
        )

    return {
        "ok": True,
        "signal": True,
        "signal_type": signal,
        "date": event_date,
        "close": float(event["Close"]),
        "ema150": float(event["EMA150"]),
        "sma471_x0745": float(event["Reference"]),
        "message": message,
    }


def get_pi_cycle_bottom_text(hist: pd.DataFrame) -> str:
    """Compatibility wrapper for existing callers."""
    try:
        return calculate_pi_cycle_bottom(hist)["message"]
    except Exception as exc:
        return (
            "Bitcoin Pi-Cycle Bottom: Fehler bei der Berechnung "
            f"({type(exc).__name__}: {exc})"
        )
