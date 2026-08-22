"""Live-Benchmark gegen den MSCI World / EUNL.DE.

Stichtag: 07.08.2026.

Bewusst KEIN Backtest: Die Funktion arbeitet ausschließlich mit den in
Offene_Positionen.csv vorhandenen echten Trades. Positionen, die am
Stichtag bereits offen waren, beginnen für den Benchmark am 07.08.2026;
später eröffnete Positionen beginnen am tatsächlichen Einstiegsdatum.

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
import os
from pathlib import Path

import pandas as pd
import yfinance as yf


BENCHMARK_TICKER = "EUNL.DE"
BENCHMARK_NAME = "iShares Core MSCI World UCITS ETF USD (Acc)"
STICHTAG = dt.date(2026, 8, 7)
POSITIONS_FILE = Path("Offene_Positionen.csv")
OUTPUT_FILE = Path("Benchmark_Live.txt")


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
        if "Close" in data.columns.get_level_values(0):
            data = data["Close"]
            if isinstance(data, pd.DataFrame):
                data = data.iloc[:, 0]
        else:
            return pd.Series(dtype=float)
    elif "Close" in data.columns:
        data = data["Close"]
    else:
        return pd.Series(dtype=float)

    series = pd.to_numeric(data, errors="coerce").dropna()
    series.index = pd.to_datetime(series.index).tz_localize(None)
    return series


def _price_on_or_after(series: pd.Series, date: dt.date):
    if series.empty:
        return None, None
    mask = series.index.date >= date
    if not mask.any():
        return None, None
    idx = series.index[mask][0]
    return float(series.loc[idx]), idx.date()


def _price_on_or_before(series: pd.Series, date: dt.date):
    if series.empty:
        return None, None
    mask = series.index.date <= date
    if not mask.any():
        return None, None
    idx = series.index[mask][-1]
    return float(series.loc[idx]), idx.date()


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


def _load_positions():
    if not POSITIONS_FILE.exists():
        raise FileNotFoundError(f"{POSITIONS_FILE} nicht gefunden")
    df = pd.read_csv(POSITIONS_FILE, sep=";", encoding="utf-8-sig")
    return df


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
            "quelle": str(row.get("Ideen_Quelle", "")).strip() or "Manuell",
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
        f"Stichtag: {STICHTAG.strftime('%d.%m.%Y')}",
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
