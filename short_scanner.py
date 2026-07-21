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
    get_fib_levels,
    clean_num,
    get_benchmark_close,
    get_eu_benchmark_close,
    check_rsi_divergence,
    get_earnings_warnung,
    get_news_headlines,
    get_ideal_delta,
    berechne_fundamental_ampel,
    get_golden_cross_status,
)
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


def check_pullback_zone_short(data):
    """Spiegelbild zu Pullback-Zone: Kurs testet EMA20/50/Kijun von UNTEN
    (Widerstandstest im Abwaertstrend), Lower-High bestaetigt (statt
    Higher-Low)."""
    if len(data) < 30:
        return False
    close = data['Close'].iloc[-1]
    zonen = [data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['Kijun'].iloc[-1]]
    in_zone = any(pd.notna(z) and 0 <= (z - close) / close <= 0.02 for z in zonen)
    if not in_zone:
        return False
    ilocs_max = argrelextrema(data['High'].tail(20).values, np.greater_equal, order=3)[0]
    if len(ilocs_max) < 2:
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
    if len(data) < 60:
        return None
    data = _indikatoren_berechnen(data)
    entry = data['Close'].iloc[-1]

    # Grundvoraussetzung (gespiegelt): Kurs UNTER WMA200 (Abwaertstrend)
    if pd.isna(data['WMA200'].iloc[-1]) or entry >= data['WMA200'].iloc[-1]:
        return None

    pfade = []
    trendlinien_bruch, _ = check_trendline_breakdown(data)
    if trendlinien_bruch:
        pfade.append("Trendlinien-Bruch")
    kumo_bruch, kumo_level = check_kumo_breakdown(data)
    if kumo_bruch:
        pfade.append("Kumo-Ausbruch unten")
    if check_ema_breakdown(data):
        pfade.append("EMA-Breakdown")
    pullback_short = check_pullback_zone_short(data)
    muster = check_bearish_confirmation(data)
    if pullback_short and muster:
        pfade.append("Pullback-Zone short")

    if not pfade:
        return None
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

    stufen = ["B-", "B", "B+", "A-", "A", "A+"]
    idx = stufen.index("B" if basis == "B" else "A")
    verschiebung = 0
    if data['Vol_Ratio'].iloc[-1] > 1.0:
        verschiebung += 1
    elif data['Vol_Ratio'].iloc[-1] < 0.5:
        verschiebung -= 1
    # Marktumfeld-Modifikator INVERTIERT (siehe Modul-Docstring): baerisches
    # Umfeld werted Short-Setups AUF statt ab
    if marktumfeld_baerisch:
        verschiebung += 1
    idx = max(0, min(len(stufen) - 1, idx + verschiebung))
    feinstufe = stufen[idx]

    # Status2/Status_Grund (NEU): ACHTUNG bei widersprüchlichem MACD (Bullisch
    # trotz Short-These) oder schwachem Volumen - AUSSER bärische Divergenz
    # validiert automatisch (gespiegelt zur Long-Logik in analyse.py).
    if divergenz_bearish:
        status2, status_grund = "VALIDE", "Alles ok"  # Divergenz steht separat in eigener Spalte (wie bei Setups.csv), nicht im Grund-Text
    elif data['MACD_Trend'].iloc[-1] == "Bullisch":
        status2, status_grund = "ACHTUNG", "Bullischer MACD-Trend (widerspricht Short-These)"
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
            return None

    # Stop OBERHALB, Ziele UNTERHALB (Widerstaende von oben werden zu Zielen)
    juengstes_hoch = round(float(data['High'].iloc[-10:].max()) * 1.02, 2)
    stop = juengstes_hoch
    risk_perc = round(((stop - entry) / entry) * 100, 2)

    fib1, fib2 = get_fib_levels(data)
    kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
    potenzial_targets = sorted(
        [v for v in [data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1],
                      data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte if pd.notna(v)],
        reverse=True,
    )
    targets_below = [t for t in potenzial_targets if t < entry]
    tp1 = targets_below[0] if targets_below else entry * 0.92
    tp2 = targets_below[1] if len(targets_below) >= 2 else tp1 * 0.95
    tech_kursziel = tp1  # analog zu analyse.py, wo Tech-Kursziel = TP1 gesetzt wird

    crv1 = round((entry - tp1) / (stop - entry), 2) if stop > entry else 0
    crv2 = round((entry - tp2) / (stop - entry), 2) if stop > entry else 0
    chance1_perc = round(((entry - tp1) / entry) * 100, 2)
    chance2_perc = round(((entry - tp2) / entry) * 100, 2)

    # Abstand_52W_Tief% (NEU, gespiegelt zu Abstand_52W_Hoch% bei Long):
    # wie weit über dem 52-Wochen-Tief - Raum, den der Kurs noch fallen
    # könnte, bevor der bisherige Tiefpunkt erreicht wird.
    tief_52w = data['Low'].min()
    abstand_52w_tief = round(((entry / tief_52w) - 1) * 100, 2) if tief_52w > 0 else None

    try:
        info = yf.Ticker(ticker).info
        firma_name = info.get('longName', ticker) or ticker
        analysten_kursziel = info.get('targetMeanPrice')
    except Exception:
        firma_name = ticker
        analysten_kursziel = None

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
    }


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

    # Grobe Marktumfeld-Einschaetzung fuer den Modifikator: baerisch, wenn
    # der Benchmark unter seinem eigenen EMA20 liegt (gleiche Logik wie im
    # Long-Scanner-Marktumfeld-Fazit, nur hier fuer den Modifikator genutzt)
    marktumfeld_baerisch_us = bool(len(spy_close) > 20 and spy_close.iloc[-1] < spy_close.ewm(span=20, adjust=False).mean().iloc[-1])
    marktumfeld_baerisch_eu = bool(len(eu_bench_close) > 20 and eu_bench_close.iloc[-1] < eu_bench_close.ewm(span=20, adjust=False).mean().iloc[-1])

    bottom_us, bottom_eu, momentum_us, momentum_eu = bestimme_bottom_sektoren()
    us_tasks, eu_tasks = sammle_universum(bottom_us, bottom_eu)
    us_tickers = [t for t, _ in us_tasks]
    eu_tickers = [t for t, _ in eu_tasks]

    print("Hole US-Kursdaten (Sammel-Abruf)...")
    us_daten = fetch_us_batch(us_tickers)
    print("Hole EU-Kursdaten (Sammel-Abruf)...")
    eu_daten = fetch_eu_batch(eu_tickers)

    ergebnisse = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(_pruefe_short_setup, t, s, "US", us_daten[t], spy_close, marktumfeld_baerisch_us, momentum_us.get(s))
            for t, s in us_tasks if t in us_daten
        ]
        for f in futures:
            r = f.result()
            if r:
                ergebnisse.append(r)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(_pruefe_short_setup, t, s, "EU", eu_daten[t], eu_bench_close, marktumfeld_baerisch_eu, momentum_eu.get(s))
            for t, s in eu_tasks if t in eu_daten
        ]
        for f in futures:
            r = f.result()
            if r:
                ergebnisse.append(r)

    print(f"DEBUG: {len(ergebnisse)} Short-Kandidaten gefunden.")

    # Fundamental-Ampel (NEU, wie bei Setups.csv): nur für die finale, kleine
    # Kandidatenliste berechnen (API-schonend, siehe analyse.py-Vorbild)
    for r in ergebnisse:
        ampel, hinweis = berechne_fundamental_ampel(r["Ticker"])
        r["Fundamental_Ampel"] = ampel
        r["Fundamental_Hinweis"] = hinweis

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
        f.write("- Marktumfeld-Modifikator invertiert: baerisches Marktumfeld wertet die Setup-\n")
        f.write("  Qualitaet AUF (+1 Stufe), nicht ab wie beim Long-Scanner.\n")
        f.write(f"- Heutiges Marktumfeld: US {'baerisch' if marktumfeld_baerisch_us else 'nicht baerisch'}, "
                f"EU {'baerisch' if marktumfeld_baerisch_eu else 'nicht baerisch'} (Basis fuer den Modifikator oben).\n")
        f.write("- RISIKOHINWEIS: Short-Positionen haben ein theoretisch unbegrenztes Verlust-\n")
        f.write("  risiko bei Kursanstieg (anders als Long, wo maximal der Einsatz verloren geht).\n")
        f.write("- Sektor-Momentum: NICHT in dieser Datei enthalten (genau wie bei Setups.csv) -\n")
        f.write("  wird aus Performance.csv/Performance_EU.csv per Sektor-Name nachgeschlagen (dort\n")
        f.write("  stehen ALLE Sektoren, nicht nur die Top-Sektoren, die Bottom-Sektoren sind also\n")
        f.write("  ebenfalls vorhanden).\n\n")

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
