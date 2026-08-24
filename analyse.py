import pandas as pd
import numpy as np
import yfinance as yf
import datetime
from zoneinfo import ZoneInfo
import json
import time
import sys
import os
import re
import io
import requests
from scipy.signal import argrelextrema
from groq import Groq
from market_data import fetch_us_batch_robust

from market_cache import get_yf_history, get_or_fetch_series
from pi_cycle_bottom import get_pi_cycle_bottom_text, calculate_pi_cycle_bottom
from bitcoin_50w_sma import get_bitcoin_50w_sma_text, calculate_bitcoin_50w_sma

# Importe für Alpaca
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
# News-API (Benzinga-Feed, in den bestehenden Alpaca-Keys enthalten) - defensiv
# importiert, damit ein Versions-Problem der Bibliothek nie den Lauf stoppt
try:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    NEWS_VERFUEGBAR = True
except Exception:
    NEWS_VERFUEGBAR = False
from concurrent.futures import ThreadPoolExecutor
import threading
from collections import Counter

# --- FUNNEL-STATISTIK (NEU 28.07.2026, Nutzerwunsch) ---
# Zählt je Ablehnungsstufe, wie viele Ticker dort rausfallen - macht das
# Tagesergebnis (insbesondere "wenige/keine Setups") interpretierbar, statt
# nur das Endergebnis zu melden. Thread-sicher, da die Analyse parallel läuft.
FUNNEL_HAUPT = Counter()
_funnel_lock = threading.Lock()


def funnel_zaehle(grund):
    with _funnel_lock:
        FUNNEL_HAUPT[grund] += 1


# --- BEINAHE-KANDIDATEN (NEU 30.07.2026, Nutzerwunsch) ---
# Wenn ein Scanner 0 valide Setups meldet, soll die Auswertung nicht nur die
# Stufe nennen, an der es scheiterte, sondern die konkreten Titel MIT WERT.
# Erfasst werden nur die SPAETEN Stufen: ein Titel, der schon ein Setup-Muster
# hatte und erst am CRV/RS/52W-Filter hing, ist naechste Woche vielleicht ein
# Kandidat - einer ohne Muster ist es nicht. Bewusst begrenzt, damit die
# Ausgabe nicht zur zweiten Kandidatenliste wird.
FUNNEL_BEINAHE = []

# --- HEBELTRADER-SETUPS + FUNNEL (NEU 10.08.2026, Nutzerwunsch) ---
# Zusaetzliche, sechste Kategorie neben Trendfolge/Trendwende/Short/Langfrist/
# Edelmetalle - findet explosive Ausbruchs-Setups (Momentum-Ausbruch-Score),
# die die anderen fuenf Kategorien eher als "noch nicht ausgeloest" oder
# "ueberhitzt" ablehnen wuerden. Bewusst als "Bauweise B" gebaut (siehe
# Chat-Diskussion 07.08.2026): KEIN eigener Scan mit eigenem Kursabruf,
# sondern eine Zusatzberechnung INNERHALB des bestehenden Trendfolge-
# Durchlaufs - Stoch_K/EMA50/Vol_Ratio werden dort ohnehin fuer JEDEN Ticker
# berechnet (sichtbar an den DEBUG-Zeilen, die fuer jeden Ticker Stoch
# ausgeben, auch abgelehnte). Dadurch: KEINE zusaetzlichen API-Aufrufe,
# nur ein paar zusaetzliche Pandas-Operationen auf bereits geladenen Daten.
#
# ZWEISTUFIG, weil die Sektor-Rotation (Kriterium 5) erst NACH der Ticker-
# Schleife vollstaendig vorliegt (df_perf/df_perf_eu werden erst danach
# gebaut): Stufe 1 (_hebeltrader_teilkriterien, innerhalb der Schleife)
# sammelt die Kriterien 1-4 plus die rohe Eigen-Performance der Aktie in
# HEBELTRADER_KANDIDATEN. Stufe 2 (_hebeltrader_finalisieren, nach dem Bau
# von df_perf/df_perf_eu) traegt den Sektor-Vergleich nach, berechnet den
# finalen Score und filtert auf die Schwelle.
# HEBELTRADER bewusst streng: nur echte Top-Setups mit ALLEN 5 Kriterien.
# 5/5 bedeutet auch: Das Sektor-Kriterium MUSS verfuegbar sein und erfuellt sein.
HEBELTRADER_SCHWELLE = 5
MOMENTUM_VOL_SCHWELLE = 1.5
MOMENTUM_EMA50_ABSTAND_PROZENT = 5.0
MOMENTUM_EMA50_MAX_ABSTAND_PROZENT = 15.0
HEBELTRADER_KANDIDATEN = []
HEBELTRADER_FUNNEL = {}


def _hebeltrader_teilkriterien(ticker, name, sektor, markt, waehrung, data, entry, stop):
    """Kriterien 1-4 (alles, was OHNE Sektor-Rotation auskommt) plus die
    rohe 5-Tage-Eigenperformance der Aktie, PLUS (NEU 08.08.2026, Nutzerwunsch
    "CRV, Stop-Loss, TP1/TP2 ermitteln, gleiche Gliederung wie alle anderen
    Kategorien") ein eigenstaendiges TP1/TP2/CRV/Risk_Perc - unabhaengig von
    der normalen Trendfolge-Zielberechnung weiter unten in der Funktion, da
    diese bei einem spaeteren fruehen Abbruch (Pattern nicht erfuellt) nie
    erreicht wuerde - genau der Fall, den Hebeltrader zusaetzlich abdecken
    soll. Nutzt dieselben Zutaten (EMAs/Kumo/Fibonacci, Realitaets-Deckel)
    wie die normale TP-Auswahl, nur als eigener, kompakter Rechenweg.
    Wird direkt aus analyze_a_setup/analyze_a_setup_eu heraus aufgerufen,
    NACH Stoch_K/EMA50/Vol_Ratio, VOR jedem moeglichen fruehen Abbruch der
    Funktion - damit werden auch Titel erfasst, die als Trendfolge-Setup
    spaeter verworfen werden."""
    try:
        if len(data) < 60 or 'Stoch_K' not in data.columns or 'EMA50' not in data.columns:
            return
        kurs = float(data['Close'].iloc[-1])
        stoch = float(data['Stoch_K'].iloc[-1])
        vol_ratio = float(data['Vol_Ratio'].iloc[-1]) if pd.notna(data['Vol_Ratio'].iloc[-1]) else 0.0
        ema50 = float(data['EMA50'].iloc[-1])
        abstand_ema50 = (kurs - ema50) / ema50 * 100 if ema50 > 0 else float('nan')

        stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=90))
        idx = data.index
        if getattr(idx, 'tz', None) is not None:
            stichtag = stichtag.tz_localize(idx.tz)
        fenster_3m = data[idx >= stichtag]
        if len(fenster_3m) < 40:
            fenster_3m = data.tail(63)
        # KORREKTUR (09.08.2026, derselbe Bug wie in trendwende_scanner.py's
        # check_multiwochen_ausbruch, dort vom Nutzer per Code-Review gefunden):
        # fenster_3m enthielt bisher den AKTUELLEN Handelstag mit - dadurch
        # konnte sich der Tag teilweise mit seinem eigenen Intraday-Hoch
        # vergleichen statt mit einem echten vorherigen 3-Monats-Hoch. iloc[:-1]
        # nimmt das Fenster OHNE die letzte (heutige) Zeile.
        fenster_3m_ohne_heute = fenster_3m.iloc[:-1] if len(fenster_3m) > 1 else fenster_3m
        hoch_3m = float(fenster_3m_ohne_heute['High'].max())
        # GEAENDERT (08.08.2026, Nutzerwunsch "realistischer bewerten"):
        # Toleranz von 0,1% auf 1% aufgeweitet - Titel, die praktisch am
        # Ausbruch stehen (>=99% des 3-Monats-Hochs), zaehlen jetzt als
        # erfuellt statt nur ein exaktes/fast-exaktes neues Hoch. Bewusst
        # EIN Punkt fuer die ganze 99-100%-Zone (keine 0,5-Punkte-Zwischen-
        # stufe) - Bruchteilspunkte haetten die gesamte Score-/Schwellen-
        # Logik verkompliziert, fuer wenig zusaetzliche Aussagekraft.
        neues_3m_hoch = kurs >= hoch_3m * 0.99

        eigene_5t = None
        if len(data) >= 6:
            basis = float(data['Close'].iloc[-6])
            if basis > 0:
                eigene_5t = (kurs / basis - 1) * 100

        kriterien = {
            "Stochastik > 80": (stoch > 80, f"{stoch:.1f}"),
            "Neues 3-Monats-Hoch (Toleranz 1%)": (neues_3m_hoch, f"Kurs {kurs:.2f} vs. Hoch {hoch_3m:.2f} ({kurs/hoch_3m*100:.1f}%)"),
            f"Volumenanstieg (>{MOMENTUM_VOL_SCHWELLE:.1f}x SMA20)": (vol_ratio > MOMENTUM_VOL_SCHWELLE, f"{vol_ratio:.2f}x"),
            f"Abstand EMA50 (5-15%)": (MOMENTUM_EMA50_ABSTAND_PROZENT <= abstand_ema50 <= MOMENTUM_EMA50_MAX_ABSTAND_PROZENT, f"{abstand_ema50:+.1f}%"),
        }

        # --- Eigenstaendige TP1/TP2/CRV-Berechnung (NEU 08.08.2026) ---
        try:
            kumo_kandidaten = []
            if 'SenkouA' in data.columns and 'SenkouB' in data.columns:
                a, b = data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]
                if pd.notna(a) and pd.notna(b):
                    kumo_kandidaten.append(max(float(a), float(b)))
            fib1, fib2 = get_fib_levels(data)
            ziel_kandidaten = [data['EMA20'].iloc[-1], data['EMA50'].iloc[-1],
                               data['EMA100'].iloc[-1], data['EMA200'].iloc[-1],
                               data['WMA200'].iloc[-1], fib1, fib2] + kumo_kandidaten
            ziel_kandidaten = sorted(float(t) for t in ziel_kandidaten if pd.notna(t) and float(t) > entry)
            tp1 = ziel_kandidaten[0] if ziel_kandidaten else entry * 1.08
            tp2 = ziel_kandidaten[1] if len(ziel_kandidaten) >= 2 else tp1 * 1.05

            realer_deckel_120 = float(data['High'].iloc[-120:].max())
            if realer_deckel_120 > entry and tp1 > realer_deckel_120:
                tp1 = realer_deckel_120
            tp2 = _begrenze_tp2_realitaetsdeckel(tp1, tp2, entry, data)

            risiko = entry - float(stop)
            if risiko <= 0:
                print(
                    f"DEBUG-HEBELTRADER-ZIELE: {ticker} -> ungültiges Risiko "
                    f"(Entry={entry:.2f}, Stop={float(stop):.2f}) - "
                    f"Titel nicht als HebelTrader-Kandidat übernommen."
                )
                return
            chance1 = (tp1 - entry) / entry * 100
            chance2 = (tp2 - entry) / entry * 100
            ziele = {
                "TP1": round(tp1, 2), "TP2": round(tp2, 2),
                "Stop": round(float(stop), 2), "Risk_Perc": round(risiko / entry * 100, 2),
                "Chance1_Perc": round(chance1, 2), "Chance2_Perc": round(chance2, 2),
                "CRV1": round((tp1 - entry) / risiko, 2) if risiko > 0 else None,
                "CRV2": round((tp2 - entry) / risiko, 2) if risiko > 0 else None,
            }
        except Exception as e:
            print(f"DEBUG-HEBELTRADER-ZIELE: {ticker} -> TP/CRV nicht berechenbar ({type(e).__name__}: {e})")
            ziele = {"TP1": None, "TP2": None, "Stop": round(float(stop), 2) if pd.notna(stop) else None,
                    "Risk_Perc": None, "Chance1_Perc": None, "Chance2_Perc": None, "CRV1": None, "CRV2": None}

        with _funnel_lock:
            HEBELTRADER_KANDIDATEN.append({
                "Ticker": str(ticker), "Name": str(name), "Sektor": str(sektor),
                "Markt": str(markt), "Waehrung": str(waehrung), "Kurs": kurs,
                "Kriterien": kriterien, "Eigene_5T": eigene_5t, **ziele,
            })
    except Exception as e:
        print(f"DEBUG-HEBELTRADER: {ticker} -> Teilkriterien nicht berechenbar ({type(e).__name__}: {e})")


def _hebeltrader_finalisieren(df_perf, df_perf_eu):
    """Stufe 2: finalisiert den HebelTrader-Score und baut parallel einen
    transparenten Funnel auf.

    Der Funnel zeigt nicht nur die Zahl der 5/5-Treffer, sondern fuer jeden
    Ausschlussschritt, wie viele Titel dort verloren gehen. Wichtig: Ein
    Titel wird fuer die Funnel-Statistik jeweils nur einmal je Stufe gezählt;
    bei den vier Momentum-Kriterien werden zusaetzlich die Einzel-Ausfaelle
    ausgewiesen. Die bestehende 5/5- und CRV-Logik bleibt unveraendert.
    """
    global HEBELTRADER_FUNNEL

    sektor_5t = {}
    sektor_rotation = {}
    try:
        top_8 = set(df_perf.nlargest(8, 'Rotation-Score')['Sektor'].tolist())
        for _, z in df_perf.iterrows():
            sektor_5t[str(z['Sektor'])] = float(z['5T'])
            sektor_rotation[str(z['Sektor'])] = (float(z['Rotation-Score']), str(z['Sektor']) in top_8)
    except Exception:
        top_8 = set()
    try:
        top_5_eu = set(df_perf_eu.nlargest(5, 'Rotation-Score')['Sektor'].tolist())
        for _, z in df_perf_eu.iterrows():
            sektor_5t[str(z['Sektor'])] = float(z['5T'])
            sektor_rotation[str(z['Sektor'])] = (float(z['Rotation-Score']), str(z['Sektor']) in top_5_eu)
    except Exception:
        top_5_eu = set()

    funnel = {
        'gesamt': len(HEBELTRADER_KANDIDATEN),
        'crv_ok': 0,
        'crv_nicht_ok': 0,
        'nach_crv': 0,
        'drop_kriterium_1': 0,
        'drop_kriterium_2': 0,
        'drop_kriterium_3': 0,
        'drop_kriterium_4': 0,
        'nach_kriterium_1': 0,
        'nach_kriterium_2': 0,
        'nach_kriterium_3': 0,
        'nach_kriterium_4': 0,
        'drop_sektor_verfuegbarkeit': 0,
        'nach_sektor_verfuegbarkeit': 0,
        'drop_sektor_rs': 0,
        'kriterium_1_ok': 0,
        'kriterium_1_nicht_ok': 0,
        'kriterium_2_ok': 0,
        'kriterium_2_nicht_ok': 0,
        'kriterium_3_ok': 0,
        'kriterium_3_nicht_ok': 0,
        'kriterium_4_ok': 0,
        'kriterium_4_nicht_ok': 0,
        'sektor_verfuegbar': 0,
        'sektor_nicht_verfuegbar': 0,
        'sektor_rs_ok': 0,
        'sektor_rs_nicht_ok': 0,
        'score_5': 0,
        'score_4': 0,
        'score_3': 0,
        'score_2': 0,
        'score_1': 0,
        'score_0': 0,
        'finale_treffer': 0,
    }

    treffer = []
    for kand in HEBELTRADER_KANDIDATEN:
        crv1, crv2 = kand.get('CRV1'), kand.get('CRV2')
        crv_ok = ((crv1 is not None and crv1 >= 1.0) or
                  (crv2 is not None and crv2 >= 1.0))
        if not crv_ok:
            funnel['crv_nicht_ok'] += 1
            continue
        funnel['crv_ok'] += 1
        funnel['nach_crv'] += 1

        kriterien = dict(kand['Kriterien'])
        kriterium_namen = list(kand['Kriterien'].keys())

        # ECHTER sequenzieller Funnel: Jeder Titel kann hier pro Stufe nur
        # einmal ausscheiden. Zusaetzlich bleiben die Einzelzaehlungen als
        # Diagnose erhalten, damit sichtbar ist, welche Kriterien insgesamt
        # besonders selten erfuellt werden.
        _funnel_verbleibend = True
        for i, key in enumerate(kriterium_namen[:4], start=1):
            ok = bool(kand['Kriterien'][key][0])
            funnel[f'kriterium_{i}_' + ('ok' if ok else 'nicht_ok')] += 1
            if _funnel_verbleibend:
                if ok:
                    funnel[f'nach_kriterium_{i}'] += 1
                else:
                    funnel[f'drop_kriterium_{i}'] += 1
                    _funnel_verbleibend = False

        # Nur Titel, die alle vier Momentum-Kriterien passiert haben, erreichen
        # im echten Funnel den Sektorvergleich.
        if not _funnel_verbleibend:
            continue

        sektor_verfuegbar = kand['Eigene_5T'] is not None and kand['Sektor'] in sektor_5t
        if not sektor_verfuegbar:
            funnel['sektor_nicht_verfuegbar'] += 1
            funnel['drop_sektor_verfuegbarkeit'] += 1
            continue

        funnel['sektor_verfuegbar'] += 1
        funnel['nach_sektor_verfuegbarkeit'] += 1
        sektor_wert = sektor_5t[kand['Sektor']]
        ok = kand['Eigene_5T'] > sektor_wert
        kriterien['Relative Stärke zum Sektor (5T)'] = (
            ok, f"Aktie {kand['Eigene_5T']:+.1f}% vs. Sektor {sektor_wert:+.1f}% (5 Tage)")
        funnel['sektor_rs_' + ('ok' if ok else 'nicht_ok')] += 1
        if not ok:
            funnel['drop_sektor_rs'] += 1
            continue

        score = sum(1 for ok, _ in kriterien.values() if ok)
        if score >= 5:
            funnel['score_5'] += 1
        elif score == 4:
            funnel['score_4'] += 1
        elif score == 3:
            funnel['score_3'] += 1
        elif score == 2:
            funnel['score_2'] += 1
        elif score == 1:
            funnel['score_1'] += 1
        else:
            funnel['score_0'] += 1

        max_punkte = len(kriterien)
        if sektor_verfuegbar and score >= HEBELTRADER_SCHWELLE and max_punkte == 5:
            rot_score, in_top = sektor_rotation.get(kand['Sektor'], (None, None))
            treffer.append({**kand, 'Kriterien_final': kriterien, 'Score': score, 'Max_Punkte': max_punkte,
                            'Rotation_Score': rot_score, 'Sektor_In_Top': in_top})
            funnel['finale_treffer'] += 1

    HEBELTRADER_FUNNEL = funnel
    treffer.sort(key=lambda t: -t['Score'])
    return treffer



def funnel_beinahe(ticker, stufe, detail, crv_sortier=None, name=None):
    """Merkt sich einen spaeten Beinahe-Treffer (thread-sicher).
    crv_sortier (NEU 30.07.2026, Nutzerwunsch): das kleinere der beiden CRVs
    (der Wert, der die Ablehnung ausgeloest hat) - fuer eine absteigende
    Sortierung in der Ausgabe, damit die Titel, die der 1.0-Schwelle am
    naechsten kamen, oben stehen statt alphabetisch verstreut.
    name (NEU 31.07.2026, Bugfix/Nutzerwunsch): der aufgeloeste Firmenname -
    ohne dieses Feld stand in der Ausgabe nur der Ticker (Inkonsistenz zur
    Watchlist, die schon Namen zeigt). Faellt auf den Ticker zurueck, wenn
    kein Name uebergeben wird."""
    with _funnel_lock:
        FUNNEL_BEINAHE.append({"Ticker": str(ticker), "Stufe": stufe, "Detail": detail,
                               "CRV_Sortier": crv_sortier, "Name": name or str(ticker)})


# --- MARKTUMFELD-KLASSIFIKATION (Score-Modell, GEÄNDERT 28.07.2026 abends,
# Nutzerentscheidung - ersetzt "der schwächste Leitindex zählt") ---
# Problem der alten Regel: ein einzelner Ausreißer (z.B. Nasdaq unter EMA50
# wegen eines rein sektoralen Tech-Ausverkaufs) stempelte die GANZE Region
# bärisch. Jetzt gewichtete Durchschnittsnote über drei Indizes je Region:
#   Stufe je Index (unverändert):
#     Bullisch: Kurs über EMA20 (und nicht unter EMA50/WMA200)
#     Neutral:  Kurs unter EMA20, aber über EMA50 und WMA200
#     Bärisch:  Kurs unter EMA50 ODER unter WMA200
#   Punkte je Index: Bullisch 2 | Neutral 1 | Bärisch 0
#   Gewichte USA:    S&P 500 x2 (Leitindex) | Nasdaq x1 (Tech-Frühwarnung)
#                    | Russell 2000 x1 (Marktbreite)
#   Gewichte Europa: DAX x2 (Leitindex) | EuroStoxx50 x1
#                    | STOXX Europe 600 x1 (Marktbreite)
#   Regionen-Score = gewichteter Durchschnitt der Punkte:
#     >= 1.5 Bullisch | <= 0.5 Bärisch | dazwischen Neutral
# Der Dow Jones ist bewusst NUR Info-Zeile in den BENCHMARKS und geht NICHT
# in den Score ein (30 Titel, kursgewichtet, ~0,95 korreliert zum S&P 500 -
# kein Informationsgewinn, würde aber das Nasdaq-Frühwarnsignal verwässern).
# get_index_benchmark_yf legt die Levels je Label hier ab (Nebeneffekt).
BENCHMARK_LEVELS = {}


def klassifiziere_index(label):
    w = BENCHMARK_LEVELS.get(label)
    if not w:
        return "N/A"
    kurs, e20, e50, w200 = w["Kurs"], w["EMA20"], w["EMA50"], w["WMA200"]
    if kurs < e50 or (not pd.isna(w200) and kurs < w200):
        return "Bärisch"
    if kurs < e20:
        return "Neutral"
    return "Bullisch"


def klassifiziere_marktumfeld(gewichtete_labels):
    """Regionen-Einstufung als Score-Modell (GEÄNDERT 28.07.2026, siehe
    Kommentarblock oben). Erwartet eine Liste von (Label, Gewicht)-Tupeln.
    Fehlt ein Index (keine Levels), wird über die verbleibenden Gewichte
    gemittelt; fehlen alle, kommt "N/A" zurück.
    Gibt (Regionen-Stufe, [Detail-Strings], Score-oder-None) zurück."""
    punkte = {"Bullisch": 2, "Neutral": 1, "Bärisch": 0}
    details, summe, gewichtssumme = [], 0.0, 0.0
    for label, gewicht in gewichtete_labels:
        stufe = klassifiziere_index(label)
        details.append(f"{label}: {stufe} (x{gewicht:g})")
        if stufe in punkte:
            summe += punkte[stufe] * gewicht
            gewichtssumme += gewicht
    if gewichtssumme == 0:
        return "N/A", details, None
    score = round(summe / gewichtssumme, 2)
    if score >= 1.5:
        regionen_stufe = "Bullisch"
    elif score <= 0.5:
        regionen_stufe = "Bärisch"
    else:
        regionen_stufe = "Neutral"
    return regionen_stufe, details, score


# Initialisierung des Clients direkt beim Start
# Wir nutzen os.getenv, um die Keys sicher aus deinen GitHub-Secrets zu lesen
alpaca_client = StockHistoricalDataClient(os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'))


# --- KONFIGURATION ---
# Initialisiere den Groq Client
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

sektoren_map = {
    "XLK": "Technologie", "XLF": "Finanzen", "XLV": "Gesundheit", "XLY": "Zyklischer Konsum",
    "XLP": "Basiskonsum", "XLE": "Energie", "XLI": "Industrie", "XLB": "Rohstoffe",
    "XLU": "Versorger", "XLRE": "Immobilien", "XLC": "Kommunikation",
    "SOXX": "Halbleiter", "SMH": "Halbleiter (Global)", "IGV": "Software", 
    "XBI": "Biotechnologie", "KRE": "Regionalbanken", "HACK": "Cybersecurity", 
    "CLOU": "Cloud Computing", "AIQ": "Künstliche Intelligenz",
    "BOTZ": "Robotik", "IHI": "Medical Devices", "PAVE": "Infrastruktur", "XRT": "Einzelhandel",
    "ITA": "Rüstung/Aerospace", "XME": "Minen/Metalle", "GDX": "Gold-Miner",
    "OIH": "Öl-Services", "TAN": "Solar/Clean Energy"
}

sektoren_aktien = {
    "XLK": [
        "AAPL", "ACN", "ADBE", "ADI", "ADSK", "AKAM", "ALAB", "AMAT", "AMD", "ANET", "ANSS", "APH",
        "ARM", "ASML", "AVGO", "BR", "CDNS", "CDW", "CIEN", "COHR", "CRM", "CRWD", "CRWV", "CSCO",
        "CTSH", "DDOG", "FFIV", "FLEX", "FSLR", "FTNT", "GDDY", "GEN", "GLW", "HPE", "HPQ", "IBM",
        "INTC", "INTU", "IT", "JBL", "JNPR", "KEYS", "KLAC", "LITE", "LRCX", "MCHP", "MDB", "MPWR",
        "MRVL", "MSFT", "MSI", "MSTR", "MU", "NBIS", "NOW", "NTAP", "NVDA", "NXPI", "ON", "ORCL",
        "PANW", "PLTR", "PTC", "Q", "QCOM", "QRVO", "ROP", "SHOP", "SMCI", "SNDK", "SNPS", "STX",
        "SWKS", "TDY", "TEL", "TER", "TRMB", "TXN", "TYL", "VRSN", "WDAY", "WDC", "ZBRA"
    ],
    "XLF": [
        "ACGL", "AFL", "AIG", "AIZ", "AJG", "ALL", "AMP", "AON", "APO", "ARES", "AXP", "BAC",
        "BEN", "BLK", "BNY", "BRK-B", "BRO", "BX", "C", "CB", "CBOE", "CFG", "CINF", "CME", "COF",
        "COIN", "CPAY", "DFS", "EFX", "EG", "ERIE", "FIS", "FISV", "FITB", "GL", "GPN", "GS",
        "HBAN", "HIG", "HOOD", "IBKR", "ICE", "IVZ", "JKHY", "JPM", "KEY", "KKR", "L", "MA", "MCO",
        "MET", "MRSH", "MS", "MSCI", "MTB", "NDAQ", "NTRS", "PFG", "PGR", "PNC", "PRU", "PYPL",
        "RF", "RJF", "SCHW", "SPGI", "STT", "SYF", "TFC", "TROW", "TRV", "USB", "V", "WFC", "WRB",
        "WTW", "XYZ"
    ],
    "XLV": [
        "A", "ABBV", "ABT", "ALGN", "ALNY", "AMGN", "BAX", "BDX", "BIIB", "BMY", "BSX", "CAH",
        "CI", "CNC", "COO", "COR", "CRL", "CVS", "DGX", "DHR", "DVA", "DXCM", "ELV", "EW", "GEHC",
        "GILD", "HCA", "HSIC", "HUM", "IDXX", "INCY", "IQV", "ISRG", "JNJ", "LH", "LLY", "MCK",
        "MDT", "MRK", "MRNA", "MTD", "PFE", "PODD", "REGN", "RMD", "RVTY", "SOLV", "STE", "SYK",
        "TECH", "TFX", "TMO", "UHS", "UNH", "VEEV", "VRTX", "VTRS", "WAT", "WST", "ZBH", "ZTS"
    ],
    "XLY": [
        "ABNB", "AMZN", "APTV", "AZO", "BBWI", "BBY", "BKNG", "BURL", "BWA", "CCL", "CMG", "CVNA",
        "DASH", "DECK", "DHI", "DPZ", "DRI", "EBAY", "EXPE", "F", "GM", "GPC", "GRMN", "HAS", "HD",
        "HLT", "LEN", "LKQ", "LOW", "LULU", "LVS", "MAR", "MCD", "MELI", "MGM", "MHK", "NCLH",
        "NKE", "NVR", "ORLY", "PDD", "PHM", "RCL", "RL", "ROST", "SBUX", "TJX", "TPR", "TSCO",
        "TSLA", "ULTA", "WYNN", "YUM"
    ],
    "XLP": [
        "ADM", "BF-B", "BG", "CASY", "CCEP", "CHD", "CL", "CLX", "COST", "DG", "DLTR", "EL", "GIS",
        "HRL", "HSY", "KDP", "KHC", "KMB", "KO", "KR", "KVUE", "MDLZ", "MKC", "MNST", "MO", "PEP",
        "PG", "PM", "SJM", "STZ", "SYY", "TAP", "TGT", "TSN", "WMT"
    ],
    "XLE": [
        "APA", "BKR", "COP", "CVX", "DVN", "EOG", "EQT", "FANG", "HAL", "HES", "KMI", "MPC", "MRO",
        "OKE", "OXY", "PSX", "SLB", "TRGP", "VLO", "WMB", "XOM"
    ],
    "XLI": [
        "ADP", "ALLE", "AME", "AOS", "AXON", "BA", "BLDR", "CARR", "CAT", "CMI", "CPRT", "CSX",
        "CTAS", "DAL", "DE", "DOV", "EME", "EMR", "ETN", "EXPD", "FAST", "FDX", "FDXF", "FER",
        "FERG", "FIX", "FTV", "GD", "GE", "GEV", "GNRC", "GWW", "HII", "HON", "HONA", "HUBB",
        "HWM", "IEX", "IR", "ITW", "J", "JBHT", "JCI", "LDOS", "LHX", "LMT", "LUV", "MAS", "MMM",
        "NDSN", "NOC", "NSC", "ODFL", "OTIS", "PAYX", "PCAR", "PH", "PNR", "PWR", "RKLB", "ROK",
        "ROL", "RSG", "RTX", "SNA", "SPCX", "SWK", "TDG", "TRI", "TT", "TXT", "UAL", "UBER", "UNP",
        "UPS", "URI", "VLTO", "VRSK", "VRT", "WAB", "WM", "XYL"
    ],
    "XLB": [
        "ALB", "AMCR", "APD", "AVY", "BALL", "CE", "CF", "CRH", "CTVA", "DD", "DOW", "ECL", "FCX",
        "FMC", "IFF", "IP", "LIN", "LYB", "MLM", "MOS", "NEM", "NUE", "PKG", "PPG", "SHW", "STLD",
        "VMC", "WRK"
    ],
    "XLU": [
        "AEE", "AEP", "AES", "ATO", "AWK", "CEG", "CMS", "CNP", "D", "DTE", "DUK", "ED", "EIX",
        "ES", "EVRG", "EXC", "FE", "LNT", "NEE", "NI", "NRG", "PCG", "PEG", "PNW", "PPL", "SO",
        "SRE", "VST", "WEC", "XEL"
    ],
    "XLRE": [
        "AMT", "ARE", "AVB", "CBRE", "CCI", "CPT", "CSGP", "DLR", "DOC", "EQIX", "EQR", "ESS",
        "EXR", "FRT", "HST", "INVH", "IRM", "KIM", "MAA", "O", "PLD", "PSA", "REG", "SBAC", "SPG",
        "UDR", "VICI", "VTR", "WELL", "WY"
    ],
    "XLC": [
        "APP", "CHTR", "CMCSA", "DIS", "EA", "ECHO", "FOX", "FOXA", "GOOG", "GOOGL", "LYV", "META",
        "NFLX", "NWS", "NWSA", "OMC", "PSKY", "T", "TMUS", "TTD", "TTWO", "VZ", "WBD"
    ],
    "AIQ": ["NVDA", "MSFT", "GOOGL", "META", "AAPL", "AMD", "TSM", "ORCL", "ADBE", "CRM",
            "PLTR", "SNOW", "NOW", "CRWD", "MRVL"],
    "BOTZ": ["NVDA", "ABB", "ISRG", "ROK", "TER", "ITW", "PTC", "FLIR", "TYL", "AMRC",
             "CGNX", "SYM"],
    "CLOU": ["SNOW", "CRWD", "OKTA", "ZS", "DDOG", "NET", "MDB", "TEAM", "DOCU",
             "TWLO", "HUBS", "BILL", "PATH", "FSLY", "ESTC"],
    "GDX": ["NEM", "GOLD", "AEM", "WPM", "FNV", "GFI", "KGC", "AU", "RGLD", "PAAS", "HMY", "EGO"],
    "HACK": ["PANW", "CRWD", "FTNT", "OKTA", "ZS", "CHKP", "QLYS", "TENB", "VRSN",
             "S", "NET", "RPD", "VRNS", "FFIV"],
    "IGV": ["MSFT", "ADBE", "CRM", "ORCL", "SNOW", "PANW", "WDAY", "INTU", "NOW", "ADSK",
            "CRWD", "ZS", "DDOG", "TEAM", "HUBS", "VEEV", "PTC", "BSY"],
    "IHI": ["ABT", "DHR", "MDT", "BSX", "SYK", "ZBH", "EW", "BAX", "RMD", "ALGN", "PODD", "DXCM", "GEHC"],
    "ITA": ["RTX", "LMT", "NOC", "GD", "BA", "LHX", "HWM", "TDG", "HEI", "AXON", "TXT", "HII"],
    "KRE": ["FITB", "HBAN", "CFG", "KEY", "ZION", "RF", "CMA", "SNV", "FLG", "WBS", "EWBC", "PNFP", "WAL"],
    "OIH": ["SLB", "HAL", "BKR", "FTI", "NOV", "WFRD", "RIG", "HP", "PTEN", "LBRT", "VAL"],
    "PAVE": ["DE", "CAT", "ETN", "JCI", "PH", "IR", "CMI", "XYL", "ITW", "EMR", "PWR", "MLM", "URI"],
    "SMH": ["NVDA", "TSM", "ASML", "AVGO", "QCOM", "TXN", "AMAT", "AMD", "LRCX", "MU",
            "KLAC", "MRVL", "MPWR", "ON", "MCHP"],
    "SOXX": ["NVDA", "AVGO", "TXN", "QCOM", "INTC", "AMD", "MU", "ADI", "LRCX", "AMAT",
             "KLAC", "MRVL", "MPWR", "SWKS", "ON", "MCHP", "TER", "ENTG"],
    "TAN": ["FSLR", "ENPH", "SEDG", "RUN", "NXT", "ARRY", "SHLS", "CSIQ", "JKS", "DQ", "MAXN", "FLNC"],
    "XBI": ["AMGN", "GILD", "BIIB", "VRTX", "REGN", "ILMN", "TECH", "MRNA", "IBB",
            "INCY", "EXEL", "NBIX", "BMRN", "UTHR"],
    "XME": ["FCX", "NUE", "STLD", "CLF", "AA", "X", "RS", "CMC", "ATI", "MP", "HL", "CRS"],
    "XRT": ["AMZN", "HD", "LOW", "TGT", "COST", "WMT", "BBY", "TJX", "ROST", "ULTA", "DKS", "BURL", "FIVE"],
}

# --- STOXX EUROPE 600 / DAX (Xetra, via yfinance) ---
# Sektor-Rotation läuft über STOXX-Europe-600-Sektor-ETFs (breiterer Referenzrahmen),
# die Kandidaten stammen aus DAX40, MDAX und Eurozonen-Large-Caps (nur EUR-Börsen).
# GEAENDERT 09.08.2026 (Nutzerwunsch, nach Fund im echten Performance_EU.csv-Log): von
# 7 auf alle 13 dax_aktien-Kategorien erweitert, komplett per Websuche gegengeprueft
# (mehrfach ueber unabhaengige Quellen bestaetigt: Yahoo Finance, TradingView, Bloomberg,
# justETF, iShares-Produktseiten). DABEI ECHTEN BUG GEFUNDEN UND BEHOBEN: EXV6.DE stand
# bisher fuer "Industrie", trackt aber tatsaechlich "Basic Resources" - der korrekte
# Industrie-ETF (Industrial Goods & Services) ist EXH4.DE. EXV6.DE passt stattdessen
# zur neuen Kategorie "Grundstoffe". War vermutlich seit Anlage des Blocks falsch (der
# alte Kommentar warnte selbst, dass nur 3 von 7 Tickern verifiziert wurden - EXV6 war
# eine der unverifizierten). "Kommunikation" (EXV2, Telecommunications) und "Konsum"
# (EXH7, Personal & Household Goods) sind KEINE perfekten 1:1-Entsprechungen, da beide
# dax_aktien-Buckets breiter gemischt sind (Konsum z.B. auch E-Commerce/Retail) - nächstliegende
# Einzel-ETF-Naeherung, kein Kompositindex. Falls ein Ticker falsch ist, liefert
# get_perf_yf() einfach eine Performance von 0 fuer diesen Sektor (kein Absturz).
eu_sektoren_etf = {
    "EXV1.DE": "Finanzen",  # Banks (Name 09.08.2026 korrigiert, siehe unten)
    "EXH5.DE": "Versicherungen",  # Insurance
    "EXV3.DE": "Technologie",  # Technology
    "EXV4.DE": "Gesundheitswesen",  # Health Care (Name 09.08.2026 korrigiert)
    "EXH4.DE": "Industrie",  # Industrial Goods & Services - NEU 09.08.2026, ersetzt fehlerhaftes EXV6.DE
    "EXH9.DE": "Versorger",  # Utilities
    "EXV5.DE": "Automobil",  # Automobiles & Parts
    "EXV6.DE": "Grundstoffe",  # Basic Resources - NEU 09.08.2026 (war faelschlich "Industrie")
    "EXV7.DE": "Chemie",  # Chemicals - NEU 09.08.2026
    "EXH1.DE": "Energie",  # Oil & Gas - NEU 09.08.2026
    "EXV2.DE": "Kommunikation",  # Telecommunications - NEU 09.08.2026
    "EXI5.DE": "Immobilien",  # Real Estate - NEU 09.08.2026
    "EXH7.DE": "Konsum",  # Personal & Household Goods - NEU 09.08.2026
}
# Alle 13 dax_aktien-Kategorien haben jetzt eine Rotation-ETF (vorher nur 7) - die
# Einschraenkung von weiter oben ("6 Kategorien ohne ETF") ist damit behoben.
# (als "Chemie/Rohstoffe", "Konsum/Handel", "Immobilien", "Telekommunikation" ohne ETF) -

eu_benchmark_ticker = "EXSA.DE"  # iShares STOXX Europe 600 UCITS ETF (DE) - EU-Referenzindex für RS

# EU-Ticker nach Sektor (Stand: Juli 2026;
# die Zusammensetzung wird von der Deutschen Börse zweimal jährlich überprüft, daher
# gelegentlich gegenchecken)
dax_aktien = {
    "Industrie": [
        "10O.DE", "1TRA.DE", "AIR.DE", "ASTA.DE", "AUM.DE", "BFSA.DE", "BMW.DE", "BNR.DE",
        "CON.DE", "DEZ.DE", "DHL.DE", "DTG.DE", "DUE.DE", "FRAG.DE", "G1A.DE", "GBF.DE", "HAG.DE",
        "HDD.DE", "HEI.DE", "HOT.DE", "JST.DE", "JUN3.DE", "KBX.DE", "KGX.DE", "KRN.DE", "KSB3.DE",
        "LHA.DE", "MBB.DE", "MBG.DE", "MTX.DE", "NDX1.DE", "NOEJ.DE", "P911.DE", "R3NK.DE",
        "RAA.DE", "RHM.DE", "SFQ.DE", "SHA.DE", "SIE.DE", "STM1.DE", "STO3.DE", "TKA.DE", "VH2.DE",
        "VOS.DE", "VOW3.DE", "WAC.DE"
    ],
    "Automobil": [
        "10O.DE", "1TRA.DE", "AUM.DE", "BMW.DE", "CON.DE", "DTG.DE", "JST.DE", "KBX.DE", "MBG.DE",
        "PAH3.DE", "RHM.DE", "SFQ.DE", "SHA.DE", "STM1.DE", "VOW3.DE", "P911.DE"
    ],
    "Chemie": [
        "ACT.DE", "BAS.DE", "EVK.DE", "FPE3.DE", "LXS.DE", "SY1.DE", "WCH.DE"
    ],
    "Energie": [
        "VBK.DE"
    ],
    "Finanzen": [
        "ALV.DE", "CBK.DE", "DB1.DE", "DBAN.DE", "DBK.DE", "DWS.DE", "FTK.DE", "GLJ.DE", "HNR1.DE",
        "HYQ.DE", "INH.DE", "MLP.DE", "MUV2.DE", "MUX.DE", "PAH3.DE", "PBB.DE", "TLX.DE", "WUW.DE"
    ],
    "Gesundheitswesen": [
        "1SXP.DE", "AFX.DE", "BAYN.DE", "DMP.DE", "DRW3.DE", "EUZ.DE", "EVT.DE", "FME.DE",
        "FRE.DE", "GXI.DE", "ILM1.DE", "MRK.DE", "QIA.DE", "RDC.DE", "SHL.DE", "SRT3.DE"
    ],
    "Grundstoffe": [
        "KCOG.DE", "NAFG.DE", "SDF.DE", "SZG.DE"
    ],
    "Immobilien": [
        "ARND.DE", "DEQ.DE", "DWNI.DE", "GYC.DE", "HABA.DE", "LEG.DE", "PAT.DE", "TEG.DE",
        "VNA.DE"
    ],
    "Kommunikation": [
        "1U1.DE", "DTE.DE", "FNTN.DE", "PSM.DE", "RTL.DE", "SAX.DE", "SPG.DE", "UTDI.DE"
    ],
    "Konsum": [
        "ADS.DE", "AG1.DE", "BEI.DE", "BOSS.DE", "BVB.DE", "CWC.DE", "DHER.DE", "DOU.DE", "EVD.DE",
        "HBH.DE", "HEN3.DE", "HFG.DE", "KWS.DE", "PUM.DE", "SIX2.DE", "SZU.DE", "TNIE.DE",
        "TUI1.DE", "ZAL.DE"
    ],
    "Technologie": [
        "ADN1.DE", "ADV.DE", "AIXA.DE", "AOF.DE", "BC8.DE", "COK.DE", "ELG.DE", "F3C.DE", "G24.DE",
        "GFT.DE", "IFX.DE", "IOS.DE", "JEN.DE", "KTN.DE", "NA9.DE", "NEM.DE", "PSAG.DE", "S92.DE",
        "SAP.DE", "SHELL.SO", "SMHN.DE", "TMV.DE", "TPE.DE", "VRV.DE", "WAF.DE", "YSN.DE"
    ],
    "Versicherungen": [
        "ALV.DE", "HNR1.DE", "MUV2.DE", "TLX.DE", "WUW.DE"
    ],
    "Versorger": [
        "EKT.DE", "ENR.DE", "EOAN.DE", "PNE.DE", "RWE.DE"
    ],
}

def berechne_indikatoren(df):
    # 1. MultiIndex entfernen (wichtig für yfinance-Struktur)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 2. Prüfen, ob 'Close' existiert (Sicherheitsprüfung)
    if 'Close' not in df.columns:
        return df 

    # 3. RSI berechnen (ersetzt die alten, fehlerhaften Zeilen 74-93)
    # get_safe_rsi kümmert sich intern um die Prüfung der Länge und Division durch Null
    df['RSI'] = get_safe_rsi(df)
    
    return df
    
def _hole_firma_name(ticker, retries=2):
    """NEU (09.08.2026, Nutzerfund: seit dem Universum-Neubau vom selben Tag
    zeigte die Auswertung bei manchen sonst unproblematischen Titeln (z.B.
    FNV, CDW, TWLO) nur den Ticker statt des Firmennamens). URSACHE: der
    Namens-Lookup lief bisher OHNE Retry und OHNE Buendelung - ein eigener
    yf.Ticker(ticker).info-Request PRO Ticker im Tagesuniversum, direkt am
    Funktionsanfang von analyze_a_setup/_eu, unabhaengig davon ob der Titel
    spaeter ueberhaupt angezeigt wird. Mit dem gewachsenen Universum (605 US
    + 159 EU statt vorher 334) steigt die Zahl dieser Einzel-Requests
    entsprechend, und yfinance drosselt .info informell - ein einzelner
    fehlgeschlagener Call fiel bisher sofort auf den Ticker als Namen zurueck.
    Gleiche, bewusst zurueckhaltende Retry-Konvention wie get_analyst_target
    (2 Versuche, 1s Pause - mehr Versuche wuerden sich bei hunderten Tickern
    spuerbar summieren, siehe dortige Begruendung). Batching wie bei den
    Kursdaten (fetch_us_batch/fetch_eu_batch) ist hier NICHT moeglich -
    yfinance bietet fuer .info keinen Sammel-Endpunkt, nur fuer .history()."""
    for i in range(retries):
        try:
            info = yf.Ticker(ticker).info
            # GEAENDERT (30.07.2026): longName ODER shortName, erst dann der
            # Ticker als Notnagel. Vorher fiel die Auswertung bei fehlendem
            # longName direkt auf den Ticker zurueck - im Lauf vom 30.07.
            # standen deshalb "APD", "CL", "SIE.DE" und "CON.DE" statt der
            # Firmennamen in der Watchlist. shortName ist bei yfinance
            # deutlich zuverlaessiger verfuegbar als longName.
            firma_name = info.get('longName') or info.get('shortName') or ticker
            firma_name = re.sub(r'\s+', ' ', str(firma_name)).strip()
            # abgeschnittenes Rest-Fragment am Ende entfernen (yfinance
            # kuerzt shortName hart, z.B. "VOLKSWAGEN AG                 V")
            firma_name = re.sub(r'\s+[A-Za-z]$', '', firma_name).strip(' ,;-')
            return firma_name if firma_name else ticker
        except Exception as e:
            if i < retries - 1:
                print(f"DEBUG: Namens-Lookup für {ticker} fehlgeschlagen (Versuch {i+1}/{retries}): {e}. Warte 1s...")
                time.sleep(1)
    print(f"DEBUG: Namens-Lookup für {ticker} nach {retries} Versuchen endgueltig fehlgeschlagen - nutze Ticker als Name.")
    return ticker


def get_analyst_target(ticker, retries=2):
    """Holt Analysten-Daten mit Retry-Logik.
    GEAENDERT (05.08.2026, externe Code-Review): retries 3->2, Wartezeit
    2s->1s. Grund: stock.info-Instabilitaet ist meist ein strukturelles
    Problem (fehlendes Feld beim jeweiligen Ticker), kein kurzer Netzwerk-
    Ruckler, den ein Retry-mit-Pause zuverlaessig behebt - der ausfuehr-
    lichere Retry kostete bei ausbleibenden Daten bis zu 6s PRO TICKER,
    was sich bei potenziell hunderten Titeln im Tagesuniversum spuerbar
    summiert (siehe auch die Beobachtung zu vielen Netzwerkaufrufen/
    Yahoo-Rate-Limits weiter oben) - kuerzere Retries begrenzen den
    Schaden, ohne die Erfolgsquote bei echten kurzen Rucklern zu verlieren."""
    for i in range(retries):
        try:
            stock = yf.Ticker(ticker)
            data = stock.info
            target = data.get('targetMeanPrice')
            
            if target and target > 0:
                return target
            return None
            
        except Exception as e:
            print(f"Versuch {i+1} für {ticker} fehlgeschlagen: {e}. Warte 1s...")
            time.sleep(1)
    return None

def get_safe_rsi(df, period=14):
    # GEAENDERT (05.08.2026, externe Code-Review, Punkt 4): df.diff()
    # verliert die erste Zeile (NaN), ewm(min_periods=period) braucht
    # also period+1 Rohwerte, um wirklich period gueltige Delta-Werte zu
    # bekommen - vorher liess die Pruefung `len(df) < period` einen Fall
    # mit genau `period` Zeilen durch, der aber intern schon in den
    # NaN->50.0-Fallback gelaufen waere. Mathematisch konsistenter jetzt.
    """Berechnet RSI und gibt immer eine saubere Series zurück.
    GEAENDERT (05.08.2026, externe Code-Review): Wilder's Glaettung
    (exponentiell, alpha=1/period) statt eines einfachen gleitenden
    Durchschnitts - das ist der Branchenstandard (TradingView, TA-Lib) und
    macht den angezeigten RSI-Wert direkt mit anderen Plattformen
    vergleichbar. Vorher wich der eigene RSI-Wert sichtbar von TradingView
    & Co. ab, obwohl beide "RSI(14)" hiessen. `adjust=False` ist Pflicht -
    sonst gewichtet pandas die juengsten Punkte staerker als Wilder's
    Definition es vorsieht."""
    if 'Close' not in df.columns or len(df) < period + 1:
        return pd.Series([50.0] * len(df), index=df.index)
    
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    
    # Division durch Null verhindern
    # GEAENDERT (05.08.2026, externe Code-Review, Punkt 2): clip() statt
    # replace(0, ...) - faengt auch extrem kleine, aber nicht exakt-null
    # Verlustwerte ab (replace(0, ...) liesse z.B. 1e-15 unveraendert
    # durch und wuerde einen unnoetig riesigen RS-Wert erzeugen).
    rs = gain / loss.clip(lower=1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


# --- FUNKTIONEN ---
def update_status_logic(row):
    """Bestimmt Status2 (VALIDE/ACHTUNG/GELAUFEN) und die Begruendung fuer
    ein bereits gefundenes Setup (df_s hat den Haupt-Pattern-Filter schon
    durchlaufen) - via df_s.apply(update_status_logic, axis=1).

    KRITISCHER BUGFIX (06.08.2026): Diese Funktion wurde am 05.08.2026
    faelschlich als "toter Code" identifiziert und geloescht - eigener
    Fehler: die damalige projektweite Suche nach "update_status_logic("
    (mit Klammer) fand die tatsaechliche Aufrufstelle nicht, weil sie die
    Funktion OHNE Klammer als Referenz an df_s.apply() uebergibt, statt sie
    direkt aufzurufen. main.yml stuerzte seither bei jedem Lauf mit
    mindestens einem gefundenen Setup mit NameError ab. Hier wiederhergestellt
    UND gleich mit der urspruenglich (05.08., externe Code-Review) korrekt
    beanstandeten Prioritaeten-Reihenfolge:
    ALT: RSI < 30 gab sofort "VALIDE" zurueck und ueberschrieb damit
    nachfolgende MACD-/Volumen-/TP1-Pruefungen - ein ueberverkaufter Titel
    mit gleichzeitig baerischem MACD wurde faelschlich trotzdem VALIDE.
    NEU: TP1-Erreichen hat oberste Prioritaet (Trade ist dann bereits
    gelaufen, unabhaengig vom RSI), danach die ACHTUNG-Kriterien (RSI>70,
    schwaches Volumen, baerischer MACD) - erst danach der Oversold-Hinweis,
    der jetzt keine ACHTUNG-Einstufung mehr ueberschreiben kann, sondern nur
    noch greift, wenn keines der Warnkriterien zutrifft."""
    rsi = row.get('RSI', 50)  # Standard 50, falls was schiefgeht
    pattern = row.get('Pattern', "Kein")
    vol_ratio = row.get('Vol_Ratio', 1.0)  # Standard 1.0, damit kein Fehler bei < 0.5
    macd_trend = row.get('MACD_Trend', "Neutral")
    kurs = row.get('Kurs', 0)
    tp1 = row.get('TP1', float('inf'))  # Unendlich, falls kein TP1 existiert
    divergenz = row.get('Divergenz', "Keine")

    if kurs >= tp1:
        return pd.Series(["GELAUFEN", "Kursziel erreicht"])
    elif rsi > 70:
        return pd.Series(["ACHTUNG", "RSI überkauft (>70)"])
    elif pattern != "Kein" and vol_ratio < 0.5:
        return pd.Series(["ACHTUNG", f"Schwaches Volumen ({round(float(vol_ratio), 2)}x SMA20)"])
    elif macd_trend == "Bärisch":
        return pd.Series(["ACHTUNG", "Bärischer MACD-Trend"])
    elif rsi < 30:
        return pd.Series(["VALIDE", "RSI überverkauft (<30) - zusätzliches Kaufsignal"])
    elif divergenz == "Bullisch":
        return pd.Series(["VALIDE", "Bullische Divergenz (Signal)"])
    else:
        return pd.Series(["VALIDE", "Alles ok"])


def get_earnings_warnung(ticker, warn_tage=7):
    """Prüft per yfinance, ob der nächste Earnings-Termin innerhalb der
    nächsten warn_tage liegt. Gibt einen Warntext zurück (z.B.
    '⚠ Earnings in 3 Tagen (21.07.2026)') oder None. Earnings-Gaps sind
    das größte Über-Nacht-Risiko für Swing-Positionen - ein Stop schützt
    nicht vor einem Gap unter den Stop-Kurs. Defensiv: jeder Fehler
    (kein Termin verfügbar, API-Aussetzer) führt still zu None."""
    try:
        kalender = yf.Ticker(ticker).calendar
        termine = None
        if isinstance(kalender, dict):
            termine = kalender.get('Earnings Date')
        if not termine:
            print(f"DEBUG-EARNINGS: {ticker} -> kein Termin im Kalender hinterlegt")
            return None
        naechster = termine[0] if isinstance(termine, (list, tuple)) else termine
        heute = datetime.date.today()
        if hasattr(naechster, 'date'):
            naechster = naechster.date()
        delta = (naechster - heute).days
        if 0 <= delta <= warn_tage:
            tage_text = "HEUTE" if delta == 0 else f"in {delta} Tag{'en' if delta != 1 else ''}"
            print(f"DEBUG-EARNINGS: {ticker} -> Termin {naechster.strftime('%d.%m.%Y')} (in {delta} Tagen) -> WARNUNG")
            return f"⚠ Earnings {tage_text} ({naechster.strftime('%d.%m.%Y')})"
        print(f"DEBUG-EARNINGS: {ticker} -> nächster Termin {naechster.strftime('%d.%m.%Y')} (in {delta} Tagen, außerhalb Warnfenster)")
        return None
    except Exception as e:
        print(f"DEBUG-EARNINGS: {ticker} -> kein Termin ermittelbar ({type(e).__name__}: {e})")
        return None


def get_earnings_rueckblick(ticker, rueckblick_tage=5):
    """Earnings-RÜCKBLICK (NEU 29.07.2026, Nutzerwunsch - Gegenstück zur
    Warnung oben): Hat ein Titel in den letzten `rueckblick_tage` Kalender-
    tagen berichtet, gibt diese Funktion eine kurze Einordnung zurück, z.B.
    '📊 Zahlen 28.07.: Erwartungen übertroffen (EPS +6,2%), Markt bestätigt
    (+2,4% am Berichtstag)'. Sonst None.

    ZWEI DATENQUELLEN, bewusst kombiniert:
      1) EPS-Überraschung (yfinance earnings_dates: 'Reported EPS' vs.
         'EPS Estimate') - die harte Zahl gegen die Analystenerwartung.
      2) Kursreaktion am Berichtstag (Schlusskurs vs. Vortagesschluss) -
         das Urteil des Marktes.
    Grund für die Kombination: die reine EPS-Zahl erklärt Kursreaktionen oft
    NICHT (Ausblick/Guidance, Margen, Sondereffekte bewegen den Kurs, stehen
    aber in keiner maschinell verfügbaren Kennzahl). Laufen beide Signale
    auseinander - Zahlen über Erwartung, Kurs fällt trotzdem - ist genau das
    die vom Nutzer gewünschte Kategorie "geteilte Meinung": der Markt hat
    etwas anderes gesehen als die nackte Gewinnzahl.

    Einstufung (Schwellen bewusst großzügig, um Rauschen auszublenden):
      EPS-Abweichung  > +2%  = übertroffen | < -2% = verfehlt | sonst = im Rahmen
      Kursreaktion    > +1,5% = positiv    | < -1,5% = negativ | sonst = neutral
      Gegenläufig (übertroffen+negativ oder verfehlt+positiv) -> Vorlauf entscheidet:

    VORLAUF-UNTERSCHEIDUNG (NEU 29.07.2026, Nutzer-Einwand): Ein Kursrutsch
    trotz guter Zahlen hat ZWEI verschiedene Ursachen, die nicht dasselbe
    bedeuten:
      (a) "Sell on good news"/Gewinnmitnahme - der Kurs ist VOR dem Bericht
          stark gelaufen, die guten Zahlen waren eingepreist, Anleger nehmen
          Gewinne mit. Kein Urteil gegen das Unternehmen.
      (b) Echte geteilte Meinung - kein auffälliger Vorlauf, der Markt sieht
          also etwas Konkretes negativ (typisch: Ausblick/Guidance, Margen).
    Unterschieden wird über die Kursentwicklung der 20 Handelstage VOR dem
    Bericht: >= +8% = Vorlauf (a), sonst (b). Gespiegelt bei schlechten
    Zahlen mit steigendem Kurs: <= -8% Vorlauf heißt "war bereits
    eingepreist" statt geteilter Meinung.
    Wichtig: Das ist eine MUSTER-Einordnung, kein Beweis - beide Ursachen
    können zusammen auftreten. Die Formulierung bleibt deshalb bewusst
    beschreibend ("Muster ... "), nicht behauptend.

    GRENZEN (ehrlich benannt, gehören in die Bewertung): nur EPS, KEIN Umsatz
    und KEINE Guidance - für beides liefert yfinance keine verlässlichen
    Quartals-Erwartungswerte. Die Kursreaktion fängt diese Lücke indirekt auf,
    ersetzt aber keine echte Bericht-Lektüre. Defensiv: jeder Fehler oder
    fehlende Wert führt still zu None bzw. zum Weglassen des jeweiligen Teils.
    """
    try:
        heute = datetime.date.today()
        eps_df = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if eps_df is None or eps_df.empty:
            print(f"DEBUG-EARNINGS-RUECKBLICK: {ticker} -> keine Earnings-Historie verfügbar")
            return None

        # Jüngsten BEREITS BERICHTETEN Termin im Rückblick-Fenster suchen
        letzter_termin, eps_abweichung = None, None
        for zeitpunkt, zeile in eps_df.iterrows():
            termin = zeitpunkt.date() if hasattr(zeitpunkt, 'date') else zeitpunkt
            delta = (heute - termin).days
            if not (0 <= delta <= rueckblick_tage):
                continue
            gemeldet = zeile.get('Reported EPS')
            erwartet = zeile.get('EPS Estimate')
            if pd.isna(gemeldet):
                continue  # Termin liegt zwar zurück, Zahlen aber noch nicht erfasst
            if letzter_termin is None or termin > letzter_termin:
                letzter_termin = termin
                if pd.notna(erwartet) and abs(float(erwartet)) > 0.001:
                    eps_abweichung = (float(gemeldet) - float(erwartet)) / abs(float(erwartet)) * 100

        if letzter_termin is None:
            return None

        # EPS-Einstufung
        if eps_abweichung is None:
            eps_stufe, eps_text = "unbekannt", "EPS-Erwartung nicht verfügbar"
        elif eps_abweichung > 2:
            eps_stufe = "uebertroffen"
            eps_text = f"Erwartungen übertroffen (EPS {eps_abweichung:+.1f}%)"
        elif eps_abweichung < -2:
            eps_stufe = "verfehlt"
            eps_text = f"Erwartungen verfehlt (EPS {eps_abweichung:+.1f}%)"
        else:
            eps_stufe = "im_rahmen"
            eps_text = f"Erwartungen getroffen (EPS {eps_abweichung:+.1f}%)"

        # Kursreaktion am Berichtstag (Schlusskurs vs. Vortagesschluss)
        reaktion, reaktion_text, vorlauf = None, "", None
        try:
            # 3 Monate statt 1: die Kursreaktion braucht nur den Vortag, der
            # Vorlauf aber 20 Handelstage VOR dem Bericht (siehe Docstring).
            hist = yf.Ticker(ticker).history(period="3mo")
            if not hist.empty:
                hist.index = [i.date() if hasattr(i, 'date') else i for i in hist.index]
                tage = [d for d in hist.index if d >= letzter_termin]
                if tage:
                    bericht_tag = min(tage)
                    pos = list(hist.index).index(bericht_tag)
                    if pos > 0:
                        vortag_kurs = float(hist['Close'].iloc[pos - 1])
                        bericht_kurs = float(hist['Close'].iloc[pos])
                        if vortag_kurs > 0:
                            reaktion = (bericht_kurs / vortag_kurs - 1) * 100
                            reaktion_text = f"{reaktion:+.1f}% am Berichtstag"
                        # Vorlauf: 20 Handelstage vor dem Bericht bis Vortag
                        if pos >= 21:
                            start_kurs = float(hist['Close'].iloc[pos - 21])
                            if start_kurs > 0:
                                vorlauf = (vortag_kurs / start_kurs - 1) * 100
        except Exception:
            pass  # Kursreaktion ist optionaler Zusatz, nie ein Abbruchgrund

        # Gesamturteil: Zahlen und Marktreaktion zusammenführen
        if reaktion is None:
            urteil = eps_text
        else:
            markt_positiv = reaktion > 1.5
            markt_negativ = reaktion < -1.5
            if eps_stufe == "uebertroffen" and markt_negativ:
                if vorlauf is not None and vorlauf >= 8:
                    urteil = (f"Erwartungen übertroffen (EPS {eps_abweichung:+.1f}%), Kurs fällt "
                              f"dennoch ({reaktion_text}) - Muster Gewinnmitnahme/'Sell on good news' "
                              f"nach starkem Vorlauf ({vorlauf:+.1f}% in den 20 Handelstagen davor)")
                else:
                    vorlauf_zusatz = (f", kein auffälliger Vorlauf ({vorlauf:+.1f}%)"
                                      if vorlauf is not None else "")
                    urteil = (f"geteilte Meinung - Zahlen über Erwartung "
                              f"(EPS {eps_abweichung:+.1f}%), Markt reagiert negativ "
                              f"({reaktion_text}){vorlauf_zusatz} - Grund eher im Ausblick als in den Zahlen")
            elif eps_stufe == "verfehlt" and markt_positiv:
                if vorlauf is not None and vorlauf <= -8:
                    urteil = (f"Erwartungen verfehlt (EPS {eps_abweichung:+.1f}%), Kurs steigt dennoch "
                              f"({reaktion_text}) - Muster 'war bereits eingepreist' nach schwachem "
                              f"Vorlauf ({vorlauf:+.1f}% in den 20 Handelstagen davor)")
                else:
                    urteil = (f"geteilte Meinung - Zahlen unter Erwartung "
                              f"(EPS {eps_abweichung:+.1f}%), Markt reagiert dennoch positiv ({reaktion_text})")
            elif eps_stufe == "unbekannt":
                urteil = f"Marktreaktion {reaktion_text} (EPS-Erwartung nicht verfügbar)"
            else:
                markt_wort = ("Markt bestätigt" if (markt_positiv and eps_stufe != "verfehlt")
                              or (markt_negativ and eps_stufe == "verfehlt")
                              else "Markt reagiert verhalten")
                urteil = f"{eps_text}, {markt_wort} ({reaktion_text})"

        print(f"DEBUG-EARNINGS-RUECKBLICK: {ticker} -> {letzter_termin.strftime('%d.%m.%Y')}: {urteil}")
        return f"📊 Zahlen {letzter_termin.strftime('%d.%m.')}: {urteil}"
    except Exception as e:
        print(f"DEBUG-EARNINGS-RUECKBLICK: {ticker} -> nicht ermittelbar ({type(e).__name__}: {e})")
        return None


_news_client = None

def get_news_headlines(ticker, max_n=3):
    """Holt die jüngsten Schlagzeilen zu einem US-Ticker über die Alpaca-
    News-API (Benzinga-Feed, in den bestehenden Keys enthalten). Gibt eine
    Liste 'TT.MM.: Titel' zurück (max. max_n). Nur für suffixlose US-Ticker -
    für EU-Titel liefert der Feed nichts, dann leere Liste. Defensiv: jeder
    Fehler führt still zu leerer Liste, News sind reiner Zusatz-Kontext."""
    global _news_client
    if not NEWS_VERFUEGBAR:
        print(f"DEBUG-NEWS: {ticker} -> übersprungen (News-API in alpaca-py nicht verfügbar)")
        return []
    if '.' in str(ticker):
        print(f"DEBUG-NEWS: {ticker} -> übersprungen (EU-Ticker, kein US-News-Feed)")
        return []
    try:
        if _news_client is None:
            _news_client = NewsClient(os.getenv('ALPACA_KEY'), os.getenv('ALPACA_SECRET'))
        req = NewsRequest(symbols=str(ticker), limit=max_n)
        antwort = _news_client.get_news(req)
        # Versionssichere Extraktion: je nach alpaca-py-Version liegen die
        # Artikel unter antwort.news ODER unter antwort.data['news'] (NewsSet)
        artikel_liste = getattr(antwort, 'news', None)
        if artikel_liste is None:
            daten = getattr(antwort, 'data', None)
            artikel_liste = daten.get('news', []) if isinstance(daten, dict) else []
        headlines = []
        for artikel in list(artikel_liste)[:max_n]:
            datum = artikel.created_at.strftime('%d.%m.') if getattr(artikel, 'created_at', None) else ''
            titel = getattr(artikel, 'headline', '') or ''
            if titel:
                headlines.append(f"{datum}: {titel}")
        print(f"DEBUG-NEWS: {ticker} -> {len(headlines)} Schlagzeile(n) gefunden")
        return headlines
    except Exception as e:
        print(f"DEBUG: News für {ticker} nicht abrufbar ({e})")
        return []


def get_eurusd_wechselkurs():
    """NEU (28.07.2026, Nutzerwunsch): reiner EUR/USD-Wechselkurs, ergaenzend
    zum US-Dollar-Index (DXY) - der DXY ist ein Waehrungskorb gegen 6 grosse
    Waehrungen (u.a. EUR, JPY, GBP), keine reine EUR/USD-Groesse, und damit
    nicht direkt geeignet, um das Waehrungsrisiko im Portfolio (EUR-/USD-
    Positionen gemischt) einzuschaetzen. Eigene, einfache Funktion statt
    Wiederverwendung von get_index_benchmark_yf: ein Wechselkurs bewegt sich
    typischerweise nur zwischen 0,85 und 1,15 - die dortige EMA-Rundung auf
    ganze Zahlen (.0f) waere hier voellig unbrauchbar, deshalb 4 Nachkomma-
    stellen und kein EMA-Trendkontext (fuer einen Wechselkurs im Rahmen
    dieses Kontext-Blocks nicht der entscheidende Punkt - der aktuelle Kurs
    selbst ist die relevante Information). Reiner Kontext-Indikator, KEINE
    Setup-Quelle, KEINE Abwertungsgrundlage."""
    try:
        hist = yf.Ticker("EURUSD=X").history(period="5d")
        if hist.empty:
            return "EUR/USD-Wechselkurs: Daten unvollständig"
        kurs = hist["Close"].iloc[-1]
        return f"EUR/USD-Wechselkurs: {kurs:.4f}"
    except Exception as e:
        print(f"DEBUG: EUR/USD-Wechselkurs nicht verfügbar ({e}).")
        return "EUR/USD-Wechselkurs: Daten unvollständig"


# FOMC-Termine (NEU 06.08.2026, externe Code-Review, Punkt 3: Termine liegen
# jetzt in fomc_termine.json statt hartkodiert im Python-Code - die jaehrliche
# Wartung ist damit ein reiner Daten-Edit, kein Code-Change mehr. Auf
# Modulebene, weil sie seit 30.07.2026 von ZWEI Funktionen gebraucht wird:
# dem Countdown (naechster Termin) und dem Rueckblick (letzter Termin).
# DEFENSIV geladen: dieses Modul wird von trendwende_scanner.py,
# short_scanner.py und edelmetalle_scanner.py per `from analyse import (...)`
# importiert - ein Fehler beim Laden darf NICHT den kompletten Import (und
# damit alle vier Scanner) zum Absturz bringen. Bei fehlender/kaputter Datei:
# leere Liste + einmalige Warnung, get_fomc_countdown()/get_fomc_rueckblick()
# behandeln eine leere Liste bereits als "kein Termin hinterlegt" bzw.
# "keine Sitzung im Fenster" - kein Absturz, nur ein fehlender Hinweis.
def _lade_fomc_termine(dateiname="fomc_termine.json"):
    try:
        with open(dateiname, "r", encoding="utf-8") as f:
            daten = json.load(f)
        termine = [datetime.date.fromisoformat(t) for t in daten["termine"]]
        if not termine:
            print(f"WARNUNG: {dateiname} enthaelt keine Termine.")
        return termine
    except Exception as e:
        print(f"WARNUNG: FOMC-Termine nicht ladbar ({dateiname}, {type(e).__name__}: {e}) - "
              f"FOMC-Countdown/-Rueckblick liefern bis zur Behebung 'kein Termin hinterlegt'.")
        return []


FOMC_TERMINE_2026 = _lade_fomc_termine()


def berechne_erfolgsbilanz(df_positionen):
    """ERFOLGSBILANZ (NEU 30.07.2026, Nutzerwunsch): Kennzahl ueber ALLE
    jemals geschlossenen Positionen (Status Gestoppt oder Verkauft) - im
    Unterschied zur Portfolio-Uebersicht, die nur den aktuellen, OFFENEN
    Bestand zeigt. Da positionen_tracker.py geschlossene Zeilen nie loescht
    (nur die Anzeige im Abschnitt 'Geschlossene Positionen' ist auf ein
    rollierendes 10-Werktage-Fenster begrenzt), steht die komplette
    Historie weiterhin in Offene_Positionen.csv - diese Funktion wertet sie
    unabhaengig vom Anzeige-Fenster vollstaendig aus.

    WICHTIGER BUGFIX EN PASSANT: Performance_Seit_Einstieg% wird von
    positionen_tracker.py nur fuer Status 'Offen' aktualisiert (die Schleife
    dort ueberspringt jede Zeile, deren Status nicht 'offen' ist). Bei
    'Gestoppt' ist der Wert korrekt eingefroren (er wurde im selben Moment
    gesetzt, in dem Ausstiegskurs = aktueller_kurs war). Bei 'Verkauft'
    dagegen traegt der Nutzer Ausstiegskurs von Hand ein, OHNE dass
    Performance_Seit_Einstieg% dabei neu berechnet wird - die Spalte kann
    also einen veralteten Stand von der letzten 'Offen'-Aktualisierung
    zeigen, der nicht zum tatsaechlichen Verkaufskurs passt. Deshalb wird
    die Performance hier fuer ALLE geschlossenen Zeilen frisch aus
    Einstieg/Ausstiegskurs/Richtung berechnet statt der Spalte zu vertrauen.

    Gibt einen fertigen Text zurueck (oder einen Hinweis, falls noch keine
    Position geschlossen wurde) - wie bei der Portfolio-Uebersicht in
    Python vorberechnet, damit Gemini nicht selbst ueber viele Zeilen
    mitteln muss (bekannter Schwachpunkt, siehe Portfolio-Uebersicht-Fix
    vom 28.07.2026)."""
    if df_positionen.empty or 'Status' not in df_positionen.columns:
        return "Erfolgsbilanz: noch keine geschlossenen Positionen erfasst."

    status_norm = df_positionen['Status'].astype(str).str.strip().str.lower()
    geschlossen = df_positionen[status_norm.isin(['gestoppt', 'verkauft'])].copy()
    if geschlossen.empty:
        return "Erfolgsbilanz: noch keine geschlossenen Positionen erfasst."

    def _performance_frisch(row):
        try:
            einstieg = float(str(row['Einstieg']).replace(',', '.'))
            ausstieg = float(str(row['Ausstiegskurs']).replace(',', '.'))
            if einstieg <= 0:
                return None
            ist_short = str(row.get('Richtung', '')).strip().lower() == 'short'
            if ist_short:
                return round(((einstieg - ausstieg) / einstieg) * 100, 2)
            return round(((ausstieg - einstieg) / einstieg) * 100, 2)
        except (ValueError, TypeError, KeyError):
            return None

    geschlossen['_perf'] = geschlossen.apply(_performance_frisch, axis=1)
    gueltig = geschlossen[geschlossen['_perf'].notna()]
    if gueltig.empty:
        return ("Erfolgsbilanz: nicht berechenbar (Einstiegs-/Ausstiegskurse "
                "der geschlossenen Positionen nicht numerisch lesbar).")

    n_gesamt = len(gueltig)
    ist_stop = gueltig['Status'].astype(str).str.strip().str.lower() == 'gestoppt'
    n_stop, n_verkauft = int(ist_stop.sum()), int((~ist_stop).sum())

    gewinner = gueltig[gueltig['_perf'] > 0]
    verlierer = gueltig[gueltig['_perf'] < 0]
    trefferquote = round(len(gewinner) / n_gesamt * 100, 1)
    perf_gesamt = round(gueltig['_perf'].mean(), 2)
    perf_gewinner = round(gewinner['_perf'].mean(), 2) if not gewinner.empty else None
    perf_verlierer = round(verlierer['_perf'].mean(), 2) if not verlierer.empty else None

    perf_stop = round(gueltig[ist_stop]['_perf'].mean(), 2) if n_stop else None
    perf_verkauft = round(gueltig[~ist_stop]['_perf'].mean(), 2) if n_verkauft else None

    zeilen = [
        f"Erfolgsbilanz (gesamter Verlauf, {n_gesamt} geschlossene Positionen - "
        f"{n_stop} durch Stop, {n_verkauft} manuell verkauft):",
        f"- Trefferquote: {trefferquote}% ({len(gewinner)} von {n_gesamt} mit positivem Ergebnis)",
        f"- Ø Performance gesamt: {perf_gesamt:+.2f}%" +
        (f" | Ø Gewinner: {perf_gewinner:+.2f}% ({len(gewinner)} Titel)" if perf_gewinner is not None else "") +
        (f" | Ø Verlierer: {perf_verlierer:+.2f}% ({len(verlierer)} Titel)" if perf_verlierer is not None else ""),
    ]
    aufschluesselung = []
    if perf_stop is not None:
        aufschluesselung.append(f"Stop erreicht: Ø {perf_stop:+.2f}% ({n_stop} Titel)")
    if perf_verkauft is not None:
        aufschluesselung.append(f"Manuell verkauft: Ø {perf_verkauft:+.2f}% ({n_verkauft} Titel)")
    if aufschluesselung:
        zeilen.append(f"- Aufschlüsselung: {' | '.join(aufschluesselung)}")

    # BESTER/SCHLECHTESTER TRADE (NEU 30.07.2026, Nutzerwunsch, Ergaenzung
    # zur Erfolgsbilanz): einfache Max/Min-Bildung ueber dieselbe _perf-
    # Spalte - kein zusaetzliches Kopfrechnen, daher risikolos im Gegensatz
    # zur fruehreren Durchschnitts-Problematik. Bei GENAU EINER geschlossenen
    # Position waeren Bester und Schlechtester identisch - dann wird nur
    # EINE Zeile ausgegeben, um die Verdopplung derselben Aussage zu
    # vermeiden. Name-Spalte kann fehlen/leer sein (aeltere Zeilen) -
    # Fallback auf den Ticker, NIE einen Namen erfinden.
    def _bezeichnung(row):
        name = str(row.get('Name', '')).strip()
        return name if name and name.lower() != 'nan' else str(row['Ticker'])

    bester = gueltig.loc[gueltig['_perf'].idxmax()]
    schlechtester = gueltig.loc[gueltig['_perf'].idxmin()]
    if n_gesamt == 1:
        zeilen.append(f"- Einziger geschlossener Trade: {_bezeichnung(bester)} "
                      f"({bester['_perf']:+.2f}%)")
    else:
        zeilen.append(f"- Bester Trade: {_bezeichnung(bester)} ({bester['_perf']:+.2f}%) "
                      f"| Schlechtester Trade: {_bezeichnung(schlechtester)} "
                      f"({schlechtester['_perf']:+.2f}%)")
    return "\n".join(zeilen)


def get_fomc_rueckblick(rueckblick_tage=7):
    """FOMC-RUECKBLICK (NEU 30.07.2026, Nutzerwunsch): Gegenstueck zum
    Countdown - WAS hat die Fed entschieden? Bisher verschwand die Sitzung
    aus der Auswertung, sobald sie vorbei war: der Countdown sprang auf den
    naechsten Termin, das Ergebnis stand nirgends (aufgefallen am 30.07.,
    dem Tag nach der Sitzung vom 29.07.).

    Datenquelle: FRED-Serien DFEDTARU/DFEDTARL (obere/untere Grenze des
    Fed-Funds-Zielkorridors) - dieselbe schluessellose CSV-Route wie die
    Zinskurve. Bewusst KEINE News-Auswertung und KEINE Interpretation der
    Pressekonferenz: der Zielkorridor ist die harte, offizielle Zahl.
    Verglichen wird der aktuelle Korridor mit dem letzten Wert VOR dem
    Sitzungstag - daraus ergibt sich Senkung/Erhoehung in Basispunkten oder
    "unveraendert".

    Rueckgabe: Text oder None (wenn im Fenster keine Sitzung lag).
    Ehrliche Einschraenkung: FRED aktualisiert die Serie mit bis zu einem
    Werktag Verzoegerung. Ist der Datenstand aelter als der Sitzungstag,
    wird genau das gemeldet statt eine Nicht-Aenderung zu behaupten."""
    heute = datetime.date.today()
    vergangene = [d for d in FOMC_TERMINE_2026 if 0 <= (heute - d).days <= rueckblick_tage]
    if not vergangene:
        return None
    letzte_sitzung = max(vergangene)
    datum_text = letzte_sitzung.strftime("%d.%m.%Y")

    try:
        oben = hole_fred_zinsreihe("DFEDTARU", tage=90)
        unten = hole_fred_zinsreihe("DFEDTARL", tage=90)
        if oben.empty or unten.empty:
            return (f"FOMC-Rückblick: Sitzung vom {datum_text} - Zielkorridor aktuell "
                    f"nicht abrufbar (FRED-Daten leer)")

        stand_datum = oben["Datum"].iloc[-1].date()
        akt_oben, akt_unten = float(oben["Wert"].iloc[-1]), float(unten["Wert"].iloc[-1])
        korridor = f"{akt_unten:.2f}-{akt_oben:.2f}%"

        if stand_datum < letzte_sitzung:
            return (f"FOMC-Rückblick: Sitzung vom {datum_text} - Entscheidung in den "
                    f"FRED-Daten noch nicht abgebildet (Datenstand {stand_datum.strftime('%d.%m.%Y')}, "
                    f"Zielkorridor unverändert {korridor}); Aktualisierung folgt "
                    f"typischerweise am naechsten Werktag")

        vor_sitzung = oben[oben["Datum"].dt.date < letzte_sitzung]
        if vor_sitzung.empty:
            return f"FOMC-Rückblick: Sitzung vom {datum_text} - Zielkorridor aktuell {korridor}"
        vor_oben = float(vor_sitzung["Wert"].iloc[-1])
        delta_bp = round((akt_oben - vor_oben) * 100)

        if abs(delta_bp) < 1:
            entscheid = f"Zielkorridor UNVERAENDERT bei {korridor}"
        elif delta_bp < 0:
            entscheid = f"Zinssenkung um {abs(delta_bp)} Basispunkte auf {korridor}"
        else:
            entscheid = f"Zinserhoehung um {delta_bp} Basispunkte auf {korridor}"
        return f"FOMC-Rückblick: Sitzung vom {datum_text} - {entscheid}"
    except Exception as e:
        print(f"DEBUG-FOMC-RUECKBLICK: nicht ermittelbar ({type(e).__name__})")
        return (f"FOMC-Rückblick: Sitzung vom {datum_text} - Zielkorridor nicht "
                f"abrufbar (Abruf-Fehler)")


def get_fomc_countdown():
    """NEU (27.07.2026, Nutzerwunsch): reiner Termin-Countdown zur naechsten
    FOMC-Sitzung (Fed-Zinsentscheid) - analog zur Earnings-Warnung pro Aktie
    (get_earnings_warnung), nur fuer den Gesamtmarkt. BEWUSST kein Versuch,
    die "echten" CME-FedWatch-Markterwartungen (Wahrscheinlichkeit fuer
    Zinserhoehung/-senkung) nachzubauen - das haette eine eigene Berechnung
    ueber Fed-Funds-Futures (ZQ=F) noetig, mit unsicherer Genauigkeit
    gegenueber der proprietaeren CME-Methodik. Stattdessen nur der reine
    Termin, da FOMC-Sitzungen ein bekannter, terminierter Volatilitaets-
    Treiber sind (Zinsentscheid + Pressekonferenz), unabhaengig von der
    Richtung der Entscheidung.
    Reiner Kontext-Indikator, KEINE Setup-Quelle, KEINE Abwertungsgrundlage.
    WARTUNG (Pflicht, jaehrlich): FOMC_TERMINE unten muss jedes Jahr um die
    neuen Termine ergaenzt werden, sobald die Fed sie veroeffentlicht (siehe
    federalreserve.gov/monetarypolicy/fomccalendars.htm) - Datum jeweils der
    ZWEITE Sitzungstag (Tag der Zinsentscheid-Veroeffentlichung, 14:00 Uhr
    US-Ostkuestenzeit, entspricht ca. 20:00 Uhr MESZ/19:00 Uhr MEZ)."""
    # Terminliste steht seit 30.07.2026 auf Modulebene (auch vom Rueckblick genutzt)
    heute = datetime.date.today()
    kommende_termine = [d for d in FOMC_TERMINE_2026 if d >= heute]
    if not kommende_termine:
        return "FOMC-Sitzung: kein bevorstehender Termin hinterlegt (FOMC_TERMINE_2026 in analyse.py fuer das naechste Jahr aktualisieren)"

    naechster_termin = min(kommende_termine)
    tage_bis = (naechster_termin - heute).days
    datum_text = naechster_termin.strftime("%d.%m.%Y")

    if tage_bis == 0:
        return f"FOMC-Sitzung: HEUTE ({datum_text}) - Zinsentscheid ca. 20:00 Uhr MESZ"
    return f"FOMC-Sitzung: in {tage_bis} Tag(en) ({datum_text})"


def hole_fred_zinsreihe(serie_id, tage=400):
    """Laedt eine taegliche Zinsreihe von FRED (St. Louis Fed) als reines CSV
    ueber die oeffentliche graph/fredgraph.csv-Route - kein API-Key noetig,
    keine zusaetzliche Bibliothek (nur requests, das ohnehin transitiv ueber
    google-api-python-client/alpaca-py im Repo vorhanden ist). Gibt ein
    DataFrame mit Spalten Datum/Wert zurueck (leer bei Fehler)."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie_id}"
    antwort = requests.get(url, timeout=15)
    antwort.raise_for_status()
    df = pd.read_csv(io.StringIO(antwort.text))
    df.columns = ["Datum", "Wert"]
    df["Datum"] = pd.to_datetime(df["Datum"])
    # FRED schreibt fehlende Handelstage (Feiertage) als "." statt einer Zahl
    df["Wert"] = pd.to_numeric(df["Wert"], errors="coerce")
    df = df.dropna(subset=["Wert"])
    return df.tail(tage)


def get_zinskurve_fred():
    """NEU (27.07.2026, Nutzerwunsch): ersetzt die bisherigen get_zins_warner
    (30J, ^TYX via yfinance) und get_10j_rendite (10J, ^TNX via yfinance) durch
    eine konsolidierte Zinskurve ueber alle vier gewuenschten Laufzeiten
    (2J/5J/10J/30J). Grund fuer FRED statt yfinance: Yahoo Finance hat fuer
    5J/10J/30J brauchbare Index-Ticker (^FVX/^TNX/^TYX), aber KEINEN
    verlaesslichen offiziellen Ticker fuer die 2-jaehrige Rendite (nur
    Futures-Naeherungen mit abweichender Methodik) - FRED (St. Louis Fed,
    Serien DGS2/DGS5/DGS10/DGS30) deckt alle vier einheitlich und offiziell ab.
    Reiner Kontext-Indikator, KEINE Setup-Quelle, KEINE Abwertungsgrundlage.

    Zinskurven-Inversion (2J-Rendite > 10J-Rendite, "2s10s") gilt als einer
    der zuverlaessigsten historischen Rezessions-Fruehindikatoren ueberhaupt -
    deshalb zusaetzlich zu den vier Einzelwerten der 10J-2J-Spread inkl.
    Crossover-Erkennung (analog zum bestehenden Golden-/Death-Cross-Muster:
    wann hat sich das Vorzeichen des Spreads zuletzt gedreht)."""
    try:
        serien = {"2J": "DGS2", "5J": "DGS5", "10J": "DGS10", "30J": "DGS30"}
        reihen = {}
        for label, serie_id in serien.items():
            df = hole_fred_zinsreihe(serie_id)
            if df.empty:
                return "Zinskurve (2J/5J/10J/30J, FRED): Daten unvollständig"
            reihen[label] = df

        aktuelle_werte = {label: df["Wert"].iloc[-1] for label, df in reihen.items()}

        # 10J-2J-Spread ueber die Zeit (auf gemeinsame Handelstage gemerged,
        # da FRED-Reihen unterschiedlicher Laufzeiten an einzelnen Tagen
        # unterschiedliche Luecken haben koennen) + juengster Vorzeichenwechsel.
        merge = pd.merge(
            reihen["2J"][["Datum", "Wert"]], reihen["10J"][["Datum", "Wert"]],
            on="Datum", suffixes=("_2J", "_10J"),
        )
        merge["Spread"] = merge["Wert_10J"] - merge["Wert_2J"]
        aktueller_spread = merge["Spread"].iloc[-1]
        status = "normal (nicht invertiert)" if aktueller_spread >= 0 else "INVERTIERT"

        vorzeichen = np.sign(merge["Spread"])
        wechsel = vorzeichen.diff().fillna(0) != 0
        crossover_datum = merge.loc[wechsel, "Datum"].max() if wechsel.any() else None
        crossover_text = (
            f", letzter Crossover am {crossover_datum.strftime('%d.%m.%Y')}"
            if pd.notna(crossover_datum) else ", kein Crossover in der Historie gefunden"
        )

        zeile1 = (
            f"Zinskurve (2J/5J/10J/30J, FRED): 2J {aktuelle_werte['2J']:.2f}% | "
            f"5J {aktuelle_werte['5J']:.2f}% | 10J {aktuelle_werte['10J']:.2f}% | "
            f"30J {aktuelle_werte['30J']:.2f}%"
        )
        zeile2 = f"10J-2J-Spread: {aktueller_spread:+.2f} Prozentpunkte - {status}{crossover_text}"
        return zeile1 + "\n" + zeile2
    except Exception as e:
        print(f"DEBUG: Zinskurve (FRED) nicht verfügbar ({e}).")
        return "Zinskurve (2J/5J/10J/30J, FRED): Daten unvollständig"


# --- REGIONEN-PERFORMANCE (NEU 29.07.2026, Nutzerwunsch) ---
# Je Region die Veraenderung des letzten Handelstages und seit Jahresanfang.
# Zweck: die Auswertung soll mit einer Zeile pro Region beginnen, damit auf
# einen Blick klar ist, wo die Woche/das Jahr steht - vor allen Setups.
#
# Index-Zuordnung und Reihenfolge (GEAENDERT 29.07.2026, Nutzerwunsch):
#   Europa = DAX, dann EuroStoxx50
#   USA    = S&P 500, dann Nasdaq
#   Asien  = Nikkei 225, Shanghai Composite, Hang Seng
# KEIN Regionen-Mittelwert (ausdruecklicher Nutzerwunsch): jeder Index wird
# mit seinem eigenen Wert ausgewiesen. Ein Mittel ueber Indizes mit sehr
# unterschiedlicher Zusammensetzung und Waehrung wuerde ohnehin eine
# Genauigkeit suggerieren, die es nicht gibt - besonders in Asien, wo Tokio,
# Shanghai und Hongkong regelmaessig in verschiedene Richtungen laufen.
# Ehrliche Einschraenkung: "letzter Handelstag" bedeutet hier immer der
# letzte VOLLSTAENDIG ABGESCHLOSSENE Tagesbalken je Index. Bei einem Morgenlauf
# liegen die asiatischen Maerkte oft noch im laufenden Handel; dann bleibt der
# Datenstand bewusst beim Vortag.
REGIONEN = {
    "Europa": [("^GDAXI", "DAX"), ("^STOXX50E", "EuroStoxx50")],
    "USA": [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq")],
    "Asien": [("^N225", "Nikkei 225"), ("000001.SS", "Shanghai Composite"),
              ("^HSI", "Hang Seng")],
}


# --- GEMEINSAMER YFINANCE-CACHE (NEU 09.08.2026, Nutzerwunsch "Requests
# einsparen, da Daten schon vorliegen") ---
# Vorher riefen FUENF Funktionen (_index_performance, get_index_benchmark_yf,
# get_kurzfrist_kontext_text, get_index_rekord_text, get_rekord_naehe_text)
# JEDE FUER SICH yf.Ticker(ticker).history(...) auf - mit unterschiedlichen
# period-Werten (300d/1y/2y/max), aber oft fuer DIESELBEN Ticker. Konkret
# ueberschnitten sich: DAX/EuroStoxx50/S&P500/Nasdaq/Nikkei/HangSeng liefen
# bis zu DREIFACH, WTI/Brent DREIFACH, Gold/Silber/Dow/Russell/STOXX600/
# Shanghai je ZWEIFACH - macht 42 Einzelabrufe fuer nur 20 tatsaechlich
# unterschiedliche Ticker. "max" ist eine Obermenge jeder kuerzeren Periode,
# die die einzelnen Funktionen brauchen - deshalb genuegt EIN Abruf mit
# period="max" pro Ticker, aus dem sich jede Funktion ihr eigenes Fenster
# schneidet (Slicing passiert weiterhin in der jeweiligen Funktion selbst,
# unveraendert). EMA-Berechnungen (z.B. in get_index_benchmark_yf) sind
# davon nicht negativ betroffen: der Einfluss sehr alter Daten auf eine
# EMA200 ist nach wenigen hundert Tagen bereits vernachlaessigbar klein -
# mehr Historie im Fenster macht das Ergebnis wenn ueberhaupt marginal
# stabiler, nicht anders. Rollierende Fenster (z.B. WMA200) sind ohnehin
# unabhaengig von zusaetzlicher Vorgeschichte.
_YF_HISTORY_CACHE = {}


# --- ABGESCHLOSSENE TAGESKERZEN (NEU 12.08.2026) ---
_ABGESCHLOSSENE_TAGESMAERKTE = {
    "^N225": ("Asia/Tokyo", datetime.time(15, 30)),
    "000001.SS": ("Asia/Shanghai", datetime.time(15, 0)),
    "^HSI": ("Asia/Hong_Kong", datetime.time(16, 0)),
    "^GDAXI": ("Europe/Berlin", datetime.time(17, 30)),
    "^STOXX50E": ("Europe/Paris", datetime.time(17, 30)),
    "^STOXX": ("Europe/Paris", datetime.time(17, 30)),
    "^GSPC": ("America/New_York", datetime.time(16, 0)),
    "^IXIC": ("America/New_York", datetime.time(16, 0)),
    "^DJI": ("America/New_York", datetime.time(16, 0)),
    "^RUT": ("America/New_York", datetime.time(16, 0)),
    "^VIX": ("America/New_York", datetime.time(16, 0)),
}


def _markt_heutiges_datum(ticker):
    regel = _ABGESCHLOSSENE_TAGESMAERKTE.get(ticker)
    if regel:
        return datetime.datetime.now(ZoneInfo(regel[0])).date()
    return datetime.date.today()


def _nur_abgeschlossene_tagesbalken(hist, ticker):
    """Verwendet fuer bekannte Boersen nur vollstaendig abgeschlossene Tageskerzen."""
    if hist is None or hist.empty:
        return hist, None
    data = hist.dropna(subset=["Close"]).copy()
    if data.empty:
        return data, None
    regel = _ABGESCHLOSSENE_TAGESMAERKTE.get(ticker)
    if regel:
        tz_name, schlusszeit = regel
        jetzt_lokal = datetime.datetime.now(ZoneInfo(tz_name))
        letzter_datum = data.index[-1].date()
        if letzter_datum == jetzt_lokal.date() and jetzt_lokal.time() < schlusszeit:
            data = data.iloc[:-1]
    if data.empty:
        return data, None
    return data, data.index[-1].date()


def _hole_kursdaten_gecached(ticker):
    """Fetcht period="max" fuer `ticker` HOECHSTENS EINMAL pro Lauf und haelt
    das Ergebnis im Modul-Cache vor. Gibt eine KOPIE zurueck (nicht die im
    Cache gehaltene Referenz), damit keine aufrufende Funktion versehentlich
    die fuer alle anderen Aufrufer gecachten Daten veraendert. Liefert bei
    Fehlern ein leeres DataFrame (wie zuvor die einzelnen Funktionen es bei
    einer Exception implizit auch getan haetten) statt selbst zu crashen -
    die aufrufenden Funktionen pruefen ohnehin bereits auf .empty."""
    if ticker not in _YF_HISTORY_CACHE:
        try:
            # Zuerst der prozessuebergreifende Dateicache, danach erst Yahoo.
            # Der bestehende RAM-Cache bleibt als schnellste zweite Ebene
            # erhalten. Dadurch koennen analyse.py, trendwende_scanner.py,
            # short_scanner.py und edelmetalle_scanner.py im selben Workflow
            # gemeinsame Benchmarkdaten wiederverwenden.
            _YF_HISTORY_CACHE[ticker] = get_yf_history(ticker)
        except Exception as e:
            print(f"DEBUG-CACHE: {ticker} nicht abrufbar ({type(e).__name__}: {e})")
            _YF_HISTORY_CACHE[ticker] = pd.DataFrame()
    return _YF_HISTORY_CACHE[ticker].copy()


def _index_performance(ticker):
    """Gibt (Vortagsveraenderung%, YTD%, aktueller_Punktestand) fuer einen
    Index zurueck, oder (None, None, None). Alles aus EINEM yfinance-Abruf
    (1 Jahr Historie). Der Punktestand (NEU 03.08.2026, Nutzerwunsch) ist
    der zuletzt bekannte Schlusskurs - dieselbe Zahl, die auch fuer die
    Prozent-Berechnung genutzt wird, kein separater Abruf noetig."""
    try:
        hist = _hole_kursdaten_gecached(ticker)
        hist, letztes_datum = _nur_abgeschlossene_tagesbalken(hist, ticker)
        if hist.empty or len(hist) < 2:
            return None, None, None, "", None
        schluss = hist['Close']
        if len(schluss) < 2:
            return None, None, None, "", None
        letzter = float(schluss.iloc[-1])
        vortag = float(schluss.iloc[-2])
        tag_pct = (letzter / vortag - 1) * 100 if vortag else None

        # STALENESS NUR TRANSPARENT AUSWEISEN:
        # Wenn Yahoo voruebergehend einen alten, aber gueltigen abgeschlossenen
        # Tagesstand liefert, wird dieser NICHT in n/a verwandelt. Der reale
        # Datenstand bleibt sichtbar und die Auswertung kann ihn transparent
        # kennzeichnen. Dadurch bleibt die Auswertung belastbar, sobald Yahoo
        # wieder aktuelle Tagesdaten liefert, ohne dass hier weitere Datenquellen
        # oder zusaetzliche API-Abfragen erforderlich sind.
        heute = _markt_heutiges_datum(ticker)
        tage_alt = (heute - letztes_datum).days
        max_alter_tage = 3 if heute.weekday() in (0, 6) else 1
        staleness_hinweis = ""
        if tage_alt > max_alter_tage:
            staleness_hinweis = (f" [WARNUNG: Datenstand vom {letztes_datum.strftime('%d.%m.%Y')}, "
                                 f"{tage_alt} Tage alt - moeglicherweise veraltet]")
            print(f"DEBUG-STALENESS: {ticker} -> letzter Datenpunkt "
                  f"{letztes_datum.strftime('%d.%m.%Y')}, {tage_alt} Tage alt - Wert bleibt sichtbar")


        # YTD: letzter Schlusskurs des VORJAHRES als Basis (nicht der erste
        # Kurs des neuen Jahres - sonst fehlt der Jahreswechsel-Gap).
        jahr = datetime.date.today().year
        idx = schluss.index
        jahre = [d.year for d in idx]
        vorjahr_positionen = [i for i, j in enumerate(jahre) if j < jahr]
        if vorjahr_positionen:
            basis = float(schluss.iloc[vorjahr_positionen[-1]])
        else:
            basis = float(schluss.iloc[0])  # Historie beginnt erst im laufenden Jahr
        ytd_pct = (letzter / basis - 1) * 100 if basis else None
        return tag_pct, ytd_pct, letzter, staleness_hinweis, letztes_datum
    except Exception as e:
        print(f"DEBUG-REGIONEN-PERFORMANCE: {ticker} nicht ermittelbar ({type(e).__name__})")
        return None, None, None, "", None


def get_handelstag_text(referenz_ticker="^GSPC", referenz_label="S&P 500"):
    """NEU (08.08.2026, Nutzerwunsch): eine klare, immer sichtbare Zeile,
    auf welchen Handelstag sich die gesamte Auswertung bezieht - und ob
    dieser abgeschlossen ist. Anlass: der DAX-Staleness-Bug vom 04.08.2026
    zeigte, dass ein veralteter Datenstand unbemerkt bleiben kann, wenn
    nirgends explizit steht, WELCHER Tag eigentlich gemeint ist.
    Nutzt EINEN Referenz-Index (Standard S&P 500, global anerkannter
    Leitindex) statt alle Benchmarks einzeln zu pruefen - das Datum des
    letzten Balkens dort gilt als Datenstand fuer die gesamte Auswertung.
    LOGIK: Der Referenzwert wird ebenfalls auf die letzte vollstaendig
    abgeschlossene Tageskerze reduziert. Damit bleibt der Handelstag auch bei
    manuellen Laeufen waehrend laufender Handelszeiten eindeutig. Bei
    einem manuellen Lauf waehrend laufender Handelszeit (workflow_dispatch)
    kann yfinance aber einen unfertigen "heutigen" Balken liefern - genau
    das faengt der Datums-Vergleich ab (letzter Balken == heute -> als
    Zwischenstand markiert, nicht als abgeschlossen)."""
    try:
        hist = _hole_kursdaten_gecached(referenz_ticker)
        hist, letztes_datum = _nur_abgeschlossene_tagesbalken(hist, referenz_ticker)
        if hist.empty or letztes_datum is None:
            return None
        heute = datetime.date.today()
        wochentage = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
                     "Samstag", "Sonntag"]
        wochentag_text = wochentage[letztes_datum.weekday()]
        if letztes_datum < heute:
            status = "abgeschlossen"
        elif letztes_datum == heute:
            status = "läuft noch / Zwischenstand (Lauf während laufender Handelszeit)"
        else:
            status = "Datum in der Zukunft - unplausibel, bitte prüfen"
        return (f"Handelstag (Datenstand dieser Auswertung): {wochentag_text}, "
               f"{letztes_datum.strftime('%d.%m.%Y')} - {status} (Referenz: {referenz_label})")
    except Exception as e:
        print(f"DEBUG-HANDELSTAG: nicht ermittelbar ({type(e).__name__}: {e})")
        return None


def get_handelstage_je_region_text():
    """Liefert den tatsaechlichen Datenstand je Region, ohne ein einzelnes
    Referenzinstrument stillschweigend auf die gesamte Region zu uebertragen.
    Wenn sich die Indizes einer Region auf unterschiedliche letzte Handelstage
    beziehen, werden alle vorhandenen Datenstaende genannt."""
    datenstaende = {}
    for region, indizes in REGIONEN.items():
        daten = []
        for ticker, _label in indizes:
            _tag, _ytd, _stand, _stale, letztes_datum = _index_performance(ticker)
            if letztes_datum is not None:
                text = letztes_datum.strftime('%d.%m.%Y')
                if text not in daten:
                    daten.append(text)
        datenstaende[region] = ' / '.join(daten) if daten else 'n/a'
    return ("Datenstaende dieser Auswertung: "
            f"USA {datenstaende['USA']} / Europa {datenstaende['Europa']} / "
            f"Asien {datenstaende['Asien']}")


def get_regionen_performance_text():
    """Baut den Briefing-Block. Fehlende Werte werden als 'n/a' ausgewiesen,
    nie stillschweigend weggelassen."""
    zeilen = ["REGIONEN-PERFORMANCE (letzter abgeschlossener Handelstag / seit Jahresanfang)"]
    zeilen.append("(je Index einzeln ausgewiesen, kein Regionen-Mittelwert; nur vollstaendig "
                  "abgeschlossene Tageskerzen werden verwendet. Datenstand je Index.)")
    for region, indizes in REGIONEN.items():
        ergebnisse = []
        for ticker, label in indizes:
            tag_pct, ytd_pct, stand, staleness, letztes_datum = _index_performance(ticker)
            ergebnisse.append((label, tag_pct, ytd_pct, stand, staleness, letztes_datum))
        region_daten = []
        for _label, _tag_pct, _ytd_pct, _stand, _staleness, _letztes_datum in ergebnisse:
            if _letztes_datum is not None:
                _datum_text = _letztes_datum.strftime('%d.%m.%Y')
                if _datum_text not in region_daten:
                    region_daten.append(_datum_text)
        if not region_daten:
            region_kopf = f"{region} (Datenstand: n/a):"
        elif len(region_daten) == 1:
            region_kopf = f"{region} (Datenstand: {region_daten[0]}):"
        else:
            region_kopf = f"{region} (Datenstände: {' / '.join(region_daten)}):"
        zeilen.append(region_kopf)
        for label, tag_pct, ytd_pct, stand, staleness, letztes_datum in ergebnisse:
            if tag_pct is None or ytd_pct is None:
                zeilen.append(f"  • {label}: n/a (Kursdaten nicht verfuegbar)")
            else:
                datenstand = letztes_datum.strftime('%d.%m.%Y') if letztes_datum else "n/a"
                zeilen.append(f"  • {label}: letzter abgeschlossener Handelstag {tag_pct:+.2f}% ({stand:,.2f}) | "
                              f"YTD {ytd_pct:+.2f}% | Datenstand {datenstand}{staleness}")
    return "\n".join(zeilen)


# --- 52-WOCHEN-KONTEXT + REKORD-NAEHE (NEU 30.07.2026, Nutzerwunsch) ---
# Anlass: Oel (WTI/Brent) hatte bisher nur EMA-Vergleiche im Briefing, keine
# Einordnung in die Jahresspanne wie die Edelmetalle ("LAGE JE METALL").
# Zusaetzlich sollen Oel UND Edelmetalle gemeldet werden, wenn sie nahe an
# ihrem Rekordhoch/-tief seit Datenbeginn stehen - nicht nur binaer
# (erreicht/nicht erreicht), sondern auch die Naehe dazu (Nutzerwunsch:
# "nicht nur das reine Erreichen des Hoechst-/Tiefpunkts mitteilen").
REKORD_NAEHE_SCHWELLE_PROZENT = 10.0


def get_kurzfrist_kontext_text(ticker, label, wochen=4, naehe_schwelle_prozent=10.0):
    """GEAENDERT 04.08.2026, Nutzerwunsch: ersetzt den vorherigen, IMMER
    angezeigten 52-Wochen-Vergleich (Abstand zu Tief UND Hoch, ganzjaehrig)
    durch eine kompaktere Darstellung:
      1) Kursverlauf der letzten `wochen` Wochen - kurzfristiger und direkter
         lesbar als ein Jahresvergleich, der bei ruhigen Phasen wenig aussagt.
      2) Ein Jahreshoch-/Jahrestief-Hinweis NUR WENN der Kurs tatsaechlich
         nahe dran ist (Schwelle 10%, dieselbe Konvention wie beim Rekord-
         Naehe-Hinweis) - der Normalfall (Kurs mittig in der Jahresspanne)
         bleibt ohne diesen Zusatz.
    Bewusst nur fuer Oel (WTI/Brent) und Gold (Nutzerwunsch, 04.08.2026) -
    Silber/Platin/Palladium behalten die volle 52W-Spannen-Anzeige in
    "LAGE JE METALL" (edelmetalle_scanner.py), da dort die Naehe zum Tief
    Teil der Trendwende-Filterlogik ist (Spannen-Position), nicht blosser
    Kontext wie hier."""
    try:
        data = _hole_kursdaten_gecached(ticker)
        if data.empty:
            return None

        kurzfrist = data.tail(wochen * 5)
        if len(kurzfrist) < 2:
            return None
        kurs = float(kurzfrist['Close'].iloc[-1])
        kurs_vor_wochen = float(kurzfrist['Close'].iloc[0])
        veraenderung = (kurs / kurs_vor_wochen - 1) * 100 if kurs_vor_wochen else None

        stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
        if getattr(data.index, 'tz', None) is not None:
            stichtag = stichtag.tz_localize(data.index.tz)
        fenster_52w = data[data.index >= stichtag]
        if len(fenster_52w) < 60:
            fenster_52w = data.tail(252)
        tief_52w = float(fenster_52w['Low'].min())
        hoch_52w = float(fenster_52w['High'].max())

        text = f"{label}: Kurs {kurs:.2f}"
        if veraenderung is not None:
            text += f" | {veraenderung:+.1f}% in den letzten {wochen} Wochen"

        if tief_52w > 0 and hoch_52w > 0:
            abstand_hoch = (hoch_52w - kurs) / hoch_52w * 100
            abstand_tief = (kurs - tief_52w) / tief_52w * 100
            if abstand_hoch <= naehe_schwelle_prozent:
                text += f" - nahe seinem 52-Wochen-Hoch ({hoch_52w:.2f}, {abstand_hoch:.1f}% darunter)"
            elif abstand_tief <= naehe_schwelle_prozent:
                text += f" - nahe seinem 52-Wochen-Tief ({tief_52w:.2f}, {abstand_tief:.1f}% darüber)"
        return text
    except Exception as e:
        print(f"DEBUG-KURZFRIST-KONTEXT: {ticker} nicht ermittelbar ({type(e).__name__})")
        return None



# --- KURZFRIST-KONTEXT ÖL/EDELMETALLE (NEU 12.08.2026) ---
def get_markt_kurzfrist_kontext_text(ticker, label):
    """Aktueller Kurs + 5 Handelstage + 4 Wochen + optionale 52W-Nähe.
    Ausschließlich abgeschlossene Tageskerzen."""
    try:
        data = _hole_kursdaten_gecached(ticker)
        data, letztes_datum = _nur_abgeschlossene_tagesbalken(data, ticker)
        if data.empty or len(data) < 25:
            return None

        close = data["Close"].dropna()
        kurs = float(close.iloc[-1])

        v5 = None
        if len(close) >= 6 and float(close.iloc[-6]):
            v5 = (kurs / float(close.iloc[-6]) - 1) * 100

        v4 = None
        if len(close) >= 21 and float(close.iloc[-21]):
            v4 = (kurs / float(close.iloc[-21]) - 1) * 100

        fenster = data.tail(252)
        hoch = float(fenster["High"].max())
        tief = float(fenster["Low"].min())

        teile = [f"{label}: Kurs {kurs:.2f}"]
        if v5 is not None:
            teile.append(f"{v5:+.1f}% 5T")
        if v4 is not None:
            teile.append(f"{v4:+.1f}% 4W")

        if hoch > 0:
            abstand_hoch = (hoch - kurs) / hoch * 100
            if abstand_hoch <= 10.0:
                teile.append(f"nahe 52W-Hoch ({hoch:.2f}, {abstand_hoch:.1f}% darunter)")
        if tief > 0:
            abstand_tief = (kurs - tief) / tief * 100
            if abstand_tief <= 10.0 and not any("nahe 52W-Hoch" in x for x in teile):
                teile.append(f"nahe 52W-Tief ({tief:.2f}, {abstand_tief:.1f}% darüber)")

        if letztes_datum is not None:
            teile.append(f"Datenstand {letztes_datum.strftime('%d.%m.%Y')}")
        return " | ".join(teile)
    except Exception as e:
        print(f"DEBUG-KURZFRIST-MARKTKONTEXT: {ticker} nicht ermittelbar ({type(e).__name__})")
        return None

# --- SAISONALITAET (NEU 02.08.2026, Nutzerwunsch) ---
# Quelle: vom Nutzer bereitgestelltes PDF "Saisonalitaet Edelmetalle &
# Rohstoffe" (RealMoneyTrader Research, 27-46 Jahre historische Daten je
# Instrument). Rein KALENDERBASIERT - kein API-Aufruf noetig, nur ein
# Abgleich des heutigen Datums gegen feste Zeitfenster. Bewusst reiner
# Fliesstext-Kontext wie bei Kupfer ("Dr. Copper") - KEIN Signal, KEIN
# Qualitaets-Modifikator, verwirft/bevorzugt keine Setups.
#
# EHRLICHE EINSCHRAENKUNGEN:
# - Platin hatte im Quellmaterial kein eigenes Diagramm, nur den Hinweis
#   "Palladiumpreise steigen, aehnlich wie Platin" - das Palladium-Fenster
#   wird deshalb NAEHERUNGSWEISE auf Platin uebertragen, im Text markiert.
# - Brent hatte ebenfalls kein eigenes Diagramm, nur WTI. Rohoel-Sorten
#   korrelieren stark, aber nicht perfekt - das WTI-Fenster wird deshalb
#   NAEHERUNGSWEISE auf Brent uebertragen, im Text markiert.
# - Saisonalitaet ist ein bekanntes, aber auch umstrittenes Feld (Gefahr
#   von Data-Mining bei einzelnen Quellen ueber viele Jahre) - deshalb
#   ausdruecklich Kontext, nie Filter oder Bewertungsgrundlage.
SAISONALITAET_FENSTER = {
    "Gold": [
        ((5, 1), (8, 31), "long", "Fenster (Mai bis August)", 43),
        ((12, 15), (1, 15), "long", "Fenster um den Jahreswechsel (Mitte Dezember bis Mitte Januar)", 43),
    ],
    "Silber": [
        ((12, 15), (2, 15), "long", "und saisonal stärkstes Fenster (Mitte Dezember bis Mitte Februar)", 46),
        ((9, 1), (9, 30), "long", "Fenster (September)", 46),
    ],
    "Palladium": [
        ((12, 15), (2, 15), "long", "besonders zuverlässiges saisonal long-günstiges Fenster (Mitte Dezember bis Mitte Februar)", 35),
        ((2, 16), (5, 31), "long", "Fenster (Mitte Februar bis Ende Mai)", 35),
    ],
    "Platin": [
        ((12, 15), (2, 15), "long", "besonders zuverlässiges saisonal long-günstiges Fenster (Mitte Dezember bis Mitte Februar, NÄHERUNGSWEISE vom Palladium-Muster übertragen, kein eigenes Diagramm in der Quelle)", 35),
        ((2, 16), (5, 31), "long", "Fenster (Mitte Februar bis Ende Mai, NÄHERUNGSWEISE vom Palladium-Muster übertragen, kein eigenes Diagramm in der Quelle)", 35),
    ],
    "WTI": [
        ((2, 15), (5, 5), "long", "stabilstes Fenster (Mitte Februar bis Anfang Mai)", 34),
        ((10, 15), (12, 15), "short", "aussichtsreichstes Fenster (Mitte Oktober bis Mitte Dezember)", 34),
    ],
    "Brent": [
        ((2, 15), (5, 5), "long", "stabilstes Fenster (Mitte Februar bis Anfang Mai, NÄHERUNGSWEISE von WTI übertragen, kein eigenes Diagramm in der Quelle)", 34),
        ((10, 15), (12, 15), "short", "aussichtsreichstes Fenster (Mitte Oktober bis Mitte Dezember, NÄHERUNGSWEISE von WTI übertragen, kein eigenes Diagramm in der Quelle)", 34),
    ],
}


def _in_saison_fenster(heute, start_md, ende_md):
    """Prueft, ob (heute.month, heute.day) im Fenster [start_md, ende_md]
    liegt - inklusive Jahreswechsel-Wrap (z.B. 15.12. bis 15.02.)."""
    heute_md = (heute.month, heute.day)
    if start_md <= ende_md:
        return start_md <= heute_md <= ende_md
    return heute_md >= start_md or heute_md <= ende_md


def get_saisonalitaet_text(label):
    """Gibt eine fertige Kontext-Zeile zurueck, wenn das heutige Datum in
    einem der definierten saisonalen Fenster fuer `label` liegt, sonst
    None (der Normalfall - die meiste Zeit des Jahres liegt kein Titel in
    einem der eng definierten Fenster, dann bleibt die Zeile schlicht
    weg, wie bei der Rekord-Naehe)."""
    fenster_liste = SAISONALITAET_FENSTER.get(label)
    if not fenster_liste:
        return None
    heute = datetime.date.today()
    for start_md, ende_md, richtung, beschreibung, jahre in fenster_liste:
        if _in_saison_fenster(heute, start_md, ende_md):
            richtung_wort = "long-günstigen" if richtung == "long" else "short-günstigen"
            return (f"Saisonalität {label} ({jahre} Jahre Historie, Quelle RealMoneyTrader "
                   f"Research): aktuell in einem historisch {richtung_wort} {beschreibung}.")
    return None


# --- REKORDHOCH-HINWEIS FUER AKTIENINDIZES (NEU 03.08.2026, Nutzerwunsch) ---
# Anlass: DAX erreichte am 03.08.2026 ein neues Rekordhoch, tauchte im
# Briefing aber nicht auf - get_rekord_naehe_text() deckt nur Oel/Edelmetalle
# ab, keine Aktienindizes. Bewusst als EIGENSTAENDIGE, einfachere Funktion
# gebaut statt die Oel/Edelmetall-Funktion wiederzuverwenden, weil die
# Anforderung eine andere ist:
#   - Schwelle 3% statt 10% (Nutzerwunsch) - enger gefasst, weil Aktien-
#     indizes prozentual ruhiger schwanken als Rohstoff-Futures.
#   - NUR EINE Meldungsart ("Rekordhoch erreicht/ueberschritten"), KEINE
#     separate schwaechere "in der Naehe"-Vorstufe wie bei Oel/Edelmetallen
#     (Nutzerwunsch: "nur Hinweis bei Ueberschreiten"). Die 3%-Schwelle
#     dient dabei als TOLERANZBAND, nicht als eigene Meldestufe: Indizes
#     setzen Rekorde oft INTRADAY, der Tagesschlusskurs kann leicht darunter
#     liegen und zaehlt trotzdem praktisch als "Rekord erreicht" - deshalb
#     wird bereits ab einem Abstand von <= 3% zum bisherigen Hoch gemeldet,
#     nicht erst beim exakten UEbertreffen des Schlusskurs-Maximums.
# EHRLICHE EINSCHRAENKUNG (wie bei Oel/Edelmetallen): "Rekord" bezieht sich
# auf die verfuegbare yfinance-'max'-Historie (meist ab den 1990ern/2000ern
# je nach Index), kein zwingend geprueftes Allzeithoch seit Index-Auflegung.
INDEX_REKORD_SCHWELLE_PROZENT = 1.0

# "Alle Indizes" (Nutzerwunsch) - jeder Aktienindex, der irgendwo im System
# als Benchmark gefuehrt wird (BENCHMARKS-Block + Regionen-Performance).
ALLE_INDIZES = [
    ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow Jones"),
    ("^RUT", "Russell 2000"),
    ("^GDAXI", "DAX"), ("^STOXX50E", "EuroStoxx50"), ("^STOXX", "STOXX Europe 600"),
    ("^N225", "Nikkei 225"), ("000001.SS", "Shanghai Composite"), ("^HSI", "Hang Seng"),
]


def get_index_rekord_text(ticker, label, schwelle_prozent=INDEX_REKORD_SCHWELLE_PROZENT):
    """Meldet NUR, wenn ein Aktienindex sein Rekordhoch seit Datenbeginn
    erreicht/ueberschritten hat oder (Toleranzband 3%) knapp darunter liegt -
    anders als get_rekord_naehe_text() gibt es HIER KEINE separate "in der
    Naehe, aber noch weit entfernt"-Meldung (siehe Modul-Kommentar oben).
    Gibt einen fertigen Text zurueck oder None (der Normalfall)."""
    try:
        data = _hole_kursdaten_gecached(ticker)
        data, letztes_datum = _nur_abgeschlossene_tagesbalken(data, ticker)
        if data.empty or len(data) < 60:
            return None
        rekord_hoch = float(data['High'].max())
        kurs = float(data['Close'].iloc[-1])
        start_jahr = data.index[0].year
        if rekord_hoch <= 0:
            return None
        abstand_pct = (rekord_hoch - kurs) / rekord_hoch * 100
        if abstand_pct > schwelle_prozent:
            return None
        if kurs >= rekord_hoch:
            return (f"{label} notiert auf einem neuen Rekordhoch seit Datenbeginn "
                   f"(ca. {start_jahr}) - aktueller Stand {kurs:,.2f} Punkte.")
        return (f"{label} hat sein bisheriges Rekordhoch seit Datenbeginn (ca. {start_jahr}, "
               f"{rekord_hoch:,.2f} Punkte) im Tagesverlauf nahezu erreicht - aktueller "
               f"Schlussstand {kurs:,.2f} Punkte ({abstand_pct:.1f}% darunter).")
    except Exception as e:
        print(f"DEBUG-INDEX-REKORD: {ticker} nicht ermittelbar ({type(e).__name__}: {e})")
        return None


def get_rekord_naehe_text(ticker, label, schwelle_prozent=REKORD_NAEHE_SCHWELLE_PROZENT):
    """Prueft, ob ein Instrument auf oder nahe seinem Rekordhoch/-tief SEIT
    DATENBEGINN steht (period='max' bei yfinance). EHRLICHE EINSCHRAENKUNG,
    die auch im Text steht: bei Futures-Continuous-Kontrakten (Oel, Metalle)
    reicht yfinance's 'max'-Historie in aller Regel nur bis ca. 2000 zurueck -
    das ist also der hoechste/tiefste Stand SEIT DIESEM DATENBEGINN, kein
    geprueftes echtes Allzeit-Rekord (z. B. Oel-Spitzen der 1980er waeren
    darin nicht erfasst). Deshalb wird das Startdatum der Reihe IMMER mit
    genannt statt "Rekord" unbelegt zu behaupten.

    GEAENDERT (30.07.2026, Nutzerwunsch): meldet nicht nur das exakte
    Erreichen, sondern auch die NAEHE dazu (Standard-Schwelle 10%) - z. B.
    "bewegt sich in der Naehe seines Rekordhochs ... - aktuell 6,2% darunter".
    Gibt eine fertige Textzeile zurueck, oder None wenn weder erreicht noch
    in der Naehe (dann bleibt die Zeile in der Ausgabe einfach weg - das ist
    der Normalfall, nur auffaellige Tage sollen ueberhaupt erscheinen)."""
    try:
        data = _hole_kursdaten_gecached(ticker)
        if data.empty or len(data) < 60:
            return None
        rekord_hoch = float(data['High'].max())
        rekord_tief = float(data['Low'].min())
        kurs = float(data['Close'].iloc[-1])
        start_jahr = data.index[0].year
        if rekord_hoch <= 0 or rekord_tief <= 0:
            return None

        meldungen = []
        # Naehe zum Rekordhoch (Toleranz 0.1% fuer "erreicht", wegen
        # Rundungsdifferenzen zwischen Intraday-High und Schlusskurs)
        abstand_hoch_pct = (rekord_hoch - kurs) / rekord_hoch * 100
        if abstand_hoch_pct <= 0.1:
            meldungen.append(f"{label} notiert auf einem neuen Rekordhoch seit "
                             f"Datenbeginn (ca. {start_jahr}).")
        elif abstand_hoch_pct <= schwelle_prozent:
            meldungen.append(f"{label} bewegt sich in der Nähe seines Rekordhochs "
                             f"seit Datenbeginn (ca. {start_jahr}) - aktuell "
                             f"{abstand_hoch_pct:.1f}% darunter.")

        # Naehe zum Rekordtief (gespiegelt)
        abstand_tief_pct = (kurs - rekord_tief) / rekord_tief * 100
        if abstand_tief_pct <= 0.1:
            meldungen.append(f"{label} notiert auf einem neuen Rekordtief seit "
                             f"Datenbeginn (ca. {start_jahr}).")
        elif abstand_tief_pct <= schwelle_prozent:
            meldungen.append(f"{label} bewegt sich in der Nähe seines Rekordtiefs "
                             f"seit Datenbeginn (ca. {start_jahr}) - aktuell "
                             f"{abstand_tief_pct:.1f}% darüber.")

        return " ".join(meldungen) if meldungen else None
    except Exception as e:
        print(f"DEBUG-REKORD-NAEHE: {ticker} nicht ermittelbar ({type(e).__name__})")
        return None


def get_index_benchmark_yf(ticker, label):
    """Generische Benchmark-Funktion für Indizes/Futures via yfinance - u.a.
    für S&P 500 (^GSPC) und Nasdaq (^IXIC) seit 27.07.2026 (vorher fälschlich
    SPY-/QQQ-ETF-Kurse über Alpaca als Indexstand ausgegeben, siehe main unten),
    außerdem DAX, EuroStoxx50 und alle weiteren Nicht-Alpaca-Benchmarks."""
    try:
        hist = _hole_kursdaten_gecached(ticker)

        if hist.empty:
            return f"{label}: Daten unvollständig"

        # Nur vollstaendig abgeschlossene Tageskerzen verwenden. Das verhindert
        # insbesondere im Morgenlauf laufende Asien-Tagesstaende.
        hist, letztes_datum = _nur_abgeschlossene_tagesbalken(hist, ticker)

        if hist.empty or len(hist) < 200:
            return f"{label}: Daten unvollständig"

        close = hist['Close']
        last_close = close.iloc[-1]

        # STALENESS-PRUEFUNG (NEU 04.08.2026, BUGFIX - Nutzer-Verdacht bestaetigt):
        # Am 04.08.2026 lieferte yfinance fuer den DAX einen Schlusskurs vom
        # VORVORTAG (Freitag 31.07. statt Montag 03.08., an dem der DAX real
        # erstmals ueber 26.000 Punkte stieg - via Websuche verifiziert). Der
        # Fehler war unsichtbar: kein Absturz, keine Exception, einfach ein
        # veralteter Wert, der wie ein aktueller aussah. Deshalb: das Datum der
        # letzten Zeile pruefen. Bei einem WOCHENTAG-Datum, das mehr als 1 Tag
        # zurueckliegt (Toleranz fuer Wochenenden: bis zu 3 Kalendertage), wird
        # das im Text sichtbar markiert statt den Wert kommentarlos als aktuell
        # auszugeben - besser eine sichtbare Warnung als ein unbemerkt falscher
        # Marktstand in der Auswertung.
        heute = _markt_heutiges_datum(ticker)
        tage_alt = (heute - letztes_datum).days
        # Wochenende grosszuegig tolerieren (Freitagsschluss am Montag/Sonntag
        # ist normal), Feiertage nicht extra beruecksichtigt (seltener Fall,
        # dann greift die Warnung einmal zusaetzlich - unschaedlich).
        max_alter_tage = 3 if heute.weekday() in (0, 6) else 1
        staleness_hinweis = ""
        if tage_alt > max_alter_tage:
            staleness_hinweis = (f" [WARNUNG: Datenstand vom {letztes_datum.strftime('%d.%m.%Y')} "
                                 f"({tage_alt} Tage alt - moeglicherweise veraltet]")
            print(f"DEBUG-STALENESS: {label} ({ticker}) -> letzter Datenpunkt "
                  f"{letztes_datum.strftime('%d.%m.%Y')}, {tage_alt} Tage alt")

        e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        e100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        weights = np.arange(1, 201)
        w200 = close.rolling(200).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True).iloc[-1]

        # Levels für die regelbasierte Marktumfeld-Klassifikation merken
        BENCHMARK_LEVELS[label] = {"Kurs": float(last_close), "EMA20": float(e20),
                                   "EMA50": float(e50), "WMA200": float(w200)}

        # Nachkommastellen (GEÄNDERT 28.07.2026, Nutzerwunsch): bei Werten
        # unter 100 (Kupfer ~6, VIX ~19, Silber ~57, WTI ~81) sind ganzzahlig
        # gerundete EMAs wertlos ("Kupfer EMA20: 6" bei Kurs 6,31) - dort
        # jetzt 2 Nachkommastellen; große Indizes bleiben ganzzahlig.
        nk = 2 if last_close < 100 else 0
        return (f"{label}: {last_close:.2f} | EMA20: {e20:.{nk}f} | EMA50: {e50:.{nk}f} | "
                f"EMA100: {e100:.{nk}f} | EMA200: {e200:.{nk}f} | WMA200: {w200:.{nk}f} | "
                f"Datenstand: {letztes_datum.strftime('%d.%m.%Y')}"
                f"{staleness_hinweis}")

    except Exception as e:
        return f"{label}: Fehler beim Abruf ({e})"

def get_benchmark_close():
    """Lädt die rohen SPY-Schlusskurse (ca. 1 Jahr) als Series.

    SPY wird pro Workflow nur einmal aus Alpaca geladen und anschließend
    prozessuebergreifend aus market_cache.json wiederverwendet. Damit holen
    Hauptscanner, Trendwende-, Short- und Einzel-Check nicht jeweils dieselbe
    Benchmark erneut ab.
    """
    try:
        def _fetch():
            start_date = datetime.datetime.now() - datetime.timedelta(days=365)
            request = StockBarsRequest(symbol_or_symbols=["SPY"], start=start_date, timeframe=TimeFrame.Day)
            bars = alpaca_client.get_stock_bars(request)
            hist = bars.df
            if hist.empty:
                return pd.Series(dtype=float)
            hist = hist.reset_index(level=0, drop=True)
            if 'close' in hist.columns:
                hist = hist.rename(columns={'close': 'Close'})
            return hist['Close']

        series = get_or_fetch_series("alpaca:SPY:close", _fetch)
        if series is None or series.empty:
            print("DEBUG: SPY-Benchmark leer, Relative Stärke wird übersprungen.")
            return None
        return series

    except Exception as e:
        print(f"FEHLER beim Laden der SPY-Benchmark: {e}")
        return None

# --- EU-SPEZIFISCHE DATENFUNKTIONEN (yfinance, da Alpaca keine STOXX-600-Werte abdeckt) ---

def get_eu_benchmark_close():
    """Lädt die rohen Schlusskurse von EXSA.DE aus dem gemeinsamen Cache."""
    try:
        hist = get_yf_history(eu_benchmark_ticker)
        if hist.empty:
            print("DEBUG: EU-Benchmark (EXSA.DE) leer, Relative Stärke EU wird übersprungen.")
            return None
        hist = hist.dropna(subset=['Close'])
        if hist.empty:
            print("DEBUG: EU-Benchmark (EXSA.DE) nach NaN-Bereinigung leer, Relative Stärke EU wird übersprungen.")
            return None
        stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
        if getattr(hist.index, 'tz', None) is not None:
            stichtag = stichtag.tz_localize(hist.index.tz)
        hist = hist[hist.index >= stichtag]
        return hist['Close']
    except Exception as e:
        print(f"FEHLER beim Laden der EU-Benchmark: {e}")
        return None

def get_perf_yf(ticker, name):
    """yfinance-Äquivalent zu get_perf() für die STOXX-Europe-600-Sektor-ETFs,
    da diese nicht über Alpaca verfügbar sind. Gleiche Kennzahlen/Formel wie US-Version."""
    try:
        hist = get_yf_history(ticker)
        if not hist.empty:
            stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
            if getattr(hist.index, 'tz', None) is not None:
                stichtag = stichtag.tz_localize(hist.index.tz)
            hist = hist[hist.index >= stichtag]

        if hist.empty:
            return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

        # NaN-Platzhalterzeilen entfernen (siehe get_index_benchmark_yf)
        hist = hist.dropna(subset=['Close'])

        if hist.empty:
            return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

        close = hist['Close']
        last = close.iloc[-1]

        def p(d):
            if len(close) > d:
                return round(((last / close.iloc[-d]) - 1) * 100, 2)
            return 0

        current_year = datetime.datetime.now().year
        ytd_data = close[close.index.year == current_year]
        ytd_perf = round(((last / ytd_data.iloc[0]) - 1) * 100, 2) if not ytd_data.empty else 0

        res = {
            "Ticker": ticker, "Sektor": name, "5T": p(5), "12T": p(12), "30T": p(30), "60T": p(60), "YTD": ytd_perf
        }
        res["Rotation-Score"] = round((res["5T"] * 0.7 + res["12T"] * 0.3), 3)
        return res

    except Exception as e:
        print(f"FEHLER bei EU-Performance-Berechnung für {ticker}: {e}")
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

def get_perf(ticker, name):
    try:
        # Zeitraum für 1 Jahr (ca. 260 Handelstage reichen für 60T Performance)
        start_date = datetime.datetime.now() - datetime.timedelta(days=365)
        
        request = StockBarsRequest(
            symbol_or_symbols=[ticker],
            start=start_date,
            timeframe=TimeFrame.Day
        )
        
        bars = alpaca_client.get_stock_bars(request)
        hist = bars.df
        
        if hist.empty:
            return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}
        
        # Index bereinigen und sicherstellen, dass 'close' vorhanden ist
        hist = hist.reset_index(level=0, drop=True)
        if 'close' in hist.columns:
            hist = hist.rename(columns={'close': 'Close'})
            
        close = hist['Close']
        last = close.iloc[-1]
        
        # Hilfsfunktion für prozentuale Performance
        # Sicherstellen, dass wir nicht über das Ende hinaus greifen
        def p(d): 
            if len(close) > d:
                return round(((last / close.iloc[-d]) - 1) * 100, 2)
            return 0
        
        # YTD Performance berechnen
        current_year = datetime.datetime.now().year
        # Wir nutzen den Index (Timestamp) um YTD zu filtern
        ytd_data = close[hist.index.year == current_year]
        ytd_perf = round(((last / ytd_data.iloc[0]) - 1) * 100, 2) if not ytd_data.empty else 0
            
        res = {
            "Ticker": ticker, "Sektor": name, "5T": p(5), "12T": p(12), "30T": p(30), "60T": p(60), "YTD": ytd_perf
        }
        res["Rotation-Score"] = round((res["5T"] * 0.7 + res["12T"] * 0.3), 3)
        return res
        
    except Exception as e:
        print(f"FEHLER bei Performance-Berechnung für {ticker}: {e}")
        return {"Ticker": ticker, "Sektor": name, "5T": 0, "12T": 0, "30T": 0, "60T": 0, "YTD": 0, "Rotation-Score": 0}

def calculate_retest_entry(hist, breakout_level):
    close = hist['Close']
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema100 = close.ewm(span=100, adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
    
    primary = [val for val in [ema20, ema50] if val < breakout_level]
    secondary = [val for val in [ema100, ema200] if val < breakout_level]
    
    if primary: return round(max(primary), 2), "Re-Test"
    if secondary: return round(max(secondary), 2), "Re-Test"
    return round(breakout_level * 0.98, 2), "Ausbruch"

def check_bullish_confirmation(df):
    """Prüft die letzte Kerze auf bullische Umkehr mit erhöhter Sicherheit."""
    # Wir brauchen mindestens 3 Kerzen, um die Dynamik (bärisch -> bullisch) zu sehen
    if len(df) < 3: return None
    
    last = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    body = abs(last['Close'] - last['Open'])
    lower_wick = min(last['Open'], last['Close']) - last['Low']
    upper_wick = last['High'] - max(last['Open'], last['Close'])
    
    # 1. HAMMER-Check (klassisch)
    if lower_wick > (2 * body) and upper_wick < body:
        return "Hammer"
        
    # 2. ERWEITERTES BULLISH ENGULFING: 
    # Vorletzte Kerze war rot (bärisch), letzte ist grün (bullisch) und umschließt den Körper
    is_prev_bearish = prev['Close'] < prev['Open']
    is_last_bullish = last['Close'] > last['Open']
    engulfs = last['Close'] > prev['Open'] and last['Open'] < prev['Close']
    
    if is_prev_bearish and is_last_bullish and engulfs:
        return "Engulfing"
        
    return None

def check_rsi_divergence(data):
    """Prüft auf RSI-Divergenz in den letzten 40 Tagen."""
    # Wir schauen auf die letzten 40 Tage für die Minima/Maxima
    df = data.tail(40)
    
    # Lokale Extrema finden (order=5 bedeutet: min 5 Kerzen Abstand für einen Peak)
    ilocs_min = argrelextrema(df['Close'].values, np.less_equal, order=5)[0]
    ilocs_max = argrelextrema(df['Close'].values, np.greater_equal, order=5)[0]
    
    # Brauchen mindestens 2 Punkte für einen Vergleich
    if len(ilocs_min) < 2 or len(ilocs_max) < 2:
        return None

    # Bullische Divergenz (Preis tiefer, RSI höher)
    if (df['Close'].iloc[ilocs_min[-1]] < df['Close'].iloc[ilocs_min[-2]]) and \
       (df['RSI'].iloc[ilocs_min[-1]] > df['RSI'].iloc[ilocs_min[-2]]):
        return "Bullisch"
        
    # Bärische Divergenz (Preis höher, RSI tiefer)
    if (df['Close'].iloc[ilocs_max[-1]] > df['Close'].iloc[ilocs_max[-2]]) and \
       (df['RSI'].iloc[ilocs_max[-1]] < df['RSI'].iloc[ilocs_max[-2]]):
        return "Bärisch"
        
    return None

def _robuste_trendlinie(x, y, max_iterationen=2, ausreisser_schwelle=2.5):
    """Passt eine Ausgleichsgerade an und entfernt dabei iterativ Ausreißer
    (Punkte mit ungewöhnlich großem Residuum), bevor neu angepasst wird -
    eine einfache, nachvollziehbare Alternative zu RANSAC (kein sklearn in
    den Abhängigkeiten dieses Projekts). BUGFIX 05.08.2026 (externe
    Code-Review, Nutzerwunsch): eine einzelne Ausreißer-Kerze konnte die
    Regression bisher spürbar verzerren - z.B. zieht eine Serie wie
    100,98,97,96,85(Ausreißer),95,94 die Ausgleichsgerade klar nach unten,
    obwohl ein Chart-Techniker die Linie über die relevanten Hochs legen
    würde, nicht über den Ausreißer. Schwelle: 2.5x der mittlere absolute
    Residualwert (Median-basiert, dadurch selbst robust gegen die
    Ausreißer, die erkannt werden sollen).
    Gibt (slope, intercept, anzahl_verwendeter_punkte) zurück."""
    x_arr, y_arr = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    for _ in range(max_iterationen):
        if len(x_arr) < 3:
            break
        slope, intercept = np.polyfit(x_arr, y_arr, 1)
        residuen = np.abs(y_arr - (slope * x_arr + intercept))
        mad = np.median(residuen)
        if mad <= 0:
            break  # perfekte Passung oder nur noch identische Residuen - fertig
        maske = residuen <= (ausreisser_schwelle * mad)
        if maske.all() or int(maske.sum()) < 3:
            break  # keine Ausreißer mehr, oder Entfernen wuerde unter 3 Punkte druecken
        x_arr, y_arr = x_arr[maske], y_arr[maske]
    slope, intercept = np.polyfit(x_arr, y_arr, 1)
    return slope, intercept, len(x_arr)


def _kein_rueckfall_seit_ausbruch(closes, linie_werte, heute_pos, fenster_tage=3):
    """Findet den juengsten Tag innerhalb der letzten `fenster_tage`, an dem
    der Schlusskurs von unter auf ueber die Linie gewechselt ist, und prueft,
    dass der Kurs SEIT DIESEM TAG (inklusive) an JEDEM Tag ueber der Linie
    geschlossen hat. BUGFIX 05.08.2026 (externe Code-Review): die alte
    Pruefung fragte nur "war irgendein Tag der letzten 3 unter der Linie?" -
    das liess ein Wipsaw-Muster durch (Tag-3 unter, Tag-2 ueber, Tag-1
    wieder unter, heute ueber) faelschlich als gueltigen Ausbruch gelten,
    obwohl der Kurs zwischenzeitlich erneut unter die Linie gefallen war.
    `closes` und `linie_werte` sind gleich lange, index-synchrone Arrays
    (Schlusskurs bzw. Linienwert an jeder Position)."""
    for i in range(1, fenster_tage + 1):
        pos = heute_pos - i
        pos_davor = pos - 1
        if pos_davor < 0:
            break
        if closes[pos_davor] <= linie_werte[pos_davor] and closes[pos] > linie_werte[pos]:
            seitdem_close = closes[pos:heute_pos + 1]
            seitdem_linie = linie_werte[pos:heute_pos + 1]
            return bool(np.all(np.asarray(seitdem_close) > np.asarray(seitdem_linie)))
    return False


def check_trendline_breakout(data, lookback=120, order=5, touch_tolerance=0.01, require_volume=True):
    """
    Sucht eine fallende Widerstands-Trendlinie durch mindestens 3 Swing-Highs
    (Toleranz: 1% Abstand zur Linie) in den letzten `lookback` Handelstagen
    und prüft, ob der Kurs innerhalb der letzten 3 Kerzen mit über-
    durchschnittlichem Volumen darüber ausgebrochen ist UND seitdem nicht
    wieder darunter gefallen ist (BUGFIX 05.08.2026, siehe
    _kein_rueckfall_seit_ausbruch). Die Trendlinie selbst wird ausreißer-
    robust angepasst (siehe _robuste_trendlinie, BUGFIX 05.08.2026).
    Nur Long-Ausbrüche (fallende Linie nach oben durchbrochen) - ein Bruch
    einer STEIGENDEN Linie nach unten wird bewusst nicht erfasst, da die
    Strategie ausschließlich Long-Setups handelt.
    Gibt (ausbruch: bool, linien_level_heute: float|None) zurück.
    """
    fenster = data.iloc[-lookback:] if len(data) > lookback else data.copy()
    if len(fenster) < 10:
        return False, None

    # Ausbruchskerzen selbst (letzte 3) von der Linienbildung ausschließen,
    # damit die Linie nicht durch den möglichen Ausbruch selbst verzerrt wird
    suchbereich = fenster.iloc[:-3]
    if len(suchbereich) < 10:
        return False, None

    highs = suchbereich['High'].values
    idx_swings = argrelextrema(highs, np.greater_equal, order=order)[0]

    if len(idx_swings) < 3:
        return False, None

    x = idx_swings.astype(float)
    y = highs[idx_swings]
    slope, intercept, verwendete_punkte = _robuste_trendlinie(x, y)

    # Nur fallende Trendlinien relevant (Ausbruch nach oben = Long-Signal)
    if slope >= 0:
        return False, None
    if verwendete_punkte < 3:
        return False, None

    # Berührungspunkte innerhalb der Toleranz zählen (mind. 3 gefordert) -
    # gegen ALLE urspruenglichen Swing-Highs, nicht nur die nach Ausreißer-
    # Filterung verbliebenen (die Linie soll trotzdem an mindestens 3 echten
    # Punkten "anliegen", nur die Anpassung selbst ist ausreißer-robust)
    linie_bei_punkten = slope * x + intercept
    beruehrungen = int(np.sum(np.abs(y - linie_bei_punkten) <= (linie_bei_punkten * touch_tolerance)))
    if beruehrungen < 3:
        return False, None

    # Linie bis heute projizieren und Ausbruch pruefen
    heute_pos = len(fenster) - 1
    linie_heute = slope * heute_pos + intercept
    close_heute = fenster['Close'].iloc[-1]

    alle_positionen = np.arange(len(fenster))
    linie_werte_alle = slope * alle_positionen + intercept
    kein_rueckfall = _kein_rueckfall_seit_ausbruch(
        fenster['Close'].values, linie_werte_alle, heute_pos)

    if require_volume:
        volumen_ok = any(
            fenster['Volume'].iloc[-1 - i] > fenster['Vol_SMA20'].iloc[-1 - i]
            for i in range(0, 3)
        )
    else:
        # Edelmetalle: kein belastbares Handelsvolumen als Pflichtbedingung.
        volumen_ok = True

    ausbruch = bool(close_heute > linie_heute) and kein_rueckfall and bool(volumen_ok)
    return ausbruch, (float(linie_heute) if ausbruch else None)

def check_kumo_breakout(data, toleranz_prozent=0.2, require_volume=True):
    """
    Prüft einen echten Kumo-Ausbruch (Ichimoku-Wolke): Der Kurs muss die
    KOMPLETTE Wolke von unten nach oben durchbrochen haben - also über BEIDEN
    Grenzen (Senkou Span A und B) stehen, nicht nur über einer (sonst befindet
    sich der Kurs noch innerhalb der Wolke, kein echter Ausbruch).
    Der Ausbruch selbst darf innerhalb der letzten 3 Tage liegen (analog zum
    Crossover-Fenster bei EMA-Breakout/Trendlinie), aktuell muss der Kurs
    weiterhin oberhalb stehen. Pflicht-Volumen an einem der letzten 3 Tage
    (nicht zwingend heute - der Ausbruchstag mit Volumen-Spike kann auch
    1-2 Tage zurückliegen, während der Kurs seitdem über der Wolke hält).
    TOLERANZBAND (NEU 05.08.2026, externe Code-Review, Nutzerwunsch): die
    reine Ungleichung "Close > obere Wolke" reagierte bisher schon auf einen
    hauchduennen Vorsprung (0,05% ueber der Wolke reichte fuer ein Signal) -
    das ist bei Tagesschluss-Rundungsrauschen kein belastbarer Ausbruch.
    `toleranz_prozent` verlangt jetzt mindestens 0,2% Abstand ueber der
    oberen Wolkengrenze, macht den Scanner also etwas robuster gegen
    hauchduenne Scheinausbrueche.
    Gibt (ausbruch: bool, wolken_obergrenze_heute: float|None) zurück.
    """
    if len(data) < 5 or 'SenkouA' not in data.columns or 'SenkouB' not in data.columns:
        return False, None

    kumo_ober = data[['SenkouA', 'SenkouB']].max(axis=1)
    heute_ober = kumo_ober.iloc[-1]
    close_heute = data['Close'].iloc[-1]

    if pd.isna(heute_ober):
        return False, None

    ueber_wolke_heute = close_heute > heute_ober * (1 + toleranz_prozent / 100)
    if not ueber_wolke_heute:
        return False, None

    # War innerhalb der letzten 3 Tage noch NICHT (vollständig) über der Wolke
    # - frischer Ausbruch, kein bereits seit langem etabliertes "über der Wolke"
    frischer_ausbruch = any(
        pd.notna(kumo_ober.iloc[-1 - i]) and data['Close'].iloc[-1 - i] <= kumo_ober.iloc[-1 - i]
        for i in range(1, 4)
    )

    if require_volume:
        volumen_ok = any(
            data['Volume'].iloc[-1 - i] > data['Vol_SMA20'].iloc[-1 - i]
            for i in range(0, 3)
        )
    else:
        # Edelmetalle: kein belastbares Handelsvolumen als Pflichtbedingung.
        volumen_ok = True

    ausbruch = bool(ueber_wolke_heute) and frischer_ausbruch and bool(volumen_ok)
    return ausbruch, (float(heute_ober) if ausbruch else None)


def check_kijun_breakout(data, frische_tage=3):
    """NEU (24.07.2026): leichteres, frueheres Ichimoku-Bestaetigungssignal
    fuer trendwende_scanner.py - Bruch ueber die Kijun-sen (Basislinie,
    26-Perioden-Mittelpunkt aus Hoch/Tief, NICHT in die Zukunft verschoben)
    statt des vollen Kumo-Ausbruchs (check_kumo_breakout oben). Grund: bei
    stark gefallenen Titeln haengt die komplette Wolke (v.a. Senkou B, auf
    52-Perioden-Basis) noch lange auf dem alten, hohen Kursniveau vor dem
    Absturz - ein Titel, der noch nahe seinem 52-Wochen-Tief steht, hat die
    komplette Wolke so gut wie nie schon durchbrochen (siehe Log-Auswertung
    24.07.2026: 0 von 106 Kandidaten ueber mehrere Tage). Die Kijun-sen
    reagiert deutlich schneller und ist als "erstes Anzeichen einer
    Trendwende" chart-technisch passender als der volle Wolken-Ausbruch, der
    eher eine bereits etablierte Erholung bestaetigt. Gleiche Logik wie
    check_kumo_breakout ansonsten (frischer Ausbruch innerhalb frische_tage
    Tagen, Pflicht-Volumen), nur mit der Kijun-sen statt der Wolken-
    Obergrenze als Schwelle. GEAENDERT (28.07.2026, Nutzerwunsch): Fenster
    jetzt als Parameter statt hartcodierter 3 Tage - trendwende_scanner.py
    ruft dies mit frische_tage=FRISCHE_TAGE (=5) auf, um die staendigen
    0-Kandidaten-Tage zu adressieren (die urspruengliche 3-Tage-Kombination
    aus RSI-Divergenz + Kijun-Ausbruch war zu selten gleichzeitig erfuellt).
    Standardwert bleibt 3 fuer Abwaertskompatibilitaet, falls die Funktion
    andernorts ohne expliziten Parameter aufgerufen wird.
    Gibt (ausbruch: bool, kijun_heute: float|None) zurueck.
    """
    if len(data) < 5 or 'Kijun' not in data.columns:
        return False, None

    kijun = data['Kijun']
    kijun_heute = kijun.iloc[-1]
    close_heute = data['Close'].iloc[-1]

    if pd.isna(kijun_heute):
        return False, None

    ueber_kijun_heute = close_heute > kijun_heute
    if not ueber_kijun_heute:
        return False, None

    frischer_ausbruch = any(
        pd.notna(kijun.iloc[-1 - i]) and data['Close'].iloc[-1 - i] <= kijun.iloc[-1 - i]
        for i in range(1, frische_tage + 1)
    )

    volumen_ok = any(
        data['Volume'].iloc[-1 - i] > data['Vol_SMA20'].iloc[-1 - i]
        for i in range(0, 3)
    )

    ausbruch = bool(ueber_kijun_heute) and frischer_ausbruch and bool(volumen_ok)
    return ausbruch, (float(kijun_heute) if ausbruch else None)


def get_fib_levels(data, lookback=60, order=5):
    """Berechnet die 0.618 und 1.618 Extension Level basierend auf dem
    letzten BESTAETIGTEN Swing-Tief und Swing-Hoch (BUGFIX 05.08.2026,
    externe Code-Review, Nutzerwunsch).

    Vorher: reines High/Low ueber ein starres 60-Tage-Fenster. Problem
    (Review-Beispiel): Trend A, dazwischen eine Seitwaertsphase, dann
    Trend B - dann koennen Hoch und Tief aus zwei voellig unabhaengigen
    Bewegungen stammen, deren Spanne charttechnisch keine sinnvolle
    Fibonacci-Basis ist. Jetzt: argrelextrema (dieselbe Swing-Erkennung wie
    bei der Trendlinien-Funktion) findet echte lokale Hoch-/Tiefpunkte im
    Fenster, verwendet werden das JUENGSTE bestaetigte Swing-Tief und das
    JUENGSTE bestaetigte Swing-Hoch - das nähert die "letzte abgeschlossene
    Bewegung" an, wie es institutionelle Chart-Technik nutzt.
    FALLBACK: liefert das Fenster zu wenige Swing-Punkte (sehr ruhiger,
    trendloser Kursverlauf ohne klare Zacken), faellt die Funktion auf das
    alte, robuste Verhalten zurueck (rohes Hoch/Tief des Fensters) - lieber
    ein grobes Ergebnis als gar keins."""
    recent_data = data.iloc[-lookback:]
    highs = recent_data['High'].values
    lows = recent_data['Low'].values

    idx_swing_hochs = argrelextrema(highs, np.greater_equal, order=order)[0]
    idx_swing_tiefs = argrelextrema(lows, np.less_equal, order=order)[0]

    if len(idx_swing_hochs) > 0 and len(idx_swing_tiefs) > 0:
        swing_high = float(highs[idx_swing_hochs[-1]])
        swing_low = float(lows[idx_swing_tiefs[-1]])
    else:
        # Fallback: altes Verhalten (rohes Fenster-Hoch/-Tief)
        swing_high = float(recent_data['High'].max())
        swing_low = float(recent_data['Low'].min())

    span = swing_high - swing_low

    # Extension-Level für techn. Kursziele (über dem aktuellen Kurs)
    fib_0618 = swing_low + (span * 1.618)
    fib_1000 = swing_low + (span * 2.0)

    return fib_0618, fib_1000

def clean_num(val, default=0.0):
    # Alles hier drunter muss um 4 Leerzeichen eingerückt sein!
    try:
        if val is None:
            return None
        return float(val)
    except Exception as e:
        print(f"DEBUG: Konvertierungsfehler bei Wert: {val} | Fehler: {e}")
        return default

def _begrenze_tp2_realitaetsdeckel(tp1, tp2, entry, data):
    """Begrenzt TP2 auf das hoechste tatsaechlich erreichte Kursniveau
    der letzten 250 Handelstage.

    Der Deckel ist ein oberes Limit. Wenn TP2 dadurch auf TP1 faellt,
    wird TP2 nicht kuenstlich wieder um 5% angehoben; ein solches Niveau
    liefert kein zusaetzliches hoeheres Ziel.
    """
    try:
        deckel_250 = float(data['High'].iloc[-250:].max())
        if deckel_250 > entry:
            tp2 = min(float(tp2), deckel_250)
        return max(float(tp1), float(tp2))
    except Exception as e:
        print(f"DEBUG: TP2-Realitaetsdeckel nicht berechenbar ({e}) - verwende bisheriges TP2.")
        return float(tp2)

def get_golden_cross_status(data, tage=10):
    """NEU (21.07.2026): rein informativer Kommentar, KEIN Filter- oder
    Bewertungskriterium - taucht nur als Zusatzinfo im Briefing auf, hat
    keinerlei Einfluss auf Setup-Erkennung oder Setup-Qualität. Prüft, ob
    EMA50 die EMA200 innerhalb der letzten `tage` Handelstage gekreuzt hat:
    Golden Cross (EMA50 von unten nach oben, klassisch positiv gedeutet)
    oder Death Cross (EMA50 von oben nach unten, klassisch negativ gedeutet).
    Kein frischer Cross -> aktuelle Struktur (EMA50 über/unter EMA200) als
    schwächere Zusatzinfo."""
    if len(data) < 210 or 'EMA50' not in data.columns or 'EMA200' not in data.columns:
        return "N/A (zu wenig Kurshistorie)"
    ema50, ema200 = data['EMA50'], data['EMA200']
    for i in range(0, tage + 1):
        idx, idx_prev = -1 - i, -2 - i
        if abs(idx_prev) > len(data):
            break
        if pd.isna(ema50.iloc[idx_prev]) or pd.isna(ema200.iloc[idx_prev]):
            continue
        if ema50.iloc[idx] > ema200.iloc[idx] and ema50.iloc[idx_prev] <= ema200.iloc[idx_prev]:
            return f"GOLDEN CROSS vor {i} Handelstag(en) (EMA50 kreuzt EMA200 nach oben)"
        if ema50.iloc[idx] < ema200.iloc[idx] and ema50.iloc[idx_prev] >= ema200.iloc[idx_prev]:
            return f"DEATH CROSS vor {i} Handelstag(en) (EMA50 kreuzt EMA200 nach unten)"
    if ema50.iloc[-1] > ema200.iloc[-1]:
        return "Kein frischer Cross (EMA50 > EMA200, langfristig bullische Struktur)"
    return "Kein frischer Cross (EMA50 < EMA200, langfristig bärische Struktur)"


_sektor_kgv_cache = {}  # NEU (23.07.2026): Cache pro Skriptlauf (Key: (Markt, Sektor)) -
                         # verhindert, dass derselbe Sektor mehrfach abgefragt wird, nur
                         # weil mehrere validierte Setups im selben Sektor liegen. Wird bei
                         # jedem Skriptstart neu geleert (kein persistenter Cache).

_sektoren_map_rev = {name: etf for etf, name in sektoren_map.items()}  # Sektor-Name -> ETF-Ticker


def _sektor_median_kgv(sektor, markt, eigener_ticker):
    """NEU (23.07.2026): ermittelt den Median-KGV der Sektor-Peers (aus den
    bereits vorhandenen sektoren_aktien/dax_aktien-Listen - KEINE zusaetzliche
    Ticker-Recherche noetig) fuer einen fairen, sektor-relativen Vergleich statt
    einer pauschalen 15/30-Grenze (Halbleiter und Minen/Metalle haben strukturell
    unterschiedliche KGV-Niveaus). API-schonend durch zwei Massnahmen: (1) Cache
    pro Sektor - wird ein Sektor schon im selben Lauf abgefragt, kommt das
    Ergebnis aus dem Cache, kein erneuter Fetch; (2) einzelne fehlgeschlagene
    Peer-Abfragen brechen die Berechnung nicht ab, sie werden einfach
    uebersprungen. Gibt None zurueck, wenn kein Sektor bekannt ist oder zu wenige
    Peers auswertbar sind (< 3) - Aufrufer faellt dann auf die alte feste
    15/30-Grenze zurueck (Sicherheitsnetz, kein Abbruch)."""
    cache_key = (markt, sektor)
    if cache_key in _sektor_kgv_cache:
        return _sektor_kgv_cache[cache_key]

    if markt == "EU":
        peers = dax_aktien.get(sektor, [])
    else:
        peers = sektoren_aktien.get(_sektoren_map_rev.get(sektor), [])

    kgv_werte = []
    for peer in peers:
        if peer == eigener_ticker:
            continue  # der zu bewertende Titel selbst zaehlt nicht als eigener Peer
        try:
            peer_kgv = yf.Ticker(peer).info.get("trailingPE")
            if peer_kgv and peer_kgv > 0:
                kgv_werte.append(peer_kgv)
        except Exception:
            continue  # einzelner Peer-Fehler soll den Sektor-Median nicht verhindern

    median = round(pd.Series(kgv_werte).median(), 1) if len(kgv_werte) >= 3 else None
    _sektor_kgv_cache[cache_key] = median
    return median


def berechne_fundamental_ampel(ticker, sektor=None, markt=None, richtung="long"):
    """GEAENDERT (23.07.2026): KGV wird jetzt relativ zum Sektor-Median bewertet
    statt an einer pauschalen 15/30-Grenze - Halbleiter/Software (strukturell
    hohe KGVs) und Minen/Banken (strukturell niedrige KGVs) waren bei der alten
    festen Grenze nicht fair vergleichbar (z.B. Broadcom vs. Freeport-McMoRan).
    Ohne sektor/markt-Angabe (oder falls kein Sektor-Median ermittelbar, siehe
    _sektor_median_kgv) faellt die Funktion auf die alte feste 15/30-Grenze
    zurueck - reines Sicherheitsnetz, kein Funktionsverlust.
    Wird nur für die bereits gefilterte, kleine Setup-Liste aufgerufen
    (nicht für das ganze ~370er-Universum) - hält die zusätzliche API-Last
    gering.

    GEAENDERT (24.07.2026): neuer Parameter `richtung` ("long"/"short").
    Bei richtung="long" (Standard, unveraendertes Verhalten): GUENSTIG =
    KGV < 80% des Sektor-Median (Kaufargument), TEUER = KGV > 130% des
    Sektor-Median (Warnsignal), dazwischen NEUTRAL.
    Bei richtung="short" ist die Bedeutung UMGEKEHRT: ein bereits guenstig
    bewertetes Papier hat fundamental WENIGER Abwaertspotenzial (spricht
    GEGEN den Short), waehrend eine teure Bewertung die Short-These STUETZT
    (Korrekturpotenzial nach unten). Ohne diese Umkehr laese sich z.B. ein
    A+-Short-Setup mit der Ampel GUENSTIG faelschlich wie ein Kaufargument
    neben der Short-Einstufung."""
    try:
        info = yf.Ticker(ticker).info
        kgv = info.get("trailingPE")
        if kgv is None or kgv <= 0:
            return "N/A", "Kein KGV verfügbar (z. B. Verlust-Unternehmen) - keine Bewertungsaussage möglich."

        sektor_median = _sektor_median_kgv(sektor, markt, ticker) if sektor and markt else None

        if richtung == "short":
            if sektor_median is None:
                if kgv < 15:
                    return "GEGEN_SHORT", f"KGV {round(kgv, 1)} - unterhalb der groben 15er-Hausnummer (kein Sektor-Vergleich möglich), spricht fundamental gegen die Short-These."
                elif kgv > 30:
                    return "STUETZT_SHORT", f"KGV {round(kgv, 1)} - oberhalb der groben 30er-Hausnummer (kein Sektor-Vergleich möglich), stützt fundamental die Short-These."
                else:
                    return "NEUTRAL", f"KGV {round(kgv, 1)} - im üblichen Rahmen (kein Sektor-Vergleich möglich)."

            rel = kgv / sektor_median
            if rel < 0.8:
                return "GEGEN_SHORT", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - {round((1 - rel) * 100)}% günstiger als der Sektor, spricht fundamental gegen die Short-These."
            elif rel > 1.3:
                return "STUETZT_SHORT", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - {round((rel - 1) * 100)}% teurer als der Sektor, stützt fundamental die Short-These."
            else:
                return "NEUTRAL", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - im üblichen Rahmen für den Sektor."

        # richtung == "long" (Standard, unveraendertes Verhalten)
        if sektor_median is None:
            if kgv < 15:
                return "GUENSTIG", f"KGV {round(kgv, 1)} - unterhalb der groben 15er-Hausnummer (kein Sektor-Vergleich möglich)."
            elif kgv > 30:
                return "TEUER", f"KGV {round(kgv, 1)} - oberhalb der groben 30er-Hausnummer (kein Sektor-Vergleich möglich)."
            else:
                return "NEUTRAL", f"KGV {round(kgv, 1)} - im üblichen Rahmen (kein Sektor-Vergleich möglich)."

        rel = kgv / sektor_median
        if rel < 0.8:
            return "GUENSTIG", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - {round((1 - rel) * 100)}% günstiger als der Sektor."
        elif rel > 1.3:
            return "TEUER", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - {round((rel - 1) * 100)}% teurer als der Sektor."
        else:
            return "NEUTRAL", f"KGV {round(kgv, 1)} vs. Sektor-Median {sektor_median} ({sektor}) - im üblichen Rahmen für den Sektor."
    except Exception as e:
        print(f"DEBUG: Fundamental-Ampel für {ticker} nicht verfügbar ({e}).")
        return "N/A", "Fundamentaldaten aktuell nicht abrufbar."



def get_ideal_delta(upside_prozent):
    # Einfache Heuristik:
    # Bei kleinem Upside brauchen wir hohes Delta für direkte Reaktion
    # Bei großem Upside reicht moderates Delta für Hebel
    if upside_prozent < 5:
        return 0.70  # Aggressiv, tief im Geld
    elif upside_prozent < 15:
        return 0.55  # Der "Sweet Spot"
    else:
        return 0.40  # Mehr Hebel, weniger Delta-Risiko

CHUNK_SIZE_US = 100


def _chunks(liste, groesse):
    for i in range(0, len(liste), groesse):
        yield liste[i:i + groesse]


def fetch_us_batch(ticker_liste):
    """Robuster US-Sammelabruf: ein ungültiges Symbol darf keinen ganzen Chunk verlieren."""
    return fetch_us_batch_robust(
        alpaca_client,
        ticker_liste,
        chunk_size=CHUNK_SIZE_US if 'CHUNK_SIZE_US' in globals() else CHUNK_SIZE,
        days=365,
    )

def fetch_eu_batch(ticker_liste):
    """NEU (09.08.2026, dieselbe Begruendung wie fetch_us_batch oben, hier
    fuer die EU-Seite): holt Kursdaten fuer ALLE EU-Ticker in wenigen
    Sammel-Requests statt einem Request pro Ticker (yf.download akzeptiert
    mehrere Ticker auf einmal) - trendwende_scanner.py nutzt das bereits,
    analyze_a_setup_eu bisher NICHT. Eigenstaendig nachgebaut (Zirkelimport-
    Grund wie bei fetch_us_batch). Gibt {ticker: DataFrame} zurueck."""
    ergebnis = {}

    for chunk in _chunks(ticker_liste, CHUNK_SIZE_US):
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


def analyze_a_setup(ticker, sektor, spy_close=None, data=None):
    upside_potenzial = None
    # Firmennamen abrufen (mit Retry, siehe _hole_firma_name)
    firma_name = _hole_firma_name(ticker)

    # 0. Initialisierung
    setup_typ = "Kein"
    pattern = "Kein"
    tp1 = 0

    # Start des Haupt-Blocks
    try:
        if data is None:
            # Fallback: Einzelabruf (GEAENDERT 09.08.2026 - der Normalfall
            # im Tageslauf uebergibt jetzt vorab gebuendelt geholte Daten
            # ueber fetch_us_batch, siehe dortige Begruendung. Dieser Zweig
            # greift nur noch, wenn KEIN data uebergeben wurde - z.B. bei
            # einzel_check.py's Ad-hoc-Einzeltickerpruefung, wo ein
            # Sammel-Abruf fuer nur einen Ticker keinen Sinn ergibt).
            start_date = datetime.datetime.now() - datetime.timedelta(days=365)
            request = StockBarsRequest(
                symbol_or_symbols=[ticker],
                start=start_date,
                timeframe=TimeFrame.Day
            )
            bars = alpaca_client.get_stock_bars(request)
            data = bars.df

            if data.empty:
                print(f"DEBUG: {ticker} -> Daten von Alpaca leer.")
                funnel_zaehle("keine_kursdaten")
                return None

            # Index und Spalten bereinigen (fetch_us_batch liefert das schon
            # in dieser Form, deshalb nur im Fallback-Zweig noetig)
            data = data.reset_index(level=0, drop=True)
            if 'close' in data.columns:
                data = data.rename(columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'open': 'Open', 'volume': 'Volume'})
        elif data.empty:
            print(f"DEBUG: {ticker} -> Daten von Alpaca leer.")
            funnel_zaehle("keine_kursdaten")
            return None
       
        # Vor der Berechnung des RSI:
        if len(data) < 15: # Puffer für 14 Perioden + 1
            print(f"Zu wenig Daten für {ticker}: {len(data)} Zeilen")
            funnel_zaehle("zu_wenig_daten")
            return None
            
        # RSI Berechnung
        delta = data['Close'].diff()
        # GEAENDERT (05.08.2026): Wilder's Glaettung statt einfachem
        # rollierendem Mittel - Konsistenz mit get_safe_rsi() weiter oben,
        # siehe dortige Begruendung.
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        # GEAENDERT (05.08.2026, siehe get_safe_rsi fuer die Begruendung)
        rs = gain / loss.clip(lower=1e-9)
        data['RSI'] = 100 - (100 / (1 + rs))
        data['RSI'] = data['RSI'].fillna(50)
        divergenz = check_rsi_divergence(data)

        # 1. Indikatoren berechnen
        data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
        data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
        data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean()
        data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean()
        data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
        data['WMA200'] = data['Close'].rolling(200).apply(lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True)
        data['Vol_SMA20'] = data['Volume'].rolling(20).mean()

        # Ichimoku-Basiswerte (NEU): Tenkan-sen/Kijun-sen als Hoch-Tief-
        # Mittelpunkte (andere Berechnungsgrundlage als die EMAs oben), Senkou
        # Span A/B als projizierte Kumo-Grenzen. Werden unten als zusätzliche
        # TP-Kandidaten (Kumo) bzw. zusätzliches Pullback-Level (Kijun-sen)
        # genutzt - erscheinen NICHT als eigene Briefing-Felder, sondern
        # fließen nur in die bestehenden TP1/TP2/Setup-Typ-Werte mit ein.
        data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
        data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
        data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)  # wie im Chart: 26 Perioden Vorlauf
        data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

        entry = data['Close'].iloc[-1]
        stop = data['Low'].rolling(10).min().iloc[-1]

        # --- NEU: Stochastik & Marktstruktur ---
        # Stochastik (14,3,3)
        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))
        data['Stoch_D'] = data['Stoch_K'].rolling(3).mean()
        
        # Marktstruktur (einfacher Higher-Low Check: Low[last] > Low[prev])
        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        
        # Danach direkt prüfen:
        if 'RSI' not in data.columns:
            print(f"RSI-Berechnung fehlgeschlagen für {ticker}")
            funnel_zaehle("zu_wenig_daten")
            return None
        
        data['Vol_Ratio'] = data['Volume'] / data['Vol_SMA20']
        data['Vol_Ratio'] = data['Vol_Ratio'].fillna(0)

        # Hebeltrader-Kriterien 1-4 (NEU 07.08.2026) - vor jedem moeglichen
        # fruehen Abbruch, damit auch spaeter verworfene Titel erfasst werden
        _hebeltrader_teilkriterien(ticker, firma_name, sektor, "US", "USD", data, entry, stop)

        # MACD Berechnung
        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        # 2. Trend-Status
        trend_status = "Unter WMA200/EMA200" if (data['Close'].iloc[-1] < data['WMA200'].iloc[-1] or data['Close'].iloc[-1] < data['EMA200'].iloc[-1]) else "OK"

        # 3. Candlestick-Muster
        c1, c2 = data.iloc[-1], data.iloc[-2]
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        if lower_wick > (2 * body): 
            pattern = "Hammer"
        elif c1['Close'] > c1['Open'] and c2['Close'] < c2['Open'] and c1['Close'] > c2['Open'] and c1['Open'] < c2['Close']:
            pattern = "Engulfing"

        # 4. EMA-Ausbruch (Crossover darf innerhalb der letzten 3 Kerzen liegen, nicht nur gestern)
        crossover_kuerzlich = any(
            data['EMA8'].iloc[-1 - i] <= data['EMA20'].iloc[-1 - i] for i in range(1, 4)
        )
        # Volumen-Bestätigung an einem der letzten 3 Tage (nicht zwingend heute -
        # der eigentliche Ausbruchstag mit dem Volumen-Spike kann auch 1-2 Tage
        # zurückliegen, während der Kurs seitdem über der EMA20 hält)
        volumen_kuerzlich = any(
            data['Volume'].iloc[-1 - i] > data['Vol_SMA20'].iloc[-1 - i] for i in range(0, 3)
        )
        ema_breakout = (data['EMA8'].iloc[-1] > data['EMA20'].iloc[-1]) and \
                       crossover_kuerzlich and \
                       volumen_kuerzlich
       
        # --- 5. Setup-Typ mit Pro-Check Filter ---
        
        # Berechnungen für den Filter
        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        stoch_k = 100 * ((data['Close'].iloc[-1] - low_min.iloc[-1]) / (high_max.iloc[-1] - low_min.iloc[-1] + 1e-9))
        
        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        buffer = 0.01  # 1.0% Puffer für Zone (vorher 0.3%)
        
        # Prüfung: Kurs in der EMA-Zone? Richtungsabhängig (NEU): Ein reiner
        # Bruch nach unten (Kurs dauerhaft unter der EMA) zählt nicht mehr als
        # Pullback-Test, auch wenn er innerhalb der Toleranz liegt - das wäre
        # ein Unterstützungsbruch, keine Bestätigung. Es zählt nur, wenn der
        # Kurs aktuell nah an der EMA liegt UND innerhalb der letzten 3 Tage
        # mindestens einmal auf/über der EMA stand (frischer Reclaim erlaubt,
        # analog zum 3-Tage-Fenster beim EMA-Breakout).
        price = data['Close'].iloc[-1]

        def ema_pullback_test(ema_series):
            ema_heute = ema_series.iloc[-1]
            nah_dran = abs(price - ema_heute) < (price * buffer)
            war_ueber_ema_kuerzlich = any(
                data['Close'].iloc[-1 - i] >= ema_series.iloc[-1 - i] for i in range(0, 3)
            )
            return nah_dran and war_ueber_ema_kuerzlich

        in_ema_zone_roh = any(ema_pullback_test(ema_series) for ema_series in [data['EMA20'], data['EMA50'], data['Kijun']])
        # Mindest-Volumen (GEÄNDERT 27.07.2026, zweite Iteration nach Nutzer-
        # feedback): Pullback-Zone war der einzige der vier Setup-Typen ohne
        # jede Volumen-Anforderung. Eine echte Volumen-SPITZE (wie bei EMA-
        # Breakout/Trendlinie/Kumo) passt hier aber fachlich nicht - ein
        # gesunder Pullback zeichnet sich klassischerweise durch ABNEHMENDES
        # Volumen aus (kein Verkaufsdruck), die Spitze gehört zum vorherigen
        # Ausbruch, nicht zur Konsolidierung selbst. Deshalb bewusst nur ein
        # Mindest-BODEN (heutiges Vol_Ratio >= 0.7) statt einer Pflicht-Spitze -
        # schliesst nur die wirklich teilnahmslosen, duennen Tage aus, ohne
        # saubere, ruhige Pullbacks faelschlich zu verwerfen.
        volumen_ausreichend = bool(data['Vol_Ratio'].iloc[-1] >= 0.7)
        in_ema_zone = in_ema_zone_roh and volumen_ausreichend

        # Dritter, eigenständiger Setup-Typ: Ausbruch aus einer fallenden
        # Trendlinie (mind. 3 Berührungspunkte, 1% Toleranz, Pflicht-Volumen)
        trendlinien_ausbruch, tl_level = check_trendline_breakout(data)

        # Vierter, eigenständiger Setup-Typ: echter Kumo-Ausbruch (Ichimoku-
        # Wolke komplett von unten nach oben durchbrochen, Pflicht-Volumen)
        kumo_ausbruch, kumo_level = check_kumo_breakout(data)

        # 5. Setup-Typ mit Pro-Check Filter
        # --- DEBUG-LOGGING ---
        # Dieser Print zeigt dir im Log genau, warum ein Setup abgelehnt wird.
        # NEU (27.07.2026): InZone-Grund ergänzt (EMA-Naehe/Higher-Low
        # gescheitert vs. Volumen unter 0,7x) - vorher war bei InZone=False
        # nicht erkennbar, welcher der beiden Gruende zutraf.
        in_zone_grund = "OK" if in_ema_zone else ("EMA-Zone nicht erfüllt" if not in_ema_zone_roh else f"Volumen zu duenn ({data['Vol_Ratio'].iloc[-1]:.2f}x < 0.7)")
        print(f"DEBUG: {ticker} | Breakout: {ema_breakout} | InZone: {in_ema_zone} (Grund: {in_zone_grund}) | "
              f"HL: {is_higher_low} | Stoch: {stoch_k:.1f} | TL-Ausbruch: {trendlinien_ausbruch} | Kumo-Ausbruch: {kumo_ausbruch}")

        # --- Filter-Logik ---
        # 1. Der Haupt-Filter (muss mit 'if' beginnen)
        if (ema_breakout or (in_ema_zone and is_higher_low) or trendlinien_ausbruch or kumo_ausbruch) and stoch_k < 90:
            
            # Setup-Typ: ALLE zutreffenden Pfade auflisten, nicht nur den ersten
            # Treffer (sonst geht z.B. ein gleichzeitiger Kumo-Ausbruch neben
            # einem Trendlinien-Ausbruch stillschweigend verloren)
            pfade = []
            if trendlinien_ausbruch:
                pfade.append("Trendlinien-Ausbruch")
            if kumo_ausbruch:
                pfade.append("Kumo-Ausbruch")
            if ema_breakout:
                pfade.append("EMA-Breakout")
            if in_ema_zone and is_higher_low:
                pfade.append("Pullback-Zone")
            basis_label = " + ".join(pfade)
            setup_typ = basis_label  # NEU: nur die Basis-Pfade, Pattern (Hammer/Engulfing) bleibt in eigener Spalte, wird NICHT mehr an Setup_Typ angehaengt
            
        # 2. Das 'else' MUSS genau unter dem 'if' stehen (gleiche Einrückung)
        else:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Haupt-Filter nicht erfüllt (Breakout={ema_breakout}, InZone={in_ema_zone}, HL={is_higher_low}, TL-Ausbruch={trendlinien_ausbruch}, Kumo-Ausbruch={kumo_ausbruch}, Stoch={stoch_k:.1f})")
            funnel_zaehle("kein_setup_muster")
            return None

        # --- Momentum-Zusatzkriterien: Relative Stärke & 52-Wochen-Hoch-Nähe ---
        # Klassische Momentum-Bausteine (u.a. CANSLIM): Eine Aktie sollte sich
        # stärker entwickeln als der breite Markt (Relative Stärke) und in der
        # Nähe ihres 52-Wochen-Hochs notieren statt nahe am Tief.

        # Relative Stärke vs. SPY (60-Tage-Performance im Vergleich zum Index)
        rel_staerke = None
        if spy_close is not None and len(spy_close) > 60 and len(data) > 60:
            stock_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
            spy_perf_60 = ((spy_close.iloc[-1] / spy_close.iloc[-60]) - 1) * 100
            rel_staerke = round(stock_perf_60 - spy_perf_60, 2)

            if rel_staerke <= -10:
                print(f"DEBUG-VERWORFEN: {ticker} | Grund: Relative Stärke vs. SPY <= -10% ({rel_staerke}%)")
                funnel_zaehle("rel_staerke_zu_schwach")
                return None

        # 52-Wochen-Hoch-Nähe (geladene Daten decken ca. 1 Jahr ab)
        hoch_52w = data['High'].max()
        abstand_52w_hoch = round(((entry / hoch_52w) - 1) * 100, 2)

        if abstand_52w_hoch < -25:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Zu weit vom 52-Wochen-Hoch entfernt ({abstand_52w_hoch}%, Hoch={hoch_52w:.2f})")
            funnel_zaehle("zu_weit_vom_52w_hoch")
            return None

        fib1, fib2 = get_fib_levels(data)
        # Kumo-Grenzen (NEU) als zusätzliche TP-Kandidaten - NaN-sicher, falls
        # die 26-Perioden-Verschiebung noch keinen gültigen Wert liefert
        kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
        potenzial_targets = sorted([data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1], data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte)
        targets_above = [t for t in potenzial_targets if t > entry]

        tp1 = targets_above[0] if targets_above else entry * 1.08
        tp2 = targets_above[1] if len(targets_above) >= 2 else tp1 * 1.05

        # --- Setup-spezifische Stop-/Ziel-Logik (Pullback vs. Breakout) ---
        # Ein Pullback-Setup (Kurs testet EMA20/50, Higher-Low bestätigt, kein
        # Breakout) hat eine andere charttechnische Erwartung als ein Breakout:
        # Das Ziel ist der letzte Swing-High vor dem Pullback (Rückkehr zum
        # vorherigen Hoch), der Stop liegt knapp unter dem jüngsten Swing-Low
        # statt einem starren 10-Tage-Tief, das bei einem Pullback-Entry oft
        # weit über das tatsächliche Setup-Risiko hinausschießt.
        # Breakout-Setups (ema_breakout=True) sind von dieser Anpassung nicht
        # betroffen und nutzen weiterhin die ursprüngliche Stop-/TP1-Logik.
        is_pullback_setup = (not ema_breakout) and in_ema_zone and is_higher_low

        if is_pullback_setup:
            # Engerer, setup-naher Stop: Tief der letzten 5 Kerzen
            swing_low_stop = data['Low'].iloc[-5:].min()
            if swing_low_stop < entry:
                stop = swing_low_stop

            # Letzter Swing-High vor dem aktuellen Pullback (Fenster -40 bis -3,
            # damit die jüngsten Pullback-Kerzen selbst nicht als Ziel zählen)
            vorlauf = data.iloc[-40:-3]
            if not vorlauf.empty:
                swing_high_target = vorlauf['High'].max()
                if swing_high_target > entry:
                    tp1 = swing_high_target
                    hoehere_ziele = [t for t in targets_above if t > tp1]
                    tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # --- Realitäts-Deckel: TP1 darf nicht über dem höchsten tatsächlich
        # erreichten Kurs der letzten 120 Handelstage liegen. Verhindert, dass
        # eine reine Fibonacci-Extension (mathematische Projektion, kein real
        # getestetes Niveau) als Ziel genutzt wird - das passiert z.B., wenn
        # der Kurs bereits über allen EMAs notiert und nur noch fib1/fib2 als
        # TP-Kandidat übrig bleibt. Greift für Breakout- UND Pullback-Setups.
        realer_deckel_120 = data['High'].iloc[-120:].max()
        if realer_deckel_120 > entry and tp1 > realer_deckel_120:
            tp1 = realer_deckel_120
            hoehere_ziele = [t for t in targets_above if t > tp1]
            tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # --- TP2-Realitäts-Deckel: großzügigeres 250-Tage-Fenster (statt 120
        # bei TP1), da TP2 bewusst ambitionierter sein darf - aber auch hier
        # keine reine Fib-Extension ohne jemals real erreichtes Kursniveau.
        tp2 = _begrenze_tp2_realitaetsdeckel(tp1, tp2, entry, data)

        analysten_ziel = get_analyst_target(ticker)
        if analysten_ziel is None: analysten_ziel = 0.0
        
        target_value = analysten_ziel if analysten_ziel > 0 else tp1
        upside_potenzial = round(((target_value - entry) / entry) * 100, 2) if entry > 0 else 0.0

        risiko = entry - stop
        if risiko <= 0:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Risiko <= 0 (Entry={entry:.2f}, Stop={stop:.2f})")
            funnel_zaehle("risiko_ungueltig")
            return None
        
        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
        chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
        chance2_perc = round(((tp2 - entry) / entry) * 100, 2)
        if crv1 < 1.0 or crv2 < 1.0:
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: CRV zu niedrig (CRV1={crv1}, CRV2={crv2}, TP1={tp1:.2f}, TP2={tp2:.2f}, Entry={entry:.2f}, Risiko={risiko:.2f})")
            funnel_zaehle("crv_unter_1")
            funnel_beinahe(ticker, "CRV-Filter",
                           f"Kurs={entry:.2f} | TP1={tp1:.2f} | Chance1={chance1_perc:.2f}% | "
                           f"CRV1={crv1} | TP2={tp2:.2f} | Chance2={chance2_perc:.2f}% | "
                           f"CRV2={crv2} | Stop={stop:.2f} | Risiko={risiko/entry*100:.2f}%",
                           crv_sortier=min(crv1, crv2), name=firma_name)
            return None
        
        risk_perc = round(((entry - stop) / entry) * 100, 2)
        last_row = data.iloc[-1]

        # Plausibilitäts-Check
        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG-VERWORFEN: {ticker} | Grund: Plausibilitäts-Check fehlgeschlagen (EMA20={last_row['EMA20']:.2f} > 2x Close={last_row['Close']:.2f})")
            funnel_zaehle("plausibilitaet")
            return None
        
        # --- Debug-Detektiv ---
        bedingung_erfuellt = (ema_breakout or (in_ema_zone and is_higher_low) or trendlinien_ausbruch or kumo_ausbruch) and stoch_k < 90
        
        # --- Universal-Debugger ---
        # Wir geben die Werte aus, bevor das IF überhaupt startet
        print(f"DEBUG-CHECK: {ticker} | Breakout: {ema_breakout} ({type(ema_breakout)}) | Zone: {in_ema_zone} ({type(in_ema_zone)}) | HL: {is_higher_low} ({type(is_higher_low)}) | TL-Ausbruch: {trendlinien_ausbruch} | Kumo-Ausbruch: {kumo_ausbruch} | Stoch: {stoch_k} ({type(stoch_k)})")

        # Sicherstellen, dass wir echte Booleans haben
        def to_bool(v):
            if isinstance(v, bool): return v
            return str(v).lower() == 'true'

        # Konvertierung
        is_breakout = to_bool(ema_breakout)
        in_zone = to_bool(in_ema_zone)
        is_hl = to_bool(is_higher_low)
        is_tl = to_bool(trendlinien_ausbruch)
        is_kumo = to_bool(kumo_ausbruch)
        stoch = float(stoch_k)

        # Die exakte Prüfung
        if (is_breakout or (in_zone and is_hl) or is_tl or is_kumo) and stoch < 90:
            
            # Setup-Typ: ALLE zutreffenden Pfade auflisten (konsistent zur
            # Hauptprüfung weiter oben)
            pfade = []
            if is_tl:
                pfade.append("Trendlinien-Ausbruch")
            if is_kumo:
                pfade.append("Kumo-Ausbruch")
            if is_breakout:
                pfade.append("EMA-Breakout")
            if in_zone and is_hl:
                pfade.append("Pullback-Zone")
            basis_label = " + ".join(pfade)
            setup_typ = basis_label  # NEU: nur die Basis-Pfade, Pattern (Hammer/Engulfing) bleibt in eigener Spalte, wird NICHT mehr an Setup_Typ angehaengt
            
            res = {
                "Ticker": str(ticker), "Name": str(firma_name), "Sektor": str(sektor),
                "Trend": str(trend_status), "Setup_Typ": str(setup_typ), "Pattern": str(pattern),
                "Golden_Cross_Status": get_golden_cross_status(data),
                "Tech-Kursziel": clean_num(tp1), "Analysten-Kursziel": float(analysten_ziel),
                "Upside-Potenzial%": float(upside_potenzial), "Status2": "VALIDE", 
                "Status_Grund": "Alles ok", "RSI": float(last_row['RSI']),
                "Divergenz": divergenz if divergenz else "Keine",
                "MACD_Trend": str(macd_trend), "CRV1": clean_num(crv1), 
                "CRV2": clean_num(crv2), "Kurs": round(last_row['Close'], 2),
                "Chance1_Perc": clean_num(chance1_perc), "Chance2_Perc": clean_num(chance2_perc),
                "Einstieg": round(last_row['Close'], 2), "Einstieg2(EMA 20)": round(last_row['EMA20'], 2),
                "Stop": clean_num(stop), "Risk_Perc": clean_num(risk_perc),
                "TP1": clean_num(tp1), "TP2": clean_num(tp2),
                "Stoch_K": stoch, "Vol_Ratio": clean_num(last_row['Vol_Ratio']), "Ideales_Delta": 0.0,
                "RS_vs_Benchmark%": clean_num(rel_staerke) if rel_staerke is not None else None,
                "Abstand_52W_Hoch%": clean_num(abstand_52w_hoch),
                "Markt": "US", "Waehrung": "USD"
            }
            return res
        
        return None

    except Exception as e:
        print(f"Fehler bei der Analyse von {ticker}: {e}")
        funnel_zaehle("fehler")
        return None

def analyze_a_setup_eu(ticker, sektor, eu_bench_close=None, data=None):
    """EU-Variante von analyze_a_setup: identische Analyse-Logik (RSI, EMAs, MACD,
    Stochastik, Breakout/Pullback-Filter, Momentum-Kriterien, CRV, setup-spezifische
    Stop/TP-Logik), aber Kursdaten via yfinance statt Alpaca, da Alpaca DAX-Werte
    nicht abdeckt. Relative Stärke wird gegen den STOXX-Europe-600-ETF statt SPY
    berechnet, sonst laufen die Kriterien 1:1 identisch zur US-Funktion."""
    upside_potenzial = None
    firma_name = _hole_firma_name(ticker)

    setup_typ = "Kein"
    pattern = "Kein"
    tp1 = 0

    try:
        if data is None:
            # Fallback: Einzelabruf (siehe analyze_a_setup US-Version fuer
            # die volle Begruendung - greift nur bei einzel_check.py's
            # Ad-hoc-Einzeltickerpruefung, wo kein Sammel-Abruf stattfindet)
            data = get_yf_history(ticker)
            if not data.empty:
                stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
                if getattr(data.index, 'tz', None) is not None:
                    stichtag = stichtag.tz_localize(data.index.tz)
                data = data[data.index >= stichtag]

            if data.empty:
                print(f"DEBUG: {ticker} -> Daten von yfinance leer.")
                funnel_zaehle("keine_kursdaten")
                return None

            # NaN-Platzhalterzeilen entfernen: yfinance legt vor Xetra-Handelsbeginn
            # teils schon eine leere Zeile für den aktuellen Tag an (NaN in Close/High/
            # Low/Volume). Ohne diese Bereinigung würde iloc[-1] auf diese Platzhalter-
            # Zeile zeigen statt auf den letzten echten Schlusskurs, was RSI, Stochastik,
            # EMAs etc. komplett auf NaN kippen lässt (siehe Log vom 2026-07-14).
            data = data.dropna(subset=['Close', 'High', 'Low', 'Volume'])

            if data.empty:
                print(f"DEBUG: {ticker} -> Nach NaN-Bereinigung keine Daten mehr übrig.")
                funnel_zaehle("keine_kursdaten")
                return None
        elif data.empty:
            print(f"DEBUG: {ticker} -> Daten von yfinance leer.")
            funnel_zaehle("keine_kursdaten")
            return None

        # yfinance liefert bereits 'Close','High','Low','Open','Volume' - keine Umbenennung nötig
        if len(data) < 15:
            print(f"Zu wenig Daten für {ticker}: {len(data)} Zeilen")
            funnel_zaehle("zu_wenig_daten")
            return None

        delta = data['Close'].diff()
        # GEAENDERT (05.08.2026): Wilder's Glaettung statt einfachem
        # rollierendem Mittel - Konsistenz mit get_safe_rsi() weiter oben,
        # siehe dortige Begruendung.
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        # GEAENDERT (05.08.2026, siehe get_safe_rsi fuer die Begruendung)
        rs = gain / loss.clip(lower=1e-9)
        data['RSI'] = 100 - (100 / (1 + rs))
        data['RSI'] = data['RSI'].fillna(50)
        divergenz = check_rsi_divergence(data)

        data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
        data['EMA20'] = data['Close'].ewm(span=20, adjust=False).mean()
        data['EMA50'] = data['Close'].ewm(span=50, adjust=False).mean()
        data['EMA100'] = data['Close'].ewm(span=100, adjust=False).mean()
        data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
        data['WMA200'] = data['Close'].rolling(200).apply(lambda p: np.dot(p, np.arange(1, 201)) / np.sum(np.arange(1, 201)), raw=True)
        data['Vol_SMA20'] = data['Volume'].rolling(20).mean()

        # Ichimoku-Basiswerte (siehe US-Funktion für Begründung)
        data['Tenkan'] = (data['High'].rolling(9).max() + data['Low'].rolling(9).min()) / 2
        data['Kijun'] = (data['High'].rolling(26).max() + data['Low'].rolling(26).min()) / 2
        data['SenkouA'] = ((data['Tenkan'] + data['Kijun']) / 2).shift(26)  # wie im Chart: 26 Perioden Vorlauf
        data['SenkouB'] = ((data['High'].rolling(52).max() + data['Low'].rolling(52).min()) / 2).shift(26)

        entry = data['Close'].iloc[-1]
        stop = data['Low'].rolling(10).min().iloc[-1]

        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        data['Stoch_K'] = 100 * ((data['Close'] - low_min) / (high_max - low_min + 1e-9))
        data['Stoch_D'] = data['Stoch_K'].rolling(3).mean()

        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]

        if 'RSI' not in data.columns:
            print(f"RSI-Berechnung fehlgeschlagen für {ticker}")
            funnel_zaehle("zu_wenig_daten")
            return None

        data['Vol_Ratio'] = data['Volume'] / data['Vol_SMA20']
        data['Vol_Ratio'] = data['Vol_Ratio'].fillna(0)

        # Hebeltrader-Kriterien 1-4 (NEU 07.08.2026) - EU-Pipeline nutzt
        # durchgaengig EUR (siehe dax_aktien-Definition weiter oben)
        _hebeltrader_teilkriterien(ticker, firma_name, sektor, "EU", "EUR", data, entry, stop)

        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        macd_trend = "Bullisch" if macd.iloc[-1] > signal.iloc[-1] else "Bärisch"

        trend_status = "Unter WMA200/EMA200" if (data['Close'].iloc[-1] < data['WMA200'].iloc[-1] or data['Close'].iloc[-1] < data['EMA200'].iloc[-1]) else "OK"

        c1, c2 = data.iloc[-1], data.iloc[-2]
        body = abs(c1['Close'] - c1['Open'])
        lower_wick = min(c1['Open'], c1['Close']) - c1['Low']
        if lower_wick > (2 * body):
            pattern = "Hammer"
        elif c1['Close'] > c1['Open'] and c2['Close'] < c2['Open'] and c1['Close'] > c2['Open'] and c1['Open'] < c2['Close']:
            pattern = "Engulfing"

        crossover_kuerzlich = any(
            data['EMA8'].iloc[-1 - i] <= data['EMA20'].iloc[-1 - i] for i in range(1, 4)
        )
        volumen_kuerzlich = any(
            data['Volume'].iloc[-1 - i] > data['Vol_SMA20'].iloc[-1 - i] for i in range(0, 3)
        )
        ema_breakout = (data['EMA8'].iloc[-1] > data['EMA20'].iloc[-1]) and \
                       crossover_kuerzlich and \
                       volumen_kuerzlich

        low_min = data['Low'].rolling(14).min()
        high_max = data['High'].rolling(14).max()
        stoch_k = 100 * ((data['Close'].iloc[-1] - low_min.iloc[-1]) / (high_max.iloc[-1] - low_min.iloc[-1] + 1e-9))

        is_higher_low = data['Low'].iloc[-1] > data['Low'].iloc[-3]
        buffer = 0.01  # 1.0% Puffer für Zone (vorher 0.3%)

        # Richtungsabhängige Zone-Prüfung (NEU, siehe US-Funktion für Begründung)
        price = data['Close'].iloc[-1]

        def ema_pullback_test(ema_series):
            ema_heute = ema_series.iloc[-1]
            nah_dran = abs(price - ema_heute) < (price * buffer)
            war_ueber_ema_kuerzlich = any(
                data['Close'].iloc[-1 - i] >= ema_series.iloc[-1 - i] for i in range(0, 3)
            )
            return nah_dran and war_ueber_ema_kuerzlich

        in_ema_zone_roh = any(ema_pullback_test(ema_series) for ema_series in [data['EMA20'], data['EMA50'], data['Kijun']])
        # Mindest-Volumen (GEÄNDERT 27.07.2026): siehe US-Funktion für Begründung -
        # bewusst Mindest-Boden (>= 0.7x) statt Pflicht-Spitze, da ein gesunder
        # Pullback klassischerweise auf abnehmendem statt steigendem Volumen laeuft.
        volumen_ausreichend = bool(data['Vol_Ratio'].iloc[-1] >= 0.7)
        in_ema_zone = in_ema_zone_roh and volumen_ausreichend

        # Dritter, eigenständiger Setup-Typ: Ausbruch aus einer fallenden
        # Trendlinie (mind. 3 Berührungspunkte, 1% Toleranz, Pflicht-Volumen)
        trendlinien_ausbruch, tl_level = check_trendline_breakout(data)

        # Vierter, eigenständiger Setup-Typ: echter Kumo-Ausbruch (Ichimoku-
        # Wolke komplett von unten nach oben durchbrochen, Pflicht-Volumen)
        kumo_ausbruch, kumo_level = check_kumo_breakout(data)

        # NEU (27.07.2026): InZone-Grund ergänzt, siehe US-Funktion für Begründung.
        in_zone_grund = "OK" if in_ema_zone else ("EMA-Zone nicht erfüllt" if not in_ema_zone_roh else f"Volumen zu duenn ({data['Vol_Ratio'].iloc[-1]:.2f}x < 0.7)")
        print(f"DEBUG-EU: {ticker} | Breakout: {ema_breakout} | InZone: {in_ema_zone} (Grund: {in_zone_grund}) | "
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
            basis_label = " + ".join(pfade)
            setup_typ = basis_label  # NEU: nur die Basis-Pfade, Pattern (Hammer/Engulfing) bleibt in eigener Spalte, wird NICHT mehr an Setup_Typ angehaengt
        else:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Haupt-Filter nicht erfüllt (Breakout={ema_breakout}, InZone={in_ema_zone}, HL={is_higher_low}, TL-Ausbruch={trendlinien_ausbruch}, Kumo-Ausbruch={kumo_ausbruch}, Stoch={stoch_k:.1f})")
            funnel_zaehle("kein_setup_muster")
            return None

        # Relative Stärke vs. STOXX Europe 600 (statt SPY)
        rel_staerke = None
        if eu_bench_close is not None and len(eu_bench_close) > 60 and len(data) > 60:
            stock_perf_60 = ((data['Close'].iloc[-1] / data['Close'].iloc[-60]) - 1) * 100
            bench_perf_60 = ((eu_bench_close.iloc[-1] / eu_bench_close.iloc[-60]) - 1) * 100
            rel_staerke = round(stock_perf_60 - bench_perf_60, 2)

            if rel_staerke <= -10:
                print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Relative Stärke vs. STOXX600 <= -10% ({rel_staerke}%)")
                funnel_zaehle("rel_staerke_zu_schwach")
                return None

        hoch_52w = data['High'].max()
        abstand_52w_hoch = round(((entry / hoch_52w) - 1) * 100, 2)

        if abstand_52w_hoch < -25:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Zu weit vom 52-Wochen-Hoch entfernt ({abstand_52w_hoch}%, Hoch={hoch_52w:.2f})")
            funnel_zaehle("zu_weit_vom_52w_hoch")
            return None

        fib1, fib2 = get_fib_levels(data)
        # Kumo-Grenzen (NEU) als zusätzliche TP-Kandidaten - NaN-sicher, falls
        # die 26-Perioden-Verschiebung noch keinen gültigen Wert liefert
        kumo_werte = [w for w in [data['SenkouA'].iloc[-1], data['SenkouB'].iloc[-1]] if pd.notna(w)]
        potenzial_targets = sorted([data['EMA20'].iloc[-1], data['EMA50'].iloc[-1], data['EMA100'].iloc[-1], data['EMA200'].iloc[-1], data['WMA200'].iloc[-1], fib1, fib2] + kumo_werte)
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

        # --- Realitäts-Deckel: TP1 darf nicht über dem höchsten tatsächlich
        # erreichten Kurs der letzten 120 Handelstage liegen (siehe US-Funktion
        # für ausführliche Begründung).
        realer_deckel_120 = data['High'].iloc[-120:].max()
        if realer_deckel_120 > entry and tp1 > realer_deckel_120:
            tp1 = realer_deckel_120
            hoehere_ziele = [t for t in targets_above if t > tp1]
            tp2 = hoehere_ziele[0] if hoehere_ziele else tp1 * 1.05

        # --- TP2-Realitäts-Deckel: großzügigeres 250-Tage-Fenster (siehe
        # US-Funktion für ausführliche Begründung).
        tp2 = _begrenze_tp2_realitaetsdeckel(tp1, tp2, entry, data)

        # Kein Analysten-Kursziel für EU-Werte (get_analyst_target ist auf US-Info-Feld
        # ausgelegt; yf liefert targetMeanPrice aber grundsätzlich auch für DAX-Werte)
        analysten_ziel = get_analyst_target(ticker)
        if analysten_ziel is None: analysten_ziel = 0.0

        target_value = analysten_ziel if analysten_ziel > 0 else tp1
        upside_potenzial = round(((target_value - entry) / entry) * 100, 2) if entry > 0 else 0.0

        risiko = entry - stop
        if risiko <= 0:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Risiko <= 0 (Entry={entry:.2f}, Stop={stop:.2f})")
            funnel_zaehle("risiko_ungueltig")
            return None

        crv1 = round((tp1 - entry) / risiko, 2)
        crv2 = round((tp2 - entry) / risiko, 2)
        chance1_perc = round(((tp1 - entry) / entry) * 100, 2)
        chance2_perc = round(((tp2 - entry) / entry) * 100, 2)
        if crv1 < 1.0 or crv2 < 1.0:
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: CRV zu niedrig (CRV1={crv1}, CRV2={crv2}, TP1={tp1:.2f}, TP2={tp2:.2f}, Entry={entry:.2f}, Risiko={risiko:.2f})")
            funnel_zaehle("crv_unter_1")
            funnel_beinahe(ticker, "CRV-Filter",
                           f"Kurs={entry:.2f} | TP1={tp1:.2f} | Chance1={chance1_perc:.2f}% | "
                           f"CRV1={crv1} | TP2={tp2:.2f} | Chance2={chance2_perc:.2f}% | "
                           f"CRV2={crv2} | Stop={stop:.2f} | Risiko={risiko/entry*100:.2f}%",
                           crv_sortier=min(crv1, crv2), name=firma_name)
            return None

        risk_perc = round(((entry - stop) / entry) * 100, 2)
        last_row = data.iloc[-1]

        if last_row['EMA20'] > (last_row['Close'] * 2):
            print(f"DEBUG-VERWORFEN-EU: {ticker} | Grund: Plausibilitäts-Check fehlgeschlagen (EMA20={last_row['EMA20']:.2f} > 2x Close={last_row['Close']:.2f})")
            funnel_zaehle("plausibilitaet")
            return None

        res = {
            "Ticker": str(ticker), "Name": str(firma_name), "Sektor": str(sektor),
            "Trend": str(trend_status), "Setup_Typ": str(setup_typ), "Pattern": str(pattern),
                "Golden_Cross_Status": get_golden_cross_status(data),
            "Tech-Kursziel": clean_num(tp1), "Analysten-Kursziel": float(analysten_ziel),
            "Upside-Potenzial%": float(upside_potenzial), "Status2": "VALIDE",
            "Status_Grund": "Alles ok", "RSI": float(last_row['RSI']),
            "Divergenz": divergenz if divergenz else "Keine",
            "MACD_Trend": str(macd_trend), "CRV1": clean_num(crv1),
            "CRV2": clean_num(crv2), "Kurs": round(last_row['Close'], 2),
            "Chance1_Perc": clean_num(chance1_perc), "Chance2_Perc": clean_num(chance2_perc),
            "Einstieg": round(last_row['Close'], 2), "Einstieg2(EMA 20)": round(last_row['EMA20'], 2),
            "Stop": clean_num(stop), "Risk_Perc": clean_num(risk_perc),
            "TP1": clean_num(tp1), "TP2": clean_num(tp2),
            "Stoch_K": float(stoch_k), "Vol_Ratio": clean_num(last_row['Vol_Ratio']), "Ideales_Delta": 0.0,
            "RS_vs_Benchmark%": clean_num(rel_staerke) if rel_staerke is not None else None,
            "Abstand_52W_Hoch%": clean_num(abstand_52w_hoch),
            "Markt": "EU", "Waehrung": "EUR"
        }
        return res

    except Exception as e:
        print(f"Fehler bei der EU-Analyse von {ticker}: {e}")
        funnel_zaehle("fehler")
        return None

if __name__ == "__main__":
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # 1. Benchmarks sicher abrufen
    # S&P 500 / Nasdaq (GEÄNDERT 27.07.2026): vorher SPY-/QQQ-ETF-Kurse über
    # Alpaca, aber faelschlich als "S&P 500"/"Nasdaq" INDEXSTAND beschriftet -
    # SPY/QQQ folgen dem jeweiligen Index zwar in der Richtung, notieren aber
    # auf einer voellig anderen Kursskala (SPY ca. 1/10 des S&P-500-Punkte-
    # stands) - das ergab z.B. "S&P 500: 738,93 Punkte" statt real ca. 7473.
    # Jetzt wie DAX/EuroStoxx & alle anderen Benchmarks ueber
    # get_index_benchmark_yf mit dem echten Index-Ticker (^GSPC/^IXIC).
    sp500_filter_text = get_index_benchmark_yf("^GSPC", "S&P 500")
    qqq_text = get_index_benchmark_yf("^IXIC", "Nasdaq")
    # Dow Jones (NEU 28.07.2026, Nutzerwunsch): reine Info-Zeile,
    # geht bewusst NICHT in den Marktumfeld-Score ein (siehe oben)
    dow_text = get_index_benchmark_yf("^DJI", "Dow Jones")
    dax_text = get_index_benchmark_yf("^GDAXI", "DAX")
    eurostoxx_text = get_index_benchmark_yf("^STOXX50E", "EuroStoxx50")
    # STOXX Europe 600 (NEU 28.07.2026): Breite-Index fuer den EU-Score
    stoxx600_text = get_index_benchmark_yf("^STOXX", "STOXX Europe 600")
    # Globale Risiko-Benchmarks (NEU): keine Setup-Quellen, dienen nur der
    # Marktumfeld-/Risikoeinschätzung im Briefing (u.a. für Gemini).
    # Russell 2000 = US-Small-Cap-Risikobereitschaft, Nikkei = größter
    # Nicht-US/EU-Markt (Frühindikator, öffnet vor Europa), Hang Seng =
    # China-Sentiment über frei handelbare Werte.
    russell_text = get_index_benchmark_yf("^RUT", "Russell 2000")
    nikkei_text = get_index_benchmark_yf("^N225", "Nikkei 225")
    hangseng_text = get_index_benchmark_yf("^HSI", "Hang Seng")
    # Rohstoff-Kontext (NEU): LIT-ETF als automatisierbarer Proxy fuer den
    # Lithium-Zyklus (echter Lithiumcarbonat-Spot aus China ist ueber
    # yfinance/Alpaca nicht verfuegbar). Nur Kontext fuer Lithium-Positionen,
    # keine Setup-Quelle, keine Abwertungsgrundlage.
    lithium_text = get_index_benchmark_yf("LIT", "Lithium-Proxy (LIT-ETF)")
    # Volatilitaets-/Angst-Index (NEU): der etablierteste Risk-On/Risk-Off-
    # Indikator. Hoher VIX (>20) = nervoeser Markt, Setups riskanter. Nur
    # Kontext fuer die Risikoeinschaetzung, keine Setup-/Abwertungsquelle.
    vix_text = get_index_benchmark_yf("^VIX", "VIX (Volatilitaet)")
    # Zinskurve (GEÄNDERT 27.07.2026, Nutzerwunsch): ersetzt die vorherigen
    # get_zins_warner (30J)/get_10j_rendite (10J) durch eine konsolidierte
    # Zinskurve ueber 2J/5J/10J/30J via FRED, inkl. 10J-2J-Inversions-Check -
    # siehe get_zinskurve_fred fuer die ausfuehrliche Begruendung.
    zins_text = get_zinskurve_fred()
    # FOMC-Countdown (NEU, 27.07.2026): reiner Termin-Hinweis, siehe
    # get_fomc_countdown fuer Begruendung/Wartungshinweis.
    fomc_text = get_fomc_countdown()
    # FOMC-Rueckblick (NEU 30.07.2026): None, wenn im Fenster keine Sitzung lag
    fomc_rueckblick_text = get_fomc_rueckblick()
    # NEU (24.07.2026): erweiterter Makro-/Rohstoff-Kontext fuer ein
    # eigenstaendiges Morgen-Briefing (unabhaengig von der Sektor-Rotation-
    # Auswahl) - alle rein informativ, keine Setup-Quelle, keine
    # Abwertungsgrundlage. Oel/Gold/Silber/Platin/Palladium/Kupfer als Futures-Kontrakte,
    # DXY als Dollar-Staerke-Indikator (treibt Rohstoffe invers + verzerrt
    # EU-Gewinne/-Kurse bei Waehrungsschwankungen), Bitcoin als zunehmend
    # verbreiteter Liquiditaets-/Risikoappetit-Gauge.
    oel_text = get_index_benchmark_yf("CL=F", "Rohöl (WTI)")
    oel_brent_text = get_index_benchmark_yf("BZ=F", "Rohöl (Brent)")
    # 52-Wochen-Kontext + Rekord-Naehe fuer Oel (NEU 30.07.2026, Nutzerwunsch:
    # dieselbe Einordnung, die die Edelmetalle schon haben ueber "LAGE JE
    # METALL" im Edelmetalle-Briefing). None-Werte werden weiter unten beim
    # Zusammenbau uebersprungen statt eine leere Zeile zu erzeugen.
    markt_kontext_texte = []
    for _tick, _label in [("CL=F", "WTI"), ("BZ=F", "Brent"),
                          ("GC=F", "Gold"), ("SI=F", "Silber"),
                          ("PL=F", "Platin"), ("PA=F", "Palladium")]:
        _text = get_markt_kurzfrist_kontext_text(_tick, _label)
        if _text:
            markt_kontext_texte.append(_text)
    rekord_texte = []
    saison_texte = []
    for _tick, _label in [("CL=F", "WTI"), ("BZ=F", "Brent"),
                          ("GC=F", "Gold"), ("SI=F", "Silber"),
                          ("PL=F", "Platin"), ("PA=F", "Palladium")]:
        _text = get_rekord_naehe_text(_tick, _label)
        if _text:
            rekord_texte.append(_text)
    # Saisonalitaet (NEU 02.08.2026, Nutzerwunsch): nur WTI/Brent hier - Gold/
    # Silber/Platin/Palladium werden im Edelmetalle-Briefing unter "LAGE JE
    # METALL" mitgefuehrt (dort liegt der thematische Rahmen).
    for _label in ["WTI", "Brent"]:
        _text = get_saisonalitaet_text(_label)
        if _text:
            saison_texte.append(_text)
    # Index-Rekordhoch-Hinweis (NEU 03.08.2026, Nutzerwunsch): alle Indizes,
    # eigene 3%-Schwelle, nur bei tatsaechlichem Erreichen/Ueberschreiten.
    index_rekord_texte = []
    for _tick, _label in ALLE_INDIZES:
        _text = get_index_rekord_text(_tick, _label)
        if _text:
            index_rekord_texte.append(_text)
    gold_text = get_index_benchmark_yf("GC=F", "Gold")
    silber_text = get_index_benchmark_yf("SI=F", "Silber")
    platin_text = get_index_benchmark_yf("PL=F", "Platin")
    palladium_text = get_index_benchmark_yf("PA=F", "Palladium")
    kupfer_text = get_index_benchmark_yf("HG=F", "Kupfer")
    # US-Dollar-Index (ENTFERNT 29.07.2026, Nutzerwunsch): wird nicht mehr
    # abgerufen/ausgewertet - EUR/USD bleibt als Waehrungs-Referenz im
    # Briefing (fuer EU-Positionen die direkt relevante Groesse).
    # EUR/USD-Wechselkurs (NEU, 28.07.2026, Nutzerwunsch): ergaenzt den DXY
    # (Dollar-Basket gegen 6 Waehrungen, keine reine EUR/USD-Groesse) um den
    # tatsaechlichen Wechselkurs - direkt relevant fuer das Waehrungsrisiko im
    # Portfolio (EUR-/USD-Positionen gemischt, siehe Portfolio-Uebersicht in
    # Abschnitt 9) und Vorarbeit fuer den noch offenen Waehrungsrisiko-To-Do-
    # Punkt. Siehe get_eurusd_wechselkurs() fuer die Begruendung der eigenen,
    # simplen Funktion statt Wiederverwendung von get_index_benchmark_yf.
    eurusd_text = get_eurusd_wechselkurs()
    btc_text = get_index_benchmark_yf("BTC-USD", "Bitcoin")
    # BITCOIN PI-CYCLE BOTTOM (NEU): reine Info. Nutzt dieselben bereits
    # gecachten BTC-Tagesdaten wie der Benchmark; keine Auswirkung auf
    # Setup-Score, CRV, Filter, Marktumfeld oder Intraday. Signal nur bei
    # bestaetigtem Tages-Cross: 150-EMA von unten ueber 471-SMA x 0.745.
    btc_hist = _hole_kursdaten_gecached("BTC-USD")
    pi_cycle_result = calculate_pi_cycle_bottom(btc_hist)
    pi_cycle_text = pi_cycle_result.get("message", "Bitcoin Pi-Cycle Bottom: nicht verfuegbar")

    # BITCOIN 50-WOCHEN-SMA (NEU): reine Info. Basis ist ausschliesslich
    # der BTC/USD-Wochenchart. Der offizielle Cross wird nur anhand eines
    # abgeschlossenen Wochen-Close erkannt. Ein separater Voralarm wird
    # bereits bei Annäherung an die 50W-SMA ausgegeben. Keine Auswirkung auf
    # Setup-Score, CRV, Filter, Marktumfeld oder Intraday-Logik.
    btc_50w_sma_result = calculate_bitcoin_50w_sma(btc_hist, consume_cross=True)
    btc_50w_sma_text = btc_50w_sma_result.get(
        "message", "Bitcoin 50W-SMA: nicht verfuegbar"
    )
    
    # 2. Performance berechnen (US-Sektor-Rotation über Alpaca)
    df_perf = pd.DataFrame([get_perf(t, n) for t, n in sektoren_map.items()]).sort_values("Rotation-Score", ascending=False)

    # 2a. EU-Sektor-Rotation separat berechnen (STOXX-Europe-600-Sektor-ETFs über yfinance,
    # da Alpaca diese nicht abdeckt). Läuft unabhängig von der US-Rotation.
    print("Berechne EU-Sektor-Rotation (STOXX Europe 600)...")
    df_perf_eu = pd.DataFrame([get_perf_yf(t, n) for t, n in eu_sektoren_etf.items()]).sort_values("Rotation-Score", ascending=False)

    # 2b. Benchmarks für die Relative-Stärke-Berechnung laden (einmalig, US + EU getrennt)
    spy_close = get_benchmark_close()
    eu_bench_close = get_eu_benchmark_close()
    
    # 3. Setups verarbeiten (PARALLEL)
    print("Starte Setup-Analyse...")
    blacklist = ["SPLK"] 
    
    # Aufgabenliste erstellen (Top 8 Sektoren, konsistent zum finalen Sektor-Filter unten)
    tasks = []
    for _, row in df_perf.head(8).iterrows():
        aktien_liste = sektoren_aktien.get(row['Ticker'], [])
        for s in aktien_liste:
            if s not in blacklist:
                tasks.append((s, row['Sektor']))

    # EU-Aufgabenliste erstellen (Top 5 von 13 ETF-Sektoren - GEAENDERT 09.08.2026:
    # eu_sektoren_etf deckt jetzt alle 13 dax_aktien-Kategorien ab, siehe dortige Historie)
    tasks_eu = []
    for _, row in df_perf_eu.head(5).iterrows():
        aktien_liste_eu = dax_aktien.get(row['Sektor'], [])
        for s in aktien_liste_eu:
            tasks_eu.append((s, row['Sektor']))

    # KEIN künstliches Ticker-Budget mehr.
    # Seit dem 09.08.2026 werden die Kursdaten über robuste Sammelabrufe
    # (inkl. Chunking/Fehlerisolierung) geladen. Ein globales Limit von 180
    # würde Teile des bewusst erweiterten Universums abschneiden und ist daher
    # fachlich nicht mehr gewollt.
    print(
        f"DEBUG: Finale Task-Anzahl -> US: {len(tasks)} | EU: {len(tasks_eu)} | "
        f"Gesamt: {len(tasks) + len(tasks_eu)} (kein künstliches Ticker-Limit)"
    )

    # SAMMEL-ABRUF (NEU 09.08.2026, Nutzerwunsch): vorher machte jeder der
    # bis zu 10 parallelen Worker unten einen EIGENEN Alpaca-Request pro
    # Ticker - bei 334 US-Tickern also bis zu 334 Einzel-Requests, bei
    # Alpacas hartem 200-Requests/Minute-Limit ein echtes Risiko. Jetzt:
    # EIN Sammel-Abruf VOR der parallelen Verarbeitung, dieselbe Methode
    # wie trendwende_scanner.py's fetch_us_batch (siehe dortige Funktion,
    # hier eigenstaendig nachgebaut wg. drohendem Zirkelimport).
    us_ticker_liste = sorted({t for t, _ in tasks})
    us_daten = fetch_us_batch(us_ticker_liste)

    # Parallel mit max_workers=10 ausführen (US)
    all_setups = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        # Führt analyze_a_setup für alle Tasks gleichzeitig aus. .copy() PFLICHT:
        # derselbe Ticker kann ueber mehrere Sektor-ETFs mehrfach in tasks stehen
        # (z.B. NVDA in XLK+SOXX+SMH+AIQ) und dann in ZWEI Threads gleichzeitig
        # laufen - ohne eigene Kopie wuerde ein Thread Indikator-Spalten in die
        # DataFrame schreiben, waehrend der andere noch daraus liest (Race
        # Condition). Fehlt ein Ticker im Sammel-Abruf (z.B. delistet), bleibt
        # data=None und analyze_a_setup faellt sauber auf den Einzelabruf-
        # Fallback zurueck statt zu crashen.
        results = list(executor.map(
            lambda p: analyze_a_setup(*p, spy_close=spy_close,
                                      data=us_daten[p[0]].copy() if p[0] in us_daten else None),
            tasks))

    # Parallel mit max_workers=10 ausführen (EU)
    results_eu = []
    if tasks_eu:
        # SAMMEL-ABRUF (NEU 09.08.2026): gleiche Begruendung wie beim US-Teil
        # oben - vorher ein yfinance-Request pro EU-Ticker, jetzt EIN
        # Sammel-Abruf vorab.
        eu_ticker_liste = sorted({t for t, _ in tasks_eu})
        eu_daten = fetch_eu_batch(eu_ticker_liste)
        with ThreadPoolExecutor(max_workers=10) as executor:
            # .copy() aus demselben Grund wie beim US-Teil (Race Condition bei
            # mehrfach vorkommendem Ticker ueber mehrere Sektoren)
            results_eu = list(executor.map(
                lambda p: analyze_a_setup_eu(*p, eu_bench_close=eu_bench_close,
                                             data=eu_daten[p[0]].copy() if p[0] in eu_daten else None),
                tasks_eu))
        
    # Ergebnisse filtern (None-Werte entfernen) und US+EU zusammenführen
    all_setups = [r for r in results if r is not None] + [r for r in results_eu if r is not None]
    print(f"Analyse beendet. {len(all_setups)} Setups gefunden.")
    
    # Deine Liste/Spalten und Reihenfolge in setup-Datei (HIER EINGERÜCKT!)
    cols = ['Ticker', 'Name', 'Sektor', 'Markt', 'Waehrung', 'Trend', 'Setup_Typ', 'Pattern', 'Tech-Kursziel', 
            'Analysten-Kursziel', 'Upside-Potenzial%', 'Status2', 'Status_Grund', 
            'RSI', 'MACD_Trend', 'CRV1', 'CRV2', 'Chance1_Perc', 'Chance2_Perc', 'Kurs', 'Einstieg', 'Einstieg2(EMA 20)', 
            'Stop', 'Risk_Perc', 'TP1', 'TP2', 'Stoch_K', 'Vol_Ratio', 'Ideales_Delta',
            'RS_vs_Benchmark%', 'Abstand_52W_Hoch%', 'Golden_Cross_Status']

    if not all_setups:
        print("Keine Setups gefunden.")
        df_s = pd.DataFrame(columns=cols)
    else:
        # Hier erzwingst du die Spaltenreihenfolge!
        # Auch wenn in einem Dictionary mal ein Wert fehlt, 
        # bleibt die Struktur durch 'columns=cols' stabil.
        df_s = pd.DataFrame(all_setups, columns=cols)
        
        # Duplikate entfernen (auf Basis der Ticker-Spalte)
        df_s = df_s.drop_duplicates(subset=['Ticker'])
        
        # Jetzt erst den Index setzen
        df_s = df_s.set_index('Ticker')
        
        # Hier die Sicherheitsprüfung einfügen:
        if 'Divergenz' not in df_s.columns:
            df_s['Divergenz'] = "Keine"

        # --- VORBEREITUNG FÜR DAS APPLY ---

        # Sicherstellen, dass alle benötigten Spalten existieren, falls der Fetch fehlgeschlagen ist
        for col in ['Divergenz', 'RSI', 'Pattern', 'Vol_Ratio', 'MACD_Trend', 'TP1']:
            if col not in df_s.columns:
                # Falls es eine Zahlenspalte ist: 0 setzen
                if col in ['RSI', 'Vol_Ratio', 'TP1']:
                    df_s[col] = 0
                # Falls es eine Textspalte ist: "Kein" oder "Neutral" setzen
                else:
                    df_s[col] = "Kein"

        # --- SICHERE VORBEREITUNG FÜR APPLY ---

        # Spalten, die sicher numerisch sein müssen
        numeric_cols = ['RSI', 'Vol_Ratio', 'Kurs', 'TP1', 'Stoch_K', 'RS_vs_Benchmark%', 'Abstand_52W_Hoch%']
        
        for col in numeric_cols:
            if col in df_s.columns:
                # Konvertiere in Zahl, Fehler werden zu NaN, diese füllen wir mit 0
                df_s[col] = pd.to_numeric(df_s[col], errors='coerce').fillna(0)
            else:
                # Falls die Spalte komplett fehlt, erstelle sie mit 0
                df_s[col] = 0

        # Spalten, die Text sein müssen
        text_cols = ['Pattern', 'MACD_Trend', 'Divergenz']
        for col in text_cols:
            if col in df_s.columns:
                df_s[col] = df_s[col].fillna("Kein").astype(str)
            else:
                df_s[col] = "Kein"

        # JETZT ist das DataFrame sauber und der Fehler beim Vergleich verschwindet
        df_s[['Status2', 'Status_Grund']] = df_s.apply(update_status_logic, axis=1)
    
    setups_vor_filter = len(df_s)  # für die Funnel-Statistik (NEU 28.07.2026)

    # Hebeltrader-Finalisierung (NEU 07.08.2026) - Stufe 2: df_perf/df_perf_eu
    # liegen jetzt vollstaendig vor, Kriterium 5 (Sektor-Vergleich) kann
    # nachgetragen werden. Unabhaengig von df_s/setups_vor_filter oben -
    # Hebeltrader-Kandidaten kommen aus ALLEN geprueften Tickern, nicht nur
    # den als Trendfolge-Setup erkannten.
    hebeltrader_treffer = _hebeltrader_finalisieren(df_perf, df_perf_eu)
    print(f"DEBUG: {len(hebeltrader_treffer)} Hebeltrader-Treffer (Schwelle "
          f"{HEBELTRADER_SCHWELLE}/5) von {len(HEBELTRADER_KANDIDATEN)} geprueften Titeln.")
    print("HEBELTRADER-FUNNEL:")
    print(f"  Universum: {HEBELTRADER_FUNNEL.get('gesamt', 0)}")
    print(f"  - CRV < 1.0 bei TP1 UND TP2: -{HEBELTRADER_FUNNEL.get('crv_nicht_ok', 0)}")
    print(f"  => CRV >= 1.0 bei mindestens einem TP: {HEBELTRADER_FUNNEL.get('crv_ok', 0)}")
    print(f"  - Kriterium 1 Stochastik > 80: -{HEBELTRADER_FUNNEL.get('drop_kriterium_1', 0)}")
    print(f"    => nach K1: {HEBELTRADER_FUNNEL.get('nach_kriterium_1', 0)}")
    print(f"  - Kriterium 2 3M-Hoch (Toleranz 1%): -{HEBELTRADER_FUNNEL.get('drop_kriterium_2', 0)}")
    print(f"    => nach K2: {HEBELTRADER_FUNNEL.get('nach_kriterium_2', 0)}")
    print(f"  - Kriterium 3 Volumen > {MOMENTUM_VOL_SCHWELLE:.1f}x SMA20: -{HEBELTRADER_FUNNEL.get('drop_kriterium_3', 0)}")
    print(f"    => nach K3: {HEBELTRADER_FUNNEL.get('nach_kriterium_3', 0)}")
    print(f"  - Kriterium 4 EMA50-Abstand 5-15%: -{HEBELTRADER_FUNNEL.get('drop_kriterium_4', 0)}")
    print(f"    => nach K4: {HEBELTRADER_FUNNEL.get('nach_kriterium_4', 0)}")
    print(f"  - Sektor-5T nicht verfuegbar: -{HEBELTRADER_FUNNEL.get('drop_sektor_verfuegbarkeit', 0)}")
    print(f"    => nach Sektor-Verfuegbarkeit: {HEBELTRADER_FUNNEL.get('nach_sektor_verfuegbarkeit', 0)}")
    print(f"  - Relative Staerke zum Sektor nicht erfuellt: -{HEBELTRADER_FUNNEL.get('drop_sektor_rs', 0)}")
    print(f"  SCORE-VERTEILUNG: 5/5={HEBELTRADER_FUNNEL.get('score_5', 0)} | 4/5={HEBELTRADER_FUNNEL.get('score_4', 0)} | 3/5={HEBELTRADER_FUNNEL.get('score_3', 0)} | 2/5={HEBELTRADER_FUNNEL.get('score_2', 0)} | 1/5={HEBELTRADER_FUNNEL.get('score_1', 0)} | 0/5={HEBELTRADER_FUNNEL.get('score_0', 0)}")
    print(f"  => FINALE HEBELTRADER-TREFFER: {HEBELTRADER_FUNNEL.get('finale_treffer', 0)}")

    # 5. FILTERN (Erweitert um Trend-Check)
    if not df_s.empty:
        top_8_sektoren = df_perf.nlargest(8, 'Rotation-Score')['Sektor'].tolist()
        top_5_eu_sektoren = df_perf_eu.nlargest(5, 'Rotation-Score')['Sektor'].tolist()

        # DEBUG: Zeigt, an welchem der beiden Kriterien (Sektor oder Trend) die
        # gefundenen Setups vor dem Filter stehen. Marktbewusst, da US- und EU-
        # Sektornamen sich überschneiden können (z.B. "Technologie" in beiden).
        print(f"DEBUG: Top-8-US-Sektoren laut Rotation-Score: {top_8_sektoren}")
        print(f"DEBUG: Top-5-EU-Sektoren laut Rotation-Score: {top_5_eu_sektoren}")
        for tk, r in df_s[['Sektor', 'Markt', 'Trend']].iterrows():
            erlaubte_liste = top_8_sektoren if r['Markt'] == 'US' else top_5_eu_sektoren
            print(f"DEBUG: Setup vor Filter -> {tk} | Markt: {r['Markt']} | Sektor: {r['Sektor']} (in Top: {r['Sektor'] in erlaubte_liste}) | Trend: {r['Trend']}")

        # NEU: Nur Sektoren-Treffer UND nur Aktien, die im Aufwärtstrend (über WMA200) sind.
        # Marktbewusst: US-Setups gegen US-Top-8, DAX-Setups gegen EU-Top-5 (separate Rotationen).
        sektor_ok = (
            ((df_s['Markt'] == 'US') & (df_s['Sektor'].isin(top_8_sektoren))) |
            ((df_s['Markt'] == 'EU') & (df_s['Sektor'].isin(top_5_eu_sektoren)))
        )
        df_s = df_s[sektor_ok & (df_s['Trend'] == 'OK')].copy()
        
        print(f"DEBUG: Setups nach Sektor-Filter & Trend-Check: {len(df_s)}")

    # 6. KONVERTIERUNG & SORTIEREN
        if not df_s.empty:
            cols_to_num = ['CRV1', 'Risk_Perc', 'Upside-Potenzial%'] # 'Upside-Potenzial%' hier hinzufügen
            for col in cols_to_num:
                df_s[col] = pd.to_numeric(df_s[col], errors='coerce').fillna(0)

            df_s['Status_Order'] = df_s['Status2'].map({'VALIDE': 0, 'ACHTUNG': 1}).fillna(2)
            
            # HIER ÄNDERN: Verwende den ursprünglichen Namen 'Upside-Potenzial%'
            df_s = df_s.sort_values(
                by=['Status_Order', 'Upside-Potenzial%', 'CRV1'], 
                ascending=[True, False, False]
            )
            df_s = df_s.drop(columns=['Status_Order'])

    # 7. BEREINIGUNG & FORMATIERUNG
    df_clean = df_s.copy()
    
    # 1. ZUERST umbenennen
    df_clean = df_clean.rename(columns={'Upside-Potenzial%': 'Upside_%_vs_Aktuell'})
    
    # 2. DANN das Delta berechnen (da der Name jetzt existiert)
    # Stelle sicher, dass die Funktion 'get_ideal_delta' weiter oben im Skript definiert ist
    df_clean['Ideales_Delta'] = df_clean['Upside_%_vs_Aktuell'].apply(get_ideal_delta)
    
    # 3. DANN Runden
    cols_to_round = [
        'Tech-Kursziel', 'Analysten-Kursziel', 'Upside_%_vs_Aktuell', 
        'RSI', 'CRV1', 'CRV2', 'Chance1_Perc', 'Chance2_Perc', 'Kurs', 'Einstieg', 'Einstieg2(EMA 20)', 
        'Stop', 'Risk_Perc', 'TP1', 'TP2', 'Stoch_K', 'Vol_Ratio', 'RS_vs_Benchmark%', 'Abstand_52W_Hoch%'
    ]
    df_clean[cols_to_round] = df_clean[cols_to_round].round(2)

    # Fundamental-Ampel (NEU, 21.07.2026): nur für die finale, bereits
    # gefilterte Setup-Liste (klein, API-schonend) - separater Kommentar,
    # kein Modifikator in der Setup-Qualitäts-Matrix.
    if not df_clean.empty:
        ampel_ergebnisse = [
            berechne_fundamental_ampel(t, df_clean.loc[t, 'Sektor'], df_clean.loc[t, 'Markt'])
            for t in df_clean.index
        ]
        df_clean['Fundamental_Ampel'] = [a for a, _ in ampel_ergebnisse]
        df_clean['Fundamental_Hinweis'] = [h for _, h in ampel_ergebnisse]
    else:
        df_clean['Fundamental_Ampel'] = []
        df_clean['Fundamental_Hinweis'] = []

    # NEU (25.07.2026): Ticker-Deduplizierung - ein Ticker kann gleichzeitig
    # in mehreren Top-Sektoren gelistet sein (z.B. ein Titel, der sowohl
    # unter "Software" als auch "Cloud Computing" gefuehrt wird), wodurch
    # derselbe Titel mit identischen Handelswerten aber unterschiedlichem
    # Sektor-Label mehrfach auftauchen konnte (df_clean ist per Ticker
    # indiziert, ein doppelter Ticker heisst hier: doppelter Index-Wert) -
    # fuer die Auswertung redundant. Sektor-Treffer werden zu einem Eintrag
    # zusammengefasst (Sektor-Feld als Komma-Liste), der erste Treffer
    # (samt seiner dort berechneten Fundamental-Ampel) bleibt bestehen -
    # die bestehende Sortierung (Status/Upside-Potenzial/CRV) bleibt dadurch
    # unangetastet, es wird nur nachtraeglich das Sektor-Feld ersetzt.
    if not df_clean.empty and df_clean.index.duplicated().any():
        vor_dedup = len(df_clean)
        sektoren_je_ticker = {}
        for ticker, sektor in zip(df_clean.index, df_clean['Sektor']):
            sektoren_je_ticker.setdefault(ticker, []).append(sektor)
        df_clean = df_clean[~df_clean.index.duplicated(keep='first')].copy()
        df_clean['Sektor'] = [", ".join(dict.fromkeys(sektoren_je_ticker[t])) for t in df_clean.index]
        print(f"DEBUG: {vor_dedup - len(df_clean)} doppelte Ticker zusammengefasst (mehrere Top-Sektoren) -> {len(df_clean)} Setups.")
    
    # 4. Leere Spalte entfernen (falls nötig)
    if 'Ideales_Delta' in df_clean.columns:
        # Hier optional noch Delta auf 2 Stellen runden, falls es eine Fließkommazahl ist
        pass
    
    # --- NEUE STATUS-REGELN (28.07.2026, Nutzerwunsch, Review der Tagesdateien) ---
    # 1) Duplikat-Check: liegt für einen Ticker bereits eine OFFENE Position im
    #    Portfolio (Offene_Positionen.csv, vom Tracker vor diesem Skript
    #    bereitgestellt), ist das Setup KEIN Neueinstieg -> eigener Status
    #    "BEREITS IM PORTFOLIO" (Anlass: EXR am 28.07. als frisches VALIDE-Setup
    #    gemeldet, während die Position schon offen war).
    # 2) Earnings-Regel: Earnings HEUTE oder MORGEN = akutes Über-Nacht-Gap-
    #    Risiko -> VALIDE wird auf ACHTUNG abgestuft (vorher nur Info-Zeile;
    #    ein Stop schützt nicht vor einem Gap unter den Stop-Kurs).
    # 3) Death-Cross-Regel: frischer Death Cross (EMA50 kreuzt EMA200 nach
    #    unten, letzte 10 Handelstage) -> VALIDE wird auf ACHTUNG abgestuft
    #    (vorher "nur Info, keine Bewertung" - Anlass: Ecolab am 28.07.).
    offene_portfolio_ticker = set()
    try:
        if os.path.exists("Offene_Positionen.csv"):
            _df_pos = pd.read_csv("Offene_Positionen.csv", sep=';', encoding='utf-8-sig')
            if not _df_pos.empty and 'Status' in _df_pos.columns and 'Ticker' in _df_pos.columns:
                _offen = _df_pos[_df_pos['Status'].astype(str).str.strip().str.lower() == 'offen']
                offene_portfolio_ticker = {
                    str(t).strip().upper() for t in _offen['Ticker']
                    if str(t).strip() and str(t).strip().lower() != 'nan'
                }
    except Exception as e:
        print(f"DEBUG: Offene_Positionen.csv für Duplikat-Check nicht lesbar ({e}) - Check entfällt heute.")

    if not df_clean.empty:
        for t in df_clean.index:
            if str(t).strip().upper() in offene_portfolio_ticker:
                df_clean.at[t, 'Status2'] = "BEREITS IM PORTFOLIO"
                df_clean.at[t, 'Status_Grund'] = "Position bereits offen - Setup bestätigt den laufenden Trade, kein Neueinstieg"
                print(f"DEBUG-STATUS: {t} -> BEREITS IM PORTFOLIO")
                continue
            if df_clean.at[t, 'Status2'] != "VALIDE":
                continue
            abstufungen = []
            # Mindest-Risiko-Regel (NEU 29.07.2026, Anlass: KLG/WK Kellogg
            # kam mit Risiko 0,17% und CRV 8,75/37,94 als VALIDE durch - eine
            # Aktie, die wegen einer laufenden Uebernahme praktisch am
            # Angebotspreis klebt). Ein Stop 0,2% unter dem Einstieg wird von
            # normalem Tagesrauschen ausgeloest, und die daraus errechneten
            # CRV-Werte sind rechnerisch riesig, aber wertlos - der Nenner
            # geht gegen null. Solche Setups sind kein Fehler der Berechnung,
            # aber ohne manuelle Pruefung nicht handelbar.
            try:
                risk_wert = float(df_clean.at[t, 'Risk_Perc'])
            except (TypeError, ValueError):
                risk_wert = None
            if risk_wert is not None and risk_wert < 1.0:
                abstufungen.append(f"Stop zu eng (Risiko nur {risk_wert:.2f}% - "
                                   f"CRV dadurch rechnerisch ueberhoeht)")
            # Zu kurze Kurshistorie: ohne EMA200/WMA200-Historie sind Trend-
            # aussage und Kursziele nicht belastbar (erkennbar am Golden-
            # Cross-Status, der dann "N/A (zu wenig Kurshistorie)" meldet).
            if "zu wenig Kurshistorie" in str(df_clean.at[t, 'Golden_Cross_Status']):
                abstufungen.append("Zu wenig Kurshistorie fuer belastbare Trendaussage")
            earnings_akut = get_earnings_warnung(t, warn_tage=1)
            if earnings_akut:
                abstufungen.append(f"Earnings-Gap-Risiko ({earnings_akut.replace('⚠ ', '')})")
            if str(df_clean.at[t, 'Golden_Cross_Status']).startswith("DEATH CROSS"):
                abstufungen.append("Frischer Death Cross (EMA50 unter EMA200)")
            if abstufungen:
                df_clean.at[t, 'Status2'] = "ACHTUNG"
                df_clean.at[t, 'Status_Grund'] = " + ".join(abstufungen)
                print(f"DEBUG-ABSTUFUNG: {t} -> ACHTUNG ({df_clean.at[t, 'Status_Grund']})")

        # Neu sortieren, da sich Status-Werte geändert haben können
        _status_rang = {'VALIDE': 0, 'ACHTUNG': 1, 'BEREITS IM PORTFOLIO': 2, 'GELAUFEN': 3}
        df_clean['_status_rang'] = df_clean['Status2'].map(_status_rang).fillna(4)
        df_clean = df_clean.sort_values(
            by=['_status_rang', 'Upside_%_vs_Aktuell', 'CRV1'],
            ascending=[True, False, False]
        ).drop(columns=['_status_rang'])

    # 8. EXPORT
    df_perf.to_csv(f"Performance({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    df_perf_eu.to_csv(f"Performance_EU({today}).csv", index=False, sep=';', encoding='utf-8-sig')
    
    # Hier exportierst du jetzt zwei Versionen (falls das so gewollt ist)
    df_clean.to_csv("setup_liste.csv", index=False)
    df_clean.to_csv(f"Setups({today}).csv", index=False, sep=';', encoding='utf-8-sig')

    relevante_setups = df_clean[df_clean['Status2'] != "GELAUFEN"]
    valide_setups = relevante_setups[relevante_setups['Status2'] == "VALIDE"]
    achtung_setups = relevante_setups[relevante_setups['Status2'] == "ACHTUNG"]
    portfolio_setups = relevante_setups[relevante_setups['Status2'] == "BEREITS IM PORTFOLIO"]
    
    with open(f"Briefing({today}).txt", "w", encoding="utf-8") as f:
        f.write(f"MARKT-UPDATE {today}\n==============================\n\n")
        f.write(get_handelstage_je_region_text() + "\n")
        f.write(f"Erstellt am: {datetime.datetime.now(ZoneInfo("Europe/Berlin")).strftime('%d.%m.%Y, %H:%M')} Uhr (MESZ/MEZ)\n\n")

        # Kurzüberblick über den zugrunde liegenden Trading-Ansatz
        f.write("STRATEGIE-ANSATZ\n")
        f.write("-"*50 + "\n")
        f.write("- Sektor-Rotation: Top-8-US-Sektoren (Alpaca) + separat Top-5-EU-Sektoren (STOXX 600, yfinance)\n")
        f.write("- Kandidaten: US-Sektoren (inkl. Themen-ETFs) + EU-Werte (DAX40/MDAX/Eurozonen-Large-Caps, EUR)\n")
        f.write("- Trend-Filter: Kurs muss über WMA200 und EMA200 liegen\n")
        f.write("- Setup: EMA8/20-Breakout ODER Pullback (Zone/Higher-Low) ODER Trendlinien-Ausbruch ODER Kumo-Ausbruch (Setup_Typ listet ALLE zutreffenden Pfade auf, z.B. \"Trendlinien-Ausbruch + Kumo-Ausbruch\")\n")
        f.write("- Pullback-Zone: Kurs nah an EMA20/50 UND in den letzten 3 Tagen mind. einmal auf/über der EMA (kein reiner Bruch nach unten), Mindest-Volumen (GEÄNDERT 27.07.2026: heutiges Vol_Ratio >= 0.7 statt einer Pflicht-Spitze - ein gesunder Pullback läuft klassischerweise auf abnehmendem statt steigendem Volumen)\n")
        f.write("- Trendlinien-Ausbruch: fallende Linie durch >= 3 Swing-Highs (120 Tage, 1% Toleranz), Pflicht-Volumen\n")
        f.write("- Momentum: Relative Stärke der Aktie > -10% vs. Benchmark (SPY bzw. STOXX600, 60 Tage)\n")
        f.write("- Momentum: Kurs max. 25% unter dem 52-Wochen-Hoch\n")
        f.write("- Risiko: CRV (Chance/Risiko) muss bei TP1 und TP2 jeweils >= 1.0 sein\n")
        f.write("- Stop: Pullback-Setups = Tief der letzten 5 Kerzen, sonst 10-Tage-Tief\n")
        f.write("- Ziel: Pullback-Setups = letzter Swing-High, sonst nächstes EMA/Fib-Level\n")
        f.write("- Realitäts-Deckel: TP1 <= reales 120-Tage-Hoch, TP2 <= reales 250-Tage-Hoch (keine reinen Fib-Extensions ohne Kursdeckung)\n")
        f.write("- Ticker-Budget: kein künstliches Gesamtlimit; robuste Sammelabrufe mit Chunking/Fehlerisolierung schützen vor API-/Rate-Limit-Problemen\n")
        f.write("- Positions-Tracking: manuell in Offene_Positionen.csv (Drive) bestätigte Trades, täglich gegen Stop geprüft\n")
        f.write("- Ichimoku, intern: Kumo-Grenzen (Senkou A/B) als zusätzliche TP-Kandidaten, Kijun-sen als zusätzliches Pullback-Level\n")
        f.write("- Kumo-Ausbruch: Kurs durchbricht komplette Wolke (über Senkou A UND B) innerhalb der letzten 3 Tage, Pflicht-Volumen\n")
        f.write("- Earnings-Regel (NEU 28.07.2026): neue Setups mit Earnings HEUTE oder MORGEN werden von VALIDE auf ACHTUNG abgestuft (akutes Über-Nacht-Gap-Risiko)\n")
        f.write("- Death-Cross-Regel (NEU 28.07.2026): frischer Death Cross (EMA50 kreuzt EMA200 nach unten, letzte 10 Handelstage) stuft VALIDE auf ACHTUNG ab\n")
        f.write("- Duplikat-Check (NEU 28.07.2026): Setups für Titel mit bereits offener Portfolio-Position erhalten den Status BEREITS IM PORTFOLIO (kein Neueinstieg, Bestätigung des laufenden Trades)\n")
        f.write("- Earnings-Rückblick (NEU 29.07.2026): nach berichteten Zahlen (letzte 5 Kalendertage) erscheint eine Zeile '📊 Zahlen TT.MM.: ...' - EPS gemeldet vs. Analystenerwartung (yfinance) KOMBINIERT mit der Kursreaktion am Berichtstag; laufen beide auseinander (Zahlen gut, Kurs fällt), lautet das Urteil 'geteilte Meinung'. Nur EPS, kein Umsatz/keine Guidance verfügbar - reiner Kontext, keine Setup-Bewertung\n\n")

        # HANDELSTAG/ERSTELLT-AM (ENTFERNT 08.08.2026, Nutzerwunsch): standen
        # bis dahin hier als eigene Zeilen - der Nutzer wollte den Datenstand
        # stattdessen direkt in Klammern hinter jedem einzelnen Index in der
        # Regionen-Performance sehen (siehe get_regionen_performance_text),
        # das ist praeziser, weil jeder Index sein EIGENES Datum zeigt statt
        # eines einzelnen globalen Referenzwerts. get_handelstag_text() bleibt
        # als Funktion bestehen (harmlos, aktuell nirgends mehr aufgerufen),
        # falls sie spaeter an anderer Stelle nochmal gebraucht wird.

        # REGIONEN-PERFORMANCE zuerst (NEU 29.07.2026): steht bewusst VOR den
        # Benchmarks, weil die Auswertung mit diesem Block beginnen soll.
        f.write(get_regionen_performance_text() + "\n\n")

        f.write(f"BENCHMARKS\n{sp500_filter_text}\n{qqq_text}\n{dow_text}\n{dax_text}\n{eurostoxx_text}\n{stoxx600_text}\n{russell_text}\n{nikkei_text}\n{hangseng_text}\n{lithium_text}\n{vix_text}\n{zins_text}\n{fomc_text}\n" + (f"{fomc_rueckblick_text}\n" if fomc_rueckblick_text else "") + f"{oel_text}\n{oel_brent_text}\n" + (("KURZFRIST-KONTEXT ÖL/EDELMETALLE\n" + "\n".join(f"- {_t}" for _t in markt_kontext_texte) + "\n") if markt_kontext_texte else "") + f"{gold_text}\n{silber_text}\n{platin_text}\n{palladium_text}\n{kupfer_text}\n{eurusd_text}\n{btc_text}\n" + (f"{pi_cycle_text}\n" if pi_cycle_result.get("signal") else "") + f"{btc_50w_sma_text}\n" + "\n")
        # LIVE-BENCHMARK (NEU 22.08.2026): Ergebnis wird vor analyse.py
        # im Workflow aus der echten Offene_Positionen.csv berechnet.
        # Reine Anzeige; keine Trading-Logik.
        benchmark_text = None
        try:
            if os.path.exists("Benchmark_Live.txt"):
                with open("Benchmark_Live.txt", "r", encoding="utf-8") as _bf:
                    benchmark_text = _bf.read().strip()
        except Exception as _e:
            print(f"WARNUNG: Benchmark_Live.txt konnte nicht gelesen werden: {_e}")
        # LIVE-BENCHMARK-KURZÜBERSICHT: Aggregierte Kennzahlen direkt nach
        # den BENCHMARKS. Die vollständigen Trade-Details bleiben weiter
        # unten im bestehenden Benchmark-Block erhalten.
        if benchmark_text:
            _bm_lines = benchmark_text.splitlines()
            _bm_start = next((i for i, _line in enumerate(_bm_lines)
                              if _line.strip() == "AKTUELLER OFFENER KORB"), None)
            _bm_live = next((i for i, _line in enumerate(_bm_lines)
                             if _line.strip() == "LIVE-SYSTEM SEIT 07.08.2026"), None)
            if _bm_start is not None and _bm_live is not None and _bm_live > _bm_start:
                _bm_summary = [
                    "LIVE-PERFORMANCE vs. MSCI WORLD",
                    "-" * 50,
                    next((x for x in _bm_lines if x.startswith("Stichtag:")), None),
                    next((x for x in _bm_lines if x.startswith("Benchmark:")), None),
                    next((x for x in _bm_lines[_bm_start + 2:_bm_live]
                          if x.startswith("Ø Performance aktuell offener Positionen")), None),
                    next((x for x in _bm_lines[_bm_start + 2:_bm_live]
                          if x.startswith("Anzahl berücksichtigter offener Positionen")), None),
                    "",
                    next((x for x in _bm_lines[_bm_live + 2:]
                          if x.startswith("Ø System-Performance")), None),
                    next((x for x in _bm_lines[_bm_live + 2:]
                          if x.startswith("Ø MSCI-World-Performance")), None),
                    next((x for x in _bm_lines[_bm_live + 2:]
                          if x.startswith("Out-/Underperformance")), None),
                    next((x for x in _bm_lines[_bm_live + 2:]
                          if x.startswith("Positionen besser als MSCI World")), None),
                    next((x for x in _bm_lines[_bm_live + 2:]
                          if x.startswith("Positionen schlechter als MSCI World")), None),
                    next((x for x in _bm_lines[_bm_live + 2:]
                          if x.startswith("Positionen gleichauf")), None),
                ]
                _bm_summary = [x for x in _bm_summary if x is not None]
                if len(_bm_summary) > 2:
                    f.write("\n" + "\n".join(_bm_summary) + "\n")

        # MARKTUMFELD (Score-Modell, GEÄNDERT 28.07.2026 abends, Nutzer-
        # entscheidung): Definition steht im Kommentarblock bei
        # klassifiziere_marktumfeld und wird hier woertlich ins Briefing
        # festgeschrieben - die Auswertung übernimmt Einstufung UND Score
        # wörtlich (siehe Master-Anweisung).
        us_stufe, us_detail, us_score = klassifiziere_marktumfeld(
            [("S&P 500", 2), ("Nasdaq", 1), ("Russell 2000", 1)])
        eu_stufe, eu_detail, eu_score = klassifiziere_marktumfeld(
            [("DAX", 2), ("EuroStoxx50", 1), ("STOXX Europe 600", 1)])
        f.write("MARKTUMFELD (Score-Modell, seit 28.07.2026 - ersetzt 'der schwächste Leitindex zählt')\n")
        f.write("Definition (festgeschrieben):\n")
        f.write("- Stufe je Index: Bullisch = Kurs über EMA20 | Neutral = unter EMA20, aber über EMA50 und WMA200 | Bärisch = unter EMA50 oder unter WMA200\n")
        f.write("- Punkte je Index: Bullisch 2 | Neutral 1 | Bärisch 0\n")
        f.write("- Gewichte USA: S&P 500 x2 (Leitindex) | Nasdaq x1 (Tech-Frühwarnung) | Russell 2000 x1 (Marktbreite)\n")
        f.write("- Gewichte Europa: DAX x2 (Leitindex) | EuroStoxx50 x1 | STOXX Europe 600 x1 (Marktbreite)\n")
        f.write("- Regionen-Score = gewichteter Durchschnitt; >= 1,5 Bullisch | <= 0,5 Bärisch | dazwischen Neutral\n")
        f.write("- Dow Jones: reine Info-Zeile in den BENCHMARKS, fließt bewusst NICHT in den Score ein\n")
        f.write(f"Marktumfeld USA: {us_stufe} (Score {us_score}) - {' | '.join(us_detail)}\n")
        f.write(f"Marktumfeld Europa: {eu_stufe} (Score {eu_score}) - {' | '.join(eu_detail)}\n\n")

        # REKORD-NAEHE Oel + Edelmetalle (NEU 30.07.2026, Nutzerwunsch): nur
        # ausgeben, wenn mindestens ein Instrument betroffen ist - der
        # Normalfall (niemand nah am Rekord) bleibt bewusst stumm, damit der
        # Block nicht taeglich mit "nichts zu melden" aufwartet.
        if rekord_texte:
            f.write("REKORD-NAEHE (Oel/Edelmetalle nahe/auf Hoch- oder Tiefstand seit "
                    "Datenbeginn, Schwelle 10%)\n")
            for _t in rekord_texte:
                f.write(f"- {_t}\n")
            f.write("\n")

        # SAISONALITAET (NEU 02.08.2026, Nutzerwunsch): rein kalenderbasierter
        # Kontext fuer WTI/Brent - nur ausgeben, wenn heute in einem der
        # definierten Fenster liegt (Normalfall: kein Block, Grossteil des
        # Jahres liegt ausserhalb der eng gefassten Fenster).
        if saison_texte:
            f.write("SAISONALITAET (Quelle: RealMoneyTrader Research, reiner Kontext - "
                    "kein Signal, kein Qualitaets-Modifikator)\n")
            for _t in saison_texte:
                f.write(f"- {_t}\n")
            f.write("\n")

        # REKORDHOCH-HINWEIS INDIZES (NEU 03.08.2026, Nutzerwunsch): nur bei
        # tatsaechlichem Erreichen/Ueberschreiten (Toleranz 1%), keine
        # separate "Naehe"-Vorstufe wie bei Oel/Edelmetallen.
        if index_rekord_texte:
            f.write("REKORDHOCH-HINWEIS INDIZES (Schwelle 1%, nur bei echter Rekordnähe/neuem Rekord)\n")
            for _t in index_rekord_texte:
                f.write(f"- {_t}\n")
            f.write("\n")

        # 1. TOP-CHANCEN (VALIDE - PRO-CHECK AKTIV, US + EU gemeinsam nach Score sortiert)
        f.write("\n" + "="*50 + "\n")
        f.write("TRADE-ZUSAMMENFASSUNG (Valide Setups, US + EU)\n")
        f.write("="*50 + "\n")

        for ticker_val, row in valide_setups.iterrows():
            # Stochastik sicher auslesen (fallback auf 0.0 falls nicht vorhanden)
            stoch_val = row.get('Stoch_K', 0.0)
            waehrungszeichen = "€" if row.get('Waehrung') == 'EUR' else "$"
            markt_label = row.get('Markt', 'US')

            f.write(f"\n>>> {ticker_val} | {row['Name']} | Markt: {markt_label} <<<\n")
            f.write(f"Sektor: {row['Sektor']} | Status: {row['Status2']} | Grund: {row['Status_Grund']}\n")
            f.write(f"Pattern: {row['Pattern']} ({row['Setup_Typ']})\n")
            f.write("-" * 40 + "\n")
            f.write(f"Kurs: {row['Kurs']}{waehrungszeichen} / RSI: {row['RSI']} / Stoch-K: {stoch_val:.1f} / MACD: {row['MACD_Trend']}\n")
            f.write(f"Einstieg: {row['Einstieg']}{waehrungszeichen} / EMA20: {row['Einstieg2(EMA 20)']}{waehrungszeichen} / Stop: {row['Stop']}{waehrungszeichen} / Risiko: {row['Risk_Perc']}%\n")
            f.write(f"TP1: {row['TP1']}{waehrungszeichen} (Chance: {row['Chance1_Perc']}%) / CRV1: {row['CRV1']} | TP2: {row['TP2']}{waehrungszeichen} (Chance: {row['Chance2_Perc']}%) / CRV2: {row['CRV2']}\n")
            f.write(f"Vol-Ratio: {row['Vol_Ratio']}x | Ideales Delta: {row['Ideales_Delta']}\n")
            f.write(f"RelStärke vs Benchmark: {row.get('RS_vs_Benchmark%', 'n/a')}% | Abstand 52W-Hoch: {row.get('Abstand_52W_Hoch%', 'n/a')}%\n")
            f.write(f"Fundamental-Ampel: {row.get('Fundamental_Ampel', 'N/A')} ({row.get('Fundamental_Hinweis', '')})\n")
            f.write(f"Golden-/Death-Cross (frischer Death Cross führt zu ACHTUNG): {row.get('Golden_Cross_Status', 'N/A')}\n")

            # Earnings-Warnung (Gap-Risiko) + jüngste Schlagzeilen (nur Kontext)
            earnings = get_earnings_warnung(ticker_val)
            if earnings:
                f.write(f"{earnings}\n")
            # Earnings-Rückblick (NEU 29.07.2026): hat der Titel gerade
            # berichtet, wie fielen die Zahlen aus und was sagte der Markt?
            rueckblick = get_earnings_rueckblick(ticker_val)
            if rueckblick:
                f.write(f"{rueckblick}\n")
            for headline in get_news_headlines(ticker_val):
                f.write(f"News {headline}\n")

            f.write(f"Suche: Hebelprodukt auf {ticker_val} (Fokus: BNP, Goldman, HSBC, UniCredit) | Ziel: {row['TP1']}{waehrungszeichen}\n")
            f.write("\n")

        # 2. WATCHLIST (ACHTUNG)
        f.write("\n" + "="*50 + "\n")
        f.write("WATCHLIST (ACHTUNG - Manuelle Prüfung erforderlich)\n")
        f.write("="*50 + "\n")

        for ticker_val, row in achtung_setups.iterrows():
            upside_val = row.get('Upside_%_vs_Aktuell')
            if upside_val is not None:
                upside_text = f"{upside_val:.2f}%"
            else:
                upside_text = "Kein Ziel"
            waehrungszeichen = "€" if row.get('Waehrung') == 'EUR' else "$"

            _watchlist_name = str(row.get('Name', '')).strip()
            if not _watchlist_name or _watchlist_name.lower() == 'nan':
                _watchlist_name = str(ticker_val)
            f.write(f"{_watchlist_name} ({ticker_val}) | Markt: {row.get('Markt', 'US')} | Grund: {row['Status_Grund']} | Kurs: {row['Kurs']}{waehrungszeichen}\n")
            f.write(f"Upside: Technisch {row['Tech-Kursziel']}{waehrungszeichen} / Potenzial: {upside_text}\n")
            f.write("-" * 30 + "\n\n")

        # 2b. BEREITS IM PORTFOLIO (NEU 28.07.2026): Setups, die auf eine
        # bereits offene Position treffen - kein Neueinstieg, aber wertvolle
        # Info: die Systematik bestätigt den laufenden Trade erneut.
        if not portfolio_setups.empty:
            f.write("\n" + "="*50 + "\n")
            f.write("BEREITS IM PORTFOLIO (Setup bestätigt offene Position - kein Neueinstieg)\n")
            f.write("="*50 + "\n")
            for ticker_val, row in portfolio_setups.iterrows():
                waehrungszeichen = "€" if row.get('Waehrung') == 'EUR' else "$"
                f.write(f"{ticker_val} ({row['Name']}) | Markt: {row.get('Markt', 'US')} | Sektor: {row['Sektor']}\n")
                f.write(f"Setup-Typ: {row['Setup_Typ']} | Kurs: {row['Kurs']}{waehrungszeichen} | TP1: {row['TP1']}{waehrungszeichen} | TP2: {row['TP2']}{waehrungszeichen} | Stop (neu berechnet): {row['Stop']}{waehrungszeichen}\n")
                f.write("Hinweis: Position bereits offen - Setup als Bestätigung des laufenden Trades werten, ggf. Stop-/Ziel-Anpassung prüfen, kein automatischer Nachkauf.\n")
                f.write("-" * 30 + "\n")


        # Vollständiger Benchmark-Block bleibt an der bisherigen Position erhalten.
        # Die Kurzübersicht steht zusätzlich oben direkt nach BENCHMARKS.
        if benchmark_text:
            f.write("\n" + "="*50 + "\n")
            f.write(benchmark_text + "\n")
            f.write("="*50 + "\n")

        # 3. OFFENE POSITIONEN (manuell bestätigte, laufende Trades)
        # Wird von positionen_tracker.py als lokale Datei bereitgestellt (läuft
        # als eigener Workflow-Schritt vor analyse.py). Getrennt von den oben
        # gescannten NEUEN Setups - hier stehen nur Positionen, die der Nutzer
        # aktiv in Offene_Positionen.csv (Google Drive) bestätigt hat.
        f.write("\n" + "="*50 + "\n")
        f.write("OFFENE POSITIONEN (manuell bestätigt)\n")
        f.write("="*50 + "\n")

        positionen_datei = "Offene_Positionen.csv"
        # GEÄNDERT (27.07.2026, Nutzerwunsch): vorher exakter Tages-Match
        # (Ausstiegsdatum == heute) - eine gestoppte Position tauchte damit
        # nur an genau dem einen Tag in der Auswertung auf und war beim
        # kleinsten Ausfall (Kontingent, verpasster Workflow-Lauf, siehe
        # bekannte GitHub-Actions-Scheduling-Problematik) fuer immer
        # "verpasst". Jetzt stattdessen ein rollierendes 10-Werktage-Fenster
        # (Kalenderwochenenden werden uebersprungen, echte Feiertage NICHT
        # beruecksichtigt - reine Kalender-Naeherung) - danach verschwindet
        # die Position automatisch aus der Auswertung, bleibt aber im Sheet
        # bestehen (manuelles Loeschen weiterhin dem Nutzer ueberlassen).
        werktage_grenze = pd.Timestamp.now().normalize() - pd.tseries.offsets.BDay(10)

        def ist_kuerzlich_gestoppt(ausstiegsdatum_str):
            try:
                datum = pd.to_datetime(str(ausstiegsdatum_str).strip(), format="%d.%m.%Y")
                return datum >= werktage_grenze
            except Exception:
                return False

        if os.path.exists(positionen_datei):
            try:
                df_positionen = pd.read_csv(positionen_datei, sep=';', encoding='utf-8-sig')
            except Exception as e:
                df_positionen = pd.DataFrame()
                f.write(f"(Fehler beim Lesen von {positionen_datei}: {e})\n")

            offene = df_positionen[df_positionen['Status'].astype(str).str.strip().str.lower() == 'offen'] if not df_positionen.empty else df_positionen
            # GEAENDERT (29.07.2026, Nutzerwunsch): auch manuell verkaufte
            # Positionen (Status 'Verkauft') gehoeren in diesen Abschnitt.
            # Vorher wurde ausschliesslich 'Gestoppt' erkannt - wer eine
            # Position vor TP1 oder Stop von Hand verkaufte, sah sie danach
            # NIRGENDS mehr: nicht bei den offenen (Status != 'Offen') und
            # nicht bei den geschlossenen. Die Historie verschwand still.
            _status_norm = df_positionen['Status'].astype(str).str.strip().str.lower() \
                if not df_positionen.empty else None
            gestoppt_kuerzlich = df_positionen[
                _status_norm.isin(['gestoppt', 'verkauft'])
                & (df_positionen['Ausstiegsdatum'].apply(ist_kuerzlich_gestoppt))
            ] if not df_positionen.empty else df_positionen

            # Sortierung (NEU 29.07.2026, Nutzerwunsch): nach Ausstiegsdatum
            # ABSTEIGEND - der aktuellste Stop steht oben, aeltere folgen.
            # Vorher galt die zufaellige Sheet-Zeilenreihenfolge; die
            # Auswertung uebernimmt diese Reihenfolge laut Master-Anweisung.
            if not gestoppt_kuerzlich.empty:
                gestoppt_kuerzlich = gestoppt_kuerzlich.copy()
                gestoppt_kuerzlich['_ausstieg_dt'] = pd.to_datetime(
                    gestoppt_kuerzlich['Ausstiegsdatum'].astype(str).str.strip(),
                    format='%d.%m.%Y', errors='coerce'
                )
                gestoppt_kuerzlich = gestoppt_kuerzlich.sort_values(
                    by='_ausstieg_dt', ascending=False
                ).drop(columns=['_ausstieg_dt'])

            if offene.empty and gestoppt_kuerzlich.empty:
                f.write("Keine offenen Positionen erfasst.\n")
                f.write(f"\n{berechne_erfolgsbilanz(df_positionen)}\n")
            else:
                def fmt_de(wert):
                    """Formatiert einen Kurs-/Prozentwert einheitlich mit genau
                    2 Nachkommastellen und deutschem Komma (168.5 -> '168,50').
                    Nicht-numerische Werte (leer, 'n/a', NaN) werden zu 'n/a'."""
                    try:
                        zahl = float(str(wert).replace(',', '.'))
                        if pd.isna(zahl):
                            return "n/a"
                        return f"{zahl:.2f}".replace('.', ',')
                    except (ValueError, TypeError):
                        return wert if str(wert).strip() not in ("", "nan") else "n/a"

                for _, prow in offene.iterrows():
                    waehrungszeichen = {"EUR": "€", "GBP": "£"}.get(str(prow.get("Waehrung", "")).strip(), "$")
                    aktueller_kurs = fmt_de(prow.get('Aktueller_Kurs', "n/a"))
                    performance = fmt_de(prow.get('Performance_Seit_Einstieg%', "n/a"))
                    richtung = str(prow.get('Richtung', '')).strip() or 'Long'
                    # Ideen_Quelle (NEU, 27.07.2026): woher die Position kam
                    # (Trendfolge/Trendwende/Short/Langfrist/Edelmetalle/Manuell,
                    # siehe positionen_tracker.py) - fehlt das Feld (aeltere
                    # Zeile ohne diese Spalte), 'Manuell' als sicheren
                    # Standard annehmen statt die Angabe wegzulassen.
                    ideen_quelle = str(prow.get('Ideen_Quelle', '')).strip()
                    if not ideen_quelle or ideen_quelle.lower() == 'nan':
                        ideen_quelle = 'Manuell'
                    f.write(f"\n>>> {prow['Ticker']} | {prow.get('Name', '')} | Markt: {prow.get('Markt', '')} | Richtung: {richtung} | Quelle: {ideen_quelle} <<<\n")
                    f.write(f"Einstieg: {fmt_de(prow['Einstieg'])}{waehrungszeichen} ({prow.get('Einstiegsdatum', '')})\n")
                    f.write(f"Aktuell: {aktueller_kurs}{waehrungszeichen} / Performance: {performance}%\n")
                    f.write(f"Stop: {fmt_de(prow['Stop'])}{waehrungszeichen} / TP1: {fmt_de(prow['TP1'])}{waehrungszeichen} / TP2: {fmt_de(prow['TP2'])}{waehrungszeichen}\n")

                    # TP-Hinweis (NEU): nur ausgeben, wenn tatsächlich gesetzt
                    # (positionen_tracker.py setzt ihn nur einmalig beim ersten
                    # Erreichen von TP1/TP2, Position bleibt trotzdem offen)
                    tp_hinweis = str(prow.get('TP_Hinweis', '')).strip()
                    if tp_hinweis and tp_hinweis.lower() != 'nan':
                        f.write(f"⚠ Kursziel-Hinweis: {tp_hinweis} (Position weiterhin offen, keine automatische Schließung)\n")

                    # Optionsschein-Zusatzzeile: nur anzeigen, wenn Produkt_Typ
                    # tatsächlich als Optionsschein befüllt wurde
                    produkt_typ = str(prow.get('Produkt_Typ', '')).strip().lower()
                    if produkt_typ == 'optionsschein':
                        emittent = prow.get('Emittent', 'n/a')
                        hebel = prow.get('Hebel', 'n/a')
                        os_performance = fmt_de(prow.get('OS_Performance%', 'n/a'))
                        os_quelle = prow.get('OS_Quelle', 'n/a')
                        f.write(f"Optionsschein: {emittent} | Hebel: {hebel}x | OS-Performance: {os_performance}% (Quelle: {os_quelle})\n")

                    # Earnings-Warnung + Schlagzeilen auch für laufende Positionen
                    earnings = get_earnings_warnung(prow['Ticker'])
                    if earnings:
                        f.write(f"{earnings}\n")
                    # Earnings-Rückblick (NEU 29.07.2026): für laufende
                    # Positionen die wichtigste Variante - die Zahlen sind
                    # raus, die Position läuft weiter, wie war das Urteil?
                    rueckblick = get_earnings_rueckblick(prow['Ticker'])
                    if rueckblick:
                        f.write(f"{rueckblick}\n")
                    for headline in get_news_headlines(prow['Ticker']):
                        f.write(f"News {headline}\n")

                # Portfolio-Übersicht (GEÄNDERT 28.07.2026, Nutzerwunsch):
                # vorher liess die Master-Anweisung Gemini den Durchschnitt
                # selbst aus bis zu 18 Einzelwerten im Kopf ausrechnen - ein
                # bekannter Schwachpunkt von Sprachmodellen bei mentaler
                # Arithmetik ueber viele Zahlen (nachweislicher Rechenfehler
                # am 28.07.2026: Gemini kam auf Ø +0,56% fuer die Setups-
                # Gruppe, korrekt waeren +0,95% gewesen - bei der Gesamtzahl
                # ergab sich dieselbe Abweichung). Jetzt hier in Python vor-
                # berechnet und als fertige Zeile geschrieben - Gemini muss
                # sie nur noch woertlich uebernehmen, kein Kopfrechnen mehr.
                if not offene.empty:
                    offene_perf = offene.copy()
                    # GEAENDERT (28.07.2026, zweite Iteration): komma-tolerant.
                    # Die Zeile fehlte im Nachmittagslauf komplett - wahrschein-
                    # lichste Ursache: Performance-Werte lagen als deutsche
                    # Komma-Strings vor, pd.to_numeric machte daraus still NaN
                    # -> gueltig war leer -> Zeile wurde uebersprungen (fmt_de
                    # in der Positions-Schleife ersetzt Kommas selbst und
                    # zeigte die Werte trotzdem korrekt an, deshalb fiel es
                    # dort nicht auf). Jetzt gleiche Komma-Toleranz wie fmt_de
                    # plus laute Debug-Meldung statt stillem Wegfall.
                    if 'Performance_Seit_Einstieg%' in offene_perf.columns:
                        _perf_roh = offene_perf['Performance_Seit_Einstieg%']
                    else:
                        _perf_roh = pd.Series('', index=offene_perf.index)
                    offene_perf['_perf_num'] = pd.to_numeric(
                        _perf_roh.astype(str).str.replace(',', '.', regex=False),
                        errors='coerce'
                    )
                    if 'Ideen_Quelle' in offene_perf.columns:
                        quelle_roh = offene_perf['Ideen_Quelle'].astype(str).str.strip()
                    else:
                        quelle_roh = pd.Series('', index=offene_perf.index)
                    offene_perf['_quelle_norm'] = quelle_roh.where(
                        ~quelle_roh.str.lower().isin(['', 'nan']), 'Manuell'
                    )
                    gueltig = offene_perf.dropna(subset=['_perf_num'])

                    if not gueltig.empty:
                        teile = []
                        for quelle, gruppe in gueltig.groupby('_quelle_norm'):
                            schnitt = gruppe['_perf_num'].mean()
                            teile.append(f"{quelle}: {len(gruppe)} Position(en), Ø {schnitt:.2f}%".replace('.', ','))
                        gesamt_schnitt = gueltig['_perf_num'].mean()
                        teile.append(f"Gesamt ({len(gueltig)} Positionen): Ø {gesamt_schnitt:.2f}%".replace('.', ','))
                        f.write(f"\nPortfolio-Übersicht: {' | '.join(teile)}\n")
                        f.write(f"\n{berechne_erfolgsbilanz(df_positionen)}\n")
                    else:
                        print("WARNUNG: Portfolio-Übersicht übersprungen - keine "
                              "numerisch lesbaren Performance-Werte in "
                              "Offene_Positionen.csv (Spalte fehlt oder Format unlesbar).")
                        f.write("\nPortfolio-Übersicht: nicht berechenbar (Performance-Werte heute nicht numerisch lesbar - siehe Lauf-Log).\n")
                        f.write(f"\n{berechne_erfolgsbilanz(df_positionen)}\n")

                if not gestoppt_kuerzlich.empty:
                    f.write("\n--- GESCHLOSSEN (letzte 10 Werktage: Stop erreicht oder manuell verkauft) ---\n")
                    for _, prow in gestoppt_kuerzlich.iterrows():
                        waehrungszeichen = {"EUR": "€", "GBP": "£"}.get(str(prow.get("Waehrung", "")).strip(), "$")
                        ideen_quelle = str(prow.get('Ideen_Quelle', '')).strip()
                        if not ideen_quelle or ideen_quelle.lower() == 'nan':
                            ideen_quelle = 'Manuell'
                        # Grund unterscheiden: automatischer Stop vs. manueller Verkauf
                        _st = str(prow.get('Status', '')).strip().lower()
                        grund_txt = "manuell verkauft" if _st == 'verkauft' else "Stop erreicht"
                        f.write(f"{prow['Ticker']} (Quelle: {ideen_quelle}) -- Einstieg: {fmt_de(prow['Einstieg'])}{waehrungszeichen} / Ausstieg: {fmt_de(prow['Ausstiegskurs'])}{waehrungszeichen} am {prow.get('Ausstiegsdatum', '')} ({grund_txt})\n")
        else:
            f.write("(Positions-Tracker hat heute keine Datei bereitgestellt - Abschnitt übersprungen.)\n")

        # Gesamtes Aktien-Universum: alle Einzelaktien aus den Kandidatenlisten
        # (US-Sektorlisten + DAX-Liste), dedupliziert (manche Ticker stehen in
        # mehreren Sektorlisten), unabhängig davon, welche Sektoren heute in
        # der Rotation waren. ETFs/Benchmarks sind nicht enthalten.
        us_universum = len({t for liste in sektoren_aktien.values() for t in liste})
        eu_universum = len({t for liste in dax_aktien.values() for t in liste})
        f.write(f"\nScan-Statistik: Aktien-Universum {us_universum + eu_universum} Titel (US: {us_universum} / EU: {eu_universum}, ohne ETFs/Benchmarks), heute {len(tasks) + len(tasks_eu)} in den Top-Sektoren analysiert, davon {len(valide_setups)} valide Setups.\n")

        # FUNNEL-STATISTIK (NEU 28.07.2026, Nutzerwunsch): macht das Tages-
        # ergebnis interpretierbar - an welcher Prüfstufe fällt wie viel raus?
        funnel_reihenfolge = [
            ("keine_kursdaten", "Keine Kursdaten geliefert (API/NaN-Bereinigung)"),
            ("zu_wenig_daten", "Zu wenig Kurshistorie"),
            ("fehler", "Fehler bei der Berechnung"),
            ("kein_setup_muster", "Keines der 4 Setup-Muster erfüllt (oder Stochastik >= 90)"),
            ("rel_staerke_zu_schwach", "Relative Stärke vs. Benchmark <= -10%"),
            ("zu_weit_vom_52w_hoch", "Mehr als 25% unter dem 52W-Hoch"),
            ("risiko_ungueltig", "Risiko <= 0 (Stop nicht unter dem Einstieg)"),
            ("crv_unter_1", "CRV-Filter (TP1 oder TP2 unter 1.0)"),
            ("plausibilitaet", "Plausibilitäts-Check fehlgeschlagen"),
        ]
        f.write("\nFUNNEL-STATISTIK Hauptscanner (Ablehnungsgründe je Prüfstufe)\n")
        f.write("-" * 50 + "\n")
        f.write(f"Analysiert (Top-Sektoren): {len(tasks) + len(tasks_eu)} Titel\n")
        with _funnel_lock:
            for _key, _beschreibung in funnel_reihenfolge:
                f.write(f"- {_beschreibung}: -{FUNNEL_HAUPT.get(_key, 0)}\n")
        f.write(f"=> Setup-Muster gefunden (vor Sektor-/Trend-Filter): {setups_vor_filter}\n")
        f.write(f"- Sektor-/Trend-Filter + Ticker-Dedupe (nicht in Top-Rotation, unter WMA200/EMA200 oder Mehrfach-Listung): -{setups_vor_filter - len(df_clean)}\n")
        f.write(f"=> Nach allen Filtern: {len(df_clean)} | davon VALIDE: {len(valide_setups)} | ACHTUNG: {len(achtung_setups)} | BEREITS IM PORTFOLIO: {len(portfolio_setups)} | GELAUFEN: {len(df_clean[df_clean['Status2'] == 'GELAUFEN'])}\n")

        # BEINAHE-KANDIDATEN (NEU 30.07.2026): die Titel der spaeten Stufen mit
        # konkretem Wert - beantwortet "woran genau ist es gescheitert?" auf
        # Titel-Ebene, nicht nur als Zahl. Besonders relevant an Tagen ohne
        # valide Setups.
        with _funnel_lock:
            beinahe = list(FUNNEL_BEINAHE)
        if beinahe:
            f.write("\nBEINAHE-KANDIDATEN Hauptscanner (Setup-Muster erfuellt, erst an einer spaeten Stufe gescheitert)\n")
            f.write("-" * 50 + "\n")
            f.write("(nur Beobachtung, KEINE Setups - zeigt, WORAN es im Einzelfall haengt)\n")
            # Sortierung (GEAENDERT 30.07.2026, Nutzerwunsch): absteigend nach dem
            # bindenden CRV - die knappsten Beinahe-Treffer (naeher an der
            # 1.0-Schwelle) stehen oben. Fehlt der Sortierwert ausnahmsweise
            # (aeltere Aufrufstelle ohne crv_sortier), rutscht der Titel ans Ende
            # statt den Sort mit einem Fehler abzubrechen.
            # Namen statt Ticker + Leerzeile zwischen Eintraegen (BUGFIX/NEU
            # 31.07.2026, Nutzerwunsch) + Portfolio-Hinweis (NEU 31.07.2026,
            # Nutzerwunsch "Hinweis ob Positionen vorhanden sind fehlt" -
            # Anlass: Mondelez tauchte als Beinahe-Kandidat auf, obwohl es
            # bereits eine offene Position ist, ohne dass das kenntlich war -
            # nutzt dieselbe offene_portfolio_ticker-Menge wie der Duplikat-
            # Check bei validen Setups oben, nur als Hinweis statt als
            # Status-Aenderung, da ein Beinahe-Kandidat kein Setup ist).
            for b in sorted(beinahe, key=lambda x: (x.get("CRV_Sortier") is None,
                                                     -(x.get("CRV_Sortier") or 0))):
                _portfolio_hinweis = (" [bereits offene Position im Portfolio]"
                                     if b['Ticker'].strip().upper() in offene_portfolio_ticker
                                     else "")
                f.write(f"{b['Name']} ({b['Ticker']}){_portfolio_hinweis}: {b['Stufe']} -> {b['Detail']}\n\n")

        # HEBELTRADER-FUNNEL (NEU 10.08.2026): transparent machen, an welcher
        # Stufe die 5/5-Kandidaten verloren gehen. Die bestehende Auswahl wird
        # dadurch nicht veraendert.
        hf = HEBELTRADER_FUNNEL
        f.write("\nHEBELTRADER-FUNNEL (5/5-Transparenz)\n")
        f.write("-" * 50 + "\n")
        f.write(f"Universum: {hf.get('gesamt', 0)} Titel\n")
        f.write(f"- CRV < 1.0 bei TP1 UND TP2: -{hf.get('crv_nicht_ok', 0)}\n")
        f.write(f"=> CRV >= 1.0 bei mindestens einem TP: {hf.get('crv_ok', 0)}\n")
        f.write(f"- Kriterium 1 Stochastik > 80: -{hf.get('drop_kriterium_1', 0)} | danach: {hf.get('nach_kriterium_1', 0)}\n")
        f.write(f"- Kriterium 2 3M-Hoch (Toleranz 1%): -{hf.get('drop_kriterium_2', 0)} | danach: {hf.get('nach_kriterium_2', 0)}\n")
        f.write(f"- Kriterium 3 Volumen > {MOMENTUM_VOL_SCHWELLE:.1f}x SMA20: -{hf.get('drop_kriterium_3', 0)} | danach: {hf.get('nach_kriterium_3', 0)}\n")
        f.write(f"- Kriterium 4 EMA50-Abstand 5-15%: -{hf.get('drop_kriterium_4', 0)} | danach: {hf.get('nach_kriterium_4', 0)}\n")
        f.write(f"- Sektor-5T nicht verfuegbar: -{hf.get('drop_sektor_verfuegbarkeit', 0)} | danach: {hf.get('nach_sektor_verfuegbarkeit', 0)}\n")
        f.write(f"- Relative Staerke zum Sektor nicht erfuellt: -{hf.get('drop_sektor_rs', 0)}\n")
        f.write(f"SCORE-VERTEILUNG: 5/5={hf.get('score_5', 0)} | 4/5={hf.get('score_4', 0)} | 3/5={hf.get('score_3', 0)} | 2/5={hf.get('score_2', 0)} | 1/5={hf.get('score_1', 0)} | 0/5={hf.get('score_0', 0)}\n")
        f.write(f"=> FINALE HEBELTRADER-TREFFER: {hf.get('finale_treffer', 0)}\n")
        f.write("(Reine Diagnose; die bestehende 5/5- und CRV-Logik bleibt unveraendert.)\n")

        # HEBELTRADER-SETUPS (NEU 07.08.2026, Nutzerwunsch): eigenstaendiger
        # Block, unabhaengig davon, ob ueberhaupt ein Trendfolge-Setup
        # gefunden wurde - Titel koennen hier auftauchen, obwohl sie oben
        # als Trendfolge VERWORFEN wurden (Ueberhitzung ist hier ja gerade
        # das gesuchte Muster, kein Ablehnungsgrund). Nur ausgeben, wenn
        # mindestens ein Treffer vorliegt (Normalfall bei Schwelle 5/5:
        # meist kein oder nur wenige Treffer).
        if hebeltrader_treffer:
            f.write("\nHEBELTRADER-SETUPS (Momentum-Ausbruch-Score, Schwelle "
                    f"{HEBELTRADER_SCHWELLE}/5 - reine Zusatzkategorie neben Trendfolge/"
                    "Trendwende/Short/Langfrist/Edelmetalle)\n")
            f.write("-" * 50 + "\n")
            f.write("(Explosive Ausbruchs-Setups, die von den anderen Kategorien eher als "
                    "'noch nicht ausgeloest' oder 'ueberhitzt' abgelehnt werden - siehe Score "
                    "unten. TP1/TP2/CRV/Stop werden EIGENSTAENDIG berechnet, unabhaengig davon, "
                    "ob ein Trendfolge-Setup fuer denselben Titel ueberhaupt zustande kam. "
                    "Fuer einen finalen HebelTrader-Treffer muessen alle 5 Kriterien (5/5) "
                    "erfuellt sein und mindestens eines von CRV1/CRV2 muss >= 1.0 sein.)\n\n")
            for t in hebeltrader_treffer:
                waehrungszeichen = {"EUR": "€"}.get(t["Waehrung"], "$")
                f.write(f"{t['Name']} ({t['Ticker']}) | Markt: {t['Markt']} | "
                       f"Score: {t['Score']}/{t['Max_Punkte']}\n")
                f.write(f"Kurs: {t['Kurs']:.2f}{waehrungszeichen}\n")
                if t.get("TP1") is not None:
                    f.write(f"TP1: {t['TP1']:.2f}{waehrungszeichen} (Chance: {t['Chance1_Perc']:.2f}%) | "
                           f"CRV1: {t['CRV1']:.2f}\n")
                    f.write(f"TP2: {t['TP2']:.2f}{waehrungszeichen} (Chance: {t['Chance2_Perc']:.2f}%) | "
                           f"CRV2: {t['CRV2']:.2f}\n")
                    f.write(f"Stop: {t['Stop']:.2f}{waehrungszeichen} (Risiko: {t['Risk_Perc']:.2f}%)\n")
                else:
                    f.write("TP1/TP2/Stop/CRV: nicht berechenbar (siehe DEBUG-Zeilen im Lauf-Log)\n")
                # Sektor-Rotation (NEU 09.08.2026, Nutzerwunsch): Momentum-
                # Kriterium 5 vergleicht die Aktie bereits GEGEN ihren Sektor -
                # diese Zeile zeigt zusaetzlich, ob der SEKTOR SELBST gerade
                # Rueckenwind hat (derselbe Rotation-Score, der auch die
                # taegliche Top-8/Top-5-Auswahl bestimmt).
                if t.get("Rotation_Score") is not None:
                    top_text = "Ja" if t["Sektor_In_Top"] else "Nein"
                    f.write(f"Sektor-Rotation: {t['Rotation_Score']:+.2f} "
                           f"(Top-{'8' if t['Markt'] == 'US' else '5'}-Sektor: {top_text})\n")
                for name, (ok, detail) in t["Kriterien_final"].items():
                    f.write(f"  {'✓' if ok else '–'} {name}: {detail}\n")
                f.write("\n")
