"""
edelmetalle_scanner.py

Separater Scanner fuer die vier klassischen Edelmetalle (Gold, Silber, Platin,
Palladium) - wendet die IDENTISCHEN Kriterien des Hauptscanners (analyse.py,
Trendfolge/Fortsetzung) auf diese vier Instrumente an, damit sie genauso wie
Aktien-Setups gehandelt werden koennen. Architektur-Entscheidung vom
24.07.2026 (siehe Chat-Verlauf):

  - Eigener, kleiner Scanner statt Integration in analyse.py: feste
    4er-Liste statt Sektor-Rotation, kein Risiko fuer das 180-Ticker-Budget
    des Hauptscanners, unabhaengig testbar/abschaltbar.
  - Kursdaten via yfinance-Futuresdaten (GC=F/SI=F/PL=F/PA=F),
    NICHT Alpaca und NICHT ETFs (GLD/SLV/PPLT/PALL haben Tracking-Differenzen
    zum Metallpreis). Die Futuresreihe bildet fuer diesen Scanner den
    zugrunde liegenden Metallpreis direkt ab; die verwendete Historie muss
    fuer EMA200/WMA200 und die weiteren technischen Kriterien ausreichend sein.
  - Alle Haupt-Kriterien 1:1 uebernommen (EMA-Breakout, Pullback-Zone,
    Trendlinien-Ausbruch, Kumo-Ausbruch, CRV>=1.0, Abstand-52W-Hoch<=-25%,
    Plausibilitaets-Check) - "alles was wichtig ist muss rein".
  - Relative Staerke (NEU): gegen einen Rohstoff-Index-ETF (DBC, Invesco DB
    Commodity Index Tracking Fund) statt SPY/STOXX600 - ein Aktien-Index als
    Vergleichsmassstab fuer Edelmetalle waere konzeptionell nicht sinnvoll,
    ein breiter Rohstoff-Index dagegen schon (bleibt derselbe -10%-Schwellwert
    wie beim Hauptscanner).
  - Fundamental-Ampel (KGV) entfaellt bewusst - Rohstoffe haben keine
    Unternehmensgewinne, eine KGV-Kennzahl ergibt hier keinen Sinn.
  - Analysten-Kursziel entfaellt bewusst (kein Analysten-Konsens fuer
    Rohstoff-Spotdaten via yfinance verfuegbar) - Tech-Kursziel bleibt einzige
    Zielgroesse, wie bei den meisten EU-Setups ohne Kursziel auch.
  - Eigene Datei (Edelmetalle_Setups.csv) + eigener Briefing-Abschnitt
    (Edelmetalle_Briefing.txt) - wird von upload_to_drive.py automatisch mit
    hochgeladen (Dateiname-Muster "Setups"/"Briefing" bereits vorhanden,
    keine Anpassung an upload_to_drive.py noetig).

DREI STRATEGIEN IN EINER DATEI (NEU 29.07.2026, Nutzerentscheidung):
  - Trendfolge (Bestand): Fortsetzung eines Aufwaertstrends, Kurs UEBER
    WMA200/EMA200 - das urspruengliche Metall-Setup.
  - Trendwende (NEU): Bodenbildung nach einem Fall, Kurs UNTER WMA200, nah am
    52-Wochen-Tief, Pflicht-Sequenz aus RSI-Divergenz (Boden) und frischem
    Kumo-Ausbruch (Trigger). Anlass: Gold/Silber notieren seit Wochen unter
    allen Durchschnitten - der Trendfolge-Filter meldet deshalb dauerhaft 0
    Setups, obwohl sich charttechnisch eine Bodenbildung abzeichnet. Die
    komplette Bodenbildung und die erste Erholung lagen bisher im blinden
    Fleck des Scanners.
  - Short (NEU): Spiegelbild der Trendfolge, setzt auf fallende Metallpreise.
  Warum EINE Datei statt drei Scanner: alle drei brauchen exakt dieselben
  Daten (4 Edelmetall-Futures + DBC-Benchmark). Drei Dateien haetten drei Abrufe, drei
  Workflow-Steps, drei Briefings und drei Drive-Uploads bedeutet - bei nur 4
  Instrumenten unverhaeltnismaessig (bei Aktien lohnt die Trennung, weil dort
  die Universen unterschiedlich sind: Top-Sektoren vs. alle Sektoren vs.
  Bottom-Sektoren). Unterschieden wird ueber die CSV-Spalte "Strategie" und
  drei getrennte Briefing-Abschnitte mit je eigener Funnel-Statistik.
  Systematisch sauber: "Edelmetalle" ist die ANLAGEKLASSE, "Trendfolge/
  Trendwende/Short" sind die STRATEGIEN darin - dieselben wie bei Aktien.

Voraussetzungen: dieselben Umgebungsvariablen wie analyse.py (ALPACA_KEY,
ALPACA_SECRET, GROQ_API_KEY - auch wenn dieses Skript selbst weder Alpaca
noch Groq direkt nutzt: der Import von analyse.py fuehrt dessen kompletten
Modul-Code aus, siehe main.yml-Kommentar bei trendwende_scanner.py fuer die
identische Begruendung). Muss im selben Verzeichnis wie analyse.py liegen.
"""

import datetime
import numpy as np
import pandas as pd
import yfinance as yf
from market_cache import get_yf_history

# --- Bewaehrte Bausteine aus dem Hauptscanner wiederverwenden ---
from collections import Counter, defaultdict

# Trendwende- und Short-Logik werden NICHT nachgebaut, sondern direkt aus
# den Aktien-Scannern importiert (NEU 29.07.2026): identische Kriterien fuer
# Aktien und Metalle, eine einzige Stelle zum Pflegen. Beide Module sind
# durch `if __name__ == "__main__"` geschuetzt, der Import startet dort also
# keinen Scan.
from trendwende_scanner import _pruefe_trendwende, SPANNEN_POSITION_MAX

# BEINAHE-KANDIDATEN (NEU 30.07.2026, Nutzerwunsch - gilt auch hier): Titel,
# die alle Muster-Pruefungen bestanden und erst am CRV-Filter scheiterten.
# Bei nur 4 Instrumenten besonders aussagekraeftig - man sieht sofort, WIE
# knapp ein Metall war. Die Short-Variante fuellt die Liste im importierten
# short_scanner-Modul (BEINAHE_SHORT); sie wird unten mit ausgegeben, damit
# die dort gesammelten Metall-Shorts nicht verloren gehen.
BEINAHE_EDELMETALL = []
from short_scanner import _pruefe_short_setup, BEINAHE_SHORT

from analyse import (
    check_rsi_divergence,
    check_trendline_breakout,
    check_kumo_breakout,
    get_fib_levels,
    get_golden_cross_status,
    clean_num,
    _begrenze_tp2_realitaetsdeckel,
    get_rekord_naehe_text,
    get_saisonalitaet_text,
    get_kurzfrist_kontext_text,
)

# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------

# Feste 4er-Liste statt Sektor-Rotation - Ticker -> Anzeige-Name.
EDELMETALLE = {
    "GC=F": "Gold",
    "SI=F": "Silber",
    "PL=F": "Platin",
    "PA=F": "Palladium",
}

# Rohstoff-Index-ETF als Vergleichsmassstab fuer die Relative-Staerke-
# Berechnung (statt SPY/STOXX600, siehe Modul-Docstring).
COMMODITY_BENCHMARK_TICKER = "DBC"

# Naehe-zum-Boden-Kriterium fuer die Metall-TRENDWENDE: Position in der
# 52-Wochen-Spanne statt Prozentabstand zum Tief (Nutzerentscheidung
# 29.07.2026). Die Konstante wird bewusst NICHT hier definiert, sondern aus
# trendwende_scanner importiert (siehe Import oben) - dort steht die
# Pruef-Logik, dort laeuft die Schatten-Messung fuer Aktien, und so gibt es
# garantiert nur EINEN Wert. Begruendung aus der Messreihe vom 29.07.2026:
#   Gold      +25,1% ueber Tief | -26,9% zum Hoch -> Spannen-Position 35%
#   Silber    +58,8% ueber Tief | -52,4% zum Hoch -> Spannen-Position 25%
#   Platin    +25,0% ueber Tief | -44,1% zum Hoch -> Spannen-Position 20%
#   Palladium +16,1% ueber Tief | -42,1% zum Hoch -> Spannen-Position 16%
# Nach der alten Prozent-Regel fielen 3 von 4 raus - darunter Silber, das
# mit -52% zum Hoch der eindeutigste Boden-Kandidat ueberhaupt war.

RS_MIN = -10.0  # gleicher Schwellwert wie beim Hauptscanner
ABSTAND_52W_HOCH_MAX = -25.0  # gleicher Schwellwert wie beim Hauptscanner


def get_commodity_benchmark_close():
    """Laedt die rohen DBC-Schlusskurse (ca. 1 Jahr) fuer die Relative-
    Staerke-Berechnung der Edelmetalle gegenueber einem breiten Rohstoff-
    Index - analog zu get_benchmark_close()/get_eu_benchmark_close() in
    analyse.py, nur mit einem Rohstoff- statt einem Aktien-Index."""
    try:
        hist = get_yf_history(COMMODITY_BENCHMARK_TICKER)
        if not hist.empty:
            stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
            if getattr(hist.index, 'tz', None) is not None:
                stichtag = stichtag.tz_localize(hist.index.tz)
            hist = hist[hist.index >= stichtag]
        if hist.empty:
            print("DEBUG: Rohstoff-Benchmark (DBC) leer, Relative Stärke wird übersprungen.")
            return None
        hist = hist.dropna(subset=['Close'])
        if hist.empty:
            print("DEBUG: Rohstoff-Benchmark (DBC) nach NaN-Bereinigung leer, Relative Stärke wird übersprungen.")
            return None
        return hist['Close']
    except Exception as e:
        print(f"FEHLER beim Laden der Rohstoff-Benchmark (DBC): {e}")
        return None


def lade_kursdaten(ticker):
    """Laedt die Kursreihe eines Metalls EINMAL pro Lauf (NEU 29.07.2026) -
    vorher holte jede Strategie ihre Daten selbst, was bei drei Strategien
    drei identische yfinance-Abrufe je Metall bedeutet haette.
    Gibt (data, grund) zurueck; grund ist None bei Erfolg, sonst der
    Funnel-Schluessel (fuer alle drei Strategien identisch)."""
    try:
        data = get_yf_history(ticker)
        if not data.empty:
            # Der gemeinsame Cache haelt die Obermenge. Fuer den Scanner
            # verwenden wir weiterhin exakt das bisherige 2-Jahres-Fenster.
            stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=730))
            if getattr(data.index, 'tz', None) is not None:
                stichtag = stichtag.tz_localize(data.index.tz)
            data = data[data.index >= stichtag]
        if data.empty:
            print(f"DEBUG-EDELMETALL: {ticker} -> Daten von yfinance leer.")
            return None, "keine_kursdaten"
        data = data.dropna(subset=['Close', 'High', 'Low'])
        if data.empty:
            print(f"DEBUG-EDELMETALL: {ticker} -> Nach NaN-Bereinigung keine Daten mehr übrig.")
            return None, "keine_kursdaten"
        if len(data) < 210:
            print(f"DEBUG-EDELMETALL: {ticker} -> Zu wenig Daten ({len(data)} Zeilen) für WMA200.")
            return None, "zu_wenig_daten"
        return data, None
    except Exception as e:
        print(f"FEHLER beim Laden von {ticker}: {e}")
        return None, "fehler"


def _metall_felder_setzen(ergebnis, ticker, name, strategie):
    """Vereinheitlicht die Rueckgabe der importierten Aktien-Prueffunktionen
    fuer Metalle: Name/Sektor/Markt/Waehrung korrekt setzen, Strategie-Spalte
    ergaenzen und die bei Rohstoffen sinnlose Fundamental-Ampel eindeutig auf
    N/A setzen (die Aktien-Funktion versucht dort ein KGV zu ziehen, das es
    fuer Edelmetall-Futures nicht gibt)."""
    ergebnis["Ticker"] = ticker
    ergebnis["Name"] = name
    ergebnis["Sektor"] = "Edelmetalle"
    ergebnis["Markt"] = "Global"
    ergebnis["Waehrung"] = "USD"
    ergebnis["Strategie"] = strategie
    ergebnis["Fundamental_Ampel"] = "N/A"
    ergebnis["Fundamental_Hinweis"] = (
        "Rohstoff - keine Unternehmensgewinne, daher keine KGV-Bewertung möglich."
    )
    return ergebnis


def analyze_edelmetall_trendwende(ticker, name, data, bench_close=None):
    """Trendwende (Bodenbildung) fuer ein Metall - ruft die IDENTISCHE
    Pruef-Funktion des Aktien-Trendwende-Scanners auf (Kurs unter WMA200,
    Naehe zum Boden ueber die Spannen-Position (siehe SPANNEN_POSITION_MAX -
    hier ABWEICHEND von den Aktien), RSI-Divergenz im 40-Tage-Fenster als Boden
    UND frischer Kumo-Ausbruch als Trigger, CRV >= 1.0).
    RISIKOKLASSE: wie bei Aktien strukturell riskanter als Trendfolge
    ("fallendes Messer") - der Risikohinweis aus der Aktien-Funktion wird
    unveraendert uebernommen und im Briefing separat ausgewiesen."""
    try:
        res, grund = _pruefe_trendwende(ticker, "Edelmetalle", "Global", data, bench_close,
                                        spannen_position_max=SPANNEN_POSITION_MAX, require_volume=False)
        if res is None:
            return None, grund
        return _metall_felder_setzen(res, ticker, name, "Trendwende"), grund
    except Exception as e:
        print(f"FEHLER Trendwende {ticker}: {e}")
        return None, "fehler"


def analyze_edelmetall_short(ticker, name, data, bench_close=None):
    """Short fuer ein Metall - ruft die IDENTISCHE Pruef-Funktion des
    Aktien-Short-Scanners auf (Kurs unter WMA200, vier gespiegelte Muster,
    RS-Filter invertiert gegen DBC, CRV >= 1.0 aus echten Abwaerts-Levels).
    ZWEI MODIFIKATOREN ENTFALLEN BEWUSST:
      - Sektor-Modifikator: Metalle haben keine Sektor-Rotation und damit
        keinen Rotation-Score (sektor_momentum=None -> Modifikator wird in
        der Aktien-Funktion defensiv uebersprungen).
      - Marktumfeld-Modifikator: das Aktien-Marktumfeld (S&P/Nasdaq/Russell)
        ist fuer Gold/Silber kein sinnvoller Massstab - waere sogar irrefuehrend,
        da Edelmetalle in Aktien-Schwaechephasen klassischerweise GEGENLAEUFIG
        laufen (Krisenwaehrung). Deshalb marktumfeld_baerisch=False.
    Die Setup-Qualitaet stuetzt sich hier allein auf Preis-/Musterlogik; Volumen ist bei Edelmetall-Futures nicht erforderlich.
    RISIKOHINWEIS: theoretisch unbegrenztes Verlustrisiko (wie bei Aktien)."""
    try:
        res, grund = _pruefe_short_setup(
            ticker, "Edelmetalle", "Global", data, bench_close,
            marktumfeld_baerisch=False, sektor_momentum=None,
            require_volume=False,
        )
        if res is None:
            return None, grund
        return _metall_felder_setzen(res, ticker, name, "Short"), grund
    except Exception as e:
        print(f"FEHLER Short {ticker}: {e}")
        return None, "fehler"


def analyze_edelmetall(ticker, name, bench_close=None, data=None):
    """Analysiert ein einzelnes Edelmetall - identische Kriterien wie
    analyze_a_setup_eu() in analyse.py (yfinance-basiert, da Alpaca keine
    Rohstoff-Edelmetalle abdeckt), aber ohne Fundamental-Ampel (kein KGV bei
    Rohstoffen) und ohne Analysten-Kursziel (nicht verfuegbar fuer Edelmetall-Futures).
    Gibt (ergebnis_dict_oder_None, funnel_grund) zurueck - der zweite Wert
    speist die Funnel-Statistik (NEU 28.07.2026). Die Earnings-Regel des
    Hauptscanners entfaellt hier bewusst (Edelmetall-Futures haben keine Earnings),
    die Death-Cross-Regel gilt dagegen identisch (siehe unten).
    """
    try:
        if data is None:
            # Fallback: Einzelaufruf wie frueher (2 Jahre statt 1 - Futures-Historien
            # haben teils luecken-behaftete Historie, mehr Puffer fuer eine
            # zuverlaessige WMA200/EMA200-Berechnung). Im regulaeren Lauf
            # kommen die Daten seit 29.07.2026 vorgeladen aus lade_kursdaten().
            data = get_yf_history(ticker)
            if not data.empty:
                stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=730))
                if getattr(data.index, 'tz', None) is not None:
                    stichtag = stichtag.tz_localize(data.index.tz)
                data = data[data.index >= stichtag]

        if data.empty:
            print(f"DEBUG-EDELMETALL: {ticker} -> Daten von yfinance leer.")
            return None, "keine_kursdaten"

        data = data.dropna(subset=['Close', 'High', 'Low'])
        if data.empty:
            print(f"DEBUG-EDELMETALL: {ticker} -> Nach NaN-Bereinigung keine Daten mehr übrig.")
            return None, "keine_kursdaten"

        if len(data) < 210:  # WMA200 braucht mind. 200 Zeilen, etwas Puffer
            print(f"DEBUG-EDELMETALL: {ticker} -> Zu wenig Daten ({len(data)} Zeilen) für WMA200.")
            return None, "zu_wenig_daten"

        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.000001)
        data['RSI'] = 100 - (100 / (1 + rs))
        data['RSI'] = data['RSI'].fillna(50)
        divergenz = check_rsi_divergence(data)

        data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
        data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
        data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean()
        data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean()
        data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
        data['WMA200'] = data['Close'].rolling(200).apply(lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True)
        if 'Volume' in data.columns and data['Volume'].notna().any():
            data['Vol_SMA20'] = data['Volume'].rolling(20).mean()
            data['Vol_Ratio'] = (data['Volume'] / data['Vol_SMA20']).replace([np.inf, -np.inf], np.nan)
        else:
            data['Vol_SMA20'] = np.nan
            data['Vol_Ratio'] = 1.0

        data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
        data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
        data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)
        data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

        entry = data['Close'].iloc[-1]
        stop = data['Low'].rolling(10).min().iloc[-1]

        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))
        data['Stoch_D'] = data['Stoch_K'].rolling(3).mean()


        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        # Trend-Filter (Pflicht, wie beim Hauptscanner): Kurs muss ueber
        # WMA200 UND EMA200 liegen - hier direkt geprueft statt per
        # nachgelagertem DataFrame-Filter (keine Sektor-Stufe bei nur 4
        # festen Tickern).
        trend_ok = data['Close'].iloc[-1] >= data['WMA200'].iloc[-1] and data['Close'].iloc[-1] >= data['EMA200'].iloc[-1]
        if not trend_ok:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Trend nicht OK (unter WMA200/EMA200)")
            return None, "kein_aufwaertstrend"

        c1, c2 = data.iloc[-1], data.iloc[-2]
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        pattern = "Kein"
        if lower_wick > (2 * body):
            pattern = "Hammer"
        elif c1['Close'] > c1['Open'] and c2['Close'] < c2['Open'] and c1['Close'] > c2['Open'] and c1['Open'] < c2['Close']:
            pattern = "Engulfing"

        crossover_kuerzlich = any(
            data['EMA8'].iloc[-1 - i] <= data['EMA20'].iloc[-1 - i] for i in range(1, 4)
        )
        # Spot-Metalle haben kein belastbares Handelsvolumen; der EMA-Breakout
        # ist deshalb rein preis-/trendbasiert.
        volumen_kuerzlich = True
        ema_breakout = (data['EMA8'].iloc[-1] > data['EMA20'].iloc[-1]) and \
                       crossover_kuerzlich and volumen_kuerzlich

        stoch_k = data['Stoch_K'].iloc[-1]
        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        buffer = 0.01
        price = data['Close'].iloc[-1]

        def ema_pullback_test(ema_series):
            ema_heute = ema_series.iloc[-1]
            nah_dran = abs(price - ema_heute) < (price * buffer)
            war_ueber_ema_kuerzlich = any(
                data['Close'].iloc[-1 - i] >= ema_series.iloc[-1 - i] for i in range(0, 3)
            )
            return nah_dran and war_ueber_ema_kuerzlich

        in_ema_zone_roh = any(ema_pullback_test(s) for s in [data['EMA20'], data['EMA50'], data['Kijun']])
        # Bei Edelmetall-Futures ist Volumen keine Pflichtbedingung; die EMA-Zone
        # wird ausschließlich über Preisnähe und Preisstruktur bewertet.
        volumen_ausreichend = True
        in_ema_zone = in_ema_zone_roh

        trendlinien_ausbruch, tl_level = check_trendline_breakout(data, require_volume=False)
        kumo_ausbruch, kumo_level = check_kumo_breakout(data, require_volume=False)

        in_zone_grund = "OK" if in_ema_zone else ("EMA-Zone nicht erfüllt" if not in_ema_zone_roh else "Preiszone nicht erfüllt")
        print(f"DEBUG-EDELMETALL: {ticker} ({name}) | Breakout: {ema_breakout} | InZone: {in_ema_zone} (Grund: {in_zone_grund}) | "
              f"HL: {is_higher_low} | Stoch: {stoch_k:.1f} | TL-Ausbruch: {trendlinien_ausbruch} | Kumo-Ausbruch: {kumo_ausbruch}")

        if (ema_breakout or (in_ema_zone and is_higher_low) or trendlinien_ausbruch or kumo_ausbruch) and stoch_k < 90:
            pfade = []
            if trendlinien_ausbruch:
                pfade.append("Trendlinien-Ausbruch")
            if kumo_ausbruch:
                pfade.append("Kumo-Ausbruch")
            if ema_breakout:
                pfade.append("EMA-Breakout")
            if in_ema_zone and is_higher_low:
                pfade.append("Pullback-Zone")
            setup_typ = " + ".join(pfade)
        else:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Haupt-Filter nicht erfüllt "
                  f"(Breakout={ema_breakout}, InZone={in_ema_zone}, HL={is_higher_low}, "
                  f"TL-Ausbruch={trendlinien_ausbruch}, Kumo-Ausbruch={kumo_ausbruch}, Stoch={stoch_k:.1f})")
            return None, "kein_setup_muster"

        # Relative Staerke vs. Rohstoff-Index (DBC) statt SPY/STOXX600
        rel_staerke = None
        if bench_close is not None and len(bench_close) > 60 and len(data) > 60:
            metall_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
            bench_perf_60 = ((bench_close.iloc[-1] / bench_close.iloc[-60]) - 1) * 100
            rel_staerke = round(metall_perf_60 - bench_perf_60, 2)
            if rel_staerke <= RS_MIN:
                print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Relative Stärke vs. DBC <= {RS_MIN}% ({rel_staerke}%)")
                return None, "rel_staerke_zu_schwach"

        hoch_52w = data['High'].max()
        abstand_52w_hoch = round(((entry / hoch_52w) - 1) * 100, 2)
        if abstand_52w_hoch < ABSTAND_52W_HOCH_MAX:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Zu weit vom 52-Wochen-Hoch entfernt ({abstand_52w_hoch}%, Hoch={hoch_52w:.2f})")
            return None, "zu_weit_vom_52w_hoch"

        fib1, fib2 = get_fib_levels(data)
        kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
        potenzial_targets = sorted([data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1],
                                     data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte)
        targets_above = [t for t in potenzial_targets if t > entry]

        tp1 = targets_above[0] if targets_above else entry * 1.08
        tp2 = targets_above[1] if len(targets_above) >= 2 else tp1 * 1.05

        is_pullback_setup = (not ema_breakout) and in_ema_zone and is_higher_low
        if is_pullback_setup:
            swing_low_stop = data['Low'].iloc[-5:].min()
            if swing_low_stop < entry:
                stop = swing_low_stop
            vorlauf = data.iloc[-40:-3]
            if not vorlauf.empty:
                swing_high_target = vorlauf['High'].max()
                if swing_high_target > entry:
                    tp1 = swing_high_target
                    hoehere_ziele = [t for t in targets_above if t > tp1]
                    tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # Realitaets-Deckel (wie beim Hauptscanner)
        realer_deckel_120 = data['High'].iloc[-120:].max()
        if realer_deckel_120 > entry and tp1 > realer_deckel_120:
            tp1 = realer_deckel_120
            hoehere_ziele = [t for t in targets_above if t > tp1]
            tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        tp2 = _begrenze_tp2_realitaetsdeckel(tp1, tp2, entry, data)

        risiko = entry - stop
        if risiko <= 0:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Risiko <= 0 (Entry={entry:.2f}, Stop={stop:.2f})")
            return None, "risiko_ungueltig"

        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
        chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
        chance2_perc = round(((tp2 - entry) / entry) * 100, 2)
        if crv1 < 1.0 or crv2 < 1.0:
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: CRV zu niedrig (CRV1={crv1}, CRV2={crv2}, TP1={tp1:.2f}, TP2={tp2:.2f}, Entry={entry:.2f}, Risiko={risiko:.2f})")
            # GEAENDERT (30.07.2026, Nutzerwunsch): Dict mit eigenem Strategie-
            # Feld statt Substring-Marker "[Trendfolge]" im Text - vermeidet
            # bruechiges String-Matching bei der Ausgabe weiter unten und
            # traegt den CRV-Sortierwert fuer die absteigende Sortierung.
            BEINAHE_EDELMETALL.append({
                "text": f"{name} ({ticker}): CRV-Filter -> CRV1 {crv1} / CRV2 {crv2} "
                       f"(Mindestwert 1.0), Kurs {entry:.2f}, TP1 {tp1:.2f}, Stop-Risiko {risiko:.2f}",
                "crv_sortier": min(crv1, crv2),
                "strategie": "Trendfolge",
            })
            return None, "crv_unter_1"

        risk_perc = round(((entry - stop) / entry) * 100, 2)
        last_row = data.iloc[-1]

        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG-EDELMETALL-VERWORFEN: {ticker} | Grund: Plausibilitätscheck fehlgeschlagen")
            return None, "plausibilitaet"

        # Death-Cross-Regel (NEU 28.07.2026, identisch zum Hauptscanner):
        # frischer Death Cross (EMA50 kreuzt EMA200 nach unten, letzte 10
        # Handelstage) stuft VALIDE auf ACHTUNG ab. Die Earnings-Regel des
        # Hauptscanners entfaellt bewusst - Edelmetall-Futures haben keine Earnings.
        gc_status = get_golden_cross_status(data)
        if str(gc_status).startswith("DEATH CROSS"):
            status2, status_grund = "ACHTUNG", "Frischer Death Cross (EMA50 unter EMA200)"
            print(f"DEBUG-EDELMETALL-ABSTUFUNG: {ticker} -> ACHTUNG (frischer Death Cross)")
        else:
            status2, status_grund = "VALIDE", "Alles ok"

        return {
            "Ticker": str(ticker), "Name": str(name), "Sektor": "Edelmetalle",
            "Strategie": "Trendfolge",  # NEU 29.07.2026 (Strategie-Spalte)
            "Markt": "Global", "Waehrung": "USD", "Trend": "OK",
            "Setup_Typ": str(setup_typ), "Pattern": str(pattern),
            "Golden_Cross_Status": gc_status,
            "Tech-Kursziel": clean_num(tp1), "Analysten-Kursziel": 0.0,
            "Upside_%_vs_Aktuell": clean_num(chance1_perc),
            "Status2": status2, "Status_Grund": status_grund,
            "RSI": float(last_row['RSI']), "Divergenz": divergenz if divergenz else "Keine",
            "MACD_Trend": str(macd_trend), "CRV1": clean_num(crv1), "CRV2": clean_num(crv2),
            "Kurs": round(last_row['Close'], 2),
            "Chance1_Perc": clean_num(chance1_perc), "Chance2_Perc": clean_num(chance2_perc),
            "Einstieg": round(last_row['Close'], 2), "Einstieg2(EMA 20)": round(last_row['EMA20'], 2),
            "Stop": clean_num(stop), "Risk_Perc": clean_num(risk_perc),
            "TP1": clean_num(tp1), "TP2": clean_num(tp2),
            "Stoch_K": float(stoch_k), "Vol_Ratio": clean_num(last_row['Vol_Ratio']),
            "Ideales_Delta": 0.0,
            "RS_vs_Benchmark%": clean_num(rel_staerke) if rel_staerke is not None else None,
            "Abstand_52W_Hoch%": clean_num(abstand_52w_hoch),
        }, "kandidat"
    except Exception as e:
        print(f"FEHLER bei {ticker} ({name}): {e}")
        return None, "fehler"


def _funnel_text_bauen(funnel, stufen, kopfzeile, funnel_namen=None):
    """Baut den Funnel-Block einer Strategie. Bei nur 4 Instrumenten sind
    leere Stufen reines Rauschen und werden weggelassen.

    funnel_namen (NEU 31.07.2026, Nutzerwunsch): optionales Dict
    {stufen_key: [Metallname, ...]} - haengt bei nur 4 Instrumenten die
    tatsaechlichen Namen direkt an die Zahl an (z. B. "...: 1 (Palladium)"
    statt nur "...: 1"). Damit muss die Auswertung nicht mehr raten oder
    umschreiben, WELCHES Metall gemeint ist - der Satz zur Engstelle kann es
    direkt uebernehmen. Ohne das Dict (Rueckwaertskompatibilitaet) faellt
    die Funktion auf die reine Zahl zurueck."""
    zeilen = [kopfzeile]
    for i, (key, beschreibung) in enumerate(stufen):
        # Die LETZTE Stufe ist immer die Treffer-Zeile - sie wird auch bei 0
        # ausgegeben (Fix 29.07.2026: vorher haing das am Schluesselnamen
        # "kandidat", weshalb die Trendwende-Ergebniszeile ["valide"] bei 0
        # Treffern komplett fehlte).
        ist_ergebniszeile = (i == len(stufen) - 1)
        anzahl = funnel.get(key, 0)
        if anzahl == 0 and not ist_ergebniszeile:
            continue
        praefix = "=>" if ist_ergebniszeile else "-"
        namen = (funnel_namen or {}).get(key)
        namen_zusatz = f" ({', '.join(namen)})" if namen else ""
        zeilen.append(f"{praefix} {beschreibung}: {anzahl}{namen_zusatz}")
    return "\n".join(zeilen)


# Funnel-Stufen je Strategie (Reihenfolge = Pruefreihenfolge im Code)
FUNNEL_STUFEN_TRENDFOLGE = [
    ("keine_kursdaten", "Keine Kursdaten geliefert (API/NaN-Bereinigung)"),
    ("zu_wenig_daten", "Zu wenig Kurshistorie fuer WMA200"),
    ("fehler", "Fehler bei der Berechnung"),
    ("kein_aufwaertstrend", "Kein Aufwaertstrend (unter WMA200/EMA200)"),
    ("kein_setup_muster", "Keines der 4 Setup-Muster erfuellt (oder Stochastik >= 90)"),
    ("rel_staerke_zu_schwach", "Relative Staerke vs. DBC zu schwach"),
    ("zu_weit_vom_52w_hoch", "Zu weit unter dem 52W-Hoch"),
    ("risiko_ungueltig", "Risiko <= 0"),
    ("crv_unter_1", "CRV-Filter (TP1 oder TP2 unter 1.0)"),
    ("plausibilitaet", "Plausibilitaets-Check fehlgeschlagen"),
    ("kandidat", "SETUP (inkl. eventueller ACHTUNG-Abstufung)"),
]

FUNNEL_STUFEN_TRENDWENDE = [
    ("keine_kursdaten", "Keine Kursdaten geliefert (API/NaN-Bereinigung)"),
    ("zu_wenig_daten", "Zu wenig Kurshistorie"),
    ("fehler", "Fehler bei der Berechnung"),
    ("nicht_unter_wma200", "Kein Abwaertstrend (nicht unter der WMA200)"),
    ("zu_weit_vom_52w_tief", f"Position in der 52W-Spanne ueber {SPANNEN_POSITION_MAX:.0%} "
                             f"(zu weit vom Boden weggelaufen)"),
    ("keine_divergenz", "Keine intakte bullische RSI-Divergenz (40T-Fenster)"),
    ("kein_frischer_kumo_ausbruch", "Kein frischer Kumo-Ausbruch (letzte 5 Handelstage)"),
    ("crv_unter_1", "CRV-Filter (TP1 oder TP2 unter 1.0)"),
    ("valide", "TRENDWENDE-KANDIDAT"),
]

FUNNEL_STUFEN_SHORT = [
    ("keine_kursdaten", "Keine Kursdaten geliefert (API/NaN-Bereinigung)"),
    ("zu_wenig_daten", "Zu wenig Kurshistorie"),
    ("fehler", "Fehler bei der Berechnung"),
    ("kein_abwaertstrend", "Kein Abwaertstrend (Kurs nicht unter WMA200)"),
    ("kein_setup_muster", "Keines der 4 gespiegelten Setup-Muster erfuellt"),
    ("macd_bullisch_ohne_divergenz", "Bullischer MACD ohne baerische Divergenz"),
    ("rs_zu_stark", "Relative Staerke > +10% (kein Short auf Marktfuehrer)"),
    ("crv_unter_1", "CRV-Filter (TP1 oder TP2 unter 1.0)"),
    ("kandidat", "SHORT-KANDIDAT"),
]


def edelmetalle_scan_starten():
    """Prueft alle 4 Metalle gegen ALLE DREI Strategien (GEAENDERT 29.07.2026).
    Die Kursdaten werden je Metall genau einmal geladen und an alle drei
    Pruefungen weitergereicht."""
    print("Edelmetalle-Scanner gestartet (Trendfolge + Trendwende + Short)...")
    bench_close = get_commodity_benchmark_close()

    ergebnisse = []
    funnel_tf, funnel_tw, funnel_sh = Counter(), Counter(), Counter()
    # Metallnamen je Ablehnungsstufe (NEU 31.07.2026, Nutzerwunsch: "alle
    # vier direkt benennen" statt nur "3 Metalle"/"1 Metall" zu zaehlen -
    # bei nur 4 Instrumenten ist eine Zahl ohne Namen unnoetig vage).
    namen_tf, namen_tw, namen_sh = (defaultdict(list) for _ in range(3))
    # DIAGNOSE je Metall (NEU 29.07.2026): bei nur 4 Instrumenten ist die
    # blosse Funnel-Zahl ("3x zu weit vom Tief") zu grob - hier steht je
    # Metall der konkrete Wert, an dem es haengt. Macht sofort sichtbar, ob
    # eine Schwelle sinnvoll greift oder ob sie fuer Rohstoffe nachjustiert
    # werden muss (Anlass: Gold zeigt lehrbuchmaessige Bodenbildung, liegt
    # nach der grossen Vorjahres-Rally aber rechnerisch weit ueber seinem
    # 52-Wochen-Tief - der fuer AKTIEN gedachte 20%-Filter trifft hier
    # womoeglich das Falsche).
    diagnose_zeilen = []

    for ticker, name in EDELMETALLE.items():
        data, ladefehler = lade_kursdaten(ticker)
        if data is None:
            # Datenfehler betrifft alle drei Strategien gleichermassen
            for f in (funnel_tf, funnel_tw, funnel_sh):
                f[ladefehler] += 1
            for n in (namen_tf, namen_tw, namen_sh):
                n[ladefehler].append(name)
            continue

        # 1) Trendfolge (Bestand) - .copy(), damit die Indikator-Spalten der
        #    einen Strategie die naechste nicht beeinflussen
        res, grund = analyze_edelmetall(ticker, name, bench_close, data=data.copy())
        funnel_tf[grund] += 1
        namen_tf[grund].append(name)
        if res is not None:
            ergebnisse.append(res)

        # WICHTIG (Fix 29.07.2026, erster Echtlauf): Trendwende und Short
        # bekommen nur das 52-WOCHEN-FENSTER. Grund: beide importierten
        # Aktien-Funktionen berechnen 52W-Tief/-Hoch als min()/max() ueber die
        # GESAMTE uebergebene Reihe - die Aktien-Scanner fuettern 365 Tage, wir
        # laden fuer die Metalle aber bewusst 2 Jahre (WMA200-Puffer bei
        # luecken-behafteter Futures-Historie). Ungeschnitten waere das
        # "52W-Tief" faktisch das 2-JAHRES-Tief; im ersten Echtlauf fielen
        # dadurch ALLE VIER Metalle faelschlich durch den Naehe-Filter.
        # GEAENDERT (29.07.2026, Nutzerwunsch): Abgrenzung nach DATUM statt
        # nach Zeilenzahl. tail(252) zaehlt Zeilen und unterstellt 252
        # Handelstage pro Jahr - bei Luecken in der Futures-Historie (Feiertage,
        # Ausfaelle, verkuerzte Reihen) reicht das Fenster dann still weiter
        # als 12 Monate zurueck und verzerrt Tief/Hoch genau wie oben, nur
        # schwaecher. Der Datumsschnitt trifft immer exakt 52 Wochen.
        stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
        try:
            index_zeiten = data.index
            if getattr(index_zeiten, 'tz', None) is not None:
                # yfinance liefert je nach Instrument tz-bewusste Zeitstempel -
                # der Vergleich mit einem naiven Stichtag wuerde dann werfen.
                stichtag = stichtag.tz_localize(index_zeiten.tz)
            data_1j = data[index_zeiten >= stichtag]
        except Exception as e:
            print(f"DEBUG-EDELMETALL: {ticker} -> Datums-Schnitt nicht moeglich "
                  f"({type(e).__name__}), nutze Fallback tail(252).")
            data_1j = data.tail(252)
        # Sicherheitsnetz: bleibt zu wenig uebrig (kurze/luecken-behaftete
        # Reihe), lieber die letzten 252 Zeilen als eine unbrauchbar kurze.
        if len(data_1j) < 60:
            print(f"DEBUG-EDELMETALL: {ticker} -> nur {len(data_1j)} Zeilen im "
                  f"52-Wochen-Fenster, nutze Fallback tail(252).")
            data_1j = data.tail(252)

        # Diagnose-Werte auf demselben 1-Jahres-Fenster wie die Pruefungen
        try:
            # GEAENDERT (08.08.2026, Nutzerwunsch "Gold, Silber, Platin, "
            # "Palladium sollen ebenso den 4-Wochen-Vergleich wie Oel "
            # "erhalten"): gilt jetzt einheitlich fuer alle vier Metalle,
            # nicht mehr nur Gold (siehe get_kurzfrist_kontext_text in
            # analyse.py fuer die volle Begruendung der Umstellung selbst).
            kurzfrist_zeile = get_kurzfrist_kontext_text(ticker, name)
            if kurzfrist_zeile:
                diagnose_zeilen.append(f"  {kurzfrist_zeile}")
            else:
                diagnose_zeilen.append(f"  {name} ({ticker}): Diagnose nicht berechenbar")
            # Rekord-Naehe (NEU 30.07.2026, Nutzerwunsch): nur anhaengen, wenn
            # ueberhaupt relevant (Funktion liefert sonst None) - haelt die
            # taegliche Diagnose bei "nichts Besonderes" kompakt.
            rekord_text = get_rekord_naehe_text(ticker, name)
            if rekord_text:
                diagnose_zeilen.append(f"    -> {rekord_text}")
            # Saisonalitaet (NEU 02.08.2026, Nutzerwunsch, Quelle: vom Nutzer
            # bereitgestelltes PDF "RealMoneyTrader Research") - kalender-
            # basierter Kontext, kein API-Aufruf, nur bei Treffer angehaengt.
            saison_text = get_saisonalitaet_text(name)
            if saison_text:
                diagnose_zeilen.append(f"    -> {saison_text}")
        except Exception:
            diagnose_zeilen.append(f"  {name} ({ticker}): Diagnose nicht berechenbar")

        # 2) Trendwende (NEU)
        res, grund = analyze_edelmetall_trendwende(ticker, name, data_1j.copy(), bench_close)
        funnel_tw[grund] += 1
        namen_tw[grund].append(name)
        if res is not None:
            ergebnisse.append(res)

        # 3) Short (NEU)
        res, grund = analyze_edelmetall_short(ticker, name, data_1j.copy(), bench_close)
        funnel_sh[grund] += 1
        namen_sh[grund].append(name)
        if res is not None:
            ergebnisse.append(res)

    anzahl = lambda s: sum(1 for r in ergebnisse if r.get("Strategie") == s)
    print(f"DEBUG: {len(ergebnisse)} Edelmetall-Setups gesamt "
          f"(Trendfolge: {anzahl('Trendfolge')} | Trendwende: {anzahl('Trendwende')} | "
          f"Short: {anzahl('Short')}).")

    kopf = f"Universum: {len(EDELMETALLE)} Edelmetalle (feste Liste)"
    diagnose_text = ("LAGE JE METALL (52-Wochen-Fenster nach Datum, Basis aller Schwellen):\n"
                     + "\n".join(diagnose_zeilen)) if diagnose_zeilen else ""
    funnel_texte = {
        "Trendfolge": _funnel_text_bauen(funnel_tf, FUNNEL_STUFEN_TRENDFOLGE, kopf, namen_tf),
        "Trendwende": _funnel_text_bauen(funnel_tw, FUNNEL_STUFEN_TRENDWENDE, kopf, namen_tw),
        "Short": _funnel_text_bauen(funnel_sh, FUNNEL_STUFEN_SHORT, kopf, namen_sh),
    }
    for strategie, text in funnel_texte.items():
        print(f"FUNNEL-STATISTIK ({strategie}):\n{text}")
    if diagnose_text:
        print(diagnose_text)
    return ergebnisse, funnel_texte, diagnose_text


# Vereinigungs-Schema aller drei Strategien (GEAENDERT 29.07.2026): Felder,
# die eine Strategie nicht kennt, bleiben in ihrer Zeile leer - so bleibt es
# EINE Datei mit stabiler Spaltenzahl, filterbar ueber die erste Spalte.
SPALTEN = [
    "Strategie",  # Trendfolge | Trendwende | Short
    "Ticker", "Name", "Sektor", "Markt", "Waehrung", "Trend", "Setup_Typ", "Pattern",
    "Tech-Kursziel", "Analysten-Kursziel", "Upside_%_vs_Aktuell", "Status2", "Status_Grund",
    "RSI", "MACD_Trend", "CRV1", "CRV2", "Chance1_Perc", "Chance2_Perc", "Kurs", "Einstieg",
    "Einstieg2(EMA 20)", "Stop", "Risk_Perc", "TP1", "TP2", "Stoch_K", "Vol_Ratio",
    "Ideales_Delta", "RS_vs_Benchmark%", "Abstand_52W_Hoch%", "Abstand_52W_Tief%",
    "Divergenz", "Golden_Cross_Status", "Qualitaets_Bonus", "Setup_Qualitaet",
    "Fundamental_Ampel", "Fundamental_Hinweis", "Risikohinweis",
]


def _schreibe_setup_block(f, r):
    """Gibt einen Treffer aus - Felder, die die jeweilige Strategie nicht
    kennt, werden uebersprungen (die drei Strategien liefern leicht
    unterschiedliche Kennzahlen)."""
    status = f" | Status: {r['Status2']} ({r['Status_Grund']})" if r.get('Status2') else ""
    f.write(f"{r['Ticker']} ({r['Name']}){status}\n")
    f.write(f"Kurs: {r['Kurs']}$\n")
    if r.get('Tech-Kursziel'):
        f.write(f"Technisches Kursziel: {r['Tech-Kursziel']}$\n")
    f.write(f"Stop: {r['Stop']}$ | Risiko: {r['Risk_Perc']}%\n")
    f.write(f"TP1: {r['TP1']}$ (Chance: {r['Chance1_Perc']}%) | CRV1: {r['CRV1']}\n")
    f.write(f"TP2: {r['TP2']}$ (Chance: {r['Chance2_Perc']}%) | CRV2: {r['CRV2']}\n")
    vol = r.get('Vol_Ratio')
    vol_txt = f" | Vol-Ratio: {vol:.2f}x" if isinstance(vol, (int, float)) else ""
    f.write(f"RSI: {r['RSI']:.2f} | MACD-Trend: {r['MACD_Trend']}{vol_txt}"
            f" | Divergenz: {r.get('Divergenz', 'n/a')}\n")
    rs = r.get('RS_vs_Benchmark%')
    rs_txt = f"{rs}%" if rs is not None else "n/a"
    if r.get('Abstand_52W_Tief%') is not None:
        abstand = f"Abstand 52W-Tief: {r['Abstand_52W_Tief%']}%"
    else:
        abstand = f"Abstand 52W-Hoch: {r.get('Abstand_52W_Hoch%', 'n/a')}%"
    f.write(f"RS vs. DBC (Rohstoff-Index): {rs_txt} | {abstand}\n")
    if r.get('Golden_Cross_Status'):
        f.write(f"Golden-/Death-Cross (frischer Death Cross führt zu ACHTUNG): {r['Golden_Cross_Status']}\n")
    if r.get('Qualitaets_Bonus'):
        f.write(f"Qualitaets-Bonus: {r['Qualitaets_Bonus']}\n")
    if r.get('Setup_Qualitaet'):
        f.write(f"Setup-Qualitaet: {r['Setup_Qualitaet']}\n")
    f.write(f"Setup-Typ: {r['Setup_Typ']}")
    if r.get('Pattern'):
        f.write(f" | Muster: {r['Pattern']}")
    f.write("\n")
    if r.get('Risikohinweis'):
        f.write(f"RISIKOHINWEIS: {r['Risikohinweis']}\n")
    f.write("\n")


# Strategie-Beschreibungen fuer das Briefing (je Abschnitt ein Block)
STRATEGIE_TEXTE = {
    "Trendfolge": (
        "- Grundidee: identische Kriterien wie der Hauptscanner (Trendfolge/\n"
        "  Fortsetzung), angewendet auf Gold/Silber/Platin/Palladium statt auf\n"
        "  Aktien - damit handelbar wie ein normales Setup.\n"
        "- Trend-Filter: Kurs muss über WMA200 UND EMA200 liegen (wie Hauptscanner).\n"
        "- Setup: EMA8/20-Breakout ODER Pullback (Zone/Higher-Low) ODER Trendlinien-\n"
        "  Ausbruch ODER Kumo-Ausbruch (Setup_Typ listet ALLE zutreffenden Pfade auf).\n"
        "- Risiko: CRV (Chance/Risiko) muss bei TP1 und TP2 jeweils >= 1.0 sein.\n"
    ),
    "Trendwende": (
        "- Grundidee (NEU 29.07.2026): Gegenteil der Trendfolge - sucht die\n"
        "  Bodenbildung nach einem Fall. Anlass: Gold und Silber notieren seit\n"
        "  Wochen unter allen Durchschnitten, der Trendfolge-Filter meldet deshalb\n"
        "  dauerhaft 0 Setups - die komplette Bodenbildung und die erste Erholung\n"
        "  lagen bisher im blinden Fleck des Scanners.\n"
        "- Identische Kriterien wie der Aktien-Trendwende-Scanner (dieselbe\n"
        "  Pruef-Funktion, nur auf Metalle angewendet):\n"
        "  Trend-Filter umgekehrt (Kurs UNTER der WMA200) und als Pflicht-SEQUENZ:\n"
        "  bullische RSI-Divergenz\n"
        "  (Boden, 40-Tage-Fenster, seitdem nicht invalidiert) UND frischer\n"
        "  Kumo-Ausbruch (Trigger, letzte 5 Handelstage). CRV >= 1.0.\n"
        "- ABWEICHUNG von den Aktien (NEU 29.07.2026): Die Naehe zum Boden wird ueber\n"
        f"  die POSITION IN DER 52-WOCHEN-SPANNE gemessen - (Kurs-Tief)/(Hoch-Tief) muss\n"
        f"  <= {SPANNEN_POSITION_MAX:.0%} sein - statt ueber den Prozentabstand zum Tief. Grund: bei\n"
        "  Metallen haengt der Prozentabstand stark von der Jahresvolatilitaet ab.\n"
        "  Silber lag am 29.07. 52% UNTER seinem Jahreshoch (klarer Boden), zugleich\n"
        "  aber 59% UEBER seinem Jahrestief - die Aktien-Regel haette genau diesen\n"
        "  Kandidaten aussortiert. Die Spannen-Position ist volatilitaetsunabhaengig.\n"
        "- RISIKOKLASSE: strukturell riskanter als Trendfolge ('fallendes Messer' -\n"
        "  ein Boden kann trotz Divergenz und Ausbruch weiter fallen). Deshalb\n"
        "  eigener Abschnitt, eigenes Label, NICHT mit der Trendfolge vermischt.\n"
    ),
    "Short": (
        "- Grundidee (NEU 29.07.2026): Spiegelbild der Trendfolge - setzt auf\n"
        "  FALLENDE Metallpreise (Put/KO statt Call).\n"
        "- Identische Kriterien wie der Aktien-Short-Scanner (dieselbe Pruef-\n"
        "  Funktion): Kurs unter WMA200, vier gespiegelte Muster (EMA-Breakdown,\n"
        "  Pullback-Zone short, Trendlinien-Bruch, Kumo-Ausbruch nach unten),\n"
        "  RS-Filter invertiert (kein Short auf Marktfuehrer), CRV >= 1.0 aus\n"
        "  echten Abwaerts-Levels.\n"
        "- ZWEI MODIFIKATOREN ENTFALLEN BEWUSST: der Sektor-Modifikator (Metalle\n"
        "  haben keine Sektor-Rotation, also keinen Rotation-Score) und der\n"
        "  Marktumfeld-Modifikator (das Aktien-Marktumfeld ist fuer Edelmetalle\n"
        "  kein sinnvoller Massstab - Metalle laufen in Aktien-Schwaechephasen\n"
        "  klassischerweise gegenlaeufig). Die Setup-Qualitaet stuetzt sich hier\n"
        "  allein auf Preis-/Musterlogik; Volumen ist bei Spot-Metallen nicht erforderlich.\n"
        "- RISIKOHINWEIS: theoretisch unbegrenztes Verlustrisiko bei Kursanstieg.\n"
    ),
}


def speichere_ergebnisse(ergebnisse, funnel_texte=None, diagnose_text=""):
    """Schreibt CSV (alle Strategien, Spalte 'Strategie') und Briefing mit
    DREI getrennten Abschnitten (GEAENDERT 29.07.2026)."""
    if funnel_texte is None:
        funnel_texte = {}
    heute = datetime.date.today().isoformat()
    df = pd.DataFrame(ergebnisse, columns=SPALTEN)  # feste Spaltenliste, auch bei 0 Zeilen
    if not df.empty:
        # Sortierung: erst nach Strategie (feste Reihenfolge wie im Briefing),
        # dann bestes CRV1 zuerst - es gibt hier keine Sektor-Rotation als
        # Sortierschluessel.
        reihenfolge = {"Trendfolge": 0, "Trendwende": 1, "Short": 2}
        df["_rang"] = df["Strategie"].map(reihenfolge).fillna(9)
        df = df.sort_values(["_rang", "CRV1"], ascending=[True, False]).drop(columns=["_rang"])

    dateiname_csv = f"Edelmetalle_Setups({heute}).csv"
    df.to_csv(dateiname_csv, index=False, sep=';', encoding='utf-8-sig')
    print(f"Gespeichert: {dateiname_csv}")

    dateiname_txt = f"Edelmetalle_Briefing({heute}).txt"
    with open(dateiname_txt, "w", encoding="utf-8-sig") as f:
        f.write(f"EDELMETALLE-SCAN {heute}\n")
        f.write("=" * 50 + "\n\n")
        f.write("GEMEINSAME GRUNDLAGEN (gelten fuer alle drei Strategien)\n")
        f.write("-" * 50 + "\n")
        f.write(
            "- Universum: feste 4er-Liste (keine Sektor-Rotation, immer alle 4 geprüft).\n"
            "- Kursbasis: Futures (GC=F/SI=F/PL=F/PA=F) - Yahoo-Futures als Datenbasis\n"
            "  Kursreihe (kein ETF-Tracking-Fehler, kein Alpaca, das Rohstoffe nicht abdeckt).\n"
  "  Volumen: fuer Edelmetall-Futures nicht erforderlich; fehlendes Handelsvolumen blockiert\n"
  "  keine preis-/trendbasierte Setup-Pruefung.\n"
            "- Relative Stärke: gegen DBC (Rohstoff-Index-ETF) statt SPY/STOXX600 -\n"
            "  ein Aktienindex wäre als Vergleichsmaßstab für Edelmetalle nicht sinnvoll.\n"
            "- Fundamental-Ampel (KGV) entfällt bewusst - Rohstoffe haben keine\n"
            "  Unternehmensgewinne. Analysten-Kursziel entfällt ebenfalls (nicht\n"
            "  verfügbar für Spot-Metalle) - Tech-Kursziel bleibt einzige Zielgröße.\n"
            "- DREI STRATEGIEN (NEU 29.07.2026): Trendfolge, Trendwende und Short laufen\n"
            "  in EINEM Scanner (gleiche Daten, ein Abruf), werden aber getrennt\n"
            "  ausgewiesen - CSV-Spalte 'Strategie', hier je ein eigener Abschnitt mit\n"
            "  eigener Funnel-Statistik. Die Pruef-Logik ist jeweils IDENTISCH mit dem\n"
            "  entsprechenden Aktien-Scanner (dieselben Funktionen, nur auf Metalle\n"
            "  angewendet) - Aktien- und Metall-Variante koennen nicht auseinanderlaufen.\n\n"
        )

        if diagnose_text:
            f.write(diagnose_text + "\n\n")

        for strategie in ("Trendfolge", "Trendwende", "Short"):
            treffer = [r for r in ergebnisse if r.get("Strategie") == strategie]
            f.write("=" * 50 + "\n")
            f.write(f"STRATEGIE: {strategie.upper()}\n")
            f.write("=" * 50 + "\n")
            f.write(STRATEGIE_TEXTE[strategie] + "\n")

            if funnel_texte.get(strategie):
                f.write(f"FUNNEL-STATISTIK {strategie} (Ablehnungsgruende je Pruefstufe)\n")
                f.write("-" * 50 + "\n")
                f.write(funnel_texte[strategie] + "\n\n")

            # Beinahe-Kandidaten dieser Strategie (NEU 30.07.2026, seit dieser
            # Aenderung als Dicts mit crv_sortier statt reiner String-Liste -
            # ermoeglicht die absteigende CRV-Sortierung, Nutzerwunsch)
            if strategie == "Short":
                beinahe = list(BEINAHE_SHORT)
            else:
                beinahe = [z for z in BEINAHE_EDELMETALL if z["strategie"] == strategie]
            if beinahe:
                f.write(f"BEINAHE-KANDIDATEN {strategie} (Muster erfuellt, erst am CRV-Filter gescheitert)\n")
                f.write("-" * 50 + "\n")
                f.write("(nur Beobachtung, KEINE Setups)\n")
                # Leerzeile zwischen den Eintraegen (NEU 31.07.2026, Nutzerwunsch
                # "Uebersichtlichkeit") - analog zum Hauptscanner/Short-Scanner.
                for eintrag in sorted(beinahe, key=lambda x: -x["crv_sortier"]):
                    f.write(eintrag["text"] + "\n\n")

            if not treffer:
                f.write(f"Keine {strategie}-Kandidaten gefunden.\n\n")
            else:
                for r in sorted(treffer, key=lambda x: x.get("CRV1") or 0, reverse=True):
                    _schreibe_setup_block(f, r)

    print(f"Gespeichert: {dateiname_txt}")
    return dateiname_csv, dateiname_txt


if __name__ == "__main__":
    ergebnisse, funnel_texte, diagnose_text = edelmetalle_scan_starten()
    speichere_ergebnisse(ergebnisse, funnel_texte, diagnose_text)
    print("Edelmetalle-Scanner abgeschlossen.")
