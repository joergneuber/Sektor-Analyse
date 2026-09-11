#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEUBER MACRO & MARKETS
Strukturelle Markttrends – eigenständiger V1-Datenbaustein

Zweck:
    Ausschließlich Rohdaten beschaffen, validieren und in EINEM Cache speichern.
    Keine makroökonomische oder marktbezogene Interpretation.

Dateien:
    struktur_trends.py
    struktur_trends_cache.json

WICHTIG:
    - Alle Quellen müssen kostenlos/public sein.
    - Keine Schätzungen durch dieses Programm.
    - Quelleneigene Schätz-/Estimate-Flags werden erhalten.
    - Bei Abruffehlern wird ein vorhandener gültiger Cache nicht zerstört.
    - Jede Datenquelle hat ihre eigene Aktualitätsprüfung.
    - Die IEA-SDMX-Mappings werden bewusst nicht erfunden. Für MESGEN/MESBAL
      werden die offiziellen Mapping-Informationen benötigt.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sys
import time
import random
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_FILE = SCRIPT_DIR / "struktur_trends_cache.json"

CACHE_VERSION = "1.1"

# ---------------------------------------------------------------------------
# Datenquellen / feste Konfiguration
# ---------------------------------------------------------------------------

OECD_STAN_BASE = (
    "https://sdmx.oecd.org/public/rest/data/"
    "OECD.STI.PIE,DSD_STAN@DF_STAN_2025,1.0/"
)

OECD_PRODUCTIVITY_BASE = (
    "https://sdmx.oecd.org/public/rest/data/"
    "OECD.SDD.TPS,DSD_PDB@DF_PDB,2.0/"
)

OECD_STAN_REFERENCE_AREAS = [
    "USA", "DEU", "FRA", "ITA", "GBR",
    "ESP", "NLD", "SWE", "JPN", "KOR",
]

OECD_STAN_ACTIVITIES = {
    "_T": "Total economy",
    "C": "Manufacturing",
    "C20_21": "Chemicals and pharmaceuticals",
    "C26": "Computer, electronic and optical products",
    "C27": "Electrical equipment",
    "C28": "Machinery",
    "C29": "Motor vehicles",
    "J": "Information and communication",
}

OECD_STAN_MEASURES = {
    "B1G": "Value added",
    "P51G": "Gross fixed capital formation",
    # Working-hours query is handled separately because the STAN
    # dimension structure for labour input must not be guessed.
    "WORKING_HOURS": "Working hours / labour input",
}

OECD_PRODUCTIVITY_REFERENCE_AREAS = [
    "USA", "DEU", "FRA", "ITA", "GBR",
    "ESP", "NLD", "SWE", "JPN", "KOR",
]

# Official OECD productivity measure identified during source review.
OECD_PRODUCTIVITY_MEASURE = "GVAHRS"

# Stanford AI Index is an annual public-data release, not a stable daily API.
# The exact public-data download URL can change between annual editions.
STANFORD_PUBLIC_DATA_URL = "https://hai.stanford.edu/ai-index"

# IEA: new SDMX datasets. Exact dimension keys are intentionally supplied
# through the official mapping files rather than guessed here.
IEA_DATASETS = {
    "electricity_generation": "MESGEN",
    "electricity_balance": "MESBAL",
}

SIPRI_DATABASE_URL = "https://www.sipri.org/databases/milex"

# ---------------------------------------------------------------------------
# Aktualitätsregeln
# ---------------------------------------------------------------------------

# Diese Werte sind bewusst technisch-konservativ und beziehen sich auf die
# erwartete Veröffentlichungsfrequenz. Sie bedeuten NICHT, dass ein Datensatz
# nach Ablauf automatisch gelöscht wird. Bei Überschreitung bleibt er im Cache
# und wird nur als zu alt/unavailable behandelt, sofern kein neuer Abruf gelingt.
MAX_AGE_DAYS = {
    "OECD_STAN": 450,
    "OECD_PRODUCTIVITY": 450,
    "AI_INDEX": 500,
    "IEA_ELECTRICITY": 75,
    "SIPRI_DEFENCE": 500,
}

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

LOG = logging.getLogger("struktur_trends")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def age_days(timestamp: str | None) -> float | None:
    dt = parse_iso(timestamp)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


def http_get(url: str, timeout: int = 60, retries: int = 5) -> bytes:
    """Robuster öffentlicher HTTP-Abruf mit besonderer Behandlung von 429.

    OECD SDMX kann bei zu vielen Einzelabfragen temporär HTTP 429 liefern.
    Wir reagieren darauf mit Retry-After bzw. exponentiellem Backoff.
    Die eigentliche Entlastung erfolgt zusätzlich dadurch, dass OECD-Abfragen
    unten gebündelt werden und nicht mehr pro Land/Branche einzeln erfolgen.
    """
    last_error: Exception | None = None
    headers = {
        "User-Agent": "Neuber-Macro-Structural-Trends/1.1",
        "Accept": "text/csv,application/vnd.sdmx.data+csv;version=2.0.0,"
                  "application/csv,application/json;q=0.9,*/*;q=0.5",
    }

    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt >= retries:
                raise RuntimeError(
                    f"HTTP-Abruf fehlgeschlagen: {url} | HTTP {exc.code}: {exc.reason}"
                ) from exc

            retry_after = exc.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 0.0
            except ValueError:
                delay = 0.0

            # Wenn der Server keine Wartezeit vorgibt: konservativer Backoff.
            # Kleiner Jitter verhindert identische Retry-Zeitpunkte.
            if delay <= 0:
                delay = min(60.0, 5.0 * (2 ** attempt))
            delay += random.uniform(0.25, 1.25)
            LOG.warning(
                "HTTP 429 von OECD/Quelle; Retry %d/%d in %.1fs: %s",
                attempt + 1, retries, delay, url
            )
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                delay = min(30.0, 2.0 * (2 ** attempt)) + random.uniform(0.1, 0.8)
                time.sleep(delay)

    raise RuntimeError(f"HTTP-Abruf fehlgeschlagen: {url} | {last_error}")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return new_cache()

    try:
        with path.open("r", encoding="utf-8") as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict):
            raise ValueError("Cache ist kein JSON-Objekt.")
        return obj
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Cache konnte nicht gelesen werden: {path} | {exc}") from exc


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
    tmp.replace(path)


def empty_field(source: str, dataset: str, frequency: str) -> dict[str, Any]:
    return {
        "source": source,
        "dataset": dataset,
        "version": None,
        "frequency": frequency,
        "status": "UNAVAILABLE",
        "unit": None,
        "data_period": None,
        "retrieved_at": None,
        "last_successful_update": None,
        "series_count": 0,
        "observation_count": 0,
        "data": {},
        "source_notes": [],
    }


def new_cache() -> dict[str, Any]:
    return {
        "cache_version": CACHE_VERSION,
        "cache_created_at": now_iso(),
        "cache_updated_at": now_iso(),

        "OECD_STAN": {
            "value_added": empty_field("OECD", "DSD_STAN@DF_STAN_2025", "annual"),
            "investment": empty_field("OECD", "DSD_STAN@DF_STAN_2025", "annual"),
            "labour_input": empty_field("OECD", "DSD_STAN@DF_STAN_2025", "annual"),
        },

        "OECD_PRODUCTIVITY": {
            "labour_productivity_level": empty_field(
                "OECD", "DSD_PDB@DF_PDB", "annual"
            ),
            "labour_productivity_growth": empty_field(
                "OECD", "DSD_PDB@DF_PDB", "annual"
            ),
        },

        "AI_INDEX": {
            "ai_investment": empty_field("Stanford AI Index", "AI Index Public Data", "annual"),
            "ai_adoption": empty_field("Stanford AI Index", "AI Index Public Data", "annual"),
            "ai_compute": empty_field("Stanford AI Index", "AI Index Public Data", "annual"),
        },

        "IEA_ELECTRICITY": {
            "electricity_generation": empty_field("IEA", "MESGEN", "monthly"),
            "electricity_balance": empty_field("IEA", "MESBAL", "monthly"),
        },

        "SIPRI_DEFENCE": {
            "military_expenditure_real": empty_field(
                "SIPRI", "Military Expenditure Database", "annual"
            ),
            "military_burden_gdp": empty_field(
                "SIPRI", "Military Expenditure Database", "annual"
            ),
            "military_share_government": empty_field(
                "SIPRI", "Military Expenditure Database", "annual"
            ),
        },
    }


def merge_metadata(field: dict[str, Any], **updates: Any) -> dict[str, Any]:
    result = deepcopy(field)
    result.update(updates)
    return result


def set_real_cached_if_valid(field: dict[str, Any], source_key: str) -> dict[str, Any]:
    """
    Bei Abruffehler alten Datenbestand erhalten.
    Status nur dann REAL_CACHED, wenn ein erfolgreicher Abruf vorhanden
    und die definierte Altersgrenze eingehalten wird.
    """
    if not field.get("data"):
        result = deepcopy(field)
        result["status"] = "UNAVAILABLE"
        return result

    age = age_days(field.get("last_successful_update"))
    max_age = MAX_AGE_DAYS[source_key]

    result = deepcopy(field)
    if age is not None and age <= max_age:
        result["status"] = "REAL_CACHED"
    else:
        result["status"] = "UNAVAILABLE"
        result.setdefault("source_notes", []).append(
            f"Cache älter als zulässige {max_age} Tage."
        )
    return result


def cache_field_summary(field: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": field.get("status"),
        "frequency": field.get("frequency"),
        "data_period": field.get("data_period"),
        "retrieved_at": field.get("retrieved_at"),
        "last_successful_update": field.get("last_successful_update"),
        "observations": sum(
            len(v) if isinstance(v, dict) else 1
            for v in field.get("data", {}).values()
        ),
    }


# ---------------------------------------------------------------------------
# OECD STAN
# ---------------------------------------------------------------------------

def oecd_stan_url(measure: str, start_period: int | None = None) -> str:
    """Bündelte STAN-Abfrage für alle benötigten Länder und Aktivitäten."""
    countries = "+".join(OECD_STAN_REFERENCE_AREAS)
    activities = "+".join(OECD_STAN_ACTIVITIES)
    key = f"A.{countries}.{activities}.{measure}.V.XDC"
    url = OECD_STAN_BASE + key + "?dimensionAtObservation=AllDimensions"
    if start_period:
        url += f"&startPeriod={start_period}"
    return url


def parse_csv_payload(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def first_present(row: dict[str, Any], *names: str) -> Any:
    normalized = {str(k).upper(): v for k, v in row.items()}
    for name in names:
        if name.upper() in normalized:
            return normalized[name.upper()]
    return None


def parse_sdmx_csv_rows(raw: bytes) -> list[dict[str, Any]]:
    """Toleranter Parser für SDMX-CSV mit erhaltenen Dimensionsmetadaten."""
    rows = parse_csv_payload(raw)
    output: list[dict[str, Any]] = []

    for row in rows:
        period = first_present(row, "TIME_PERIOD", "TIME")
        value = first_present(row, "OBS_VALUE", "VALUE")
        if period is None or value in (None, ""):
            continue

        try:
            numeric = float(str(value).replace(",", "."))
        except ValueError:
            continue

        output.append({
            "period": str(period),
            "value": numeric,
            "unit": first_present(row, "UNIT", "UNIT_MEASURE"),
            "reference_area": first_present(row, "REF_AREA", "REFERENCE_AREA"),
            "activity": first_present(row, "ACTIVITY", "ACTIVITY_CODE"),
            "measure": first_present(row, "MEASURE", "MEASURE_CODE"),
            "frequency": first_present(row, "FREQ", "FREQUENCY"),
            "transformation": first_present(row, "TRANSFORMATION"),
            "observation_status": first_present(
                row, "OBS_STATUS", "OBSERVATION_STATUS", "OBS_STATUS_CODE"
            ),
            "source_flag": first_present(
                row, "OBS_FLAG", "OBSERVATION_FLAG", "OBSERVATION_STATUS"
            ),
        })

    return output


def fetch_stan_measure(measure: str, start_period: int = 2000) -> list[dict[str, Any]]:
    url = oecd_stan_url(measure, start_period)
    raw = http_get(url)
    return parse_sdmx_csv_rows(raw)


def _group_stan_rows(rows: list[dict[str, Any]], measure: str) -> dict[str, Any]:
    """Gruppiert STAN-Rohdaten und entfernt nur exakte Serien-Duplikate.

    Eine Serie wird durch Land + Aktivität definiert. Innerhalb der Serie
    bleiben unterschiedliche Perioden/Einheiten/Transformationen erhalten.
    Identische Beobachtungen werden deterministisch dedupliziert.
    """
    observations: dict[str, Any] = {}

    for row in rows:
        country = str(row.get("reference_area") or "").strip()
        activity = str(row.get("activity") or "").strip()

        if country not in OECD_STAN_REFERENCE_AREAS:
            continue
        if activity not in OECD_STAN_ACTIVITIES:
            continue

        key = f"{country}|{activity}"
        block = observations.setdefault(
            key,
            {
                "reference_area": country,
                "activity": activity,
                "activity_name": OECD_STAN_ACTIVITIES[activity],
                "measure": measure,
                "observations": [],
            },
        )

        observation = {
            k: v for k, v in row.items()
            if k not in {"reference_area", "activity", "measure"}
        }

        # Deduplizierung ohne Informationsverlust:
        # gleiche Periode + gleiche technische Metadaten + gleicher Wert
        # werden nur einmal gespeichert.
        dedupe_key = json.dumps(
            observation,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        seen = block.setdefault("_seen", set())
        if dedupe_key not in seen:
            seen.add(dedupe_key)
            block["observations"].append(observation)

    # Interne Dedupe-Hilfsmenge nicht in den Cache schreiben.
    for block in observations.values():
        block.pop("_seen", None)
        block["observations"].sort(
            key=lambda x: (
                str(x.get("period") or ""),
                str(x.get("unit") or ""),
                str(x.get("transformation") or ""),
            )
        )

    return observations


def _count_observations(container: dict[str, Any]) -> int:
    return sum(
        len(block.get("observations", []))
        for block in container.values()
    )


def _audit_stan_container(container: dict[str, Any], measure: str) -> tuple[int, int]:
    """Prüft, dass nur konfigurierte STAN-Serien im Cache landen."""
    series_count = 0
    observation_count = 0

    for key, block in container.items():
        country = block.get("reference_area")
        activity = block.get("activity")

        if country not in OECD_STAN_REFERENCE_AREAS:
            raise RuntimeError(f"Unerwartetes STAN-Land im Cache: {country!r}")
        if activity not in OECD_STAN_ACTIVITIES:
            raise RuntimeError(f"Unerwartete STAN-Aktivität im Cache: {activity!r}")
        if block.get("measure") != measure:
            raise RuntimeError(
                f"Falsches STAN-Measure in Serie {key}: "
                f"{block.get('measure')!r} statt {measure!r}"
            )

        series_count += 1
        observation_count += len(block.get("observations", []))

    if series_count == 0 or observation_count == 0:
        raise RuntimeError(
            f"STAN-Audit fehlgeschlagen: measure={measure}, "
            f"series={series_count}, observations={observation_count}"
        )

    return series_count, observation_count


def update_oecd_stan(cache: dict[str, Any], start_period: int = 2000) -> None:
    mapping = {
        "value_added": "B1G",
        "investment": "P51G",
    }

    for field_name, measure in mapping.items():
        field = cache["OECD_STAN"][field_name]
        retrieved = now_iso()
        try:
            rows = fetch_stan_measure(measure, start_period=start_period)
            observations = _group_stan_rows(rows, measure)
            series_count, observation_count = _audit_stan_container(
                observations, measure
            )

            field.update({
                "version": "1.0",
                "status": "REAL",
                "retrieved_at": retrieved,
                "last_successful_update": retrieved,
                "data_period": _latest_period(observations),
                "data": observations,
                "series_count": series_count,
                "observation_count": observation_count,
                "source_notes": [
                    "STAN-Abfrage gebündelt über konfigurierte Länder und Aktivitäten.",
                    "Exakte Beobachtungsduplikate werden vor dem Cache entfernt.",
                ],
            })

        except Exception as exc:
            LOG.warning("OECD STAN %s: %s", field_name, exc)
            cache["OECD_STAN"][field_name] = set_real_cached_if_valid(
                field, "OECD_STAN"
            )

    labour = cache["OECD_STAN"]["labour_input"]
    note = (
        "Working-hours SDMX-Abfrage muss anhand der aktuellen OECD-STAN-"
        "Strukturabfrage parametrisiert werden; kein geratener Key."
    )
    if note not in labour.setdefault("source_notes", []):
        labour["source_notes"].append(note)
    labour["status"] = "UNAVAILABLE" if not labour.get("data") else labour["status"]

def _latest_period(container: dict[str, Any]) -> str | None:
    periods: list[str] = []
    for block in container.values():
        for row in block.get("observations", []):
            p = row.get("period")
            if p:
                periods.append(str(p))
    return max(periods) if periods else None


# ---------------------------------------------------------------------------
# OECD Productivity
# ---------------------------------------------------------------------------

def update_oecd_productivity(cache: dict[str, Any], start_period: int = 2000) -> None:
    """Produktivitäts-Level als eine gebündelte OECD-Abfrage."""
    countries = "+".join(OECD_PRODUCTIVITY_REFERENCE_AREAS)
    key = f"{countries}.A.{OECD_PRODUCTIVITY_MEASURE}._T.XDC_H..N.."
    url = (
        OECD_PRODUCTIVITY_BASE + key
        + "?dimensionAtObservation=AllDimensions"
        + f"&startPeriod={start_period}"
    )

    level = cache["OECD_PRODUCTIVITY"]["labour_productivity_level"]
    try:
        raw = http_get(url)
        rows = parse_sdmx_csv_rows(raw)
        grouped: dict[str, Any] = {}
        for row in rows:
            country = str(row.get("reference_area") or "").strip()
            if country not in OECD_PRODUCTIVITY_REFERENCE_AREAS:
                continue
            grouped.setdefault(
                country,
                {
                    "reference_area": country,
                    "measure": OECD_PRODUCTIVITY_MEASURE,
                    "observations": [],
                },
            )["observations"].append({
                k: v for k, v in row.items() if k != "reference_area"
            })

        if grouped:
            ts = now_iso()
            observation_count = _count_observations(grouped)
            level.update({
                "version": "2.0",
                "status": "REAL",
                "retrieved_at": ts,
                "last_successful_update": ts,
                "data_period": _latest_period(grouped),
                "series_count": len(grouped),
                "observation_count": observation_count,
                "data": grouped,
                "source_notes": [
                    "Gebündelte OECD-Produktivitätsabfrage über die konfigurierte Länderliste.",
                ],
            })
        else:
            raise RuntimeError("OECD Productivity lieferte keine verwertbaren Beobachtungen.")
    except Exception as exc:
        LOG.warning("OECD Productivity: %s", exc)
        cache["OECD_PRODUCTIVITY"]["labour_productivity_level"] = (
            set_real_cached_if_valid(level, "OECD_PRODUCTIVITY")
        )

    growth = cache["OECD_PRODUCTIVITY"]["labour_productivity_growth"]
    note = (
        "Growth wird als offizielle OECD-Reihe geführt; die konkrete "
        "Growth-Dimension wird vor Produktiveinsatz anhand der aktuellen "
        "PDB-SDMX-Struktur festgelegt. Keine Python-Eigenberechnung."
    )
    if note not in growth.setdefault("source_notes", []):
        growth["source_notes"].append(note)
    if not growth.get("data"):
        growth["status"] = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Stanford AI Index
# ---------------------------------------------------------------------------

def update_ai_index(cache: dict[str, Any]) -> None:
    """
    Stanford AI Index:
    jährlicher Public-Data-Bestand. Keine erfundene Download-URL und keine
    Scraping-/Interpretationslogik. Der Public-Data-Link kann pro Jahr ändern.
    """
    for field_name in (
        "ai_investment", "ai_adoption", "ai_compute"
    ):
        field = cache["AI_INDEX"][field_name]
        field.setdefault("source_notes", []).append(
            "Offizieller Stanford AI Index Public-Data-Bestand. "
            "Jährliche Aktualisierung; konkreter Public-Data-Download wird "
            "pro Index-Version verifiziert."
        )

        if field.get("data"):
            field["status"] = "REAL_CACHED"
        else:
            field["status"] = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# IEA MESGEN / MESBAL
# ---------------------------------------------------------------------------

def update_iea(cache: dict[str, Any]) -> None:
    """
    Absichtlich konservativ:
    Die IEA hat auf die neue SDMX-Struktur umgestellt und stellt offizielle
    MESGEN/MESBAL-Mapping-Dateien bereit. Die konkreten Keys dürfen nicht
    geraten werden. Bis die Mapping-Datei lokal/programmatisch eingelesen
    werden kann, bleiben die Felder UNAVAILABLE bzw. bestehender Cache wird
    nach Alter geprüft.
    """
    for field_name, dataset in IEA_DATASETS.items():
        field = cache["IEA_ELECTRICITY"][field_name]
        field.setdefault("source_notes", []).append(
            f"{dataset}: offizielle IEA-SDMX-Mapping-Datei erforderlich; "
            "kein frei erfundener SDMX-Key."
        )
        if field.get("data"):
            cache["IEA_ELECTRICITY"][field_name] = set_real_cached_if_valid(
                field, "IEA_ELECTRICITY"
            )
        else:
            field["status"] = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# SIPRI
# ---------------------------------------------------------------------------

def update_sipri(cache: dict[str, Any]) -> None:
    """
    SIPRI veröffentlicht die Military Expenditure Database als offiziellen
    Download. Der konkrete XLSX-Download wird nicht hier hart erfunden;
    die offizielle Datenbankseite ist die Referenz.
    """
    for field_name in (
        "military_expenditure_real",
        "military_burden_gdp",
        "military_share_government",
    ):
        field = cache["SIPRI_DEFENCE"][field_name]
        field.setdefault("source_notes", []).append(
            "Quelle: offizielle SIPRI Military Expenditure Database; "
            "jährliche XLSX-Datenbasis."
        )
        if field.get("data"):
            cache["SIPRI_DEFENCE"][field_name] = set_real_cached_if_valid(
                field, "SIPRI_DEFENCE"
            )
        else:
            field["status"] = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Prüfung / Status
# ---------------------------------------------------------------------------

def check_cache(cache: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []

    expected = {
        "OECD_STAN": {
            "value_added", "investment", "labour_input"
        },
        "OECD_PRODUCTIVITY": {
            "labour_productivity_level", "labour_productivity_growth"
        },
        "AI_INDEX": {
            "ai_investment", "ai_adoption", "ai_compute"
        },
        "IEA_ELECTRICITY": {
            "electricity_generation", "electricity_balance"
        },
        "SIPRI_DEFENCE": {
            "military_expenditure_real",
            "military_burden_gdp",
            "military_share_government",
        },
    }

    for section, fields in expected.items():
        if section not in cache:
            errors.append(f"Fehlender Abschnitt: {section}")
            continue
        for field in fields:
            if field not in cache[section]:
                errors.append(f"Fehlendes Feld: {section}.{field}")

    if cache.get("cache_version") != CACHE_VERSION:
        errors.append(
            f"Unerwartete Cache-Version: {cache.get('cache_version')}"
        )

    return not errors, errors


def print_summary(cache: dict[str, Any]) -> None:
    print("NEUBER MACRO & MARKETS – STRUKTUR-TRENDS CACHE")
    print("=" * 56)
    print(f"Cache-Version : {cache.get('cache_version')}")
    print(f"Aktualisiert  : {cache.get('cache_updated_at')}")
    print()

    for section, fields in cache.items():
        if not isinstance(fields, dict) or section in (
            "cache_version", "cache_created_at", "cache_updated_at"
        ):
            continue

        print(f"[{section}]")
        for name, field in fields.items():
            summary = cache_field_summary(field)
            print(
                f"  {name:32s} "
                f"{summary['status']:14s} "
                f"period={str(summary['data_period']):12s} "
                f"obs={summary['observations']}"
            )
        print()


# ---------------------------------------------------------------------------
# Orchestrierung
# ---------------------------------------------------------------------------

def run(update: bool = True, start_period: int = 2000) -> int:
    cache = read_json(CACHE_FILE)

    # Struktur ergänzen, falls ein älterer/teilweiser Cache vorliegt.
    template = new_cache()
    for section, fields in template.items():
        if section in ("cache_version", "cache_created_at", "cache_updated_at"):
            continue
        cache.setdefault(section, {})
        for field_name, field in fields.items():
            cache[section].setdefault(field_name, field)

    if update:
        update_oecd_stan(cache, start_period=start_period)
        update_oecd_productivity(cache, start_period=start_period)
        update_ai_index(cache)
        update_iea(cache)
        update_sipri(cache)

    # Cache-Metadaten beim Übergang von einer älteren Cache-Version
    # kontrolliert auf die aktuelle Strukturversion migrieren.
    # Die vorhandenen Beobachtungen werden dabei nicht verworfen oder verändert.
    cache["cache_version"] = CACHE_VERSION
    cache["cache_updated_at"] = now_iso()
    atomic_write_json(CACHE_FILE, cache)

    ok, errors = check_cache(cache)
    print_summary(cache)

    if not ok:
        print("CHECK: FEHLER")
        for error in errors:
            print(f"  - {error}")
        return 2

    print("CHECK: OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Zentraler Cache für strukturelle Markttrends."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Nur Cache-Struktur prüfen, keine Quellen abrufen.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Cache-Zusammenfassung anzeigen.",
    )
    parser.add_argument(
        "--start-period",
        type=int,
        default=2000,
        help="Ältestes abzurufendes Jahr (Standard: 2000).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if args.check or args.show:
        cache = read_json(CACHE_FILE)
        if args.show:
            print_summary(cache)
        ok, errors = check_cache(cache)
        if not ok:
            for error in errors:
                print(f"ERROR: {error}")
            return 2
        return 0

    return run(update=True, start_period=args.start_period)


if __name__ == "__main__":
    sys.exit(main())

