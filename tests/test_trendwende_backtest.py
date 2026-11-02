import sys
import sqlite3
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import trendwende_backtest as bt


def synthetic(n=520):
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    # Deterministic falling-then-reversing series with oscillation so pivots exist.
    t = np.arange(n)
    close = 130 - 0.12*t + 5*np.sin(t/7)
    close[260:] += 0.22*(t[260:] - 260)
    close[360:] += 0.35*(t[360:] - 360)
    high = close + 1.5 + 0.3*np.sin(t/3)
    low = close - 1.5 - 0.3*np.cos(t/4)
    open_ = close + 0.2*np.sin(t/5)
    volume = 1_000_000 + 100_000*(1 + np.sin(t/11))
    return pd.DataFrame({
        "Open": open_, "High": high, "Low": low, "Close": close,
        "Adj Close": close, "Volume": volume,
    }, index=idx)


def test_variant_matrix_is_parallel_and_complete():
    variants = bt.variant_matrix()
    names = [v.name for v in variants]
    assert len(variants) == len(set(names))
    assert names[0] == "CURRENT"
    # 16 combinations of HL/HH/BOS/RETEST + current + 4 trendline combinations.
    assert len(variants) == 20


def test_confirmed_pivots_do_not_use_unconfirmed_tail():
    d = synthetic()
    a = bt.structure_state(d.iloc[:450].copy(), order=5)
    d2 = d.copy()
    # Alter only future bars after the earlier decision point.
    d2.iloc[451:, d2.columns.get_loc("High")] *= 1.50
    d2.iloc[451:, d2.columns.get_loc("Low")] *= 0.50
    b = bt.structure_state(d2.iloc[:450].copy(), order=5)
    assert a == b


def test_forward_evaluation_is_causal_and_bounded():
    d = synthetic()
    r = bt.evaluate_forward(d, 400, (1, 3, 5, 20))
    assert r["entry"] == float(d["Close"].iloc[400])
    assert r["ret_1d"] == float(d["Close"].iloc[401] / d["Close"].iloc[400] - 1) * 100
    assert r["mfe_20d"] >= r["ret_20d"]
    assert r["mae_20d"] <= r["ret_20d"] + 1e-12


def test_variant_filter_only_removes_current_signals():
    d = bt.indicators(synthetic())
    current_ok, _ = bt.current_trigger(d.iloc[:520])
    state = bt.structure_state(d.iloc[:520])
    for v in bt.variant_matrix():
        assert bt.variant_accepts(v, False, state) is False
    if current_ok:
        assert bt.variant_accepts(bt.Variant("CURRENT"), True, state) is True



def test_read_connections_are_closed_after_history_and_ticker_queries(tmp_path):
    db = tmp_path / "trendwende-read.sqlite"
    with closing(sqlite3.connect(db)) as conn:
        with conn:
            conn.execute(
                "CREATE TABLE bars (ticker TEXT, date TEXT, open REAL, high REAL, "
                "low REAL, close REAL, adj_close REAL, volume REAL)"
            )
            conn.execute(
                "INSERT INTO bars VALUES ('TEST','2026-10-30',10,12,9,11,11,100)"
            )
            conn.execute("CREATE TABLE instruments (ticker TEXT, row_count INTEGER)")
            conn.execute("INSERT INTO instruments VALUES ('TEST', 300)")

    original_connect = sqlite3.connect
    opened = []

    def capture_connection(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    with patch("trendwende_backtest.sqlite3.connect", side_effect=capture_connection):
        history = bt.load_history(db, "TEST")
        tickers = bt.list_tickers(db)

    assert len(history) == 1
    assert tickers == ["TEST"]
    assert len(opened) == 2
    for connection in opened:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            assert "closed" in str(exc).lower()
        else:
            raise AssertionError("Trendwende-Backtest ließ eine SQLite-Verbindung offen")
