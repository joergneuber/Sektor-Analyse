"""
short_scanner.py

Vierte, eigenstaendige Scanner-Kategorie: spiegelt die vier Setup-Muster
des Trendfolge-Scanners (analyse.py) als BAERISCHE Varianten fuer
Short-Positionen (Put-Optionsscheine/KO statt Call).

Architektur-Entscheidung (Stand 21.07.2026):
  - Universum: gleiches ~370er-Sektoren-Universum wie die anderen Scanner,
    aber die BOTTOM-Sektoren (schwaechste Rotation) statt der Top-Sektoren -
    Short-Kandidaten liegen typischerweise in aktuell schwachen Sektoren.
  - Separater, frueherer Workflow (ca. 04:00 Uhr MESZ, siehe short_check.yml)
    - laedt sein Ergebnis wie die anderen Scanner zu Drive hoch.
    gemini_auswertung.py laedt die Dateien von dort automatisch nach (siehe
    lade_short_dateien_von_drive dort) - kein gemeinsames Dateisystem noetig.
  - Gespiegelte Muster (alle vier, analog zur Long-Setup-Qualitaets-Matrix):
      EMA-Breakdown       <-> EMA-Breakout
      Pullback-Zone short <-> Pullback-Zone
      Trendlinien-Bruch   <-> Trendlinien-Ausbruch
      Kumo-Ausbruch unten <-> Kumo-Ausbruch
  - RS-Filter INVERTIERT: verwirft Titel mit RS_vs_Benchmark > +10% (nur
    Nachzuegler shorten, keine Marktfuehrer).
  - Marktumfeld-Modifikator INVERTIERT: ein baerisches Marktumfeld WERTET
    Short-Setups AUF (+1 Stufe), nicht ab - Rueckenwind fuer die Idee.
  - Stop OBERHALB des Einstiegs, Kursziele UNTERHALB (Widerstaende von oben
    werden zu Zielen, siehe potenzial_targets_unterhalb).

WICHTIGER HINWEIS ZUR TRENDLINIEN-BRUCH-ERKENNUNG: Die exakte Methodik der
Trendlinien-Erkennung im Original-Long-Scanner (analyse.py) ist dort nicht
als eigenstaendige, wiederverwendbare Funktion ausgelagert (liegt inline in
analyze_a_setup). Hier daher eine eigenstaendige, funktional gleichwertige
Umsetzung per linearer Regression durch die letzten lokalen Hochpunkte
(scipy.signal.argrelextrema + linregress) - vermutlich nicht Zeile-fuer-
Zeile identisch zur Original-Implementierung, aber nach demselben Prinzip
(≥ 3 Punkte, Bruch nach unten mit Volumen-Bestaetigung).

Voraussetzungen: dieselben Umgebungsvariablen wie analyse.py
(ALPACA_KEY, ALPACA_SECRET, GROQ_API_KEY - Import von analyse.py fuehrt
dessen kompletten Modul-Code aus, siehe main.yml-Erfahrung beim
Trendwende-Scanner). Muss im selben Verzeichnis wie analyse.py liegen.
"""

import datetime
import re
import math
import numpy as np
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
from scipy.signal import argrelextrema

from analyse import (
    alpaca_client,
    sektoren_aktien,
    dax_aktien,
    sektoren_map,
    eu_sektoren_etf,
    get_perf,
    get_perf_yf,
    clean_num,
    get_benchmark_close,
    get_eu_benchmark_close,
    check_rsi_divergence,
    get_earnings_warnung,
    get_news_headlines,
    get_ideal_delta,
    berechne_fundamental_ampel,
    get_golden_cross_status,
    get_index_benchmark_yf,
    klassifiziere_marktumfeld,
)
from collections import Counter

# Beinahe-Treffer der letzten Stufe (NEU 30.07.2026) - siehe Kommentar
# an der Sammelstelle in _pruefe_short_setup.
BEINAHE_SHORT = []
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

BOTTOM_SEKTOREN_US = 8   # spiegelt Top-8 beim Long-Scanner
BOTTOM_SEKTOREN_EU = 5   # spiegelt Top-5 beim Long-Scanner
FRISCHE_TAGE = 3         # gleiches Fenster wie bei Kumo/EMA im Long-Scanner
CHUNK_SIZE = 100         # Sammel-Abrufe wie beim Trendwende-Scanner
RS_MAX = 10.0            # verwirft Titel mit RS_vs_Benchmark > +10%


def _chunks(liste, groesse):
    for i in range(0, len(liste), groesse):
        yield liste[i:i + groesse]


# ---------------------------------------------------------------------------
# SAMMEL-ABRUFE (identisches Prinzip wie trendwende_scanner.py - ein
# Request pro Chunk statt einem Request pro Ticker)
# ---------------------------------------------------------------------------

def fetch_us_batch(ticker_liste):
    ergebnis = {}
    start_date = datetime.datetime.now() - datetime.timedelta(days=365)
    for chunk in _chunks(ticker_liste, CHUNK_SIZE):
        try:
            request = StockBarsRequest(symbol_or_symbols=chunk, start=start_date, timeframe=TimeFrame.Day)
            df_alle = alpaca_client.get_stock_bars(request).df
        except Exception as e:
            print(f"FEHLER beim Sammel-Abruf US-Chunk ({len(chunk)} Ticker): {e}")
            continue
        if df_alle.empty:
            continue
        for ticker in chunk:
            try:
                data = df_alle.loc[ticker].copy()
            except KeyError:
                continue
            if data.empty:
                continue
            data = data.rename(columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 'volume': 'Volume'})
            ergebnis[ticker] = data
    print(f"DEBUG: US-Sammel-Abruf (Short) lieferte Daten fuer {len(ergebnis)}/{len(ticker_liste)} Ticker.")
    return ergebnis


def fetch_eu_batch(ticker_liste):
    ergebnis = {}
    for chunk in _chunks(ticker_liste, CHUNK_SIZE):
        try:
            df_alle = yf.download(tickers=" ".join(chunk), period="1y", group_by='ticker', threads=True, auto_adjust=False, progress=False)
        except Exception as e:
            print(f"FEHLER beim Sammel-Abruf EU-Chunk ({len(chunk)} Ticker): {e}")
            continue
        if df_alle.empty:
            continue
        for ticker in chunk:
            try:
                data = df_alle[ticker].copy() if isinstance(df_alle.columns, pd.MultiIndex) else df_alle.copy()
            except KeyError:
                continue
            data = data.dropna(subset=['Close', 'High', 'Low', 'Volume'])
            if data.empty:
                continue
            ergebnis[ticker] = data
    print(f"DEBUG: EU-Sammel-Abruf (Short) lieferte Daten fuer {len(ergebnis)}/{len(ticker_liste)} Ticker.")
    return ergebnis


# ---------------------------------------------------------------------------
# INDIKATOREN (identisch zum Trendwende-Scanner)
# ---------------------------------------------------------------------------

def _indikatoren_berechnen(data):
    data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
    data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
    data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean()
    data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean()
    data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
    data['WMA200'] = data['Close'].rolling(200).apply(
        lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True
    )
    data['Vol_SMA20'] = data['Volume'].rolling(20).mean()
    data['Vol_Ratio'] = (data['Volume'] / data['Vol_SMA20']).fillna(0)

    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 1e-9)
    data['RSI'] = (100 - (100 / (1 + rs))).fillna(50)

    exp1 = data['Close'].ewm(span=12, adjust=False).mean()
    exp2 = data['Close'].ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal = macd.ewm(span=9, adjust=False).mean()
    data['MACD_Trend'] = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Baerisch"

    data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
    data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
    data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)
    data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

    # Stochastik (14,3) - fehlte bisher, ist aber Teil von Setups.csv
    low_min = data['Low'].rolling(14).min()
    high_max = data['High'].rolling(14).max()
    data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))

    return data


# ---------------------------------------------------------------------------
# GESPIEGELTE SETUP-MUSTER (baerische Varianten der 4 Long-Muster)
# ---------------------------------------------------------------------------

def check_ema_breakdown(data):
    """Spiegelbild zu EMA-Breakout: EMA8 kreuzt EMA20 von oben nach unten,
    mit Volumen-Bestaetigung, Ausbruch innerhalb der letzten FRISCHE_TAGE."""
    if len(data) < 25:
        return False
    for i in range(0, FRISCHE_TAGE + 1):
        idx, idx_prev = -1 - i, -2 - i
        if abs(idx_prev) > len(data):
            break
        crossover = data['EMA8'].iloc[idx] < data['EMA20'].iloc[idx] and data['EMA8'].iloc[idx_prev] >= data['EMA20'].iloc[idx_prev]
        vol_ok = data['Vol_Ratio'].iloc[idx] > 1.0
        if crossover and vol_ok and data['EMA8'].iloc[-1] < data['EMA20'].iloc[-1]:
            return True
    return False


def check_pullback_zone_short(data, ticker=None):
    """Spiegelbild zu Pullback-Zone: Kurs testet EMA20/50/Kijun von UNTEN
    (Widerstandstest im Abwaertstrend), Lower-High bestaetigt (statt
    Higher-Low). Mindest-Volumen (GEÄNDERT 27.07.2026, zweite Iteration nach
    Nutzerfeedback, analog zu analyse.py/edelmetalle_scanner.py): bewusst nur
    ein Mindest-BODEN (heutiges Vol_Ratio >= 0.7) statt einer Pflicht-Spitze -
    ein gesunder Pullback/Widerstandstest laeuft klassischerweise auf
    abnehmendem statt steigendem Volumen, eine Spitzen-Pflicht waere hier
    fachlich unpassend. ticker (optional): nur fuers Debug-Logging, aendert
    die Logik nicht."""
    if len(data) < 30:
        return False
    close = data['Close'].iloc[-1]
    zonen = [data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['Kijun'].iloc[-1]]
    in_zone = any(pd.notna(z) and 0 <= (z - close) / close <= 0.02 for z in zonen)
    if not in_zone:
        if ticker:
            print(f"DEBUG-SHORT-PULLBACK: {ticker} -> InZone: False (Grund: EMA-Zone nicht erfüllt)")
        return False
    vol_ratio_heute = data['Vol_Ratio'].iloc[-1]
    volumen_ausreichend = bool(vol_ratio_heute >= 0.7)
    if not volumen_ausreichend:
        if ticker:
            print(f"DEBUG-SHORT-PULLBACK: {ticker} -> InZone: False (Grund: Volumen zu duenn ({vol_ratio_heute:.2f}x < 0.7))")
        return False
    ilocs_max = argrelextrema(data['High'].tail(20).values, np.greater_equal, order=3)[0]
    if len(ilocs_max) < 2:
        if ticker:
            print(f"DEBUG-SHORT-PULLBACK: {ticker} -> InZone: False (Grund: zu wenig Swing-Hochs fuer Lower-High-Pruefung)")
        return False
    lower_high = data['High'].tail(20).iloc[ilocs_max[-1]] < data['High'].tail(20).iloc[ilocs_max[-2]]
    return bool(lower_high)


def check_trendline_breakdown(data, lookback=120, order=5, touch_tolerance=0.01):
    """Exaktes Spiegelbild von check_trendline_breakout in analyse.py: sucht
    eine STEIGENDE Stütz-Trendlinie durch mindestens 3 Swing-Tiefs (Toleranz
    1%) und prüft, ob der Kurs innerhalb der letzten 3 Kerzen mit über-
    durchschnittlichem Volumen darunter ausgebrochen ist."""
    fenster = data.iloc[-lookback:] if len(data) > lookback else data.copy()
    if len(fenster) < 10:
        return False, None
    suchbereich = fenster.iloc[:-3]
    if len(suchbereich) < 10:
        return False, None

    lows = suchbereich['Low'].values
    idx_swings = argrelextrema(lows, np.less_equal, order=order)[0]
    if len(idx_swings) < 3:
        return False, None

    x = idx_swings.astype(float)
    y = lows[idx_swings]
    slope, intercept = np.polyfit(x, y, 1)

    # Nur STEIGENDE Stützlinien relevant (Bruch nach unten = Short-Signal)
    if slope <= 0:
        return False, None

    linie_bei_punkten = slope * x + intercept
    beruehrungen = int(np.sum(np.abs(y - linie_bei_punkten) <= (linie_bei_punkten * touch_tolerance)))
    if beruehrungen < 3:
        return False, None

    heute_pos = len(fenster) - 1
    linie_heute = slope * heute_pos + intercept
    close_heute = fenster['Close'].iloc[-1]

    crossunder_kuerzlich = any(
        fenster['Close'].iloc[-1 - i] >= (slope * (heute_pos - i) + intercept)
        for i in range(1, 4)
    )
    volumen_ok = any(
        fenster['Volume'].iloc[-1 - i] > fenster['Vol_SMA20'].iloc[-1 - i]
        for i in range(0, 3)
    )

    bruch = bool(close_heute < linie_heute) and crossunder_kuerzlich and bool(volumen_ok)
    return bruch, (float(linie_heute) if bruch else None)


def check_kumo_breakdown(data):
    """Spiegelbild zu check_kumo_breakout in analyse.py: Kurs muss die
    KOMPLETTE Wolke von oben nach unten durchbrochen haben (unter BEIDEN
    Grenzen), Bruch innerhalb der letzten FRISCHE_TAGE, Pflicht-Volumen."""
    if len(data) < 5 or 'SenkouA' not in data.columns or 'SenkouB' not in data.columns:
        return False, None
    kumo_unter = data[['SenkouA', 'SenkouB']].min(axis=1)
    heute_unter = kumo_unter.iloc[-1]
    close_heute = data['Close'].iloc[-1]
    if pd.isna(heute_unter) or close_heute >= heute_unter:
        return False, None
    frischer_bruch = any(
        pd.notna(kumo_unter.iloc[-1 - i]) and data['Close'].iloc[-1 - i] >= kumo_unter.iloc[-1 - i]
        for i in range(1, FRISCHE_TAGE + 1) if abs(-1 - i) <= len(data)
    )
    if not frischer_bruch:
        return False, None
    vol_ok = any(data['Vol_Ratio'].iloc[-1 - i] > 1.0 for i in range(0, FRISCHE_TAGE) if abs(-1 - i) <= len(data))
    if not vol_ok:
        return False, None
    return True, round(float(heute_unter), 2)


def get_fib_levels_short(data):
    """Abwaerts-Pendant zu get_fib_levels in analyse.py (dort: Extension-Level
    UEBER dem Kurs fuer Long-Kursziele). Hier: Retracement/Extension-Level
    UNTERHALB des aktuellen Kurses, gleiche Basis (Hoch/Tief der letzten 60
    Tage)."""
    recent_data = data.iloc[-60:]
    swing_high = recent_data['High'].max()
    swing_low = recent_data['Low'].min()
    span = swing_high - swing_low

    fib_0618 = swing_high - (span * 0.618)   # Retracement-Ziel nach unten
    fib_1000 = swing_low - (span * 0.618)    # Extension unter das bisherige Tief

    return fib_0618, fib_1000


def get_swing_lows_below(data, entry, lookback=120, order=5, max_n=3):
    """Echte Pivot-Tiefs (lokale Kurstiefs) aus der juengeren Kurshistorie als
    zusaetzliche Abwaerts-Ziel-Kandidaten, gefiltert auf < entry. Gleiches
    Fenster/Prinzip wie check_trendline_breakdown weiter unten."""
    fenster = data.iloc[-lookback:] if len(data) > lookback else data.copy()
    if len(fenster) < 10:
        return []
    lows = fenster['Low'].values
    idx_swings = argrelextrema(lows, np.less_equal, order=order)[0]
    kandidaten = sorted(
        {round(float(lows[i]), 4) for i in idx_swings if pd.notna(lows[i]) and lows[i] < entry},
        reverse=True,
    )
    return kandidaten[:max_n]


def get_round_number_targets(entry, anzahl=2):
    """Psychologische runde Kursmarken UNTERHALB des Einstiegs als
    zusaetzliche Ziel-Kandidaten - an runden Zahlen (glatte Euro-/Dollar-
    Betraege) haeufen sich erfahrungsgemaess Limit-/Stop-Orders, was sie zu
    plausiblen Unterstuetzungszonen macht. Die Rundungs-Schrittweite skaliert
    mit der Kursgroessenordnung (z.B. 5$-Schritte bei einer 100$-Aktie,
    0,1$-Schritte bei einer 3$-Aktie)."""
    if entry >= 1000:
        schritt = 50
    elif entry >= 100:
        schritt = 5
    elif entry >= 10:
        schritt = 1
    elif entry >= 1:
        schritt = 0.1
    else:
        schritt = 0.01

    marken = []
    naechste_runde = math.floor(entry / schritt) * schritt
    if naechste_runde >= entry:
        naechste_runde -= schritt
    aktuell = naechste_runde
    while len(marken) < anzahl and aktuell > 0:
        marken.append(round(aktuell, 4))
        aktuell -= schritt
    return marken


def sammle_abwaerts_ziele(data, entry, mindest_abstand_perc=1.0, dedupe_abstand_perc=1.5):
    """NEU (24.07.2026, erweitert 25.07.2026): ersetzt die zuvor 1:1 aus
    analyse.py uebernommenen Long-Zielfunktionen (EMA20/50/100/200/WMA200 +
    get_fib_levels), die bei einem Short praktisch nutzlos waren: die
    Grundvoraussetzung fuer ein Short-Setup ist entry < WMA200, d.h. der Kurs
    liegt bereits UNTER all seinen eigenen EMAs/der WMA200 - diese Levels
    lagen damit fast immer UEBER dem Einstieg statt darunter.
    Sammelt jetzt alle plausiblen charttechnischen Abwaerts-Ziel-Kandidaten:
    Fib-Retracement/Extension nach unten, 52-Wochen-Tief, echte Pivot-Tiefs,
    die Ichimoku-Wolke, gleitende Durchschnitte (nur falls sie ausnahmsweise
    doch unter dem Kurs liegen, z.B. bei einem besonders scharfen Einbruch)
    und psychologische runde Kursmarken. Zwei Filter sorgen dafuer, dass
    daraus verwertbare TP1/TP2 statt Rauschen werden:
    - mindest_abstand_perc: Kandidaten, die weniger als X% unter dem Kurs
      liegen, werden verworfen (sonst waere TP1 z.B. 0,1% unter dem Kurs -
      kein sinnvolles erstes Kursziel).
    - dedupe_abstand_perc: liegen zwei Kandidaten weniger als Y% auseinander,
      wird nur der naeher am Kurs liegende behalten (sonst koennten TP1 und
      TP2 praktisch identisch werden, z.B. eine runde Zahl direkt neben einem
      Swing-Tief)."""
    fib1, fib2 = get_fib_levels_short(data)
    kumo_werte = [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]]
    tief_52w = float(data['Low'].min())
    swing_lows = get_swing_lows_below(data, entry)
    ema_werte = [
        data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1],
        data['EMA200'].iloc[-1], data['WMA200'].iloc[-1],
    ]
    runde_zahlen = get_round_number_targets(entry)

    alle_kandidaten = [fib1, fib2, tief_52w] + kumo_werte + swing_lows + ema_werte + runde_zahlen
    roh = sorted(
        {round(float(v), 4) for v in alle_kandidaten if pd.notna(v) and v < entry},
        reverse=True,
    )

    # Mindestabstand zum Kurs (Rauschen direkt unter dem Einstieg raus)
    mindest_wert = entry * (1 - mindest_abstand_perc / 100)
    gefiltert = [v for v in roh if v <= mindest_wert]

    # Dedupe: zu nah beieinander liegende Kandidaten zusammenfassen
    ziele = []
    for v in gefiltert:
        if not ziele or (ziele[-1] - v) / entry * 100 >= dedupe_abstand_perc:
            ziele.append(v)

    return ziele


def check_bearish_confirmation(df):
    """Spiegelbild zu check_bullish_confirmation in analyse.py: Shooting
    Star (langer oberer Docht, kleiner Koerper) oder Bearish Engulfing."""
    if len(df) < 3:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last['Close'] - last['Open'])
    upper_wick = last['High'] - max(last['Open'], last['Close'])
    lower_wick = min(last['Open'], last['Close']) - last['Low']

    if upper_wick > (2 * body) and lower_wick < body:
        return "Shooting-Star"

    is_prev_bullish = prev['Close'] > prev['Open']
    is_last_bearish = last['Close'] < last['Open']
    engulfs = last['Open'] > prev['Close'] and last['Close'] < prev['Open']
    if is_prev_bullish and is_last_bearish and engulfs:
        return "Bearish-Engulfing"
    return None


# ---------------------------------------------------------------------------
# SETUP-QUALITAETS-MATRIX (gespiegelt, siehe Modul-Docstring)
# ---------------------------------------------------------------------------

def _pruefe_short_setup(ticker, sektor, markt, data, bench_close=None, marktumfeld_baerisch=False, sektor_momentum=None):
    """Gibt (ergebnis_dict_oder_None, funnel_grund) zurueck - der zweite Wert
    speist die Funnel-Statistik (NEU 28.07.2026, Nutzerwunsch: '0 Kandidaten'
    soll interpretierbar sein)."""
    if len(data) < 60:
        return None, "zu_wenig_daten"

    # Namensaufloesung VORGEZOGEN (BUGFIX 31.07.2026, Nutzerwunsch: Beinahe-
    # Kandidaten sollen wie ueberall sonst mit vollem Namen erscheinen, nicht
    # nur Ticker). longName bevorzugt, shortName als Rueckfall, sonst Ticker -
    # gleiche Logik wie in analyse.py.
    try:
        _info = yf.Ticker(ticker).info
        firma_name = _info.get('longName') or _info.get('shortName') or ticker
        firma_name = re.sub(r'\s+', ' ', str(firma_name)).strip()
        firma_name = re.sub(r'\s+[A-Za-z]$', '', firma_name).strip(' ,;-')
        if not firma_name:
            firma_name = ticker
        analysten_kursziel = _info.get('targetMeanPrice')
    except Exception:
        firma_name = ticker
        analysten_kursziel = None

    data = _indikatoren_berechnen(data)
    entry = data['Close'].iloc[-1]

    # Grundvoraussetzung (gespiegelt): Kurs UNTER WMA200 (Abwaertstrend)
    if pd.isna(data['WMA200'].iloc[-1]) or entry >= data['WMA200'].iloc[-1]:
        return None, "kein_abwaertstrend"

    pfade = []
    trendlinien_bruch, _ = check_trendline_breakdown(data)
    if trendlinien_bruch:
        pfade.append("Trendlinien-Bruch")
    kumo_bruch, kumo_level = check_kumo_breakdown(data)
    if kumo_bruch:
        pfade.append("Kumo-Ausbruch unten")
    if check_ema_breakdown(data):
        pfade.append("EMA-Breakdown")
    pullback_short = check_pullback_zone_short(data, ticker=ticker)
    muster = check_bearish_confirmation(data)
    if pullback_short and muster:
        pfade.append("Pullback-Zone short")

    if not pfade:
        return None, "kein_setup_muster"
    setup_typ = " + ".join(pfade)

    # Basis-Einstufung (gespiegelte Matrix aus Gemini-Anleitung Abschnitt 2):
    # Trendlinien-Bruch ODER Kumo-Ausbruch unten -> A, Pullback-Zone-short
    # UND Muster -> A, alles andere -> B
    basis = "A" if ("Trendlinien-Bruch" in pfade or "Kumo-Ausbruch unten" in pfade or "Pullback-Zone short" in pfade) else "B"

    # Divergenz (NEU): echte check_rsi_divergence-Funktion wiederverwendet
    # (deckt beide Richtungen ab). Bärische Divergenz validiert das Setup
    # analog zur Long-Logik unabhängig von anderen ACHTUNG-Kriterien.
    divergenz = check_rsi_divergence(data)  # "Bullisch"/"Bärisch"/None
    divergenz_bearish = (divergenz == "Bärisch")

    # NEU (23.07.2026): Bullischer MACD widerspricht der Short-These direkt
    # und wird nur durch eine bärische Divergenz aufgehoben (die validiert
    # staerker, als der MACD widerspricht) - vorher wurde das Setup nur mit
    # Status2=ACHTUNG markiert und trotzdem ausgegeben, jetzt wird es an
    # dieser Stelle komplett verworfen.
    if data['MACD_Trend'].iloc[-1] == "Bullisch" and not divergenz_bearish:
        return None, "macd_bullisch_ohne_divergenz"

    stufen = ["B-", "B", "B+", "A-", "A", "A+"]
    idx = stufen.index("B" if basis == "B" else "A")
    verschiebung = 0
    if data['Vol_Ratio'].iloc[-1] > 1.0:
        verschiebung += 1
    elif data['Vol_Ratio'].iloc[-1] < 0.5:
        verschiebung -= 1
    # Sektor-Score des Setup-Sektors aus Performance.csv-Momentum ziehen
    sektor_score = None
    if sektor_momentum:
        try:
            sektor_score = float(sektor_momentum.get('Rotation-Score'))
        except (TypeError, ValueError):
            sektor_score = None

    # SEKTOR-MODIFIKATOR (NEU 28.07.2026, Nutzerentscheidung, gespiegelt zum
    # Long-Scanner): das Setup wird danach beurteilt, ob sein EIGENER Sektor
    # Abwaerts-Rueckenwind hat - nicht nur, ob der Gesamtmarkt schwach ist.
    #   Rotation-Score <= -2.0 -> +1 Stufe (klarer Abwaerts-Rueckenwind)
    #   Rotation-Score >  0    -> -1 Stufe (Sektor dreht nach oben - Short
    #                                       gegen den Sektortrend)
    #   dazwischen             ->  0
    if sektor_score is not None:
        if sektor_score <= -2.0:
            verschiebung += 1
        elif sektor_score > 0:
            verschiebung -= 1

    # Marktumfeld-Modifikator INVERTIERT, seit 28.07.2026 NUR NOCH MIT
    # SEKTOR-BESTAETIGUNG: baerisches Umfeld wertet Short-Setups auf, aber
    # nur, wenn der Sektor-Score < 1.0 ist (Bestaetigung statt Pauschale -
    # ein nach oben rotierender Sektor macht den globalen Rueckenwind fuer
    # DIESEN Titel wertlos). Ohne Sektor-Score (None) greift die Aufwertung
    # wie bisher (defensiv: keine Daten = keine Verschaerfung der Regel).
    if marktumfeld_baerisch and (sektor_score is None or sektor_score < 1.0):
        verschiebung += 1
    idx = max(0, min(len(stufen) - 1, idx + verschiebung))
    feinstufe = stufen[idx]

    # Status2/Status_Grund (GEÄNDERT 23.07.2026): der bullische-MACD-Fall
    # wird jetzt schon weiter oben komplett verworfen (return None), taucht
    # hier also nicht mehr auf - übrig bleibt nur noch schwaches Volumen als
    # ACHTUNG-Grund, AUSSER bärische Divergenz validiert automatisch
    # (gespiegelt zur Long-Logik in analyse.py).
    if divergenz_bearish:
        status2, status_grund = "VALIDE", "Alles ok"  # Divergenz steht separat in eigener Spalte (wie bei Setups.csv), nicht im Grund-Text
    elif data['Vol_Ratio'].iloc[-1] < 0.5:
        status2, status_grund = "ACHTUNG", "Schwaches Volumen"
    else:
        status2, status_grund = "VALIDE", "Kein Störfaktor erkannt"

    rel_staerke = None
    if bench_close is not None and len(bench_close) > 60 and len(data) > 60:
        stock_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
        bench_perf_60 = ((bench_close.iloc[-1] / bench_close.iloc[-60]) - 1) * 100
        rel_staerke = round(stock_perf_60 - bench_perf_60, 2)
        # RS-Filter INVERTIERT: nur Nachzuegler shorten (siehe Modul-Docstring)
        if rel_staerke > RS_MAX:
            print(f"DEBUG-SHORT-VERWORFEN: {ticker} -> RS zu stark fuer Short ({rel_staerke}% > {RS_MAX}%)")
            return None, "rs_zu_stark"

    # Stop OBERHALB, Ziele UNTERHALB (Widerstaende von oben werden zu Zielen)
    juengstes_hoch = round(float(data['High'].iloc[-10:].max()) * 1.02, 2)
    stop = juengstes_hoch
    risk_perc = round(((stop - entry) / entry) * 100, 2)

    abwaerts_ziele = sammle_abwaerts_ziele(data, entry)
    tp1 = abwaerts_ziele[0] if abwaerts_ziele else entry * 0.92
    tp2 = abwaerts_ziele[1] if len(abwaerts_ziele) >= 2 else (tp1 * 0.95)
    tech_kursziel = tp1  # analog zu analyse.py, wo Tech-Kursziel = TP1 gesetzt wird

    crv1 = round((entry - tp1) / (stop - entry), 2) if stop > entry else 0
    crv2 = round((entry - tp2) / (stop - entry), 2) if stop > entry else 0
    chance1_perc = round(((entry - tp1) / entry) * 100, 2)
    chance2_perc = round(((entry - tp2) / entry) * 100, 2)

    # NEU (25.07.2026): Risiko-Filter, analog zur bestehenden Konvention bei
    # Long-Setups und Edelmetalle-Setups ("CRV muss bei TP1 UND TP2 jeweils
    # >= 1.0 sein") - vorher gab es diesen Filter bei Shorts noch nicht,
    # wodurch auch Setups mit deutlich schlechterem Chance/Risiko-Verhaeltnis
    # ausgegeben wurden.
    if crv1 < 1.0 or crv2 < 1.0:
        print(f"DEBUG-SHORT-VERWORFEN: {ticker} -> CRV zu niedrig (CRV1={crv1}, CRV2={crv2})")
        # BEINAHE-KANDIDAT (NEU 30.07.2026): beim Short faellt seit Wochen
        # praktisch alles an dieser Stufe - dann soll wenigstens sichtbar sein,
        # WIE knapp und bei welchen Titeln.
        # GEAENDERT (30.07.2026, Nutzerwunsch): Eintrag als Dict statt reinem
        # String - crv_sortier haelt den BINDENDEN (kleineren) CRV fest, damit
        # die Ausgabe absteigend danach sortiert werden kann (knappste
        # Beinahe-Treffer zuerst, statt alphabetisch nach Ticker verstreut).
        # BUGFIX (31.07.2026): 'risiko' existierte in dieser Funktion nicht -
        # der Short-Scanner nennt die Kennzahl 'risk_perc' (Zeile oben, bereits
        # berechnet) und sie ist ein PROZENTWERT (Groessenordnung ~0-20), nicht
        # ein Kursbetrag wie 'entry'/'tp1' - deshalb hier .2f mit Prozentzeichen
        # statt wie bei Kursen. Fehler brach den kompletten Lauf ab (NameError
        # in einem ThreadPoolExecutor-Future reisst main() komplett mit), sobald
        # der ERSTE Short-Titel am CRV-Filter scheiterte - traf also praktisch
        # jeden Lauf, da CRV-Ablehnungen der Normalfall sind (siehe Funnel-
        # Statistik der letzten Tage).
        BEINAHE_SHORT.append({
            "text": f"{firma_name} ({ticker}): CRV-Filter -> Kurs={entry:.2f} | TP1={tp1:.2f} | "
                   f"Chance1={chance1_perc:.2f}% | CRV1={crv1} | TP2={tp2:.2f} | "
                   f"Chance2={chance2_perc:.2f}% | CRV2={crv2} | Stop={stop:.2f} | "
                   f"Risiko={risk_perc:.2f}%",
            "crv_sortier": min(crv1, crv2),
        })
        return None, "crv_unter_1"

    # Abstand_52W_Tief% (NEU, gespiegelt zu Abstand_52W_Hoch% bei Long):
    # wie weit über dem 52-Wochen-Tief - Raum, den der Kurs noch fallen
    # könnte, bevor der bisherige Tiefpunkt erreicht wird.
    tief_52w = data['Low'].min()
    abstand_52w_tief = round(((entry / tief_52w) - 1) * 100, 2) if tief_52w > 0 else None

    return {
        "Ticker": ticker, "Name": firma_name, "Sektor": sektor, "Markt": markt,
        "Waehrung": "EUR" if markt == "EU" else "USD",
        "Trend": "OK",  # Grundvoraussetzung (Kurs < WMA200) bereits weiter oben geprüft
        "Setup_Typ": setup_typ, "Pattern": muster or "Kein",
        "Tech-Kursziel": round(clean_num(tech_kursziel), 2),
        "Analysten-Kursziel": round(clean_num(analysten_kursziel), 2) if analysten_kursziel else None,
        "Upside_%_vs_Aktuell": chance1_perc,  # Pendant zu Long: % bis Tech-Kursziel
        "Status2": status2, "Status_Grund": status_grund,
        "RSI": round(clean_num(data['RSI'].iloc[-1]), 2),
        "MACD_Trend": data['MACD_Trend'].iloc[-1],
        "CRV1": crv1, "CRV2": crv2,
        "Chance1_Perc": chance1_perc, "Chance2_Perc": chance2_perc,
        "Kurs": round(clean_num(entry), 2),
        "Einstieg": round(clean_num(entry), 2),
        "Einstieg2(EMA 20)": round(clean_num(data['EMA20'].iloc[-1]), 2),
        "Stop": stop, "Risk_Perc": risk_perc,
        "TP1": round(clean_num(tp1), 2), "TP2": round(clean_num(tp2), 2),
        "Stoch_K": round(clean_num(data['Stoch_K'].iloc[-1]), 2),
        "Vol_Ratio": round(clean_num(data['Vol_Ratio'].iloc[-1]), 2),
        "Ideales_Delta": get_ideal_delta(chance1_perc),
        "RS_vs_Benchmark%": rel_staerke,
        "Abstand_52W_Tief%": abstand_52w_tief,
        "Divergenz": divergenz or "Keine",
        "Golden_Cross_Status": get_golden_cross_status(data),
        "Setup_Qualitaet": feinstufe,
        "Risikohinweis": (
            "Short-Setup - setzt auf fallende Kurse (Put-Optionsschein/KO). "
            "Theoretisch unbegrenztes Verlustrisiko bei Kursanstieg (anders als bei Long, "
            "wo maximal der Einsatz verloren geht) - Positionsgroesse entsprechend konservativ waehlen."
        ),
    }, "kandidat"


# ---------------------------------------------------------------------------
# HAUPTPROGRAMM
# ---------------------------------------------------------------------------

def bestimme_bottom_sektoren():
    """Analog zu Top-8/Top-5 im Long-Scanner, aber die SCHWAECHSTEN Sektoren
    (nlargest -> nsmallest). Gibt (bottom_us_sektoren, bottom_eu_sektoren,
    momentum_us, momentum_eu) - die beiden momentum-Dicts liefern je
    Sektor-Name {5T, 12T, Rotation-Score} fuer die Briefing-Ausgabe (NEU,
    analog zum Sektor-Momentum-Feld bei den normalen Setups)."""
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)
    df_perf_eu = pd.DataFrame([get_perf_yf(t, n) for t, n in eu_sektoren_etf.items()]).sort_values("Rotation-Score", ascending=False)

    bottom_us = df_perf.nsmallest(BOTTOM_SEKTOREN_US, 'Rotation-Score')['Sektor'].tolist()
    bottom_eu = df_perf_eu.nsmallest(BOTTOM_SEKTOREN_EU, 'Rotation-Score')['Sektor'].tolist()

    momentum_us = df_perf.set_index('Sektor')[['5T', '12T', 'Rotation-Score']].to_dict('index')
    momentum_eu = df_perf_eu.set_index('Sektor')[['5T', '12T', 'Rotation-Score']].to_dict('index')

    print(f"DEBUG: Bottom-{BOTTOM_SEKTOREN_US}-US-Sektoren laut Rotation-Score: {bottom_us}")
    print(f"DEBUG: Bottom-{BOTTOM_SEKTOREN_EU}-EU-Sektoren laut Rotation-Score: {bottom_eu}")
    return bottom_us, bottom_eu, momentum_us, momentum_eu


def sammle_universum(bottom_us_sektoren, bottom_eu_sektoren):
    # BUGFIX (21.07.2026): sektoren_aktien nutzt ETF-TICKER als Schlüssel
    # (z. B. "XLK", "SOXX"), waehrend bottom_us_sektoren LESBARE NAMEN
    # enthaelt (z. B. "Halbleiter" - kommt aus get_perf()). Ohne dieses
    # Mapping matcht kein einziger US-Sektor (0 US-Ticker im Testlauf vom
    # 21.07.2026) - bei dax_aktien sind die Schluessel zufaellig schon
    # Namen, deshalb ist der EU-Teil davon nicht betroffen.
    name_zu_ticker = {name: ticker for ticker, name in sektoren_map.items()}
    bottom_us_ticker_keys = [name_zu_ticker[n] for n in bottom_us_sektoren if n in name_zu_ticker]

    us_tasks, eu_tasks = [], []
    for sektor_ticker, aktien in sektoren_aktien.items():
        if sektor_ticker in bottom_us_ticker_keys:
            sektor_name_lesbar = sektoren_map.get(sektor_ticker, sektor_ticker)
            us_tasks.extend([(t, sektor_name_lesbar) for t in aktien])
    for sektor_name, aktien in dax_aktien.items():
        if sektor_name in bottom_eu_sektoren:
            eu_tasks.extend([(t, sektor_name) for t in aktien])
    print(f"DEBUG: Short-Universum (nur Bottom-Sektoren) -> US: {len(us_tasks)} | EU: {len(eu_tasks)}")
    return us_tasks, eu_tasks


def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    print("Short-Scanner gestartet...")

    spy_close = get_benchmark_close()
    eu_bench_close = get_eu_benchmark_close()

    # Marktumfeld (GEAENDERT 28.07.2026, Nutzerwunsch): gleiche regelbasierte
    # 3-Stufen-Klassifikation wie im Hauptscanner (Bullisch = Kurs ueber EMA20 |
    # Neutral = unter EMA20, aber ueber EMA50 und WMA200 | Baerisch = unter
    # EMA50 oder unter WMA200; je Region zaehlt der schwaechere Leitindex) -
    # ersetzt die fruehere, deutlich grobere "unter EMA20 = baerisch"-Heuristik,
    # damit Short-Modifikator und Marktumfeld-Fazit der Auswertung nie mehr
    # auseinanderlaufen. get_index_benchmark_yf fuellt dabei BENCHMARK_LEVELS
    # (identischer Mechanismus wie im Hauptscanner-Briefing).
    for _tick, _label in [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"),
                          ("^RUT", "Russell 2000"),
                          ("^GDAXI", "DAX"), ("^STOXX50E", "EuroStoxx50"),
                          ("^STOXX", "STOXX Europe 600")]:
        get_index_benchmark_yf(_tick, _label)
    us_stufe, us_detail, us_score = klassifiziere_marktumfeld(
        [("S&P 500", 2), ("Nasdaq", 1), ("Russell 2000", 1)])
    eu_stufe, eu_detail, eu_score = klassifiziere_marktumfeld(
        [("DAX", 2), ("EuroStoxx50", 1), ("STOXX Europe 600", 1)])

    # Defensiver Fallback: liefert die Index-Abfrage keine Levels (API-Fehler),
    # greift die alte EMA20-Heuristik auf Basis der ohnehin geladenen
    # Benchmark-Reihen - besser eine grobe Einstufung als gar keine.
    if us_stufe == "N/A":
        marktumfeld_baerisch_us = bool(len(spy_close) > 20 and spy_close.iloc[-1] < spy_close.ewm(span=20, adjust=False).mean().iloc[-1])
        us_stufe = "Baerisch (Fallback EMA20)" if marktumfeld_baerisch_us else "Nicht baerisch (Fallback EMA20)"
    else:
        marktumfeld_baerisch_us = (us_stufe == "Bärisch")
    if eu_stufe == "N/A":
        marktumfeld_baerisch_eu = bool(len(eu_bench_close) > 20 and eu_bench_close.iloc[-1] < eu_bench_close.ewm(span=20, adjust=False).mean().iloc[-1])
        eu_stufe = "Baerisch (Fallback EMA20)" if marktumfeld_baerisch_eu else "Nicht baerisch (Fallback EMA20)"
    else:
        marktumfeld_baerisch_eu = (eu_stufe == "Bärisch")

    bottom_us, bottom_eu, momentum_us, momentum_eu = bestimme_bottom_sektoren()
    us_tasks, eu_tasks = sammle_universum(bottom_us, bottom_eu)
    us_tickers = [t for t, _ in us_tasks]
    eu_tickers = [t for t, _ in eu_tasks]

    print("Hole US-Kursdaten (Sammel-Abruf)...")
    us_daten = fetch_us_batch(us_tickers)
    print("Hole EU-Kursdaten (Sammel-Abruf)...")
    eu_daten = fetch_eu_batch(eu_tickers)

    ergebnisse = []
    # Funnel-Statistik (NEU 28.07.2026): zaehlt je Ablehnungsstufe, wie viele
    # Ticker dort rausfallen - macht "0 Kandidaten" interpretierbar.
    funnel = Counter()
    funnel["keine_kursdaten"] = (
        sum(1 for t, _ in us_tasks if t not in us_daten)
        + sum(1 for t, _ in eu_tasks if t not in eu_daten)
    )
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(_pruefe_short_setup, t, s, "US", us_daten[t], spy_close, marktumfeld_baerisch_us, momentum_us.get(s))
            for t, s in us_tasks if t in us_daten
        ]
        for f in futures:
            r, grund = f.result()
            funnel[grund] += 1
            if r:
                ergebnisse.append(r)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(_pruefe_short_setup, t, s, "EU", eu_daten[t], eu_bench_close, marktumfeld_baerisch_eu, momentum_eu.get(s))
            for t, s in eu_tasks if t in eu_daten
        ]
        for f in futures:
            r, grund = f.result()
            funnel[grund] += 1
            if r:
                ergebnisse.append(r)

    print(f"DEBUG: {len(ergebnisse)} Short-Kandidaten gefunden.")

    # Funnel für Konsole + Briefing aufbereiten (Reihenfolge = Pruefstufen)
    gesamt_universum = len(us_tasks) + len(eu_tasks)
    funnel_stufen = [
        ("keine_kursdaten", "Keine Kursdaten geliefert (API)"),
        ("zu_wenig_daten", "Zu wenig Kurshistorie"),
        ("kein_abwaertstrend", "Kein Abwaertstrend (Kurs nicht unter WMA200)"),
        ("kein_setup_muster", "Keines der 4 gespiegelten Setup-Muster erfuellt"),
        ("macd_bullisch_ohne_divergenz", "Bullischer MACD ohne baerische Divergenz (widerspricht Short-These)"),
        ("rs_zu_stark", f"Relative Staerke > +{RS_MAX}% (Marktfuehrer werden nicht geshortet)"),
        ("crv_unter_1", "CRV-Filter (TP1 oder TP2 unter 1.0)"),
        ("kandidat", "KANDIDAT (vor Dedupe/Status-Split)"),
    ]
    funnel_zeilen = [f"Universum gesamt (Bottom-Sektoren): {gesamt_universum} Titel"]
    verbleibend = gesamt_universum
    for _key, _beschreibung in funnel_stufen:
        _anzahl = funnel.get(_key, 0)
        if _key == "kandidat":
            funnel_zeilen.append(f"=> KANDIDATEN: {_anzahl}")
        else:
            verbleibend -= _anzahl
            funnel_zeilen.append(f"- {_beschreibung}: -{_anzahl} (verbleiben {verbleibend})")
    funnel_text = "\n".join(funnel_zeilen)
    print("FUNNEL-STATISTIK:\n" + funnel_text)

    # Fundamental-Ampel (NEU, wie bei Setups.csv): nur für die finale, kleine
    # Kandidatenliste berechnen (API-schonend, siehe analyse.py-Vorbild)
    for r in ergebnisse:
        ampel, hinweis = berechne_fundamental_ampel(r["Ticker"], r["Sektor"], r["Markt"], richtung="short")
        r["Fundamental_Ampel"] = ampel
        r["Fundamental_Hinweis"] = hinweis

    # NEU (25.07.2026): Ticker-Deduplizierung - ein Ticker kann gleichzeitig
    # in mehreren Bottom-Sektoren gelistet sein (z.B. HUBS unter "Software"
    # UND "Cloud Computing"), wodurch derselbe Titel mit identischen
    # Handelswerten (Kurs/Einstieg/Stop/TP1/TP2/CRV, da rein technisch
    # ermittelt) aber unterschiedlichem Sektor-Label mehrfach im Ergebnis
    # auftauchte - fuer die Auswertung redundant, da man denselben Titel
    # nicht zweimal traden wuerde. Alle Sektor-Treffer werden jetzt zu einem
    # Eintrag zusammengefasst (Sektor-Feld als Komma-Liste), der erste
    # Treffer (samt seiner dort berechneten Fundamental-Ampel) bleibt
    # bestehen - die Ampel wurde bewusst VOR dem Zusammenfassen berechnet,
    # damit der Sektor-Median-Vergleich noch auf einem einzelnen, echten
    # Sektornamen basiert statt auf der kombinierten Komma-Liste.
    zusammengefasst = {}
    for r in ergebnisse:
        ticker = r["Ticker"]
        if ticker not in zusammengefasst:
            eintrag = dict(r)
            eintrag["_sektoren"] = [r["Sektor"]]
            zusammengefasst[ticker] = eintrag
        else:
            zusammengefasst[ticker]["_sektoren"].append(r["Sektor"])
    for eintrag in zusammengefasst.values():
        eintrag["Sektor"] = ", ".join(dict.fromkeys(eintrag.pop("_sektoren")))
    vor_dedup = len(ergebnisse)
    ergebnisse = list(zusammengefasst.values())
    if len(ergebnisse) < vor_dedup:
        print(f"DEBUG: {vor_dedup - len(ergebnisse)} doppelte Ticker zusammengefasst (mehrere Bottom-Sektoren) -> {len(ergebnisse)} Short-Kandidaten.")

    # Spaltenreihenfolge EXAKT wie Setups.csv (siehe analyse.py), Ticker
    # vorangestellt (fehlt dort, da index=False - hier bewusst behalten,
    # nuetzlicher Bezug), Abstand_52W_Hoch% -> Abstand_52W_Tief% (gespiegelt),
    # Setup_Qualitaet/Risikohinweis als Short-spezifische Zusatzfelder ans Ende.
    SPALTEN = [
        "Ticker", "Name", "Sektor", "Markt", "Waehrung", "Trend", "Setup_Typ", "Pattern",
        "Tech-Kursziel", "Analysten-Kursziel", "Upside_%_vs_Aktuell", "Status2", "Status_Grund",
        "RSI", "MACD_Trend", "CRV1", "CRV2", "Chance1_Perc", "Chance2_Perc", "Kurs",
        "Einstieg", "Einstieg2(EMA 20)", "Stop", "Risk_Perc", "TP1", "TP2", "Stoch_K",
        "Vol_Ratio", "Ideales_Delta", "RS_vs_Benchmark%", "Abstand_52W_Tief%", "Divergenz",
        "Golden_Cross_Status",
        "Fundamental_Ampel", "Fundamental_Hinweis", "Setup_Qualitaet", "Risikohinweis",
    ]
    df = pd.DataFrame(ergebnisse, columns=SPALTEN)
    if not df.empty:
        # Sortierung exakt wie in analyse.py (Setups.csv): erst Status2
        # (VALIDE vor ACHTUNG), dann Chance1_Perc absteigend (Pendant zu
        # Upside-Potenzial% beim Long-Scanner - der Prozentsatz bis zum
        # Tech-Kursziel/TP1), dann CRV1 absteigend.
        df['_status_order'] = df['Status2'].map({'VALIDE': 0, 'ACHTUNG': 1}).fillna(2)
        df = df.sort_values(by=['_status_order', 'Chance1_Perc', 'CRV1'], ascending=[True, False, False])
        df = df.drop(columns=['_status_order'])

    dateiname_csv = f"Short_Setups({today}).csv"
    df.to_csv(dateiname_csv, index=False, sep=';', encoding='utf-8-sig')
    print(f"Gespeichert: {dateiname_csv}")

    dateiname_briefing = f"Short_Briefing({today}).txt"
    with open(dateiname_briefing, "w", encoding="utf-8") as f:
        f.write(f"SHORT-SCAN {today}\n" + "=" * 50 + "\n\n")
        f.write("STRATEGIE-ANSATZ (Short, separat vom Trendfolge-/Trendwende-/Langfrist-Scanner)\n")
        f.write("-" * 50 + "\n")
        f.write("- Grundidee: Spiegelbild des Trendfolge-Scanners - setzt auf FALLENDE statt\n")
        f.write("  steigende Kurse (Put-Optionsscheine/KO statt Call).\n")
        f.write(f"- Universum: Bottom-{BOTTOM_SEKTOREN_US}-US- und Bottom-{BOTTOM_SEKTOREN_EU}-EU-Sektoren\n")
        f.write("  (schwaechste Rotation), nicht die Top-Sektoren wie beim Long-Scanner.\n")
        f.write("- Vier gespiegelte Muster: EMA-Breakdown, Pullback-Zone short, Trendlinien-\n")
        f.write("  Bruch, Kumo-Ausbruch nach unten (Details: siehe Gemini-Anleitung Abschnitt 9).\n")
        f.write(f"- RS-Filter invertiert: Titel mit RS vs. Benchmark > +{RS_MAX}% werden verworfen\n")
        f.write("  (nur Nachzuegler shorten, keine Marktfuehrer).\n")
        f.write("- Sektor-Modifikator (NEU 28.07.2026): Rotation-Score des Setup-Sektors <= -2,0\n")
        f.write("  -> +1 Stufe (Abwaerts-Rueckenwind) | Score > 0 -> -1 Stufe (Short gegen drehenden\n")
        f.write("  Sektor) | dazwischen neutral.\n")
        f.write("- Marktumfeld-Modifikator invertiert: baerisches Marktumfeld wertet die Setup-\n")
        f.write("  Qualitaet AUF (+1 Stufe), nicht ab wie beim Long-Scanner - seit 28.07.2026\n")
        f.write("  NUR NOCH mit Sektor-Bestaetigung (Rotation-Score des Setup-Sektors < 1,0).\n")
        f.write("- Heutiges Marktumfeld (Score-Modell wie Hauptscanner, seit 28.07.2026: Stufe je\n")
        f.write("  Index ueber EMA20/50/WMA200, Punkte Bullisch 2/Neutral 1/Baerisch 0, Gewichte\n")
        f.write("  S&P 500 x2/Nasdaq x1/Russell 2000 x1 bzw. DAX x2/EuroStoxx50 x1/STOXX 600 x1;\n")
        f.write("  Score >= 1,5 Bullisch | <= 0,5 Baerisch | sonst Neutral):\n")
        f.write(f"  US: {us_stufe} (Score {us_score}) | EU: {eu_stufe} (Score {eu_score}) - fuer den\n")
        f.write(f"  Aufwertungs-Modifikator zaehlt nur 'Baerisch' (US: {'JA' if marktumfeld_baerisch_us else 'nein'}, EU: {'JA' if marktumfeld_baerisch_eu else 'nein'}).\n")
        f.write("- RISIKOHINWEIS: Short-Positionen haben ein theoretisch unbegrenztes Verlust-\n")
        f.write("  risiko bei Kursanstieg (anders als Long, wo maximal der Einsatz verloren geht).\n")
        f.write("- Risiko: CRV (Chance/Risiko) muss bei TP1 und TP2 jeweils >= 1.0 sein (NEU,\n")
        f.write("  25.07.2026, analog zu Setups.csv/Edelmetalle_Setups.csv) - Ziele (TP1/TP2)\n")
        f.write("  kommen aus echten Abwaerts-Levels (Fib-Retracement nach unten, 52-Wochen-\n")
        f.write("  Tief, Pivot-Tiefs, Ichimoku-Wolke, ggf. gleitende Durchschnitte/runde Zahlen),\n")
        f.write("  nicht mehr aus einem pauschalen 8%/12,6%-Fallback - dadurch fallen an manchen\n")
        f.write("  Tagen ALLE Kandidaten durch dieses Kriterium (kein Fehler, siehe unten).\n")
        f.write("- Sektor-Momentum: NICHT in dieser Datei enthalten (genau wie bei Setups.csv) -\n")
        f.write("  wird aus Performance.csv/Performance_EU.csv per Sektor-Name nachgeschlagen (dort\n")
        f.write("  stehen ALLE Sektoren, nicht nur die Top-Sektoren, die Bottom-Sektoren sind also\n")
        f.write("  ebenfalls vorhanden).\n\n")

        # Funnel-Statistik (NEU 28.07.2026): macht insbesondere "0 Kandidaten"
        # interpretierbar - an welcher Pruefstufe faellt wie viel raus?
        f.write("FUNNEL-STATISTIK (Ablehnungsgruende je Pruefstufe)\n")
        f.write("-" * 50 + "\n")
        f.write(funnel_text + "\n")
        if BEINAHE_SHORT:
            f.write("\nBEINAHE-KANDIDATEN (Muster erfuellt, erst am CRV-Filter gescheitert)\n")
            f.write("-" * 50 + "\n")
            f.write("(nur Beobachtung, KEINE Setups)\n")
            # Leerzeile zwischen den Eintraegen (NEU 31.07.2026, Nutzerwunsch
            # "Uebersichtlichkeit") - analog zur Watchlist im Hauptscanner.
            for eintrag in sorted(BEINAHE_SHORT, key=lambda x: -x["crv_sortier"]):
                f.write(eintrag["text"] + "\n\n")
        if not df.empty:
            f.write(f"=> Nach Dedupe: {len(df)} | davon VALIDE: {int((df['Status2'] == 'VALIDE').sum())} | ACHTUNG: {int((df['Status2'] == 'ACHTUNG').sum())}\n")
        f.write("\n")

        if df.empty:
            f.write("Keine Short-Kandidaten gefunden.\n")
        else:
            for _, row in df.iterrows():
                f.write(
                    f"{row['Ticker']} ({row['Name']}) | Markt: {row['Markt']} | Sektor: {row['Sektor']} | Status: {row['Status2']} ({row['Status_Grund']})\n"
                    f"Kurs: {row['Kurs']}\n"
                    f"Technisches Kursziel: {row['Tech-Kursziel']} | Analysten-Kursziel: {row['Analysten-Kursziel'] if pd.notna(row['Analysten-Kursziel']) else 'N/A'}\n"
                    f"Stop: {row['Stop']} (oberhalb) | Risiko: {row['Risk_Perc']}%\n"
                    f"TP1: {row['TP1']} (Chance: {row['Chance1_Perc']}%) | CRV1: {row['CRV1']} | "
                    f"TP2: {row['TP2']} (Chance: {row['Chance2_Perc']}%) | CRV2: {row['CRV2']}\n"
                    f"RSI: {row['RSI']} | MACD-Trend: {row['MACD_Trend']} | Vol-Ratio: {row['Vol_Ratio']} | Divergenz: {row['Divergenz']}\n"
                    f"RS vs. Benchmark: {row['RS_vs_Benchmark%']}% | Abstand 52W-Tief: {row['Abstand_52W_Tief%']}%\n"
                    f"Fundamental-Ampel: {row['Fundamental_Ampel']} ({row['Fundamental_Hinweis']})\n"
                    f"Golden-/Death-Cross (nur Info, keine Bewertung): {row['Golden_Cross_Status']}\n"
                    f"Setup-Typ: {row['Setup_Typ']} | Setup-Qualitaet: [{row['Setup_Qualitaet']}] | Muster: {row['Pattern']}\n"
                )
                earnings = get_earnings_warnung(row['Ticker'])
                if earnings:
                    f.write(f"{earnings}\n")
                for headline in get_news_headlines(row['Ticker']):
                    f.write(f"News {headline}\n")
                f.write(f"Risikohinweis: {row['Risikohinweis']}\n\n")

    print(f"Gespeichert: {dateiname_briefing}")
    print("Short-Scanner abgeschlossen.")


if __name__ == "__main__":
    main()
