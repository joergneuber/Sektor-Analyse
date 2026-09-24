"""
Trendwende-Research-Backtester
==============================

Isolierter Forschungs-/Backtest-Layer. Aendert KEINE produktive Trendwende-Logik.

Ziel:
- historische OHLCV-Daten aus historical_ohlcv.sqlite verwenden
- den aktuellen Trendwende-Signalstand ("CURRENT") kausal reproduzieren
- alternative Strukturvarianten parallel auf denselben Stichtagen pruefen
- Forward-Returns/MFE/MAE messen
- keine Zukunftsdaten fuer die Signalentscheidung verwenden

Bewusst nicht Teil des produktiven Scanners:
- Fundamental-Ampel wird nicht simuliert (historisch nicht kausal aus dem
  aktuellen externen Datenstand rekonstruierbar).
- CRV/TP-Filter sind optional nicht Teil des primären Signaltests; der Zweck
  dieses Research-Layers ist die QUALITAET der Trendwendeerkennung.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

# Die produktiven Konstanten werden per AST aus trendwende_scanner.py gelesen.
# Dadurch wird der Research-Layer nicht durch Seiteneffekte/Secrets des
# Produktionsscanners blockiert und bleibt trotzdem quellengebunden.
import ast

def _production_constants(source: Path = Path("trendwende_scanner.py")) -> dict:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    wanted = {
        "ABSTAND_52W_TIEF_MAX", "DIVERGENZ_FENSTER_TAGE", "FRISCHE_TAGE",
        "MULTIWOCHEN_LOOKBACK_TAGE", "MULTIWOCHEN_VOLUMEN_SCHWELLE",
        "WMA200_LOOKBACK_TAGE",
    }
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    values[target.id] = ast.literal_eval(node.value)
    missing = wanted - values.keys()
    if missing:
        raise RuntimeError(f"Produktive Trendwende-Konstanten fehlen: {sorted(missing)}")
    return values

# Initialisierung erfolgt nach CLI-Argumenten in main/run; die Defaults werden
# erst dann aus der echten Produktionsdatei geladen.


DEFAULT_HORIZONS = (1, 3, 5, 10, 20, 40)
DEFAULT_PIVOT_ORDER = 5
DEFAULT_RETEST_TOLERANCE = 0.01


@dataclass(frozen=True)
class Variant:
    name: str
    require_higher_low: bool = False
    require_higher_high: bool = False
    require_bos: bool = False
    require_retest: bool = False
    require_trendline: bool = False


_C = _production_constants()
ABSTAND_52W_TIEF_MAX = _C["ABSTAND_52W_TIEF_MAX"]
DIVERGENZ_FENSTER_TAGE = _C["DIVERGENZ_FENSTER_TAGE"]
FRISCHE_TAGE = _C["FRISCHE_TAGE"]
MULTIWOCHEN_LOOKBACK_TAGE = _C["MULTIWOCHEN_LOOKBACK_TAGE"]
MULTIWOCHEN_VOLUMEN_SCHWELLE = _C["MULTIWOCHEN_VOLUMEN_SCHWELLE"]
WMA200_LOOKBACK_TAGE = _C["WMA200_LOOKBACK_TAGE"]

def variant_matrix() -> list[Variant]:
    """
    Vollstaendige 2^4-Matrix der zusaetzlichen Strukturbedingungen.

    Trendlinie ist absichtlich ein eigener Faktor. Dadurch wird nicht
    vorausgesetzt, dass die Video-Idee automatisch besser ist.
    """
    out = [Variant("CURRENT")]
    for hl, hh, bos, retest in itertools.product([False, True], repeat=4):
        if not any((hl, hh, bos, retest)):
            continue
        flags = []
        if hl:
            flags.append("HL")
        if hh:
            flags.append("HH")
        if bos:
            flags.append("BOS")
        if retest:
            flags.append("RETEST")
        out.append(Variant("CURRENT+" + "+".join(flags), hl, hh, bos, retest, False))

    # Separater Trendlinien-Faktor und drei sinnvolle Kombinationen.
    out.extend([
        Variant("CURRENT+TL", require_trendline=True),
        Variant("CURRENT+TL+HL", require_higher_low=True, require_trendline=True),
        Variant("CURRENT+TL+HL+BOS", require_higher_low=True, require_bos=True, require_trendline=True),
        Variant("CURRENT+TL+HL+BOS+RETEST", require_higher_low=True, require_bos=True,
                require_retest=True, require_trendline=True),
    ])
    return out


def load_history(db_path: Path, ticker: str) -> pd.DataFrame:
    with sqlite3.connect(str(db_path)) as conn:
        df = pd.read_sql_query(
            """SELECT date, open AS Open, high AS High, low AS Low, close AS Close,
                      adj_close AS "Adj Close", volume AS Volume
               FROM bars WHERE ticker=? ORDER BY date""",
            conn, params=(ticker,), parse_dates=["date"],
        )
    if df.empty:
        return df
    return df.set_index("date").sort_index()


def list_tickers(db_path: Path) -> list[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT ticker FROM instruments WHERE row_count >= 300 ORDER BY ticker"
        )]


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["EMA20"] = d["Close"].ewm(span=20, adjust=False).mean()
    d["EMA50"] = d["Close"].ewm(span=50, adjust=False).mean()
    d["EMA100"] = d["Close"].ewm(span=100, adjust=False).mean()
    d["EMA200"] = d["Close"].ewm(span=200, adjust=False).mean()
    weights = np.arange(1, 201)
    d["WMA200"] = d["Close"].rolling(200).apply(
        lambda p: np.dot(p, weights) / weights.sum(), raw=True
    )
    d["Vol_SMA20"] = d["Volume"].rolling(20).mean()
    d["Vol_Ratio"] = (d["Volume"] / d["Vol_SMA20"]).replace([np.inf, -np.inf], np.nan)

    delta = d["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    d["RSI"] = 100 - (100 / (1 + rs))
    d.loc[(loss == 0) & (gain > 0), "RSI"] = 100
    d.loc[(gain == 0) & (loss > 0), "RSI"] = 0

    tenkan = (d["High"].rolling(9).max() + d["Low"].rolling(9).min()) / 2
    kijun = (d["High"].rolling(26).max() + d["Low"].rolling(26).min()) / 2
    d["SenkouA"] = ((tenkan + kijun) / 2).shift(26)
    d["SenkouB"] = ((d["High"].rolling(52).max() + d["Low"].rolling(52).min()) / 2).shift(26)
    return d


def current_trigger(d: pd.DataFrame) -> tuple[bool, dict]:
    """Signal-Level-Reproduktion des aktuellen Trendwende-Kerns."""
    if len(d) < 60 or pd.isna(d["WMA200"].iloc[-1]):
        return False, {"reason": "too_little_history"}

    was_below = any(
        pd.notna(d["WMA200"].iloc[-1 - i]) and
        d["Close"].iloc[-1 - i] < d["WMA200"].iloc[-1 - i]
        for i in range(WMA200_LOOKBACK_TAGE)
        if i + 1 <= len(d)
    )
    if not was_below:
        return False, {"reason": "not_below_wma200"}

    low52 = float(d["Low"].tail(252).min())
    if low52 <= 0:
        return False, {"reason": "bad_52w_low"}
    dist = (float(d["Close"].iloc[-1]) / low52 - 1) * 100
    if dist > ABSTAND_52W_TIEF_MAX:
        return False, {"reason": "too_far_from_52w_low"}

    div = bullish_rsi_divergence(d, DIVERGENZ_FENSTER_TAGE)
    kumo = kumo_breakout(d, FRISCHE_TAGE)
    p1 = div and kumo

    p2 = multiweek_breakout(d)
    ok = p1 or p2
    return ok, {
        "reason": "current_signal" if ok else "no_trigger",
        "divergence": div,
        "kumo": kumo,
        "path1": p1,
        "path2": p2,
        "dist_52w_low": dist,
    }


def bullish_rsi_divergence(d: pd.DataFrame, window: int) -> bool:
    x = d.tail(window + 15)
    piv = argrelextrema(x["Close"].values, np.less_equal, order=5)[0]
    if len(piv) < 2:
        return False
    last = piv[-1]
    if last < len(x) - 1 - window:
        return False
    return (
        x["Close"].iloc[last] < x["Close"].iloc[piv[-2]]
        and x["RSI"].iloc[last] > x["RSI"].iloc[piv[-2]]
        and not (
            pd.notna(x["Close"].iloc[last + 1:].min())
            and x["Close"].iloc[last + 1:].min() < x["Close"].iloc[last]
        )
    )


def kumo_breakout(d: pd.DataFrame, fresh: int) -> bool:
    if len(d) < 60:
        return False
    upper = pd.concat([d["SenkouA"], d["SenkouB"]], axis=1).max(axis=1)
    if pd.isna(upper.iloc[-1]) or d["Close"].iloc[-1] <= upper.iloc[-1]:
        return False
    for i in range(fresh):
        idx = -1 - i
        prev = idx - 1
        if abs(prev) > len(d):
            break
        if any(pd.isna(v) for v in (
            d["Close"].iloc[idx], upper.iloc[idx],
            d["Close"].iloc[prev], upper.iloc[prev],
        )):
            continue
        if d["Close"].iloc[prev] <= upper.iloc[prev] and d["Close"].iloc[idx] > upper.iloc[idx]:
            return True
    return False


def multiweek_breakout(d: pd.DataFrame) -> bool:
    n = MULTIWOCHEN_LOOKBACK_TAGE
    if len(d) < n + 20:
        return False
    old_high = float(d["High"].iloc[-(n + 1):-1].max())
    close = float(d["Close"].iloc[-1])
    vr = float(d["Vol_Ratio"].iloc[-1]) if pd.notna(d["Vol_Ratio"].iloc[-1]) else 0.0
    return close >= old_high * 0.999 and vr >= MULTIWOCHEN_VOLUMEN_SCHWELLE


def confirmed_pivots(d: pd.DataFrame, order: int = DEFAULT_PIVOT_ORDER) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """
    Nur Pivots, die bis zum aktuellen Stichtag bereits bestaetigt sind.

    Ein Pivot an Index i wird erst verwendet, wenn i+order <= letzter
    beobachteter Index. Dadurch entsteht kein Look-ahead durch argrelextrema.
    """
    n = len(d)
    max_idx = n - 1 - order
    if max_idx < order:
        return [], []
    highs = d["High"].to_numpy()
    lows = d["Low"].to_numpy()
    hi = argrelextrema(highs, np.greater_equal, order=order)[0]
    lo = argrelextrema(lows, np.less_equal, order=order)[0]
    hi = [(int(i), float(highs[i])) for i in hi if i <= max_idx]
    lo = [(int(i), float(lows[i])) for i in lo if i <= max_idx]
    return hi, lo


def structure_state(d: pd.DataFrame, order: int = DEFAULT_PIVOT_ORDER,
                    retest_tolerance: float = DEFAULT_RETEST_TOLERANCE) -> dict:
    highs, lows = confirmed_pivots(d, order)
    result = {
        "higher_low": False, "higher_high": False, "bullish_structure": False,
        "bos": False, "retest": False, "trendline": False,
        "last_swing_high": np.nan, "last_swing_low": np.nan,
    }
    if len(lows) >= 2:
        result["higher_low"] = lows[-1][1] > lows[-2][1]
        result["last_swing_low"] = lows[-1][1]
    if len(highs) >= 2:
        result["higher_high"] = highs[-1][1] > highs[-2][1]
        result["last_swing_high"] = highs[-1][1]

    result["bullish_structure"] = result["higher_low"] and result["higher_high"]

    # Bullish BOS: close breaks the last confirmed swing high, and a higher
    # low exists before that swing high. The swing high itself must be known
    # before today's decision.
    if highs and len(lows) >= 2:
        last_hi_i, last_hi = highs[-1]
        last_lo_i, last_lo = lows[-1]
        prev_lo = lows[-2][1]
        if last_lo > prev_lo and last_hi_i < len(d) - 1:
            result["bos"] = float(d["Close"].iloc[-1]) > last_hi

    # Retest wird nur ueber bereits abgeschlossene VORHERIGE Kerzen bewertet.
    # Der heutige Tag darf nicht gleichzeitig BOS und Retest sein, weil OHLC
    # keine Intraday-Reihenfolge liefert. Dadurch bleibt die Aussage kausal.
    if not np.isnan(result["last_swing_high"]) and highs:
        level = result["last_swing_high"]
        for i in range(max(0, len(d) - 4), len(d) - 1):
            low = float(d["Low"].iloc[i])
            close = float(d["Close"].iloc[i])
            if low <= level * (1 + retest_tolerance) and close >= level:
                result["retest"] = True
                break

    result["trendline"] = trendline_breakout(d)
    return result


def trendline_breakout(d: pd.DataFrame, lookback: int = 120, order: int = 5,
                       tolerance: float = 0.01) -> bool:
    """Long-Trendlinie analog zur produktiven Methodik, kausal fuer den Stichtag."""
    w = d.iloc[-lookback:] if len(d) > lookback else d.copy()
    if len(w) < 20:
        return False
    search = w.iloc[:-3]
    vals = search["High"].to_numpy()
    piv = argrelextrema(vals, np.greater_equal, order=order)[0]
    if len(piv) < 3:
        return False
    x, y = piv.astype(float), vals[piv]
    slope, intercept = np.polyfit(x, y, 1)
    if slope >= 0:
        return False
    line = slope * x + intercept
    touches = np.sum(np.abs(y - line) <= np.abs(line) * tolerance)
    if touches < 3:
        return False

    all_pos = np.arange(len(w))
    all_line = slope * all_pos + intercept
    close = w["Close"].to_numpy()

    # Current close must be above the line.
    if close[-1] <= all_line[-1]:
        return False

    # There must be a recent crossing and no return below it.
    for i in range(1, 4):
        pos = len(w) - 1 - i
        prev = pos - 1
        if prev < 0:
            continue
        if close[prev] <= all_line[prev] and close[pos] > all_line[pos]:
            if not np.all(close[pos:] > all_line[pos:]):
                return False
            if np.any(w["Volume"].to_numpy()[max(0, len(w)-3):] >
                      w["Volume"].rolling(20).mean().to_numpy()[max(0, len(w)-3):]):
                return True
    return False


def variant_accepts(v: Variant, current_ok: bool, state: dict) -> bool:
    if not current_ok:
        return False
    if v.require_higher_low and not state["higher_low"]:
        return False
    if v.require_higher_high and not state["higher_high"]:
        return False
    if v.require_bos and not state["bos"]:
        return False
    if v.require_retest and not state["retest"]:
        return False
    if v.require_trendline and not state["trendline"]:
        return False
    return True


def evaluate_forward(d: pd.DataFrame, signal_pos: int, horizons: tuple[int, ...]) -> dict:
    entry = float(d["Close"].iloc[signal_pos])
    out = {"entry": entry}
    for h in horizons:
        end = min(len(d) - 1, signal_pos + h)
        future = d.iloc[signal_pos + 1:end + 1]
        if future.empty:
            out[f"ret_{h}d"] = np.nan
            out[f"mfe_{h}d"] = np.nan
            out[f"mae_{h}d"] = np.nan
            continue
        out[f"ret_{h}d"] = float(d["Close"].iloc[end] / entry - 1) * 100
        out[f"mfe_{h}d"] = float(future["High"].max() / entry - 1) * 100
        out[f"mae_{h}d"] = float(future["Low"].min() / entry - 1) * 100
    return out


def backtest_ticker(ticker: str, df: pd.DataFrame, start: str | None, end: str | None,
                    variants: list[Variant], horizons: tuple[int, ...],
                    pivot_order: int, retest_tolerance: float) -> tuple[list[dict], dict]:
    d = df.copy()
    if start:
        d = d.loc[pd.Timestamp(start):]
    if end:
        d = d.loc[:pd.Timestamp(end)]
    if len(d) < 350:
        return [], {"ticker": ticker, "days": len(d), "signals": 0}

    # Precompute indicators once; each signal date receives only data up to t.
    d = indicators(d)
    rows = []

    # Minimum history = 252d 52W + WMA200 + divergence/kumo buffers.
    first = max(252, 220)
    for pos in range(first, len(d)):
        window = d.iloc[:pos + 1]
        current_ok, diag = current_trigger(window)
        if not current_ok:
            continue
        state = structure_state(window, pivot_order, retest_tolerance)
        for v in variants:
            if variant_accepts(v, current_ok, state):
                row = {
                    "ticker": ticker,
                    "date": d.index[pos].strftime("%Y-%m-%d"),
                    "variant": v.name,
                    "current_path1": bool(diag.get("path1", False)),
                    "current_path2": bool(diag.get("path2", False)),
                    "higher_low": bool(state["higher_low"]),
                    "higher_high": bool(state["higher_high"]),
                    "bos": bool(state["bos"]),
                    "retest": bool(state["retest"]),
                    "trendline": bool(state["trendline"]),
                    "dist_52w_low": float(diag.get("dist_52w_low", np.nan)),
                }
                row.update(evaluate_forward(d, pos, horizons))
                rows.append(row)
    return rows, {"ticker": ticker, "days": len(d), "signals": sum(r["variant"] == "CURRENT" for r in rows)}


def summarize(events: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=["variant", "signals"])
    records = []
    for variant, g in events.groupby("variant", sort=False):
        r = {"variant": variant, "signals": len(g), "tickers": g["ticker"].nunique()}
        for h in horizons:
            col = f"ret_{h}d"
            vals = pd.to_numeric(g[col], errors="coerce").dropna()
            r[f"mean_ret_{h}d"] = vals.mean() if len(vals) else np.nan
            r[f"median_ret_{h}d"] = vals.median() if len(vals) else np.nan
            r[f"positive_{h}d_pct"] = (vals > 0).mean() * 100 if len(vals) else np.nan
            r[f"mfe_mean_{h}d"] = pd.to_numeric(g[f"mfe_{h}d"], errors="coerce").mean()
            r[f"mae_mean_{h}d"] = pd.to_numeric(g[f"mae_{h}d"], errors="coerce").mean()
        records.append(r)
    return pd.DataFrame(records)


def run(db_path: Path, tickers: list[str], start: str | None, end: str | None,
        output_dir: Path, pivot_order: int, retest_tolerance: float,
        horizons: tuple[int, ...]) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    variants = variant_matrix()
    all_rows = []
    coverage = []

    for ticker in tickers:
        df = load_history(db_path, ticker)
        if df.empty:
            coverage.append({"ticker": ticker, "status": "NO_DATA", "rows": 0})
            continue
        rows, meta = backtest_ticker(
            ticker, df, start, end, variants, horizons, pivot_order, retest_tolerance
        )
        all_rows.extend(rows)
        coverage.append({**meta, "status": "OK"})

    events = pd.DataFrame(all_rows)
    summary = summarize(events, horizons)

    events_path = output_dir / "trendwende_backtest_events.csv"
    summary_path = output_dir / "trendwende_backtest_summary.csv"
    coverage_path = output_dir / "trendwende_backtest_coverage.csv"
    config_path = output_dir / "trendwende_backtest_config.json"

    events.to_csv(events_path, index=False)
    summary.to_csv(summary_path, index=False)
    pd.DataFrame(coverage).to_csv(coverage_path, index=False)
    config_path.write_text(json.dumps({
        "variants": [v.__dict__ for v in variants],
        "start": start, "end": end, "pivot_order": pivot_order,
        "retest_tolerance": retest_tolerance, "horizons": horizons,
        "lookahead_protection": True,
        "production_files_changed": False,
        "note": "Signaltest ohne historische Fundamental-Ampel/CRV; Fokus ist Trendwende-Erkennung.",
    }, indent=2), encoding="utf-8")

    return {
        "variants": len(variants),
        "tickers": len(tickers),
        "events": len(events),
        "summary": summary,
        "events_path": str(events_path),
        "summary_path": str(summary_path),
        "coverage_path": str(coverage_path),
        "config_path": str(config_path),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Trendwende Research Backtest")
    p.add_argument("--db", type=Path, default=Path("historical_ohlcv.sqlite"))
    p.add_argument("--ticker", nargs="*", help="optional explicit ticker list")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--out", type=Path, default=Path("trendwende_backtest"))
    p.add_argument("--pivot-order", type=int, default=DEFAULT_PIVOT_ORDER)
    p.add_argument("--retest-tolerance", type=float, default=DEFAULT_RETEST_TOLERANCE)
    p.add_argument("--horizons", nargs="*", type=int, default=list(DEFAULT_HORIZONS))
    args = p.parse_args()

    tickers = args.ticker or list_tickers(args.db)
    if not tickers:
        raise SystemExit("Keine historischen Ticker gefunden.")
    result = run(
        args.db, tickers, args.start, args.end, args.out,
        args.pivot_order, args.retest_tolerance, tuple(args.horizons),
    )
    print(f"TRENDWENDE-BACKTEST: variants={result['variants']} tickers={result['tickers']} events={result['events']}")
    print(result["summary"].to_string(index=False))
    print(f"EVENTS={result['events_path']}")
    print(f"SUMMARY={result['summary_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
