"""
Persistente historische OHLCV-Datenbank fuer Neuber Macro & Markets.

Die Datenbank ist bewusst von market_cache.py getrennt:
- market_cache.py = kurzlebiger Workflow-Cache
- historical_ohlcv.sqlite = dauerhafter Research-/Backtest-Bestand

Quelle: yfinance/Yahoo Finance, mit auto_adjust=False, damit rohe OHLCV-Werte
und Adj Close getrennt nachvollziehbar bleiben. Der Loader ist inkrementell:
beim Erstaufbau werden fehlende Ticker mit period='max' geladen; danach werden
nur Ticker mit fehlendem/zu altem letzten Handelstag aktualisiert.

Die Tickerliste wird sicher per AST aus analyse.py gelesen. analyse.py wird dabei
NICHT importiert und daher auch nicht ausgefuehrt.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

import pandas as pd

DEFAULT_DB = Path(os.environ.get("NMM_HISTORICAL_OHLCV_DB", "historical_ohlcv.sqlite"))
DEFAULT_SOURCE_FILE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_SOURCE", "analyse.py"))
DEFAULT_DYNAMIC_UNIVERSE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE", "historical_ohlcv_universe.json"))
SCHEMA_VERSION = 1
BATCH_SIZE = int(os.environ.get("NMM_HISTORICAL_OHLCV_BATCH_SIZE", "40"))
REQUEST_PAUSE_SECONDS = float(os.environ.get("NMM_HISTORICAL_OHLCV_PAUSE", "1.0"))
STALE_AFTER_DAYS = int(os.environ.get("NMM_HISTORICAL_OHLCV_STALE_DAYS", "3"))

# Explizite zusaetzliche Benchmarks/Marktserien, die in analyse.py direkt per
# get_index_benchmark_yf() abgefragt werden. Sie werden unabhaengig von der
# AST-Erkennung aufgenommen, damit eine Aenderung an der Funktionsstruktur
# nicht versehentlich die Research-Datenbank verkleinert.
EXPLICIT_BENCHMARKS = {
    "^GSPC", "^IXIC", "^DJI", "^GDAXI", "^STOXX50E", "^STOXX", "^RUT",
    "^N225", "^HSI", "LIT", "^VIX", "CL=F", "BZ=F", "GC=F", "SI=F",
    "PL=F", "PA=F", "HG=F", "BTC-USD", "EURUSD=X", "SPY", "QQQ",
}

REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _string_values(node: ast.AST) -> set[str]:
    values: set[str] = set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        values.add(node.value.strip())
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for item in node.elts:
            values.update(_string_values(item))
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            values.update(_string_values(key))
            values.update(_string_values(value))
    return values


def _load_dynamic_universe(path: Path = DEFAULT_DYNAMIC_UNIVERSE) -> set[str]:
    """Liest zusaetzlich dauerhaft registrierte Einzel-Check-Ticker."""
    if not path.exists():
        return set()
    try:
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("tickers", []) if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            return set()
        return {str(t).strip().upper() for t in values if str(t).strip()}
    except (OSError, ValueError, TypeError) as exc:
        print(f"WARNUNG-HIST-OHLCV: dynamisches Universum konnte nicht gelesen werden: {exc}", file=sys.stderr)
        return set()


def register_ticker(ticker: str, path: Path = DEFAULT_DYNAMIC_UNIVERSE) -> bool:
    """Registriert einen erfolgreich geprüften Einzel-Check-Ticker dauerhaft."""
    ticker = str(ticker or "").strip().upper()
    if not ticker or len(ticker) > 30 or any(ch in ticker for ch in " \t\n"):
        return False
    values = _load_dynamic_universe(path)
    if ticker in values:
        return False
    values.add(ticker)
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"version": 1, "tickers": sorted(values)}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    print(f"HIST-OHLCV-UNIVERSUM: {ticker} dauerhaft registriert ({path})")
    return True


def discover_universe(
    source_file: Path = DEFAULT_SOURCE_FILE,
    dynamic_universe: Path = DEFAULT_DYNAMIC_UNIVERSE,
) -> list[str]:
    """Ermittelt Ticker aus analyse.py plus dauerhaft registrierte Einzel-Checks."""
    source = source_file.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(source_file))
    wanted = {"sektoren_map", "sektoren_aktien", "eu_sektoren_etf", "dax_aktien", "eu_benchmark_ticker"}
    tickers: set[str] = set(EXPLICIT_BENCHMARKS)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        hit = names & wanted
        if not hit or not isinstance(node.value, ast.Dict):
            if "eu_benchmark_ticker" in hit:
                tickers.update(_string_values(node.value))
            continue

        # Sektor-Mappings/ETF-Mappings: Schluessel sind echte Ticker; Werte
        # sind teilweise nur menschenlesbare Sektornamen und duerfen deshalb
        # nicht als Instrumente in die DB gelangen.
        if hit & {"sektoren_map", "eu_sektoren_etf"}:
            for key in node.value.keys:
                tickers.update(_string_values(key))

        # US-Sektoruniversum: Dict-Schluessel sind ebenfalls ETFs; die Listen
        # in den Werten enthalten die eigentlichen US-Aktien.
        if "sektoren_aktien" in hit:
            for key, value in zip(node.value.keys, node.value.values):
                tickers.update(_string_values(key))
                tickers.update(_string_values(value))

        # EU-Aktien: Dict-Schluessel sind Sektorlabels, die Listenwerte sind
        # die eigentlichen Ticker.
        if "dax_aktien" in hit:
            for value in node.value.values:
                tickers.update(_string_values(value))

    # Direkte Benchmark-Aufrufe im Hauptcode, z.B. ^GSPC oder GC=F.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "get_index_benchmark_yf" and node.args:
            tickers.update(_string_values(node.args[0]))

    tickers.update(_load_dynamic_universe(dynamic_universe))
    return sorted(t for t in tickers if t and len(t) <= 30 and not any(ch in t for ch in " \t\n"))


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def init_db(db_path: Path = DEFAULT_DB) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS instruments (
                ticker TEXT PRIMARY KEY,
                first_date TEXT,
                last_date TEXT,
                row_count INTEGER NOT NULL DEFAULT 0,
                last_update_utc TEXT,
                source TEXT NOT NULL DEFAULT 'yfinance'
            );
            CREATE TABLE IF NOT EXISTS bars (
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                adj_close REAL,
                volume REAL,
                source TEXT NOT NULL DEFAULT 'yfinance',
                PRIMARY KEY (ticker, date)
            );
            CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date);
            CREATE INDEX IF NOT EXISTS idx_bars_ticker_date ON bars(ticker, date);
            """
        )
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('source_convention',?)", ("yfinance;auto_adjust=False;interval=1d",))


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    df = frame.copy()
    if isinstance(df.columns, pd.MultiIndex):
        # A single ticker can still arrive with a MultiIndex. Reduce one level
        # only when the remaining labels uniquely identify OHLCV fields.
        if len(df.columns.levels) >= 2:
            candidates = []
            for level in range(df.columns.nlevels):
                vals = list(df.columns.get_level_values(level))
                if any(v in REQUIRED_COLUMNS for v in vals):
                    candidates.append(level)
            if candidates:
                df.columns = df.columns.get_level_values(candidates[-1])
    rename = {c: str(c).title().replace("Adj Close", "Adj Close") for c in df.columns}
    df = df.rename(columns=rename)
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    idx = pd.to_datetime(df.index, errors="coerce")
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx
    df = df[~df.index.isna()].copy()
    for col in REQUIRED_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[REQUIRED_COLUMNS]


def _extract_ticker_frame(hist: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if hist is None or hist.empty:
        return pd.DataFrame()
    if not isinstance(hist.columns, pd.MultiIndex):
        return _normalise_frame(hist)
    for level in range(hist.columns.nlevels):
        vals = list(hist.columns.get_level_values(level))
        if ticker in vals:
            try:
                return _normalise_frame(hist.xs(ticker, axis=1, level=level, drop_level=True))
            except (KeyError, ValueError):
                pass
    # Fallback fuer die seltene Struktur (field, ticker) / (ticker, field).
    wanted = {}
    for col in hist.columns:
        parts = [str(x) for x in col]
        if ticker in parts:
            other = next((x for x in parts if x in REQUIRED_COLUMNS), None)
            if other:
                wanted[other] = col
    if wanted:
        return _normalise_frame(hist[[wanted[c] for c in wanted]])
    return pd.DataFrame()


def _download_batch(tickers: list[str], *, start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    if not tickers:
        return {}
    kwargs = dict(tickers=tickers if len(tickers) > 1 else tickers[0], interval="1d", auto_adjust=False, progress=False, threads=True)
    if start is None:
        kwargs["period"] = "max"
    else:
        kwargs["start"] = start
        kwargs["end"] = end or (dt.date.today() + dt.timedelta(days=1)).isoformat()
    hist = yf.download(**kwargs)
    return {ticker: _extract_ticker_frame(hist, ticker) for ticker in tickers}


def _db_latest(conn: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in conn.execute("SELECT ticker, MAX(date) FROM bars GROUP BY ticker")}


def _upsert_frames(conn: sqlite3.Connection, frames: dict[str, pd.DataFrame]) -> int:
    rows = []
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    for ticker, frame in frames.items():
        df = _normalise_frame(frame)
        for idx, row in df.iterrows():
            date = pd.Timestamp(idx).strftime("%Y-%m-%d")
            rows.append((ticker, date, *[None if pd.isna(row[c]) else float(row[c]) for c in REQUIRED_COLUMNS], "yfinance"))
    if not rows:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO bars
           (ticker,date,open,high,low,close,adj_close,volume,source)
           VALUES (?,?,?,?,?,?,?,?,?)""", rows
    )
    for ticker in frames:
        conn.execute(
            """INSERT INTO instruments(ticker,first_date,last_date,row_count,last_update_utc,source)
               SELECT ?, MIN(date), MAX(date), COUNT(*), ?, 'yfinance' FROM bars WHERE ticker=?
               ON CONFLICT(ticker) DO UPDATE SET first_date=excluded.first_date,last_date=excluded.last_date,
               row_count=excluded.row_count,last_update_utc=excluded.last_update_utc,source=excluded.source""",
            (ticker, now, ticker),
        )
    return len(rows)


def update_database(db_path: Path = DEFAULT_DB, source_file: Path = DEFAULT_SOURCE_FILE, dynamic_universe: Path = DEFAULT_DYNAMIC_UNIVERSE, *, force_bootstrap: bool = False) -> dict:
    init_db(db_path)
    tickers = discover_universe(source_file, dynamic_universe)
    stats = {"tickers": len(tickers), "missing": 0, "updated": 0, "rows": 0, "errors": 0}
    with _connect(db_path) as conn:
        latest = _db_latest(conn)
        missing = tickers if force_bootstrap else [t for t in tickers if t not in latest]
        stats["missing"] = len(missing)

        # Erstaufbau: period=max in kontrollierten Batches.
        for batch in _chunks(missing, BATCH_SIZE):
            try:
                frames = _download_batch(batch)
                with conn:
                    stats["rows"] += _upsert_frames(conn, frames)
                stats["updated"] += sum(1 for f in frames.values() if not f.empty)
            except Exception as exc:
                stats["errors"] += 1
                print(f"WARNUNG-HIST-OHLCV: Bootstrap-Batch fehlgeschlagen ({len(batch)}): {exc}", file=sys.stderr)
            if REQUEST_PAUSE_SECONDS:
                time.sleep(REQUEST_PAUSE_SECONDS)

        # Inkrementell: nur Reihen aktualisieren, die hinter dem erwarteten
        # letzten abgeschlossenen Handelstag liegen. Ein 5-Tage-Puffer deckt
        # Wochenenden und einzelne Feiertage ab.
        expected = pd.Timestamp.today().normalize() - pd.offsets.BDay(1)
        current_latest = _db_latest(conn)
        stale = [t for t in tickers if t in current_latest and pd.Timestamp(current_latest[t]) < expected - pd.Timedelta(days=STALE_AFTER_DAYS - 1)]
        for batch in _chunks(stale, BATCH_SIZE):
            min_date = min(pd.Timestamp(current_latest[t]) for t in batch) - pd.Timedelta(days=5)
            try:
                frames = _download_batch(batch, start=min_date.strftime("%Y-%m-%d"))
                with conn:
                    # Nur neue Zeilen werden uebernommen; bereits vorhandene
                    # Werte duerfen durch Yahoo-Aktualisierungen korrigiert werden.
                    stats["rows"] += _upsert_frames(conn, frames)
                stats["updated"] += sum(1 for f in frames.values() if not f.empty)
            except Exception as exc:
                stats["errors"] += 1
                print(f"WARNUNG-HIST-OHLCV: Update-Batch fehlgeschlagen ({len(batch)}): {exc}", file=sys.stderr)
            if REQUEST_PAUSE_SECONDS:
                time.sleep(REQUEST_PAUSE_SECONDS)

    with _connect(db_path) as check_conn:
        current_latest = _db_latest(check_conn)
    stats["ok"] = sum(1 for ticker in tickers if ticker in current_latest)
    stats["missing_after"] = len(tickers) - stats["ok"]
    print(
        "HIST-OHLCV: tickers={tickers} missing_initial={missing} updated={updated} "
        "rows={rows} errors={errors} coverage_ok={ok} coverage_missing={missing_after} db={db}".format(
            **stats, db=db_path
        )
    )
    if stats["missing_after"]:
        print(
            f"WARNUNG-HIST-OHLCV: Datenbank unvollstaendig: "
            f"expected={len(tickers)} ok={stats['ok']} missing={stats['missing_after']} errors={stats['errors']}",
            file=sys.stderr,
        )
    return stats


def coverage(db_path: Path = DEFAULT_DB, source_file: Path = DEFAULT_SOURCE_FILE, dynamic_universe: Path = DEFAULT_DYNAMIC_UNIVERSE) -> pd.DataFrame:
    tickers = discover_universe(source_file, dynamic_universe)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ticker, first_date, last_date, row_count, last_update_utc, source FROM instruments"
        ).fetchall()
    existing = {r[0]: r[1:] for r in rows}
    out = []
    for ticker in tickers:
        r = existing.get(ticker)
        out.append({
            "Ticker": ticker,
            "First": r[0] if r else None,
            "Last": r[1] if r else None,
            "Rows": r[2] if r else 0,
            "UpdatedUTC": r[3] if r else None,
            "Source": r[4] if r else None,
            "Status": "OK" if r and r[2] else "MISSING",
        })
    return pd.DataFrame(out).sort_values(["Status", "Ticker"])


def load_history(ticker: str, db_path: Path = DEFAULT_DB) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            """SELECT date, open AS Open, high AS High, low AS Low, close AS Close,
                      adj_close AS "Adj Close", volume AS Volume
               FROM bars WHERE ticker=? ORDER BY date""",
            conn, params=(ticker,), parse_dates=["date"]
        )
    if df.empty:
        return df
    return df.set_index("date")


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistente historische OHLCV-Datenbank")
    parser.add_argument("command", choices=["update", "coverage", "query"], nargs="?", default="update")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_FILE)
    parser.add_argument("--dynamic-universe", type=Path, default=DEFAULT_DYNAMIC_UNIVERSE)
    parser.add_argument("--summary", action="store_true", help="bei coverage nur die Coverage-Zusammenfassung ausgeben")
    parser.add_argument("--ticker")
    parser.add_argument("--bootstrap", action="store_true", help="beim Update alle Ticker als fehlend behandeln")
    args = parser.parse_args()

    if args.command == "update":
        update_database(args.db, args.source, args.dynamic_universe, force_bootstrap=args.bootstrap)
        return 0
    if args.command == "coverage":
        if not args.db.exists():
            print(f"Datenbank fehlt: {args.db}")
            return 2
        report = coverage(args.db, args.source, args.dynamic_universe)
        if args.summary:
            expected = len(report)
            ok = int((report["Status"] == "OK").sum())
            missing = expected - ok
            print(f"HIST-OHLCV-COVERAGE: expected={expected} ok={ok} missing={missing}")
            if missing:
                print("HIST-OHLCV-COVERAGE: FEHLENDE TICKER:")
                print(" ".join(report.loc[report["Status"] == "MISSING", "Ticker"].tolist()))
        else:
            print(report.to_string(index=False))
        return 0 if (report["Status"] == "OK").all() else 1
    if not args.ticker:
        parser.error("query benoetigt --ticker")
    df = load_history(args.ticker, args.db)
    if df.empty:
        print(f"Keine Daten fuer {args.ticker}")
        return 1
    print(df.tail(10).to_string())
    print(f"ROWS={len(df)} FIRST={df.index.min().date()} LAST={df.index.max().date()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
