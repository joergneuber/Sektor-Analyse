#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offene Positionen + Check

Eigenstaendiger technischer Check fuer AUSSCHLIESSLICH offene Positionen.
Die bestehende Offene_Positionen.csv wird nur gelesen und niemals veraendert.

Feste Regeln:
- Steuerungsart kennt nur "Aktiver Trade" und "Buy & Hold".
- Technische Analyse nur bei Status == "Offen".
- Breakout aktiviert NICHT automatisch Fibonacci.
- Fibonacci/Fibonacci-Extension erst nach bestaetigter A-B-C-Struktur.
- Historische Major-Level (insbesondere ATH) bleiben unabhaengig erhalten.
- Ein gebrochener Widerstand wird als moegliche Retest-/Supportzone weitergefuehrt.
- Keine technischen Zielwerte werden in TP1/TP2 der Originaldatei geschrieben.
- Letzter abgeschlossener Tages-Schluss wird fuer aktuelle technische Werte verwendet.
- Fehlende/duenne Daten fuehren zu einer expliziten Datenqualitaetsmeldung statt
  zu erfundenen Zielzonen.
- Die Ausgabe ist eine neue Datei / ein neues Google Sheet "Offene Positionen+Check".
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import re
import ast
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.signal import argrelextrema

# Optional: Google Sheets/Drive nur fuer die Ausgabe. Der lokale Check funktioniert
# auch ohne Google-Credentials und erzeugt dann die CSV-Ausgabe.
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    GOOGLE_AVAILABLE = True
except Exception:
    GOOGLE_AVAILABLE = False


INPUT_FILE = "Offene_Positionen.csv"
OUTPUT_CSV = "Offene Positionen+Check.csv"
DRIVE_NAME = "Offene Positionen+Check"
FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"
GOLD_SPOT_TICKER = "XAUUSD=X"
# Historischer Widerstand gilt fuer die Fibonacci-Sperre nur dann als
# "unmittelbar", wenn er maximal 10 % oberhalb des aktuellen Kurses liegt.
# Weiter entfernte Major-Level/ATH bleiben eigenstaendige Referenzen und
# blockieren Fibonacci nicht.
IMMEDIATE_RESISTANCE_MAX_DISTANCE = 0.10

HEADERS = [
    "Ticker", "Name", "Steuerungsart", "Sektor", "Markt", "Waehrung",
    "Status", "Einstieg", "Aktueller_Kurs", "Performance_Seit_Einstieg%",
    "Technischer_Zustand", "Trendrichtung", "Technische_Lage",
    "Support_1", "Support_2", "Widerstand_1", "Widerstand_2",
    "Breakout_Status", "A-B-C_Status", "Fibonacci_Status",
    "Fibonacci_Ziel_1", "Fibonacci_Ziel_2", "Fibonacci_Ziel_3", "Trendkanal_Obergrenze",
    "Measured_Move_Ziel", "Formation", "Round_Number_Zone", "Major_Resistance",
    "Ueberdehnung", "Relative_Staerke_Sektor", "Konfluenz", "Retest_Support",
    "Technische_Zielzone", "Datenqualitaet", "Analysehinweis",
]

NUMERIC_COLUMNS = {
    "Einstieg", "Aktueller_Kurs", "Performance_Seit_Einstieg%",
    "Support_1", "Support_2", "Widerstand_1", "Widerstand_2",
    "Fibonacci_Ziel_1", "Fibonacci_Ziel_2", "Fibonacci_Ziel_3", "Trendkanal_Obergrenze",
    "Measured_Move_Ziel", "Round_Number_Zone", "Major_Resistance",
}

HISTORY_HEADERS = [
    "Ticker", "Name", "Sektor", "Markt", "Waehrung", "Richtung",
    "Ideen_Quelle", "Einstiegsdatum", "Einstieg", "Aktueller_Kurs",
    "Stop", "TP1", "TP2", "Status", "Ausstiegsdatum", "Ausstiegskurs",
    "Performance_Seit_Einstieg%", "TP_Hinweis", "Alert_Hinweis",
    "Produkt_Typ", "Emittent", "Hebel", "OS_Einstiegskurs",
    "OS_Manueller_Kurs", "OS_Performance%", "OS_Quelle", "OS_WKN",
]


@dataclass
class TechnicalResult:
    close: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    rsi: Optional[float] = None
    trend: str = "Nicht bestimmbar"
    state: str = "Nicht bestimmbar"
    support1: Optional[float] = None
    support2: Optional[float] = None
    resistance1: Optional[float] = None
    resistance2: Optional[float] = None
    breakout_status: str = "Kein bestätigter Breakout"
    abc_status: str = "Nicht bestätigt"
    fib_status: str = "Nicht aktiv"
    fib1: Optional[float] = None
    fib2: Optional[float] = None
    fib3: Optional[float] = None
    channel_upper: Optional[float] = None
    measured_move: Optional[float] = None
    formation: str = "Keine belastbare Formation"
    round_number: Optional[float] = None
    overextension: str = "Nicht bestimmbar"
    sector_rs: str = "Nicht bestimmbar"
    major_resistance: Optional[float] = None
    confluence: str = "Keine"
    retest_support: Optional[float] = None
    data_quality: str = ""
    note: str = ""


def parse_number(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float, np.number)):
        try:
            return float(value) if math.isfinite(float(value)) else None
        except Exception:
            return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    text = text.replace("%", "").replace("€", "").replace("$", "").strip()
    # Projektdateien verwenden deutsche Dezimal-Kommas.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return None


def fmt_num(value, decimals: int = 2):
    if value is None or not math.isfinite(float(value)):
        return ""
    return round(float(value), decimals)


def steuerungsart(ideen_quelle: str) -> str:
    # "Zielorientiert" ist bewusst KEINE eigene Steuerungsart mehr.
    # Langfrist = Buy & Hold; alle aktiven Setup-Quellen = Aktiver Trade.
    return "Buy & Hold" if str(ideen_quelle or "").strip().lower() == "langfrist" else "Aktiver Trade"


def read_positions(path: str) -> pd.DataFrame:
    # Bewusst exakt das bestehende Projektformat: Semikolon + deutsches Dezimal-Komma.
    df = pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    if "Status" not in df.columns or "Ticker" not in df.columns:
        raise ValueError("Offene_Positionen.csv hat nicht das erwartete Positionsschema.")
    df.columns = [str(c).strip() for c in df.columns]
    df["Status"] = df["Status"].astype(str).str.strip()
    return df


def is_open(row) -> bool:
    return str(row.get("Status", "")).strip().lower() == "offen"


def clean_history(hist: pd.DataFrame) -> pd.DataFrame:
    if hist is None or hist.empty:
        return pd.DataFrame()
    data = hist.copy()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    required = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    if "Close" not in required or "High" not in required or "Low" not in required:
        return pd.DataFrame()
    data = data.dropna(subset=["Close", "High", "Low"]).copy()
    if data.empty:
        return data
    data.index = pd.to_datetime(data.index)
    data = data[~data.index.duplicated(keep="last")].sort_index()
    return data


def fetch_history(ticker: str) -> pd.DataFrame:
    try:
        hist = yf.Ticker(ticker).history(period="max", auto_adjust=False, actions=False)
        return clean_history(hist)
    except Exception as exc:
        print(f"WARNUNG: {ticker}: Kursdaten nicht abrufbar: {exc}")
        return pd.DataFrame()


def last_completed_close(data: pd.DataFrame) -> Optional[float]:
    if data.empty:
        return None
    return parse_number(data["Close"].iloc[-1])


def cluster_levels(levels: Iterable[float], tolerance: float = 0.015) -> list[float]:
    vals = sorted({round(float(x), 8) for x in levels if x is not None and math.isfinite(float(x)) and float(x) > 0})
    if not vals:
        return []
    clusters: list[list[float]] = [[vals[0]]]
    for val in vals[1:]:
        center = float(np.mean(clusters[-1]))
        if abs(val - center) / center <= tolerance:
            clusters[-1].append(val)
        else:
            clusters.append([val])
    return [float(np.mean(c)) for c in clusters]


def swing_levels(data: pd.DataFrame, order: int = 5) -> tuple[list[float], list[float]]:
    recent = data.iloc[-252:].copy()
    if len(recent) < max(30, order * 3):
        return [], []
    highs = recent["High"].to_numpy(dtype=float)
    lows = recent["Low"].to_numpy(dtype=float)
    hi_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    lo_idx = argrelextrema(lows, np.less_equal, order=order)[0]
    return [float(highs[i]) for i in hi_idx], [float(lows[i]) for i in lo_idx]


def find_nearest(levels: Iterable[float], close: float, above: bool, count: int = 2) -> list[float]:
    vals = [float(x) for x in levels if x is not None]
    if above:
        vals = [x for x in vals if x > close * 1.002]
        vals.sort()
    else:
        vals = [x for x in vals if x < close * 0.998]
        vals.sort(reverse=True)
    return vals[:count]


def detect_abc(data: pd.DataFrame, close: float, direction: str) -> tuple[bool, str, Optional[tuple[float, float]], Optional[tuple[float, float, float]]]:
    """A-B-C nur fuer eine klar erkennbare letzte Swing-Struktur.

    Long: A = Swing-Low, B = nachfolgendes Swing-High, C = hoeheres Swing-Low.
    Fuer eine aktive Extension wird zusaetzlich verlangt, dass der aktuelle
    Schlusskurs B ueberwunden hat. Ein einfacher Breakout allein reicht nicht.
    Short wird spiegelbildlich behandelt.
    """
    if len(data) < 60:
        return False, "Zu wenig Daten fuer A-B-C", None, None
    d = data.iloc[-180:].copy()
    h = d["High"].to_numpy(dtype=float)
    l = d["Low"].to_numpy(dtype=float)
    order = 5
    hi_idx = list(argrelextrema(h, np.greater_equal, order=order)[0])
    lo_idx = list(argrelextrema(l, np.less_equal, order=order)[0])
    pivots = [(i, "H", float(h[i])) for i in hi_idx] + [(i, "L", float(l[i])) for i in lo_idx]
    pivots.sort(key=lambda x: x[0])
    # Komprimiere auf alternierende Pivottypen, behalte den extremeren Punkt.
    alt = []
    for p in pivots:
        if alt and alt[-1][1] == p[1]:
            if (p[2] > alt[-1][2]) if p[1] == "H" else (p[2] < alt[-1][2]):
                alt[-1] = p
        else:
            alt.append(p)
    # Immer die JÜNGSTE qualifizierte A-B-C-Struktur verwenden.
    # Wichtig: Eine ältere bereits bestätigte Struktur darf eine jüngere,
    # noch nicht bestätigte Struktur niemals überstimmen (Look-ahead-/
    # Zustandsfehler). Deshalb rückwärts durch die Kandidaten suchen und
    # beim ersten qualifizierten Kandidaten stoppen.
    if direction == "bullisch":
        for i in range(len(alt) - 3, -1, -1):
            a, b, c = alt[i:i+3]
            if (a[1], b[1], c[1]) == ("L", "H", "L") and c[2] > a[2] and b[2] > a[2]:
                # C muss abgeschlossen sein und der Kurs darf noch nicht unter C liegen.
                if c[0] < len(d) - 1 and close >= c[2] * 0.995:
                    confirmed = close > b[2] * 1.002
                    status = "Bestätigt" if confirmed else "Struktur vorhanden – noch kein A-B-C-Breakout"
                    return confirmed, status, (a[2], b[2]), (a[2], b[2], c[2])
    elif direction == "baerisch":
        for i in range(len(alt) - 3, -1, -1):
            a, b, c = alt[i:i+3]
            if (a[1], b[1], c[1]) == ("H", "L", "H") and c[2] < a[2] and b[2] < a[2]:
                if c[0] < len(d) - 1 and close <= c[2] * 1.005:
                    confirmed = close < b[2] * 0.998
                    status = "Bestätigt" if confirmed else "Struktur vorhanden – noch kein A-B-C-Breakdown"
                    return confirmed, status, (a[2], b[2]), (a[2], b[2], c[2])
    return False, "Keine qualifizierte A-B-C-Struktur", None, None


def fibonacci_extension(data: pd.DataFrame, abc: tuple[float, float, float], direction: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """A-B-C-Extension: 127,2 / 161,8 / 261,8 Prozent des AB-Impulses."""
    a, b, c = abc
    if direction == "bullisch":
        move = b - a
        if move <= 0:
            return None, None, None
        return c + move * 1.272, c + move * 1.618, c + move * 2.618
    if direction == "baerisch":
        move = a - b
        if move <= 0:
            return None, None, None
        return c - move * 1.272, c - move * 1.618, c - move * 2.618
    return None, None, None


def trend_channel_upper(data: pd.DataFrame, close: float) -> Optional[float]:
    """Robuste obere Trendkanal-Projektion aus mindestens drei Swing-Hochs.
    Es wird bewusst nur projiziert, wenn die Hochpunkte eine steigende Geometrie
    bilden; bei zu wenig/instabilen Punkten bleibt das Feld leer."""
    d = data.iloc[-180:].copy()
    if len(d) < 60:
        return None
    hi = d["High"].to_numpy(float)
    idx = argrelextrema(hi, np.greater_equal, order=5)[0]
    if len(idx) < 3:
        return None
    idx = idx[-6:]
    y = hi[idx]
    x = idx.astype(float)
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        return None
    fitted = slope * x + intercept
    if np.max(np.abs(y - fitted) / np.maximum(y, 1e-9)) > 0.04:
        return None
    proj = slope * (len(d) - 1) + intercept
    return float(proj) if proj > close * 1.005 else None


def detect_formation(data: pd.DataFrame, close: float, direction: str) -> tuple[str, Optional[float]]:
    """Konservative Measured-Move-Erkennung für Range/Dreieck/Flaggen-ähnliche
    Konsolidierungen. Keine Formation wird erzwungen."""
    d = data.iloc[-80:].copy()
    if len(d) < 40:
        return "Keine belastbare Formation", None
    hi = float(d["High"].max()); lo = float(d["Low"].min())
    width = hi - lo
    if width <= 0 or close <= 0:
        return "Keine belastbare Formation", None
    recent = d.iloc[-20:]
    recent_width = float(recent["High"].max() - recent["Low"].min())
    # Kompakte Konsolidierung nach vorheriger Bewegung -> konservativer Range measured move.
    if recent_width <= width * 0.55:
        prior = d.iloc[:-20]
        if len(prior) >= 15:
            ph = float(prior["High"].max()); pl = float(prior["Low"].min())
            impulse = ph - pl
            if impulse > close * 0.08:
                if direction == "bullisch" and close >= float(recent["Low"].min()):
                    return "Konsolidierung / Flaggen-ähnlich", float(close + impulse)
                if direction == "baerisch":
                    return "Konsolidierung / Flaggen-ähnlich", float(close - impulse)
    # Dreieck: schrumpfende Bandbreite in der zweiten Hälfte.
    first = d.iloc[:40]; second = d.iloc[-40:]
    w1 = float(first["High"].max() - first["Low"].min())
    w2 = float(second["High"].max() - second["Low"].min())
    if w1 > 0 and w2 < w1 * 0.65:
        return "Dreieck/Konsolidierung", float(close + w1 if direction == "bullisch" else close - w1)
    return "Keine belastbare Formation", None


def round_number_zone(close: float) -> Optional[float]:
    if close <= 0:
        return None
    # Psychologische Marken passend zur Größenordnung des Instruments.
    step = 0.01 if close < 1 else 0.05 if close < 10 else 1 if close < 100 else 5 if close < 1000 else 50
    candidate = round(close / step) * step
    if abs(candidate - close) / close <= 0.025:
        return float(candidate)
    return None


def overextension_signal(data: pd.DataFrame, close: float, ema200: Optional[float]) -> str:
    if ema200 is None or ema200 <= 0:
        return "Nicht bestimmbar – GD200 fehlt"
    dist = abs(close - ema200) / ema200 * 100
    bb_mid = data["Close"].rolling(20).mean().iloc[-1]
    bb_std = data["Close"].rolling(20).std(ddof=0).iloc[-1]
    if pd.notna(bb_mid) and pd.notna(bb_std) and bb_std > 0:
        z = abs(close - bb_mid) / (2 * bb_std)
        if dist >= 25 or z >= 1.8:
            return f"Hoch – GD200-Abstand {dist:.1f}% / BB-Ausdehnung {z:.1f}x"
        if dist >= 15 or z >= 1.3:
            return f"Moderat – GD200-Abstand {dist:.1f}% / BB-Ausdehnung {z:.1f}x"
    if dist >= 15:
        return f"Moderat – GD200-Abstand {dist:.1f}%"
    return f"Keine markante Überdehnung – GD200-Abstand {dist:.1f}%"


_SECTOR_MAP_CACHE = None

def _load_sector_maps() -> tuple[dict, dict, dict, dict]:
    """Liest die bereits im Projekt vorhandenen Sektor-Mappings aus analyse.py,
    ohne analyse.py zu importieren/auszuführen. Damit bleibt die Check-Datei
    unabhängig von API-Keys und übernimmt trotzdem die Projektquelle der Wahrheit."""
    global _SECTOR_MAP_CACHE
    if _SECTOR_MAP_CACHE is not None:
        return _SECTOR_MAP_CACHE
    maps = ({}, {}, {}, {})
    path = os.path.join(os.path.dirname(__file__), "analyse.py")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        names = {"sektoren_map": 0, "sektoren_aktien": 1, "eu_sektoren_etf": 2, "dax_aktien": 3}
        values = [None] * 4
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                idx = names.get(node.targets[0].id)
                if idx is not None:
                    try:
                        values[idx] = ast.literal_eval(node.value)
                    except Exception:
                        pass
        maps = tuple(v if isinstance(v, dict) else {} for v in values)
    except Exception:
        pass
    _SECTOR_MAP_CACHE = maps
    return maps


def _sector_etf_for_row(row) -> tuple[Optional[str], Optional[str]]:
    ticker = str(row.get("Ticker", "")).strip()
    sector = str(row.get("Sektor", "")).strip()
    sector_map, us_stocks, eu_etfs, eu_stocks = _load_sector_maps()
    if "." in ticker:
        for sec, tickers in eu_stocks.items():
            if ticker in tickers:
                for etf, etf_sector in eu_etfs.items():
                    if etf_sector == sec:
                        return etf, sec
    else:
        for etf, tickers in us_stocks.items():
            if ticker in tickers:
                return etf, sector_map.get(etf, sector)

    # Robuster Fallback: Wenn der Ticker noch nicht explizit im Projekt-Mapping
    # steht, verwenden wir das bereits in Offene_Positionen.csv vorhandene
    # Sektor-Feld und suchen dazu den passenden Sektor-ETF. Dadurch gehen
    # vorhandene Sektordaten nicht verloren, nur weil ein neuer Titel noch
    # nicht in der Ticker-Liste des Scanners hinterlegt wurde.
    if sector:
        for etf, etf_sector in eu_etfs.items():
            if str(etf_sector).strip().lower() == sector.lower():
                return etf, sector
        for etf, mapped_sector in sector_map.items():
            if str(mapped_sector).strip().lower() == sector.lower():
                return etf, sector

    return None, sector or None


def sector_relative_strength(row, close: float, data: pd.DataFrame) -> str:
    """Berechnet die bestehende Projektdefinition der Sektor-RS: 5T-Aktie
    gegen den passenden Sektor-ETF. Keine neue Rotationslogik, sondern die
    bereits vorhandene Projektlogik als Kontextsignal."""
    if row is None or data.empty or len(data) < 6:
        return "Nicht verfügbar – zu wenig Daten"
    etf, sector = _sector_etf_for_row(row)
    if not etf:
        return "Nicht verfügbar – kein Sektor-ETF im Projekt-Mapping"
    try:
        stock_5d = (float(data["Close"].iloc[-1]) / float(data["Close"].iloc[-6]) - 1) * 100
        etf_data = fetch_history(etf)
        if etf_data.empty or len(etf_data) < 6:
            return f"Nicht verfügbar – {etf}: keine ausreichenden ETF-Daten"
        etf_5d = (float(etf_data["Close"].iloc[-1]) / float(etf_data["Close"].iloc[-6]) - 1) * 100
        diff = stock_5d - etf_5d
        if diff >= 3:
            label = "Stark positiv"
        elif diff > 0:
            label = "Positiv"
        elif diff <= -3:
            label = "Negativ"
        else:
            label = "Leicht negativ"
        return f"{label} ({diff:+.1f} %-Pkt.; Aktie {stock_5d:+.1f}% vs. {sector or 'Sektor'} {etf_5d:+.1f}%)"
    except Exception as exc:
        return f"Nicht verfügbar – Berechnung fehlgeschlagen ({type(exc).__name__})"

def determine_state(data: pd.DataFrame, close: float, resistances: list[float], supports: list[float], abc_status: str) -> tuple[str, str, str]:
    ema20 = data["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = data["Close"].ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = data["Close"].ewm(span=200, adjust=False).mean().iloc[-1] if len(data) >= 200 else None
    ema50_prev = data["Close"].ewm(span=50, adjust=False).mean().iloc[-6] if len(data) >= 205 else data["Close"].ewm(span=50, adjust=False).mean().iloc[max(0, len(data)-6)]
    slope_up = ema50 > ema50_prev
    slope_down = ema50 < ema50_prev
    if ema200 is not None and close > ema50 > ema200 and slope_up:
        trend = "Bullisch"
    elif ema200 is not None and close < ema50 < ema200 and slope_down:
        trend = "Baerisch"
    elif close > ema50 and slope_up:
        trend = "Leicht bullisch"
    elif close < ema50 and slope_down:
        trend = "Leicht baerisch"
    else:
        trend = "Seitwaerts/neutral"

    if resistances and close >= resistances[0] * 0.995:
        state = "Widerstandstest"
        # bestätigter Breakout = Schluss über der nächsten markanten Zone.
        if close > resistances[0] * 1.002:
            state = "Breakout / Aufwaertszustand"
    elif supports and close <= supports[0] * 1.005:
        state = "Supporttest"
    elif trend in {"Bullisch", "Leicht bullisch"}:
        state = "Aufwaertstrend"
    elif trend in {"Baerisch", "Leicht baerisch"}:
        state = "Abwaertstrend"
    else:
        state = "Seitwaerts/neutral"

    if abc_status == "Bestätigt":
        state += " + A-B-C bestätigt"
    lage = f"{state}; Trend={trend}"
    return trend, state, lage


def immediate_resistance_within_10pct(future_highs: Iterable[float], close: float) -> Optional[float]:
    """Naechster historischer Widerstand <= 10 % oberhalb des Kurses.
    Genau 10,0 % blockiert Fibonacci; >10 % ist ein entferntes Major-Level.
    """
    if close is None or close <= 0:
        return None
    upper = close * (1.0 + IMMEDIATE_RESISTANCE_MAX_DISTANCE)
    candidates = [
        float(level) for level in future_highs
        if level > close * 1.002
        and level <= upper + max(1e-12, abs(upper) * 1e-10)
    ]
    return min(candidates) if candidates else None


def analyze_technical(data: pd.DataFrame, row=None) -> TechnicalResult:
    result = TechnicalResult()
    if data.empty:
        result.data_quality = "Keine Kursdaten"
        result.note = "Technische Analyse nicht möglich; keine belastbare Kursreihe verfügbar."
        return result
    if len(data) < 60:
        result.data_quality = f"Zu wenig Kursdaten ({len(data)} Handelstage)"
        result.close = last_completed_close(data)
        result.note = "Für eine vollständige technische Zustandsanalyse sind mindestens 60 Handelstage erforderlich."
        return result

    close = last_completed_close(data)
    result.close = close
    if close is None or close <= 0:
        result.data_quality = "Ungültiger Schlusskurs"
        return result

    closes = data["Close"].astype(float)
    result.ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    result.ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
    result.ema200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1]) if len(data) >= 200 else None
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = gain / loss.clip(lower=1e-9)
    result.rsi = float((100 - 100/(1+rs)).iloc[-1])

    swing_hi, swing_lo = swing_levels(data)
    # Zwei getrennte Ebenen: aktuelle Zonen oberhalb des Kurses UND die
    # zuletzt gebrochene Widerstandszone. Dadurch kann ein echter Breakout
    # erkannt werden, ohne den gebrochenen Widerstand aus dem Datenmodell
    # zu verlieren.
    prior_highs = [x for x in swing_hi if x < close * 1.002]
    future_highs = [x for x in swing_hi if x > close * 1.002]
    prior_resistance = max(prior_highs) if prior_highs else None
    supports = cluster_levels(find_nearest(swing_lo, close, above=False, count=8))
    resistances = cluster_levels(find_nearest(future_highs, close, above=True, count=8))
    # Direkte EMA-Supports werden nur aufgenommen, wenn sie unter dem Kurs liegen.
    if result.ema20 < close * 0.998:
        supports.append(result.ema20)
    if result.ema50 < close * 0.998:
        supports.append(result.ema50)
    supports = sorted(cluster_levels(supports), reverse=True)
    resistances = sorted(cluster_levels(resistances))

    # 52W-Hoch ist immer ein Major-Kandidat, wenn es oberhalb des Kurses liegt.
    high_52w = float(data["High"].iloc[-252:].max())
    major = high_52w if high_52w > close * 1.01 else None
    # Für lange Historien: echtes historisches Hoch zusätzlich prüfen. Beim Gold-
    # Spot ist das entscheidend, damit das bekannte ATH nicht verloren geht.
    all_time_high = float(data["High"].max())
    if all_time_high > close * 1.01:
        major = max(major or 0, all_time_high)

    # Vorläufige Trendrichtung fuer A-B-C.
    ema50_prev = float(closes.ewm(span=50, adjust=False).mean().iloc[-6])
    if result.ema200 is not None and close > result.ema50 > result.ema200 and result.ema50 > ema50_prev:
        direction = "bullisch"
    elif result.ema200 is not None and close < result.ema50 < result.ema200 and result.ema50 < ema50_prev:
        direction = "baerisch"
    elif close >= result.ema50:
        direction = "bullisch"
    else:
        direction = "baerisch"

    abc_ok, abc_status, abc_range, abc_points = detect_abc(data, close, direction)
    result.abc_status = abc_status

    # V2 Zielzonen: Fibonacci wird NUR nach Breakout + bestätigter A-B-C-Struktur
    # aktiviert. Ein Breakout allein reicht ausdrücklich nicht.
    # Ein bestätigtes A-B-C enthält bereits den strukturellen Break über B.
    # Falls kein historischer Widerstand oberhalb vorhanden ist, darf dieser
    # Fall ausdrücklich NICHT Fibonacci blockieren. Existiert ein relevanter
    # historischer Widerstand, muss dieser zusätzlich überwunden sein.
    # Fibonacci wird NUR nach bestaetigtem A-B-C + strukturellem Breakout
    # aktiviert. Ein weiter oben liegendes Major-Level/ATH blockiert Fibonacci
    # NICHT: es bleibt als Major Resistance separat bestehen. Damit bleiben
    # unsere beiden Faelle konsistent:
    #   Fall A: keine unmittelbare historische Resistance oberhalb ->
    #           bestaetigtes A-B-C reicht fuer Fibonacci.
    #   Fall B: eine unmittelbare historische Resistance wurde gerade getestet
    #           bzw. gebrochen -> erst deren Breakout muss bestaetigt sein;
    #           danach darf Fibonacci aktiv werden, auch wenn ein weiter
    #           entferntes Major-Level/ATH noch oberhalb liegt.
    # `abc_ok` bestaetigt bereits den Break ueber B. `prior_resistance` ist die
    # letzte markante Widerstandszone VOR dem aktuellen Kurs und wird deshalb
    # zusaetzlich abgesichert. `resistances` oberhalb bleiben Ziel-/Kontextzonen,
    # blockieren Fibonacci aber nicht.
    # Fall A: Keine unmittelbare historische Resistance oberhalb -> ein
    # bestaetigtes A-B-C mit B-Bruch reicht fuer Fibonacci.
    # Fall B: Eine unmittelbare historische Resistance oberhalb des Kurses
    # existiert noch -> sie muss zuerst gebrochen sein. Weiter entfernte
    # Major-Level/ATH (>10 %) blockieren Fibonacci nicht.
    immediate_resistance = immediate_resistance_within_10pct(future_highs, close)

    breakout_for_fib = bool(
        abc_ok and abc_points and immediate_resistance is None
    )
    if breakout_for_fib:
        result.fib1, result.fib2, result.fib3 = fibonacci_extension(data, abc_points, direction)
        result.fib_status = "Aktiv – Breakout + A-B-C bestätigt"
    elif abc_ok and abc_points and immediate_resistance is not None:
        result.fib_status = (
            f"Nicht aktiv – unmittelbarer historischer Widerstand "
            f"bei {immediate_resistance:.2f} noch nicht gebrochen"
        )
    elif abc_ok and abc_points:
        result.fib_status = "Nicht aktiv – A-B-C-Breakout nicht bestätigt"
    else:
        result.fib_status = "Nicht aktiv – bestätigte A-B-C-Struktur fehlt"

    # V2 parallel: dynamische Referenzen und Kontextsignale werden unabhängig
    # von Fibonacci berechnet. Sie sind keine automatischen Kursziele, wenn die
    # Datenqualität/Struktur sie nicht belastbar hergibt.
    result.channel_upper = trend_channel_upper(data, close)
    result.formation, result.measured_move = detect_formation(data, close, direction)
    result.round_number = round_number_zone(close)
    result.overextension = overextension_signal(data, close, result.ema200)
    result.sector_rs = sector_relative_strength(row, close, data)

    result.support1 = supports[0] if len(supports) > 0 else None
    result.support2 = supports[1] if len(supports) > 1 else None
    result.resistance1 = resistances[0] if len(resistances) > 0 else None
    result.resistance2 = resistances[1] if len(resistances) > 1 else None

    # Breakout: gegen die letzte markante Widerstandszone VOR dem aktuellen
    # Kurs pruefen. Nach dem Breakout wird genau diese Zone als Retest-Support
    # erhalten. Ein Widerstand oberhalb des aktuellen Kurses bleibt dagegen
    # ein kommendes Ziel und ist kein bereits bestaetigter Breakout.
    if prior_resistance is not None and close > prior_resistance * 1.002:
        result.breakout_status = "Bestätigter Breakout"
        result.retest_support = prior_resistance
    elif prior_resistance is not None and close >= prior_resistance * 0.995:
        result.breakout_status = "Widerstand wird getestet"
    else:
        result.breakout_status = "Kein bestätigter Breakout"

    state_resistances = resistances if resistances else ([prior_resistance] if prior_resistance is not None else [])
    result.trend, result.state, _ = determine_state(data, close, state_resistances, supports, result.abc_status)
    if result.breakout_status == "Bestätigter Breakout":
        result.state = "Breakout / Aufwaertszustand" + (" + A-B-C bestätigt" if result.abc_status == "Bestätigt" else "")

    # Major-Level vor der Konfluenz festlegen. Das historische ATH/52W-Hoch bleibt
    # unabhängig erhalten und darf nie durch eine Fibonacci-Projektion überschrieben werden.
    result.major_resistance = major

    # Konfluenzanalyse: mehrere unabhängige Referenzen in einer Zone bündeln.
    # Fibonacci ist dabei nur aktiv, wenn Breakout + A-B-C bestätigt wurden.
    refs = []
    for label, value in [
        ("Fibonacci 127,2%", result.fib1),
        ("Fibonacci 161,8%", result.fib2),
        ("Fibonacci 261,8%", result.fib3),
        ("Trendkanal", result.channel_upper),
        ("Measured Move", result.measured_move),
        ("Round Number", result.round_number),
        ("Major", result.major_resistance),
    ]:
        if value is not None and value > close:
            refs.append((label, float(value)))

    groups = []
    for label, value in refs:
        placed = False
        for group in groups:
            center = float(np.mean([v for _, v in group]))
            if abs(value - center) / center <= 0.02:
                group.append((label, value))
                placed = True
                break
        if not placed:
            groups.append([(label, value)])

    strong = [g for g in groups if len(g) >= 2]
    if strong:
        g = max(strong, key=len)
        lo = min(v for _, v in g)
        hi = max(v for _, v in g)
        result.confluence = (f"{lo:.2f}-{hi:.2f}: " +
                             ", ".join(label for label, _ in g))
    elif refs:
        result.confluence = "Keine Mehrfach-Konfluenz; Referenzen: " + ", ".join(label for label, _ in refs[:6])

    # Primärlogik der Zielzone:
    # 1) historische Widerstände oberhalb haben Vorrang, wenn vorhanden;
    # 2) fehlt ein relevanter historischer Widerstand, werden aktive Fibonacci-
    #    Extensions als primäre Projektion verwendet;
    # 3) parallel bleiben Kanal, Formation und Round Number als Bestätigung;
    # 4) Major-Level bleibt immer separat.
    if resistances:
        primary = [(v, "Historischer Widerstand") for v in resistances if v > close]
    else:
        primary = []

    if not primary and result.fib_status.startswith("Aktiv"):
        primary = [(v, label) for v, label in [
            (result.fib1, "Fibonacci 127,2%"),
            (result.fib2, "Fibonacci 161,8%"),
            (result.fib3, "Fibonacci 261,8%"),
        ] if v is not None and v > close]

    # Konfluenzzone hat Vorrang vor einer einzelnen Referenz, sofern sie nicht
    # ausschließlich aus dem Major-Level besteht.
    if strong:
        g = min(strong, key=lambda group: min(v for _, v in group))
        non_major = [(label, value) for label, value in g if label != "Major"]
        if len(non_major) >= 2:
            lo = min(v for _, v in non_major)
            hi = max(v for _, v in non_major)
            result.note = f"Konfluenzzone {lo:.2f}-{hi:.2f}: " + ", ".join(label for label, _ in non_major)
        elif primary:
            value, label = min(primary, key=lambda x: x[0])
            result.note = f"Nächste technische Referenz: {value:.2f} ({label})."
    elif primary:
        value, label = min(primary, key=lambda x: x[0])
        result.note = f"Nächste technische Referenz: {value:.2f} ({label})."
    else:
        # Kein historischer Widerstand: Kanal/Formation/Round Number dienen als
        # sekundäre Referenzen. Das ist ausdrücklich kein erzwungenes Kursziel.
        secondary = [(v, label) for v, label in [
            (result.channel_upper, "Trendkanal"),
            (result.measured_move, "Measured Move"),
            (result.round_number, "Round Number"),
        ] if v is not None and v > close]
        if secondary:
            value, label = min(secondary, key=lambda x: x[0])
            result.note = f"Keine relevante historische Resistance oberhalb; nächste Referenz: {value:.2f} ({label})."
        else:
            result.note = "Keine belastbare Zielreferenz oberhalb des aktuellen Kurses."

    quality = "hoch" if len(data) >= 200 else "mittel"
    # yfinance liefert einen DatetimeIndex; Offline-/Testreihen können jedoch
    # auch einen numerischen Index haben. Die Datenqualitaetsmeldung darf
    # deshalb niemals am Index-Typ scheitern.
    try:
        last_date = pd.Timestamp(data.index[-1]).date().isoformat()
    except Exception:
        last_date = str(data.index[-1])
    result.data_quality = f"{quality}; {len(data)} Handelstage; letzter Schluss {last_date}"
    # Der bereits berechnete Zielzonen-/Konfluenzhinweis bleibt erhalten.
    # Nur wenn noch kein spezifischer Hinweis gesetzt wurde, verwenden wir
    # die allgemeine Regelmeldung.
    if not result.note:
        result.note = (
            "Breakout aktiviert Fibonacci nicht automatisch. "
            "Fibonacci nur bei bestätigter A-B-C-Struktur. "
            "Major-Level bleibt separat erhalten."
        )
    return result


def performance(entry: Optional[float], close: Optional[float], direction: str) -> Optional[float]:
    if entry is None or close is None or entry == 0:
        return None
    raw = (close - entry) / entry * 100
    return -raw if str(direction).strip().lower() == "short" else raw


def make_row(row, tech: TechnicalResult) -> dict:
    ticker = str(row.get("Ticker", "")).strip()
    name = str(row.get("Name", "")).strip() or ticker
    entry = parse_number(row.get("Einstieg"))
    close = tech.close
    perf = performance(entry, close, str(row.get("Richtung", "Long")))
    if tech.close is None:
        quality = tech.data_quality
    else:
        quality = tech.data_quality

    sector = str(row.get("Sektor", "")).strip()
    if ticker.upper() == "PPFD.SG":
        sector = "Edelmetalle / Silber"
    elif ticker.upper() == GOLD_SPOT_TICKER:
        sector = "Edelmetalle / Gold"

    target_zone = ""
    # Die Zielzone folgt der bereits berechneten V2-Hierarchie. Die Konfluenz
    # darf nur dann die primäre Darstellung übernehmen, wenn mindestens zwei
    # unabhängige Referenzen zusammenfallen; ein Major-Level allein ist keine
    # Konfluenz.
    if tech.confluence and "Keine Mehrfach-Konfluenz" not in tech.confluence and "Keine" not in tech.confluence:
        target_zone = tech.confluence
    elif tech.resistance1 is not None and tech.close is not None and tech.resistance1 > tech.close:
        target_zone = f"{tech.resistance1:.2f} (Historischer Widerstand)"
    elif tech.fib_status.startswith("Aktiv"):
        fibs = [(v, label) for v, label in [
            (tech.fib1, "Fibonacci 127,2%"), (tech.fib2, "Fibonacci 161,8%"),
            (tech.fib3, "Fibonacci 261,8%"),
        ] if v is not None and tech.close is not None and v > tech.close]
        if fibs:
            value, label = min(fibs, key=lambda x: x[0])
            target_zone = f"{value:.2f} ({label})"
    else:
        secondary = [(v, label) for v, label in [
            (tech.channel_upper, "Trendkanal"), (tech.measured_move, "Measured Move"),
            (tech.round_number, "Round Number"),
        ] if v is not None and tech.close is not None and v > tech.close]
        if secondary:
            value, label = min(secondary, key=lambda x: x[0])
            target_zone = f"{value:.2f} ({label})"
    if tech.major_resistance is not None:
        target_zone = (target_zone + " | Major Resistance " + f"{tech.major_resistance:.2f}").strip(" |") if target_zone else f"Major Resistance {tech.major_resistance:.2f}"
    return {
        "Ticker": ticker,
        "Name": name,
        "Steuerungsart": steuerungsart(row.get("Ideen_Quelle", "")),
        "Sektor": sector,
        "Markt": str(row.get("Markt", "")).strip(),
        "Waehrung": str(row.get("Waehrung", "")).strip(),
        "Status": "Offen",
        "Einstieg": fmt_num(entry),
        "Aktueller_Kurs": fmt_num(close),
        "Performance_Seit_Einstieg%": fmt_num(perf),
        "Technischer_Zustand": tech.state,
        "Trendrichtung": tech.trend,
        "Technische_Lage": tech.state,
        "Support_1": fmt_num(tech.support1),
        "Support_2": fmt_num(tech.support2),
        "Widerstand_1": fmt_num(tech.resistance1),
        "Widerstand_2": fmt_num(tech.resistance2),
        "Breakout_Status": tech.breakout_status,
        "A-B-C_Status": tech.abc_status,
        "Fibonacci_Status": tech.fib_status,
        "Fibonacci_Ziel_1": fmt_num(tech.fib1),
        "Fibonacci_Ziel_2": fmt_num(tech.fib2),
        "Fibonacci_Ziel_3": fmt_num(tech.fib3),
        "Trendkanal_Obergrenze": fmt_num(tech.channel_upper),
        "Measured_Move_Ziel": fmt_num(tech.measured_move),
        "Formation": tech.formation,
        "Round_Number_Zone": fmt_num(tech.round_number),
        "Major_Resistance": fmt_num(tech.major_resistance),
        "Ueberdehnung": tech.overextension,
        "Relative_Staerke_Sektor": tech.sector_rs,
        "Konfluenz": tech.confluence,
        "Retest_Support": fmt_num(tech.retest_support),
        "Technische_Zielzone": target_zone,
        "Datenqualitaet": quality,
        "Analysehinweis": tech.note,
    }


def extract_closed_history(df: pd.DataFrame) -> pd.DataFrame:
    """Uebernimmt Gestoppt/Verkauft als historische Faktenbasis.
    Keine aktuelle technische Neuberechnung fuer geschlossene Positionen.
    """
    closed = df[df["Status"].astype(str).str.strip().str.lower().isin({"gestoppt", "verkauft"})].copy()
    for col in HISTORY_HEADERS:
        if col not in closed.columns:
            closed[col] = ""
    return closed[HISTORY_HEADERS].copy()


def run_local(input_file: str = INPUT_FILE, output_csv: str = OUTPUT_CSV,
              history_csv: str = "Geschlossene Positionen.csv"):
    df = read_positions(input_file)
    open_df = df[df.apply(is_open, axis=1)].copy()
    closed_df = extract_closed_history(df)
    results = []
    print(f"OFFENE POSITIONEN + CHECK: {len(open_df)} offene Positionen gefunden.")
    print(f"HISTORIE: {len(closed_df)} geschlossene Positionen uebernommen.")
    print("Gestoppte/verkaufte Positionen werden NICHT technisch neu berechnet.")

    for _, row in open_df.iterrows():
        ticker = str(row.get("Ticker", "")).strip()
        if not ticker or ticker.upper() == "ANLEITUNG":
            continue
        print(f"CHECK: {ticker} | {row.get('Name','')}")
        hist = fetch_history(ticker)
        tech = analyze_technical(hist, row)
        if not hist.empty and tech.close is not None:
            ath = float(hist["High"].max())
            if ath > tech.close * 1.01:
                tech.major_resistance = ath
        results.append(make_row(row, tech))

    out = pd.DataFrame(results, columns=HEADERS)
    out.to_csv(output_csv, sep=";", decimal=",", index=False, encoding="utf-8-sig")
    closed_df.to_csv(history_csv, sep=";", decimal=",", index=False, encoding="utf-8-sig")
    print(f"LOKAL ERSTELLT: {output_csv} | {len(out)} offene Positionen")
    print(f"HISTORIE ERSTELLT: {history_csv} | {len(closed_df)} geschlossene Positionen")
    return out, closed_df


def google_credentials():
    """Verwendet exakt den bestehenden Projekt-OAuth-Mechanismus.

    Der im GDRIVE_TOKEN gespeicherte Scope wird übernommen; es werden KEINE
    eigenen Scopes erzwungen. Der Token wird einmal geladen und fuer den
    gesamten Drive/Sheets-Lauf wiederverwendet.
    """
    if not GOOGLE_AVAILABLE:
        raise RuntimeError("Google-Credentials/Client-Bibliotheken sind nicht verfügbar.")

    token = os.getenv("GDRIVE_TOKEN")
    if not token:
        raise RuntimeError("GDRIVE_TOKEN fehlt.")

    try:
        info = json.loads(token)
        creds = Credentials.from_authorized_user_info(info)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid:
            raise RuntimeError("GDRIVE_TOKEN ist nach Laden/Refresh nicht gültig.")
        return creds
    except Exception as exc:
        raise RuntimeError(f"GDRIVE_TOKEN konnte nicht verwendet werden: {exc}") from exc

def _write_sheet(sheets, spreadsheet_id: str, title: str, values: list[list],
                 widths: dict[str, int], freeze_rows: int = 2,
                 target_title: Optional[str] = None):
    """Schreibt in einen Ziel- oder temporären Tab.

    Sicherheitsregel:
    - Für produktive Updates wird zunächst in einen temporären Tab geschrieben.
    - Der bestehende produktive Tab wird erst nach erfolgreichem vollständigem
      Schreiben/Formatieren per Batch-Rename ausgetauscht.
    - Schlägt das Schreiben vorher fehl, bleibt der bestehende Tab unverändert.
    """
    actual_title = target_title or title
    ss = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    props = {x["properties"]["title"]: x["properties"] for x in ss["sheets"]}

    if actual_title not in props:
        created = sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]}
        ).execute()
        sheet_id = created["replies"][0]["addSheet"]["properties"]["sheetId"]
    else:
        sheet_id = props[actual_title]["sheetId"]

    # Bei einem temporären Tab ist `title` der tatsächliche Tabname.
    write_title = title

    # Ein vorhandener Zieltab wird hier NICHT geleert. Nur der temporäre Tab
    # wird beschrieben. Das ist der zentrale Datenverlustschutz.
    if write_title != actual_title:
        if write_title in props:
            # Kollision mit einem alten temporären Rest -> neuen eindeutigen Namen
            # erzeugen; der alte Rest bleibt unangetastet.
            write_title = f"{title}_{dt.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
            created = sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": write_title}}}]}
            ).execute()
            sheet_id = created["replies"][0]["addSheet"]["properties"]["sheetId"]
        else:
            created = sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": write_title}}}]}
            ).execute()
            sheet_id = created["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Nur der temporäre Tab wird geleert; ein eventueller produktiver Tab bleibt
    # bis zum erfolgreichen Austausch unangetastet.
    sheets.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"'{write_title}'!A:AZ", body={}
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"'{write_title}'!A1",
        valueInputOption="USER_ENTERED", body={"values": values}
    ).execute()

    ncols = len(values[0])
    requests = [
        {"updateSheetProperties":{"properties":{"sheetId":sheet_id,"gridProperties":{"frozenRowCount":freeze_rows}},"fields":"gridProperties.frozenRowCount"}},
        {"mergeCells":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":ncols},"mergeType":"MERGE_ALL"}},
        {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":ncols},"cell":{"userEnteredFormat":{"textFormat":{"bold":True,"fontSize":14}}},"fields":"userEnteredFormat.textFormat"}},
        {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":2,"startColumnIndex":0,"endColumnIndex":ncols},"cell":{"userEnteredFormat":{"textFormat":{"bold":True},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat.textFormat,userEnteredFormat.wrapStrategy"}},
        {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"ROWS","startIndex":0,"endIndex":2},"properties":{"pixelSize":34},"fields":"pixelSize"}},
        {"setBasicFilter":{"filter":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":len(values),"startColumnIndex":0,"endColumnIndex":ncols}}}},
    ]
    for i,col in enumerate(values[1]):
        if col in NUMERIC_COLUMNS or col.endswith("%"):
            requests.append({"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":2,"endRowIndex":len(values),"startColumnIndex":i,"endColumnIndex":i+1},"cell":{"userEnteredFormat":{"numberFormat":{"type":"NUMBER","pattern":"0.00"}}},"fields":"userEnteredFormat.numberFormat"}})
        requests.append({"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":i,"endIndex":i+1},"properties":{"pixelSize":widths.get(col,120)},"fields":"pixelSize"}})
    sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests":requests}).execute()
    return sheet_id


def _swap_temp_tabs(sheets, spreadsheet_id: str, temp_to_target: dict[str, str], legacy_title: Optional[str] = None) -> list[int]:
    """Tauscht vollständig vorbereitete temporäre Tabs gegen Produktivtabs.

    Der Austausch erfolgt in EINEM batchUpdate. Bei einem Fehler vor diesem
    Punkt bleiben die bisherigen Produktivtabs unverändert.
    Die alten Tabs werden zunächst nur in Backup-Namen umbenannt; sie werden
    bewusst NICHT sofort gelöscht. So bleibt im Fehlerfall ein Wiederherstellungs-
    stand innerhalb desselben Google Sheets erhalten.
    """
    ss = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    props = {x["properties"]["title"]: x["properties"] for x in ss["sheets"]}
    requests = []
    old_ids = []

    # Falls der bisherige Haupttab noch einen alten Namen trägt, wird auch er
    # im selben atomaren Batch als Backup umbenannt. Dadurch bleibt bis zum
    # erfolgreichen Swap alles unverändert.
    if legacy_title and legacy_title in props:
        legacy_backup_title = f"_BACKUP_{legacy_title}_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": props[legacy_title]["sheetId"], "title": legacy_backup_title},
            "fields": "title"
        }})
        old_ids.append(props[legacy_title]["sheetId"])

    for temp_title, target_title in temp_to_target.items():
        temp = props.get(temp_title)
        if temp is None:
            raise RuntimeError(f"Temporärer Tab fehlt vor dem Austausch: {temp_title}")
        old = props.get(target_title)
        if old is not None:
            backup_title = f"_BACKUP_{target_title}_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": old["sheetId"], "title": backup_title},
                "fields": "title"
            }})
            old_ids.append(old["sheetId"])
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": temp["sheetId"], "title": target_title},
            "fields": "title"
        }})

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()
    return old_ids


def _cleanup_backups(sheets, spreadsheet_id: str):
    """Löscht alte Backup-Tabs erst NACH erfolgreichem Produktiv-Swap.

    Scheitert die Bereinigung, bleiben die Backups absichtlich erhalten.
    Datenverlust hat Vorrang vor einer kosmetisch perfekten Tab-Anzahl.
    """
    ss = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    backup_ids = [
        s["properties"]["sheetId"] for s in ss["sheets"]
        if str(s["properties"]["title"]).startswith("_BACKUP_")
    ]
    if not backup_ids:
        return
    requests = [{"deleteSheet": {"sheetId": sid}} for sid in backup_ids]
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def _validate_output_before_upload(df: pd.DataFrame, closed_df: pd.DataFrame):
    """Lokale Plausibilitätsprüfung als letzte Schranke vor Google-Änderungen."""
    if df is None or not isinstance(df, pd.DataFrame):
        raise RuntimeError("Offene Check-Ausgabe ist keine gültige Tabelle.")
    if closed_df is None or not isinstance(closed_df, pd.DataFrame):
        raise RuntimeError("Historien-Ausgabe ist keine gültige Tabelle.")
    if len(df) == 0:
        raise RuntimeError("Sicherheitsabbruch: 0 offene Positionen würden hochgeladen.")
    required_open = {"Ticker", "Status"}
    if not required_open.issubset(df.columns):
        raise RuntimeError("Sicherheitsabbruch: offene Check-Ausgabe hat Pflichtspalten nicht.")
    if not (df["Status"].astype(str).str.strip().str.lower() == "offen").all():
        raise RuntimeError("Sicherheitsabbruch: Tab 1 enthält eine Position, die nicht 'Offen' ist.")
    required_hist = {"Ticker", "Status"}
    if not required_hist.issubset(closed_df.columns):
        raise RuntimeError("Sicherheitsabbruch: Historien-Ausgabe hat Pflichtspalten nicht.")
    if closed_df["Ticker"].astype(str).str.strip().eq("").any():
        raise RuntimeError("Sicherheitsabbruch: Historie enthält einen leeren Ticker.")


def _snapshot_google_sheet(drive, spreadsheet_id: str) -> Optional[str]:
    """Erstellt vor dem produktiven Austausch eine dauerhafte Drive-Kopie.

    Die Kopie wird NICHT automatisch gelöscht. Sie dient als zusätzlicher
    Notfallstand, falls nach einem API-/Netzwerkfehler eine Wiederherstellung
    nötig wird.
    """
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    copied = drive.files().copy(
        fileId=spreadsheet_id,
        body={"name": f"{DRIVE_NAME} - Backup {stamp}", "parents": [FOLDER_ID]}
    ).execute()
    return copied.get("id")


def read_existing_history(sheets, spreadsheet_id: str) -> list[list]:
    """Liest Tab 2 vor dem Überschreiben. Fehlt der Tab, kommt eine leere Historie."""
    try:
        response = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="'Geschlossene Positionen'!A:AA",
        ).execute()
        return response.get("values", [])
    except Exception as exc:
        print(f"WARNUNG: Bestehende Historie konnte nicht gelesen werden: {exc}")
        return []


def upsert_google_sheet(df: pd.DataFrame, closed_df: pd.DataFrame, creds) -> Optional[str]:
    if creds is None:
        print("INFO: Keine Google-Credentials – lokale CSVs bleiben die Ausgabe.")
        return None

    _validate_output_before_upload(df, closed_df)

    drive=build("drive","v3",credentials=creds)
    sheets=build("sheets","v4",credentials=creds)
    q=f"name='{DRIVE_NAME}' and '{FOLDER_ID}' in parents and trashed=false"
    files=drive.files().list(q=q,fields="files(id,name,mimeType)").execute().get("files",[])
    if files:
        spreadsheet_id=files[0]["id"]
    else:
        created=drive.files().create(
            body={"name":DRIVE_NAME,"mimeType":"application/vnd.google-apps.spreadsheet","parents":[FOLDER_ID]},
            fields="id,name"
        ).execute()
        spreadsheet_id=created["id"]

    existing = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute().get("sheets", [])
    existing_titles = [s["properties"]["title"] for s in existing]

    # Bestehende Historie VOR jedem Überschreiben sichern.
    existing_history_rows = read_existing_history(sheets, spreadsheet_id)

    # Falls das alte Sheet einen anders benannten ersten Tab hat, wird dessen
    # Name erst nach erfolgreicher Vorbereitung der neuen Tabs geändert.
    old_main_title = None
    if "Offene Positionen + Check" not in existing_titles and existing:
        old_main_title = existing[0]["properties"]["title"]

    now=dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    values=[[f"Offene Positionen + Check | Stand {now}"]+[""]*(len(HEADERS)-1),HEADERS]
    for _,r in df.iterrows(): values.append([r.get(c,"") for c in HEADERS])
    widths = {
        "Ticker": 95, "Name": 240, "Steuerungsart": 125, "Sektor": 150,
        "Markt": 65, "Waehrung": 75, "Status": 75, "Einstieg": 85,
        "Aktueller_Kurs": 105, "Performance_Seit_Einstieg%": 120,
        "Technischer_Zustand": 190, "Trendrichtung": 125, "Technische_Lage": 220,
        "Support_1": 90, "Support_2": 90, "Widerstand_1": 100, "Widerstand_2": 100,
        "Breakout_Status": 180, "A-B-C_Status": 260, "Fibonacci_Status": 260,
        "Fibonacci_Ziel_1": 120, "Fibonacci_Ziel_2": 120,
        "Fibonacci_Ziel_3": 120, "Trendkanal_Obergrenze": 130,
        "Measured_Move_Ziel": 130, "Formation": 220, "Round_Number_Zone": 130,
        "Major_Resistance": 120, "Ueberdehnung": 300,
        "Relative_Staerke_Sektor": 300, "Konfluenz": 300, "Retest_Support": 120,
        "Technische_Zielzone": 220, "Datenqualitaet": 220, "Analysehinweis": 360,
    }

    merged_history = merge_closed_history(existing_history_rows, closed_df)
    hvalues=[[f"Geschlossene Positionen | historische Faktenbasis | Stand {now}"]+[""]*(len(HISTORY_HEADERS)-1),HISTORY_HEADERS]
    for _,r in merged_history.iterrows(): hvalues.append([r.get(c,"") for c in HISTORY_HEADERS])
    hwidths={c:140 for c in HISTORY_HEADERS}
    hwidths.update({"Ticker":95,"Name":260,"TP_Hinweis":260,"Alert_Hinweis":260})

    # Datenverlustschutz:
    # 1) bestehende Historie lesen
    # 2) beide neuen Tabs vollständig lokal/temporär schreiben
    # 3) produktiven Stand erst danach per Batch-Rename austauschen
    # 4) zusätzliche Drive-Sicherung des bisherigen Sheets vor dem Swap
    temp_suffix = f"__TMP_{dt.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    temp_open = f"__TMP_OFFEN_{temp_suffix}"
    temp_closed = f"__TMP_GESCHLOSSEN_{temp_suffix}"

    backup_id = None
    try:
        # Wenn das Sheet bereits existiert, vor dem ersten produktiven Austausch
        # eine separate Drive-Sicherung erstellen. Diese bleibt bewusst erhalten.
        if existing:
            backup_id = _snapshot_google_sheet(drive, spreadsheet_id)
            print(f"DATENSCHUTZ: Backup des bisherigen Google Sheets erstellt | ID={backup_id}")

        _write_sheet(sheets, spreadsheet_id, temp_open, values, widths, 2, target_title=temp_open)
        _write_sheet(sheets, spreadsheet_id, temp_closed, hvalues, hwidths, 2, target_title=temp_closed)

        # Beide neuen Tabs sind vollständig beschrieben und formatiert. Erst jetzt
        # werden sie gegen die produktiven Tabs getauscht.
        targets = {
            temp_open: "Offene Positionen + Check",
            temp_closed: "Geschlossene Positionen",
        }
        # Temporäre Namen stehen in props; deshalb direkter, atomarer Swap.
        _swap_temp_tabs(sheets, spreadsheet_id, targets, old_main_title)

        # Nur nach erfolgreichem Swap werden alte Backup-Tabs entfernt.
        # Scheitert die Bereinigung, bleiben sie als zusätzliche Sicherheitskopie.
        try:
            _cleanup_backups(sheets, spreadsheet_id)
        except Exception as cleanup_exc:
            print(f"WARNUNG: Backup-Tabs konnten nicht bereinigt werden; sie bleiben absichtlich erhalten: {cleanup_exc}")

        print(f"GOOGLE SHEET AKTUALISIERT: {DRIVE_NAME} | offene={len(df)} | historisch={len(merged_history)} | 2 produktive Tabs")
        return spreadsheet_id
    except Exception as exc:
        print("DATENSCHUTZ: Produktiver Swap NICHT erfolgreich abgeschlossen.")
        print("DATENSCHUTZ: Bestehende produktive Tabs wurden vor dem Swap nicht verändert.")
        if backup_id:
            print(f"DATENSCHUTZ: Notfall-Backup bleibt erhalten | ID={backup_id}")
        raise RuntimeError(f"Google-Upload sicher abgebrochen: {exc}") from exc

def main():
    output, closed_history = run_local(INPUT_FILE, OUTPUT_CSV)
    try:
        creds = google_credentials()
        upsert_google_sheet(output, closed_history, creds)
    except Exception as exc:
        print(f"FEHLER: Google Drive/Sheets konnte nicht aktualisiert werden: {exc}")
        print("ABBRUCH: Lokale Dateien erstellt, Google Sheet nicht erfolgreich aktualisiert.")
        raise
    print("GOOGLE DRIVE: Offene Positionen+Check erfolgreich erstellt/aktualisiert.")
    print(f"FERTIG: Offen={len(output)} | Historie={len(closed_history)} | Originaldatei unverändert.")


if __name__ == "__main__":
    main()
