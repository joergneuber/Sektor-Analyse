"""
Gemeinsame robuste Marktdaten-Abrufe fuer Neuber Macro & Markets.

Ziel: Ein einzelnes ungueltiges Alpaca-Symbol darf niemals einen kompletten
100-Ticker-Sammelabruf verwerfen. Fehlerhafte Chunks werden bei einem
"invalid symbol"-Fehler rekursiv geteilt, bis die gueltigen Titel trotzdem
verarbeitet werden koennen. Bekannte Alpaca-Symbolschreibweisen werden nur
fuer den API-Request normalisiert; im Ergebnis bleibt der Original-Ticker
als Schluessel erhalten.
"""

from __future__ import annotations

import datetime
from typing import Any

import pandas as pd
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# Yahoo-/Projekt-Schreibweise -> Alpaca-Schreibweise.
# Nur im Request verwenden, niemals den Projekt-Ticker dauerhaft umbenennen.
ALPACA_SYMBOL_ALIASES = {
    "BRK-B": "BRK.B",
    "BF-B": "BF.B",
}


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _request_chunk(client: Any, original_symbols: list[str], start_date: datetime.datetime,
                   ergebnis: dict[str, pd.DataFrame], depth: int = 0) -> None:
    """Fordert einen Chunk an; bei invalid-symbol-Fehlern wird er geteilt."""
    if not original_symbols:
        return

    request_symbols = [ALPACA_SYMBOL_ALIASES.get(s, s) for s in original_symbols]
    try:
        request = StockBarsRequest(
            symbol_or_symbols=request_symbols,
            start=start_date,
            timeframe=TimeFrame.Day,
        )
        df_alle = client.get_stock_bars(request).df
    except Exception as exc:
        msg = str(exc)
        invalid_symbol = "invalid symbol" in msg.lower()

        if invalid_symbol and len(original_symbols) > 1:
            mitte = len(original_symbols) // 2
            _request_chunk(client, original_symbols[:mitte], start_date, ergebnis, depth + 1)
            _request_chunk(client, original_symbols[mitte:], start_date, ergebnis, depth + 1)
            return

        if invalid_symbol and len(original_symbols) == 1:
            print(
                f"DEBUG-US-DATEN: Ticker {original_symbols[0]} von Alpaca abgelehnt "
                f"(ungültiges Symbol) - nur dieser Ticker wird übersprungen."
            )
        else:
            print(
                f"FEHLER beim Sammel-Abruf US-Chunk ({len(original_symbols)} Ticker): {exc}"
            )
        return

    if df_alle is None or df_alle.empty:
        return

    for original, request_symbol in zip(original_symbols, request_symbols):
        try:
            # Alpaca liefert bei mehreren Symbolen einen MultiIndex. Bei einem
            # einzelnen Symbol kann die Struktur ebenfalls direkt adressierbar sein.
            try:
                data = df_alle.loc[request_symbol].copy()
            except KeyError:
                # Manche Client-Versionen liefern trotz Alias den Originalticker.
                data = df_alle.loc[original].copy()
        except KeyError:
            continue

        if data.empty:
            continue

        if 'close' in data.columns:
            data = data.rename(columns={
                'close': 'Close', 'high': 'High', 'low': 'Low',
                'open': 'Open', 'volume': 'Volume'
            })
        ergebnis[original] = data


def fetch_us_batch_robust(client: Any, ticker_liste: list[str], *,
                          chunk_size: int = 100, days: int = 365) -> dict[str, pd.DataFrame]:
    """Robuster Alpaca-Sammelabruf.

    Ein invalides Symbol verwirft nicht mehr den gesamten Chunk. Nur echte
    Einzel-Ticker-Fehler werden ausgelassen; technische/temporäre Chunk-Fehler
    werden weiterhin klar geloggt und nicht durch aggressive Einzelabfragen
    kaschiert.
    """
    ergebnis: dict[str, pd.DataFrame] = {}
    start_date = datetime.datetime.now() - datetime.timedelta(days=days)
    ticker_liste = list(dict.fromkeys(str(t).strip() for t in ticker_liste if str(t).strip()))

    for chunk in _chunks(ticker_liste, chunk_size):
        _request_chunk(client, chunk, start_date, ergebnis)

    print(
        f"DEBUG: Robuster US-Sammel-Abruf lieferte Daten fuer "
        f"{len(ergebnis)}/{len(ticker_liste)} Ticker."
    )
    return ergebnis
