#!/usr/bin/env python3
"""Read-only external cross-check for OHLCV candles flagged INVALID.

Compares the stored rows against Yahoo Finance history(repair=True) and,
where a deterministic Stooq symbol mapping exists, Stooq daily CSV data.
The script never opens SQLite and never writes to the production database.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

ROOT = Path.cwd()
INPUT_CSV = ROOT / os.environ.get("NMM_OHLCV_INVALID_INPUT", "OHLCV_Invalid_Detail.csv")
DETAIL_CSV = ROOT / "OHLCV_Invalid_Quellenvergleich.csv"
SUMMARY_JSON = ROOT / "OHLCV_Invalid_Quellenvergleich.json"
REPORT_MD = ROOT / "OHLCV_Invalid_Quellenvergleich.md"
STOOQ_URL = "https://stooq.com/q/d/l/"
REQUEST_TIMEOUT = max(5, int(os.environ.get("NMM_OHLCV_SOURCE_TIMEOUT", "25")))
REQUEST_PAUSE = max(0.0, float(os.environ.get("NMM_OHLCV_SOURCE_PAUSE", "0.15")))
MAX_TICKERS = max(0, int(os.environ.get("NMM_OHLCV_SOURCE_MAX_TICKERS", "0")))
PRICE_TOLERANCE = float(os.environ.get("NMM_OHLCV_PRICE_TOLERANCE", "0.0001"))

PRICE_FIELDS = ("open", "high", "low", "close")
CME_SETTLEMENT_TICKERS = {"CL=F", "GC=F", "SI=F", "PL=F", "PA=F", "HG=F"}


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_ohlc(row: dict[str, Any]) -> bool:
    values = {key: _float_or_none(row.get(key)) for key in PRICE_FIELDS}
    if any(value is None or value <= 0 for value in values.values()):
        return False
    return (
        values["high"] >= values["low"]
        and values["high"] >= values["open"]
        and values["high"] >= values["close"]
        and values["low"] <= values["open"]
        and values["low"] <= values["close"]
    )


def _price_match(left: dict[str, Any], right: dict[str, Any], tolerance: float = PRICE_TOLERANCE) -> bool:
    for field in PRICE_FIELDS:
        a, b = _float_or_none(left.get(field)), _float_or_none(right.get(field))
        if a is None or b is None:
            return False
        if abs(a - b) > max(1e-8, max(abs(a), abs(b)) * tolerance):
            return False
    return True


def _has_only_close_range_reason(reasons: str) -> bool:
    values = {part for part in str(reasons or "").split("|") if part}
    return bool(values) and values <= {"high_below_close", "low_above_close"}


def stooq_symbol_for_ticker(ticker: str, overrides: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Return a conservative Stooq mapping; futures and indices are not guessed."""
    symbol = str(ticker or "").strip().upper()
    overrides = overrides or {}
    if symbol in overrides and str(overrides[symbol]).strip():
        return str(overrides[symbol]).strip().lower(), "explicit_environment_mapping"
    if symbol.startswith("^") or "=F" in symbol or symbol.endswith("-USD"):
        return None, "unsupported_index_future_or_crypto"
    if symbol == "EURUSD=X":
        return "eurusd", "known_fx_pair_mapping"
    suffix_map = {
        ".DE": ".de", ".L": ".uk", ".SW": ".ch", ".AS": ".nl",
        ".MI": ".it", ".PA": ".fr", ".CO": ".dk", ".TO": ".ca",
    }
    for yahoo_suffix, stooq_suffix in suffix_map.items():
        if symbol.endswith(yahoo_suffix):
            return symbol[:-len(yahoo_suffix)].lower() + stooq_suffix, "exchange_suffix_mapping"
    if symbol.isalnum():
        return symbol.lower() + ".us", "project_us_symbol_convention"
    return None, "no_safe_mapping"


def _load_overrides() -> dict[str, str]:
    raw = os.environ.get("NMM_OHLCV_STOOQ_SYMBOL_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("mapping must be a JSON object")
        return {str(k).upper(): str(v) for k, v in parsed.items() if str(v).strip()}
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"NMM_OHLCV_STOOQ_SYMBOL_MAP ist ungueltig: {exc}") from exc


def load_invalid_rows(path: Path = INPUT_CSV) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"INVALID-Einzeldatei fehlt: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["ticker"] = str(row.get("ticker", "")).strip().upper()
        row["date"] = str(row.get("date", ""))[:10]
        for field in (*PRICE_FIELDS, "volume"):
            row[field] = _float_or_none(row.get(field))
    return [row for row in rows if row.get("ticker") and row.get("date")]


def fetch_yahoo_repaired(ticker: str, start: str, end: str) -> dict[str, dict[str, Any]]:
    """Fetch one ticker through yfinance's documented repair path."""
    import yfinance as yf

    history = yf.Ticker(ticker).history(
        start=start, end=end, interval="1d", auto_adjust=False,
        actions=False, repair=True,
    )
    output: dict[str, dict[str, Any]] = {}
    if history is None or history.empty:
        return output
    for index, row in history.iterrows():
        day = index.date().isoformat() if hasattr(index, "date") else str(index)[:10]
        record = {field: _float_or_none(row.get(field.title())) for field in PRICE_FIELDS}
        record["volume"] = _float_or_none(row.get("Volume"))
        output[day] = record
    return output


def fetch_stooq_rows(symbol: str, start: str, end: str) -> dict[str, dict[str, Any]]:
    """Fetch daily Stooq CSV for a mapped symbol. Empty response is not proof of absence."""
    params = {
        "s": symbol,
        "d1": start.replace("-", ""),
        "d2": end.replace("-", ""),
        "i": "d",
    }
    response = requests.get(STOOQ_URL, params=params, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0 OHLCV-read-only-diagnostic"})
    response.raise_for_status()
    content = response.text.strip()
    if not content or content.lower().startswith("no data"):
        return {}
    output: dict[str, dict[str, Any]] = {}
    reader = csv.DictReader(content.splitlines())
    for row in reader:
        day = str(row.get("Date", ""))[:10]
        if not day:
            continue
        output[day] = {
            "open": _float_or_none(row.get("Open")),
            "high": _float_or_none(row.get("High")),
            "low": _float_or_none(row.get("Low")),
            "close": _float_or_none(row.get("Close")),
            "volume": _float_or_none(row.get("Volume")),
        }
    return output


def _row_date_bounds(rows: list[dict[str, Any]]) -> tuple[str, str]:
    dates = sorted(row["date"] for row in rows)
    start = (dt.date.fromisoformat(dates[0]) - dt.timedelta(days=5)).isoformat()
    end = (dt.date.fromisoformat(dates[-1]) + dt.timedelta(days=6)).isoformat()
    return start, end


def _classify(row: dict[str, Any], yahoo: dict[str, Any] | None,
              stooq: dict[str, Any] | None, stooq_status: str,
              stooq_mapping_method: str = "") -> tuple[str, str]:
    ticker = row["ticker"]
    reasons = str(row.get("invalid_reasons") or "")
    db_is_valid = _valid_ohlc(row)
    if db_is_valid:
        return "OK", "Gespeicherte OHLC-Werte bestehen die Strukturpruefung; erneute Pruefung empfohlen."

    if ticker in CME_SETTLEMENT_TICKERS and _has_only_close_range_reason(reasons):
        if stooq and _valid_ohlc(stooq) and _price_match(row, stooq):
            return "SETTLEMENT_SONDERFALL", "CME-Kontrakt: OHLC passt zur unabhaengigen Quelle; Close liegt ausserhalb der Intraday-Spanne und kann Settlement-Semantik widerspiegeln."
        return "SETTLEMENT_SONDERFALL", "CME-Kontrakt mit ausschliesslich Close-ausserhalb-High/Low; Settlement-Sonderfall ist plausibel, aber nicht abschliessend belegt."

    if stooq and _valid_ohlc(stooq):
        if not _price_match(row, stooq):
            if stooq_mapping_method in {"explicit_environment_mapping", "exchange_suffix_mapping", "known_fx_pair_mapping"}:
                return "BESTÄTIGT_FEHLERHAFT", "Stooq liefert fuer denselben Tag eine strukturell gueltige, deutlich abweichende OHLC-Kerze; das Symbol ist ueber einen expliziten Markt-/FX-Mappingpfad zugeordnet."
            return "ABWEICHUNG_UNGEKLÄRT", "Stooq liefert eine abweichende Kerze, aber das Symbol-Mapping ist nur eine allgemeine Projektkonvention und nicht unabhaengig bestaetigt."
        return "OK", "Unabhaengige Quelle stimmt innerhalb der Toleranz ueberein."

    if yahoo and _valid_ohlc(yahoo) and not _price_match(row, yahoo):
        return "ABWEICHUNG_UNGEKLÄRT", "Yahoo repair=True liefert eine gueltige, abweichende Kerze; gleicher Datenanbieter, daher keine unabhaengige Bestaetigung."

    if stooq_status in {"request_error", "no_data", "unsupported_mapping"}:
        return "ABWEICHUNG_UNGEKLÄRT", f"Keine belastbare Zweitquellenbestaetigung (Stooq: {stooq_status})."
    return "ABWEICHUNG_UNGEKLÄRT", "Die verfuegbaren Quellen bestaetigen weder eine Korrektur noch die gespeicherten Werte."


def run_crosscheck(rows: list[dict[str, Any]], *, yahoo_fetch=fetch_yahoo_repaired,
                   stooq_fetch=fetch_stooq_rows, sleep=time.sleep) -> dict[str, Any]:
    overrides = _load_overrides()
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_ticker[row["ticker"]].append(row)
    tickers = sorted(by_ticker)
    if MAX_TICKERS:
        tickers = tickers[:MAX_TICKERS]
    details: list[dict[str, Any]] = []
    provider_summary: dict[str, Counter] = {"yahoo_repair": Counter(), "stooq": Counter()}
    ticker_provider_cache: dict[str, tuple[dict, dict, str, str, str]] = {}

    for ticker in tickers:
        ticker_rows = by_ticker[ticker]
        start, end = _row_date_bounds(ticker_rows)
        try:
            yahoo_rows = yahoo_fetch(ticker, start, end)
            yahoo_status = "rows_returned" if yahoo_rows else "no_data"
        except Exception as exc:
            yahoo_rows, yahoo_status = {}, f"request_error:{type(exc).__name__}:{exc}"
        if REQUEST_PAUSE:
            sleep(REQUEST_PAUSE)
        stooq_symbol, mapping_method = stooq_symbol_for_ticker(ticker, overrides)
        if not stooq_symbol:
            stooq_rows, stooq_status = {}, "unsupported_mapping"
        else:
            try:
                stooq_rows = stooq_fetch(stooq_symbol, start, end)
                stooq_status = "rows_returned" if stooq_rows else "no_data"
            except Exception as exc:
                stooq_rows, stooq_status = {}, f"request_error:{type(exc).__name__}:{exc}"
        provider_summary["yahoo_repair"][yahoo_status.split(":", 1)[0]] += 1
        provider_summary["stooq"][stooq_status.split(":", 1)[0]] += 1
        ticker_provider_cache[ticker] = (yahoo_rows, stooq_rows, yahoo_status, stooq_status, mapping_method)
        if REQUEST_PAUSE:
            sleep(REQUEST_PAUSE)

    for ticker in tickers:
        yahoo_rows, stooq_rows, yahoo_status, stooq_status, mapping_method = ticker_provider_cache[ticker]
        for row in by_ticker[ticker]:
            yahoo = yahoo_rows.get(row["date"])
            stooq = stooq_rows.get(row["date"])
            classification, basis = _classify(row, yahoo, stooq, stooq_status, mapping_method)
            details.append({
                **row,
                "yahoo_repair_status": "bar_returned" if yahoo else yahoo_status,
                "yahoo_repair_open": yahoo.get("open") if yahoo else None,
                "yahoo_repair_high": yahoo.get("high") if yahoo else None,
                "yahoo_repair_low": yahoo.get("low") if yahoo else None,
                "yahoo_repair_close": yahoo.get("close") if yahoo else None,
                "yahoo_repair_valid_ohlc": _valid_ohlc(yahoo) if yahoo else None,
                "stooq_symbol": stooq_symbol_for_ticker(ticker, overrides)[0] or "",
                "stooq_mapping_method": mapping_method,
                "stooq_status": "bar_returned" if stooq else stooq_status,
                "stooq_open": stooq.get("open") if stooq else None,
                "stooq_high": stooq.get("high") if stooq else None,
                "stooq_low": stooq.get("low") if stooq else None,
                "stooq_close": stooq.get("close") if stooq else None,
                "stooq_valid_ohlc": _valid_ohlc(stooq) if stooq else None,
                "classification": classification,
                "classification_basis": basis,
            })

    counts = Counter(row["classification"] for row in details)
    return {
        "diagnostic": "OHLCV_invalid_external_crosscheck",
        "diagnostic_version": 1,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only": True,
        "database_access": "none; input is the exported invalid-candle CSV",
        "ticker_count_requested": len(by_ticker),
        "ticker_count_checked": len(tickers),
        "invalid_candle_count_checked": len(details),
        "invalid_candle_count_skipped_by_max_tickers": sum(len(by_ticker[t]) for t in set(by_ticker) - set(tickers)),
        "price_tolerance_relative": PRICE_TOLERANCE,
        "provider_ticker_status_counts": {key: dict(value) for key, value in provider_summary.items()},
        "classification_counts": dict(counts),
        "limitations": [
            "Yahoo repair=True ist keine unabhaengige Zweitquelle und wird daher allein nicht als Fehlerbestaetigung gewertet.",
            "Stooq-Zuordnungen aus Boersensuffixen bzw. US-Projektkonvention sind Kandidaten-Mappings; keine Datenverfuegbarkeit wird vorausgesetzt.",
            "Futures werden nicht automatisch auf Stooq-Symbole gemappt. Settlement-Sonderfall wird nur fuer bekannte CME-Kontrakte und ausschliesslich Close-ausserhalb-Range markiert.",
            "Keine Providerantwort ist kein Beleg dafuer, dass die gespeicherte Kerze korrekt oder falsch ist.",
        ],
        "rows": details,
    }


def write_outputs(result: dict[str, Any]) -> None:
    rows = result.get("rows", [])
    columns = [
        "ticker", "date", "open", "high", "low", "close", "volume", "source", "invalid_reasons", "recent_90d",
        "yahoo_repair_status", "yahoo_repair_open", "yahoo_repair_high", "yahoo_repair_low", "yahoo_repair_close", "yahoo_repair_valid_ohlc",
        "stooq_symbol", "stooq_mapping_method", "stooq_status", "stooq_open", "stooq_high", "stooq_low", "stooq_close", "stooq_valid_ohlc",
        "classification", "classification_basis",
    ]
    with DETAIL_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = {key: value for key, value in result.items() if key != "rows"}
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    lines = [
        "# OHLCV-Quellenvergleich der INVALID-Kerzen", "",
        f"- Gepruefte Ticker: {result['ticker_count_checked']} / {result['ticker_count_requested']}",
        f"- Gepruefte Kerzen: {result['invalid_candle_count_checked']}",
        f"- Uebersprungene Kerzen durch Limit: {result['invalid_candle_count_skipped_by_max_tickers']}",
        "", "## Klassifikation", "", "| Kategorie | Kerzen |", "|---|---:|",
    ]
    for key, value in sorted(result["classification_counts"].items()):
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Providerstatus", "", "| Provider | Status | Ticker |", "|---|---|---:|"]
    for provider, statuses in result["provider_ticker_status_counts"].items():
        for status, count in sorted(statuses.items()):
            lines.append(f"| {provider} | {status} | {count} |")
    lines += ["", "## Grenzen der Aussagekraft", ""]
    lines.extend(f"- {item}" for item in result["limitations"])
    lines += ["", "Die Einzelvergleiche stehen in `OHLCV_Invalid_Quellenvergleich.csv`; diese Diagnose liest keine SQLite-Datei und veraendert keine Produktionsdaten.", ""]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    rows = load_invalid_rows()
    result = run_crosscheck(rows)
    write_outputs(result)
    print(json.dumps({key: value for key, value in result.items() if key != "rows"},
                     ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("invalid_candle_count_skipped_by_max_tickers", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
