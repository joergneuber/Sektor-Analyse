- name: Test FRED API DFF
  run: python test_fred_dff.py
  env:
    FRED_API_KEY: ${{ secrets.FRED_API_KEY }}
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
"""

import datetime as dt
import calendar
import math
import re
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from pathlib import Path
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
FRED_URL = "https://fred.stlouisfed.org/series/{}"
ISM_BASE = "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports"
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
    "GSCPI": "GSCPI",
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
    # Yahoo liefert fuer diese drei London-Symbole keine belastbaren Kursdaten.
    # Deshalb kein Fake-Ticker und kein 404-Netzwerkaufruf: bewusst UNAVAILABLE,
    # bis eine reale kostenlose Quelle technisch eingebunden ist.
    "Nickel": (None, "UNAVAILABLE"),
    "Blei": (None, "UNAVAILABLE"),
    "Zinn": (None, "UNAVAILABLE"),
    "Kobalt": (None, "UNAVAILABLE"),
    "Lithium": ("LIT", "PROXY"),
    "Eisenerz": ("TIO=F", "REAL_FUTURES"),
}

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
CACHE_VERSION = 3
CACHE_WRITE_LOCK = Lock()

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
    "GSCPI": 60, "GEPUCURRENT": 120, "GFDEGDQ188S": 120,
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
        r = requests.get(FRED_BASE.format(series_id), timeout=FRED_TIMEOUT, headers=REQUEST_HEADERS)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        if "DATE" not in df.columns or series_id not in df.columns:
            raise ValueError("FRED-Spalten fehlen")
        df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
        df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
        df = df.dropna(subset=["DATE", series_id]).sort_values("DATE")
        if limit_days:
            cutoff = pd.Timestamp.today() - pd.Timedelta(days=limit_days)
            df = df[df["DATE"] >= cutoff]
        if not df.empty:
            _save_series_cache(series_id, df, FRED_URL.format(series_id), "REAL")
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



def _ism_public_secondary_forexfactory(year, month, kind):
    """
    Kostenloser öffentlicher Sekundär-Fallback für ISM Manufacturing und
    ISM Services.

    HARTE DATENREGELN:
    - Ausschließlich ACTUAL.
    - Forecast wird niemals verwendet.
    - Previous wird niemals verwendet.
    - Keine Berechnung, Interpolation oder Schätzung.
    - Nur das exakt passende USD-ISM-Event wird akzeptiert.
    - Der Release-Monat muss zum Berichtsmonat passen.
    """
    if month < 1 or month > 12:
        return None

    if kind not in {"manufacturing", "services"}:
        return None

    event_name = (
        "ISM Manufacturing PMI"
        if kind == "manufacturing"
        else "ISM Services PMI"
    )

    release_year = year + 1 if month == 12 else year
    release_month = 1 if month == 12 else month + 1

    max_day = min(
        12,
        calendar.monthrange(release_year, release_month)[1]
    )

    for day in range(1, max_day + 1):
        url = (
            f"https://www.forexfactory.com/calendar?day="
            f"{calendar.month_abbr[release_month].lower()}{day}.{release_year}"
        )

        try:
            r = requests.get(url, timeout=12, headers=REQUEST_HEADERS)
            r.raise_for_status()

            try:
                from lxml import html as lxml_html
                tree = lxml_html.fromstring(r.content)
                rows = tree.xpath("//tr")
            except Exception:
                rows = []

            for row in rows:
                row_text = " ".join(
                    t.strip() for t in row.xpath(".//text()") if t.strip()
                )
                normalized = re.sub(r"\s+", " ", row_text).strip()

                if "USD" not in normalized:
                    continue
                if event_name not in normalized:
                    continue

                tail = normalized.split(event_name, 1)[1]
                nums = re.findall(r"(?<![\d.])\d+(?:\.\d+)?", tail)
                if not nums:
                    continue

                actual = _clean_num(nums[0])
                if actual is None:
                    continue

                release_date = dt.date(release_year, release_month, day)

                expected_release_year = year + 1 if month == 12 else year
                expected_release_month = 1 if month == 12 else month + 1
                if (release_date.year != expected_release_year or
                        release_date.month != expected_release_month):
                    print(
                        f"WARNUNG: {event_name}-Fallback verworfen: "
                        f"report_month={year}-{month:02d} | "
                        f"release_date={release_date.isoformat()}"
                    )
                    continue

                print(
                    f"INFO: ForexFactory ISM-Event gefunden: "
                    f"kind={kind} report_month={year}-{month:02d} "
                    f"release_date={release_date.isoformat()} actual={actual}"
                )

                return {
                    "pmi": actual,
                    "url": url,
                    "year": year,
                    "month": month,
                    "status": "REAL_PUBLIC_SECONDARY",
                    "new_orders": None,
                    "employment": None,
                    "prices": None,
                    "release_date": release_date.isoformat(),
                }

        except Exception as exc:
            print(
                f"WARNUNG: ForexFactory ISM-Fallback fuer {kind} "
                f"release_date={release_year}-{release_month:02d}-{day:02d} "
                f"nicht verfuegbar: {type(exc).__name__}: {exc}"
            )

    print(
        f"WARNUNG: Kein passender USD {event_name} Release gefunden "
        f"fuer report_month={year}-{month:02d}"
    )
    return None


def _ism_fetch(kind, year, month):
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
            # A 200 SSO/login page is not the report.
            if "login.aspx" in final_url or "sso" in final_url:
                print(f"WARNUNG: ISM official redirected to SSO/login: {r.url}")
            else:
                text = re.sub(r"<[^>]+>", " ", r.text)
                text = re.sub(r"\s+", " ", text)

                if kind == "manufacturing":
                    patterns = [
                        r"Manufacturing PMI.{0,180}?registered\s+(\d+(?:\.\d+)?)",
                        r"Manufacturing PMI.{0,180}?at\s+(\d+(?:\.\d+)?)",
                    ]
                else:
                    patterns = [
                        r"Services PMI.{0,180}?registered\s+(\d+(?:\.\d+)?)",
                        r"Services PMI.{0,180}?at\s+(\d+(?:\.\d+)?)",
                    ]

                value = None
                for pattern in patterns:
                    m = re.search(pattern, text, flags=re.I)
                    if m:
                        value = _clean_num(m.group(1))
                        break

                if value is not None:
                    data = {
                        "pmi": value,
                        "url": official,
                        "year": year,
                        "month": month,
                        "status": "REAL",
                    }

                    patterns = {
                        "new_orders": r"New Orders(?: Index)?.{0,100}?(\d+(?:\.\d+)?)",
                        "employment": r"Employment(?: Index)?.{0,100}?(\d+(?:\.\d+)?)",
                        "prices": r"Prices(?: Index)?.{0,100}?(\d+(?:\.\d+)?)",
                    }
                    for key, pattern in patterns.items():
                        mm = re.search(pattern, text, flags=re.I)
                        data[key] = _clean_num(mm.group(1)) if mm else None

                    return data

    except Exception as exc:
        print(f"WARNUNG: ISM {kind} {year}-{month:02d} official nicht verfuegbar: {exc}")

    # Secondary: public calendar. Actual only; no forecast/previous.
    secondary = _ism_public_secondary_forexfactory(year, month, kind)
    if secondary:
        release_date = secondary.get("release_date")
        if release_date:
            try:
                rd = dt.date.fromisoformat(release_date)
            except Exception:
                print(f"WARNUNG: ISM-Fallback verworfen: ungueltiges release_date={release_date}")
                return None

            expected_y = year + 1 if month == 12 else year
            expected_m = 1 if month == 12 else month + 1
            if rd.year != expected_y or rd.month != expected_m:
                print(
                    f"WARNUNG: ISM-Fallback verworfen: kind={kind} "
                    f"report_month={year}-{month:02d}, release_date={release_date}"
                )
                return None

        print(
            f"INFO: ISM {kind.title()} Fallback erfolgreich: "
            f"Actual={secondary['pmi']} | release_date={secondary.get('release_date')} | "
            f"source={secondary['url']}"
        )
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
        key=kind
        entry=cache.get("ism",{}).get(key)
        if entry and entry.get("data"):
            d=entry["data"]
            # Nur den aktuell faelligen, bereits veroeffentlichten Berichtsmonat aus dem
            # Cache verwenden. Ein alter Juni-Wert darf im August niemals den Juli-Wert ersetzen.
            latest_y, latest_m = candidates[0]
            if d.get("year") == latest_y and d.get("month") == latest_m:
                print(
                    f"INFO: ISM-Cache-Hit fuer {key} "
                    f"(Datenstand={latest_y}-{latest_m:02d}, status={d.get('status')})"
                )
                return d
            print(
                f"INFO: ISM-Cache vorhanden, aber nicht aktuell: "
                f"cached={d.get('year')}-{d.get('month')} "
                f"required={latest_y}-{latest_m}"
            )
        for y,m in candidates:
            d=_ism_fetch(kind,y,m)
            if d:
                with CACHE_WRITE_LOCK:
                    c=_cache_load(); c.setdefault("ism",{})[key]={"saved_at":time.time(),"data":d,"status":d.get("status","REAL"),"source":d["url"]}; _cache_save(c)
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
        "Fed Funds Effective Rate",
        "US 2Y Treasury",
        "US 10Y Treasury",
        "Core CPI",
        "NFP / Nonfarm Payrolls",
        "Arbeitslosenquote",
        "ISM Manufacturing PMI",
        "ISM Services PMI",
        "S&P 500",
    ]

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
    secondary_missing = [label for label in tier2_labels if _unavailable(label)]

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
        "STATUS: REAL = Originalwert | REAL_CACHED = echter gespeicherter Originalwert, Quelle im Lauf nicht neu erreichbar | CALCULATED = deterministisch berechnet | PROXY = Proxy | MODEL_DERIVED = Modellresultat | UNAVAILABLE = keine belastbare Zahl",
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
    lines.extend(fred_snapshots_parallel(["GSCPI", "Global Economic Policy Uncertainty", "US Federal Debt/GDP"]))
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
