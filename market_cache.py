"""
Neuber Macro & Markets - prozessuebergreifender Marktdaten-Cache.

Der Cache ist bewusst klein und dateibasiert. Er dient dazu, gemeinsame
Benchmark-/Marktdaten innerhalb eines GitHub-Actions-Laufs zwischen mehreren
separaten Python-Prozessen wiederzuverwenden.

- yfinance-Historien werden pro Ticker nur einmal geladen (period="max") und
  fuer kuerzere Fenster lokal geschnitten.
- Alpaca-SPY wird separat als Close-Serie gecacht.
- Standard-TTL: 20 Minuten. Damit bleiben mehrere Scanner-Schritte innerhalb
  eines Laufs konsistent, ohne stundenalte Marktdaten blind wiederzuverwenden.
- Schreibvorgaenge erfolgen atomar; ein kleiner Lock verhindert parallele
  Schreibkollisionen.

Der Cache ist kein dauerhafter Markt-Datenspeicher und wird nicht von
upload_to_drive.py hochgeladen.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from io import StringIO
from pathlib import Path
from typing import Callable

import pandas as pd

CACHE_VERSION = 1
CACHE_TTL_MINUTES = float(os.environ.get("NMM_MARKET_CACHE_TTL_MINUTES", "20"))
CACHE_FILE = Path(os.environ.get("NMM_MARKET_CACHE_FILE", "market_cache.json"))
LOCK_FILE = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".lock")
LOCK_TIMEOUT_SECONDS = 15.0
LOCK_RETRY_SECONDS = 0.1


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"version": CACHE_VERSION, "entries": {}}
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != CACHE_VERSION:
            return {"version": CACHE_VERSION, "entries": {}}
        if not isinstance(data.get("entries"), dict):
            return {"version": CACHE_VERSION, "entries": {}}
        return data
    except Exception as exc:
        print(f"WARNUNG-MARKET-CACHE: Cache-Datei nicht lesbar ({type(exc).__name__}: {exc}) - starte leer.")
        return {"version": CACHE_VERSION, "entries": {}}


def _save_cache(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{CACHE_FILE.name}.",
        suffix=".tmp",
        dir=str(CACHE_FILE.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, CACHE_FILE)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _acquire_lock() -> bool:
    deadline = time.time() + LOCK_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            time.sleep(LOCK_RETRY_SECONDS)
        except Exception:
            return False
    return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _fresh(entry: dict) -> bool:
    try:
        age = time.time() - float(entry["saved_at"])
        return age <= CACHE_TTL_MINUTES * 60
    except Exception:
        return False


def _get_entry(key: str) -> dict | None:
    data = _load_cache()
    entry = data.get("entries", {}).get(key)
    if entry and _fresh(entry):
        return entry
    return None


def get_or_fetch_dataframe(
    key: str,
    fetcher: Callable[[], pd.DataFrame],
) -> pd.DataFrame:
    """Liefert ein gecachtes DataFrame oder ruft `fetcher` genau einmal auf.

    Das DataFrame wird als JSON im Cache gespeichert. Eine Kopie wird an den
    Aufrufer zurueckgegeben, damit lokale Bereinigungen den Cache nicht veraendern.
    """
    entry = _get_entry(key)
    if entry is not None:
        try:
            df = pd.read_json(StringIO(entry["payload"]), orient="split")
            df.index = pd.to_datetime(df.index)
            return df.copy()
        except Exception as exc:
            print(f"WARNUNG-MARKET-CACHE: Eintrag {key} unlesbar ({type(exc).__name__}: {exc}) - lade neu.")

    df = fetcher()
    if df is None:
        return pd.DataFrame()
    df = df.copy()

    # Bei Fehler/leerem Ergebnis nicht als erfolgreichen Marktstand cachen.
    if df.empty:
        return df

    payload = df.to_json(orient="split", date_format="iso")
    if _acquire_lock():
        try:
            data = _load_cache()
            data.setdefault("entries", {})[key] = {
                "saved_at": time.time(),
                "kind": "dataframe",
                "payload": payload,
            }
            _save_cache(data)
        except Exception as exc:
            print(f"WARNUNG-MARKET-CACHE: Schreiben von {key} fehlgeschlagen: {exc}")
        finally:
            _release_lock()
    return df.copy()


def get_or_fetch_series(
    key: str,
    fetcher: Callable[[], pd.Series | pd.DataFrame],
) -> pd.Series | None:
    """Liefert eine gecachte Close-Serie; DataFrame-Eingaben werden auf Close reduziert."""
    entry = _get_entry(key)
    if entry is not None:
        try:
            df = pd.read_json(StringIO(entry["payload"]), orient="split")
            df.index = pd.to_datetime(df.index)
            return df.iloc[:, 0].copy()
        except Exception as exc:
            print(f"WARNUNG-MARKET-CACHE: Serien-Eintrag {key} unlesbar ({type(exc).__name__}: {exc}) - lade neu.")

    obj = fetcher()
    if obj is None:
        return None
    if isinstance(obj, pd.Series):
        series = obj.copy()
    else:
        if obj.empty:
            return None
        if "Close" in obj.columns:
            series = obj["Close"].copy()
        elif obj.shape[1] == 1:
            series = obj.iloc[:, 0].copy()
        else:
            return None

    series = series.dropna()
    if series.empty:
        return None

    payload = series.to_frame("Close").to_json(orient="split", date_format="iso")
    if _acquire_lock():
        try:
            data = _load_cache()
            data.setdefault("entries", {})[key] = {
                "saved_at": time.time(),
                "kind": "series",
                "payload": payload,
            }
            _save_cache(data)
        except Exception as exc:
            print(f"WARNUNG-MARKET-CACHE: Schreiben von {key} fehlgeschlagen: {exc}")
        finally:
            _release_lock()
    return series.copy()


def get_yf_history(ticker: str) -> pd.DataFrame:
    """Gemeinsame yfinance-Historie pro Ticker; immer period='max'."""
    import yfinance as yf

    return get_or_fetch_dataframe(
        f"yf:{ticker}",
        lambda: yf.Ticker(ticker).history(period="max"),
    )


def refresh_yf_history(ticker: str) -> pd.DataFrame:
    """Erzwingt einen frischen Yahoo-Abruf und ersetzt den Cache-Eintrag."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="max")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    payload = df.to_json(orient="split", date_format="iso")
    if _acquire_lock():
        try:
            data = _load_cache()
            data.setdefault("entries", {})[f"yf:{ticker}"] = {
                "saved_at": time.time(), "kind": "dataframe", "payload": payload,
            }
            _save_cache(data)
        except Exception as exc:
            print(f"WARNUNG-MARKET-CACHE: Refresh von {ticker} konnte nicht gespeichert werden: {exc}")
        finally:
            _release_lock()
    return df.copy()



def fetch_yf_history_uncached(ticker: str) -> pd.DataFrame:
    """Laedt yfinance direkt, ohne den gemeinsamen Cache zu veraendern."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="max")
    if df is None or df.empty:
        return pd.DataFrame()
    return df.copy()

def store_yf_history(ticker: str, df: pd.DataFrame) -> None:
    """Schreibt eine bereits validierte yfinance-Historie in den gemeinsamen Cache."""
    if df is None or df.empty:
        return
    payload = df.to_json(orient="split", date_format="iso")
    if _acquire_lock():
        try:
            data = _load_cache()
            data.setdefault("entries", {})[f"yf:{ticker}"] = {
                "saved_at": time.time(),
                "kind": "dataframe",
                "payload": payload,
            }
            _save_cache(data)
        except Exception as exc:
            print(f"WARNUNG-MARKET-CACHE: Speichern von {ticker} fehlgeschlagen: {exc}")
        finally:
            _release_lock()


def invalidate_yf_history(ticker: str) -> None:
    """Entfernt einen als veraltet erkannten yfinance-Cacheeintrag."""
    if not _acquire_lock():
        return
    try:
        data = _load_cache()
        data.setdefault("entries", {}).pop(f"yf:{ticker}", None)
        _save_cache(data)
    except Exception as exc:
        print(f"WARNUNG-MARKET-CACHE: Ungueltiger Cacheeintrag {ticker} konnte nicht entfernt werden: {exc}")
    finally:
        _release_lock()


def refresh_yf_history_direct(ticker: str) -> pd.DataFrame:
    """
    Erzwungener Direktabruf ueber die Yahoo-Chart-Route.

    Wichtig: Der Abruf wird hier bewusst NOCH NICHT gecacht. Erst der Aufrufer
    kann nach der Handelsschluss-/Staleness-Pruefung entscheiden, ob die
    gelieferte Historie valide genug ist. So kann ein erneut veralteter
    Yahoo-Stand keinen frischen Cache-Zeitstempel bekommen.
    """
    import requests

    letzte_fehlermeldung = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{ticker}"
        params = {
            "period1": 0,
            "period2": int(time.time()) + 86400,
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
        try:
            antwort = requests.get(
                url,
                params=params,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            antwort.raise_for_status()
            payload_json = antwort.json()
            result = (payload_json.get("chart") or {}).get("result")
            if not result:
                raise RuntimeError("Yahoo Chart lieferte kein result")

            result = result[0]
            timestamps = result.get("timestamp") or []
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            if not timestamps or not quote:
                raise RuntimeError("Yahoo Chart lieferte keine Tagesdaten")

            index = pd.to_datetime(timestamps, unit="s", utc=True)
            data = pd.DataFrame(
                {
                    "Open": quote.get("open", []),
                    "High": quote.get("high", []),
                    "Low": quote.get("low", []),
                    "Close": quote.get("close", []),
                    "Volume": quote.get("volume", []),
                },
                index=index,
            )
            data = data.dropna(subset=["Close"])
            if data.empty:
                raise RuntimeError("Yahoo Chart lieferte nur leere Schlusskurse")
            return data.copy()
        except Exception as exc:
            letzte_fehlermeldung = exc
            continue

    if letzte_fehlermeldung is not None:
        raise letzte_fehlermeldung
    return pd.DataFrame()

def clear_stale_cache() -> None:
    """Entfernt abgelaufene Eintraege; Fehler werden bewusst ignoriert."""
    if not CACHE_FILE.exists():
        return
    try:
        data = _load_cache()
        entries = data.get("entries", {})
        data["entries"] = {k: v for k, v in entries.items() if _fresh(v)}
        _save_cache(data)
    except Exception:
        pass
