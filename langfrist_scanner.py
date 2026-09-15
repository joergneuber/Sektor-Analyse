"""
langfrist_scanner.py

Dritte, eigenstaendige Scanner-Kategorie neben dem Trendfolge-Scanner
(analyse.py) und dem Trendwende-Scanner (trendwende_scanner.py): sucht NICHT
nach kurzfristigen technischen Setups. Er stellt fuer eine kuratierte
Liste bekannter Qualitaets-/Blue-Chip-Aktien eine deterministische
Faktenlage aus Fundamentaldaten und langfristigem Chart-Kontext bereit -
fuer eine langfristige Positionierung (Halten ueber Monate/Jahre), nicht
fuer kurzfristige Trades. Die Interpretation der Fakten erfolgt ausserhalb
dieses Scanners.

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


def sicheres_info_feld(info, feld, default=None):
    wert = info.get(feld, default)
    if wert is None:
        return default
    try:
        return float(wert)
    except (ValueError, TypeError):
        return default


def normalisiere_dividendenrendite(wert):
    """Normalisiert Yahoo/yfinance dividendYield auf Prozent.

    Das Feld kann je nach Datenquelle/Version als Bruch (0.037 = 3,7 %)
    oder bereits als Prozent (3.7 = 3,7 %) vorliegen. Fehlende Daten bleiben
    bewusst None: fehlende Dividende ist nicht dasselbe wie 0 %.
    """
    if wert is None:
        return None
    if wert < 0:
        return None
    if wert > 1:
        return round(wert, 2)
    return round(wert * 100, 2)


def ermittle_dividendenrendite(info, aktueller_kurs):
    """Ermittelt die Dividendenrendite bevorzugt aus Dividende/Kurs.

    dividendRate bzw. trailingAnnualDividendRate sind absolute
    Dividendenbeträge je Aktie und damit robuster als das teilweise
    inkonsistente dividendYield-Feld. dividendYield bleibt Fallback.
    """
    if aktueller_kurs is not None and aktueller_kurs > 0:
        for feld in ("dividendRate", "trailingAnnualDividendRate"):
            dividende = sicheres_info_feld(info, feld)
            if dividende is not None and dividende >= 0:
                return round(dividende / aktueller_kurs * 100, 2)
    return normalisiere_dividendenrendite(sicheres_info_feld(info, "dividendYield"))


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



def berechne_langfrist_chart_fakten(ticker, aktueller_kurs):
    """Ermittelt deterministische Chart-Fakten fuer den langfristigen Kontext.

    Keine Einstieg-, Stop-, TP- oder Bewertungsentscheidung: Es werden nur
    Kurs-/Durchschnittswerte und daraus direkt berechenbare Relationen
    ausgegeben. Der gemeinsame Cache liefert period="max"; fuer die
    langfristige Chart-Kontextlogik werden trotzdem nur die letzten 365
    Kalendertage verwendet, damit 52-Wochen-Werte und Durchschnitte auf
    derselben Zeitbasis beruhen.
    """
    try:
        hist = get_yf_history(ticker)
        if hist.empty or "Close" not in hist.columns:
            return None

        stichtag = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=365))
        if getattr(hist.index, "tz", None) is not None:
            stichtag = stichtag.tz_localize(hist.index.tz)
        hist = hist[hist.index >= stichtag]
        if hist.empty or len(hist) < 240:
            return None

        close = hist["Close"]
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        sma200 = float(close.iloc[-200:].mean())
        sma200_vor_20 = float(close.iloc[-220:-20].mean())
        sma200_vor_40 = float(close.iloc[-240:-40].mean())
        hoch_52w = float(hist["High"].max()) if "High" in hist.columns else None
        tief_52w = float(hist["Low"].min()) if "Low" in hist.columns else None

        fakten = {
            "EMA50": round(ema50, 2),
            "SMA200": round(sma200, 2),
            "SMA200_vor_20_Tagen": round(sma200_vor_20, 2),
            "SMA200_vor_40_Tagen": round(sma200_vor_40, 2),
            "52W_Hoch": round(hoch_52w, 2) if hoch_52w is not None else None,
            "52W_Tief": round(tief_52w, 2) if tief_52w is not None else None,
            "Kurs_vs_SMA200_Perc": round((aktueller_kurs / sma200 - 1) * 100, 2) if sma200 else None,
            "Kurs_vs_EMA50_Perc": round((aktueller_kurs / ema50 - 1) * 100, 2) if ema50 else None,
            "SMA200_Veraenderung_20T_Perc": round((sma200 / sma200_vor_20 - 1) * 100, 2) if sma200_vor_20 else None,
            "SMA200_Veraenderung_40T_Perc": round((sma200 / sma200_vor_40 - 1) * 100, 2) if sma200_vor_40 else None,
            "Kurs_vs_52W_Hoch_Perc": round((aktueller_kurs / hoch_52w - 1) * 100, 2) if hoch_52w else None,
            "Kurs_vs_52W_Tief_Perc": round((aktueller_kurs / tief_52w - 1) * 100, 2) if tief_52w else None,
            "EMA50_vs_SMA200_Perc": round((ema50 / sma200 - 1) * 100, 2) if sma200 else None,
        }
        return fakten
    except Exception as e:
        print(f"WARNUNG: Langfrist-Chartdaten fuer {ticker} fehlgeschlagen ({e}) - Felder bleiben leer.")
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
        dividendenrendite = ermittle_dividendenrendite(info, aktueller_kurs)
        verschuldung_de = sicheres_info_feld(info, "debtToEquity")
        umsatzwachstum = sicheres_info_feld(info, "revenueGrowth")
        gewinnwachstum = sicheres_info_feld(info, "earningsGrowth")
        trailing_eps = sicheres_info_feld(info, "trailingEps")

        if aktueller_kurs is None:
            print(f"DEBUG-LANGFRIST-UEBERSPRUNGEN: {ticker} -> Kurs nicht verfuegbar, ueberspringe.")
            return None

        kgv_naeherung = berechne_naeherungs_kgv(t, aktueller_kurs, trailing_eps)

        kgv_abweichung_perc = None
        if kgv_aktuell is not None and kgv_naeherung is not None and kgv_naeherung > 0:
            kgv_abweichung_perc = round((kgv_aktuell / kgv_naeherung - 1) * 100, 2)

        chart_fakten = berechne_langfrist_chart_fakten(ticker, aktueller_kurs) or {}

        return {
            "Ticker": ticker,
            "Name": name,
            "Markt": markt,
            "Sektor": sektor,
            "Kurs": round(aktueller_kurs, 2),
            "KGV_aktuell": round(kgv_aktuell, 2) if kgv_aktuell is not None else None,
            "KGV_Naeherung_5J": kgv_naeherung,
            "KGV_Aktuell_vs_Naeherung_Perc": kgv_abweichung_perc,
            "Trailing_EPS": trailing_eps,
            "KGV_forward": round(kgv_forward, 2) if kgv_forward is not None else None,
            "KUV": round(kuv, 2) if kuv is not None else None,
            "KBV": round(kbv, 2) if kbv is not None else None,
            "Dividendenrendite_Perc": dividendenrendite,
            "Verschuldung_DE": round(verschuldung_de, 1) if verschuldung_de is not None else None,
            "Umsatzwachstum_Perc": round(umsatzwachstum * 100, 2) if umsatzwachstum is not None else None,
            "Gewinnwachstum_Perc": round(gewinnwachstum * 100, 2) if gewinnwachstum is not None else None,
            **chart_fakten,
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
        "Ticker", "Name", "Markt", "Sektor", "Kurs",
        "KGV_aktuell", "KGV_Naeherung_5J", "KGV_Aktuell_vs_Naeherung_Perc",
        "Trailing_EPS", "KGV_forward", "KUV", "KBV", "Dividendenrendite_Perc",
        "Verschuldung_DE", "Umsatzwachstum_Perc", "Gewinnwachstum_Perc",
        "EMA50", "SMA200", "SMA200_vor_20_Tagen", "SMA200_vor_40_Tagen",
        "52W_Hoch", "52W_Tief", "Kurs_vs_SMA200_Perc", "Kurs_vs_EMA50_Perc",
        "SMA200_Veraenderung_20T_Perc", "SMA200_Veraenderung_40T_Perc",
        "Kurs_vs_52W_Hoch_Perc", "Kurs_vs_52W_Tief_Perc", "EMA50_vs_SMA200_Perc",
    ]

    df = pd.DataFrame(ergebnisse)
    for spalte in SPALTEN + ["Trailing_EPS"]:
        if spalte not in df.columns:
            df[spalte] = None

    if not df.empty:
        # Keine Bewertungs-/Kandidaten-Sortierung: Reihenfolge des kuratierten
        # Universums bleibt erhalten. Der Scanner nimmt keine Interpretation vor.
        df = df[SPALTEN]

    dateiname_csv = f"Langfrist_Bewertung({today}).csv"
    df.to_csv(dateiname_csv, index=False, sep=';', encoding='utf-8-sig')
    print(f"Gespeichert: {dateiname_csv}")

    dateiname_briefing = f"Langfrist_Briefing({today}).txt"
    with open(dateiname_briefing, "w", encoding="utf-8") as f:
        f.write(f"LANGFRIST-BEWERTUNG {today}\n" + "=" * 50 + "\n\n")
        f.write("FAKTENBASIERTE LANGFRIST-ANALYSE (Fundamental x Long-Term-Chart)\n")
        f.write("-" * 50 + "\n")
        f.write("- Grundidee: Keine kurzfristige Trade-Idee und keine vorweggenommene\n")
        f.write("  Investment-Interpretation. Der Scanner stellt eine deterministische\n")
        f.write("  Faktenlage aus Fundamentaldaten und langfristigem Chart-Kontext bereit.\n")
        f.write("- FUNDAMENTALDATEN: aktuelles KGV, 5J-KGV-Naeherung auf Basis des heutigen\n")
        f.write("  EPS, KGV-Abweichung zur Naeherung, Forward-KGV, KUV, KBV,\n")
        f.write("  Dividendenrendite, Verschuldung sowie Umsatz- und Gewinnwachstum.\n")
        f.write("- WICHTIGE EINSCHRAENKUNG: KGV_Naeherung_5J ist KEINE echte historische KGV-\n")
        f.write("  Reihe. Sie entsteht durch Anwendung des heutigen EPS auf historische Kurse\n")
        f.write("  der letzten 5 Jahre und ist damit ein rechnerischer Vergleichswert.\n")
        f.write("- CHARTDATEN: EMA50, SMA200, SMA200-Werte vor 20/40 Handelstagen,\n")
        f.write("  52-Wochen-Hoch/-Tief sowie rein mathematische Kurs-/Durchschnitts-\n")
        f.write("  und Abstandsrelationen. Es werden keine Trend-, Kauf-, Verkaufs-,\n")
        f.write("  Value-Trap- oder sonstigen Investmenturteile vorweggenommen.\n")
        f.write("- Keine Bewertungs-Klassifizierung, keine automatische Kandidatenauswahl,\n")
        f.write("  kein Einstiegssignal, kein Stop, kein Kursziel und kein CRV. Die\n")
        f.write("  Interpretation der Fakten erfolgt in der nachgelagerten Auswertung.\n\n")

        # BESTANDS-STATISTIK: nur Datenverfuegbarkeit, keine Bewertungs-
        # oder Kandidatenklassifizierung.
        uebersprungen = len(LANGFRIST_UNIVERSUM) - len(ergebnisse)
        f.write("BESTANDS-STATISTIK (Datenverfuegbarkeit)\n")
        f.write("-" * 50 + "\n")
        f.write(f"Universum: {len(LANGFRIST_UNIVERSUM)} Titel | Keine Daten (Kurs/KGV fehlt oder Fehler): {uebersprungen} | Faktenbasis: {len(ergebnisse)}\n")
        f.write("\n")

        if df.empty:
            f.write("Keine Titel erfolgreich ausgewertet.\n")
        else:
            for _, row in df.iterrows():
                def _text(wert, suffix=""):
                    return "N/A" if pd.isna(wert) else f"{wert}{suffix}"

                f.write(
                    f"{row['Ticker']} ({row['Name']}) | Markt: {row['Markt']} | Sektor: {row['Sektor']}\n"
                    f"Kurs: {row['Kurs']}\n"
                    f"KGV aktuell: {_text(row['KGV_aktuell'])} | KGV-Naeherung (5J): {_text(row['KGV_Naeherung_5J'])} | KGV aktuell vs. Naeherung: {_text(row['KGV_Aktuell_vs_Naeherung_Perc'], '%')}\n"
                    f"Trailing EPS: {_text(row['Trailing_EPS'])} | KGV forward: {_text(row['KGV_forward'])} | KUV: {_text(row['KUV'])} | KBV: {_text(row['KBV'])}\n"
                    f"Dividendenrendite: {_text(row['Dividendenrendite_Perc'], '%')} | Verschuldung (D/E): {_text(row['Verschuldung_DE'])}\n"
                    f"Umsatzwachstum: {_text(row['Umsatzwachstum_Perc'], '%')} | Gewinnwachstum: {_text(row['Gewinnwachstum_Perc'], '%')}\n"
                    f"EMA50: {_text(row['EMA50'])} | SMA200: {_text(row['SMA200'])}\n"
                    f"SMA200 vor 20 Tagen: {_text(row['SMA200_vor_20_Tagen'])} | SMA200 vor 40 Tagen: {_text(row['SMA200_vor_40_Tagen'])}\n"
                    f"52W-Hoch: {_text(row['52W_Hoch'])} | 52W-Tief: {_text(row['52W_Tief'])}\n"
                    f"Kurs vs. SMA200: {_text(row['Kurs_vs_SMA200_Perc'], '%')} | Kurs vs. EMA50: {_text(row['Kurs_vs_EMA50_Perc'], '%')}\n"
                    f"SMA200-Veraenderung 20T: {_text(row['SMA200_Veraenderung_20T_Perc'], '%')} | SMA200-Veraenderung 40T: {_text(row['SMA200_Veraenderung_40T_Perc'], '%')}\n"
                    f"Kurs vs. 52W-Hoch: {_text(row['Kurs_vs_52W_Hoch_Perc'], '%')} | Kurs vs. 52W-Tief: {_text(row['Kurs_vs_52W_Tief_Perc'], '%')}\n"
                    f"EMA50 vs. SMA200: {_text(row['EMA50_vs_SMA200_Perc'], '%')}\n\n"
                )

    print(f"Gespeichert: {dateiname_briefing}")
    print("Langfrist-Bewertungs-Scanner abgeschlossen.")


if __name__ == "__main__":
    main()
