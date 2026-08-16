"""
Makro-Szenario-Daten fuer Neuber Macro & Markets.

Zweck:
  Liefert einen separaten, taeglich aktualisierten Datensatz fuer die
  spaetere Makro-Interpretation durch Gemini. Dieses Modul ist rein
  informativ und greift NICHT in Setup-, CRV-, Score- oder Portfolio-Logik ein.

Datenquellen:
  FRED (oeffentliche CSV-Route, kein API-Key), yfinance fuer Markt-/Rohstoff-
  preise. Wo ein einzelner Rohstoff keinen stabilen Yahoo-Futures-Ticker hat,
  wird ein klar gekennzeichneter Proxy verwendet oder der Wert als nicht
  verfuegbar ausgegeben.
"""

import datetime as dt
import os
import math
import json
import requests
import pandas as pd
import yfinance as yf

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

FRED_SERIES = {
    # Geldpolitik / Liquiditaet
    "Fed Funds Effective Rate": "DFF",
    "ECB Deposit Facility Rate": "ECBDFR",
    "M2": "M2SL",
    "Realzins 10Y TIPS": "DFII10",
    # Inflation / Preise
    "CPI": "CPIAUCSL",
    "Core CPI": "CPILFESL",
    "PCE": "PCEPI",
    "Core PCE": "PCEPILFE",
    "PPI": "PPIACO",
    "Durchschnittlicher Stundenlohn": "CES0500000003",
    # Konjunktur / Arbeit
    "Reales BIP": "GDPC1",
    "Arbeitslosenquote": "UNRATE",
    "NFP / Nonfarm Payrolls": "PAYEMS",
    "JOLTS Job Openings": "JTSJOL",
    "Initial Jobless Claims": "ICSA",
    "Industrieproduktion": "INDPRO",
    "Kapazitaetsauslastung": "TCU",
    "Consumer Sentiment": "UMCSENT",
    # Kredit / Financial Conditions
    "SLOOS C&I Tightening": "DRTSCILM",
    "US High Yield OAS": "BAMLH0A0HYM2",
    "US Investment Grade OAS": "BAMLC0A0CM",
    "Chicago Fed NFCI": "NFCI",
    # Exogen / Unsicherheit / Fiskal
    "GSCPI": "GSCPI",
    "Global Economic Policy Uncertainty": "GEPUCURRENT",
    "US Federal Debt/GDP": "GFDEGDQ188S",
}

MARKET_DATA = {
    # Aktien
    "S&P 500": ("^GSPC", "Markt"),
    "Nasdaq Composite": ("^IXIC", "Markt"),
    "Russell 2000": ("^RUT", "Markt"),
    "DAX": ("^GDAXI", "Markt"),
    "EuroStoxx 50": ("^STOXX50E", "Markt"),
    "Nikkei 225": ("^N225", "Markt"),
    # Volatilitaet / FX / Krypto
    "VIX": ("^VIX", "Risiko"),
    "DXY": ("DX-Y.NYB", "FX"),
    "EUR/USD": ("EURUSD=X", "FX"),
    "USD/JPY": ("JPY=X", "FX"),
    "Bitcoin": ("BTC-USD", "Krypto"),
    "Ethereum": ("ETH-USD", "Krypto"),
    # Energie
    "WTI": ("CL=F", "Energie"),
    "Brent": ("BZ=F", "Energie"),
    "Erdgas": ("NG=F", "Energie"),
    # Edelmetalle
    "Gold": ("GC=F", "Edelmetall"),
    "Silber": ("SI=F", "Edelmetall"),
    "Platin": ("PL=F", "Edelmetall"),
    "Palladium": ("PA=F", "Edelmetall"),
    # Industriemetalle / Zukunftsinfrastruktur
    "Kupfer": ("HG=F", "Industrie"),
    "Aluminium": ("ALI=F", "Industrie"),
    # Bei diesen drei Yahoo-Tickern ist die Verfuegbarkeit wechselhaft;
    # deshalb stehen robuste ETF-Proxies als Fallback bereit.
    "Zink": ("ZNC=F", "Industrie"),
    "Nickel": ("NICKEL.L", "Industrie-Proxy"),
    "Blei": ("LEAD.L", "Industrie-Proxy"),
    "Zinn": ("TIN.L", "Industrie-Proxy"),
    "Kobalt": ("COBALT.L", "Industrie-Proxy"),
    "Lithium": ("LIT", "Industrie-Proxy"),
    "Eisenerz": ("TIO=F", "Industrie"),
}


def _clean_num(value):
    try:
        value = float(value)
        return None if not math.isfinite(value) else value
    except Exception:
        return None


def fred_series(series_id, limit_days=5000):
    url = FRED_BASE.format(series_id)
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        from io import StringIO
        df = pd.read_csv(StringIO(r.text))
        if "DATE" not in df.columns or series_id not in df.columns:
            return pd.DataFrame()
        df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
        df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
        df = df.dropna(subset=["DATE", series_id]).sort_values("DATE")
        if limit_days:
            cutoff = pd.Timestamp.today() - pd.Timedelta(days=limit_days)
            df = df[df["DATE"] >= cutoff]
        return df
    except Exception as e:
        print(f"WARNUNG: FRED {series_id} nicht verfuegbar: {e}")
        return pd.DataFrame()


def fred_snapshot(name, series_id):
    df = fred_series(series_id)
    if df.empty:
        return f"{name}: Daten nicht verfuegbar (FRED {series_id})"
    vals = df[series_id].tolist()
    dates = df["DATE"].tolist()
    current = _clean_num(vals[-1])
    current_date = dates[-1].strftime("%Y-%m-%d")
    def ago(n):
        if len(vals) <= n:
            return None
        return _clean_num(vals[-1-n])
    p1 = ago(1)
    # FRED-Reihen haben unterschiedliche Frequenzen; deshalb werden zusaetzlich
    # 30/90/365 Kalendertage ueber den letzten verfuegbaren Punkt gesucht.
    def nearest_days(days):
        target = dates[-1] - pd.Timedelta(days=days)
        candidates = df[df["DATE"] <= target]
        return _clean_num(candidates[series_id].iloc[-1]) if not candidates.empty else None
    p30, p90, p365 = nearest_days(30), nearest_days(90), nearest_days(365)
    def pct(a, b):
        if a is None or b in (None, 0): return None
        return (a/b - 1.0) * 100.0
    changes = []
    for label, old in (("30T", p30), ("90T", p90), ("1J", p365)):
        ch = pct(current, old)
        if ch is not None:
            changes.append(f"{label} {ch:+.2f}%")
    # Rohwert kompakt, ohne eine scheinbare Praezision fuer alle Reihen zu erzwingen.
    if abs(current) >= 1000:
        value_text = f"{current:,.1f}"
    elif abs(current) >= 10:
        value_text = f"{current:.2f}"
    else:
        value_text = f"{current:.3f}"
    return f"{name}: {value_text} | Datenstand {current_date} | " + (" | ".join(changes) if changes else "keine Vergleichswerte") + f" | FRED {series_id}"


def fedwatch_snapshot():
    """Optionaler CME-FedWatch-Hook.

    Die offizielle CME-FedWatch-API ist entgeltpflichtig/entitled. Deshalb
    wird ohne Secret bewusst NICHT versucht, eine instabile Webseiten-
    Darstellung zu scrapen. Mit CME_FEDWATCH_BEARER_TOKEN kann die offizielle
    REST-Route genutzt werden.
    """
    token = os.environ.get("CME_FEDWATCH_BEARER_TOKEN")
    if not token:
        return ("CME FedWatch: aktuell nicht automatisch abgerufen | "
                "offizielle CME-API optional ueber CME_FEDWATCH_BEARER_TOKEN; "
                "keine ersatzweise erfundene Wahrscheinlichkeit")
    url = "https://markets.api.cmegroup.com/fedwatch_rt/v1/forecasts/latest"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        return "CME FedWatch: " + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        return f"CME FedWatch: API nicht verfuegbar ({type(e).__name__})"


def market_snapshot(name, ticker, category):
    try:
        hist = yf.download(ticker, period="2y", interval="1d", auto_adjust=False, progress=False, threads=False)
        if hist is None or hist.empty:
            return f"{name}: Daten nicht verfuegbar ({ticker})"
        if isinstance(hist.columns, pd.MultiIndex):
            close = hist["Close"].iloc[:, 0]
        else:
            close = hist["Close"]
        close = pd.to_numeric(close, errors="coerce").dropna()
        if close.empty:
            return f"{name}: Daten nicht verfuegbar ({ticker})"
        current = float(close.iloc[-1])
        date = close.index[-1]
        def old(days):
            if len(close) < 2: return None
            target = date - pd.Timedelta(days=days)
            c = close[close.index <= target]
            return float(c.iloc[-1]) if not c.empty else None
        vals = []
        for label, days in (("5T",5), ("1M",30), ("3M",90), ("6M",180), ("1J",365)):
            o = old(days)
            if o not in (None, 0):
                vals.append(f"{label} {(current/o-1)*100:+.2f}%")
        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1] if len(close) >= 20 else None
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1] if len(close) >= 50 else None
        trend = []
        if ema20 is not None: trend.append(f"EMA20 {'darueber' if current > ema20 else 'darunter'}")
        if ema50 is not None: trend.append(f"EMA50 {'darueber' if current > ema50 else 'darunter'}")
        return f"{name}: {current:.4f} | Datenstand {date.strftime('%Y-%m-%d')} | {' | '.join(vals)} | {' | '.join(trend)} | Quelle {ticker} ({category})"
    except Exception as e:
        return f"{name}: Daten nicht verfuegbar ({ticker}, {type(e).__name__})"


def main():
    today = dt.date.today().isoformat()
    output = f"Makro_Briefing({today}).txt"
    lines = []
    lines.append("NEUBER MACRO & MARKETS")
    lines.append(f"MAKRO-DATENPAKET | Datenabruf: {today}")
    lines.append("Zweck: Rohdaten fuer Makro-Interpretation und Zukunftsszenarien; keine Veraenderung bestehender Trading-Logik.")
    lines.append("")

    lines.append("1. MONETAERES UMFELD, ZINSEN & LIQUIDITAET")
    for name in ["Fed Funds Effective Rate", "ECB Deposit Facility Rate", "M2", "Realzins 10Y TIPS"]:
        lines.append(fred_snapshot(name, FRED_SERIES[name]))
    lines.append(fedwatch_snapshot())
    for name, sid in [("US 2Y Treasury", "DGS2"), ("US 5Y Treasury", "DGS5"), ("US 10Y Treasury", "DGS10"), ("US 30Y Treasury", "DGS30")]:
        lines.append(fred_snapshot(name, sid))
    lines.append("")

    lines.append("2. INFLATION, ARBEIT & KONJUNKTUR")
    for name in ["CPI", "Core CPI", "PCE", "Core PCE", "PPI", "Durchschnittlicher Stundenlohn", "Reales BIP", "Arbeitslosenquote", "NFP / Nonfarm Payrolls", "JOLTS Job Openings", "Initial Jobless Claims", "Industrieproduktion", "Kapazitaetsauslastung", "Consumer Sentiment"]:
        lines.append(fred_snapshot(name, FRED_SERIES[name]))
    lines.append("PMI: nicht als feste FRED-Reihe erzwungen; bis zur Auswahl einer belastbaren, automatisierbaren Quelle wird die Konjunkturdiagnose ueber BIP, Industrieproduktion, Arbeitsmarkt, NY-Fed-/andere Fruehindikatoren und Marktpreise bestaetigt.")
    lines.append("")

    lines.append("3. KREDIT, FINANCIAL CONDITIONS & RISIKO")
    for name in ["SLOOS C&I Tightening", "US High Yield OAS", "US Investment Grade OAS", "Chicago Fed NFCI"]:
        lines.append(fred_snapshot(name, FRED_SERIES[name]))
    lines.append("")

    lines.append("4. EXOGENE FAKTOREN, LIEFERKETTEN & FISKAL")
    for name in ["GSCPI", "Global Economic Policy Uncertainty", "US Federal Debt/GDP"]:
        lines.append(fred_snapshot(name, FRED_SERIES[name]))
    lines.append("Geopolitik: kein kuenstlicher Tages-Score. Ereignisse werden nur dann in die Szenariointerpretation aufgenommen, wenn sie in den bereitgestellten Daten/News konkret belegt sind.")
    lines.append("")

    lines.append("5. MARKT, FX, KRYPTO & ROHSTOFFE")
    for name, (ticker, category) in MARKET_DATA.items():
        lines.append(market_snapshot(name, ticker, category))
    lines.append("")

    lines.append("6. INTERPRETATIONSREGELN FUER GEMINI")
    lines.append("Die Makroanalyse soll die Daten nicht einzeln kommentieren, sondern widerspruchsfrei zu einem Gesamtbild verbinden.")
    lines.append("Makroachsen: Wachstum | Inflation | Geldpolitik | Liquiditaet | Kredit | Risk Appetite | Bewertung | Angebotsschock | struktureller Capex-Zyklus.")
    lines.append("Horizonte: 1-4 Wochen | 1-3 Monate | 3-6 Monate | >6 Monate.")
    lines.append("Szenarien: Base Case | Bull Case | Bear Case. Wahrscheinlichkeiten muessen zusammen 100% ergeben und sind Modellurteile, keine statistisch exakten Wahrscheinlichkeiten.")
    lines.append("Fuer perspektivische Trades: Asset-/Sektor-Richtung, Treiber, Gegentreiber, bevorzugte Themen und klare Regime-Killer nennen. Keine automatische Veraenderung von Setup-, CRV-, Score- oder Portfolio-Logik.")
    lines.append("Lithium ist als struktureller Speicher-/Batterie-/Netzausbau-Indikator zu interpretieren, nicht isoliert als Konjunktursignal. Bei Proxy-Daten immer die Proxy-Natur beruecksichtigen.")

    with open(output, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Makro-Datenpaket gespeichert: {output}")


if __name__ == "__main__":
    main()
