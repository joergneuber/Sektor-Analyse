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

        bewertungs_status = "Neutral"
        if kgv_naeherung is not None and kgv_naeherung > 0:
            verhaeltnis = kgv_aktuell / kgv_naeherung
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
        "KGV_Naeherung_5J", "KGV_forward", "KUV", "KBV",
        "Dividendenrendite_Perc", "Verschuldung_DE", "Umsatzwachstum_Perc",
        "Gewinnwachstum_Perc", "Bewertungs_Status",
    ]
    df = pd.DataFrame(ergebnisse, columns=SPALTEN)
    if not df.empty:
        rang = {"Guenstig": 0, "Neutral": 1, "Teuer": 2}
        df["_rang"] = df["Bewertungs_Status"].map(rang).fillna(3)
        df = df.sort_values(by=["_rang", "KGV_aktuell"], ascending=[True, True]).drop(columns=["_rang"])

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
        f.write("- Kein Stop, kein Kursziel, keine CRV-Angabe - das ist bewusst kein Trade-Setup,\n")
        f.write("  sondern eine Bewertungs-Uebersicht zur eigenen Weiterrecherche.\n\n")

        if df.empty:
            f.write("Keine Titel erfolgreich ausgewertet.\n")
        else:
            for _, row in df.iterrows():
                f.write(
                    f"{row['Ticker']} ({row['Name']}) | Markt: {row['Markt']} | Sektor: {row['Sektor']}\n"
                    f"Kurs: {row['Kurs']}\n"
                    f"KGV aktuell: {row['KGV_aktuell']} | KGV-Näherung (5J): {row['KGV_Naeherung_5J']} | Bewertung: {row['Bewertungs_Status']}\n"
                    f"KGV forward: {row['KGV_forward']} | KUV: {row['KUV']} | KBV: {row['KBV']}\n"
                    f"Dividendenrendite: {row['Dividendenrendite_Perc']}% | Verschuldung (D/E): {row['Verschuldung_DE']}\n"
                    f"Umsatzwachstum: {row['Umsatzwachstum_Perc']}% | Gewinnwachstum: {row['Gewinnwachstum_Perc']}%\n\n"
                )

    print(f"Gespeichert: {dateiname_briefing}")
    print("Langfrist-Bewertungs-Scanner abgeschlossen.")


if __name__ == "__main__":
    main()
