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
    """Gemeinsame yfinance-Historie pro Ticker; immer period='max'.

    FALLBACK GEGEN VERALTETE 'max'-ANTWORTEN (NEU, Anlass: Auswertung vom
    18.08.2026, in der period='max' fuer S&P 500, Nasdaq, Dow Jones, DAX,
    EuroStoxx50, STOXX 600, Russell 2000 UND VIX gleichzeitig einen 4 Tage
    alten Datenstand lieferte - also nicht nur ein Einzelfall bei einem
    exotischen Ticker, sondern ein breiteres, bekanntes yfinance-Verhalten:
    der 'max'-Endpunkt haengt manchmal an einem verzoegerten Yahoo-Edge-
    Cache. Ein zusaetzlicher, kurzer period='5d'-Request trifft haeufig
    einen anderen, frischeren Yahoo-Endpunkt. Liefert dieser Kurzabruf
    neuere Zeilen als die 'max'-Historie, werden NUR diese neueren Zeilen
    ergaenzt/ueberschrieben - die lange Historie bleibt sonst unangetastet.
    Schlaegt der Kurzabruf fehl oder liefert er nichts Neueres, wird
    unveraendert die bisherige 'max'-Historie zurueckgegeben (kein
    Verhaltensunterschied zu vorher)."""
    import yfinance as yf

    df_max = get_or_fetch_dataframe(
        f"yf:{ticker}",
        lambda: yf.Ticker(ticker).history(period="max"),
    )

    try:
        df_recent = get_or_fetch_dataframe(
            f"yf:{ticker}:recent5d",
            lambda: yf.Ticker(ticker).history(period="5d"),
        )
    except Exception as exc:
        print(f"WARNUNG-MARKET-CACHE: Kurzabruf (5d) fuer {ticker} fehlgeschlagen "
              f"({type(exc).__name__}: {exc}) - bleibe bei 'max'-Historie.")
        return df_max

    if df_recent is None or df_recent.empty:
        print(f"DEBUG-YF-FALLBACK: {ticker} -> period='5d'-Kurzabruf lief, "
              f"lieferte aber leere Daten - bleibe bei 'max'-Historie.")
        return df_max
    if df_max is None or df_max.empty:
        return df_recent

    max_letztes_datum = df_max.index.max()
    recent_letztes_datum = df_recent.index.max()
    if recent_letztes_datum <= max_letztes_datum:
        print(f"DEBUG-YF-FALLBACK: {ticker} -> period='5d' liefert nichts Neueres "
              f"als 'max' (beide enden bei {max_letztes_datum.date()}) - "
              f"bleibe bei 'max'-Historie.")
        return df_max

    print(f"DEBUG-YF-FALLBACK: {ticker} -> period='max' endete bei "
          f"{max_letztes_datum.date()}, period='5d' liefert bis "
          f"{recent_letztes_datum.date()} - fuehre zusammen.")

    kombiniert = pd.concat([df_max, df_recent])
    # Bei ueberlappenden Datumswerten die Zeile aus dem frischeren 5d-Abruf
    # behalten (keep='last', da df_recent nach df_max angehaengt wurde).
    kombiniert = kombiniert[~kombiniert.index.duplicated(keep='last')]
    kombiniert = kombiniert.sort_index()
    return kombiniert


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
