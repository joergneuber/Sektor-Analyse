"""
einzel_check.py  (NEU 29.07.2026)

Prueft BELIEBIGE Ticker gegen die eigenen Filter - unabhaengig von der
taeglichen Sektor-Rotation. Anlass: eine externe Empfehlung (Boersenbrief,
Forum, Bekannter) soll durch die eigene Systematik geschickt werden, bevor
gekauft wird. Der regulaere Lauf kann das nicht beantworten, weil der
Hauptscanner taeglich nur die Top-8-US-/Top-5-EU-Sektoren abdeckt - liegt der
Titel in einem anderen Sektor, taucht er schlicht nirgends auf.

WICHTIG - was dieses Skript NICHT tut: Es umgeht den Rotations-Filter
bewusst. Ein Treffer hier bedeutet also "erfuellt die technischen Kriterien",
NICHT "der Sektor hat Rueckenwind". Der Rotations-Gedanke ist Teil der
Strategie - fehlt er, ist das Setup schwaecher als eines aus dem Tageslauf.
Deshalb gibt das Skript den Rotation-Score des Sektors mit aus, sofern eine
Performance-Datei des Tages daneben liegt.

AUFRUF:
    python einzel_check.py GM F CMI BWA PCAR
    python einzel_check.py VOW3.DE            (EU-Ticker mit Boersen-Suffix)

Ohne Argumente wird die TICKER_DEFAULT-Liste unten geprueft.
Benoetigt dieselben Umgebungsvariablen wie analyse.py (ALPACA_KEY,
ALPACA_SECRET, GROQ_API_KEY) und muss im selben Verzeichnis liegen.
"""

import datetime
import glob
import os
import sys

import pandas as pd
import yfinance as yf

from analyse import (
    analyze_a_setup,
    analyze_a_setup_eu,
    get_benchmark_close,
    get_eu_benchmark_close,
)
from trendwende_scanner import _pruefe_trendwende
from short_scanner import _pruefe_short_setup

TICKER_DEFAULT = ["SIX2.DE", "DRH.F", "ENR.DE", "ALB", "NEM", "BABA"]

# Sektor-Zuordnung nur fuer die Anzeige des Rotation-Scores. Unbekannte
# Ticker laufen mit "N/A" durch - die technische Pruefung braucht ihn nicht.
SEKTOR_HINWEIS = {
    "GM": "Zyklischer Konsum", "F": "Zyklischer Konsum",
    "CMI": "Infrastruktur", "BWA": "Zyklischer Konsum", "PCAR": "Industrie",
}


def lade_rotation_scores():
    """Liest die neueste Performance-Datei im Verzeichnis, falls vorhanden."""
    scores = {}
    for muster in ("Performance(*).csv", "Performance_EU(*).csv"):
        for pfad in sorted(glob.glob(muster)):
            try:
                df = pd.read_csv(pfad, sep=';', encoding='utf-8-sig')
                for _, z in df.iterrows():
                    scores[str(z['Sektor'])] = float(z['Rotation-Score'])
            except Exception:
                pass
    return scores


def hole_kursdaten(ticker):
    """52 Wochen + Puffer, wie die regulaeren Scanner (Datums-Schnitt)."""
    data = yf.Ticker(ticker).history(period="2y")
    if data.empty:
        return None
    data = data.dropna(subset=['Close', 'High', 'Low', 'Volume'])
    stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
    if getattr(data.index, 'tz', None) is not None:
        stichtag = stichtag.tz_localize(data.index.tz)
    fenster = data[data.index >= stichtag]
    return fenster if len(fenster) >= 60 else data.tail(252)


def pruefe(ticker, spy_close, eu_close, scores):
    ist_eu = '.' in ticker
    sektor = SEKTOR_HINWEIS.get(ticker, "N/A")
    score = scores.get(sektor)
    print("=" * 62)
    print(f"{ticker}   (Sektor laut Zuordnung: {sektor}"
          + (f", Rotation-Score {score:+.3f}" if score is not None else "")
          + ")")
    print("=" * 62)

    # 1) Trendfolge - der Weg, auf dem der Titel normalerweise ins Briefing kaeme
    try:
        res = (analyze_a_setup_eu(ticker, sektor, eu_close) if ist_eu
               else analyze_a_setup(ticker, sektor, spy_close))
        if res:
            print(f"  TRENDFOLGE: TREFFER - Status {res.get('Status2')} "
                  f"({res.get('Status_Grund')})")
            print(f"    Setup: {res.get('Setup_Typ')} | Kurs {res.get('Kurs')} | "
                  f"Stop {res.get('Stop')} (Risiko {res.get('Risk_Perc')}%)")
            print(f"    TP1 {res.get('TP1')} (CRV {res.get('CRV1')}) | "
                  f"TP2 {res.get('TP2')} (CRV {res.get('CRV2')})")
            print(f"    RSI {res.get('RSI')} | MACD {res.get('MACD_Trend')} | "
                  f"Ampel {res.get('Fundamental_Ampel')}")
        else:
            print("  TRENDFOLGE: kein Setup (Grund siehe DEBUG-Zeilen oben)")
    except Exception as e:
        print(f"  TRENDFOLGE: Fehler ({type(e).__name__}: {e})")

    data = hole_kursdaten(ticker)
    if data is None or data.empty:
        print("  TRENDWENDE/SHORT: keine Kursdaten")
        return

    # 2) Trendwende - greift auch bei Titeln unter der WMA200
    try:
        res, grund = _pruefe_trendwende(ticker, sektor, "EU" if ist_eu else "US",
                                        data.copy(), eu_close if ist_eu else spy_close)
        if res:
            print(f"  TRENDWENDE: TREFFER - {res.get('Setup_Typ')} | "
                  f"Kurs {res.get('Kurs')} | Stop {res.get('Stop')} | "
                  f"TP1 {res.get('TP1')} (CRV {res.get('CRV1')}) | "
                  f"Bonus: {res.get('Qualitaets_Bonus')}")
        else:
            print(f"  TRENDWENDE: kein Kandidat (Stufe: {grund})")
    except Exception as e:
        print(f"  TRENDWENDE: Fehler ({type(e).__name__}: {e})")

    # 3) Short - Marktumfeld-Modifikator bewusst neutral (Einzelpruefung
    #    ausserhalb des Tageslaufs, kein Rotations-Kontext)
    try:
        res, grund = _pruefe_short_setup(ticker, sektor, "EU" if ist_eu else "US",
                                         data.copy(), eu_close if ist_eu else spy_close,
                                         marktumfeld_baerisch=False, sektor_momentum=None)
        if res:
            print(f"  SHORT: TREFFER - {res.get('Setup_Typ')} | "
                  f"Kurs {res.get('Kurs')} | Stop {res.get('Stop')} | "
                  f"TP1 {res.get('TP1')} (CRV {res.get('CRV1')}) | "
                  f"Qualitaet {res.get('Setup_Qualitaet')}")
        else:
            print(f"  SHORT: kein Kandidat (Stufe: {grund})")
    except Exception as e:
        print(f"  SHORT: Fehler ({type(e).__name__}: {e})")


if __name__ == "__main__":
    ticker_liste = sys.argv[1:] or TICKER_DEFAULT
    print(f"EINZEL-CHECK {datetime.date.today().isoformat()} - "
          f"{len(ticker_liste)} Titel: {', '.join(ticker_liste)}")
    print("Hinweis: Rotations-Filter bewusst umgangen - ein Treffer heisst "
          "'technisch erfuellt', nicht 'Sektor hat Rueckenwind'.\n")

    spy_close = get_benchmark_close()
    eu_close = get_eu_benchmark_close()
    scores = lade_rotation_scores()

    for t in ticker_liste:
        pruefe(t.strip(), spy_close, eu_close, scores)
        print()
