"""Live-Benchmark gegen den MSCI World / EUNL.DE.

Stichtag: 07.08.2026.

Bewusst KEIN Backtest: Die Funktion liest echte Trades aus dem Google-Drive-
Master „Offene Positionen+Check“. Offene Positionen stammen aus Tab 1
(„Offene Positionen+Check“), abgeschlossene Positionen aus Tab 2
(„Geschlossene Positionen“). Positionen, die am Stichtag bereits offen waren,
beginnen für den Benchmark am 07.08.2026; später eröffnete Positionen beginnen
am tatsächlichen Einstiegsdatum.

Zwei Kennzahlen:
1) Aktueller offener Korb: arithmetisches Mittel der Performance aller
   aktuell offenen Positionen, EUNL.DE selbst wird als Benchmark ausgeschlossen.
2) Live-System seit Stichtag: gleichgewichtete Trade-Performance aller
   Positionen, die am/seit Stichtag aktiv waren, jeweils gegen die
   EUNL.DE-Performance im exakt gleichen Zeitraum.

Die gleichgewichtete Darstellung ist bewusst als Trade-/Signalvergleich
gekennzeichnet und nicht als kapitalgewichtete Depotperformance.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
import yfinance as yf


BENCHMARK_TICKER = "EUNL.DE"
BENCHMARK_NAME = "iShares Core MSCI World UCITS ETF USD (Acc)"
STICHTAG = dt.date(2026, 8, 7)
DRIVE_NAME = "Offene Positionen+Check"
FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"
GOOGLE_SHEETS_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
OPEN_TAB = "Offene Positionen+Check"
CLOSED_TAB = "Geschlossene Positionen"
OUTPUT_FILE = Path("Benchmark_Live.txt")

OPEN_REQUIRED_COLUMNS = {
    "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
    "Aktueller_Kurs", "Performance_Seit_Einstieg%", "Richtung",
}
CLOSED_REQUIRED_COLUMNS = {
    "Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg",
    "Ausstiegsdatum", "Ausstiegskurs", "Richtung",
}


def _norm_num(value):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        text = str(value).strip().replace("%", "")
        if not text or text.lower() == "nan":
            return None
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    for dayfirst in (True, False):
        try:
            return pd.to_datetime(text, dayfirst=dayfirst, errors="raise").date()
        except Exception:
            pass
    return None


def _download_close(ticker: str, start: dt.date, end: dt.date) -> pd.Series:
    """Lädt Schlusskurse robust für einen Zeitraum.

    yfinance behandelt end exklusiv; deshalb wird ein zusätzlicher Kalendertag
    angehängt. MultiIndex-Ausgaben werden auf eine einfache Close-Serie reduziert.
    """
    end_exclusive = end + dt.timedelta(days=3)
    data = yf.download(
        ticker,
        start=start.isoformat(),
        end=end_exclusive.isoformat(),
        progress=False,
        auto_adjust=False,
        actions=False,
        threads=False,
    )
    if data is None or data.empty:
        return pd.Series(dtype=float)

    if isinstance(data.columns, pd.MultiIndex):
        if ticker in data.columns.get_level_values(0):
            frame = data.xs(ticker, axis=1, level=0, drop_level=True).copy()
        elif ticker in data.columns.get_level_values(1):
            frame = data.xs(ticker, axis=1, level=1, drop_level=True).copy()
        else:
            return pd.Series(dtype=float)
    else:
        frame = data.copy()

    # Benchmark-Performance darf keine Rohdaten-Luecke unbemerkt ueberspringen.
    # Das Ende des angeforderten historischen Fensters ist ein eigener
    # reproduzierbarer Stichtag und darf nicht gegen den heutigen Cutoff
    # geprueft werden.
    from ohlcv_quality_gate import (
        apply_ohlcv_quality_gate, _calendar_sessions, _last_completed_session_date,
    )
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.loc[~frame.index.isna()].sort_index()
    frame = frame.loc[(frame.index.date >= start) & (frame.index.date <= end)].copy()
    as_of = pd.Timestamp(end) + pd.Timedelta(days=1, hours=12)
    as_of = as_of.tz_localize("UTC")
    completed_end = _last_completed_session_date(ticker, as_of=as_of)
    expected = (
        _calendar_sessions(ticker, start.isoformat(), completed_end)
        if completed_end and completed_end >= start.isoformat() else None
    )
    if not expected:
        blocked = pd.Series(dtype=float)
        blocked.attrs["ohlcv_quality"] = {
            "ticker": ticker, "quality_status": "INCOMPLETE",
            "reason_codes": ["HISTORICAL_BENCHMARK_CALENDAR_UNAVAILABLE"],
            "rule_version": "1.4",
        }
        return blocked
    window_sessions = min(252, len(expected))
    gated, quality = apply_ohlcv_quality_gate(
        frame, ticker, window_sessions=window_sessions,
        preserve_expected_sessions=True, full_history=True,
        require_structure=False, as_of=as_of,
    )
    if quality.quality_status == "INCOMPLETE" or "Close" not in gated.columns:
        blocked = pd.Series(dtype=float)
        blocked.attrs["ohlcv_quality"] = quality.to_dict()
        return blocked

    series = pd.to_numeric(gated["Close"], errors="coerce")
    series.index = pd.to_datetime(series.index).tz_localize(None)
    series.attrs["ohlcv_quality"] = quality.to_dict()
    return series


def _price_on_or_after(series: pd.Series, date: dt.date):
    if series.empty:
        return None, None
    mask = series.index.date >= date
    candidates = series.loc[mask]
    if candidates.empty or pd.isna(candidates.iloc[0]):
        return None, None
    idx = candidates.index[0]
    return float(candidates.iloc[0]), idx.date()


def _price_on_or_before(series: pd.Series, date: dt.date):
    if series.empty:
        return None, None
    mask = series.index.date <= date
    candidates = series.loc[mask]
    if candidates.empty or pd.isna(candidates.iloc[-1]):
        return None, None
    idx = candidates.index[-1]
    return float(candidates.iloc[-1]), idx.date()


def _system_return(start_price, end_price, short=False):
    if start_price is None or end_price is None or start_price <= 0 or end_price <= 0:
        return None
    if short:
        return (start_price / end_price - 1.0) * 100.0
    return (end_price / start_price - 1.0) * 100.0


def _fmt(value):
    if value is None:
        return "n/a"
    return f"{value:+.2f}%".replace(".", ",")


def _sheet_values_to_frame(values, tab_name, required_columns):
    """Wandelt den Google-Sheet-Aufbau (Titelzeile, Header, Daten) sicher in ein DataFrame um."""
    if not values or len(values) < 2:
        raise RuntimeError(f"Google-Sheet-Tab '{tab_name}' enthält keine Titel-/Headerstruktur.")
    headers = [str(value).strip() for value in values[1]]
    if not headers or any(not value for value in headers):
        raise RuntimeError(f"Google-Sheet-Tab '{tab_name}' besitzt leere Headerfelder.")
    if len(headers) != len(set(headers)):
        raise RuntimeError(f"Google-Sheet-Tab '{tab_name}' besitzt doppelte Spaltennamen.")
    missing = sorted(required_columns - set(headers))
    if missing:
        raise RuntimeError(
            f"Google-Sheet-Tab '{tab_name}' hat nicht das erwartete Schema; "
            f"fehlende Spalten: {', '.join(missing)}."
        )
    records = []
    for row_number, raw in enumerate(values[2:], start=3):
        if not any(str(value).strip() for value in raw if value is not None):
            continue
        padded = list(raw[:len(headers)]) + [""] * max(0, len(headers) - len(raw))
        records.append(dict(zip(headers, padded)))
    return pd.DataFrame(records, columns=headers)


def _combine_position_tabs(open_values, closed_values):
    """Kombiniert ausschließlich offene Tab-1-Daten mit der archivierten Tab-2-Historie."""
    open_df = _sheet_values_to_frame(open_values, OPEN_TAB, OPEN_REQUIRED_COLUMNS)
    closed_df = _sheet_values_to_frame(closed_values, CLOSED_TAB, CLOSED_REQUIRED_COLUMNS)

    if not open_df.empty:
        statuses = open_df["Status"].astype(str).str.strip().str.lower()
        if not statuses.eq("offen").all():
            bad = sorted(set(open_df.loc[~statuses.eq("offen"), "Status"].astype(str)))
            raise RuntimeError(
                f"Google-Sheet-Tab '{OPEN_TAB}' enthält nicht-offene Statuswerte: {bad[:5]}."
            )
        directions = open_df["Richtung"].astype(str).str.strip().str.lower()
        if not directions.isin({"long", "short"}).all():
            bad_tickers = open_df.loc[~directions.isin({"long", "short"}), "Ticker"].astype(str).tolist()
            raise RuntimeError(
                "Google-Sheet-Tab 1 enthält fehlende/ungültige Richtung Long/Short "
                f"für: {bad_tickers[:10]}. Benchmark wird sicherheitshalber abgebrochen."
            )

    if not closed_df.empty:
        statuses = closed_df["Status"].astype(str).str.strip().str.lower()
        if not statuses.isin({"gestoppt", "verkauft"}).all():
            bad = sorted(set(closed_df.loc[~statuses.isin({"gestoppt", "verkauft"}), "Status"].astype(str)))
            raise RuntimeError(
                f"Google-Sheet-Tab '{CLOSED_TAB}' enthält unerwartete Statuswerte: {bad[:5]}."
            )
        directions = closed_df["Richtung"].astype(str).str.strip().str.lower()
        if not directions.isin({"long", "short"}).all():
            bad_tickers = closed_df.loc[~directions.isin({"long", "short"}), "Ticker"].astype(str).tolist()
            raise RuntimeError(
                "Google-Sheet-Tab 2 enthält fehlende/ungültige Richtung Long/Short "
                f"für: {bad_tickers[:10]}."
            )

    # Das offene Ausgabe-Tab enthält Steuerungsart, aber keine Ideen_Quelle.
    # Keine fachlich andere Spalte als Ideen_Quelle ausgeben oder umdeuten.
    if "Ideen_Quelle" not in open_df.columns:
        open_df["Ideen_Quelle"] = ""
    for column in ("Ausstiegsdatum", "Ausstiegskurs"):
        if column not in open_df.columns:
            open_df[column] = ""

    # Archivdaten besitzen Ideen_Quelle bereits. Tab-1-/Tab-2-Schema wird vereinheitlicht,
    # ohne bestehende Archivwerte zu überschreiben.
    combined = pd.concat([open_df, closed_df], ignore_index=True, sort=False)
    if combined.empty:
        return pd.DataFrame()
    return combined


def _load_positions():
    """Lädt den Benchmark-Datenbestand ausschließlich aus dem produktiven Google-Drive-Master."""
    token = os.getenv("GDRIVE_TOKEN")
    if not token:
        raise RuntimeError("GDRIVE_TOKEN fehlt; lokaler CSV-Fallback ist bewusst deaktiviert.")

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except Exception as exc:
        raise RuntimeError(f"Google-API-Bibliotheken sind nicht verfügbar: {exc}") from exc

    try:
        info = json.loads(token)
        credentials = Credentials.from_authorized_user_info(info)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials.valid:
            raise RuntimeError("GDRIVE_TOKEN ist nach Laden/Refresh nicht gültig.")
    except Exception as exc:
        raise RuntimeError(f"GDRIVE_TOKEN konnte nicht verwendet werden: {exc}") from exc

    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    query = (
        f"name='{DRIVE_NAME}' and '{FOLDER_ID}' in parents and trashed=false "
        f"and mimeType='{GOOGLE_SHEETS_MIME_TYPE}'"
    )
    matches = drive.files().list(
        q=query, fields="files(id,name,mimeType)"
    ).execute().get("files", [])
    matches = [
        item for item in matches
        if item.get("name") == DRIVE_NAME
        and item.get("mimeType") == GOOGLE_SHEETS_MIME_TYPE
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Google-Drive-Master nicht eindeutig: erwartet genau eine Datei "
            f"'{DRIVE_NAME}' im konfigurierten Projektordner, gefunden: {len(matches)}."
        )
    spreadsheet_id = matches[0]["id"]

    open_values = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range="'Offene Positionen+Check'!A:AZ",
    ).execute().get("values", [])
    closed_values = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range="'Geschlossene Positionen'!A:AA",
    ).execute().get("values", [])
    positions = _combine_position_tabs(open_values, closed_values)
    print(
        "BENCHMARK-DATENQUELLE: Google Drive | "
        f"offen={sum(positions.get('Status', pd.Series(dtype=str)).astype(str).str.strip().str.lower() == 'offen')}, "
        f"historisch={sum(positions.get('Status', pd.Series(dtype=str)).astype(str).str.strip().str.lower().isin({'gestoppt','verkauft'}))}"
    )
    return positions


def calculate_benchmark():
    df = _load_positions()
    if df.empty:
        return {"text": "Benchmark: keine Positionen vorhanden.", "ok": True}

    today = dt.datetime.now().date()
    # Für die Benchmark niemals einen Endzeitpunkt vor dem Stichtag verwenden.
    today = max(today, STICHTAG)

    status = df.get("Status", pd.Series("", index=df.index)).astype(str).str.strip().str.lower()
    benchmark_excluded = df.get("Ticker", pd.Series("", index=df.index)).astype(str).str.strip().str.upper() == BENCHMARK_TICKER

    # Nur Positionen, die am/seit Stichtag aktiv waren.
    eligible = []
    for idx, row in df.iterrows():
        ticker = str(row.get("Ticker", "")).strip().upper()
        if not ticker or ticker == "NAN" or ticker == BENCHMARK_TICKER:
            continue
        entry_date = _parse_date(row.get("Einstiegsdatum"))
        if entry_date is None:
            continue
        exit_date = _parse_date(row.get("Ausstiegsdatum"))
        if exit_date is not None and exit_date < STICHTAG:
            continue
        if entry_date > today:
            continue

        active_from = max(entry_date, STICHTAG)
        active_to = exit_date if exit_date is not None else today
        if active_to < active_from:
            continue

        eligible.append((idx, row, ticker, entry_date, exit_date, active_from, active_to))

    # Nur tatsächlich benötigte Ticker laden.
    tickers = sorted({BENCHMARK_TICKER} | {item[2] for item in eligible})
    histories = {}
    for ticker in tickers:
        try:
            histories[ticker] = _download_close(ticker, STICHTAG - dt.timedelta(days=5), today + dt.timedelta(days=3))
        except Exception as exc:
            print(f"WARNUNG: Benchmark-Kursdaten für {ticker} nicht ladbar: {exc}")
            histories[ticker] = pd.Series(dtype=float)

    benchmark_hist = histories.get(BENCHMARK_TICKER, pd.Series(dtype=float))

    trade_results = []
    for idx, row, ticker, entry_date, exit_date, active_from, active_to in eligible:
        hist = histories.get(ticker, pd.Series(dtype=float))
        if hist.empty:
            continue

        # Für Positionen, die am Stichtag schon offen waren, verwenden wir den
        # ersten verfügbaren Schlusskurs am/kurz nach dem Stichtag.
        if entry_date < STICHTAG:
            system_start, system_start_date = _price_on_or_after(hist, STICHTAG)
        else:
            system_start = _norm_num(row.get("Einstieg"))
            system_start_date = entry_date
            if system_start is None:
                system_start, system_start_date = _price_on_or_after(hist, entry_date)

        if exit_date is not None and str(row.get("Status", "")).strip().lower() in {"gestoppt", "verkauft"}:
            system_end = _norm_num(row.get("Ausstiegskurs"))
            system_end_date = exit_date
            if system_end is None:
                system_end, system_end_date = _price_on_or_before(hist, exit_date)
        else:
            system_end = _norm_num(row.get("Aktueller_Kurs"))
            system_end_date = today
            if system_end is None:
                system_end, system_end_date = _price_on_or_before(hist, today)

        if system_start is None or system_end is None:
            continue

        short = str(row.get("Richtung", "")).strip().lower() == "short"
        system_perf = _system_return(system_start, system_end, short=short)

        # EUNL.DE wird für denselben Zeitraum als Buy-&-Hold-Referenz verwendet.
        bench_start, bench_start_date = _price_on_or_after(benchmark_hist, system_start_date or active_from)
        bench_end, bench_end_date = _price_on_or_before(benchmark_hist, system_end_date or active_to)
        bench_perf = _system_return(bench_start, bench_end, short=False)

        if system_perf is None or bench_perf is None:
            continue

        trade_results.append({
            "ticker": ticker,
            "name": str(row.get("Name", ticker)).strip(),
            "richtung": "Short" if short else "Long",
            "quelle": str(row.get("Ideen_Quelle", "")).strip()
                or (
                    f"Steuerungsart: {str(row.get('Steuerungsart', '')).strip()}"
                    if str(row.get("Steuerungsart", "")).strip()
                    else "Nicht angegeben"
                ),
            "status": str(row.get("Status", "")).strip(),
            "from": system_start_date,
            "to": system_end_date,
            "system": system_perf,
            "benchmark": bench_perf,
            "delta": system_perf - bench_perf,
        })

    # Aktueller offener Korb: bestehende Portfolio-Performance, aber EUNL.DE
    # selbst wird ausgeschlossen, sonst wäre der Benchmark in seinem eigenen
    # Vergleich enthalten.
    offene = []
    for _, row in df.iterrows():
        ticker = str(row.get("Ticker", "")).strip().upper()
        if ticker in {"", "NAN", BENCHMARK_TICKER}:
            continue
        if str(row.get("Status", "")).strip().lower() != "offen":
            continue

        perf = _norm_num(row.get("Performance_Seit_Einstieg%"))
        if perf is None:
            # Fallback: Falls der Tracker ausnahmsweise keinen aktuellen
            # Performance-Wert geschrieben hat, wird der Wert hier direkt
            # aus Einstieg + aktuellem Kurs rekonstruiert. So bleibt der
            # Benchmark unabhängig von einem einzelnen Tracker-Ausfall.
            entry = _norm_num(row.get("Einstieg"))
            current = _norm_num(row.get("Aktueller_Kurs"))
            if current is None:
                hist = histories.get(ticker, pd.Series(dtype=float))
                current, _ = _price_on_or_before(hist, today)
            if entry is not None and current is not None:
                perf = _system_return(
                    entry, current,
                    short=str(row.get("Richtung", "")).strip().lower() == "short"
                )
        if perf is not None:
            offene.append((ticker, str(row.get("Name", ticker)).strip(), perf))

    offener_schnitt = sum(x[2] for x in offene) / len(offene) if offene else None

    if trade_results:
        system_avg = sum(x["system"] for x in trade_results) / len(trade_results)
        benchmark_avg = sum(x["benchmark"] for x in trade_results) / len(trade_results)
        delta_avg = system_avg - benchmark_avg
        beat_count = sum(1 for x in trade_results if x["delta"] > 0)
        tie_count = sum(1 for x in trade_results if abs(x["delta"]) < 1e-9)
        loss_count = len(trade_results) - beat_count - tie_count
    else:
        system_avg = benchmark_avg = delta_avg = None
        beat_count = tie_count = loss_count = 0

    lines = [
        "LIVE-BENCHMARK vs. MSCI WORLD",
        "=" * 60,
        f"Start der Systemrechnung / Trading-Engine: {STICHTAG.strftime('%d.%m.%Y')}",
        f"Benchmark: {BENCHMARK_NAME} | Ticker: {BENCHMARK_TICKER}",
        "Methode: gleichgewichteter Trade-/Signalvergleich; kein Backtest.",
        "EUNL.DE selbst ist aus dem Systemkorb ausgeschlossen, damit der Benchmark nicht mit sich selbst verglichen wird.",
        "",
        "AKTUELLER OFFENER KORB",
        "-" * 60,
    ]

    if offener_schnitt is None:
        lines.append("Ø Performance aktuell offener Positionen: n/a")
    else:
        lines.append(f"Ø Performance aktuell offener Positionen (ohne EUNL.DE): {_fmt(offener_schnitt)}")
        lines.append(f"Anzahl berücksichtigter offener Positionen: {len(offene)}")

    lines += [
        "",
        "LIVE-SYSTEM SEIT 07.08.2026",
        "-" * 60,
        f"Ø System-Performance: {_fmt(system_avg)}",
        f"Ø MSCI-World-Performance im jeweils gleichen Zeitraum: {_fmt(benchmark_avg)}",
        f"Out-/Underperformance: {_fmt(delta_avg)}",
        f"Positionen besser als MSCI World: {beat_count}/{len(trade_results)}",
        f"Positionen schlechter als MSCI World: {loss_count}/{len(trade_results)}",
        f"Positionen gleichauf: {tie_count}/{len(trade_results)}",
    ]

    if trade_results:
        lines += ["", "TRADE-DETAILS", "-" * 60]
        for item in trade_results:
            lines.append(
                f"{item['ticker']} | {item['richtung']} | {item['quelle']} | "
                f"{item['from'].strftime('%d.%m.%Y')}–{item['to'].strftime('%d.%m.%Y')} | "
                f"System {_fmt(item['system'])} | MSCI {_fmt(item['benchmark'])} | "
                f"Delta {_fmt(item['delta'])}"
            )

    text = "\n".join(lines) + "\n"
    OUTPUT_FILE.write_text(text, encoding="utf-8")
    return {
        "text": text,
        "ok": True,
        "open_count": len(offene),
        "trade_count": len(trade_results),
        "open_avg": offener_schnitt,
        "system_avg": system_avg,
        "benchmark_avg": benchmark_avg,
        "delta": delta_avg,
    }


if __name__ == "__main__":
    result = calculate_benchmark()
    print(result["text"])
