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
from datetime import datetime
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

# Offizielle, kostenfreie Alternativquellen fuer kritische US-Daten.
# Sie werden nur verwendet, wenn FRED im aktuellen Lauf nicht erreichbar ist.
# BLS Public Data API v1 ist ohne Registrierung nutzbar. Treasury H.15 ist
# eine offizielle Federal-Reserve-Veröffentlichung.
BLS_API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_SERIES = {
    "CPI": "CUUR0000SA0",
    "Core CPI": "CUUR0000SA0L1E",
    "Durchschnittlicher Stundenlohn": "CES0500000003",
    "Arbeitslosenquote": "LNS14000000",
    "NFP / Nonfarm Payrolls": "CES0000000001",
    "PPI": "WPSFD4",
}
BLS_URL = "https://www.bls.gov/data/"
H15_URL = "https://www.federalreserve.gov/releases/h15/"
FED_FOMC_RELEASE_INDEX = "https://www.federalreserve.gov/newsevents/pressreleases/2026-press-fomc.htm"
MACRO_CACHE_DIR = Path(os.environ.get("NMM_MACRO_CACHE_DIR", ".macro_cache"))
MACRO_CACHE_FILE = MACRO_CACHE_DIR / "macro_cache.json"
FRED_TIMEOUT = float(os.environ.get("NMM_FRED_TIMEOUT_SECONDS", "8"))
MARKET_TIMEOUT = float(os.environ.get("NMM_MARKET_TIMEOUT_SECONDS", "12"))
CACHE_VERSION = 1
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


def _bls_cache_key(series_id):
    return f"BLS:{series_id}"


def _bls_fetch(series_ids):
    """Liest mehrere BLS-Serien in einem echten API-Aufruf. Keine Schätzung."""
    if not series_ids:
        return {}
    today = dt.date.today()
    start_year = str(today.year - 3)
    end_year = str(today.year)
    payload = {"seriesid": series_ids, "startyear": start_year, "endyear": end_year}
    try:
        r = requests.post(BLS_API_URL, json=payload, timeout=8, headers=REQUEST_HEADERS)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            return {}
        out = {}
        for series in data.get("Results", {}).get("series", []):
            rows = []
            for item in series.get("data", []):
                period = item.get("period", "")
                if not period.startswith("M") or period in ("M13",):
                    continue
                try:
                    value = float(item["value"].replace(",", ""))
                except Exception:
                    continue
                rows.append({
                    "date": f"{item['year']}-{int(period[1:]):02d}-01",
                    "value": value,
                })
            if rows:
                rows.sort(key=lambda x: x["date"])
                out[series.get("seriesID")] = rows
        return out
    except Exception as exc:
        print(f"WARNUNG: BLS nicht verfuegbar: {exc}")
        return {}


def _official_bls_snapshot(name):
    series_id = BLS_SERIES.get(name)
    if not series_id:
        return None
    cache = _cache_load()
    entry = cache.get("bls", {}).get(series_id)
    data = _bls_fetch([series_id])
    rows = data.get(series_id, [])
    status = "REAL"
    if not rows and entry and entry.get("rows"):
        cached_date = entry.get("data_date")
        if cached_date and _cache_valid(cached_date, 60):
            rows = entry["rows"]
            status = "REAL_CACHED"
    if not rows:
        return None
    latest = rows[-1]
    # BLS-Monatsdaten sind echte Originalwerte. Keine Fortschreibung.
    with CACHE_WRITE_LOCK:
        cache = _cache_load()
        cache.setdefault("bls", {})[series_id] = {
            "saved_at": time.time(),
            "data_date": latest["date"],
            "rows": rows,
            "status": "REAL",
            "source": f"{BLS_API_URL}{series_id}",
        }
        _cache_save(cache)
    return (
        f"{name}: {_fmt(latest['value'], 4)} | Datenstand={latest['date']} | STATUS={status} | "
        f"SOURCE=BLS {series_id} | {BLS_API_URL}{series_id}"
    )


def _parse_h15_row(table, label):
    for _, row in table.iterrows():
        values = [str(x).strip() for x in row.tolist()]
        if values and values[0].lower() == label.lower():
            nums = []
            for x in values[1:]:
                x = x.replace(",", "")
                if re.fullmatch(r"\d+(?:\.\d+)?", x):
                    nums.append(float(x))
            if nums:
                return nums[-1]
    return None


def _official_h15_snapshot(name):
    label_map = {
        "US 2Y Treasury": ("2-year", False),
        "US 5Y Treasury": ("5-year", False),
        "US 10Y Treasury": ("10-year", False),
        "US 30Y Treasury": ("30-year", False),
        "Realzins 10Y TIPS": ("10-year", True),
    }
    spec = label_map.get(name)
    if not spec:
        return None
    label, real = spec
    try:
        r = requests.get(H15_URL, timeout=8, headers=REQUEST_HEADERS)
        r.raise_for_status()
        tables = pd.read_html(StringIO(r.text))
        value = None
        for table in tables:
            text = table.astype(str).to_string()
            if real and "Inflation indexed" not in text:
                continue
            if not real and "Inflation indexed" in text:
                continue
            value = _parse_h15_row(table, label)
            if value is not None:
                break
        if value is None:
            return None
        m = re.search(r"Release date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", r.text)
        data_date = dt.datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat() if m else None
        if not data_date:
            return None
        return (
            f"{name}: {_fmt(value, 4)} | Datenstand={data_date} | STATUS=REAL | "
            f"SOURCE=Federal Reserve H.15 | {H15_URL}"
        )
    except Exception as exc:
        print(f"WARNUNG: Fed H.15 fuer {name} nicht verfuegbar: {exc}")
        return None


def _parse_rate_token(token):
    token = token.strip().replace("‑", "-").replace("–", "-")
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    m = re.fullmatch(r"(\d+)-(\d+)/(\d+)", token)
    if m:
        return float(m.group(1)) + float(m.group(2)) / float(m.group(3))
    return None


def _official_fed_target_snapshot(name):
    if name not in ("Fed Target Range Upper", "Fed Target Range Lower"):
        return None
    try:
        idx = requests.get(FED_FOMC_RELEASE_INDEX, timeout=8, headers=REQUEST_HEADERS)
        idx.raise_for_status()
        matches = re.findall(r'href=["\']([^"\']*monetary2026\d{4}a\.htm)["\'][^>]*>\s*Federal Reserve issues FOMC statement', idx.text, flags=re.I)
        if not matches:
            return None
        href = matches[0]
        url = href if href.startswith("http") else "https://www.federalreserve.gov" + href
        r = requests.get(url, timeout=8, headers=REQUEST_HEADERS)
        r.raise_for_status()
        text = re.sub(r"\s+", " ", r.text)
        m = re.search(r"target range for the federal funds rate(?: at| of)?\s+(\d+(?:-\d+/\d+|\.\d+)?)\s+to\s+(\d+(?:-\d+/\d+|\.\d+)?)\s+percent", text, flags=re.I)
        if not m:
            return None
        lower = _parse_rate_token(m.group(1)); upper = _parse_rate_token(m.group(2))
        if lower is None or upper is None:
            return None
        value = upper if name.endswith("Upper") else lower
        dm = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+2026", text)
        data_date = dt.datetime.strptime(dm.group(0), "%B %d, %Y").date().isoformat() if dm else None
        if not data_date:
            return None
        return f"{name}: {value:.4f} | Datenstand={data_date} | STATUS=REAL | SOURCE=Federal Reserve FOMC statement | {url}"
    except Exception as exc:
        print(f"WARNUNG: Fed Zielkorridor offizielle Quelle nicht verfuegbar: {exc}")
        return None


def official_fallback_snapshot(name):
    # Fed-Zielkorridor zuerst aus dem aktuellen offiziellen FOMC-Statement.
    line = _official_fed_target_snapshot(name)
    if line:
        return line
    # BLS zuerst fuer Arbeitsmarkt/Inflation/PPI.
    line = _official_bls_snapshot(name)
    if line:
        return line
    # Federal Reserve H.15 fuer Treasury-Renditen und TIPS.
    return _official_h15_snapshot(name)


def fred_series(series_id, limit_days=5000):
    cache = _cache_load()
    entry = cache.get("fred", {}).get(series_id)
    if entry and entry.get("payload"):
        try:
            df = pd.read_json(StringIO(entry["payload"]), orient="split")
            df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
            df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
            df = df.dropna(subset=["DATE", series_id]).sort_values("DATE")
            if not df.empty:
                latest = df["DATE"].iloc[-1].date().isoformat()
                max_age = FRED_MAX_AGE_DAYS.get(series_id, 60)
                if _cache_valid(latest, max_age):
                    return df
        except Exception as exc:
            print(f"WARNUNG-MAKRO-CACHE: FRED {series_id} unlesbar ({exc}) - lade neu.")

    try:
        r = requests.get(FRED_BASE.format(series_id), timeout=FRED_TIMEOUT, headers=REQUEST_HEADERS)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        if "DATE" not in df.columns or series_id not in df.columns:
            return pd.DataFrame()
        df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
        df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
        df = df.dropna(subset=["DATE", series_id]).sort_values("DATE")
        if limit_days:
            cutoff = pd.Timestamp.today() - pd.Timedelta(days=limit_days)
            df = df[df["DATE"] >= cutoff]
        if not df.empty:
            with CACHE_WRITE_LOCK:
                cache = _cache_load()
                cache.setdefault("fred", {})[series_id] = {
                    "saved_at": time.time(),
                    "data_date": df["DATE"].iloc[-1].date().isoformat(),
                    "payload": df.to_json(orient="split", date_format="iso"),
                    "status": "REAL",
                    "source": FRED_URL.format(series_id),
                }
                _cache_save(cache)
        return df
    except Exception as exc:
        print(f"WARNUNG: FRED {series_id} nicht verfuegbar: {exc}")
        # Ein echter, noch gueltiger Cache-Wert darf als REAL_CACHED weiter
        # verwendet werden. Es wird niemals ein Wert aus dem Nichts erzeugt.
        if entry and entry.get("payload"):
            try:
                df = pd.read_json(StringIO(entry["payload"]), orient="split")
                df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
                df[series_id] = pd.to_numeric(df[series_id], errors="coerce")
                df = df.dropna(subset=["DATE", series_id]).sort_values("DATE")
                latest = df["DATE"].iloc[-1].date().isoformat() if not df.empty else None
                if _cache_valid(latest, FRED_MAX_AGE_DAYS.get(series_id, 60)):
                    return df
            except Exception:
                pass
        return pd.DataFrame()


def fred_snapshot(name, series_id):
    df = fred_series(series_id)
    if df.empty:
        fallback = official_fallback_snapshot(name)
        if fallback:
            return fallback
        return f"{name}: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=FRED {series_id} | {FRED_URL.format(series_id)}"
    value = _clean_num(df[series_id].iloc[-1])
    date = df["DATE"].iloc[-1].strftime("%Y-%m-%d")
    cache = _cache_load()
    entry = cache.get("fred", {}).get(series_id, {})
    status = "REAL_CACHED" if entry and entry.get("data_date") == date and entry.get("saved_at", 0) < time.time() - 1 else "REAL"
    return (
        f"{name}: {_fmt(value, 4)} | Datenstand={date} | STATUS={status} | "
        f"SOURCE=FRED {series_id} | {FRED_URL.format(series_id)}"
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
        f"Aktueller Fed-Zielkorridor aus FRED: Untergrenze={_fmt(lower,4)}% | Obergrenze={_fmt(upper,4)}% | STATUS={'REAL' if lower is not None and upper is not None else 'UNAVAILABLE'}",
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


def _ism_fetch(kind, year, month):
    month_name = calendar.month_name[month].lower()
    path = "pmi" if kind == "manufacturing" else "services"
    url = f"{ISM_BASE}/{path}/{month_name}/"
    try:
        r = requests.get(url, timeout=20, headers=REQUEST_HEADERS)
        if r.status_code != 200:
            return None
        text = re.sub(r"\s+", " ", r.text)
        title_pattern = r"(?:Manufacturing|Services) PMI.{0,80}?at\s+(\d+(?:\.\d+)?)%"
        m = re.search(title_pattern, text, flags=re.I)
        if not m:
            return None
        value = _clean_num(m.group(1))
        if value is None:
            return None
        data = {"pmi": value, "url": url, "year": year, "month": month}
        # Die naechsten Kernkomponenten werden nur erfasst, wenn sie im Original
        # eindeutig vorhanden sind. Kein Wert wird aus Kontext geschaetzt.
        patterns = {
            "new_orders": r"New Orders Index(?:[^\d]{0,80})(\d+(?:\.\d+)?)",
            "employment": r"Employment Index(?:[^\d]{0,80})(\d+(?:\.\d+)?)",
            "prices": r"Prices Index(?:[^\d]{0,80})(\d+(?:\.\d+)?)",
        }
        for key, pattern in patterns.items():
            mm = re.search(pattern, text, flags=re.I)
            data[key] = _clean_num(mm.group(1)) if mm else None
        return data
    except Exception as exc:
        print(f"WARNUNG: ISM {kind} {year}-{month:02d} nicht verfuegbar: {exc}")
        return None


def ism_snapshot(today):
    # Vormonat ist der erwartete letzte vollstaendig veroeffentlichte Monat.
    first = today.replace(day=1)
    candidates = []
    for offset in range(1, 4):
        y, m = first.year, first.month - offset
        while m <= 0:
            y -= 1
            m += 12
        candidates.append((y, m))

    manufacturing = services = None
    for y, m in candidates:
        manufacturing = _ism_fetch("manufacturing", y, m)
        if manufacturing:
            break
    for y, m in candidates:
        services = _ism_fetch("services", y, m)
        if services:
            break

    lines = []
    if manufacturing:
        lines.append(
            f"ISM Manufacturing PMI: {manufacturing['pmi']:.1f} | Datenmonat={manufacturing['year']}-{manufacturing['month']:02d} | "
            f"New Orders={_fmt(manufacturing['new_orders'],1)} | Employment={_fmt(manufacturing['employment'],1)} | Prices={_fmt(manufacturing['prices'],1)} | "
            f"STATUS=REAL | SOURCE={manufacturing['url']}"
        )
    else:
        lines.append("ISM Manufacturing PMI: NICHT VERFUEGBAR | STATUS=UNAVAILABLE | SOURCE=ISM")
    if services:
        lines.append(
            f"ISM Services PMI: {services['pmi']:.1f} | Datenmonat={services['year']}-{services['month']:02d} | "
            f"New Orders={_fmt(services['new_orders'],1)} | Employment={_fmt(services['employment'],1)} | Prices={_fmt(services['prices'],1)} | "
            f"STATUS=REAL | SOURCE={services['url']}"
        )
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
    critical_labels = [
        "Fed Target Range Upper",
        "Fed Target Range Lower",
        "US 2Y Treasury",
        "US 10Y Treasury",
        "CPI",
        "Arbeitslosenquote",
        "Reales BIP",
        "US High Yield OAS",
        "VIX",
        "S&P 500",
        "DXY",
        "Gold",
        "WTI",
        "Kupfer",
        "Bitcoin",
        "Ethereum",
        "ISM Manufacturing PMI",
        "ISM Services PMI",
    ]
    missing = [label for label in critical_labels if any(line.startswith(label + ": NICHT VERFUEGBAR") for line in lines)]
    # Fed-Markterwartung ist kritisch; sie darf nicht ausfallen oder geschaetzt werden.
    if any(line.startswith("FED-MARKTERWARTUNG: NICHT BERECHENBAR") for line in lines):
        missing.append("FED-MARKTERWARTUNG")
    gate = "FREIGEGEBEN" if not missing else "GESPERRT"
    return gate, missing


def cache_stats():
    cache = _cache_load()
    return {
        "fred": len(cache.get("fred", {})),
        "bls": len(cache.get("bls", {})),
        "market": len(cache.get("market", {})),
        "fed_futures": len(cache.get("fed_futures", {})),
        "ism": len(cache.get("ism", {})),
        "file": str(MACRO_CACHE_FILE),
    }


def main():
    today = dt.date.today()
    output = f"Makro_Briefing({today.isoformat()}).txt"
    lines = []
    lines += [
        "NEUBER MACRO & MARKETS",
        f"MAKRO-DATENPAKET | Datenabruf={today.isoformat()}",
        "HARTE DATENREGEL: Keine Zahl wird geschaetzt. Fehlende Werte bleiben NICHT VERFUEGBAR.",
        "STATUS: REAL = Originalwert | REAL_CACHED = echter gespeicherter Originalwert, Quelle im Lauf nicht neu erreichbar | CALCULATED = deterministisch berechnet | PROXY = Proxy | MODEL_DERIVED = Modellresultat | UNAVAILABLE = keine belastbare Zahl",
        "",
        "CACHE-STATUS VOR ABRUF: " + json.dumps(cache_stats(), ensure_ascii=False),
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
        "Reales BIP", "Arbeitslosenquote", "NFP / Nonfarm Payrolls", "JOLTS Job Openings",
        "Initial Jobless Claims", "Industrieproduktion", "Kapazitaetsauslastung", "Consumer Sentiment",
    ]))
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

    gate, missing = data_quality_gate(lines)
    lines.append("6. DATENQUALITAETS-GATEKEEPER")
    lines.append(f"MAKRO-SZENARIO-GATE: {gate}")
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
    if missing:
        print("KRITISCHE_DATENLUECKEN=" + ", ".join(missing))
    print("CACHE-STATUS NACH ABRUF=" + json.dumps(cache_stats(), ensure_ascii=False))


if __name__ == "__main__":
    main()
