"""Makro-Szenario-Datenpaket fuer Neuber Macro & Markets.

HARTE DATENREGELN
-----------------
- Keine geschaetzten/erfundenen Datenwerte.
- Fehlende Daten bleiben UNAVAILABLE.
- REAL = veroeffentlichter Originalwert.
- CALCULATED = deterministisch aus REAL-Werten berechnet.
- PROXY = beobachteter Proxy, niemals als Originalpreis ausgeben.
- MODEL_DERIVED = nur fuer die spaetere Szenario-/Wahrscheinlichkeitslogik.
- Ein fehlender kritischer Datenbaustein sperrt die Makro-Szenariofreigabe.

Dieses Modul veraendert keine Setup-, CRV-, Score-, Portfolio- oder Intraday-Logik.

VERSION = "v6.7"
"""

import datetime as dt
from datetime import datetime
import calendar
import math
import re
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from pathlib import Path
from io import StringIO, BytesIO

import pandas as pd
import requests
import yfinance as yf

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
NYFED_GSCPI_URL = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx"
FRED_URL = "https://fred.stlouisfed.org/series/{}"
ISM_BASE = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports"
LME_OFFICIAL_PRICES_URL = "https://www.lme.com/market-data/reports-and-data/lme-official-prices"
FED_FUTURES_PUBLIC_URL = "https://www.pomeroygrain.com/markets.aspx?cg=30-Day+Fed+Funds"

FRED_SERIES = {
    "Fed Funds Effective Rate": "DFF",
    "Fed Target Range Upper": "DFEDTARU",
    "Fed Target Range Lower": "DFEDTARL",
    "ECB Deposit Facility Rate": "ECBDFR",
    "M2": "M2SL",
    "Realzins 10Y TIPS": "DFII10",
    "CPI": "CPIAUCSL",
    "Core CPI": "CPILFESL",
    "PCE": "PCEPI",
    "Core PCE": "PCEPILFE",
    "PPI": "PPIACO",
    "Durchschnittlicher Stundenlohn": "CES0500000003",
    "Reales BIP": "GDPC1",
    "Arbeitslosenquote": "UNRATE",
    "NFP / Nonfarm Payrolls": "PAYEMS",
    "JOLTS Job Openings": "JTSJOL",
    "Initial Jobless Claims": "ICSA",
    "Industrieproduktion": "INDPRO",
    "Kapazitaetsauslastung": "TCU",
    "Consumer Sentiment": "UMCSENT",
    "SLOOS C&I Tightening": "DRTSCILM",
    "US High Yield OAS": "BAMLH0A0HYM2",
    "US Investment Grade OAS": "BAMLC0A0CM",
    "Chicago Fed NFCI": "NFCI",
    "Global Economic Policy Uncertainty": "GEPUCURRENT",
    "US Federal Debt/GDP": "GFDEGDQ188S",
    "US 2Y Treasury": "DGS2",
    "US 5Y Treasury": "DGS5",
    "US 10Y Treasury": "DGS10",
    "US 30Y Treasury": "DGS30",
}

MARKET_DATA = {
    "S&P 500": ("^GSPC", "REAL_MARKET"),
    "Nasdaq Composite": ("^IXIC", "REAL_MARKET"),
    "Russell 2000": ("^RUT", "REAL_MARKET"),
    "DAX": ("^GDAXI", "REAL_MARKET"),
    "EuroStoxx 50": ("^STOXX50E", "REAL_MARKET"),
    "Nikkei 225": ("^N225", "REAL_MARKET"),
    "VIX": ("^VIX", "REAL_MARKET"),
    "DXY": ("DX-Y.NYB", "REAL_MARKET"),
    "EUR/USD": ("EURUSD=X", "REAL_MARKET"),
    "USD/JPY": ("JPY=X", "REAL_MARKET"),
    "Bitcoin": ("BTC-USD", "REAL_MARKET"),
    "Ethereum": ("ETH-USD", "REAL_MARKET"),
    "WTI": ("CL=F", "REAL_FUTURES"),
    "Brent": ("BZ=F", "REAL_FUTURES"),
    "Erdgas": ("NG=F", "REAL_FUTURES"),
    "Gold": ("GC=F", "REAL_FUTURES"),
    "Silber": ("SI=F", "REAL_FUTURES"),
    "Platin": ("PL=F", "REAL_FUTURES"),
    "Palladium": ("PA=F", "REAL_FUTURES"),
    "Kupfer": ("HG=F", "REAL_FUTURES"),
    "Aluminium": ("ALI=F", "REAL_FUTURES"),
    "Zink": ("ZNC=F", "REAL_FUTURES"),
    # LME ist fuer diese vier Metalle die primaere kostenlose Quelle fuer
    # den day-delayed aktuellen Official Price. Historische LME-Reihen
    # werden hier bewusst NICHT erfunden oder kostenpflichtig bezogen.
    "Nickel": ("LME:Nickel", "REAL_LME"),
    "Blei": ("LME:Lead", "REAL_LME"),
    "Zinn": ("LME:Tin", "REAL_LME"),
    "Kobalt": ("LME:Cobalt", "REAL_LME"),
    "Lithium": ("LIT", "PROXY"),
    "Eisenerz": ("TIO=F", "REAL_FUTURES"),
}

LME_METALS = {"Nickel": "Nickel", "Blei": "Lead", "Zinn": "Tin", "Kobalt": "Cobalt"}

MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M", 7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NeuberMacro/1.0)"}
FED_H15_URL = "https://www.federalreserve.gov/releases/h15/"
TREASURY_YIELD_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
TREASURY_REAL_YIELD_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_real_yield_curve&field_tdr_date_value={year}&page&_format=csv"
BEA_NIPA_Q_URL = "https://apps.bea.gov/national/Release/TXT/NipaDataQ.txt"
DBNOMICS_BASE = "https://api.db.nomics.world/v22/series"
BLS_API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"

MACRO_CACHE_DIR = Path(os.environ.get("NMM_MACRO_CACHE_DIR", ".macro_cache"))
MACRO_CACHE_FILE = MACRO_CACHE_DIR / "macro_cache.json"
FRED_TIMEOUT = float(os.environ.get("NMM_FRED_TIMEOUT_SECONDS", "8"))
MARKET_TIMEOUT = float(os.environ.get("NMM_MARKET_TIMEOUT_SECONDS", "12"))
CACHE_VERSION = 5
CACHE_WRITE_LOCK = Lock()
LME_PRICE_CACHE = None
LME_PRICE_CACHE_TIME = 0.0
LME_REQUEST_ATTEMPTED = False

# Maximale Zeit, die ein gespeicherter REAL-Wert als verwendbar gilt, wenn die
# Quelle beim aktuellen Lauf nicht erreichbar ist. Die Gültigkeit richtet sich
# bewusst nach der Veröffentlichungsfrequenz des Datenpunkts, nicht nach dem
# Abrufzeitpunkt. Ein echter Monatswert bleibt also gültig, bis ein neuer
# veröffentlichter Monatswert vorliegt; tägliche Markt-/Zinswerte dürfen nur
# wenige Handelstage alt sein.
FRED_MAX_AGE_DAYS = {
    "DFF": 7, "DFEDTARU": 45, "DFEDTARL": 45, "ECBDFR": 45, "M2SL": 45,
    "DFII10": 7, "DGS2": 7, "DGS5": 7, "DGS10": 7, "DGS30": 7,
    "CPIAUCSL": 60, "CPILFESL": 60, "PCEPI": 60, "PCEPILFE": 60, "PPIACO": 60,
    "CES0500000003": 60, "GDPC1": 120, "UNRATE": 60, "PAYEMS": 60,
    "JTSJOL": 90, "ICSA": 14, "INDPRO": 60, "TCU": 60, "UMCSENT": 60,
    "DRTSCILM": 120, "BAMLH0A0HYM2": 7, "BAMLC0A0CM": 7, "NFCI": 14,
    "GEPUCURRENT": 120, "GFDEGDQ188S": 120,
}


def _cache_load():
    if not MACRO_CACHE_FILE.exists():
        return {"version": CACHE_VERSION, "fred": {}, "market": {}, "fed_futures": {}, "ism": {}}
    try:
        with MACRO_CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != CACHE_VERSION:
            return {"version": CACHE_VERSION, "fred": {}, "market": {}, "fed_futures": {}, "ism": {}}
        return data
    except Exception as exc:
        print(f"WARNUNG-MAKRO-CACHE: Cache nicht lesbar ({exc}) - starte leer.")
        return {"version": CACHE_VERSION, "fred": {}, "market": {}, "fed_futures": {}, "ism": {}}


def _cache_save(data):
    MACRO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MACRO_CACHE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MACRO_CACHE_FILE)


def _cache_valid(data_date, max_age_days, today=None):
    if not data_date:
        return False
    try:
        d = dt.date.fromisoformat(data_date)
        ref = today or dt.date.today()
        return (ref - d).days <= max_age_days
    except Exception:
        return False


def _clean_num(value):
    try:
        value = float(value)
        return None if not math.isfinite(value) else value
    except Exception:
        return None


def _fmt(value, decimals=4):
    value = _clean_num(value)
    return "NICHT VERFUEGBAR" if value is None else f"{value:.{decimals}f}"


def _df_from_cache_entry(entry, series_id):
    if not entry or not entry.get("payload"):
        return pd.DataFrame()
    try:
        df = pd.read_json(StringIO(entry["payload"]), orient="split")
        df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
        df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
        return df.dropna(subset=["DATE", series_id]).sort_values("DATE")
    except Exception:
        return pd.DataFrame()


def _save_series_cache(series_id, df, source, status="REAL"):
    if df is None or df.empty:
        return
    with CACHE_WRITE_LOCK:
        cache = _cache_load()
        cache.setdefault("fred", {})[series_id] = {
            "saved_at": time.time(),
            "data_date": df["DATE"].iloc[-1].date().isoformat(),
            "payload": df.to_json(orient="split", date_format="iso"),
            "status": status,
            "source": source,
        }
        _cache_save(cache)


def gscpi_series():
    """Offizielle GSCPI-Monatsreihe der Federal Reserve Bank of New York."""
    try:
        r = requests.get(NYFED_GSCPI_URL, timeout=20, headers=REQUEST_HEADERS)
        r.raise_for_status()
        df = pd.read_excel(
            BytesIO(r.content),
            sheet_name="GSCPI Monthly Data",
        )
        date_col = next((c for c in df.columns if str(c).strip().lower() == "date"), None)
        value_col = next((c for c in df.columns if str(c).strip().lower() == "gscpi"), None)
        if date_col is None or value_col is None:
            raise ValueError("GSCPI Monthly Data enthaelt nicht die erwarteten Spalten Date/GSCPI")
        df["DATE"] = pd.to_datetime(df[date_col], errors="coerce")
        df["GSCPI"] = pd.to_numeric(df[value_col], errors="coerce")
        df = df.dropna(subset=["DATE", "GSCPI"]).sort_values("DATE")
        if df.empty:
            raise ValueError("GSCPI Monthly Data enthaelt keine verwertbaren Beobachtungen")
        return df[["DATE", "GSCPI"]], NYFED_GSCPI_URL
    except Exception as exc:
        print(f"WARNUNG: GSCPI New-York-Fed-Abruf nicht verfuegbar: {exc}")
        return pd.DataFrame(), None


def gscpi_snapshot():
    df, source = gscpi_series()
    if df.empty:
        return f"GSCPI: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=New York Fed | {NYFED_GSCPI_URL}"
    value = _clean_num(df["GSCPI"].iloc[-1])
    date = df["DATE"].iloc[-1].strftime("%Y-%m-%d")
    return f"GSCPI: {_fmt(value,4)} | Datenstand={date} | STATUS=REAL | SOURCE={source}"


def _rate_token_to_float(token):
    token = str(token).strip().replace("‑", "-").replace("–", "-").replace("—", "-")
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)\s*/\s*(\d+)", token)
    if m:
        den = int(m.group(3))
        if den == 0:
            return None
        return int(m.group(1)) + int(m.group(2)) / den
    try:
        return float(token)
    except Exception:
        return None


def _official_fomc_target_series(series_id):
    """Nur tatsächlich veröffentlichte Fed-Zielkorridore akzeptieren."""
    urls = []

    try:
        year = dt.date.today().year
        index_url = f"https://www.federalreserve.gov/newsevents/pressreleases/{year}-press-fomc.htm"
        r = requests.get(index_url, timeout=8, headers=REQUEST_HEADERS)
        if r.status_code == 200:
            links = re.findall(r'href=["\']([^"\']*monetary\d{8}a\.htm)["\']', r.text, flags=re.I)
            for link in links:
                if link.startswith("/"):
                    link = "https://www.federalreserve.gov" + link
                elif not link.startswith("http"):
                    link = "https://www.federalreserve.gov/newsevents/pressreleases/" + link
                urls.append(link)
    except Exception:
        pass

    try:
        dates = []
        for y in [dt.date.today().year, dt.date.today().year - 1]:
            dates.extend(_fomc_meeting_dates(y))
        for meeting in sorted({d for d in dates if d <= dt.date.today()}, reverse=True):
            urls.append(
                f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{meeting:%Y%m%d}a.htm"
            )
    except Exception:
        pass

    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            r = requests.get(url, timeout=8, headers=REQUEST_HEADERS)
            if r.status_code != 200:
                continue
            text = re.sub(r"<[^>]+>", " ", r.text)
            text = re.sub(r"\s+", " ", text)

            patterns = [
                r"target range for the federal funds rate.*?at\s+"
                r"((?:\d+(?:\.\d+)?|\d+\s*-\s*\d+\s*/\s*\d+))\s+to\s+"
                r"((?:\d+(?:\.\d+)?|\d+\s*-\s*\d+\s*/\s*\d+))\s+percent",
                r"target range.*?at\s+"
                r"((?:\d+(?:\.\d+)?|\d+\s*-\s*\d+\s*/\s*\d+))\s+to\s+"
                r"((?:\d+(?:\.\d+)?|\d+\s*-\s*\d+\s*/\s*\d+))\s+percent",
            ]
            m = next((re.search(p, text, flags=re.I) for p in patterns if re.search(p, text, flags=re.I)), None)
            if not m:
                continue

            lower = _rate_token_to_float(m.group(1))
            upper = _rate_token_to_float(m.group(2))
            if lower is None or upper is None or upper < lower:
                continue

            dm = re.search(r"monetary(\d{8})a\.htm", url)
            meeting_date = dt.datetime.strptime(dm.group(1), "%Y%m%d").date() if dm else dt.date.today()
            value = upper if series_id == "DFEDTARU" else lower
            return pd.DataFrame({"DATE": [pd.Timestamp(meeting_date)], series_id: [value]}), url
        except Exception:
            continue

    return pd.DataFrame(), None
def _treasury_series(series_id):
    """Offizielle U.S.-Treasury-Tagesreihen. Kein FRED-Abhaengigkeit."""
    nominal = {"DGS2": "2 yr", "DGS5": "5 yr", "DGS10": "10 yr", "DGS30": "30 yr"}
    real = {"DFII10": "10 yr"}
    if series_id not in nominal and series_id not in real:
        return pd.DataFrame(), None

    try:
        year = dt.date.today().year
        url = (TREASURY_YIELD_URL if series_id in nominal else TREASURY_REAL_YIELD_URL).format(year=year)
        r = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))

        date_col = next((c for c in df.columns if str(c).strip().lower() == "date"), None)
        if not date_col:
            raise ValueError("Treasury-Spalte 'Date' fehlt")

        wanted = (nominal if series_id in nominal else real)[series_id]

        def norm(c):
            return re.sub(r"[^a-z0-9]+", " ", str(c).strip().lower()).strip()

        target = norm(wanted)
        value_col = next((c for c in df.columns if norm(c) == target), None)

        if value_col is None:
            # Robust gegen "2-year", "2 year", "2 Yr", "2-Year Treasury Rate" etc.
            y = re.match(r"(\d+)", wanted).group(1)
            candidates = []
            for c in df.columns:
                nc = norm(c)
                if re.fullmatch(rf"{y}\s*(yr|year|years)", nc):
                    candidates.append(c)
                elif re.search(rf"\b{y}\s*(yr|year|years)\b", nc):
                    candidates.append(c)
            if candidates:
                value_col = candidates[0]

        if value_col is None:
            raise ValueError(f"keine passende Laufzeitspalte fuer {series_id}")

        out = pd.DataFrame({
            "DATE": pd.to_datetime(df[date_col], errors="coerce"),
            series_id: pd.to_numeric(
                df[value_col].astype(str).str.replace(",", "", regex=False),
                errors="coerce"
            )
        }).dropna().sort_values("DATE")

        if out.empty:
            raise ValueError(f"keine numerischen Treasury-Werte fuer {series_id}")

        return out, url

    except Exception as exc:
        print(f"WARNUNG: U.S.-Treasury-Quelle fuer {series_id} nicht verfuegbar: {exc}")
        return pd.DataFrame(), None
def _h15_series(series_id):
    """Offizielle Federal Reserve H.15 als zusaetzlicher Fallback."""
    try:
        r = requests.get(FED_H15_URL, timeout=7, headers=REQUEST_HEADERS)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))
        # H.15 bleibt ein Fallback; die Primaerquelle fuer Treasury-Renditen ist Treasury selbst.
        for table in tables:
            flat = table.astype(str)
            for _, row in flat.iterrows():
                txt = " | ".join(str(x) for x in row.tolist())
                nums = re.findall(r"(?<![A-Za-z])(-?\d+(?:\.\d+)?)(?![A-Za-z])", txt)
                vals = [_clean_num(x) for x in nums]
                vals = [x for x in vals if x is not None]
                if vals:
                    # Nur als letzter Fallback, wenn die Zeile eindeutig die gewuenschte Laufzeit nennt.
                    label = txt.lower()
                    target = {"DGS2":"2-year", "DGS5":"5-year", "DGS10":"10-year", "DGS30":"30-year", "DFII10":"10-year"}.get(series_id)
                    if target and target in label:
                        return pd.DataFrame({"DATE":[pd.Timestamp.today().normalize()], series_id:[vals[-1]]}), FED_H15_URL
    except Exception as exc:
        print(f"WARNUNG: H.15-Fallback fuer {series_id} nicht verfuegbar: {exc}")
    return pd.DataFrame(), None

def _bea_real_gdp_series():
    """BEA NIPA quarterly table 1.1.6 / account code A191RX."""
    try:
        r = requests.get(BEA_NIPA_Q_URL, timeout=8, headers=REQUEST_HEADERS)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        code_col = next((c for c in df.columns if str(c).strip().lower() in {"%seriescode", "seriescode"}), None)
        period_col = next((c for c in df.columns if str(c).strip().lower() == "period"), None)
        value_col = next((c for c in df.columns if str(c).strip().lower() == "value"), None)
        if not code_col or not period_col or not value_col:
            return pd.DataFrame(), None
        x = df[df[code_col].astype(str).str.strip() == "A191RX"].copy()
        x["VALUE"] = pd.to_numeric(x[value_col], errors="coerce")
        x = x.dropna(subset=["VALUE"])
        dates=[]
        for period in x[period_col].astype(str):
            m=re.fullmatch(r"(\d{4})Q([1-4])", period.strip())
            if m:
                dates.append(pd.Timestamp(year=int(m.group(1)), month=(int(m.group(2))-1)*3+1, day=1))
            else:
                dates.append(pd.NaT)
        x["DATE"] = dates
        x=x.dropna(subset=["DATE"])[["DATE","VALUE"]].rename(columns={"VALUE":"GDPC1"}).sort_values("DATE")
        if not x.empty:
            return x, BEA_NIPA_Q_URL
    except Exception as exc:
        print(f"WARNUNG: BEA NIPA GDP nicht verfuegbar: {exc}")
    return pd.DataFrame(), None


BLS_SERIES = {
    "CPIAUCSL": "CUSR0000SA0",
    "CPILFESL": "CUSR0000SA0L1E",
    "UNRATE": "LNS14000000",
    "PAYEMS": "CES0000000001",
    "CES0500000003": "CES0500000003",
    "PPIACO": "WPSFD4",
}


def _bls_series(series_id, years_back=3):
    bls_id = BLS_SERIES.get(series_id)
    if not bls_id:
        return pd.DataFrame(), None
    try:
        end_year = dt.date.today().year
        start_year = max(2000, end_year - years_back)
        url = BLS_API_URL + bls_id
        r = requests.get(url, timeout=8, headers=REQUEST_HEADERS)
        r.raise_for_status()
        data = r.json()
        rows = []
        for item in data.get("Results", {}).get("series", [{}])[0].get("data", []):
            period = item.get("period", "")
            year = item.get("year", "")
            if not re.fullmatch(r"M(0[1-9]|1[0-2])", period):
                continue
            try:
                date = pd.Timestamp(year=int(year), month=int(period[1:]), day=1)
                value = _clean_num(item.get("value"))
            except Exception:
                continue
            if value is not None and start_year <= int(year) <= end_year:
                rows.append((date, value))
        if not rows:
            return pd.DataFrame(), None
        return pd.DataFrame(rows, columns=["DATE", series_id]).sort_values("DATE"), url
    except Exception as exc:
        print(f"WARNUNG: BLS-Quelle fuer {series_id} nicht verfuegbar: {exc}")
        return pd.DataFrame(), None

def _bea_release_gdp_series():
    """Fallback: offizieller BEA-GDP-Release. Nur real veroeffentlichte Advance-Estimate-Werte."""
    url = "https://www.bea.gov/news/2026/gdp-advance-estimate-2nd-quarter-2026"
    try:
        r = requests.get(url, timeout=8, headers=REQUEST_HEADERS)
        if r.status_code != 200:
            return pd.DataFrame(), None
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text)
        m = re.search(
            r"Real gross domestic product \(GDP\) increased at an annual rate of\s+"
            r"(-?\d+(?:\.\d+)?)\s+percent in the second quarter of 2026",
            text, flags=re.I
        )
        if not m:
            return pd.DataFrame(), None
        value = _clean_num(m.group(1))
        if value is None:
            return pd.DataFrame(), None
        # This is the official BEA annualized growth rate, not the GDPC1 level.
        # Return it under a dedicated series id so it is never confused with GDPC1.
        return pd.DataFrame({
            "DATE": [pd.Timestamp("2026-04-01")],
            "GDP_GROWTH_QOQ_ANNUALIZED": [value]
        }), url
    except Exception as exc:
        print(f"WARNUNG: BEA Release GDP nicht verfuegbar: {exc}")
        return pd.DataFrame(), None

def _public_hy_oas_series(series_id):
    """Öffentlicher Sekundärabruf der exakt gleichen FRED/ICE-BofA-Reihe.
    Autario stellt die Reihe BAMLH0A0HYM2 ohne API-Key als JSON bereit.
    """
    if series_id != "BAMLH0A0HYM2":
        return pd.DataFrame(), None

    dataset_id = "e4a3e9e1-0e3f-4bc3-8b6f-bd8f0eb8c8c9"
    url = f"https://autario.com/api/v1/public/datasets/{dataset_id}/data?limit=1000"

    try:
        r = requests.get(url, timeout=12, headers=REQUEST_HEADERS)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("data", [])

        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                continue

            date_raw = row.get("date") or row.get("DATE")
            value_raw = row.get("value")
            if value_raw is None:
                value_raw = row.get(series_id)

            if date_raw is None or value_raw is None:
                continue

            try:
                date = pd.to_datetime(date_raw, errors="coerce")
                value = _clean_num(value_raw)
            except Exception:
                continue

            if pd.isna(date) or value is None:
                continue

            parsed.append((date, value))

        if not parsed:
            raise ValueError("API lieferte keine verwertbaren Beobachtungen")

        out = (
            pd.DataFrame(parsed, columns=["DATE", series_id])
            .drop_duplicates("DATE")
            .sort_values("DATE")
        )

        return out, url

    except Exception as exc:
        print(f"WARNUNG: oeffentlicher HY-OAS-Abruf nicht verfuegbar: {exc}")
        return pd.DataFrame(), None

def _fred_direct_csv_series(series_id):
    """Direkter FRED-CSV-Abruf als Fallback, getrennt vom normalen FRED-Endpunkt."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        r = requests.get(url, timeout=8, headers=REQUEST_HEADERS)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        if "DATE" not in df.columns or series_id not in df.columns:
            return pd.DataFrame(), None
        df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
        df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
        df = df.dropna(subset=["DATE", series_id]).sort_values("DATE")
        if not df.empty:
            return df, url
    except Exception as exc:
        print(f"WARNUNG: direkter FRED-CSV-Abruf fuer {series_id} nicht verfuegbar: {exc}")
    return pd.DataFrame(), None

def bea_gdp_snapshot():
    """Official BEA GDP release. Uses the published annualized QoQ growth rate.
    This is deliberately NOT mislabeled as the GDPC1 level series.
    """
    url = "https://www.bea.gov/news/2026/gdp-advance-estimate-2nd-quarter-2026"
    try:
        r = requests.get(url, timeout=10, headers=REQUEST_HEADERS)
        r.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text)
        m = re.search(
            r"Real gross domestic product \(GDP\) increased at an annual rate of\s+"
            r"(-?\d+(?:\.\d+)?)\s+percent in the second quarter of 2026",
            text, flags=re.I
        )
        if not m:
            return "Reales BIP-Wachstum: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=BEA"
        value = _clean_num(m.group(1))
        if value is None:
            return "Reales BIP-Wachstum: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=BEA"
        return (
            f"Reales BIP-Wachstum: {_fmt(value,1)}% annualisiert | "
            f"Datenstand=2026-Q2 | STATUS=REAL | SOURCE={url}"
        )
    except Exception as exc:
        print(f"WARNUNG: BEA GDP Release nicht verfuegbar: {exc}")
        return "Reales BIP-Wachstum: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=BEA"


def _dbnomics_series(series_id):
    """Kostenloser Sekundaer-Fallback. Provider bleibt im Output explizit benannt."""
    mapping={
        "BAMLH0A0HYM2":"FRED/BAMLH0A0HYM2",
        "BAMLC0A0CM":"FRED/BAMLC0A0CM",
    }
    path=mapping.get(series_id)
    if not path:
        return pd.DataFrame(), None
    url=f"{DBNOMICS_BASE}/{path}?observations=1"
    try:
        r=requests.get(url, timeout=8, headers=REQUEST_HEADERS)
        r.raise_for_status()
        data=r.json().get("series", {}).get("docs", [])
        if not data:
            return pd.DataFrame(), None
        doc=data[0]
        periods=doc.get("period", [])
        values=doc.get("value", [])
        rows=[]
        for d,v in zip(periods,values):
            v=_clean_num(v)
            if v is not None:
                rows.append((pd.to_datetime(d, errors="coerce"),v))
        rows=[x for x in rows if pd.notna(x[0])]
        if rows:
            return pd.DataFrame(rows, columns=["DATE",series_id]).sort_values("DATE"), url
    except Exception as exc:
        print(f"WARNUNG: DBnomics-Fallback fuer {series_id} nicht verfuegbar: {exc}")
    return pd.DataFrame(), None


def _alternate_series(series_id):
    if series_id in {"DFEDTARU","DFEDTARL"}:
        return _official_fomc_target_series(series_id)
    if series_id in {"DGS2","DGS5","DGS10","DGS30","DFII10"}:
        treasury, source = _treasury_series(series_id)
        if not treasury.empty:
            return treasury, source
        return _h15_series(series_id)
    if series_id in BLS_SERIES:
        bls, source = _bls_series(series_id)
        if not bls.empty:
            return bls, source
    if series_id == "GDPC1":
        gdp, source = _bea_real_gdp_series()
        if not gdp.empty:
            return gdp, source
        # Do NOT relabel annualized GDP growth as a GDPC1 level.
        return pd.DataFrame(), None
    if series_id == "BAMLH0A0HYM2":
        public, source = _public_hy_oas_series(series_id)
        if not public.empty:
            return public, source
        direct, source = _fred_direct_csv_series(series_id)
        if not direct.empty:
            return direct, source
        return _dbnomics_series(series_id)
    if series_id == "BAMLC0A0CM":
        direct, source = _fred_direct_csv_series(series_id)
        if not direct.empty:
            return direct, source
        return _dbnomics_series(series_id)
    return pd.DataFrame(), None


def fred_series(series_id, limit_days=5000):
    cache = _cache_load()
    entry = cache.get("fred", {}).get(series_id)
    if entry and entry.get("payload"):
        df = _df_from_cache_entry(entry, series_id)
        if not df.empty:
            latest = df["DATE"].iloc[-1].date().isoformat()
            if _cache_valid(latest, FRED_MAX_AGE_DAYS.get(series_id, 60)):
                print(f"INFO: FRED-Cache-Hit fuer {series_id} (Datenstand={latest})")
                return df

    try:
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            raise RuntimeError("FRED_API_KEY ist nicht gesetzt")
        api_url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "api_key": api_key,
            "file_type": "json",
            "series_id": series_id,
            "sort_order": "asc",
        }
        if limit_days:
            observation_start = (dt.date.today() - dt.timedelta(days=limit_days)).isoformat()
            params["observation_start"] = observation_start
        r = requests.get(api_url, params=params, timeout=FRED_TIMEOUT, headers=REQUEST_HEADERS)
        r.raise_for_status()
        observations = r.json().get("observations", [])
        parsed = []
        for observation in observations:
            date = pd.to_datetime(observation.get("date"), errors="coerce")
            value = _clean_num(observation.get("value"))
            if pd.notna(date) and value is not None:
                parsed.append((date, value))
        if not parsed:
            raise ValueError(f"FRED API lieferte keine verwertbaren {series_id}-Beobachtungen")
        df = pd.DataFrame(parsed, columns=["DATE", series_id]).drop_duplicates("DATE").sort_values("DATE")
        if not df.empty:
            _save_series_cache(series_id, df, api_url, "REAL")
        return df
    except Exception as exc:
        print(f"WARNUNG: FRED {series_id} nicht verfuegbar: {exc}")

    # Offizielle bzw. klar dokumentierte kostenlose Fallbacks.
    alt, source = _alternate_series(series_id)
    if alt is not None and not alt.empty:
        _save_series_cache(series_id, alt, source, "REAL_PUBLIC_SECONDARY" if series_id == "BAMLH0A0HYM2" and "autario.com" in str(source) else "REAL")
        print(f"INFO: Fallback erfolgreich fuer {series_id}: {source}")
        return alt

    # Nur ein noch innerhalb der Datenaltersgrenze liegender echter Cachewert.
    if entry and entry.get("payload"):
        df = _df_from_cache_entry(entry, series_id)
        if not df.empty and _cache_valid(df["DATE"].iloc[-1].date().isoformat(), FRED_MAX_AGE_DAYS.get(series_id,60)):
            return df
    return pd.DataFrame()
def fred_snapshot(name, series_id):
    df = fred_series(series_id)
    if df.empty:
        return f"{name}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=FRED {series_id} | {FRED_URL.format(series_id)}"
    value = _clean_num(df[series_id].iloc[-1])
    date = df["DATE"].iloc[-1].strftime("%Y-%m-%d")
    cache = _cache_load()
    entry = cache.get("fred", {}).get(series_id, {})
    source = entry.get("source", FRED_URL.format(series_id))
    status = entry.get("status", "REAL")
    # Wenn Quelle im aktuellen Lauf nicht erreichbar war, aber ein echter gespeicherter
    # Wert verwendet wurde, bleibt er REAL_CACHED. Der Wert selbst wird nie veraendert.
    if status == "REAL" and entry.get("saved_at",0) < time.time()-1:
        # Nicht automatisch als cached markieren, weil der Abrufstatus unbekannt ist.
        status = "REAL"
    return f"{name}: {_fmt(value,4)} | Datenstand={date} | STATUS={status} | SOURCE={source}"


def _lme_official_prices():
    """LME Official Prices mit einem Abruf pro Prozess und sicherem Fallback.

    Primaer: oeffentliche LME Official Prices (day-delayed).
    Bei Ausfall: letzter tatsaechlich gespeicherter offizieller LME-Wert.
    Der Fallback ist immer DEGRADED und niemals gate-kritisch.
    """
    global LME_PRICE_CACHE, LME_PRICE_CACHE_TIME, LME_REQUEST_ATTEMPTED

    # Ein Lauf darf die LME-Seite nur einmal anfragen. Alle vier Metalle
    # verwenden danach exakt denselben Snapshot.
    if LME_REQUEST_ATTEMPTED:
        return LME_PRICE_CACHE or {}
    LME_REQUEST_ATTEMPTED = True

    def cache_get():
        try:
            entry = _cache_load().get("lme", {})
            return entry if isinstance(entry, dict) else {}
        except Exception:
            return {}

    def cache_put(data):
        if not data:
            return
        with CACHE_WRITE_LOCK:
            cache = _cache_load()
            cache["lme"] = {"saved_at": time.time(), "data": data}
            _cache_save(cache)

    try:
        response = requests.get(
            LME_OFFICIAL_PRICES_URL,
            timeout=20,
            headers={
                **REQUEST_HEADERS,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        response.raise_for_status()
        html = response.text
    except Exception as exc:
        print(f"WARNUNG: LME-Abruf fehlgeschlagen: {exc}")
        cached = cache_get().get("data", {})
        result = {}
        for metal, data in cached.items():
            if not isinstance(data, dict):
                continue
            # Nur echte, bereits veröffentlichte LME-Werte dürfen als Fallback
            # dienen. Das ursprüngliche LME-Datum bleibt unverändert.
            bid = _clean_num(data.get("cash_bid"))
            ask = _clean_num(data.get("cash_ask"))
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                continue
            d = dict(data)
            d["cash_bid"] = bid
            d["cash_ask"] = ask
            d["fallback"] = True
            d["status"] = "DEGRADED"
            result[metal] = d
        if result:
            print(
                "INFO: LME-Cache-Fallback verwendet; letzter offizieller "
                "LME-Datenstand bleibt unveraendert."
            )
        LME_PRICE_CACHE = result
        LME_PRICE_CACHE_TIME = time.time()
        return result

    try:
        tables = pd.read_html(StringIO(html))
    except Exception as exc:
        print(f"WARNUNG: LME-Tabelle konnte nicht gelesen werden: {exc}")
        tables = []

    page_date = None
    date_matches = re.findall(
        r"(?i)(?:data\s+valid\s+for|date|dated|pricing date|trade date)\s*[:\-]?"
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})",
        html,
    )
    for raw_date in date_matches:
        parsed_date = pd.to_datetime(raw_date, errors="coerce", dayfirst=True)
        if pd.notna(parsed_date):
            page_date = parsed_date.strftime("%Y-%m-%d")
            break

    result = {}
    for _, lme_name in LME_METALS.items():
        for table in tables:
            found = _lme_find_cash_bid_offer(table, lme_name)
            if found is not None:
                found["date"] = page_date
                found["fallback"] = False
                found["status"] = "REAL"
                result[lme_name] = found
                break

    if result:
        cache_put(result)
    else:
        cached = cache_get().get("data", {})
        for metal, data in cached.items():
            if not isinstance(data, dict):
                continue
            bid = _clean_num(data.get("cash_bid"))
            ask = _clean_num(data.get("cash_ask"))
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                continue
            d = dict(data)
            d["cash_bid"] = bid
            d["cash_ask"] = ask
            d["fallback"] = True
            d["status"] = "DEGRADED"
            result[metal] = d
        if result:
            print(
                "INFO: LME-Seite erreichbar, aber keine verwertbare Tabelle; "
                "LME-Cache-Fallback verwendet."
            )

    LME_PRICE_CACHE = result
    LME_PRICE_CACHE_TIME = time.time()
    return result


def _lme_find_cash_bid_offer(table, metal):
    """Liest ausschliesslich eindeutig benannte LME-Cash-Bid/Cash-Offer-Spalten.

    Keine Positionsheuristik. Eine Tabelle wird nur akzeptiert, wenn die
    Spaltenheader explizit Cash + Bid bzw. Cash + Offer/Ask enthalten und die
    Metallzeile eindeutig gefunden wird.
    """
    df = table.copy()
    if isinstance(df.columns, pd.MultiIndex):
        flattened = []
        for col in df.columns:
            parts = [str(x).strip() for x in col if str(x).strip().lower() not in {"nan", "none", ""}]
            flattened.append(" ".join(parts))
        df.columns = flattened
    else:
        df.columns = [str(c).strip() for c in df.columns]

    def norm(x):
        return re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()

    def header_tokens(x):
        return set(norm(x).split())

    bid_candidates = []
    offer_candidates = []
    for col in df.columns:
        tokens = header_tokens(col)
        if "cash" not in tokens:
            continue
        if "bid" in tokens:
            bid_candidates.append(col)
        if "offer" in tokens or "ask" in tokens:
            offer_candidates.append(col)

    # Genau eine eindeutige Spalte pro Richtung; bei Ambiguitaet kein Raten.
    if len(bid_candidates) != 1 or len(offer_candidates) != 1:
        return None
    cash_bid_col = bid_candidates[0]
    cash_offer_col = offer_candidates[0]

    metal_norm = norm(metal)
    metal_columns = [c for c in df.columns if norm(c) in {"metal", "name", "commodity", "contract"}]
    candidate_rows = []
    for idx, row in df.iterrows():
        if metal_columns:
            names = [norm(row[c]) for c in metal_columns]
        else:
            # Nur wenn es keine erkennbare Metallspalte gibt, darf die komplette
            # Zeile nach dem exakten Metallnamen durchsucht werden.
            names = [norm(v) for v in row.tolist()]
        if metal_norm in names:
            candidate_rows.append(idx)

    if len(candidate_rows) != 1:
        return None

    row = df.loc[candidate_rows[0]]
    bid = _clean_num(str(row[cash_bid_col]).replace(",", ""))
    offer = _clean_num(str(row[cash_offer_col]).replace(",", ""))
    if bid is None or offer is None or bid <= 0 or offer <= 0:
        return None
    return {"cash_bid": bid, "cash_ask": offer}

def lme_snapshot(name, ticker):
    metal = ticker.split(":", 1)[1] if ticker and ":" in ticker else name
    lme_name = LME_METALS.get(name, metal)
    data = _lme_official_prices().get(lme_name)
    if not data:
        return f"{name}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | DATENTYP=REAL_LME | SOURCE={LME_OFFICIAL_PRICES_URL}"
    value = (data["cash_bid"] + data["cash_ask"]) / 2.0
    status = data.get("status", "REAL")
    return (
        f"{name}: {_fmt(value,2)} USD/t | Datenstand={data.get('date') or 'unbekannt'} | "
        f"STATUS={status} | DATENTYP=REAL_LME | SOURCE={LME_OFFICIAL_PRICES_URL} | "
        f"CASH_BID={data['cash_bid']:.2f} | CASH_ASK={data['cash_ask']:.2f}"
    )


def _market_history(ticker):
    cache = _cache_load()
    entry = cache.get("market", {}).get(ticker)
    if entry and entry.get("payload"):
        try:
            series = pd.read_json(StringIO(entry["payload"]), orient="split")
            series.index = pd.to_datetime(series.index)
            close = pd.to_numeric(series.iloc[:, 0], errors="coerce").dropna()
            latest = close.index[-1].date().isoformat() if not close.empty else None
            # Tages-/Futuresdaten: am Wochenende darf der letzte Handelstag
            # verwendet werden; nach sieben Kalendertagen ist der Cache zu alt.
            if latest and _cache_valid(latest, 7):
                return close
        except Exception:
            pass
    try:
        hist = yf.download(ticker, period="2y", interval="1d", auto_adjust=False, progress=False, threads=False)
        if hist is None or hist.empty:
            raise ValueError("keine Daten")
        if isinstance(hist.columns, pd.MultiIndex):
            close = hist["Close"].iloc[:, 0]
        else:
            close = hist["Close"]
        close = pd.to_numeric(close, errors="coerce").dropna()
        if close.empty:
            raise ValueError("keine Close-Daten")
        with CACHE_WRITE_LOCK:
            cache = _cache_load()
            cache.setdefault("market", {})[ticker] = {
                "saved_at": time.time(),
                "data_date": pd.Timestamp(close.index[-1]).date().isoformat(),
                "payload": close.to_frame("Close").to_json(orient="split", date_format="iso"),
                "status": "REAL",
            }
            _cache_save(cache)
        return close
    except Exception as exc:
        print(f"WARNUNG: Marktdaten {ticker} nicht verfuegbar: {exc}")
        if entry and entry.get("payload"):
            try:
                series = pd.read_json(StringIO(entry["payload"]), orient="split")
                series.index = pd.to_datetime(series.index)
                close = pd.to_numeric(series.iloc[:, 0], errors="coerce").dropna()
                if not close.empty and _cache_valid(close.index[-1].date().isoformat(), 7):
                    return close
            except Exception:
                pass
        return pd.Series(dtype=float)


def market_snapshot(name, ticker, data_type):
    if data_type == "REAL_LME":
        return lme_snapshot(name, ticker)
    if not ticker or data_type == "UNAVAILABLE":
        return f"{name}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | DATENTYP=UNAVAILABLE | SOURCE=keine belastbare kostenlose Quelle im aktuellen Projektstand"
    close = _market_history(ticker)
    status = "REAL" if data_type != "PROXY" else "PROXY"
    if close.empty:
        return f"{name}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE={ticker} | DATENTYP={data_type}"
    current = float(close.iloc[-1])
    date = close.index[-1]
    parts = []
    for label, days in (("5T", 5), ("1M", 30), ("3M", 90), ("6M", 180), ("1J", 365)):
        target = date - pd.Timedelta(days=days)
        old = close[close.index <= target]
        if not old.empty and float(old.iloc[-1]) != 0:
            parts.append(f"{label}={(current / float(old.iloc[-1]) - 1) * 100:+.2f}%")
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1] if len(close) >= 20 else None
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1] if len(close) >= 50 else None
    trend = []
    if ema20 is not None:
        trend.append(f"EMA20={'DARUEBER' if current > ema20 else 'DARUNTER'}")
    if ema50 is not None:
        trend.append(f"EMA50={'DARUEBER' if current > ema50 else 'DARUNTER'}")
    return (
        f"{name}: {current:.6f} | Datenstand={date.strftime('%Y-%m-%d')} | "
        f"STATUS={status} | DATENTYP={data_type} | "
        f"{' | '.join(parts) if parts else 'keine Vergleichswerte'} | "
        f"{' | '.join(trend) if trend else 'keine Trendwerte'} | SOURCE={ticker}"
    )


def _month_contract(year, month):
    return f"ZQ{MONTH_CODES[month]}{str(year)[-2:]}"


def _extract_fed_futures_from_html(html):
    """Liest die oeffentliche Tabelle einer freien Marktseite.

    Es werden ausschliesslich tatsaechlich angezeigte Kontraktpreise uebernommen.
    Keine Preisinterpolation oder Schätzung.
    """
    found = {}
    try:
        tables = pd.read_html(StringIO(html))
        for table in tables:
            text = table.astype(str).to_string()
            if "ZQU26" not in text and "30-Day Fed Funds" not in text:
                continue
            for _, row in table.iterrows():
                row_text = " ".join(str(x) for x in row.tolist())
                m = re.search(r"\b(ZQ[A-Z]\d{2})\b", row_text)
                if not m:
                    continue
                symbol = m.group(1)
                nums = re.findall(r"(?<!\d)(9[0-9]\.\d{1,4})(?!\d)", row_text)
                if not nums:
                    continue
                # Tabellen sind Contract / High / Low / Last / Change / Time.
                # Fuer den Futures-Preis verwenden wir ausschliesslich die dritte
                # reale 96.xx-Zahl nach dem Kontrakt (= Last), nie High/Low/Change.
                price = _clean_num(nums[2] if len(nums) >= 3 else nums[-1])
                if price is not None:
                    found[symbol] = price
    except Exception:
        pass

    # Robuster Fallback: direkte Zeilen-/Textsuche. Auch hier nur reale angezeigte Zahlen.
    for symbol in re.findall(r"\bZQ[A-Z]\d{2}\b", html):
        if symbol in found:
            continue
        pos = html.find(symbol)
        snippet = re.sub(r"<[^>]+>", " ", html[max(0, pos - 100):pos + 500])
        nums = re.findall(r"(?<!\d)(9[0-9]\.\d{1,4})(?!\d)", snippet)
        if nums:
            found[symbol] = _clean_num(nums[2] if len(nums) >= 3 else nums[-1])
    return found


def get_public_fed_futures():
    cache = _cache_load()
    entry = cache.get("fed_futures", {}).get("quotes")
    if entry and entry.get("quotes"):
        # Futureskurse sind intraday marktabhaengig. Ein gespeicherter Stand
        # darf nur innerhalb desselben Handelstags als Fallback verwendet werden.
        if entry.get("data_date") == dt.date.today().isoformat():
            return entry["quotes"]
    try:
        r = requests.get(FED_FUTURES_PUBLIC_URL, timeout=10, headers=REQUEST_HEADERS)
        r.raise_for_status()
        quotes = _extract_fed_futures_from_html(r.text)
        if quotes:
            with CACHE_WRITE_LOCK:
                cache = _cache_load()
                cache.setdefault("fed_futures", {})["quotes"] = {
                    "saved_at": time.time(),
                    "data_date": dt.date.today().isoformat(),
                    "quotes": quotes,
                    "status": "REAL",
                    "source": FED_FUTURES_PUBLIC_URL,
                }
                _cache_save(cache)
        return quotes
    except Exception as exc:
        print(f"WARNUNG: oeffentliche Fed-Futures-Seite nicht verfuegbar: {exc}")
        # Kein alter Tagesstand: die Fed-Wahrscheinlichkeit ist sonst potentiell
        # veraendert und darf nicht aus einem alten Kurs fortgeschrieben werden.
        return {}


def _fomc_meeting_dates(year):
    # Quelle/Terminplan: lokale JSON-Datei aus dem Repository. Die Datei wird
    # jaehrlich gepflegt und entspricht dem offiziellen Fed-Kalender.
    try:
        import json
        with open("fomc_termine.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return [dt.date.fromisoformat(x) for x in data.get("termine", []) if x.startswith(str(year))]
    except Exception:
        return []


def _next_fomc_date(today):
    dates = [d for d in _fomc_meeting_dates(today.year) + _fomc_meeting_dates(today.year + 1) if d >= today]
    return min(dates) if dates else None


def fed_expectation_snapshot(today):
    meeting = _next_fomc_date(today)
    if meeting is None:
        return [
            "FED-MARKTERWARTUNG: NICHT BERECHENBAR | STATUS=UNAVAILABLE | Grund: kein verifizierter FOMC-Termin im Repository",
            "FED-FUTURES-DATEN: NICHT BERECHENBAR",
        ]

    # Die FOMC-Termine sind offiziell auf federalreserve.gov veroeffentlicht.
    # Die Berechnung orientiert sich an der von CME publizierten Methodik:
    # ZQ-Preis = 100 - erwarteter Monatsdurchschnitt EFFR; Nicht-FOMC-Ankermonat
    # dient zur Fortpflanzung des Start-/Endsatzes.
    year, month = meeting.year, meeting.month
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    # Folgemonat nach dem FOMC-Monat. Falls dieser ebenfalls einen FOMC-Termin
    # hat, suchen wir den naechsten vollstaendigen Monat ohne FOMC-Termin.
    anchor_year, anchor_month = next_year, next_month
    meetings = set(_fomc_meeting_dates(anchor_year) + _fomc_meeting_dates(year))
    while dt.date(anchor_year, anchor_month, 1) <= dt.date(year + 1, 12, 1):
        if not any(d.year == anchor_year and d.month == anchor_month for d in meetings):
            break
        if anchor_month == 12:
            anchor_month, anchor_year = 1, anchor_year + 1
        else:
            anchor_month += 1
    # Wir brauchen mindestens Prev, Meeting, Next und Anchor-Kontrakte.
    required = [
        (prev_year, prev_month),
        (year, month),
        (next_year, next_month),
        (anchor_year, anchor_month),
    ]
    quotes = get_public_fed_futures()
    prices = {}
    for y, m in required:
        symbol = _month_contract(y, m)
        prices[(y, m)] = quotes.get(symbol)

    missing = [f"{_month_contract(y, m)}" for y, m in required if prices[(y, m)] is None]
    if missing:
        return [
            f"FED-MARKTERWARTUNG: NICHT BERECHENBAR | STATUS=UNAVAILABLE | Fehlende reale Futures-Quotes: {', '.join(missing)}",
            f"FOMC naechster Termin: {meeting.isoformat()} | Quelle Terminplan: fomc_termine.json / Federal Reserve",
        ]

    def avg(y, m):
        return 100.0 - prices[(y, m)]

    # Nicht-FOMC-Anker: EFFR Avg(anchor) = EFFR End(next).
    anchor_avg = avg(anchor_year, anchor_month)
    next_days = calendar.monthrange(next_year, next_month)[1]
    # Wenn der naechste Monat ein FOMC-Monat ist, wird sein Endsatz aus dem
    # naechsten Nicht-FOMC-Anker rekonstruiert.
    next_start = None
    next_end = anchor_avg
    next_meeting = next((d for d in _fomc_meeting_dates(next_year) if d.month == next_month), None)
    if next_meeting:
        n = next_meeting.day - 1
        m = next_days - n
        next_avg = avg(next_year, next_month)
        if n <= 0 or m <= 0:
            return ["FED-MARKTERWARTUNG: NICHT BERECHENBAR | STATUS=UNAVAILABLE | ungueltige FOMC-Tagesgewichtung"]
        next_start = (next_avg - (m / next_days) * next_end) / (n / next_days)
    else:
        next_start = anchor_avg

    # Meeting-Monat: Start = Ende des vorherigen Nicht-FOMC-Monats, sofern der
    # Vormonat kein FOMC hatte. Sonst waere eine weitere Rekursion erforderlich.
    prev_meeting = any(d.year == prev_year and d.month == prev_month for d in _fomc_meeting_dates(prev_year))
    if prev_meeting:
        return ["FED-MARKTERWARTUNG: NICHT BERECHENBAR | STATUS=UNAVAILABLE | Vormonat ist ebenfalls FOMC-Monat; Rekursion erforderlich"]
    start = avg(prev_year, prev_month)
    end = next_start
    delta = end - start
    expected_moves = delta / 0.25
    direction = "HIKE" if delta > 0 else "CUT" if delta < 0 else "HOLD"
    magnitude = abs(expected_moves)

    if magnitude < 1.0:
        move_prob = magnitude
        hold_prob = 1.0 - magnitude
        move_text = f"{direction}_25BP={move_prob * 100:.2f}% | HOLD={hold_prob * 100:.2f}%"
    else:
        whole = int(math.floor(magnitude))
        remainder = magnitude - whole
        base_bp = whole * 25
        next_bp = (whole + 1) * 25
        p_base = 1.0 - remainder
        p_next = remainder
        move_text = f"{direction}_{base_bp}BP={p_base * 100:.2f}% | {direction}_{next_bp}BP={p_next * 100:.2f}%"

    upper_df = fred_series("DFEDTARU")
    lower_df = fred_series("DFEDTARL")
    upper = _clean_num(upper_df["DFEDTARU"].iloc[-1]) if not upper_df.empty else None
    lower = _clean_num(lower_df["DFEDTARL"].iloc[-1]) if not lower_df.empty else None

    return [
        f"FED-MARKTERWARTUNG: FOMC={meeting.isoformat()} | STATUS=CALCULATED | {move_text}",
        f"Implizierter EFFR-Start={start:.4f}% | Implizierter EFFR-Ende={end:.4f}% | Erwartete EFFR-Aenderung={delta:+.4f} Prozentpunkte | Erwartete 25bp-Schritte={expected_moves:+.4f}",
        f"Aktueller Fed-Zielkorridor: Untergrenze={_fmt(lower,4)}% | Obergrenze={_fmt(upper,4)}% | STATUS={'REAL' if lower is not None and upper is not None else 'UNAVAILABLE'}",
        f"Reale Futures: {', '.join(f'{_month_contract(y,m)}={prices[(y,m)]:.4f}' for y,m in required)} | SOURCE={FED_FUTURES_PUBLIC_URL}",
        "Berechnungsmethodik: CME-publizierte FedWatch-Methodik (monatlicher ZQ-Preis = 100 - durchschnittlich erwarteter EFFR; Nicht-FOMC-Ankermonat zur Start-/Endsatz-Rekonstruktion).",
        "WICHTIG: Die Wahrscheinlichkeiten sind MODEL_DERIVED aus realen Futurespreisen; sie sind keine geschaetzten Marktdatenwerte.",
    ]


def _latest_ism_month(today):
    # Wir pruefen den Vormonat und, falls die Seite noch nicht veroeffentlicht ist,
    # den Monat davor. Es wird nie ein zukuenftiger oder geschaetzter PMI eingesetzt.
    for offset in (1, 2, 3):
        first = today.replace(day=1)
        y, m = first.year, first.month - offset
        while m <= 0:
            y -= 1
            m += 12
        return y, m
    return None




def _ism_official_get(url):
    """Abruf der offiziellen ISM-Seite mit kurzen Retries.
    Gibt nur eine HTTP-Antwort zurueck; es werden keine Ersatzwerte erzeugt.
    """
    last_exc = None
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                timeout=10,
                headers=REQUEST_HEADERS,
                allow_redirects=True,
            )
            print(
                f"INFO: ISM official HTTP attempt={attempt + 1} "
                f"status={response.status_code} final_url={response.url}"
            )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_exc = exc
            print(
                f"WARNUNG: ISM official HTTP attempt={attempt + 1} "
                f"fehlgeschlagen: {type(exc).__name__}: {exc}"
            )
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return None



def _extract_te_release_date(text):
    """Extrahiert ein explizites Veröffentlichungsdatum aus einer TE-Seite.
    Gibt YYYY-MM-DD zurück; Reference-Monat bleibt die Datenmonatsprüfung.
    """
    from datetime import datetime
    patterns = (
        r"(?i)\b(?:released?|release date|published|publication date)\b\s*[:\-]?\s*"
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{4}|[A-Z][a-z]+\s+\d{1,2},\s+\d{4})",
        r"(?i)\b(\d{4}-\d{2}-\d{2})\b",
    )
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        raw = m.group(1)
        for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%B %d, %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return raw
    return None

def _te_calendar_actual(html, expected_ref):
    """Liest Actual-Wert und Release-Datum aus der TE-Kalendertabelle.

    Trading Economics hat die Kalenderstruktur mehrfach variiert. Die robuste
    Regel ist deshalb: finde eine Tabelle mit einer expliziten Actual-Spalte,
    finde darin ein Release-Datum im Monat nach dem angeforderten Reference-
    Monat und nimm ausschließlich den Actual-Wert derselben Zeile.
    Forecast/Previous/Consensus werden niemals verwendet.
    """
    try:
        tables = pd.read_html(StringIO(html))
    except Exception:
        return None, None

    try:
        month_abbr, year = expected_ref.split()
        month_num = next(
            i for i in range(1, 13)
            if calendar.month_abbr[i].casefold() == month_abbr.casefold()
        )
        next_year = int(year) + (1 if month_num == 12 else 0)
        next_month = 1 if month_num == 12 else month_num + 1
    except Exception:
        return None, None

    def parse_date(value):
        raw = str(value).strip()
        for fmt in (
            "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y",
            "%d-%m-%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
        ):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                pass
        parsed = pd.to_datetime(raw, errors="coerce", dayfirst=True)
        return parsed.date() if pd.notna(parsed) else None

    for table in tables:
        df = table.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                " ".join(str(x).strip() for x in col
                         if str(x).strip().lower() not in {"nan", "none", ""}).strip()
                for col in df.columns
            ]
        else:
            df.columns = [str(c).strip() for c in df.columns]
        cols = [str(c).casefold() for c in df.columns]
        if "actual" not in cols:
            continue
        actual_idx = cols.index("actual")

        for _, row in df.iterrows():
            cells = [str(v).strip() for v in row.tolist()]
            release_date = None
            for cell in cells:
                d = parse_date(cell)
                if d is not None and d.year == next_year and d.month == next_month:
                    release_date = d
                    break
            if release_date is None or actual_idx >= len(cells):
                continue
            value = _clean_num(cells[actual_idx].replace(",", ""))
            if value is None or not 0.0 <= value <= 100.0:
                continue
            return value, release_date.isoformat()

    return None, None

def _te_text_actual(html, expected_ref, series_label):
    """Fallback bei geänderter TE-Tabellenstruktur; nur expliziter Istwert."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    month_abbr, year = expected_ref.split()
    month_num = next(
        i for i in range(1, 13)
        if calendar.month_abbr[i].casefold() == month_abbr.casefold()
    )
    month_name = calendar.month_name[month_num]

    pattern = (
        rf"{re.escape(series_label)}.*?"
        rf"(?:increased|decreased|rose|fell|edged up|edged down|"
        rf"climbed|dropped|advanced|declined|remained|was unchanged)"
        rf"\s+to\s+(\d+(?:\.\d+)?)"
        rf"(?:\s+points?)?"
        rf"\s+in\s+{re.escape(month_name)}\s+{re.escape(year)}"
    )
    m = re.search(pattern, text, flags=re.I)
    return _clean_num(m.group(1)) if m else None



def _te_release_month_matches_reference(release_date, year, month):
    """Prueft hart, ob das Release-Date im erwarteten Folgemonat liegt.

    ISM-Werte werden typischerweise im Folgemonat veroeffentlicht. Ein
    unbekanntes Release-Date wird NICHT als bestaetigt behandelt.
    """
    if not release_date:
        return False
    try:
        rd = datetime.strptime(release_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False

    expected_month = month + 1
    expected_year = year
    if expected_month == 13:
        expected_month = 1
        expected_year += 1

    return rd.year == expected_year and rd.month == expected_month



# v5.9.6: Official ISM report pages are the preferred public secondary source.
# The report pages contain the complete monthly table (PMI, New Orders,
# Employment, Prices) and are much more stable than scraping narrative text.
ISM_OFFICIAL_REPORT_URLS = {
    "manufacturing": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/{month}/",
    "services": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/{month}/",
}

def _ism_month_slug(month):
    names = (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december"
    )
    return names[int(month) - 1]

def _ism_official_report_url(year, month, kind):
    # ISM's report pages are month-based; year is implicit in the published
    # report page. We validate the requested month from the page itself.
    return ISM_OFFICIAL_REPORT_URLS[kind].format(month=_ism_month_slug(month))

def _ism_official_report_secondary(year, month, kind):
    """
    Parse the official ISM monthly report page as a REAL_PUBLIC_SECONDARY.
    This is deliberately independent from the ISM ecommerce/SSO endpoint.
    All four Tier-1 fields must come from the same report month.
    """
    url = _ism_official_report_url(year, month, kind)
    try:
        r = requests.get(url, timeout=20, headers=REQUEST_HEADERS)
        r.raise_for_status()
        html = r.text
    except Exception as exc:
        print(f"WARNUNG: ISM official report page failed kind={kind}: {exc}")
        return None

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    low = text.casefold()

    sector = "Services" if kind == "services" else "Manufacturing"
    month_name = _ism_month_slug(month).capitalize()
    month_token = f"{month_name} {year}".casefold()

    # Hard evidence that this is the requested monthly report.
    if month_token not in low:
        print(f"WARNUNG: ISM official report page does not match requested month kind={kind} ref={month_token}")
        return None

    patterns = {
        "services": {
            "pmi": r"Services\s+PMI.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
            "new_orders": r"New Orders\s+Index.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
            "employment": r"Employment\s+Index.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
            "prices": r"Prices\s+Index.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
        },
        "manufacturing": {
            "pmi": r"Manufacturing\s+PMI.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
            "new_orders": r"New Orders\s+Index.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
            "employment": r"Employment\s+Index.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
            "prices": r"Prices(?:\s+Index)?\s+.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
        },
    }[kind]

    values = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, flags=re.I)
        if not m:
            print(f"WARNUNG: ISM official report missing {kind}.{key}")
            return None
        try:
            values[key] = float(m.group(1))
        except Exception:
            print(f"WARNUNG: ISM official report invalid {kind}.{key}={m.group(1)!r}")
            return None

    # Independent table/history evidence for the requested month.
    # The report page itself is the authoritative source; no consensus/forecast
    # or previous-month value is accepted.
    values["release_date"] = None
    date_patterns = [
        r"(?:August|September|October|November|December|January|February|March|April|May|June|July)\s+\d{1,2},\s+\d{4}",
        r"\b\d{4}-\d{2}-\d{2}\b",
    ]
    dates = []
    for dp in date_patterns:
        dates.extend(re.findall(dp, text, flags=re.I))
    parsed_dates = []
    for raw in dates:
        for fmt in ("%B %d, %Y", "%Y-%m-%d"):
            try:
                parsed_dates.append(datetime.strptime(raw, fmt).date())
                break
            except Exception:
                pass

    # Prefer a release date in the expected month after the report month.
    expected_year = year + (1 if month == 12 else 0)
    expected_month = 1 if month == 12 else month + 1
    matching = [d for d in parsed_dates if d.year == expected_year and d.month == expected_month]
    if matching:
        values["release_date"] = min(matching).isoformat()
    else:
        # Official ISM schedule: Manufacturing is released on the first
        # business day of the following month; Services on the third business
        # day of the following month. This is a release-calendar date, not a
        # fabricated economic value, and is used only for release-date
        # validation of the official report.
        import calendar as _cal
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        first = datetime(next_year, next_month, 1).date()
        business_days = []
        d = first
        while d.month == next_month:
            if d.weekday() < 5:
                business_days.append(d)
            if len(business_days) >= 3:
                break
            d = d + __import__("datetime").timedelta(days=1)
        idx = 0 if kind == "manufacturing" else 2
        if len(business_days) <= idx:
            print(f"WARNUNG: ISM release schedule calculation failed kind={kind}")
            return None
        values["release_date"] = business_days[idx].isoformat()

    values.update({
        "source": "ISM_OFFICIAL_REPORT",
        "status": "REAL_PUBLIC_SECONDARY",
        "kind": kind,
        "year": year,
        "month": month,
        "reference": f"{year}-{month:02d}",
    })
    return values

def _ism_public_secondary_tradingeconomics(year, month, kind):
    """Robuster oeffentlicher ISM-Secondary-Fallback.

    Vier eigenstaendige TE-Indikatorseiten werden abgefragt. Akzeptiert wird
    nur der Actual-Wert des exakt passenden Reference-Monats. Forecast,
    Consensus und Previous werden niemals als Istwert verwendet.
    """
    if month < 1 or month > 12 or kind not in {"manufacturing", "services"}:
        return None

    if kind == "manufacturing":
        urls = {
            "pmi": "https://tradingeconomics.com/united-states/manufacturing-pmi",
            "new_orders": "https://tradingeconomics.com/united-states/ism-manufacturing-new-orders",
            "employment": "https://tradingeconomics.com/united-states/ism-manufacturing-employment",
            "prices": "https://tradingeconomics.com/united-states/ism-manufacturing-prices",
        }
        labels = {
            "pmi": "Manufacturing PMI",
            "new_orders": "ISM Manufacturing New Orders",
            "employment": "ISM Manufacturing Employment",
            "prices": "ISM Manufacturing Prices",
        }
    else:
        urls = {
            "pmi": "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
            "new_orders": "https://tradingeconomics.com/united-states/ism-non-manufacturing-new-orders",
            "employment": "https://tradingeconomics.com/united-states/ism-non-manufacturing-employment",
            "prices": "https://tradingeconomics.com/united-states/ism-non-manufacturing-prices",
        }
        labels = {
            "pmi": "ISM Services PMI",
            "new_orders": "ISM Services New Orders",
            "employment": "ISM Services Employment",
            "prices": "ISM Services Prices",
        }

    expected_ref = f"{calendar.month_abbr[month]} {year}"
    found = {}
    release_dates = {}

    for key, url in urls.items():
        try:
            r = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
            r.raise_for_status()
            value, release_date = _te_calendar_actual(r.text, expected_ref)
            if value is None:
                value = _te_text_actual(r.text, expected_ref, labels[key])
            if value is None:
                print(
                    f"WARNUNG: TradingEconomics ISM {kind} {key} ohne "
                    f"Actual-Wert fuer Reference={expected_ref}"
                )
                continue
            found[key] = value
            if release_date:
                release_dates[key] = release_date
        except Exception as exc:
            print(
                f"WARNUNG: TradingEconomics ISM {kind} {key} nicht verfuegbar: "
                f"{type(exc).__name__}: {exc}"
            )

    required = ("pmi", "new_orders", "employment", "prices")
    missing = [key for key in required if key not in found]
    if missing:
        print(
            f"WARNUNG: TradingEconomics ISM {kind} unvollstaendig fuer "
            f"{year}-{month:02d}; fehlend={','.join(missing)}"
        )
        return None

    # Sicherheitsregel: Ein Secondary-Datensatz darf nur freigegeben
    # werden, wenn JEDE der vier Serien ein explizites Release-Date liefert
    # und jedes Release-Date im erwarteten Folgemonat des Reference-Monats liegt.
    invalid_release_dates = [
        key for key in required
        if key not in release_dates
        or not _te_release_month_matches_reference(release_dates[key], year, month)
    ]
    if invalid_release_dates:
        print(
            f"WARNUNG: TradingEconomics ISM {kind} Release-Date-Pruefung "
            f"fehlgeschlagen fuer Reference={expected_ref}; "
            f"ungueltig/fehlend={','.join(invalid_release_dates)}"
        )
        return None

    if len(set(release_dates.values())) != 1:
        print(
            f"WARNUNG: TradingEconomics ISM {kind} hat unterschiedliche "
            f"Release-Dates fuer Reference={expected_ref}; kein Secondary-Gate."
        )
        return None

    release_date = next(iter(release_dates.values()))

    print(
        f"INFO: TradingEconomics ISM {kind.title()} vollstaendig: "
        f"report_month={year}-{month:02d} "
        f"PMI={found['pmi']} NewOrders={found['new_orders']} "
        f"Employment={found['employment']} Prices={found['prices']} "
        f"release_date={release_date}"
    )

    return {
        "pmi": found["pmi"],
        "url": " | ".join(urls.values()),
        "year": year,
        "month": month,
        "status": "REAL_PUBLIC_SECONDARY",
        "new_orders": found["new_orders"],
        "employment": found["employment"],
        "prices": found["prices"],
        "release_date": release_date,
    }




def _ism_public_secondary_fxblue_services(year: int, month: int) -> dict | None:
    """Robuster Services-only FX Blue fallback.

    FX Blue fuehrt die vier ISM-Services-Reihen als eigene Kalenderseiten.
    Die Event-Erkennung ist absichtlich tolerant gegen unterschiedliche
    HTML-/Textdarstellungen (voller Wochentag, Abkuerzung, Komma, optionale
    Uhrzeit), waehrend die Datenvalidierung strikt bleibt: Es wird nur das
    Event im Folgemonat des angeforderten Reference-Monats akzeptiert und nur
    ein expliziter Actual-Wert derselben Event-Zeile. Forecast/Previous werden
    niemals als Actual verwendet.
    """
    base = "https://publisher2.fxblue.com/calendar/item"
    urls = {
        "pmi": f"{base}/ISM_Services_PMI_US",
        "new_orders": f"{base}/ISM_Services_New_Orders_Index_US",
        "employment": f"{base}/ISM_Services_Employment_Index_US",
        "prices": f"{base}/ISM_Services_Prices_Paid_US",
    }

    next_year = year + (1 if month == 12 else 0)
    next_month = 1 if month == 12 else month + 1
    found: dict[str, float] = {}
    release_dates: dict[str, str] = {}

    # FX Blue currently exposes e.g. "Wednesday 5 August 2026 14:00".
    # CI/HTML variants may contain commas or abbreviated weekday/month names,
    # therefore event discovery must not depend on one literal rendering.
    date_re = re.compile(
        r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
        r"Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.?\s*,?\s*)?"
        r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})"
        r"(?:\s*,?\s+(\d{1,2}:\d{2}))?",
        re.I,
    )

    month_lookup = {calendar.month_name[i].casefold(): i for i in range(1, 13)}
    month_lookup.update({calendar.month_abbr[i].casefold(): i for i in range(1, 13)})

    def parse_page(raw_text: str, key: str):
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw_text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        low = text.casefold()
        marker = low.find("past events")
        history = text[marker:] if marker >= 0 else text
        events = list(date_re.finditer(history))

        print(
            f"INFO: FXBlue ISM services {key}: Event-Suche Reference={year}-{month:02d} "
            f"target_release={next_year}-{next_month:02d} candidates={len(events)}"
        )

        target_idx = None
        target_date = None
        for idx, m in enumerate(events):
            day, month_name, release_year = m.group(1), m.group(2), m.group(3)
            month_num = month_lookup.get(month_name.casefold().rstrip('.'))
            if month_num is None:
                continue
            try:
                d = datetime(int(release_year), month_num, int(day)).date()
            except (ValueError, TypeError):
                continue
            if d.year == next_year and d.month == next_month:
                target_idx = idx
                target_date = d
                break

        if target_idx is None:
            # Diagnostic only: show the discovered dates, never arbitrary values.
            preview = []
            for m in events[:12]:
                mn = month_lookup.get(m.group(2).casefold().rstrip('.'))
                if mn:
                    preview.append(f"{m.group(1)}-{mn:02d}-{m.group(3)}")
            print(
                f"WARNUNG: FXBlue ISM services {key}: kein Event fuer "
                f"target_release={next_year}-{next_month:02d}; "
                f"erkannte_daten={','.join(preview) if preview else 'NONE'}"
            )
            return None, None

        row_end = events[target_idx + 1].start() if target_idx + 1 < len(events) else len(history)
        row = history[events[target_idx].end():row_end]

        # 1) Strict explicit Actual signal. Accept the labels used by FX Blue
        # and common machine-readable variants, but never infer Actual from an
        # arbitrary numeric token. Percent signs/whitespace are harmless; N/A
        # and similar markers remain terminal invalid values.
        invalid_actual = {"-", "–", "—", "n/a", "na", "null", "none", "previous", "forecast", "actual", ""}
        actual_patterns = (
            r"\bActual(?:\s+(?:Value|Value/Outcome))?\b\s*[:=]?\s*([^\s|;,<]+)",
            r"\bactualValue\b\s*[:=]?\s*([^\s|;,<}]+)",
            r"\boutcome\b\s*[:=]?\s*([^\s|;,<}]+)",
        )
        for pattern in actual_patterns:
            actual_matches = list(re.finditer(pattern, row, flags=re.I))
            if not actual_matches:
                continue
            raw_actual = actual_matches[-1].group(1).strip().strip('\"\'')
            if raw_actual.casefold() in invalid_actual:
                print(f"WARNUNG: FXBlue ISM services {key}: explizites Actual ist ungueltig ({raw_actual!r})")
                return target_date, None
            # Accept ordinary numeric formatting such as 54.1, 54,1 or 54.1%.
            numeric = re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?%?", raw_actual)
            if not numeric:
                print(f"WARNUNG: FXBlue ISM services {key}: Actual nicht numerisch ({raw_actual!r})")
                return target_date, None
            value = _clean_num(raw_actual.rstrip('%').replace(',', '.'))
            if value is None:
                print(f"WARNUNG: FXBlue ISM services {key}: Actual nicht verwertbar ({raw_actual!r})")
                return target_date, None
            return target_date, value

        # 2) Machine-readable HTML attributes. Only explicit Actual attributes
        # are accepted; generic data-value is deliberately NOT accepted.
        attr_matches = re.findall(
            r"data-(?:actual|actual-value|actualvalue|outcome)\s*=\s*[\"\']([^\"\']*)",
            row, flags=re.I
        )
        for raw_actual in attr_matches:
            raw_actual = raw_actual.strip()
            if raw_actual.casefold() in invalid_actual:
                return target_date, None
            if not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?%?", raw_actual):
                continue
            value = _clean_num(raw_actual.rstrip('%').replace(',', '.'))
            if value is not None:
                return target_date, value

        # 3) Strict three-column fallback only: Forecast | Actual | Previous.
        # The second cell must itself be numeric; '-' / N/A / blank is invalid.
        try:
            tables = pd.read_html(StringIO(raw_text))
        except Exception:
            tables = []
        for table in tables:
            df = table.copy()
            if isinstance(df.columns, pd.MultiIndex):
                cols = [
                    " ".join(str(x).strip() for x in col
                             if str(x).strip().lower() not in {"nan", "none", ""}).strip()
                    for col in df.columns
                ]
            else:
                cols = [str(c).strip() for c in df.columns]
            norm_cols = [re.sub(r"[^a-z]+", " ", c.casefold()).strip() for c in cols]
            if len(cols) == 3 and norm_cols == ["forecast", "actual", "previous"]:
                for _, values in df.iterrows():
                    cells = [str(v).strip() for v in values.tolist()]
                    if len(cells) != 3:
                        continue
                    value = _clean_num(cells[1].rstrip('%').replace(',', '.'))
                    if value is not None:
                        return target_date, value

            # Some FX Blue renderings expose the three headers as the first
            # table row rather than dataframe column names. Accept that only
            # when the table is exactly three columns and the row is literally
            # Forecast | Actual | Previous.
            if len(df.columns) == 3:
                for ridx, values in df.iterrows():
                    header = [re.sub(r"[^a-z]+", " ", str(v).casefold()).strip() for v in values.tolist()]
                    if header != ["forecast", "actual", "previous"]:
                        continue
                    for _, data_values in df.iloc[ridx + 1:].iterrows():
                        cells = [str(v).strip() for v in data_values.tolist()]
                        if len(cells) != 3:
                            continue
                        value = _clean_num(cells[1].rstrip('%').replace(',', '.'))
                        if value is not None:
                            return target_date, value

        print(f"WARNUNG: FXBlue ISM services {key}: Event gefunden, aber kein valides Actual")
        return target_date, None

    for key, url in urls.items():
        try:
            r = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
            r.raise_for_status()
            release_date, value = parse_page(r.text, key)
            if release_date is None:
                continue
            if value is None or not 0.0 <= value <= 100.0:
                print(f"WARNUNG: FXBlue ISM services {key} ungueltiger Actual-Wert")
                continue
            found[key] = value
            release_dates[key] = release_date.isoformat()
        except Exception as exc:
            print(
                f"WARNUNG: FXBlue ISM services {key} nicht verfuegbar: "
                f"{type(exc).__name__}: {exc}"
            )

    required = ("pmi", "new_orders", "employment", "prices")
    missing = [key for key in required if key not in found]
    if missing:
        print(
            f"WARNUNG: FXBlue ISM services unvollstaendig fuer {year}-{month:02d}; "
            f"fehlend={','.join(missing)}"
        )
        return None

    if len(set(release_dates.values())) != 1:
        print(
            "WARNUNG: FXBlue ISM services unterschiedliche Release-Dates; "
            "Secondary-Datensatz verworfen."
        )
        return None

    release_date = next(iter(release_dates.values()))
    print(
        f"INFO: FXBlue ISM Services vollstaendig: "
        f"report_month={year}-{month:02d} PMI={found['pmi']} "
        f"NewOrders={found['new_orders']} Employment={found['employment']} "
        f"Prices={found['prices']} release_date={release_date}"
    )
    return {
        "pmi": found["pmi"],
        "new_orders": found["new_orders"],
        "employment": found["employment"],
        "prices": found["prices"],
        "url": " | ".join(urls.values()),
        "year": year,
        "month": month,
        "reference": f"{year}-{month:02d}",
        "release_date": release_date,
        "source": "FXBLUE_ISM_SERVICES",
        "status": "REAL_PUBLIC_SECONDARY",
        "kind": "services",
    }

def _ism_extract_official(text, kind):
    """Extrahiert den offiziellen ISM-Monatsbericht.
    Primär wird die HTML-Tabelle verwendet, weil dort aktueller Wert,
    Previous und Change eindeutig getrennt sind. Narrative Textsuche bleibt
    nur als Fallback. Keine Forecast-/Consensus-Werte.
    """
    raw_html = text
    clean = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw_html, flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    pmi_label = "Manufacturing PMI" if kind == "manufacturing" else "Services PMI"

    def norm(x):
        return re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()

    def num(x):
        return _clean_num(str(x).replace(",", "").replace("%", "").strip())

    try:
        tables = pd.read_html(StringIO(raw_html))
    except Exception:
        tables = []

    def flatten_columns(df):
        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                " ".join(
                    str(x).strip() for x in col
                    if str(x).strip().lower() not in {"nan", "none", ""}
                ).strip() for col in df.columns
            ]
        else:
            df.columns = [str(c).strip() for c in df.columns]
        return df

    def row_value(labels):
        targets={norm(x) for x in labels}
        for table in tables:
            df=flatten_columns(table)
            for _,row in df.iterrows():
                cells=[str(v).strip() for v in row.tolist()]
                if not cells or norm(cells[0]) not in targets:
                    continue
                for cell in cells[1:]:
                    value=num(cell)
                    if value is not None and 0 <= value <= 100:
                        return value
        return None

    pmi=row_value([pmi_label])
    if pmi is None:
        for pat in [
            rf"\b{re.escape(pmi_label)}\b(?:\^?®)?\s+(?:registered|at)\s+(\d+(?:\.\d+)?)",
            rf"\b{re.escape(pmi_label)}\b[^0-9]{{0,180}}?(\d+(?:\.\d+)?)\s*%",
        ]:
            m=re.search(pat,clean,re.I)
            if m:
                pmi=num(m.group(1)); break
    if pmi is None:
        return None

    data={"pmi":pmi,"new_orders":row_value(["New Orders"]),
          "employment":row_value(["Employment"]),
          "prices":row_value(["Prices"])}

    for key,label in (("new_orders","New Orders"),("employment","Employment"),("prices","Prices")):
        if data[key] is not None:
            continue
        for pat in [
            rf"\b{re.escape(label)}(?:\s+Index)?\b\s+(?:registered|at|reading of)\s+(\d+(?:\.\d+)?)",
            rf"\b{re.escape(label)}(?:\s+Index)?\b[^0-9]{{0,180}}?(\d+(?:\.\d+)?)\s*%",
        ]:
            m=re.search(pat,clean,re.I)
            if m:
                data[key]=num(m.group(1)); break
    return data


def _ism_cache_entry_valid(entry, kind, year, month):
    """Accept cache only when reference month and all four Tier-1 values match."""
    if not isinstance(entry, dict):
        return False
    ref = str(entry.get("reference", entry.get("period", ""))).strip()
    expected = f"{year}-{month:02d}"
    if ref not in {expected, f"{year}-{month}"}:
        # Legacy cache format may store year/month separately.
        try:
            if int(entry.get("year")) != int(year) or int(entry.get("month")) != int(month):
                return False
        except (TypeError, ValueError):
            return False

    # Cache generations used several equivalent field names and sometimes
    # stored numeric values as strings containing a percent sign. Normalize
    # aliases here; the four Tier-1 values still remain mandatory.
    aliases = {
        "pmi": ("pmi", "PMI", "services_pmi", "manufacturing_pmi"),
        "new_orders": ("new_orders", "newOrders", "New Orders", "new_orders_index"),
        "employment": ("employment", "Employment", "employment_index"),
        "prices": ("prices", "Prices", "prices_index", "prices_paid"),
    }
    for key, names in aliases.items():
        value = next((entry.get(name) for name in names if entry.get(name) is not None), None)
        try:
            value = _clean_num(str(value).replace("%", ""))
        except Exception:
            value = None
        if value is None or not (0.0 <= float(value) <= 100.0):
            return False
    return True


def _ism_cache_get_valid(kind, year, month):
    try:
        cache=_cache_load()
    except Exception:
        return None
    root=cache.get("ism", cache.get("ISM", {})) if isinstance(cache,dict) else {}
    candidates=[]
    if isinstance(root,dict):
        candidates += [
            root.get(kind),
            root.get(f"{kind}_{year}_{month:02d}"),
            root.get(f"{kind}_{year}_{month}"),
            root.get(f"{kind}_{year}_{month:d}"),
        ]
        # Some legacy/current cache files use keys like
        # "manufacturing" / "services" whose payload carries year/month.
    # Also support the project's known list-style cache entries.
    entries=cache.get("ism_entries", []) if isinstance(cache,dict) else []
    if isinstance(entries,list):
        candidates += [e for e in entries if isinstance(e,dict) and e.get("kind")==kind]
    for entry in candidates:
        # ISM cache entries are stored as a wrapper:
        # {"saved_at": ..., "data": {...}, "status": ..., "source": ...}
        # Validate the actual data payload, not the wrapper.
        payload = entry.get("data") if isinstance(entry, dict) and isinstance(entry.get("data"), dict) else entry
        if isinstance(payload, dict):
            # Some cache generations stored year/month/reference on the wrapper
            # while the actual four values lived below data. Preserve that
            # metadata for validation instead of rejecting an otherwise complete
            # current-month cache.
            candidate = dict(payload)
            if isinstance(entry, dict):
                for meta_key in ("reference", "period", "year", "month"):
                    if meta_key not in candidate and meta_key in entry:
                        candidate[meta_key] = entry[meta_key]
        else:
            candidate = payload
        if _ism_cache_entry_valid(candidate,kind,year,month):
            # Return a canonical schema so downstream code never depends on
            # which legacy alias happened to be stored in the cache.
            alias_map = {
                "pmi": ("pmi", "PMI", "services_pmi", "manufacturing_pmi"),
                "new_orders": ("new_orders", "newOrders", "New Orders", "new_orders_index"),
                "employment": ("employment", "Employment", "employment_index"),
                "prices": ("prices", "Prices", "prices_index", "prices_paid"),
            }
            result = dict(candidate)
            for canonical, names in alias_map.items():
                result[canonical] = next(
                    candidate.get(name) for name in names if candidate.get(name) is not None
                )
                result[canonical] = _clean_num(str(result[canonical]).replace("%", ""))
            if isinstance(entry, dict):
                result.setdefault("status", entry.get("status", "REAL_CACHED"))
                result.setdefault("source", entry.get("source", "ISM_SECONDARY_CACHE"))
            return result
    return None
def _ism_fetch(kind, year, month):
    # A complete, month-matching cache entry is a valid resilience path.
    # It is explicitly validated; it is never accepted merely because it exists.
    cached = _ism_cache_get_valid(kind, year, month)
    if cached is not None:
        print(
            f"INFO: ISM-Secondary-Cache validiert und uebernommen fuer {kind}: "
            f"reference={year}-{month:02d} (PMI + 3 Unterpunkte vorhanden)."
        )
        result = dict(cached)
        result["year"] = year
        result["month"] = month
        result["status"] = result.get("status", "REAL_CACHED")
        result["source"] = result.get("source", "ISM_SECONDARY_CACHE")
        return result

    month_name = calendar.month_name[month].lower()

    # Primary: official ISM release.
    official = (
        f"https://www.ismworld.org/supply-management-news-and-reports/"
        f"reports/ism-pmi-reports/{'pmi' if kind == 'manufacturing' else 'services'}/{month_name}/"
    )

    try:
        r = _ism_official_get(official)
        if r.status_code == 200:
            final_url = r.url.lower()
            if "login.aspx" in final_url or "sso" in final_url:
                print(f"WARNUNG: ISM official redirected to SSO/login: {r.url}")
            else:
                parsed = _ism_extract_official(r.text, kind)
                if parsed is not None:
                    data = {
                        **parsed,
                        "url": official,
                        "year": year,
                        "month": month,
                        "status": "REAL",
                    }
                    missing_fields = [k for k in ("new_orders", "employment", "prices") if data[k] is None]
                    if missing_fields:
                        print(
                            f"WARNUNG: ISM {kind} official PMI erkannt, aber Unterkomponenten fehlen: "
                            f"{', '.join(missing_fields)}; Secondary-Fallback wird versucht"
                        )
                    else:
                        print(
                            f"INFO: ISM {kind.title()} official vollstaendig: "
                            f"PMI={data['pmi']} NewOrders={data['new_orders']} "
                            f"Employment={data['employment']} Prices={data['prices']}"
                        )
                        return data

    except Exception as exc:
        print(f"WARNUNG: ISM {kind} {year}-{month:02d} official nicht verfuegbar: {exc}")

    # Secondary: Trading Economics. Nur reale, bereits veroeffentlichte
    # "Last"-Werte des exakt passenden Berichtsmonats; niemals Forecast oder Previous.
    secondary = _ism_official_report_secondary(year, month, kind)
    if secondary:
        return secondary

    secondary = _ism_public_secondary_tradingeconomics(year, month, kind)
    if secondary:
        return secondary

    # Tertiary public fallback: independent event calendar. It is used only
    # after the official report and Trading Economics have failed validation.
    if kind == "services":
        secondary = _ism_public_secondary_fxblue_services(year, month)
        if secondary:
            return secondary
    return None

def ism_snapshot(today):
    cache=_cache_load()
    candidates=[]
    first=today.replace(day=1)
    for offset in range(1,4):
        y,m=first.year,first.month-offset
        while m<=0: y-=1; m+=12
        candidates.append((y,m))

    def get(kind):
        key = kind
        latest_y, latest_m = candidates[0]

        # EIN autoritativer Cachepfad: zuerst den aktuell faelligen Monat
        # validieren. Statusnamen wie REAL_PUBLIC_SECONDARY duerfen einen
        # vollstaendig validierten Cache niemals erneut zurueckweisen.
        cached = _ism_cache_get_valid(kind, latest_y, latest_m)
        if cached is not None:
            print(
                f"INFO: ISM-Secondary-Cache validiert und uebernommen fuer {kind}: "
                f"reference={latest_y}-{latest_m:02d} (PMI + 3 Unterpunkte vorhanden)."
            )
            return cached

        entry = cache.get("ism", {}).get(key)
        if entry and entry.get("data"):
            d = entry["data"]
            print(
                f"INFO: ISM-Cache vorhanden, aber nicht verwendbar fuer {key}: "
                f"cached={d.get('year')}-{d.get('month')} required={latest_y}-{latest_m}; "
                f"validierung fehlgeschlagen."
            )

        for y, m in candidates:
            d = _ism_fetch(kind, y, m)
            if d:
                with CACHE_WRITE_LOCK:
                    c = _cache_load()
                    c.setdefault("ism", {})[key] = {
                        "saved_at": time.time(),
                        "data": d,
                        "status": d.get("status", "REAL"),
                        "source": d.get("url", d.get("source", "ISM")),
                    }
                    _cache_save(c)
                return d
        return None

    manufacturing=get("manufacturing")
    services=get("services")
    lines=[]
    if manufacturing:
        lines.append(f"ISM Manufacturing PMI: {manufacturing['pmi']:.1f} | Datenmonat={manufacturing['year']}-{manufacturing['month']:02d} | New Orders={_fmt(manufacturing['new_orders'],1)} | Employment={_fmt(manufacturing['employment'],1)} | Prices={_fmt(manufacturing['prices'],1)} | STATUS={manufacturing.get("status","REAL")} | SOURCE={manufacturing["url"]}")
    else:
        lines.append("ISM Manufacturing PMI: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=ISM")
    if services:
        lines.append(f"ISM Services PMI: {services['pmi']:.1f} | Datenmonat={services['year']}-{services['month']:02d} | New Orders={_fmt(services['new_orders'],1)} | Employment={_fmt(services['employment'],1)} | Prices={_fmt(services['prices'],1)} | STATUS={services.get("status","REAL")} | SOURCE={services["url"]}")
    else:
        lines.append("ISM Services PMI: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=ISM")
    lines.append("PMI-Regel: >50 = Expansion des jeweiligen Sektors; <50 = Kontraktion. Keine Prognose des naechsten PMI-Werts.")
    return lines



def market_snapshots_parallel():
    """Laedt Markt-/Rohstoffhistorien parallel und nutzt den persistenten Cache."""
    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(market_snapshot, name, ticker, data_type): name
            for name, (ticker, data_type) in MARKET_DATA.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                ticker, data_type = MARKET_DATA[name]
                results[name] = f"{name}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE={ticker or 'keine'} | DATENTYP={data_type} | FEHLER={exc}"
    return [results[name] for name in MARKET_DATA]

def fred_snapshots_parallel(names):
    """Laedt FRED-Serien parallel. Cache-Treffer erzeugen keinen Netzwerkaufruf.

    Das reduziert die Laufzeit bei einem FRED-Ausfall von vielen seriellen
    Timeouts auf ungefaehr ein Timeout-Fenster, ohne Datenregeln zu lockern.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(names)))) as pool:
        futures = {pool.submit(fred_snapshot, name, FRED_SERIES[name]): name for name in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = f"{name}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=FRED {FRED_SERIES[name]} | FEHLER={exc}"
    return [results[name] for name in names]

def data_quality_gate(lines):
    # Tier 1: Core-/Gate-Daten. Fehlt eine dieser Datenreihen bzw. ist sie
    # aufgrund der bestehenden Daten-/Cache-Logik nicht belastbar verfuegbar,
    # wird das Makro-Szenario gesperrt.
    tier1_labels = [
        "Fed Funds Effective Rate", "US 2Y Treasury", "US 10Y Treasury",
        "Core CPI", "NFP / Nonfarm Payrolls", "Arbeitslosenquote",
        "ISM Manufacturing PMI", "ISM Services PMI", "S&P 500",
    ]

    # ISM-Unterkomponenten stehen als Felder innerhalb der PMI-Zeile.
    ism_component_fields = (
        ("ISM Manufacturing PMI", "New Orders"),
        ("ISM Manufacturing PMI", "Employment"),
        ("ISM Manufacturing PMI", "Prices"),
        ("ISM Services PMI", "New Orders"),
        ("ISM Services PMI", "Employment"),
        ("ISM Services PMI", "Prices"),
    )

    # Tier 2: Sekundaere Kontextdaten. Fehlende/zu alte Daten verschlechtern
    # die Datenqualitaet, sperren das Szenario aber nicht.
    tier2_labels = [
        "PCE",
        "Core PCE",
        "Realzins 10Y TIPS",
        "US High Yield OAS",
        "Chicago Fed NFCI",
        "VIX",
        "DXY",
        "Reales BIP-Wachstum",
        "M2",
        "JOLTS Job Openings",
        "Industrieproduktion",
        "Consumer Sentiment",
        "Kapazitaetsauslastung",
        "SLOOS C&I Tightening",
        "US Investment Grade OAS",
    ]

    # Tier 3: Optionale Anreicherungsdaten. Sie haben keinen Einfluss auf
    # Gate oder Datenqualitaetsstatus.
    tier3_labels = [
        "GSCPI",
        "Global Economic Policy Uncertainty",
        "US Federal Debt/GDP",
    ]

    def _unavailable(label):
        return any(line.startswith(label + ": NICHT VERFUEGBAR") for line in lines)

    critical_missing = [label for label in tier1_labels if _unavailable(label)]

    # Komponenten aus den tatsaechlich erzeugten PMI-Zeilen pruefen.
    for pmi_label, field in ism_component_fields:
        matching = [line for line in lines if line.startswith(pmi_label + ":")]
        if not matching:
            critical_missing.append(f"{pmi_label} {field}")
            continue
        m = re.search(rf"(?:^|\|)\s*{re.escape(field)}\s*=\s*([^|]+)", matching[0], flags=re.I)
        if not m or m.group(1).strip().upper() in {"NICHT VERFUEGBAR", "UNAVAILABLE", "NONE", "N/A"}:
            critical_missing.append(f"{pmi_label} {field}")

    secondary_missing = [label for label in tier2_labels if _unavailable(label)]

    # LME is secondary/quality-relevant, never gate-critical.
    # LME outages may degrade data quality, but can NEVER block the macro scenario.
    lme_missing = [
        metal for metal in ("Nickel", "Blei", "Zinn", "Kobalt")
        if any(
            (line.startswith(metal + ":") or line.startswith("LME " + metal + ":"))
            and (
                "NICHT VERFUEGBAR" in line.upper()
                or re.search(r"STATUS=(?:DEGRADED|UNAVAILABLE)\b", line, flags=re.I)
            )
            for line in lines
        )
    ]
    secondary_missing.extend(f"LME {metal}" for metal in lme_missing)

    gate = "GESPERRT" if critical_missing else "FREIGEGEBEN"
    data_quality = "BLOCKED" if critical_missing else ("DEGRADED" if secondary_missing else "HEALTHY")
    missing = critical_missing
    return gate, missing, data_quality, secondary_missing


def main():
    today = dt.date.today()
    cache = _cache_load()
    fred_cache_count = sum(1 for e in cache.get("fred", {}).values() if e.get("payload"))
    ism_cache_count = sum(1 for e in cache.get("ism", {}).values() if e.get("data"))
    market_cache_count = sum(1 for e in cache.get("market", {}).values() if e.get("payload"))
    print(
        f"MAKRO-CACHE: version={cache.get('version')} | "
        f"FRED_ENTRIES={fred_cache_count} | ISM_ENTRIES={ism_cache_count} | "
        f"MARKET_ENTRIES={market_cache_count} | FILE={MACRO_CACHE_FILE}"
    )
    output = f"Makro_Briefing({today.isoformat()}).txt"
    lines = []
    lines += [
        "NEUBER MACRO & MARKETS",
        f"MAKRO-DATENPAKET | Datenabruf={today.isoformat()}",
        "HARTE DATENREGEL: Keine Zahl wird geschaetzt. Fehlende Werte bleiben NICHT VERFUEGBAR.",
        "STATUS: REAL = Originalwert/Primaerquelle | REAL_PUBLIC_SECONDARY = echter Wert aus oeffentlicher Sekundaerquelle | REAL_CACHED = echter gespeicherter Originalwert, Quelle im Lauf nicht neu erreichbar | CALCULATED = deterministisch berechnet | PROXY = Proxy | MODEL_DERIVED = Modellresultat | UNAVAILABLE = keine belastbare Zahl",
        "",
        "1. MONETAERES UMFELD, ZINSEN & LIQUIDITAET",
    ]
    lines.extend(fred_snapshots_parallel([
        "Fed Funds Effective Rate", "Fed Target Range Lower", "Fed Target Range Upper",
        "ECB Deposit Facility Rate", "M2", "Realzins 10Y TIPS",
        "US 2Y Treasury", "US 5Y Treasury", "US 10Y Treasury", "US 30Y Treasury",
    ]))
    lines.extend(fed_expectation_snapshot(today))
    lines.append("")

    lines.append("2. INFLATION, ARBEIT & KONJUNKTUR")
    lines.extend(fred_snapshots_parallel([
        "CPI", "Core CPI", "PCE", "Core PCE", "PPI", "Durchschnittlicher Stundenlohn",
        "Arbeitslosenquote", "NFP / Nonfarm Payrolls", "JOLTS Job Openings",
        "Initial Jobless Claims", "Industrieproduktion", "Kapazitaetsauslastung", "Consumer Sentiment",
    ]))
    lines.append(bea_gdp_snapshot())
    lines.extend(ism_snapshot(today))
    lines.append("")

    lines.append("3. KREDIT, FINANCIAL CONDITIONS & RISIKO")
    lines.extend(fred_snapshots_parallel(["SLOOS C&I Tightening", "US High Yield OAS", "US Investment Grade OAS", "Chicago Fed NFCI"]))
    lines.append("")

    lines.append("4. EXOGENE FAKTOREN, LIEFERKETTEN & FISKAL")
    lines.append(gscpi_snapshot())
    lines.extend(fred_snapshots_parallel(["Global Economic Policy Uncertainty", "US Federal Debt/GDP"]))
    lines.append("Geopolitik: kein kuenstlicher Tages-Score. Nur konkret belegte Ereignisse aus den bereitgestellten Quellen duerfen interpretiert werden.")
    lines.append("")

    lines.append("5. MARKT, FX, KRYPTO & ROHSTOFFE")
    lines.extend(market_snapshots_parallel())
    lines.append("")

    gate, missing, data_quality, secondary_missing = data_quality_gate(lines)
    lines.append("6. DATENQUALITAETS-GATEKEEPER")
    lines.append(f"MAKRO-SZENARIO-GATE: {gate}")
    lines.append(f"DATENQUALITAET: {data_quality}")
    lines.append(f"SEKUNDAERE DATENLUECKEN: {', '.join(secondary_missing) if secondary_missing else 'KEINE'}")
    lme_missing_report = [m for m in ("Nickel", "Blei", "Zinn", "Kobalt")
                           if f"LME {m}" in secondary_missing]
    lines.append(
        f"LME-QUALITAET: {'DEGRADED' if lme_missing_report else 'HEALTHY'}"
        + (f" ({', '.join(lme_missing_report)})" if lme_missing_report else "")
    )
    lines.append(f"KRITISCHE DATENLUECKEN: {', '.join(missing) if missing else 'KEINE'}")
    lines.append("REGEL: Bei GESPERRT darf keine Base/Bull/Bear-Prognose mit Zahlen ausgegeben werden. Die Tagesauswertung darf die bestehende regelbasierte Analyse trotzdem weiter ausgeben.")
    lines.append("CACHE-REGEL: REAL_CACHED darf nur verwendet werden, wenn der gespeicherte Originalwert innerhalb seiner definierten Datenaltersgrenze liegt. Es werden keine Werte fortgeschrieben oder geschaetzt.")
    lines.append("")

    lines.append("7. INTERPRETATIONSREGELN FUER GEMINI")
    lines.append("Makroachsen: Wachstum | Inflation | Geldpolitik | Liquiditaet | Kredit | Risk Appetite | Bewertung | Angebotsschock | struktureller Capex-Zyklus.")
    lines.append("Horizonte: 1-4 Wochen | 1-3 Monate | 3-6 Monate | >6 Monate.")
    lines.append("Szenarien: Base Case | Bull Case | Bear Case. Szenario-Wahrscheinlichkeiten sind MODEL_DERIVED, niemals reale Marktdaten und niemals geschaetzte Eingangsdaten.")
    lines.append("Wenn der Makro-Szenario-Gate GESPERRT ist: KEINE Szenario-Wahrscheinlichkeiten und KEINE erfundenen Ersatzwerte. Stattdessen Datenluecke benennen.")
    lines.append("Lithium ist als struktureller Speicher-/Batterie-/Netzausbau-Indikator zu interpretieren. PROXY-Daten duerfen niemals als Original-Rohstoffpreise bezeichnet werden.")

    with open(output, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Makro-Datenpaket gespeichert: {output}")
    print(f"MAKRO-SZENARIO-GATE={gate}")
    print(f"MAKRO-DATENQUALITAET={data_quality}")
    if secondary_missing:
        print("SEKUNDAERE_DATENLUECKEN=" + ", ".join(secondary_missing))
    if missing:
        print("KRITISCHE_DATENLUECKEN=" + ", ".join(missing))


if __name__ == "__main__":
    main()
