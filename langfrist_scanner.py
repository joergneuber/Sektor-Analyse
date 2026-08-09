"""
langfrist_scanner.py

Dritte, eigenstaendige Scanner-Kategorie neben dem Trendfolge-Scanner
(analyse.py) und dem Trendwende-Scanner (trendwende_scanner.py): sucht NICHT
nach kurzfristigen technischen Setups, sondern bewertet eine kuratierte
Liste bekannter Qualitaets-/Blue-Chip-Aktien fundamental - fuer eine
langfristige Positionierung (Halten ueber Monate/Jahre), nicht fuer
kurzfristige Trades.

Architektur-Entscheidung (Stand 20.07.2026, siehe gemeinsame Abstimmung):
  - Eigene, kleinere Universum-Liste (nicht das ~370er-Sektoren-Universum) -
    robustere Datenlage bei bekannten Blue Chips, und KGV-basierte Bewertung
    ist bei zyklischen/verlustschreibenden Werten ohnehin nicht sinnvoll.
  - Woechentlicher statt taeglicher Lauf (siehe langfrist_check.yml) - das
    aktuelle KGV haengt vom Kurs ab und bewegt sich taeglich, aber die
    Fundamentaldaten selbst (Umsatz, Verschuldung) aendern sich nur
    quartalsweise; woechentlich ist der Kompromiss.
  - Kennzahlen-Mix: KGV (aktuell + historische Naeherung), KUV, KBV,
    Dividendenrendite, Verschuldung (Debt/Equity), Umsatz- und
    Gewinnwachstum.

WICHTIGE EINSCHRAENKUNG (unbedingt beim Lesen der Ausgabe beachten):
  Eine ECHTE historische KGV-Reihe braeuchte historische Gewinne pro
  Quartal ueber Jahre - das ist über yfinance nicht zuverlaessig verfuegbar
  (besonders bei EU-Titeln). Stattdessen wird eine NAEHERUNG berechnet:
  der heutige Gewinn pro Aktie (EPS) angewendet auf die historischen
  Kursverlaeufe der letzten 5 Jahre. Das zeigt, ob der aktuelle Kurs
  guenstig oder teuer im Verhaeltnis zur eigenen 5-Jahres-Handelsspanne
  steht (mit heutiger Ertragskraft gerechnet) - das ist etwas anderes als
  "war die Aktie historisch auf diesem KGV-Niveau", da sich der Gewinn ja
  ueber die Zeit veraendert hat. Wird im Output explizit als Naeherung
  gekennzeichnet.

Voraussetzungen: pip install yfinance pandas
"""

import datetime
import pandas as pd
import yfinance as yf
from market_cache import get_yf_history


# ---------------------------------------------------------------------------
# KURATIERTES UNIVERSUM (bewusst klein und auf bekannte, liquide
# Qualitaets-/Blue-Chip-Werte beschraenkt - robustere Datenlage als beim
# breiten Sektoren-Universum der anderen beiden Scanner)
# ---------------------------------------------------------------------------

LANGFRIST_UNIVERSUM = {
    # US - Offizielle S&P 500 Dividend Aristocrats (25+ Jahre in Folge
    # steigende Dividende), Stand NOBL-ETF-Holdings vom 20.07.2026 - 69
    # Titel, ersetzt die vorherige 15er-Ratezusammenstellung durch eine
    # objektive, nachvollziehbare Auswahlregel.
    "WST": ("West Pharmaceutical Services, Inc.", "US", "Gesundheit"),
    "ADP": ("Automatic Data Processing, Inc.", "US", "Industrie"),
    "ABBV": ("AbbVie Inc.", "US", "Gesundheit"),
    "ADM": ("Archer-Daniels-Midland Company", "US", "Basiskonsum"),
    "EXPD": ("Expeditors International of Washington, Inc.", "US", "Industrie"),
    "BEN": ("Franklin Resources, Inc.", "US", "Finanzen"),
    "HRL": ("Hormel Foods Corporation", "US", "Basiskonsum"),
    "GWW": ("W.W. Grainger, Inc.", "US", "Industrie"),
    "TROW": ("T. Rowe Price Group, Inc.", "US", "Finanzen"),
    "SWK": ("Stanley Black & Decker, Inc.", "US", "Industrie"),
    "SJM": ("The J.M. Smucker Company", "US", "Basiskonsum"),
    "CTAS": ("Cintas Corporation", "US", "Industrie"),
    "ESS": ("Essex Property Trust, Inc.", "US", "Immobilien"),
    "NUE": ("Nucor Corporation", "US", "Grundstoffe"),
    "CHRW": ("C.H. Robinson Worldwide, Inc.", "US", "Industrie"),
    "FRT": ("Federal Realty Investment Trust", "US", "Immobilien"),
    "GD": ("General Dynamics Corporation", "US", "Industrie"),
    "CL": ("Colgate-Palmolive Company", "US", "Basiskonsum"),
    "KMB": ("Kimberly-Clark Corporation", "US", "Basiskonsum"),
    "JNJ": ("Johnson & Johnson", "US", "Gesundheit"),
    "ES": ("Eversource Energy", "US", "Versorger"),
    "CAH": ("Cardinal Health, Inc.", "US", "Gesundheit"),
    "CAT": ("Caterpillar Inc.", "US", "Industrie"),
    "KVUE": ("Kenvue Inc.", "US", "Basiskonsum"),
    "SYY": ("Sysco Corporation", "US", "Basiskonsum"),
    "KO": ("The Coca-Cola Company", "US", "Basiskonsum"),
    "CINF": ("Cincinnati Financial Corporation", "US", "Finanzen"),
    "ABT": ("Abbott Laboratories", "US", "Gesundheit"),
    "GPC": ("Genuine Parts Company", "US", "Industrie"),
    "AFL": ("Aflac Incorporated", "US", "Finanzen"),
    "FDS": ("FactSet Research Systems Inc.", "US", "Finanzen"),
    "CB": ("Chubb Limited", "US", "Finanzen"),
    "AMCR": ("Amcor plc", "US", "Grundstoffe"),
    "SPGI": ("S&P Global Inc.", "US", "Finanzen"),
    "PPG": ("PPG Industries, Inc.", "US", "Grundstoffe"),
    "TGT": ("Target Corporation", "US", "Einzelhandel"),
    "PG": ("The Procter & Gamble Company", "US", "Basiskonsum"),
    "CHD": ("Church & Dwight Co., Inc.", "US", "Basiskonsum"),
    "ED": ("Consolidated Edison, Inc.", "US", "Versorger"),
    "LIN": ("Linde plc", "US", "Grundstoffe"),
    "O": ("Realty Income Corporation", "US", "Immobilien"),
    "NDSN": ("Nordson Corporation", "US", "Industrie"),
    "ITW": ("Illinois Tool Works Inc.", "US", "Industrie"),
    "MDT": ("Medtronic plc", "US", "Gesundheit"),
    "BDX": ("Becton, Dickinson and Company", "US", "Gesundheit"),
    "CVX": ("Chevron Corporation", "US", "Energie"),
    "ECL": ("Ecolab Inc.", "US", "Grundstoffe"),
    "BRO": ("Brown & Brown, Inc.", "US", "Finanzen"),
    "APD": ("Air Products and Chemicals, Inc.", "US", "Grundstoffe"),
    "ROP": ("Roper Technologies, Inc.", "US", "Technologie"),
    "FAST": ("Fastenal Company", "US", "Industrie"),
    "MKC": ("McCormick & Company, Incorporated", "US", "Basiskonsum"),
    "XOM": ("Exxon Mobil Corporation", "US", "Energie"),
    "SHW": ("The Sherwin-Williams Company", "US", "Grundstoffe"),
    "NEE": ("NextEra Energy, Inc.", "US", "Versorger"),
    "DOV": ("Dover Corporation", "US", "Industrie"),
    "ATO": ("Atmos Energy Corporation", "US", "Versorger"),
    "CLX": ("The Clorox Company", "US", "Basiskonsum"),
    "EMR": ("Emerson Electric Co.", "US", "Industrie"),
    "AOS": ("A. O. Smith Corporation", "US", "Industrie"),
    "ERIE": ("Erie Indemnity Company", "US", "Finanzen"),
    "BF-B": ("Brown-Forman Corporation", "US", "Basiskonsum"),
    "MCD": ("McDonald's Corporation", "US", "Einzelhandel"),
    "PEP": ("PepsiCo, Inc.", "US", "Basiskonsum"),
    "WMT": ("Walmart Inc.", "US", "Einzelhandel"),
    "IBM": ("International Business Machines Corporation", "US", "Technologie"),
    "LOW": ("Lowe's Companies, Inc.", "US", "Einzelhandel"),
    "PNR": ("Pentair plc", "US", "Industrie"),
    "ALB": ("Albemarle Corporation", "US", "Grundstoffe"),
    # EU - Blue Chips (eigene Auswahl, kein offizieller Aristokraten-Index -
    # sag Bescheid, falls hierfür auch eine objektive europäische Liste
    # recherchiert werden soll)
    "SAP.DE": ("SAP SE", "EU", "Technologie"),
    "ASML.AS": ("ASML Holding N.V.", "EU", "Technologie"),
    "OR.PA": ("L'Oreal S.A.", "EU", "Basiskonsum"),
    "NESN.SW": ("Nestle S.A.", "EU", "Basiskonsum"),
    "NOVN.SW": ("Novartis AG", "EU", "Gesundheit"),
    "ROG.SW": ("Roche Holding AG", "EU", "Gesundheit"),
    "MC.PA": ("LVMH Moet Hennessy Louis Vuitton", "EU", "Luxusgueter"),
    "SIE.DE": ("Siemens AG", "EU", "Industrie"),
    "ALV.DE": ("Allianz SE", "EU", "Versicherungen"),
    "AIR.PA": ("Airbus SE", "EU", "Industrie"),
}

# Schwellenwerte fuer die Bewertungs-Einstufung (Naeherungs-KGV vs. aktuelles
# KGV, siehe Modul-Docstring zur Einschraenkung)
GUENSTIG_SCHWELLE = 0.90  # aktuelles KGV < 90% der Naeherung -> "guenstig"
TEUER_SCHWELLE = 1.10     # aktuelles KGV > 110% der Naeherung -> "teuer"


def sicheres_info_feld(info, feld, default=None):
    wert = info.get(feld, default)
    if wert is None:
        return default
    try:
        return float(wert)
    except (ValueError, TypeError):
        return default


def normalisiere_dividendenrendite(wert):
    """Yahoo/yfinance hat das Feld 'dividendYield' im Lauf der Zeit von einem
    Bruch (0.0371 = 3,71%) auf bereits-Prozent (3.71 = 3,71%) umgestellt -
    ohne Ankündigung, und je nach Ticker/Zeitpunkt inkonsistent beobachtet.
    Statt blind mit 100 zu multiplizieren (fuehrte zu Werten wie "371%"),
    wird hier anhand der Groessenordnung erkannt, welches Format vorliegt:
    Werte > 1 sind fuer eine Dividendenrendite unplausibel als Bruch (das
    waere > 100%) - dann ist es bereits Prozent. Werte <= 1 werden als
    Bruch behandelt und mit 100 multipliziert."""
    if wert is None:
        return 0.0
    if wert > 1:
        return round(wert, 2)
    return round(wert * 100, 2)


def berechne_naeherungs_kgv(ticker_obj, aktueller_kurs, trailing_eps):
    """NAEHERUNG (siehe Modul-Docstring): wendet den heutigen Gewinn pro
    Aktie auf die historischen Kurse der letzten 5 Jahre an, um zu sehen, ob
    der aktuelle Kurs guenstig/teuer relativ zur eigenen 5-Jahres-
    Handelsspanne ist. KEINE echte historische KGV-Reihe."""
    if trailing_eps is None or trailing_eps <= 0:
        return None
    try:
        hist = ticker_obj.history(period="5y", interval="1mo")
        if hist.empty or "Close" not in hist.columns:
            return None
        naeherungs_kgv_reihe = hist["Close"] / trailing_eps
        return round(float(naeherungs_kgv_reihe.mean()), 2)
    except Exception:
        return None


# Schwelle fuer den Verzerrungs-Filter (NEU, 27.07.2026): weichen aktuelles
# und Forward-KGV mehr als um diesen Faktor voneinander ab, deutet das auf
# einen Einmaleffekt in den Trailing-Earnings hin (z.B. Abschreibung,
# Sondergewinn) - der aktuelle Gewinn pro Aktie ist dann keine brauchbare
# Bewertungsgrundlage, weder fuer das KGV selbst noch fuer die daraus
# abgeleitete 5-Jahres-Naeherung (Beispiel 27.07.2026: GPC mit KGV_aktuell
# 496.84 vs. KGV_forward 15.0 - beide Werte fuer sich genommen wenig
# aussagekraeftig, obwohl die Naeherungs-Rechnung technisch "funktioniert").
VERZERRUNGS_FAKTOR = 3.0

# Zweites Verzerrungs-Kriterium (NEU, 27.07.2026, Nutzerfall BF-B): ein
# starker JUENGSTER Gewinnrueckgang verzerrt die 5J-Naeherung auf eine Art,
# die der obige Faktor-Check NICHT immer erwischt - BF-B hatte KGV_aktuell
# 17.05 vs. KGV_forward 15.32 (Verhaeltnis 1.11, unauffaellig), aber
# Gewinnwachstum -62.7%: die Naeherung teilt historische Kurse durch den
# HEUTIGEN, frisch eingebrochenen Gewinn - das treibt den 5J-Schnitt
# rechnerisch nach oben, ohne dass eine echte Unterbewertung vorliegt (der
# nahezu identische KGV_forward zeigt, dass der Markt selbst keinen
# grossen Rabatt einpreist). Schwelle bewusst konservativ (-40%), um echte,
# aber moderate zyklische Gewinnschwankungen nicht faelschlich rauszuwerfen.
GEWINNRUECKGANG_VERZERRUNGS_SCHWELLE = -0.40


def ist_kgv_verzerrt(kgv_aktuell, kgv_forward):
    if kgv_aktuell is None or kgv_forward is None or kgv_aktuell <= 0 or kgv_forward <= 0:
        return False
    verhaeltnis = kgv_aktuell / kgv_forward
    return verhaeltnis > VERZERRUNGS_FAKTOR or verhaeltnis < (1 / VERZERRUNGS_FAKTOR)


def ist_gewinnbasis_verzerrt(gewinnwachstum):
    """Prueft das zweite Verzerrungs-Kriterium (siehe
    GEWINNRUECKGANG_VERZERRUNGS_SCHWELLE oben). gewinnwachstum wird hier als
    Bruch erwartet (z.B. -0.627 fuer -62.7%), nicht als Prozentzahl."""
    return gewinnwachstum is not None and gewinnwachstum < GEWINNRUECKGANG_VERZERRUNGS_SCHWELLE


def berechne_einstieg_stop_targets(ticker, aktueller_kurs, trailing_eps, kgv_naeherung_5j):
    """NEU (27.07.2026, Nutzerwunsch): ergaenzt die reine KGV-Bewertung um
    eine grobe Einstiegs-/Stop-Orientierung samt zwei TP-Varianten - wird
    NUR fuer Titel mit Bewertungs_Status = 'Guenstig' aufgerufen (siehe
    main()), nicht fuer alle 79, um die zusaetzlichen yfinance-Abrufe gering
    zu halten.

    Zwei unabhaengige TP-Logiken, bewusst beide ausgegeben (Nutzerwunsch,
    da beide Ansaetze unterschiedliche Fragen beantworten):
    - TP1_Bewertung/TP2_Bewertung: rein rechnerisch aus der KGV-Naeherung -
      TP1 = Kurs, bei dem das aktuelle KGV wieder dem eigenen 5J-Schnitt
      entspricht (die Rabatt-Luecke schliesst sich vollstaendig), TP2 =
      derselbe Kurs nochmal um die bestehende TEUER_SCHWELLE gestreckt
      (Uebertreibung nach oben, analog zur bestehenden Guenstig/Teuer-
      Logik). Setzt voraus, dass der heutige Gewinn pro Aktie halbwegs
      stabil bleibt - KEINE Prognose, nur eine rechnerische Rueck-
      Projektion derselben Naeherungs-Methodik wie das Rabatt-Feld.
    - Einstieg/Stop/TP1_Chart/TP2_Chart: rein charttechnisch aus dem
      1-Jahres-Kursverlauf (EMA50/EMA200/WMA200 als Unterstuetzungs-
      Zonen, 52-Wochen-Hoch als erstes chartechnisches Ziel) - unabhaengig
      von der Bewertung. Bewusst GROBER als bei den taeglichen Setups
      (kein Ichimoku/RSI/Divergenz-Apparat), da es hier nur um eine
      Orientierung fuer eine langfristige Position geht, nicht um ein
      praezises Kurzfrist-Timing.

    Gibt None zurueck, wenn nicht genug Kursdaten fuer die 200-Tage-
    Durchschnitte vorliegen (z.B. sehr junge Notierung) - dann bleiben die
    entsprechenden Felder in der Ausgabe leer, kein Fehler."""
    try:
        hist = get_yf_history(ticker)
        if not hist.empty:
            stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
            if getattr(hist.index, 'tz', None) is not None:
                stichtag = stichtag.tz_localize(hist.index.tz)
            hist = hist[hist.index >= stichtag]
        if hist.empty or "Close" not in hist.columns or len(hist) < 210:
            return None

        close = hist["Close"]
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        gewichte = list(range(1, len(close) + 1))[-200:]
        wma200 = float((close.iloc[-200:] * gewichte).sum() / sum(gewichte))
        hoch_52w = float(hist["High"].max())

        # Einstieg (chartbasiert): naechste Unterstuetzung UNTERHALB des
        # aktuellen Kurses unter den drei langfristigen Durchschnitten -
        # liegt der Kurs schon nah dran (<= 5%), gilt der aktuelle Kurs
        # selbst als guter Einstieg, sonst wird die Unterstuetzung als
        # Ruecksetzer-Zone vorgeschlagen.
        stuetzen_unten = [s for s in (ema50, ema200, wma200) if s <= aktueller_kurs]
        naechste_stuetze = max(stuetzen_unten) if stuetzen_unten else None

        if naechste_stuetze is not None and (aktueller_kurs - naechste_stuetze) / aktueller_kurs * 100 <= 5:
            einstieg_hinweis = f"Jetzt ({round(aktueller_kurs, 2)}) - nah an Unterstuetzung"
        elif naechste_stuetze is not None:
            einstieg_hinweis = f"Ruecksetzer abwarten (~{round(naechste_stuetze, 2)})"
        else:
            einstieg_hinweis = "Kurs liegt unter allen langfristigen Durchschnitten - Vorsicht, moeglicher Trendbruch"

        # Stop (chartbasiert): 5% unter der tieferen der beiden 200er-
        # Durchschnitte - ein nachhaltiger Bruch des langfristigen Trends
        # gilt hier als strukturelle Entwertung der These, nicht ein
        # kurzfristiger Ruecksetzer.
        stop_chart = round(min(ema200, wma200) * 0.95, 2)

        tp1_chart = round(hoch_52w, 2)
        tp2_chart = round(hoch_52w * 1.10, 2)

        tp1_bewertung = None
        tp2_bewertung = None
        if trailing_eps and trailing_eps > 0 and kgv_naeherung_5j and kgv_naeherung_5j > 0:
            tp1_bewertung = round(trailing_eps * kgv_naeherung_5j, 2)
            tp2_bewertung = round(trailing_eps * kgv_naeherung_5j * TEUER_SCHWELLE, 2)

        return {
            "Einstieg_Hinweis": einstieg_hinweis,
            "Stop_Chart": stop_chart,
            "TP1_Chart": tp1_chart,
            "TP2_Chart": tp2_chart,
            "TP1_Bewertung": tp1_bewertung,
            "TP2_Bewertung": tp2_bewertung,
        }
    except Exception as e:
        print(f"WARNUNG: Einstieg/Stop/TP-Berechnung fuer {ticker} fehlgeschlagen ({e}) - Felder bleiben leer.")
        return None


def analysiere_langfrist_titel(ticker, name, markt, sektor):
    try:
        t = yf.Ticker(ticker)
        info = t.info

        aktueller_kurs = sicheres_info_feld(info, "currentPrice") or sicheres_info_feld(info, "regularMarketPrice")
        kgv_aktuell = sicheres_info_feld(info, "trailingPE")
        kgv_forward = sicheres_info_feld(info, "forwardPE")
        kuv = sicheres_info_feld(info, "priceToSalesTrailing12Months")
        kbv = sicheres_info_feld(info, "priceToBook")
        dividendenrendite = sicheres_info_feld(info, "dividendYield")
        verschuldung_de = sicheres_info_feld(info, "debtToEquity")
        umsatzwachstum = sicheres_info_feld(info, "revenueGrowth")
        gewinnwachstum = sicheres_info_feld(info, "earningsGrowth")
        trailing_eps = sicheres_info_feld(info, "trailingEps")

        if aktueller_kurs is None or kgv_aktuell is None:
            print(f"DEBUG-LANGFRIST-UEBERSPRUNGEN: {ticker} -> Kurs oder KGV nicht verfuegbar, ueberspringe.")
            return None

        kgv_naeherung = berechne_naeherungs_kgv(t, aktueller_kurs, trailing_eps)

        verzerrt = ist_kgv_verzerrt(kgv_aktuell, kgv_forward) or ist_gewinnbasis_verzerrt(gewinnwachstum)
        rabatt_vs_5j_perc = None
        if verzerrt:
            # Trailing-Gewinn durch Einmaleffekt verzerrt (siehe
            # VERZERRUNGS_FAKTOR oben) - weder KGV_aktuell noch die daraus
            # abgeleitete 5J-Naeherung sind dann eine brauchbare
            # Bewertungsgrundlage, unabhaengig vom rechnerischen Verhaeltnis.
            bewertungs_status = "Nicht aussagekraeftig"
        else:
            bewertungs_status = "Neutral"
            if kgv_naeherung is not None and kgv_naeherung > 0:
                verhaeltnis = kgv_aktuell / kgv_naeherung
                rabatt_vs_5j_perc = round((1 - verhaeltnis) * 100, 2)
                if verhaeltnis < GUENSTIG_SCHWELLE:
                    bewertungs_status = "Guenstig"
                elif verhaeltnis > TEUER_SCHWELLE:
                    bewertungs_status = "Teuer"

        return {
            "Ticker": ticker,
            "Name": name,
            "Markt": markt,
            "Sektor": sektor,
            "Kurs": round(aktueller_kurs, 2),
            "KGV_aktuell": round(kgv_aktuell, 2),
            "KGV_Naeherung_5J": kgv_naeherung,
            "Rabatt_vs_5J_Perc": rabatt_vs_5j_perc,
            "Trailing_EPS": trailing_eps,
            "KGV_forward": round(kgv_forward, 2) if kgv_forward else None,
            "KUV": round(kuv, 2) if kuv else None,
            "KBV": round(kbv, 2) if kbv else None,
            "Dividendenrendite_Perc": normalisiere_dividendenrendite(dividendenrendite),
            "Verschuldung_DE": round(verschuldung_de, 1) if verschuldung_de else None,
            "Umsatzwachstum_Perc": round(umsatzwachstum * 100, 2) if umsatzwachstum is not None else None,
            "Gewinnwachstum_Perc": round(gewinnwachstum * 100, 2) if gewinnwachstum is not None else None,
            "Bewertungs_Status": bewertungs_status,
        }
    except Exception as e:
        print(f"FEHLER Langfrist-Analyse {ticker}: {e}")
        return None


def main():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    print("Langfrist-Bewertungs-Scanner gestartet...")
    print(f"Universum: {len(LANGFRIST_UNIVERSUM)} kuratierte Qualitaets-/Blue-Chip-Titel")

    ergebnisse = []
    for ticker, (name, markt, sektor) in LANGFRIST_UNIVERSUM.items():
        print(f"Analysiere {ticker}...")
        r = analysiere_langfrist_titel(ticker, name, markt, sektor)
        if r:
            ergebnisse.append(r)

    print(f"DEBUG: {len(ergebnisse)}/{len(LANGFRIST_UNIVERSUM)} Titel erfolgreich ausgewertet.")

    SPALTEN = [
        "Ticker", "Name", "Markt", "Sektor", "Kurs", "KGV_aktuell",
        "KGV_Naeherung_5J", "Rabatt_vs_5J_Perc", "KGV_forward", "KUV", "KBV",
        "Dividendenrendite_Perc", "Verschuldung_DE", "Umsatzwachstum_Perc",
        "Gewinnwachstum_Perc", "Bewertungs_Status",
        "Einstieg_Hinweis", "Stop_Chart", "TP1_Chart", "TP2_Chart",
        "TP1_Bewertung", "TP2_Bewertung",
    ]
    # DataFrame zunaechst mit ALLEN Schluesseln aus ergebnisse aufbauen
    # (inkl. Trailing_EPS, das intern fuer die Guenstig-Zielberechnung
    # unten gebraucht wird, aber nicht in die finale CSV soll) - danach
    # fehlende Ziel-Spalten (nur fuer Guenstig-Kandidaten befuellt)
    # nachruesten.
    df = pd.DataFrame(ergebnisse)
    for spalte in SPALTEN + ["Trailing_EPS"]:
        if spalte not in df.columns:
            df[spalte] = None

    if not df.empty:
        rang = {"Guenstig": 0, "Neutral": 1, "Teuer": 2, "Nicht aussagekraeftig": 3}
        df["_rang"] = df["Bewertungs_Status"].map(rang).fillna(4)
        df = df.sort_values(by=["_rang", "Rabatt_vs_5J_Perc"], ascending=[True, False]).drop(columns=["_rang"])

        # Einstieg/Stop/TP-Ziele (NEU, 27.07.2026): nur fuer die tatsaechlich
        # interessanten Guenstig-Kandidaten berechnen (zusaetzlicher
        # yfinance-Abruf pro Titel) statt fuer alle 79 - siehe
        # berechne_einstieg_stop_targets fuer die Methodik.
        guenstig_idx = df.index[df["Bewertungs_Status"] == "Guenstig"]
        print(f"DEBUG: Berechne Einstieg/Stop/TP-Ziele fuer {len(guenstig_idx)} Guenstig-Kandidaten...")
        for idx in guenstig_idx:
            ticker = df.at[idx, "Ticker"]
            targets = berechne_einstieg_stop_targets(
                ticker, df.at[idx, "Kurs"], df.at[idx, "Trailing_EPS"], df.at[idx, "KGV_Naeherung_5J"]
            )
            if targets:
                for feld, wert in targets.items():
                    df.at[idx, feld] = wert

    df = df[SPALTEN]

    dateiname_csv = f"Langfrist_Bewertung({today}).csv"
    df.to_csv(dateiname_csv, index=False, sep=';', encoding='utf-8-sig')
    print(f"Gespeichert: {dateiname_csv}")

    dateiname_briefing = f"Langfrist_Briefing({today}).txt"
    with open(dateiname_briefing, "w", encoding="utf-8") as f:
        f.write(f"LANGFRIST-BEWERTUNG {today}\n" + "=" * 50 + "\n\n")
        f.write("STRATEGIE-ANSATZ (Langfrist-Bewertung, separat von Trendfolge- und Trendwende-Scanner)\n")
        f.write("-" * 50 + "\n")
        f.write("- Grundidee: Keine kurzfristige Trade-Idee, sondern fundamentale Bewertung fuer\n")
        f.write("  eine LANGFRISTIGE Positionierung (Halten ueber Monate/Jahre).\n")
        f.write("- Universum: kuratierte Liste bekannter Qualitaets-/Blue-Chip-Aktien (nicht das\n")
        f.write("  breite Sektoren-Universum der anderen Scanner) - robustere Datenlage, und eine\n")
        f.write("  KGV-Bewertung ist bei zyklischen/verlustschreibenden Werten ohnehin nicht\n")
        f.write("  aussagekraeftig.\n")
        f.write("- WICHTIGE EINSCHRAENKUNG: KGV_Naeherung_5J ist KEINE echte historische KGV-\n")
        f.write("  Reihe (dafuer fehlen zuverlaessige historische Quartalsgewinne). Stattdessen:\n")
        f.write("  heutiger Gewinn pro Aktie angewendet auf die Kursverlaeufe der letzten 5 Jahre -\n")
        f.write("  zeigt, ob der aktuelle Kurs guenstig/teuer relativ zur eigenen 5-Jahres-\n")
        f.write("  Handelsspanne ist (mit heutiger Ertragskraft gerechnet).\n")
        f.write(f"- Bewertungs_Status: 'Guenstig' wenn aktuelles KGV < {int(GUENSTIG_SCHWELLE*100)}% der Naeherung,\n")
        f.write(f"  'Teuer' wenn > {int(TEUER_SCHWELLE*100)}%, sonst 'Neutral'.\n")
        f.write(f"- Rabatt_vs_5J_Perc (NEU): direkter Prozentwert, wie weit das aktuelle KGV unter\n")
        f.write(f"  (positiv) bzw. ueber (negativ) dem eigenen 5-Jahres-Schnitt liegt - macht die\n")
        f.write(f"  Bewertungs_Status-Kategorie konkret vergleichbar statt nur einzuteilen.\n")
        f.write(f"- Verzerrungs-Filter (NEU, zwei Kriterien): der Titel wird als 'Nicht aussagekraeftig'\n")
        f.write(f"  markiert statt faelschlich Guenstig/Teuer einzustufen, wenn (1) aktuelles KGV und\n")
        f.write(f"  Forward-KGV um mehr als Faktor {VERZERRUNGS_FAKTOR:g} voneinander abweichen (deutet auf einen\n")
        f.write(f"  Einmaleffekt in den Trailing-Earnings hin, z.B. Abschreibung/Sondergewinn), ODER\n")
        f.write(f"  (2) das Gewinnwachstum unter {int(GEWINNRUECKGANG_VERZERRUNGS_SCHWELLE*100)}% liegt (ein juengster\n")
        f.write(f"  Gewinneinbruch verzerrt die 5J-Naeherung nach oben, da historische Kurse durch den\n")
        f.write(f"  heutigen, gedrueckten Gewinn geteilt werden - Beispielfall 27.07.2026: BF-B mit\n")
        f.write(f"  KGV_aktuell nah am KGV_forward, aber -62.7% Gewinnwachstum). In beiden Faellen ist\n")
        f.write(f"  der aktuelle Gewinn pro Aktie keine brauchbare Bewertungsgrundlage.\n")
        f.write(f"- Einstieg/Stop/TP1/TP2 (NEU, NUR fuer Guenstig-Kandidaten berechnet): grobe\n")
        f.write(f"  Orientierung aus dem 1-Jahres-Kursverlauf (EMA50/EMA200/WMA200 als Stuetzen,\n")
        f.write(f"  52-Wochen-Hoch als Chart-Ziel) - deutlich grober als bei den taeglichen Setups\n")
        f.write(f"  (kein Ichimoku/RSI/Divergenz-Apparat), da es hier nur um eine Orientierung fuer\n")
        f.write(f"  eine langfristige Position geht, nicht um praezises Kurzfrist-Timing. ZWEI\n")
        f.write(f"  unabhaengige TP-Varianten: TP1/TP2_Bewertung = rechnerische Rueck-Projektion aus\n")
        f.write(f"  der KGV-Naeherung (Kurs, bei dem sich die Rabatt-Luecke schliesst, bzw. leicht\n")
        f.write(f"  darueber hinaus), TP1/TP2_Chart = charttechnisch aus dem 52-Wochen-Hoch. Beide\n")
        f.write(f"  koennen stark auseinanderliegen - das ist normal, sie beantworten unterschiedliche\n")
        f.write(f"  Fragen (Bewertungs-Normalisierung vs. Chart-Widerstand).\n")
        f.write("- Kein Stop, kein Kursziel, keine CRV-Angabe - das ist bewusst kein Trade-Setup,\n")
        f.write("  sondern eine Bewertungs-Uebersicht zur eigenen Weiterrecherche.\n\n")

        # BESTANDS-STATISTIK (NEU 28.07.2026, Pendant zur Funnel-Statistik
        # der anderen Scanner): Der Langfrist-Scanner verwirft keine Titel
        # (kein Ablehnungs-Trichter wie bei den Setup-Scannern), sondern
        # klassifiziert das komplette Universum - die aussagekraeftige
        # Statistik ist daher die VERTEILUNG auf die Bewertungsstufen plus
        # die Zahl der Titel ohne verwertbare Daten. So ist sofort ablesbar,
        # ob z. B. "nur 2 Guenstig-Titel" an einem teuren Gesamtmarkt liegt
        # oder an vielen Datenluecken.
        uebersprungen = len(LANGFRIST_UNIVERSUM) - len(ergebnisse)
        f.write("BESTANDS-STATISTIK (Verteilung statt Ablehnungs-Funnel)\n")
        f.write("-" * 50 + "\n")
        f.write(f"Universum: {len(LANGFRIST_UNIVERSUM)} Titel | Keine Daten (Kurs/KGV fehlt oder Fehler): {uebersprungen} | Bewertet: {len(ergebnisse)}\n")
        # BEINAHE-GUENSTIG (NEU 30.07.2026, Nutzerwunsch "bei 0 Treffern zeigen,
        # woran es scheiterte" - hier sinngemaess uebertragen): Der Langfrist-
        # Scanner verwirft nichts, aber in die Auswertung gehen nur die
        # Guenstig-Titel. Das Pendant zum Beinahe-Kandidaten ist deshalb der
        # Titel, der die Guenstig-Schwelle KNAPP verpasst hat - also im
        # Neutral-Bereich, aber dicht an der Grenze. Ohne diese Liste bliebe
        # an einem Tag ohne Guenstig-Titel voellig offen, ob der Markt weit
        # entfernt oder haarscharf daneben war.
        if not df.empty:
            _grenze_perc = round((1 - GUENSTIG_SCHWELLE) * 100, 1)
            _beinahe = df[(df["Bewertungs_Status"] == "Neutral")
                          & (df["Rabatt_vs_5J_Perc"].notna())
                          & (df["Rabatt_vs_5J_Perc"] >= _grenze_perc - 5)]
            if not _beinahe.empty:
                f.write(f"BEINAHE GUENSTIG (Rabatt vs. 5J-Naeherung innerhalb von 5 Punkten "
                        f"unter der Guenstig-Schwelle von {_grenze_perc}%)\n")
                f.write("-" * 50 + "\n")
                f.write("(nur Beobachtung, KEINE Kandidaten - zeigt, wie knapp die Schwelle verfehlt wurde)\n")
                for _, _z in _beinahe.sort_values("Rabatt_vs_5J_Perc", ascending=False).iterrows():
                    f.write(f"{_z['Name']} ({_z['Ticker']}): Rabatt {_z['Rabatt_vs_5J_Perc']}% "
                            f"(Schwelle {_grenze_perc}%)\n")
                f.write("\n")

        if not df.empty:
            _verteilung = df["Bewertungs_Status"].value_counts()
            _teile = [f"{_stufe}: {int(_verteilung.get(_stufe, 0))}"
                      for _stufe in ["Guenstig", "Neutral", "Teuer", "Nicht aussagekraeftig"]]
            f.write("Verteilung: " + " | ".join(_teile) + "\n")
            f.write(f"=> In der Auswertung erscheinen nur die {int(_verteilung.get('Guenstig', 0))} Guenstig-Titel (siehe Master-Anweisung Abschnitt 6).\n")
        f.write("\n")

        if df.empty:
            f.write("Keine Titel erfolgreich ausgewertet.\n")
        else:
            for _, row in df.iterrows():
                rabatt = row['Rabatt_vs_5J_Perc']
                rabatt_text = f"{rabatt}%" if pd.notna(rabatt) else "N/A"
                f.write(
                    f"{row['Ticker']} ({row['Name']}) | Markt: {row['Markt']} | Sektor: {row['Sektor']}\n"
                    f"Kurs: {row['Kurs']}\n"
                    f"KGV aktuell: {row['KGV_aktuell']} | KGV-Naeherung (5J): {row['KGV_Naeherung_5J']} | Rabatt vs. 5J-Schnitt: {rabatt_text} | Bewertung: {row['Bewertungs_Status']}\n"
                    f"KGV forward: {row['KGV_forward']} | KUV: {row['KUV']} | KBV: {row['KBV']}\n"
                    f"Dividendenrendite: {row['Dividendenrendite_Perc']}% | Verschuldung (D/E): {row['Verschuldung_DE']}\n"
                    f"Umsatzwachstum: {row['Umsatzwachstum_Perc']}% | Gewinnwachstum: {row['Gewinnwachstum_Perc']}%\n"
                )
                if pd.notna(row['Einstieg_Hinweis']):
                    f.write(
                        f"Einstieg: {row['Einstieg_Hinweis']} | Stop (Chart): {row['Stop_Chart']}\n"
                        f"TP1 (Bewertung): {row['TP1_Bewertung']} | TP2 (Bewertung): {row['TP2_Bewertung']}\n"
                        f"TP1 (Chart): {row['TP1_Chart']} | TP2 (Chart): {row['TP2_Chart']}\n"
                    )
                f.write("\n")

    print(f"Gespeichert: {dateiname_briefing}")
    print("Langfrist-Bewertungs-Scanner abgeschlossen.")


if __name__ == "__main__":
    main()
