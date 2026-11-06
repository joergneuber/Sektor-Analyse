#!/usr/bin/env python3
"""Independent, read-only Alpha Vantage cross-check for five recent INVALID candles.

Reads only OHLCV_Invalid_Detail.csv; never opens or writes the production SQLite DB.
Uses Alpha Vantage's symbol search + raw daily OHLC endpoint for equities and
FX_DAILY for EUR/USD. Missing credentials/data are explicitly unresolved.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import requests

ROOT = Path.cwd()
INPUT_CSV = ROOT / os.environ.get("NMM_OHLCV_INVALID_INPUT", "OHLCV_Invalid_Detail.csv")
DETAIL_CSV = ROOT / "OHLCV_Invalid_AlphaVantage.csv"
SUMMARY_JSON = ROOT / "OHLCV_Invalid_AlphaVantage.json"
REPORT_MD = ROOT / "OHLCV_Invalid_AlphaVantage.md"
API_URL = "https://www.alphavantage.co/query"
API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
REQUEST_TIMEOUT = max(5, int(os.environ.get("NMM_OHLCV_ALPHAVANTAGE_TIMEOUT", "20")))
REQUEST_PAUSE = max(0.0, float(os.environ.get("NMM_OHLCV_ALPHAVANTAGE_PAUSE_SECONDS", "12")))
PRICE_FIELDS = ("open", "high", "low", "close")
TARGETS = {
    ("ABBNY", "2026-11-05"),
    ("EURUSD=X", "2026-08-17"),
    ("EURUSD=X", "2026-11-02"),
    ("LZAGF", "2026-11-05"),
    ("SIEGY", "2026-11-05"),
}


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
    values = {field: _float_or_none(row.get(field)) for field in PRICE_FIELDS}
    if any(value is None or value <= 0 for value in values.values()):
        return False
    return (
        values["high"] >= values["low"]
        and values["high"] >= values["open"]
        and values["high"] >= values["close"]
        and values["low"] <= values["open"]
        and values["low"] <= values["close"]
    )


def _price_match(left: dict[str, Any], right: dict[str, Any], tolerance: float = 0.0001) -> bool:
    for field in PRICE_FIELDS:
        a, b = _float_or_none(left.get(field)), _float_or_none(right.get(field))
        if a is None or b is None:
            return False
        if abs(a - b) > max(1e-8, max(abs(a), abs(b)) * tolerance):
            return False
    return True


def load_target_rows(path: Path = INPUT_CSV) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"INVALID-Einzeldatei fehlt: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    selected = []
    for source in source_rows:
        ticker = str(source.get("ticker", "")).strip().upper()
        day = str(source.get("date", ""))[:10]
        if (ticker, day) not in TARGETS:
            continue
        row = dict(source)
        row["ticker"], row["date"] = ticker, day
        for field in PRICE_FIELDS:
            row[field] = _float_or_none(row.get(field))
        selected.append(row)
    selected.sort(key=lambda item: (item["ticker"], item["date"]))
    return selected


def _api_json(params: dict[str, str], http_get: Callable = requests.get) -> dict[str, Any]:
    response = http_get(API_URL, params=params, timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent": "OHLCV-read-only-independent-diagnostic/1.0"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Alpha Vantage returned a non-object response")
    provider_error = payload.get("Error Message") or payload.get("Note") or payload.get("Information")
    if provider_error:
        raise RuntimeError(f"provider_message:{str(provider_error)[:240]}")
    return payload


def _parse_daily_series(payload: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    series = payload.get("Time Series (Daily)") or payload.get("Time Series FX (Daily)")
    if not isinstance(series, dict):
        return {}
    output = {}
    for day, values in series.items():
        output[str(day)[:10]] = {
            "open": _float_or_none(values.get("1. open")),
            "high": _float_or_none(values.get("2. high")),
            "low": _float_or_none(values.get("3. low")),
            "close": _float_or_none(values.get("4. close")),
        }
    return output


def fetch_equity_daily(ticker: str, *, http_get: Callable = requests.get,
                       sleep: Callable = time.sleep) -> tuple[dict[str, dict[str, Any]], str, str]:
    """Verify an exact US symbol in AV's directory, then fetch raw daily bars."""
    if not API_KEY:
        return {}, "provider_not_configured", ""
    search = _api_json({"function": "SYMBOL_SEARCH", "keywords": ticker, "apikey": API_KEY}, http_get)
    matches = search.get("bestMatches", [])
    exact = [item for item in matches if str(item.get("1. symbol", "")).upper() == ticker]
    if len(exact) != 1 or str(exact[0].get("4. region", "")).lower() != "united states":
        return {}, "symbol_not_verified", ""
    if REQUEST_PAUSE:
        sleep(REQUEST_PAUSE)
    payload = _api_json({"function": "TIME_SERIES_DAILY", "symbol": ticker,
                         "outputsize": "compact", "apikey": API_KEY}, http_get)
    bars = _parse_daily_series(payload)
    return bars, ("rows_returned" if bars else "no_data"), str(exact[0].get("2. name", ""))


def fetch_fx_daily(*, http_get: Callable = requests.get) -> tuple[dict[str, dict[str, Any]], str]:
    if not API_KEY:
        return {}, "provider_not_configured"
    payload = _api_json({"function": "FX_DAILY", "from_symbol": "EUR", "to_symbol": "USD",
                         "outputsize": "compact", "apikey": API_KEY}, http_get)
    bars = _parse_daily_series(payload)
    return bars, ("rows_returned" if bars else "no_data")


def _classify(row: dict[str, Any], source: dict[str, Any] | None,
              source_status: str, *, is_fx: bool = False) -> tuple[str, str]:
    if not source:
        return "ABWEICHUNG_UNGEKLÄRT", f"Keine Alpha-Vantage-Kerze für diesen Handelstag (Status: {source_status})."
    if not _valid_ohlc(source):
        return "ABWEICHUNG_UNGEKLÄRT", "Alpha-Vantage-Daten sind für diesen Tag strukturell nicht valide."
    if _price_match(row, source):
        return "OK", "Unabhängige Alpha-Vantage-Rohdaten stimmen innerhalb 0,01 % relativer Toleranz überein."
    if is_fx:
        return "ABWEICHUNG_UNGEKLÄRT", (
            "Alpha Vantage weicht ab; bei FX-Tageskerzen können abweichende Tagesgrenzen/Session-Cutoffs "
            "die OHLC-Werte verändern. Keine Fehlerbestätigung ohne Session-Abgleich."
        )
    return "BESTÄTIGT_FEHLERHAFT", (
        "Alpha Vantage bestätigt für das exakt verifizierte US-Symbol am selben Datum eine strukturell "
        "valide, deutlich abweichende rohe Tageskerze."
    )


def run_crosscheck(rows: list[dict[str, Any]], *, http_get: Callable = requests.get,
                   sleep: Callable = time.sleep) -> dict[str, Any]:
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append(row)
    provider_cache: dict[str, tuple[dict[str, dict[str, Any]], str, str]] = {}
    for ticker in sorted(by_ticker):
        if ticker == "EURUSD=X":
            try:
                bars, status = fetch_fx_daily(http_get=http_get)
                provider_cache[ticker] = (bars, status, "EUR/USD FX_DAILY")
            except Exception as exc:
                provider_cache[ticker] = ({}, f"request_error:{type(exc).__name__}:{exc}", "")
        else:
            try:
                bars, status, name = fetch_equity_daily(ticker, http_get=http_get, sleep=sleep)
                provider_cache[ticker] = (bars, status, name)
            except Exception as exc:
                provider_cache[ticker] = ({}, f"request_error:{type(exc).__name__}:{exc}", "")
        if API_KEY and REQUEST_PAUSE and ticker != sorted(by_ticker)[-1]:
            sleep(REQUEST_PAUSE)

    details = []
    for ticker in sorted(by_ticker):
        bars, status, provider_name = provider_cache[ticker]
        for row in by_ticker[ticker]:
            source = bars.get(row["date"])
            is_fx = ticker == "EURUSD=X"
            row_status = "bar_returned" if source else ("date_not_returned" if bars else status)
            classification, basis = _classify(row, source, row_status, is_fx=is_fx)
            details.append({
                **row,
                "independent_provider": "Alpha Vantage",
                "independent_symbol": "EUR/USD" if is_fx else ticker,
                "independent_instrument_name": provider_name,
                "independent_status": row_status,
                "independent_open": source.get("open") if source else None,
                "independent_high": source.get("high") if source else None,
                "independent_low": source.get("low") if source else None,
                "independent_close": source.get("close") if source else None,
                "independent_valid_ohlc": _valid_ohlc(source) if source else None,
                "classification": classification,
                "classification_basis": basis,
            })

    missing_targets = sorted(TARGETS - {(row["ticker"], row["date"]) for row in rows})
    counts = Counter(item["classification"] for item in details)
    return {
        "diagnostic": "OHLCV_target_independent_crosscheck",
        "diagnostic_version": 1,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only": True,
        "database_access": "none; reads exported INVALID CSV only",
        "provider": "Alpha Vantage",
        "api_key_configured": bool(API_KEY),
        "requested_targets": len(TARGETS),
        "targets_found_in_invalid_csv": len(details),
        "targets_missing_from_invalid_csv": [f"{ticker} {day}" for ticker, day in missing_targets],
        "provider_request_count_expected_max": 7,
        "price_tolerance_relative": 0.0001,
        "classification_counts": dict(counts),
        "limitations": [
            "Aktien werden nur bei exakt verifiziertem US-Symbol und strukturell valider Roh-Tageskerze als bestaetigt klassifiziert.",
            "EUR/USD-Abweichungen bleiben wegen moeglicher unterschiedlicher FX-Tagesgrenzen ausdruecklich ungeklärt.",
            "Alpha-Vantage compact liefert die letzten 100 Tagesdatenpunkte; full ist fuer TIME_SERIES_DAILY ein Premium-Endpunkt.",
            "Fehlender API-Schluessel, fehlende Daten, Quotenfehler und nicht verifizierte Symbole sind kein Beleg fuer Richtigkeit oder Fehler.",
            "Es werden keine SQLite-Dateien gelesen oder veraendert; der API-Schluessel wird nicht ausgegeben oder gespeichert.",
        ],
        "rows": details,
    }


def write_outputs(result: dict[str, Any]) -> None:
    rows = result["rows"]
    columns = ["ticker", "date", "open", "high", "low", "close", "invalid_reasons",
               "independent_provider", "independent_symbol", "independent_instrument_name",
               "independent_status", "independent_open", "independent_high", "independent_low",
               "independent_close", "independent_valid_ohlc", "classification", "classification_basis"]
    with DETAIL_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = {key: value for key, value in result.items() if key != "rows"}
    SUMMARY_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Unabhängiger OHLCV-Zielabgleich – Alpha Vantage", "",
             f"- API-Key konfiguriert: {result['api_key_configured']}",
             f"- Zielkerzen gefunden: {result['targets_found_in_invalid_csv']} / {result['requested_targets']}",
             f"- Fehlende Zielkerzen in INVALID-CSV: {len(result['targets_missing_from_invalid_csv'])}",
             "", "## Klassifikation", "", "| Status | Anzahl |", "|---|---:|"]
    for key, value in sorted(result["classification_counts"].items()):
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Einzelbefunde", "", "| Ticker | Datum | Providerstatus | Klassifikation |", "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['ticker']} | {row['date']} | {row['independent_status']} | {row['classification']} |")
    lines += ["", "## Grenzen der Aussagekraft", ""]
    lines.extend(f"- {item}" for item in result["limitations"])
    lines += ["", "Die Diagnose arbeitet ausschliesslich mit der exportierten INVALID-CSV; die Produktionsdatenbank bleibt unangetastet.", ""]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    rows = load_target_rows()
    result = run_crosscheck(rows)
    write_outputs(result)
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, ensure_ascii=False, indent=2))
    return 0 if not result["targets_missing_from_invalid_csv"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
