"""
trendwende_scanner.py

Separater Scanner fuer Trendwende-Kandidaten (Boden-Suche nach einem Fall),
als eigenstaendiger Workflow-Schritt neben dem bewaehrten Trendfolge-Scanner
(analyse.py). Bewusst als eigenes Skript, eigene Datei, eigener Briefing-
Abschnitt - siehe Architektur-Entscheidung vom 19.07.2026:
  - andere Grundannahme (Boden statt Fortsetzung) => eigene Filterlogik statt
    einer gemeinsamen if-Kaskade mit dem Hauptscanner
  - eigene, hoehere Risikoklasse => eigenes Label + Risikohinweis
  - unabhaengig testbar/abschaltbar => eigener Workflow-Schritt, greift NICHT
    in analyse.py ein und wird nur importiert (Ichimoku/Kumo/RSI-Bausteine)
  - eigenes, breiteres Universum (nicht auf Top-Sektoren beschraenkt)

Kriterien (Stand 19.07.2026, aus gemeinsamer Abstimmung A-F):
  A - Universum: komplettes Sektoren-Universum (alle US- + EU-Sektoren, nicht
      nur die taeglichen Top-Sektoren) UND zusaetzlich gefiltert auf Naehe
      zum 52-Wochen-Tief.
  B - Strenge: ausgewogen (siehe ABSTAND_52W_TIEF_MAX unten - moderat, nicht
      nur exakte neue Tiefs).
  C - Wende-Bestaetigung: bullische RSI-Divergenz UND Kumo-Ausbruch MUESSEN
      beide vorliegen, UND beide muessen innerhalb der letzten 3 Handelstage
      aufgetreten sein (kein "alter" Signalstand).
  D - Kennzeichnung: eigenes Label "Trendwende-Setup" + eigene Risikohinweis-
      Spalte, klar getrennt von den normalen Trendfolge-Setups.
  E - Workflow: taeglich, eigener Schritt NACH dem Hauptscanner (siehe
      main.yml), eigene Datei (Trendwende_Setups.csv) + eigener Briefing-
      Abschnitt (Trendwende_Briefing.txt).
  F - Stop: enger, wende-spezifisch - knapp unter dem juengsten Verlaufstief
      (nicht das 10-Tage-Tief des Hauptscanners, das bei Trendwenden oft zu
      weit weg liegt).

Architektur-Update (19.07.2026): Kursdaten werden per SAMMEL-ABRUF geholt
(mehrere Ticker pro API-Request statt einem Request pro Ticker) - dadurch
kein festes Ticker-Budget mehr noetig, das komplette Universum wird jeden
Tag vollstaendig abgedeckt (vorher wurde bei zu vielen Tickern u.a. die
komplette EU-Seite stillschweigend uebersprungen).

Voraussetzungen: dieselben Umgebungsvariablen wie analyse.py
(ALPACA_KEY, ALPACA_SECRET). Muss im selben Verzeichnis wie analyse.py
liegen (wird importiert).
"""

import datetime
import numpy as np
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
from scipy.signal import argrelextrema

# --- Bewaehrte Bausteine aus dem Hauptscanner wiederverwenden ---
from analyse import (
    alpaca_client,
    sektoren_aktien,
    dax_aktien,
    check_kumo_breakout,
    check_bullish_confirmation,
    get_fib_levels,
    clean_num,
    get_benchmark_close,
    get_eu_benchmark_close,
)
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ---------------------------------------------------------------------------
# KONFIGURATION (B - Strenge, hier zentral einstellbar)
# ---------------------------------------------------------------------------

# Wie nah am 52-Wochen-Tief ein Kandidat maximal noch sein darf, um als
# "Trendwende-Kandidat" zu gelten (ausgewogen: nicht nur exakte Tiefs, aber
# auch keine Werte, die schon weit vom Tief weggelaufen sind).
ABSTAND_52W_TIEF_MAX = 10.0  # Prozent oberhalb des 52-Wochen-Tiefs

# Zeitfenster fuer "frisches" Signal (C - beide Bestaetigungen muessen
# innerhalb dieser letzten N Handelstage aufgetreten sein)
FRISCHE_TAGE = 3

# Chunk-Groesse fuer Sammel-Abrufe (Alpaca/yfinance koennen mehrere Ticker in
# einem Request abfragen - das ersetzt die 370-440 einzelnen API-Calls von
# vorher durch nur eine Handvoll Sammel-Calls, siehe fetch_us_batch/
# fetch_eu_batch unten. Kein festes Ticker-Budget mehr noetig, da dadurch
# die Rate-Limit-Sorge von vorher entfaellt - Chunking hier nur als
# Sicherheitsnetz gegen zu lange einzelne Requests.
CHUNK_SIZE = 100

STOP_PUFFER = 0.98  # 2% Puffer unter dem juengsten Verlaufstief


# ---------------------------------------------------------------------------
# ZUSATZ-BAUSTEIN: RSI-Divergenz mit Frische-Pruefung
# ---------------------------------------------------------------------------

def check_rsi_divergence_recent(data, frische_tage=FRISCHE_TAGE):
    """Wie check_rsi_divergence in analyse.py, aber zusaetzlich mit Pflicht,
    dass der juengste lokale Tiefpunkt (der fuer die Divergenz herangezogene
    Bodenpunkt) innerhalb der letzten `frische_tage` Handelstage liegt - sonst
    waere die Divergenz schon "alt" und kein aktuelles Wende-Signal mehr.
    Gibt True/False zurueck (nur bullische Divergenz relevant fuer diesen
    Scanner, da wir ausschliesslich nach Boeden suchen)."""
    df = data.tail(40)
    ilocs_min = argrelextrema(df['Close'].values, np.less_equal, order=5)[0]

    if len(ilocs_min) < 2:
        return False

    letzter_tiefpunkt_idx = ilocs_min[-1]
    ist_frisch = letzter_tiefpunkt_idx >= (len(df) - 1 - frische_tage)
    if not ist_frisch:
        return False

    bullische_divergenz = (
        df['Close'].iloc[ilocs_min[-1]] < df['Close'].iloc[ilocs_min[-2]]
    ) and (
        df['RSI'].iloc[ilocs_min[-1]] > df['RSI'].iloc[ilocs_min[-2]]
    )
    return bool(bullische_divergenz)


def check_stochastik_crossover_recent(data, frische_tage=FRISCHE_TAGE, ueberverkauft_schwelle=20):
    """Qualitaets-Bonus-Signal: prueft, ob innerhalb der letzten `frische_tage`
    Handelstage ein Stochastik-Crossover (%K kreuzt %D von unten) stattfand,
    UND die Stochastik dabei aus der ueberverkauften Zone (< 20) kam - klassisches
    Bottom-Fishing-Signal, unabhaengig von RSI-Divergenz und Kumo-Ausbruch
    berechnet (andere Grundlage: Kurslage im 14-Tage-Hoch/Tief-Bereich statt
    Preis-Momentum-Vergleich bzw. Ichimoku-Wolke)."""
    if len(data) < 20 or 'Stoch_K' not in data.columns:
        return False

    for i in range(0, frische_tage + 1):
        idx = -1 - i
        idx_prev = idx - 1
        if abs(idx_prev) > len(data):
            break
        k_heute, d_heute = data['Stoch_K'].iloc[idx], data['Stoch_D'].iloc[idx]
        k_gestern, d_gestern = data['Stoch_K'].iloc[idx_prev], data['Stoch_D'].iloc[idx_prev]
        if pd.isna(k_heute) or pd.isna(d_heute) or pd.isna(k_gestern) or pd.isna(d_gestern):
            continue
        crossover = (k_heute > d_heute) and (k_gestern <= d_gestern)
        aus_ueberverkauft = (k_gestern < ueberverkauft_schwelle) or (k_heute < ueberverkauft_schwelle)
        if crossover and aus_ueberverkauft:
            return True
    return False


def juengstes_verlaufstief(data, fenster=10):
    """F - wende-spezifischer Stop: juengstes markantes Tief der letzten
    `fenster` Kerzen, mit kleinem Sicherheitspuffer darunter (statt des
    10-Tage-Tiefs / 5-Kerzen-Tiefs aus dem Hauptscanner, das bei
    Trendwenden oft weit vom aktuellen Kurs entfernt liegt)."""
    tief = data['Low'].iloc[-fenster:].min()
    return round(float(tief) * STOP_PUFFER, 2)


def _chunks(liste, groesse):
    for i in range(0, len(liste), groesse):
        yield liste[i:i + groesse]


def fetch_us_batch(ticker_liste):
    """Holt Kursdaten fuer ALLE US-Ticker in wenigen Sammel-Requests statt
    einem Request pro Ticker (Alpaca unterstuetzt mehrere Symbole pro
    StockBarsRequest). Gibt {ticker: DataFrame} zurueck - fehlende/leere
    Ticker werden einfach ausgelassen (kein Fehler)."""
    ergebnis = {}
    start_date = datetime.datetime.now() - datetime.timedelta(days=365)

    for chunk in _chunks(ticker_liste, CHUNK_SIZE):
        try:
            request = StockBarsRequest(
                symbol_or_symbols=chunk, start=start_date, timeframe=TimeFrame.Day
            )
            bars = alpaca_client.get_stock_bars(request)
            df_alle = bars.df
        except Exception as e:
            print(f"FEHLER beim Sammel-Abruf US-Chunk ({len(chunk)} Ticker): {e}")
            continue

        if df_alle.empty:
            continue

        # MultiIndex (symbol, timestamp) bei mehreren Symbolen - pro Ticker
        # aufsplitten, Spalten wie beim Hauptscanner umbenennen.
        for ticker in chunk:
            try:
                data = df_alle.loc[ticker].copy()
            except KeyError:
                continue
            if data.empty:
                continue
            if 'close' in data.columns:
                data = data.rename(columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 'volume': 'Volume'})
            ergebnis[ticker] = data

    print(f"DEBUG: US-Sammel-Abruf lieferte Daten fuer {len(ergebnis)}/{len(ticker_liste)} Ticker.")
    return ergebnis


def fetch_eu_batch(ticker_liste):
    """Holt Kursdaten fuer ALLE EU-Ticker in wenigen Sammel-Requests statt
    einem Request pro Ticker (yf.download akzeptiert mehrere Ticker auf
    einmal). Gibt {ticker: DataFrame} zurueck."""
    ergebnis = {}

    for chunk in _chunks(ticker_liste, CHUNK_SIZE):
        try:
            df_alle = yf.download(
                tickers=" ".join(chunk), period="1y", group_by='ticker',
                threads=True, auto_adjust=False, progress=False
            )
        except Exception as e:
            print(f"FEHLER beim Sammel-Abruf EU-Chunk ({len(chunk)} Ticker): {e}")
            continue

        if df_alle.empty:
            continue

        for ticker in chunk:
            try:
                # Bei mehreren Tickern liefert yfinance ein MultiIndex-
                # Spaltenformat (Ticker, Feld) - bei genau einem Ticker im
                # letzten Chunk waere es flach, daher der Fallback.
                if isinstance(df_alle.columns, pd.MultiIndex):
                    data = df_alle[ticker].copy()
                else:
                    data = df_alle.copy()
            except KeyError:
                continue
            data = data.dropna(subset=['Close', 'High', 'Low', 'Volume'])
            if data.empty:
                continue
            ergebnis[ticker] = data

    print(f"DEBUG: EU-Sammel-Abruf lieferte Daten fuer {len(ergebnis)}/{len(ticker_liste)} Ticker.")
    return ergebnis


# ---------------------------------------------------------------------------
# KERNLOGIK: EIN TICKER (Daten kommen bereits aus dem Sammel-Abruf, kein
# weiterer Netzwerk-Call noetig - die Filterung selbst ist reine lokale
# Pandas-Berechnung und damit fuer das komplette Universum unproblematisch)
# ---------------------------------------------------------------------------

def _indikatoren_berechnen(data):
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

    # Ichimoku Kumo-Grenzen (fuer check_kumo_breakout)
    data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
    data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
    data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)
    data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

    # Stochastik (14,3,3) - fuer den optionalen Qualitaets-Bonus
    # "frischer Crossover aus ueberverkaufter Zone" (siehe
    # check_stochastik_crossover_recent unten)
    low_min = data['Low'].rolling(14).min()
    high_max = data['High'].rolling(14).max()
    data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))
    data['Stoch_D'] = data['Stoch_K'].rolling(3).mean()

    return data


def _pruefe_trendwende(ticker, sektor, markt, data, bench_close=None):
    if len(data) < 60:
        return None

    data = _indikatoren_berechnen(data)
    entry = data['Close'].iloc[-1]

    # A/B - Grundvoraussetzung: Kurs UNTER WMA200 (gefallen, nicht im
    # Aufwaertstrend - Gegenteil des Hauptscanner-Filters) UND nahe am
    # 52-Wochen-Tief.
    if pd.isna(data['WMA200'].iloc[-1]) or entry >= data['WMA200'].iloc[-1]:
        return None

    tief_52w = data['Low'].min()
    abstand_52w_tief = round(((entry / tief_52w) - 1) * 100, 2)
    if abstand_52w_tief > ABSTAND_52W_TIEF_MAX:
        return None

    # C - beide Bestaetigungen Pflicht, beide muessen frisch sein
    divergenz_ok = check_rsi_divergence_recent(data)
    kumo_ausbruch, kumo_level = check_kumo_breakout(data)

    if not (divergenz_ok and kumo_ausbruch):
        print(f"DEBUG-TRENDWENDE-VERWORFEN: {ticker} | Divergenz: {divergenz_ok} | "
              f"Kumo-Ausbruch: {kumo_ausbruch} | Abstand 52W-Tief: {abstand_52w_tief}%")
        return None

    # Qualitaets-Bonus (NEU, optional - kein Ausschlusskriterium): zwei
    # zusaetzliche, unabhaengige Signale koennen die Einstufung anheben,
    # sind aber NICHT Pflicht wie RSI-Divergenz/Kumo-Ausbruch. Bewusst als
    # eigene, von der Setup-Qualitaets-Skala des Hauptscanners (B-/A+)
    # visuell unterschiedliche Bezeichnung, um die strikte Trennung der
    # beiden Setup-Kategorien nicht zu verwischen (siehe Abschnitt 7 der
    # Gemini-Anleitung).
    candlestick_muster = check_bullish_confirmation(data)  # "Hammer"/"Engulfing"/None
    stoch_crossover = check_stochastik_crossover_recent(data)

    bonus_komponenten = []
    if candlestick_muster:
        bonus_komponenten.append(candlestick_muster)
    if stoch_crossover:
        bonus_komponenten.append("Stochastik-Crossover")

    anzahl_bonus = len(bonus_komponenten)
    if anzahl_bonus == 0:
        qualitaets_bonus = "Basis"
    elif anzahl_bonus == 1:
        qualitaets_bonus = "Bestätigt"
    else:
        qualitaets_bonus = "Stark bestätigt"

    setup_typ = "RSI-Divergenz + Kumo-Ausbruch"
    if bonus_komponenten:
        setup_typ += " + " + " + ".join(bonus_komponenten)

    # Relative Staerke (nur Info, kein Ausschlusskriterium wie beim
    # Hauptscanner - bei Trendwenden ist schwache RS gegenueber dem Markt
    # ja gerade der Ausgangspunkt)
    rel_staerke = None
    if bench_close is not None and len(bench_close) > 60 and len(data) > 60:
        stock_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
        bench_perf_60 = ((bench_close.iloc[-1] / bench_close.iloc[-60]) - 1) * 100
        rel_staerke = round(stock_perf_60 - bench_perf_60, 2)

    # F - wende-spezifischer, engerer Stop
    stop = juengstes_verlaufstief(data)
    risk_perc = round(((entry - stop) / entry) * 100, 2)

    # TP-Kandidaten: naechste Widerstaende oberhalb (EMAs, Fib-Extension,
    # Kumo-Obergrenze) - gleiche Logik wie im Hauptscanner, unabhaengig von
    # der Trend-Richtung gueltig.
    fib1, fib2 = get_fib_levels(data)
    kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
    potenzial_targets = sorted(
        [data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1],
         data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte
    )
    targets_above = [t for t in potenzial_targets if t > entry]
    tp1 = targets_above[0] if targets_above else entry * 1.08
    tp2 = targets_above[1] if len(targets_above) >= 2 else tp1 * 1.05

    crv1 = round((tp1 - entry) / (entry - stop), 2) if entry > stop else 0
    crv2 = round((tp2 - entry) / (entry - stop), 2) if entry > stop else 0
    chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
    chance2_perc = round(((tp2 - entry) / entry) * 100, 2)

    try:
        firma_name = yf.Ticker(ticker).info.get('longName', ticker) or ticker
    except Exception:
        firma_name = ticker

    return {
        "Ticker": ticker,
        "Name": firma_name,
        "Markt": markt,
        "Sektor": sektor,
        "Kurs": round(clean_num(entry), 2),
        "TP1": round(clean_num(tp1), 2),
        "CRV1": crv1,
        "Chance1_Perc": chance1_perc,
        "TP2": round(clean_num(tp2), 2),
        "CRV2": crv2,
        "Chance2_Perc": chance2_perc,
        "Stop": stop,
        "Risk_Perc": risk_perc,
        "RSI": round(clean_num(data['RSI'].iloc[-1]), 2),
        "MACD_Trend": data['MACD_Trend'].iloc[-1],
        "Vol_Ratio": round(clean_num(data['Vol_Ratio'].iloc[-1]), 2),
        "RS_vs_Benchmark%": rel_staerke,
        "Abstand_52W_Tief%": abstand_52w_tief,
        "Setup_Typ": f"Trendwende ({setup_typ})",
        "Qualitaets_Bonus": qualitaets_bonus,
        "Risikohinweis": (
            "Trendwende-Setup - strukturell riskanter als Trendfolge-Setups "
            "(\u201eMesser-Gefahr\u201c). Enger, wende-spezifischer Stop - Positionsgroesse entsprechend anpassen."
        ),
    }


def analyze_trendwende_us(ticker, sektor, data, spy_close=None):
    try:
        return _pruefe_trendwende(ticker, sektor, "US", data, spy_close)
    except Exception as e:
        print(f"FEHLER Trendwende US {ticker}: {e}")
        return None


def analyze_trendwende_eu(ticker, sektor, data, eu_bench_close=None):
    try:
        return _pruefe_trendwende(ticker, sektor, "EU", data, eu_bench_close)
    except Exception as e:
        print(f"FEHLER Trendwende EU {ticker}: {e}")
        return None


# ---------------------------------------------------------------------------
# HAUPTPROGRAMM
# ---------------------------------------------------------------------------

def sammle_universum():
    """A - komplettes Universum: ALLE Sektoren (nicht nur Top-Rotation),
    dedupliziert. Gibt (us_tasks, eu_tasks) als Listen von (Ticker, Sektor).
    Kein Budget-Limit mehr noetig, da die Kursdaten per Sammel-Abruf geholt
    werden (siehe fetch_us_batch/fetch_eu_batch) - das eigentliche
    Rate-Limit-Risiko waren die vielen EINZELNEN Requests, nicht die
    Ticker-Anzahl an sich."""
    us_tasks = []
    gesehen_us = set()
    for sektor_ticker, aktien in sektoren_aktien.items():
        for ticker in aktien:
            if ticker not in gesehen_us:
                gesehen_us.add(ticker)
                us_tasks.append((ticker, sektor_ticker))

    eu_tasks = []
    gesehen_eu = set()
    for sektor_name, aktien in dax_aktien.items():
        for ticker in aktien:
            if ticker not in gesehen_eu:
                gesehen_eu.add(ticker)
                eu_tasks.append((ticker, sektor_name))

    print(f"DEBUG: Trendwende-Universum -> US: {len(us_tasks)} | EU: {len(eu_tasks)} | Gesamt: {len(us_tasks) + len(eu_tasks)}")
    return us_tasks, eu_tasks


def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    print("Trendwende-Scanner gestartet...")

    spy_close = get_benchmark_close()
    eu_bench_close = get_eu_benchmark_close()

    us_tasks, eu_tasks = sammle_universum()
    us_tickers = [t for t, _ in us_tasks]
    eu_tickers = [t for t, _ in eu_tasks]

    print("Hole US-Kursdaten (Sammel-Abruf)...")
    us_daten = fetch_us_batch(us_tickers)
    print("Hole EU-Kursdaten (Sammel-Abruf)...")
    eu_daten = fetch_eu_batch(eu_tickers)

    ergebnisse = []
    print("Starte Trendwende-Analyse (US)...")
    # Ab hier reine lokale Berechnung (Daten liegen bereits vor) - Threads
    # dienen hier nur noch der CPU-Parallelisierung, nicht mehr dem
    # Kaschieren von Netzwerk-Latenz wie vorher.
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(analyze_trendwende_us, t, s, us_daten[t], spy_close)
            for t, s in us_tasks if t in us_daten
        ]
        for f in futures:
            r = f.result()
            if r:
                ergebnisse.append(r)

    print("Starte Trendwende-Analyse (EU)...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(analyze_trendwende_eu, t, s, eu_daten[t], eu_bench_close)
            for t, s in eu_tasks if t in eu_daten
        ]
        for f in futures:
            r = f.result()
            if r:
                ergebnisse.append(r)

    print(f"DEBUG: {len(ergebnisse)} Trendwende-Kandidaten gefunden.")

    # Spalten fest vorgeben (NEU): bei 0 Treffern ist ergebnisse=[] - ohne
    # explizite Spaltenliste entsteht dann eine DataFrame KOMPLETT OHNE
    # Spalten (nicht nur ohne Zeilen), was eine praktisch leere 4-Byte-CSV-
    # Datei ohne Kopfzeile erzeugt. Google Drive kann so eine Datei nicht als
    # Tabelle rendern ("Vorschau konnte nicht angezeigt werden"). Mit fester
    # Spaltenliste bleibt die Kopfzeile auch bei 0 Treffern erhalten.
    SPALTEN_TRENDWENDE = [
        "Ticker", "Name", "Markt", "Sektor", "Kurs", "TP1", "CRV1", "Chance1_Perc",
        "TP2", "CRV2", "Chance2_Perc",
        "Stop", "Risk_Perc", "RSI", "MACD_Trend", "Vol_Ratio", "RS_vs_Benchmark%",
        "Abstand_52W_Tief%", "Setup_Typ", "Qualitaets_Bonus", "Risikohinweis",
    ]
    df = pd.DataFrame(ergebnisse, columns=SPALTEN_TRENDWENDE)
    if not df.empty:
        bonus_rang = {"Stark bestätigt": 0, "Bestätigt": 1, "Basis": 2}
        df['_bonus_rang'] = df['Qualitaets_Bonus'].map(bonus_rang).fillna(3)
        df = df.sort_values(by=["_bonus_rang", "CRV1"], ascending=[True, False]).drop(columns=['_bonus_rang'])

    # E - eigene Datei
    dateiname_csv = f"Trendwende_Setups({today}).csv"
    df.to_csv(dateiname_csv, index=False, sep=';', encoding='utf-8-sig')
    print(f"Gespeichert: {dateiname_csv}")

    # E - eigener Briefing-Abschnitt (separate Datei, wird beim Gemini-Schritt
    # zusaetzlich zu den vier bestehenden Dateien mit hochgeladen)
    dateiname_briefing = f"Trendwende_Briefing({today}).txt"
    with open(dateiname_briefing, "w", encoding="utf-8") as f:
        f.write(f"TRENDWENDE-SCAN {today}\n" + "=" * 50 + "\n\n")

        # Trading-Idee ausfuehrlich beschreiben (analog zum STRATEGIE-ANSATZ-
        # Block im Haupt-Briefing von analyse.py), damit sowohl beim Lesen
        # der Datei als auch fuer die Gemini-Auswertung klar ist, wonach hier
        # gesucht wird und warum das etwas anderes ist als die normalen Setups.
        f.write("STRATEGIE-ANSATZ (Trendwende, separat vom Hauptscanner)\n")
        f.write("-" * 50 + "\n")
        f.write("- Grundidee: Gegenteil des Hauptscanners. Der Hauptscanner sucht Fortsetzung\n")
        f.write("  etablierter Aufwaertstrends (\"laeuft schon\"). Dieser Scan sucht stattdessen\n")
        f.write("  den Boden nach einem Fall - also Titel, die gefallen sind und erste\n")
        f.write("  Anzeichen einer Trendwende zeigen.\n")
        f.write("- Universum: KOMPLETTES Sektoren-Universum (alle US- + EU-Sektoren), nicht nur\n")
        f.write("  die taeglichen Top-Rotations-Sektoren des Hauptscanners - Wende-Kandidaten\n")
        f.write("  liegen typischerweise in aktuell schwachen, nicht in starken Sektoren.\n")
        f.write(f"- Trend-Filter (umgekehrt zum Hauptscanner): Kurs muss UNTER der WMA200 liegen.\n")
        f.write(f"- Naehe zum Tief: Kurs darf max. {ABSTAND_52W_TIEF_MAX}% ueber seinem 52-Wochen-Tief\n")
        f.write("  liegen (ausgewogene Schwelle - nicht nur exakte neue Tiefs, aber auch keine\n")
        f.write("  Titel, die schon deutlich vom Tief weggelaufen sind).\n")
        f.write(f"- Wende-Bestaetigung (BEIDE Pflicht, kein ODER): bullische RSI-Divergenz\n")
        f.write("  (Kurs macht neues Tief, RSI aber nicht - Verkaufsdruck laesst nach) UND\n")
        f.write("  Kumo-Ausbruch (Kurs durchbricht die komplette Ichimoku-Wolke nach oben -\n")
        f.write(f"  erste technische Trendwechsel-Bestaetigung). Beide Signale muessen innerhalb\n")
        f.write(f"  der letzten {FRISCHE_TAGE} Handelstage aufgetreten sein, sonst gilt das Signal als veraltet.\n")
        f.write("- Qualitaets-Bonus (optional, NICHT Pflicht): zwei zusaetzliche Signale koennen\n")
        f.write("  die Einstufung anheben, sind aber kein Ausschlusskriterium wie die beiden\n")
        f.write("  Pflicht-Signale oben - Candlestick-Bestaetigung (Hammer/Engulfing auf der\n")
        f.write("  aktuellen Kerze) und ein frischer Stochastik-Crossover (%K kreuzt %D von unten,\n")
        f.write(f"  aus der ueberverkauften Zone < 20, innerhalb der letzten {FRISCHE_TAGE} Handelstage).\n")
        f.write("  Einstufung: 0 Bonus-Signale = 'Basis', 1 = 'Bestaetigt', 2 = 'Stark bestaetigt'.\n")
        f.write(f"- Stop (enger als beim Hauptscanner): juengstes markantes Verlaufstief der\n")
        f.write(f"  letzten 10 Kerzen, minus {int((1 - STOP_PUFFER) * 100)}% Sicherheitspuffer - bewusst enger als das\n")
        f.write("  10-Tage-Tief des Hauptscanners, da bei Trendwenden der \"alte\" Boden oft weit\n")
        f.write("  vom aktuellen Kurs entfernt liegt.\n")
        f.write("- Ziel (TP1/TP2): naechste Widerstaende oberhalb des aktuellen Kurses (EMA-\n")
        f.write("  Linien, Fibonacci-Extension, Kumo-Obergrenze) - gleiche Logik wie beim\n")
        f.write("  Hauptscanner, nur unabhaengig von der Trendrichtung angewendet.\n")
        f.write("- RISIKOKLASSE: Strukturell riskanter als die normalen Trendfolge-Setups\n")
        f.write("  (\"Messer-Gefahr\" - ein fallendes Messer kann trotz Divergenz/Ausbruch weiter\n")
        f.write("  fallen). Deshalb eigene Datei, eigener Abschnitt, eigenes Label - bewusst\n")
        f.write("  NICHT mit den \"sicheren\" Trendfolge-Setups vermischt.\n\n")

        if df.empty:
            f.write("Keine Trendwende-Kandidaten gefunden.\n")
        else:
            for _, row in df.iterrows():
                f.write(
                    f"{row['Ticker']} ({row['Name']}) | Markt: {row['Markt']} | Sektor: {row['Sektor']}\n"
                    f"Kurs: {row['Kurs']} | Stop: {row['Stop']} | Risiko: {row['Risk_Perc']}%\n"
                    f"TP1: {row['TP1']} (Chance: {row['Chance1_Perc']}%) | CRV1: {row['CRV1']} | TP2: {row['TP2']} (Chance: {row['Chance2_Perc']}%) | CRV2: {row['CRV2']}\n"
                    f"RSI: {row['RSI']} | MACD-Trend: {row['MACD_Trend']} | Vol-Ratio: {row['Vol_Ratio']}\n"
                    f"Abstand 52W-Tief: {row['Abstand_52W_Tief%']}% | RS vs. Benchmark: {row['RS_vs_Benchmark%']}%\n"
                    f"Setup-Typ: {row['Setup_Typ']}\n"
                    f"Qualitäts-Bonus: {row['Qualitaets_Bonus']}\n"
                    f"Risikohinweis: {row['Risikohinweis']}\n\n"
                )

    print(f"Gespeichert: {dateiname_briefing}")
    print("Trendwende-Scanner abgeschlossen.")


if __name__ == "__main__":
    main()
