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

VERGLEICHSMESSUNG TRENDWENDE (NEU 29.07.2026): Die Trendwende wird ZWEIMAL
geprueft - einmal mit der Aktien-Regel (max. 20% ueber dem 52W-Tief) und
einmal mit der Metall-Regel (Position in der 52-Wochen-Spanne <=
SPANNEN_POSITION_MAX). Zweck: ueber ein paar Wochen sichtbar machen, ob die
Aktien-Regel systematisch Boden-Kandidaten wegfiltert - ohne am Tagesbetrieb
etwas zu aendern. Anlass: am 29.07. scheiterten 5 von 6 geprueften Titeln
(u.a. Albemarle mit Stochastik 8,2 und Draegerwerk mit 7,9 - beide klar
ueberverkauft) an genau dieser Stufe. Der Tageslauf bleibt unveraendert bei
der Aktien-Regel; hier ist es reine Messung.

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
from edelmetalle_scanner import SPANNEN_POSITION_MAX

# Standard-Liste (GEAENDERT 29.07.2026): alle an diesem Tag geprueften Titel,
# damit sich ein Wiederholungslauf ohne Tipparbeit starten laesst - fuer die
# Vergleichsmessung Aktien-Regel vs. Metall-Regel (siehe Modul-Docstring) ist
# genau das der Zweck: dieselben Titel ueber Wochen beobachten.
# Ueber das workflow_dispatch-Eingabefeld jederzeit ueberschreibbar.
TICKER_DEFAULT = [
    # Runde 1 - Kandidaten aus dem Boersenbrief-Teaser ("Zykliker mit Umbau")
    "GM", "F", "CMI", "BWA", "PCAR",
    # Runde 2 - eigene Beobachtungsliste
    "BABA", "NEM", "ALB", "SIX2.DE", "DRH.F", "ENR.DE",
]

# Sektor-Zuordnung NUR fuer die Anzeige des Rotation-Scores - die technische
# Pruefung braucht sie nicht, unbekannte Ticker laufen mit "N/A" durch.
# Namen bewusst exakt wie in Performance(...).csv / Performance_EU(...).csv,
# sonst findet der Nachschlag nichts.
SEKTOR_HINWEIS = {
    "GM": "Zyklischer Konsum",
    "F": "Zyklischer Konsum",
    "BWA": "Zyklischer Konsum",
    "BABA": "Zyklischer Konsum",
    "CMI": "Infrastruktur",
    "PCAR": "Industrie",
    "NEM": "Gold-Miner",
    "ALB": "Rohstoffe",
    "SIX2.DE": "Industrie",      # Sixt SE
    "DRH.F": "Rüstung/Aerospace",  # DroneShield Ltd, Frankfurt-Notierung in EUR
    "ENR.DE": "Industrie",       # Siemens Energy AG
}

# Klarnamen fuer die Ausgabe - macht das Log ohne Nachschlagen lesbar
# (der Scanner selbst arbeitet weiterhin mit Tickern).
NAME_HINWEIS = {
    "GM": "General Motors", "F": "Ford Motor Company", "CMI": "Cummins",
    "BWA": "BorgWarner", "PCAR": "Paccar", "BABA": "Alibaba",
    "NEM": "Newmont", "ALB": "Albemarle", "SIX2.DE": "Sixt SE",
    "DRH.F": "DroneShield (EUR, Frankfurt)", "ENR.DE": "Siemens Energy",
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
    klarname = NAME_HINWEIS.get(ticker)
    kopf = f"{ticker}" + (f" - {klarname}" if klarname else "")
    print("=" * 62)
    print(f"{kopf}   (Sektor laut Zuordnung: {sektor}"
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

    # 2) Trendwende - ZWEIMAL geprueft (siehe Modul-Docstring): einmal mit
    #    der Aktien-Regel des Tageslaufs, einmal mit der Metall-Regel.
    #    Beide Ergebnisse nebeneinander, damit ueber die Zeit sichtbar wird,
    #    ob die Aktien-Regel Boden-Kandidaten systematisch verwirft.
    def _trendwende(spannen_max=None):
        return _pruefe_trendwende(ticker, sektor, "EU" if ist_eu else "US",
                                  data.copy(), eu_close if ist_eu else spy_close,
                                  spannen_position_max=spannen_max)

    def _ausgabe(label, res, grund):
        if res:
            print(f"  {label}: TREFFER - {res.get('Setup_Typ')} | "
                  f"Kurs {res.get('Kurs')} | Stop {res.get('Stop')} | "
                  f"TP1 {res.get('TP1')} (CRV {res.get('CRV1')}) | "
                  f"Bonus: {res.get('Qualitaets_Bonus')}")
        else:
            print(f"  {label}: kein Kandidat (Stufe: {grund})")

    ergebnisse_tw = {}
    for label, spannen_max in (
        ("TRENDWENDE (Aktien-Regel: max. 20% ueber 52W-Tief)", None),
        (f"TRENDWENDE (Metall-Regel: Spannen-Position <= {SPANNEN_POSITION_MAX:.0%})",
         SPANNEN_POSITION_MAX),
    ):
        try:
            res, grund = _trendwende(spannen_max)
            _ausgabe(label, res, grund)
            ergebnisse_tw[spannen_max] = (res is not None, grund)
        except Exception as e:
            print(f"  {label}: Fehler ({type(e).__name__}: {e})")
            ergebnisse_tw[spannen_max] = (None, "fehler")

    # Abweichung ausdruecklich benennen - das ist der eigentliche Messwert
    aktien_ok = ergebnisse_tw.get(None, (None, None))[0]
    metall_ok = ergebnisse_tw.get(SPANNEN_POSITION_MAX, (None, None))[0]
    if aktien_ok is False and metall_ok is True:
        # Kennzahl mitliefern, damit die Abweichung nachvollziehbar ist
        try:
            kurs = float(data['Close'].iloc[-1])
            tief = float(data['Low'].min())
            hoch = float(data['High'].max())
            print(f"  >>> ABWEICHUNG: nur die Metall-Regel laesst diesen Titel zu "
                  f"({(kurs/tief-1)*100:.1f}% ueber 52W-Tief, aber Spannen-Position "
                  f"{(kurs-tief)/(hoch-tief):.0%}) - Kandidat fuer die Frage, ob die "
                  f"Aktien-Regel zu eng ist.")
        except Exception:
            print("  >>> ABWEICHUNG: nur die Metall-Regel laesst diesen Titel zu.")
    elif aktien_ok is True and metall_ok is False:
        print("  >>> ABWEICHUNG umgekehrt: nur die Aktien-Regel laesst diesen Titel zu "
              "(Titel ist nah am Tief, sitzt aber hoch in der Jahresspanne - "
              "typisch fuer eine enge Seitwaertsspanne).")

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
