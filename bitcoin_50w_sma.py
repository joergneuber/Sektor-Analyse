"""Bitcoin 50-week SMA trend-change alert.

Definition:
- Basis is the BTC/USD WEEKLY chart.
- The indicator is the 50-week simple moving average (50W SMA).
- The official cross is confirmed only by a COMPLETED weekly close.
- A separate pre-alert is emitted when the latest completed daily BTC close
  comes within PREALERT_DISTANCE_PCT of the latest completed 50W SMA.

The feature is informational only. It does not change CRV, setup scores,
filters, trade decisions or any other analysis logic.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd


BTC_50W_SMA = 50
PREALERT_DISTANCE_PCT = 3.0
STATE_FILE = Path(__file__).resolve().parent / ".btc_50w_sma_state" / "state.json"


def _normalise_index(data: pd.DataFrame) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    data = data.copy()
    idx = pd.to_datetime(data.index, errors="coerce")
    valid = ~idx.isna()
    data = data.loc[valid].copy()
    idx = idx[valid]
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC")
    else:
        idx = idx.tz_localize("UTC")
    data.index = idx
    return data.sort_index()


def _weekly_completed_closes(hist: pd.DataFrame) -> pd.Series:
    """Build completed BTC weekly closes; never use the currently forming week."""
    data = _normalise_index(hist)
    if data.empty or "Close" not in data.columns:
        return pd.Series(dtype=float)

    close = pd.to_numeric(data["Close"], errors="coerce").dropna()
    if close.empty:
        return close

    # W-SUN means a Sunday-ending BTC week. The current bucket is incomplete
    # until the following Monday, so remove it unconditionally.
    weekly = close.resample("W-SUN").last().dropna()
    # The current Sunday-ending bucket is still forming until the week is
    # complete. Main workflows run on weekdays, so the clean rule is to use
    # only week-ending dates strictly before today.
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    weekly = weekly[[d < today_utc for d in weekly.index.date]]

    return weekly


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
        # Alert persistence must never stop the main analysis.
        pass


def _result(message: str, signal: bool = False, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "signal": signal, "message": message, **extra}


def calculate_bitcoin_50w_sma(hist: pd.DataFrame, *, consume_cross: bool = True) -> dict[str, Any]:
    """Return current 50W-SMA status, pre-alert and newly confirmed cross.

    consume_cross=True is used by the regular analysis. Benchmark snapshots
    may inspect the signal but must not consume/report it as delivered.
    """
    weekly = _weekly_completed_closes(hist)
    minimum = BTC_50W_SMA + 1
    if len(weekly) < minimum:
        return {
            "ok": False,
            "signal": False,
            "message": "Bitcoin 50W-SMA: Daten unvollständig",
        }

    sma = weekly.rolling(BTC_50W_SMA, min_periods=BTC_50W_SMA).mean()
    values = pd.concat([weekly.rename("Close"), sma.rename("SMA50W")], axis=1).dropna()
    if len(values) < 2:
        return {
            "ok": False,
            "signal": False,
            "message": "Bitcoin 50W-SMA: Daten unvollständig",
        }

    prev_diff = values["Close"].shift(1) - values["SMA50W"].shift(1)
    curr_diff = values["Close"] - values["SMA50W"]
    values["cross_up"] = (prev_diff <= 0) & (curr_diff > 0)
    values["cross_down"] = (prev_diff >= 0) & (curr_diff < 0)

    state = _load_state()
    events = values[values["cross_up"] | values["cross_down"]]
    last_event = events.iloc[-1] if not events.empty else None
    last_event_idx = events.index[-1] if not events.empty else None

    # The official weekly cross is reported once. On a fresh deployment we only
    # consider the latest completed week, so no ancient historical cross fires.
    last_reported_cross = state.get("last_reported_cross_date")
    new_event = None
    if last_event is not None:
        event_date = last_event_idx.date()
        if last_reported_cross:
            try:
                new_event = last_event if event_date > pd.Timestamp(last_reported_cross).date() else None
            except Exception:
                new_event = last_event
        else:
            new_event = last_event if event_date == values.index[-1].date() else None

    if new_event is not None:
        event_date = last_event_idx.date()
        direction = "UP" if bool(new_event["cross_up"]) else "DOWN"
        # A cross is only consumed by the regular Monday-Friday analysis.
        # Benchmark snapshots may observe the signal but must never mark it
        # as reported; this guarantees the next regular analysis can still
        # deliver it.
        if consume_cross:
            state["last_reported_cross_date"] = str(event_date)
            state["last_cross_direction"] = direction
            _save_state(state)

        weekend_cross = (
            dt.datetime.now(dt.timezone.utc).date() == event_date + dt.timedelta(days=1)
            and event_date.weekday() == 6
        )
        weekend_label = " Cross am Wochenende (Sonntag) – jetzt in der regulären Montagsauswertung." if weekend_cross else ""
        if direction == "UP":
            message = (
                "🟢 Achtung: BTC/USD kreuzt die 50-Wochen-Durchschnittslinie "
                "auf Wochenbasis nach oben – möglicher starker Trendwechsel! "
                f"Wochen-Close {float(new_event['Close']):,.0f} USD, "
                f"50W-SMA {float(new_event['SMA50W']):,.0f} USD." + weekend_label
            )
        else:
            message = (
                "🔴 Achtung: BTC/USD kreuzt die 50-Wochen-Durchschnittslinie "
                "auf Wochenbasis nach unten – möglicher starker Trendwechsel! "
                f"Wochen-Close {float(new_event['Close']):,.0f} USD, "
                f"50W-SMA {float(new_event['SMA50W']):,.0f} USD." + weekend_label
            )
        return _result(
            message,
            signal=True,
            signal_type=f"CROSS_{direction}",
            cross_date=event_date,
            weekly_close=float(new_event["Close"]),
            sma50w=float(new_event["SMA50W"]),
        )

    # Pre-alert uses the latest available completed daily BTC close against the
    # latest completed weekly SMA. This is intentionally separate from the
    # official weekly-close cross.
    daily = _normalise_index(hist)
    prealert_close = None
    if not daily.empty and "Close" in daily.columns:
        daily_close = pd.to_numeric(daily["Close"], errors="coerce").dropna()
        if not daily_close.empty:
            today_utc = dt.datetime.now(dt.timezone.utc).date()
            daily_close = daily_close[daily_close.index.date < today_utc]
            if not daily_close.empty:
                prealert_close = float(daily_close.iloc[-1])

    latest_sma = float(values.iloc[-1]["SMA50W"])
    latest_weekly_close = float(values.iloc[-1]["Close"])
    weekly_side = "über" if latest_weekly_close > latest_sma else "unter"

    if prealert_close is not None and latest_sma > 0:
        distance_pct = (prealert_close - latest_sma) / latest_sma * 100.0
    else:
        distance_pct = None

    prealert = False
    if distance_pct is not None and abs(distance_pct) <= PREALERT_DISTANCE_PCT:
        current_week_key = str(values.index[-1].date())
        side = "UP" if prealert_close >= latest_sma else "DOWN"
        prealert_key = f"{current_week_key}:{side}"
        if state.get("last_prealert_key") != prealert_key:
            prealert = True
            # Only the regular analysis is allowed to mutate persistent state.
            # Benchmark snapshots are strictly read-only.
            if consume_cross:
                state["last_prealert_key"] = prealert_key
                _save_state(state)

    if prealert:
        direction_text = "von unten" if prealert_close < latest_sma else "von oben"
        return _result(
            "🟡 Achtung: BTC/USD nähert sich der 50-Wochen-Durchschnittslinie "
            "auf Wochenbasis – mögliche Trade-Möglichkeit! "
            f"BTC {prealert_close:,.0f} USD, 50W-SMA {latest_sma:,.0f} USD, "
            f"Abstand {abs(distance_pct):.1f}% ({direction_text}).",
            signal=True,
            signal_type="PREALERT",
            distance_pct=distance_pct,
            daily_close=prealert_close,
            sma50w=latest_sma,
        )

    distance_text = "n/v" if distance_pct is None else f"{distance_pct:+.1f}%"
    return _result(
        "Bitcoin 50W-SMA: kein neuer Cross. "
        f"Letzter abgeschlossener Wochen-Close {latest_weekly_close:,.0f} USD "
        f"{weekly_side} der 50W-SMA {latest_sma:,.0f} USD; "
        f"aktueller Abstand {distance_text}.",
        signal=False,
        signal_type="STATUS",
        weekly_close=latest_weekly_close,
        sma50w=latest_sma,
        distance_pct=distance_pct,
    )


def get_bitcoin_50w_sma_text(hist: pd.DataFrame) -> str:
    try:
        return calculate_bitcoin_50w_sma(hist)["message"]
    except Exception as exc:
        return f"Bitcoin 50W-SMA: Fehler bei der Berechnung ({type(exc).__name__}: {exc})"
