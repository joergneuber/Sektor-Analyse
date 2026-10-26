"""Fail-closed audit of the persistent historical OHLCV SQLite database.

The module intentionally reports ``unavailable`` unless a database path and a
recognised schema can be verified. It never infers full coverage from prose.
Configure the database path with TRADING_RADAR_OHLCV_DB.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from pathlib import Path
from typing import Iterable


_REQUIRED_BAR_FIELDS = ("open", "high", "low", "close", "volume")
_STALE_AFTER_DAYS = 7


def _unavailable(reason: str, candidates: list[str]) -> dict:
    return {
        "status": "unavailable",
        "checked_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "universe_count": None,
        "covered_count": None,
        "missing_tickers": [],
        "stale_tickers": [],
        "invalid_ohlcv_tickers": [],
        "affected_candidates": [],
        "unmapped_candidate_tickers": list(candidates),
        "candidate_impact": "candidate_mapping_incomplete",
        "candidate_mapping_complete": False,
        "error": reason,
    }


def audit_snapshot(candidate_tickers: Iterable[str] | None = None) -> dict:
    """Audit symbol coverage, OHLCV field validity, freshness and candidate impact.

    Recognised schema: ``instruments`` with a ``ticker`` or ``symbol`` column,
    and ``bars`` with a matching ticker/symbol column, a date/timestamp column,
    and open/high/low/close/volume columns. Unknown schemas fail closed.
    """
    candidates = sorted({
        str(item).strip().upper()
        for item in (candidate_tickers or [])
        if str(item).strip()
    })
    configured = os.environ.get("TRADING_RADAR_OHLCV_DB", "").strip()
    if not configured:
        return _unavailable("TRADING_RADAR_OHLCV_DB ist nicht konfiguriert", candidates)
    db_path = Path(configured).expanduser()
    if not db_path.is_file():
        return _unavailable(f"OHLCV-Datenbank nicht gefunden: {db_path}", candidates)

    try:
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"instruments", "bars"}.issubset(tables):
                return _unavailable("Erwartete Tabellen instruments und bars fehlen", candidates)

            instrument_cols = {r["name"].casefold(): r["name"] for r in conn.execute("PRAGMA table_info(instruments)")}
            bar_cols = {r["name"].casefold(): r["name"] for r in conn.execute("PRAGMA table_info(bars)")}
            inst_symbol = instrument_cols.get("ticker") or instrument_cols.get("symbol")
            bar_symbol = bar_cols.get("ticker") or bar_cols.get("symbol")
            date_col = next((bar_cols[x] for x in ("date", "datetime", "timestamp", "bar_date") if x in bar_cols), None)
            required = {x: bar_cols.get(x) for x in _REQUIRED_BAR_FIELDS}
            if not inst_symbol or not bar_symbol or not date_col or any(v is None for v in required.values()):
                return _unavailable("OHLCV-Schema nicht eindeutig erkannt; Audit verweigert Entwarnung", candidates)

            universe = sorted({
                str(r[0]).strip().upper()
                for r in conn.execute(f'SELECT "{inst_symbol}" FROM instruments WHERE "{inst_symbol}" IS NOT NULL')
                if str(r[0]).strip()
            })
            if not universe:
                return _unavailable("Instrumentenuniversum ist leer", candidates)

            latest_cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=_STALE_AFTER_DAYS)).date().isoformat()
            covered, missing, stale, invalid = [], [], [], []
            latest_by_symbol = {}
            for ticker in universe:
                rows = conn.execute(
                    f'SELECT "{date_col}", "{required["open"]}", "{required["high"]}", '
                    f'"{required["low"]}", "{required["close"]}", "{required["volume"]}" '
                    f'FROM bars WHERE UPPER("{bar_symbol}") = ? ORDER BY "{date_col}" DESC',
                    (ticker,),
                ).fetchall()
                if not rows:
                    missing.append(ticker)
                    continue
                latest = rows[0]
                latest_by_symbol[ticker] = str(latest[0])
                valid_rows = []
                for row in rows:
                    try:
                        o, h, l, c, v = [float(row[i]) for i in range(1, 6)]
                        valid = all(map(lambda x: x == x and abs(x) != float("inf"), (o, h, l, c, v)))
                        valid = valid and min(o, h, l, c) > 0 and v >= 0 and h >= max(o, c, l) and l <= min(o, c, h)
                    except (TypeError, ValueError):
                        valid = False
                    if valid:
                        valid_rows.append(row)
                if not valid_rows:
                    invalid.append(ticker)
                    continue
                latest_valid_date = str(valid_rows[0][0])[:10]
                if latest_valid_date < latest_cutoff:
                    stale.append(ticker)
                    continue
                covered.append(ticker)

            universe_set = set(universe)
            unmapped = sorted(set(candidates) - universe_set)
            bad = set(missing) | set(stale) | set(invalid)
            affected = sorted(set(candidates) & bad)
            mapping_complete = bool(candidates) and not unmapped
            no_gaps = not missing and not stale and not invalid and len(covered) == len(universe)
            status = "complete" if no_gaps else "gaps"
            candidate_impact = (
                "none_detected_in_checked_candidates"
                if mapping_complete and not affected and no_gaps
                else "not_individually_mapped" if mapping_complete else "candidate_mapping_incomplete"
            )
            return {
                "status": status,
                "checked_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
                "universe_count": len(universe),
                "covered_count": len(covered),
                "missing_tickers": sorted(missing),
                "stale_tickers": sorted(stale),
                "invalid_ohlcv_tickers": sorted(invalid),
                "affected_candidates": affected,
                "unmapped_candidate_tickers": unmapped,
                "candidate_impact": candidate_impact,
                "candidate_mapping_complete": mapping_complete,
                "error": None,
            }
    except (OSError, sqlite3.Error) as exc:
        return _unavailable(f"OHLCV-Datenbankprüfung fehlgeschlagen: {type(exc).__name__}: {exc}", candidates)
