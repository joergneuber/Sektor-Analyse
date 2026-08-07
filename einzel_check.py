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
from trendwende_scanner import _pruefe_trendwende, _indikatoren_berechnen
from short_scanner import _pruefe_short_setup
from edelmetalle_scanner import SPANNEN_POSITION_MAX

# --- MOMENTUM-AUSBRUCH-SCORE (NEU 07.08.2026, Nutzerwunsch) ---
# Anlass: die drei bestehenden Strategien (Trendfolge/Trendwende/Short) sind
# als EINSTIEGSFILTER gebaut - sie suchen ein bereits abgeschlossenes,
# bestaetigtes Muster (Breakout+Volumen, Divergenz+Kumo-Trigger, CRV>=1.0
# aus echten Kurszielen). Explosive "Hebeltrader"-Ausbruchsstorys wollen
# aber oft GENAU DEN MOMENT erwischen, in dem sich eine Bewegung erst
# aufbaut oder gerade beschleunigt - und werden vom bestehenden System
# entweder noch VOR dem Trigger abgelehnt (Kumo/Trendlinie noch nicht
# durchbrochen) oder NACH dem Trigger wegen Ueberhitzung (Stochastik>90,
# CRV durch den bereits gelaufenen Kurs zu niedrig). Der Momentum-Ausbruch-
# Score bildet das bewusst ANDERSHERUM ab: er belohnt genau die Symptome
# einer explosiven Bewegung, statt sie als "ueberhitzt" abzuwerten.
# AUSDRUECKLICH NUR ZUSAETZLICHE BEOBACHTUNG - beeinflusst KEINE der drei
# bestehenden Pruefungen, kein Filter, keine Kaufempfehlung. 5 Kriterien,
# je 1 Punkt, Score 0-5.
MOMENTUM_VOL_SCHWELLE = 1.5       # Vol_Ratio > 1.5x SMA20 = deutlicher Anstieg
MOMENTUM_EMA50_ABSTAND_PROZENT = 5.0  # Kurs mind. 5% ueber EMA50


def momentum_ausbruch_score(ticker, data, sektor, sektor_5t):
    """Berechnet den 5-Kriterien-Score direkt aus den bereits geladenen
    Kursdaten (`data`, via hole_kursdaten) - kein zusaetzlicher API-Aufruf.
    Nutzt _indikatoren_berechnen aus trendwende_scanner.py fuer Stoch_K/
    Vol_Ratio/EMA50 - dieselbe Funktion, die auch der Trendwende-Scanner
    nutzt, damit die Werte 1:1 mit dem Rest des Systems uebereinstimmen.

    Kriterien (je 1 Punkt):
      1. Stochastik > 80 (kurzfristig ueberkauft = im Ausbruchsmodus, wird
         von den bestehenden Strategien eher als Warnsignal gewertet)
      2. Neues 3-Monats-Hoch (Datums-Fenster ~90 Tage, Fallback letzte 63
         Handelstage bei Datenluecken - gleiches Muster wie das 52-Wochen-
         Fenster bei Oel/Edelmetallen)
      3. Volumenanstieg: Vol_Ratio > 1.5x (deutlich ueber dem 20-Tage-
         Durchschnitt - typisch fuer den Tag, an dem eine Story "zieht")
      4. Abstand EMA50: Kurs mindestens 5% ueber der EMA50 (starke
         Ausdehnung vom mittelfristigen Mittelwert - genau das, was ein
         Einstiegsfilter oft als "zu weit gelaufen" ablehnt)
      5. Relative Staerke ZUM SEKTOR (nicht zum Gesamtmarkt wie beim
         bestehenden RS_vs_Benchmark%): eigene 5-Tage-Performance versus
         die 5-Tage-Performance des Sektors aus Performance(...).csv -
         nur verfuegbar, wenn der Sektor dort gefunden wird, sonst entfaellt
         dieses eine Kriterium (Score dann nur aus den uebrigen 4 Punkten,
         deutlich gekennzeichnet).

    Gibt einen fertigen Text zurueck oder einen Fehlertext bei Datenproblemen."""
    try:
        df = _indikatoren_berechnen(data.copy())
        if len(df) < 60:
            return "  MOMENTUM-AUSBRUCH-SCORE: zu wenig Kurshistorie"

        kurs = float(df['Close'].iloc[-1])
        stoch = float(df['Stoch_K'].iloc[-1])
        vol_ratio = float(df['Vol_Ratio'].iloc[-1])
        ema50 = float(df['EMA50'].iloc[-1])
        abstand_ema50 = (kurs - ema50) / ema50 * 100 if ema50 > 0 else float('nan')

        # 3-Monats-Hoch: Datums-Fenster (gleiche Konvention wie bei den
        # 52-Wochen-Fenstern) mit Fallback auf Zeilenzahl bei Datenluecken
        stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=90))
        idx = df.index
        if getattr(idx, 'tz', None) is not None:
            stichtag = stichtag.tz_localize(idx.tz)
        fenster_3m = df[idx >= stichtag]
        if len(fenster_3m) < 40:
            fenster_3m = df.tail(63)
        hoch_3m = float(fenster_3m['High'].max())
        # GEAENDERT (08.08.2026, Nutzerwunsch "realistischer bewerten"): 0.1%
        # -> 1% Toleranz, Konsistenz mit derselben Aenderung in analyse.py's
        # _hebeltrader_teilkriterien (siehe dortige Begruendung).
        neues_3m_hoch = kurs >= hoch_3m * 0.99

        punkte = []
        p1 = stoch > 80
        punkte.append(("Stochastik > 80", p1, f"{stoch:.1f}"))
        p2 = neues_3m_hoch
        punkte.append(("Neues 3-Monats-Hoch (Toleranz 1%)", p2, f"Kurs {kurs:.2f} vs. Hoch {hoch_3m:.2f} ({kurs/hoch_3m*100:.1f}%)"))
        p3 = vol_ratio > MOMENTUM_VOL_SCHWELLE
        punkte.append((f"Volumenanstieg (>{MOMENTUM_VOL_SCHWELLE:.1f}x SMA20)", p3, f"{vol_ratio:.2f}x"))
        p4 = abstand_ema50 >= MOMENTUM_EMA50_ABSTAND_PROZENT
        punkte.append((f"Abstand EMA50 (>={MOMENTUM_EMA50_ABSTAND_PROZENT:.0f}%)", p4, f"{abstand_ema50:+.1f}%"))

        rs_sektor_text = "Sektor unbekannt/nicht in Performance-Datei - Kriterium entfällt"
        p5 = False
        max_punkte = 4
        if sektor in sektor_5t and len(df) >= 6:
            eigene_5t = (kurs / float(df['Close'].iloc[-6]) - 1) * 100
            sektor_wert = sektor_5t[sektor]
            p5 = eigene_5t > sektor_wert
            rs_sektor_text = f"Aktie {eigene_5t:+.1f}% vs. Sektor {sektor_wert:+.1f}% (5 Tage)"
            punkte.append(("Relative Stärke zum Sektor (5T)", p5, rs_sektor_text))
            max_punkte = 5

        score = sum(1 for _, ok, _ in punkte if ok)
        zeilen = [f"  MOMENTUM-AUSBRUCH-SCORE: {score}/{max_punkte}"
                 + (" (Sektor-Kriterium nicht verfügbar)" if max_punkte == 4 else "")]
        for name, ok, detail in punkte:
            zeilen.append(f"    {'✓' if ok else '–'} {name}: {detail}")
        return "\n".join(zeilen)
    except Exception as e:
        return f"  MOMENTUM-AUSBRUCH-SCORE: Fehler ({type(e).__name__}: {e})"


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
    """Liest die neueste Performance-Datei im Verzeichnis, falls vorhanden.
    GEAENDERT (07.08.2026, Momentum-Ausbruch-Score): erfasst zusaetzlich die
    5-Tage-Sektor-Performance ("5T"-Spalte, von analyse.py's get_perf()
    ermittelt) - Grundlage fuer eine ECHTE Aktie-vs-Sektor-Relative-Staerke
    statt nur Aktie-vs-Gesamtmarkt (SPY/STOXX600, das war bisher der einzige
    RS-Wert im System). Rueckgabe jetzt zwei Dicts statt einem."""
    scores = {}
    sektor_5t = {}
    for muster in ("Performance(*).csv", "Performance_EU(*).csv"):
        for pfad in sorted(glob.glob(muster)):
            try:
                df = pd.read_csv(pfad, sep=';', encoding='utf-8-sig')
                for _, z in df.iterrows():
                    scores[str(z['Sektor'])] = float(z['Rotation-Score'])
                    if '5T' in df.columns:
                        sektor_5t[str(z['Sektor'])] = float(z['5T'])
            except Exception:
                pass
    return scores, sektor_5t


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


def pruefe(ticker, spy_close, eu_close, scores, sektor_5t):
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

    # 1b) Momentum-Ausbruch-Score (NEU 07.08.2026) - reine Zusatzbeobachtung,
    # unabhaengig von den drei Strategie-Pruefungen, siehe Modul-Docstring
    # der Funktion fuer die Begruendung.
    print(momentum_ausbruch_score(ticker, data, sektor, sektor_5t))
    print()

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
    if len(sys.argv) > 1:
        ticker_liste = []

        for arg in sys.argv[1:]:
            # erlaubt:
            # GM,F,NEM
            # GM, F, NEM
            # GM F NEM
            teile = arg.split(",")

            for t in teile:
                t = t.strip().rstrip(",").upper()
                if t:
                    ticker_liste.append(t)
    else:
        ticker_liste = TICKER_DEFAULT

    print(
        f"EINZEL-CHECK {datetime.date.today().isoformat()} - "
        f"{len(ticker_liste)} Titel: {', '.join(ticker_liste)}"
    )

    print(
        "Hinweis: Rotations-Filter bewusst umgangen - "
        "ein Treffer heisst 'technisch erfuellt', "
        "nicht 'Sektor hat Rueckenwind'.\n"
    )

    spy_close = get_benchmark_close()
    eu_close = get_eu_benchmark_close()
    scores, sektor_5t = lade_rotation_scores()

    for t in ticker_liste:
        pruefe(t, spy_close, eu_close, scores, sektor_5t)
        print()