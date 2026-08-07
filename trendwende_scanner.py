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
  C - Wende-Bestaetigung (GEAENDERT 28.07.2026, zeitlich entkoppelte Sequenz):
      bullische RSI-Divergenz UND Kumo-Ausbruch MUESSEN beide vorliegen,
      aber mit UNTERSCHIEDLICHEN Zeitfenstern:
        1) Boden-Bedingung: RSI-Divergenz innerhalb der letzten
           DIVERGENZ_FENSTER_TAGE Handelstage, seitdem NICHT invalidiert
           (kein tieferer Schlusskurs nach dem Divergenz-Tief).
        2) Trigger: frischer Kumo-Ausbruch (komplette Wolke) innerhalb der
           letzten FRISCHE_TAGE Handelstage.
      Begruendung: Divergenz entsteht AM Boden, der Kumo-Ausbruch folgt
      naturgemaess erst Tage/Wochen spaeter - ein gemeinsames 3-5-Tage-
      Fenster war strukturell fast nie erfuellbar (Log 24./28.07.2026:
      Divergenz- und Ausbruch-Gruppe ueber alle Ticker komplett disjunkt).
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
import math
import re
from collections import Counter
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
    check_bullish_confirmation,
    get_fib_levels,
    clean_num,
    get_benchmark_close,
    get_eu_benchmark_close,
    berechne_fundamental_ampel,
    get_earnings_warnung,
    get_news_headlines,
)
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ---------------------------------------------------------------------------
# KONFIGURATION (B - Strenge, hier zentral einstellbar)
# ---------------------------------------------------------------------------

# Wie nah am 52-Wochen-Tief ein Kandidat maximal noch sein darf, um als
# "Trendwende-Kandidat" zu gelten (ausgewogen: nicht nur exakte Tiefs, aber
# auch keine Werte, die schon weit vom Tief weggelaufen sind).
ABSTAND_52W_TIEF_MAX = 20.0  # Prozent oberhalb des 52-Wochen-Tiefs (GEÄNDERT 23.07.2026, vorher 10.0 - siehe WMA200_LOOKBACK_TAGE-Kommentar unten für die Begründung)
WMA200_LOOKBACK_TAGE = 15  # NEU (23.07.2026): Kurs muss innerhalb dieser Anzahl Handelstage
                            # unter der WMA200 gelegen haben, nicht zwingend heute noch -
                            # siehe Begründung bei _pruefe_trendwende

# Zeitfenster fuer den frischen TRIGGER (C.2 - Kumo-Ausbruch) sowie den
# optionalen Stochastik-Crossover-Bonus. GEAENDERT (28.07.2026): gilt NICHT
# mehr fuer die RSI-Divergenz - die hat ihr eigenes, laengeres Fenster
# (DIVERGENZ_FENSTER_TAGE unten). Grund: Divergenz (am Boden) und Kumo-
# Ausbruch (Wochen spaeter) in EIN gemeinsames 3-5-Tage-Fenster zu zwingen
# war strukturell fast nie erfuellbar - die beiden Signalgruppen waren im
# Debug-Log vom 24./28.07.2026 ueber alle Ticker komplett disjunkt.
FRISCHE_TAGE = 5

# Zeitfenster fuer die BODEN-Bedingung (C.1 - bullische RSI-Divergenz):
# die Divergenz darf bis zu N Handelstage zurueckliegen, MUSS aber seitdem
# intakt sein (kein Schlusskurs unter dem Divergenz-Tief, sonst ist der
# Boden gebrochen und die Divergenz invalidiert - siehe
# check_rsi_divergence_recent). Bewusst deutlich laenger als FRISCHE_TAGE:
# die typische Turnaround-Sequenz ist "erst Divergenz-Boden, dann Tage/
# Wochen spaeter der Kumo-Ausbruch als Bestaetigung".
DIVERGENZ_FENSTER_TAGE = 40

# Alternative Naehe-zum-Boden-Regel (NEU 29.07.2026): Position in der
# 52-Wochen-Spanne, (Kurs - Tief) / (Hoch - Tief). Fuer die Edelmetalle ist
# sie seit 29.07.2026 AKTIV (edelmetalle_scanner.py importiert diese
# Konstante von hier - eine Quelle, kein zweiter Wert). Fuer AKTIEN laeuft
# sie vorerst nur als SCHATTEN-MESSUNG mit: der Tageslauf entscheidet
# weiterhin nach ABSTAND_52W_TIEF_MAX, zusaetzlich wird aber gezaehlt, was
# die Spannen-Regel zulassen wuerde (siehe Briefing-Block "SCHATTEN-
# MESSUNG"). Hintergrund: der Prozentabstand zum Tief bestraft volatile
# Titel - DroneShield lag am 29.07. 69% unter dem Jahreshoch und damit klar
# am Boden, aber rechnerisch 20,7% ueber dem Jahrestief und fiel dadurch um
# 0,7 Prozentpunkte durch den Filter; die Spannen-Position betrug 7%.
SPANNEN_POSITION_MAX = 0.35

# BEINAHE-KANDIDATEN CRV-Filter (NEU 09.08.2026, Nutzerwunsch): analog zu
# FUNNEL_BEINAHE (analyse.py) und BEINAHE_SHORT (short_scanner.py) - Titel,
# die BEIDE Pflicht-Signale (intakte Divergenz + frischer Kumo-Ausbruch)
# erfuellt haben und ausschliesslich am CRV-Filter gescheitert sind. Anlass:
# 6-Tage-Auswertung (01.-07.08.2026) zeigte an 5 von 6 Tagen mindestens einen
# Titel, der die letzte Pflicht-Stufe erreichte, aber IMMER am CRV scheiterte -
# ohne diese Diagnose war nicht erkennbar, ob das knapp (CRV 0,95) oder weit
# (CRV 0,3) daneben lag. Kein Lock noetig (list.append() ist dank GIL atomar,
# gleiches Muster wie BEINAHE_SHORT in short_scanner.py).
BEINAHE_TRENDWENDE = []

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

def check_rsi_divergence_recent(data, fenster_tage=DIVERGENZ_FENSTER_TAGE):
    """BODEN-Bedingung (C.1) - GEAENDERT 28.07.2026: die bullische RSI-
    Divergenz muss nicht mehr in denselben 5 Tagen wie der Kumo-Ausbruch
    liegen, sondern darf bis zu `fenster_tage` Handelstage zurueckliegen.
    Dafuer kommt eine INVALIDIERUNGS-Regel dazu: schliesst der Kurs nach dem
    Divergenz-Tief noch einmal TIEFER, ist der Boden gebrochen und die
    Divergenz zaehlt nicht mehr (klassische Divergenz-Regel - ein
    unterschrittenes Divergenz-Tief ist ein gescheitertes Signal, kein
    "immer noch gueltiges").
    Gibt True/False zurueck (nur bullische Divergenz relevant fuer diesen
    Scanner, da wir ausschliesslich nach Boeden suchen)."""
    # Etwas Vorlauf vor dem Fenster mitnehmen, damit argrelextrema am linken
    # Rand echte lokale Minima erkennen kann (order=5 braucht Nachbarn).
    df = data.tail(fenster_tage + 15)
    ilocs_min = argrelextrema(df['Close'].values, np.less_equal, order=5)[0]

    if len(ilocs_min) < 2:
        return False

    letzter_tiefpunkt_idx = ilocs_min[-1]

    # 1) Divergenz-Tief muss innerhalb des Fensters liegen (nicht "uralt")
    im_fenster = letzter_tiefpunkt_idx >= (len(df) - 1 - fenster_tage)
    if not im_fenster:
        return False

    # 2) Divergenz selbst: Kurs macht tieferes Tief, RSI ein hoeheres Tief
    bullische_divergenz = (
        df['Close'].iloc[letzter_tiefpunkt_idx] < df['Close'].iloc[ilocs_min[-2]]
    ) and (
        df['RSI'].iloc[letzter_tiefpunkt_idx] > df['RSI'].iloc[ilocs_min[-2]]
    )
    if not bullische_divergenz:
        return False

    # 3) Invalidierung: seit dem Divergenz-Tief darf KEIN tieferer
    #    Schlusskurs aufgetreten sein (leerer Slice, falls das Tief die
    #    letzte Kerze ist -> min() ist NaN -> Vergleich False -> ok).
    tief_close = df['Close'].iloc[letzter_tiefpunkt_idx]
    danach_min = df['Close'].iloc[letzter_tiefpunkt_idx + 1:].min()
    if pd.notna(danach_min) and danach_min < tief_close:
        return False

    return True


def tage_seit_kumo_ausbruch(data, max_rueckblick=60):
    """DIAGNOSE (NEU 30.07.2026, reine Beobachtung - KEIN Filter): Wie viele
    Handelstage liegt der LETZTE Kumo-Ausbruch zurueck? Rueckgabe: Anzahl
    (0 = heute), None wenn im Rueckblick keiner gefunden wurde, oder
    "nicht_ueber_wolke" wenn der Kurs aktuell gar nicht ueber der Wolke steht.

    Zweck: Die Schatten-Messung vom 30.07. zeigte, dass der frische
    Kumo-Ausbruch die eigentliche Engstelle ist (12 Titel mit intakter
    Divergenz, 0 mit Ausbruch). Bevor das Trigger-Fenster verbreitert wird
    (5 -> 7 Tage), soll messbar sein, OB das ueberhaupt hilft: liegt der
    letzte Ausbruch bei diesen Titeln 6-7 Tage zurueck, bringt eine
    Verbreiterung sofort Kandidaten - steht der Kurs dagegen noch unter der
    Wolke (Titel macht neue Tiefs), aendert auch ein 30-Tage-Fenster nichts."""
    try:
        if len(data) < 60:
            return None
        kumo_ober = pd.concat([data['SenkouA'], data['SenkouB']], axis=1).max(axis=1)
        if pd.isna(kumo_ober.iloc[-1]) or data['Close'].iloc[-1] <= kumo_ober.iloc[-1]:
            return "nicht_ueber_wolke"
        for i in range(0, min(max_rueckblick, len(data) - 1)):
            idx, idx_prev = -1 - i, -2 - i
            c_h, k_h = data['Close'].iloc[idx], kumo_ober.iloc[idx]
            c_v, k_v = data['Close'].iloc[idx_prev], kumo_ober.iloc[idx_prev]
            if pd.isna(c_h) or pd.isna(k_h) or pd.isna(c_v) or pd.isna(k_v):
                continue
            if c_v <= k_v and c_h > k_h:
                return i
        return None
    except Exception:
        return None


def check_kumo_breakout_recent(data, frische_tage=FRISCHE_TAGE):
    """TRIGGER (C.2) - NEU 28.07.2026 (Nutzerwunsch): zurueck zum ECHTEN
    Kumo-Ausbruch als Pflicht-Signal (statt des am 24.07. eingebauten
    Kijun-Ausbruchs, der nur die Basislinie kreuzt und damit ein deutlich
    schwaecheres Signal war als in Briefing/Doku beschrieben). Lokal
    implementiert statt analyse.py's check_kumo_breakout, damit das
    Frische-Fenster hier frei parametrierbar ist (analyse.py prueft fest
    3 Tage) und kein Pflicht-Volumen verlangt wird - Trendwende-Boeden
    entstehen haeufig in Desinteresse-Phasen mit duennem Volumen; Volumen
    fliesst hier bewusst nur informativ (Vol_Ratio-Spalte) ein.
    Bedingungen:
      - HEUTE steht der Schlusskurs ueber der KOMPLETTEN Wolke
        (ueber Senkou A UND B) - der Ausbruch ist also noch intakt.
      - Innerhalb der letzten `frische_tage` Handelstage gab es den
        eigentlichen Durchbruch: Vortag auf/unter der Wolken-Oberkante,
        Folgetag darueber (frisches Signal, kein alter Zustand)."""
    if len(data) < 60:
        return False

    kumo_ober = pd.concat([data['SenkouA'], data['SenkouB']], axis=1).max(axis=1)

    # Ausbruch muss heute noch intakt sein
    if pd.isna(kumo_ober.iloc[-1]) or data['Close'].iloc[-1] <= kumo_ober.iloc[-1]:
        return False

    for i in range(0, frische_tage):
        idx = -1 - i
        idx_prev = idx - 1
        if abs(idx_prev) > len(data):
            break
        c_heute, k_heute = data['Close'].iloc[idx], kumo_ober.iloc[idx]
        c_davor, k_davor = data['Close'].iloc[idx_prev], kumo_ober.iloc[idx_prev]
        if pd.isna(c_heute) or pd.isna(k_heute) or pd.isna(c_davor) or pd.isna(k_davor):
            continue
        if c_davor <= k_davor and c_heute > k_heute:
            return True
    return False


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


def get_swing_highs_above(data, entry, lookback=120, order=5, max_n=3):
    """Aufwaerts-Pendant zu get_swing_lows_below im short_scanner.py: echte
    Pivot-Hochs (lokale Kurshochs) aus der juengeren Kurshistorie als
    zusaetzliche Aufwaerts-Ziel-Kandidaten, gefiltert auf > entry."""
    fenster = data.iloc[-lookback:] if len(data) > lookback else data.copy()
    if len(fenster) < 10:
        return []
    highs = fenster['High'].values
    idx_swings = argrelextrema(highs, np.greater_equal, order=order)[0]
    kandidaten = sorted(
        {round(float(highs[i]), 4) for i in idx_swings if pd.notna(highs[i]) and highs[i] > entry}
    )
    return kandidaten[:max_n]


def get_round_number_targets_up(entry, anzahl=2):
    """Aufwaerts-Pendant zu get_round_number_targets im short_scanner.py:
    psychologische runde Kursmarken OBERHALB des Einstiegs. Gleiche
    kursgroessen-skalierte Schrittweite wie beim Short-Pendant."""
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
    naechste_runde = math.ceil(entry / schritt) * schritt
    if naechste_runde <= entry:
        naechste_runde += schritt
    aktuell = naechste_runde
    while len(marken) < anzahl:
        marken.append(round(aktuell, 4))
        aktuell += schritt
    return marken


def sammle_aufwaerts_ziele(data, entry, mindest_abstand_perc=1.0, dedupe_abstand_perc=1.5):
    """NEU (analog zu sammle_abwaerts_ziele in short_scanner.py, hier nach
    oben gespiegelt): ersetzt die zuvor ungefilterte EMA/Fib/Kumo-Liste, aus
    der TP1 haeufig ein technisch kaum aussagekraeftiger, viel zu naher Wert
    war (bei einem gerade erst vom Boden abgedrehten Titel liegt z.B. die
    EMA20 fast zwangslaeufig hauchduenn ueber dem Kurs). Sammelt alle
    plausiblen charttechnischen Aufwaerts-Ziel-Kandidaten: Fib-Extension
    (get_fib_levels aus analyse.py, bereits die Aufwaerts-Variante), 52-
    Wochen-Hoch, echte Pivot-Hochs, die Ichimoku-Wolke, gleitende
    Durchschnitte (nur falls sie oberhalb des Kurses liegen) und
    psychologische runde Kursmarken. Zwei Filter sorgen fuer verwertbare
    TP1/TP2 statt Rauschen:
    - mindest_abstand_perc: Kandidaten, die weniger als X% ueber dem Kurs
      liegen, werden verworfen (sonst waere TP1 z.B. 0,1% ueber dem Kurs -
      kein sinnvolles erstes Kursziel).
    - dedupe_abstand_perc: liegen zwei Kandidaten weniger als Y% auseinander,
      wird nur der naeher am Kurs liegende behalten."""
    fib1, fib2 = get_fib_levels(data)
    kumo_werte = [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]]
    hoch_52w = float(data['High'].max())
    swing_highs = get_swing_highs_above(data, entry)
    ema_werte = [
        data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1],
        data['EMA200'].iloc[-1], data['WMA200'].iloc[-1],
    ]
    runde_zahlen = get_round_number_targets_up(entry)

    alle_kandidaten = [fib1, fib2, hoch_52w] + kumo_werte + swing_highs + ema_werte + runde_zahlen
    roh = sorted(
        {round(float(v), 4) for v in alle_kandidaten if pd.notna(v) and v > entry}
    )

    # Mindestabstand zum Kurs (Rauschen direkt ueber dem Einstieg raus)
    mindest_wert = entry * (1 + mindest_abstand_perc / 100)
    gefiltert = [v for v in roh if v >= mindest_wert]

    # Dedupe: zu nah beieinander liegende Kandidaten zusammenfassen
    ziele = []
    for v in gefiltert:
        if not ziele or (v - ziele[-1]) / entry * 100 >= dedupe_abstand_perc:
            ziele.append(v)

    return ziele


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

    # Ichimoku Kumo-Grenzen (fuer check_kumo_breakout_recent)
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


def _pruefe_trendwende(ticker, sektor, markt, data, bench_close=None,
                       spannen_position_max=None):
    """Gibt (ergebnis_dict_oder_None, funnel_grund) zurueck - der zweite Wert
    speist die Funnel-Statistik (NEU 28.07.2026, Nutzerwunsch: '0 Kandidaten'
    soll interpretierbar sein - an welcher Stufe faellt wie viel raus?).

    spannen_position_max (NEU 29.07.2026, fuer Edelmetalle): schaltet die
    Naehe-zum-Boden-Pruefung von "max. X% ueber dem 52W-Tief" auf "Position
    in der 52-Wochen-Spanne" um: (Kurs - Tief) / (Hoch - Tief) <= Wert.
    Hintergrund (Messreihe 29.07.2026): der Prozentabstand zum Tief haengt
    stark von der Jahresvolatilitaet ab. Silber lag 52% UNTER seinem
    52W-Hoch - also klar am Boden - aber zugleich 59% UEBER seinem 52W-Tief,
    weil sich der Preis im selben Jahr erst mehr als verdoppelt hatte. Der
    20%-Filter warf damit genau die Titel raus, die der Scanner finden soll.
    Die Spannen-Position ist volatilitaetsunabhaengig und misst direkt die
    Absicht: "im unteren Bereich der Jahresspanne, aber nicht weggelaufen".
    Bei None gilt unveraendert die Prozent-Regel (Aktien - dort liefert sie
    taeglich ~80 Kandidaten und funktioniert)."""
    if len(data) < 60:
        return None, "zu_wenig_daten"

    data = _indikatoren_berechnen(data)
    entry = data['Close'].iloc[-1]

    # A/B - Grundvoraussetzung (GELOCKERT 23.07.2026): Kurs muss innerhalb
    # der letzten WMA200_LOOKBACK_TAGE Handelstage UNTER der WMA200 gelegen
    # haben (nicht zwingend heute noch). Grund: ein vollstaendiger Kumo-
    # Ausbruch (siehe unten) ist ein spaetes, traeges Signal - bis der Kurs
    # wirklich ueber die komplette Wolke steigt, hat er sich in der Praxis
    # meist schon so weit erholt, dass er auch schon wieder ueber der
    # WMA200 liegt. Die alte "ist JETZT noch drunter"-Bedingung stand damit
    # fast im Widerspruch zum Kumo-Ausbruch-Kriterium und war der Grund,
    # warum praktisch nie beide gleichzeitig erfuellt waren.
    if pd.isna(data['WMA200'].iloc[-1]):
        return None, "zu_wenig_daten"
    war_unter_wma200 = any(
        pd.notna(data['WMA200'].iloc[-1 - i]) and data['Close'].iloc[-1 - i] < data['WMA200'].iloc[-1 - i]
        for i in range(0, WMA200_LOOKBACK_TAGE) if (1 + i) <= len(data)
    )
    if not war_unter_wma200:
        return None, "nicht_unter_wma200"

    tief_52w = data['Low'].min()
    abstand_52w_tief = round(((entry / tief_52w) - 1) * 100, 2)
    if spannen_position_max is None:
        # Aktien: unveraendert Prozentabstand zum Tief
        if abstand_52w_tief > ABSTAND_52W_TIEF_MAX:
            return None, "zu_weit_vom_52w_tief"
    else:
        # Edelmetalle: Position in der 52-Wochen-Spanne (siehe Docstring)
        hoch_52w = data['High'].max()
        spanne = hoch_52w - tief_52w
        if spanne <= 0:
            return None, "zu_weit_vom_52w_tief"
        spannen_position = (entry - tief_52w) / spanne
        if spannen_position > spannen_position_max:
            print(f"DEBUG-TRENDWENDE-VERWORFEN: {ticker} | Spannen-Position "
                  f"{spannen_position:.0%} > {spannen_position_max:.0%} "
                  f"(Tief {tief_52w:.2f} / Kurs {entry:.2f} / Hoch {hoch_52w:.2f})")
            return None, "zu_weit_vom_52w_tief"

    # C - beide Bestaetigungen Pflicht, aber zeitlich ENTKOPPELT
    # (GEAENDERT 28.07.2026, Nutzerwunsch: Pflicht-Signal soll wieder der
    # ECHTE Kumo-Ausbruch sein, nicht der Kijun-Ausbruch vom 24.07. Damit
    # das nicht erneut in dauerhafte 0-Kandidaten-Tage laeuft, wurde das
    # eigentliche Problem behoben - nicht das UND, sondern das gemeinsame
    # Zeitfenster: Divergenz entsteht am Boden, der Kumo-Ausbruch folgt
    # erst Tage/Wochen spaeter. Deshalb jetzt Sequenz-Logik:
    #   1) Divergenz im 40-Tage-Fenster, nicht invalidiert (Boden)
    #   2) Kumo-Ausbruch frisch im 5-Tage-Fenster (Trigger)
    divergenz_ok = check_rsi_divergence_recent(data)
    if not divergenz_ok:
        return None, "keine_divergenz"

    kumo_ausbruch = check_kumo_breakout_recent(data)
    if not kumo_ausbruch:
        print(f"DEBUG-TRENDWENDE-VERWORFEN: {ticker} | Divergenz: True | "
              f"Kumo-Ausbruch (frisch): False | Abstand 52W-Tief: {abstand_52w_tief}%")
        return None, "kein_frischer_kumo_ausbruch"

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

    # TP-Kandidaten (NEU - siehe sammle_aufwaerts_ziele): gefilterte/dedupte
    # Liste aus Fib-Extension, 52W-Hoch, Swing-Hochs, EMAs/WMA200 (nur falls
    # oberhalb des Kurses) und runden Zahlen - ersetzt die alte ungefilterte
    # EMA/Fib-Liste, die TP1 haeufig auf einen technisch bedeutungslos nahen
    # Wert (z.B. EMA20 hauchduenn ueber dem Kurs) gesetzt hat.
    aufwaerts_ziele = sammle_aufwaerts_ziele(data, entry)
    tp1 = aufwaerts_ziele[0] if aufwaerts_ziele else entry * 1.08
    tp2 = aufwaerts_ziele[1] if len(aufwaerts_ziele) >= 2 else tp1 * 1.05

    crv1 = round((tp1 - entry) / (entry - stop), 2) if entry > stop else 0
    crv2 = round((tp2 - entry) / (entry - stop), 2) if entry > stop else 0
    chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
    chance2_perc = round(((tp2 - entry) / entry) * 100, 2)

    # NEU: Risiko-Filter, analog zur bestehenden Konvention bei Long-,
    # Edelmetalle- und (seit 25.07.2026) Short-Setups - CRV muss bei TP1 UND
    # TP2 jeweils >= 1.0 sein, sonst wird das Setup verworfen. Gerade bei
    # Trendwende-Setups mit ihrem strukturell weiter entfernten Stop (juengstes
    # Verlaufstief, siehe oben) faellt das haeufiger ins Gewicht als beim
    # Hauptscanner - bewusste Entscheidung: lieber weniger, dafuer belastbare
    # Kandidaten als ein TP1 mit z.B. CRV 0,12.
    if crv1 < 1.0 or crv2 < 1.0:
        print(f"DEBUG-TRENDWENDE-VERWORFEN: {ticker} -> CRV zu niedrig (CRV1={crv1}, CRV2={crv2})")
        try:
            firma_name_beinahe = yf.Ticker(ticker).info.get('longName', ticker) or ticker
        except Exception:
            firma_name_beinahe = ticker
        BEINAHE_TRENDWENDE.append({
            "text": f"{firma_name_beinahe} ({ticker}): CRV-Filter -> CRV1 {crv1} / CRV2 {crv2} "
                   f"(Mindestwert 1.0 je TP), Kurs {entry:.2f}, TP1 {tp1:.2f}, TP2 {tp2:.2f}, "
                   f"Stop-Risiko {risk_perc:.2f}%",
            "crv_sortier": min(crv1, crv2),
        })
        return None, "crv_unter_1"

    try:
        firma_name = yf.Ticker(ticker).info.get('longName', ticker) or ticker
    except Exception:
        firma_name = ticker

    # Fundamental-Ampel (NEU, 28.07.2026, Nutzerwunsch): Trendwende war bisher
    # rein technisch (nur RSI-Divergenz + Kijun-Ausbruch), ohne die fundamentale
    # Bestaetigung, die einen echten Turnaround von einem fallenden Messer
    # unterscheidet - genau die Kombination aus Charttechnik + verbesserten
    # Fundamentaldaten macht eine Trendwende-Story erst wirklich ueberzeugend.
    # Wiederverwendet dieselbe Funktion wie der Hauptscanner (richtung="long",
    # da Trendwende immer auf einen Boden/Aufwaertswende setzt).
    fundamental_ampel, fundamental_hinweis = berechne_fundamental_ampel(
        ticker, sektor=sektor, markt=markt, richtung="long"
    )

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
        "Fundamental_Ampel": fundamental_ampel,
        "Fundamental_Hinweis": fundamental_hinweis,
        "Risikohinweis": (
            "Trendwende-Setup - strukturell riskanter als Trendfolge-Setups "
            "(\u201eMesser-Gefahr\u201c). Enger, wende-spezifischer Stop - Positionsgroesse entsprechend anpassen."
        ),
    }, "valide"


def analyze_trendwende_us(ticker, sektor, data, spy_close=None):
    try:
        return _pruefe_trendwende(ticker, sektor, "US", data, spy_close)
    except Exception as e:
        print(f"FEHLER Trendwende US {ticker}: {e}")
        return None, "fehler"


def analyze_trendwende_eu(ticker, sektor, data, eu_bench_close=None):
    try:
        return _pruefe_trendwende(ticker, sektor, "EU", data, eu_bench_close)
    except Exception as e:
        print(f"FEHLER Trendwende EU {ticker}: {e}")
        return None, "fehler"


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
    # Funnel-Statistik (NEU 28.07.2026): zaehlt je Ablehnungsstufe, wie viele
    # Ticker dort rausfallen - macht "0 Kandidaten" interpretierbar (an
    # welcher Stufe klemmt es?) statt nur das Endergebnis zu melden.
    funnel = Counter()
    funnel["keine_kursdaten"] = (
        sum(1 for t, _ in us_tasks if t not in us_daten)
        + sum(1 for t, _ in eu_tasks if t not in eu_daten)
    )

    print("Starte Trendwende-Analyse (US)...")
    # Ab hier reine lokale Berechnung (Daten liegen bereits vor) - Threads
    # dienen hier nur noch der CPU-Parallelisierung, nicht mehr dem
    # Kaschieren von Netzwerk-Latenz wie vorher.
    # DIVERGENZ-WATCHLIST (NEU 28.07.2026): Titel, die die Boden-Bedingung
    # (intakte Divergenz) erfuellen und nur noch auf den frischen Kumo-
    # Trigger warten - das ist die Kandidaten-Pipeline der naechsten Tage.
    divergenz_watchlist = []
    # Ticker -> Handelstage seit letztem Kumo-Ausbruch (Diagnose, NEU 30.07.2026)
    kumo_diagnose = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            (t, executor.submit(analyze_trendwende_us, t, s, us_daten[t], spy_close))
            for t, s in us_tasks if t in us_daten
        ]
        for t, f in futures:
            r, grund = f.result()
            funnel[grund] += 1
            if grund == "kein_frischer_kumo_ausbruch":
                divergenz_watchlist.append(t)
                kumo_diagnose[t] = tage_seit_kumo_ausbruch(
                    _indikatoren_berechnen(us_daten[t].copy()))
            if r:
                ergebnisse.append(r)

    print("Starte Trendwende-Analyse (EU)...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            (t, executor.submit(analyze_trendwende_eu, t, s, eu_daten[t], eu_bench_close))
            for t, s in eu_tasks if t in eu_daten
        ]
        for t, f in futures:
            r, grund = f.result()
            funnel[grund] += 1
            if grund == "kein_frischer_kumo_ausbruch":
                divergenz_watchlist.append(t)
                kumo_diagnose[t] = tage_seit_kumo_ausbruch(
                    _indikatoren_berechnen(eu_daten[t].copy()))
            if r:
                ergebnisse.append(r)

    print(f"DEBUG: {len(ergebnisse)} Trendwende-Kandidaten gefunden.")

    # --- SCHATTEN-MESSUNG (NEU 29.07.2026, reine Beobachtung) ---
    # Derselbe Durchlauf noch einmal mit der Spannen-Regel statt der
    # Prozent-Regel. Aendert NICHTS am Ergebnis: weder CSV noch Kandidaten
    # noch Watchlist werden beruehrt - es wird nur gezaehlt, was die andere
    # Regel zulassen wuerde. Zweck: die Entscheidung "Regel auch fuer Aktien
    # umstellen?" auf dem vollen Universum (statt an einzelnen handverlesenen
    # Titeln) treffen zu koennen. Rein lokale Rechnung, keine zusaetzlichen
    # API-Abrufe - die Kursdaten liegen bereits vor.
    funnel_schatten = Counter()
    nur_spannen_regel = []   # von der Spannen-Regel zugelassen, von der Prozent-Regel nicht
    nur_prozent_regel = []   # umgekehrt
    schatten_divergenz = []  # davon mit intakter Divergenz (die relevante Teilmenge)

    # EIN Durchlauf je Regel (GEAENDERT: vorher lief die Schatten-Pruefung
    # zweimal - einmal fuer die Zaehlung, einmal fuer den Titel-Vergleich.
    # Beides wird hier in einer Schleife erledigt, das spart einen kompletten
    # Analyse-Durchlauf ueber das Universum.)
    for t, s, daten, bench, markt in (
        [(t, s, us_daten[t], spy_close, "US") for t, s in us_tasks if t in us_daten]
        + [(t, s, eu_daten[t], eu_bench_close, "EU") for t, s in eu_tasks if t in eu_daten]
    ):
        try:
            grund_prozent = _pruefe_trendwende(t, s, markt, daten, bench)[1]
            grund_spanne = _pruefe_trendwende(t, s, markt, daten, bench,
                                              spannen_position_max=SPANNEN_POSITION_MAX)[1]
        except Exception:
            funnel_schatten["fehler"] += 1
            continue
        funnel_schatten[grund_spanne] += 1
        # "kam ueber die Naehe-Stufe hinaus" = Grund ist NICHT zu_weit_vom_52w_tief
        prozent_ok = grund_prozent != "zu_weit_vom_52w_tief"
        spanne_ok = grund_spanne != "zu_weit_vom_52w_tief"
        if spanne_ok and not prozent_ok:
            nur_spannen_regel.append(t)
            # Nur relevant, wenn danach auch die Boden-Bedingung haelt
            if grund_spanne in ("kein_frischer_kumo_ausbruch", "crv_unter_1", "valide"):
                schatten_divergenz.append(t)
        elif prozent_ok and not spanne_ok:
            nur_prozent_regel.append(t)

    # gesamt_universum wird erst weiter unten (Funnel-Aufbereitung) gesetzt -
    # hier lokal berechnen, damit die Schatten-Messung davor stehen kann.
    universum_gesamt = len(us_tasks) + len(eu_tasks)
    durch_prozent = universum_gesamt - funnel.get("keine_kursdaten", 0) \
        - funnel.get("zu_wenig_daten", 0) - funnel.get("fehler", 0) \
        - funnel.get("nicht_unter_wma200", 0) - funnel.get("zu_weit_vom_52w_tief", 0)
    durch_spanne = universum_gesamt - funnel_schatten.get("keine_kursdaten", 0) \
        - funnel_schatten.get("zu_wenig_daten", 0) - funnel_schatten.get("fehler", 0) \
        - funnel_schatten.get("nicht_unter_wma200", 0) \
        - funnel_schatten.get("zu_weit_vom_52w_tief", 0)

    schatten_zeilen = [
        "(reine Beobachtung - der Tageslauf oben entscheidet unveraendert nach der Aktien-Regel)",
        f"Aktien-Regel  (AKTIV, max. {ABSTAND_52W_TIEF_MAX}% ueber 52W-Tief): "
        f"{durch_prozent} Titel passieren die Naehe-Stufe",
        f"Spannen-Regel (TEST, Position <= {SPANNEN_POSITION_MAX:.0%} der 52W-Spanne): "
        f"{durch_spanne} Titel passieren die Naehe-Stufe",
        f"- nur die Spannen-Regel laesst zu: {len(nur_spannen_regel)} Titel"
        + (f" ({', '.join(sorted(nur_spannen_regel)[:15])}"
           + (" ..." if len(nur_spannen_regel) > 15 else "") + ")" if nur_spannen_regel else ""),
        f"  davon mit intakter RSI-Divergenz (die eigentlich relevante Teilmenge): "
        f"{len(schatten_divergenz)}"
        + (f" ({', '.join(sorted(schatten_divergenz))})" if schatten_divergenz else ""),
        f"- nur die Aktien-Regel laesst zu: {len(nur_prozent_regel)} Titel"
        + (f" ({', '.join(sorted(nur_prozent_regel)[:15])}"
           + (" ..." if len(nur_prozent_regel) > 15 else "") + ")" if nur_prozent_regel else ""),
        f"=> Kandidaten (nach ALLEN Stufen) mit Spannen-Regel: "
        f"{funnel_schatten.get('valide', 0)} (aktiv heute: {len(ergebnisse)})",
    ]
    schatten_text = "\n".join(schatten_zeilen)
    print("SCHATTEN-MESSUNG:\n" + schatten_text)

    # Funnel fuer Konsole + Briefing aufbereiten (Reihenfolge = Pruefstufen)
    gesamt_universum = len(us_tasks) + len(eu_tasks)
    funnel_stufen = [
        ("keine_kursdaten", "Keine Kursdaten geliefert (API)"),
        ("zu_wenig_daten", "Zu wenig Historie / WMA200 nicht berechenbar"),
        ("fehler", "Fehler bei der Berechnung"),
        ("nicht_unter_wma200", f"Nicht (kuerzlich, {WMA200_LOOKBACK_TAGE}T-Lookback) unter der WMA200"),
        ("zu_weit_vom_52w_tief", f"Mehr als {ABSTAND_52W_TIEF_MAX}% ueber dem 52W-Tief"),
        ("keine_divergenz", f"Keine intakte bullische RSI-Divergenz ({DIVERGENZ_FENSTER_TAGE}T-Fenster)"),
        ("kein_frischer_kumo_ausbruch", f"Kein frischer Kumo-Ausbruch (letzte {FRISCHE_TAGE} Handelstage)"),
        ("crv_unter_1", "CRV-Filter (TP1 oder TP2 unter 1.0)"),
        ("valide", "VALIDE"),
    ]
    funnel_zeilen = [f"Universum gesamt: {gesamt_universum} Titel"]
    verbleibend = gesamt_universum
    for key, beschreibung in funnel_stufen:
        anzahl = funnel.get(key, 0)
        if key == "valide":
            funnel_zeilen.append(f"=> VALIDE: {anzahl}")
        else:
            verbleibend -= anzahl
            funnel_zeilen.append(f"- {beschreibung}: -{anzahl} (verbleiben {verbleibend})")
    funnel_text = "\n".join(funnel_zeilen)
    print("FUNNEL-STATISTIK:\n" + funnel_text)

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
        "Abstand_52W_Tief%", "Setup_Typ", "Qualitaets_Bonus",
        "Fundamental_Ampel", "Fundamental_Hinweis", "Risikohinweis",
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
        f.write("- Wende-Bestaetigung (BEIDE Pflicht, kein ODER - seit 28.07.2026 zeitlich\n")
        f.write("  entkoppelte SEQUENZ statt gemeinsamem Zeitfenster):\n")
        f.write(f"  1) Boden-Bedingung: bullische RSI-Divergenz (Kurs macht neues Tief, RSI aber\n")
        f.write(f"     nicht - Verkaufsdruck laesst nach) innerhalb der letzten {DIVERGENZ_FENSTER_TAGE} Handelstage,\n")
        f.write("     die seitdem NICHT invalidiert wurde (kein Schlusskurs unter dem Divergenz-\n")
        f.write("     Tief - sonst ist der Boden gebrochen und das Signal gescheitert).\n")
        f.write(f"  2) Trigger: frischer Kumo-Ausbruch (Kurs durchbricht die komplette Ichimoku-\n")
        f.write(f"     Wolke nach oben) innerhalb der letzten {FRISCHE_TAGE} Handelstage.\n")
        f.write("  Begruendung der Entkopplung: die Divergenz entsteht AM Boden, der Kumo-\n")
        f.write("  Ausbruch folgt naturgemaess erst Tage bis Wochen spaeter - beide in EIN\n")
        f.write("  kurzes Fenster zu zwingen war strukturell fast nie erfuellbar.\n")
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
        f.write("  NICHT mit den \"sicheren\" Trendfolge-Setups vermischt.\n")
        f.write("- Fundamentale Bestaetigung (NEU, 28.07.2026): zusaetzlich zu den rein\n")
        f.write("  technischen Pflicht-Signalen oben liefert die Fundamental-Ampel (KGV vs.\n")
        f.write("  Sektor-Median, identische Logik wie beim Hauptscanner) sowie eine Earnings-\n")
        f.write("  Warnung (Ueber-Nacht-Gap-Risiko) und juengste Schlagzeilen zusaetzlichen\n")
        f.write("  fundamentalen Kontext, der einen echten Turnaround von einem bloss\n")
        f.write("  technischen Fehlsignal unterscheiden hilft - ersetzt keines der beiden\n")
        f.write("  Pflicht-Signale, ist aber Teil jeder Ausgabe.\n\n")

        # Funnel-Statistik (NEU 28.07.2026): macht insbesondere "0 Kandidaten"
        # interpretierbar - an welcher Pruefstufe faellt wie viel raus?
        f.write("FUNNEL-STATISTIK (Ablehnungsgruende je Pruefstufe)\n")
        f.write("-" * 50 + "\n")
        f.write(funnel_text + "\n\n")

        # SCHATTEN-MESSUNG (NEU 29.07.2026): Regel-Vergleich auf dem vollen
        # Universum - Entscheidungsgrundlage, ob die Naehe-Regel auch fuer
        # Aktien umgestellt wird. Aendert nichts am heutigen Ergebnis.
        f.write("SCHATTEN-MESSUNG Naehe-Regel (Aktien-Regel vs. Spannen-Regel)\n")
        f.write("-" * 50 + "\n")
        f.write(schatten_text + "\n\n")

        # BEINAHE-KANDIDATEN CRV-Filter (NEU 09.08.2026, Nutzerwunsch nach der
        # 6-Tage-Schatten-Messung): BEINAHE_TRENDWENDE wurde bereits seit
        # Einfuehrung des CRV-Filters gesammelt (siehe _pruefe_trendwende),
        # aber NIE ausgegeben - eine Luecke, keine bewusste Auslassung. Die
        # DIVERGENZ-WATCHLIST deckt eine ANDERE, FRUEHERE Stufe ab (wartet
        # noch auf den Kumo-Trigger) - dieser Block hier zeigt Titel, die
        # BEIDE Pflicht-Signale UND den Kumo-Trigger bereits geschafft haben
        # und erst am letzten Schritt (CRV) scheitern. Beide Bloecke sind
        # NICHT redundant, auch wenn das am 30.07.2026 zunaechst so
        # eingeschaetzt wurde. Bewusst UNABHAENGIG vom divergenz_watchlist-
        # Block platziert (eigene Bedingung), Format/Sortierung analog zu
        # FUNNEL_BEINAHE in analyse.py (BEINAHE-KANDIDATEN Hauptscanner).
        if BEINAHE_TRENDWENDE:
            f.write("BEINAHE-KANDIDATEN CRV-Filter (Boden-Bedingung + frischer Kumo-Ausbruch "
                    "erfuellt, erst am CRV gescheitert)\n")
            f.write("-" * 50 + "\n")
            f.write("(nur Beobachtung, KEINE Setups - zeigt, wie knapp CRV verfehlt wurde)\n")
            for b in sorted(BEINAHE_TRENDWENDE,
                            key=lambda x: (x.get("crv_sortier") is None, -(x.get("crv_sortier") or 0))):
                f.write(b["text"] + "\n\n")

        # DIVERGENZ-WATCHLIST (NEU 28.07.2026): die Titel der vorletzten
        # Funnel-Stufe - Boden-Bedingung erfuellt, Trigger steht noch aus.
        # Springt einer davon in den naechsten Tagen frisch ueber die Wolke,
        # wird er zum Kandidaten. Bewusst NUR Beobachtung.
        if divergenz_watchlist:
            # Namen statt Ticker (GEAENDERT 29.07.2026, Nutzerwunsch: die
            # Auswertung soll ueberall Namen zeigen). Nur fuer die wenigen
            # Watchlist-Titel je ein leichter yfinance-Info-Abruf mit
            # Ticker-Fallback bei Fehlern - der Ticker bleibt in Klammern
            # fuer die eigene Zuordnung (Sheet/Log arbeiten mit Tickern).
            def _name_oder_ticker(t):
                """GEAENDERT 29.07.2026: longName BEVORZUGT, shortName nur als
                Fallback. Grund: yfinance kuerzt shortName hart auf ~30 Zeichen
                und fuellt teils mit Leerzeichen auf - im Lauf vom 29.07.
                standen deshalb "JinkoSolar Holding Company Limi" und
                "VOLKSWAGEN AG                 V" im Briefing. longName ist
                der vollstaendige Firmenname. Zusaetzlich: Mehrfach-Leerzeichen
                zusammenziehen und ein abgeschnittenes Rest-Fragment am Ende
                entfernen (einzelner Buchstabe oder ein Wortanfang direkt hinter
                einem Leerzeichen, wie das "V" bei Volkswagen)."""
                try:
                    info = yf.Ticker(t).info
                    name = info.get('longName') or info.get('shortName')
                    if not name:
                        return t
                    name = re.sub(r'\s+', ' ', str(name)).strip()
                    # Einzelnen Rest-Buchstaben am Ende abschneiden ("... AG V")
                    name = re.sub(r'\s+[A-Za-z]$', '', name).strip(' ,;-')
                    return f"{name} ({t})" if name else t
                except Exception:
                    return t
            def _mit_diagnose(t):
                d = kumo_diagnose.get(t)
                if d == "nicht_ueber_wolke":
                    zusatz = "Kurs noch unter/in der Wolke"
                elif isinstance(d, int):
                    zusatz = f"letzter Kumo-Ausbruch vor {d} Handelstagen"
                else:
                    zusatz = "kein Kumo-Ausbruch in 60 Tagen"
                return f"{_name_oder_ticker(t)} [{zusatz}]"

            watchlist_namen = [_mit_diagnose(t) for t in sorted(divergenz_watchlist)]
            f.write("DIVERGENZ-WATCHLIST (Boden-Bedingung intakt, wartet auf frischen Kumo-Trigger)\n")
            f.write("-" * 50 + "\n")
            f.write("(nur Beobachtung, KEINE Setups - erscheint in der Auswertung nur als einzeiliger Beobachtungssatz mit den NAMEN)\n")
            f.write(f"Klammer-Diagnose (NEU 30.07.2026): wie weit der letzte Kumo-Ausbruch zurueckliegt.\n")
            f.write(f"Aktuelles Trigger-Fenster: {FRISCHE_TAGE} Handelstage - Werte knapp darueber zeigen, ob eine\n")
            f.write("Verbreiterung des Fensters ueberhaupt Kandidaten braechte; 'Kurs noch unter/in der Wolke'\n")
            f.write("heisst: kein Fenster der Welt hilft, der Trigger ist noch nicht passiert.\n")
            f.write(", ".join(watchlist_namen) + "\n\n")

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
                    f"Fundamental-Ampel: {row['Fundamental_Ampel']} ({row['Fundamental_Hinweis']})\n"
                    f"Risikohinweis: {row['Risikohinweis']}\n"
                )
                # Earnings-Warnung (Gap-Risiko) + juengste Schlagzeilen (NEU,
                # 28.07.2026, analog zum Hauptscanner) - live berechnet statt
                # in der CSV gespeichert, da beide Werte sich schnell aendern
                # (identisches Vorgehen wie in analyse.py's Setups-Ausgabe).
                earnings = get_earnings_warnung(row['Ticker'])
                if earnings:
                    f.write(f"{earnings}\n")
                for headline in get_news_headlines(row['Ticker']):
                    f.write(f"News {headline}\n")
                f.write("\n")

    print(f"Gespeichert: {dateiname_briefing}")
    print("Trendwende-Scanner abgeschlossen.")


if __name__ == "__main__":
    main()
