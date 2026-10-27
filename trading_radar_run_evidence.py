"""Run-wide Trading Radar source, coverage and risk evidence.

The evidence is deliberately conservative: AVAILABLE means every required source
category was identified. Missing categories are reported separately and cannot
be hidden by finding a portfolio file alone.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.cwd()
START_FILE = ROOT / "trading_radar_run_evidence_start.json"
END_FILE = ROOT / "trading_radar_run_evidence_end.json"
SUMMARY_FILE = ROOT / "trading_radar_run_evidence.json"

# Workflow-owned paths and known data contracts; additional files are discovered
# by both name and content, across supported production formats.
PORTFOLIO_PATHS = (
    "Offene_Positionen.csv",
    "Offene_Positionen_Check.csv",
    "Offene_Positionen_Check.xlsx",
    "Geschlossene Positionen_Initialbestand.csv",
)
EVENT_PATHS = (
    "fomc_termine.json",
    "forexfactory_events.json",
    "forexfactory_calendar.json",
    "event_calendar.json",
)
SUPPORTED = {".csv", ".tsv", ".json", ".xlsx", ".xls", ".txt", ".md"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _read_text(path: Path, limit: int = 1_000_000) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")[:limit]
    except (OSError, UnicodeError):
        return ""


def _csv_metadata(path: Path) -> dict:
    result = {"row_count": None, "columns": [], "read_error": None}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(handle, dialect=dialect)
            result["columns"] = [str(x).strip() for x in (reader.fieldnames or [])]
            result["row_count"] = sum(1 for _ in reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        result["read_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _metadata(path: Path) -> dict:
    stat = path.stat()
    item = {
        "path": _safe_relative(path),
        "format": path.suffix.lower().lstrip(".") or "no-extension",
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": sha256(path),
        "row_count": None,
        "columns": [],
        "read_error": None,
    }
    if path.suffix.lower() in {".csv", ".tsv"}:
        item.update(_csv_metadata(path))
    elif path.suffix.lower() == ".json":
        try:
            parsed = json.loads(_read_text(path))
            item["json_top_level_type"] = type(parsed).__name__
            if isinstance(parsed, list):
                item["row_count"] = len(parsed)
            elif isinstance(parsed, dict):
                item["json_top_level_keys"] = sorted(str(k) for k in parsed.keys())[:100]
        except (json.JSONDecodeError, TypeError) as exc:
            item["read_error"] = f"{type(exc).__name__}: {exc}"
    elif path.suffix.lower() in {".xlsx", ".xls"}:
        # Do not require spreadsheet libraries for evidence collection; record
        # format/hash/path and let coverage state that row-level parsing is absent.
        item["row_count"] = None
        item["read_error"] = "Spreadsheet metadata only; row-level parser not configured"
    return item


def _all_files() -> list[Path]:
    excluded = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        if any(part in excluded for part in path.parts):
            continue
        if path.name in {"trading_radar_run_evidence_start.json",
                         "trading_radar_run_evidence_end.json",
                         "trading_radar_run_evidence.json"}:
            continue
        files.append(path)
    return sorted(files)


def _classify(path: Path, meta: dict, text: str) -> set[str]:
    name = path.name.casefold()
    low = text.casefold()
    categories = set()
    columns = " ".join(meta.get("columns", [])).casefold()

    # Portfolio is based on explicit position/portfolio schema, not any CSV.
    portfolio_name = any(token in name for token in (
        "offene_position", "offene position", "geschlossene position", "portfolio_snapshot",
        "portfolio_positions", "positionsbestand",
    ))
    portfolio_columns = (
        ("ticker" in columns or "symbol" in columns)
        and any(k in columns for k in ("einstieg", "einstiegskurs", "entry", "entry_price"))
        and any(k in columns for k in ("stop", "stop_loss", "stop-loss"))
    )
    if portfolio_name or portfolio_columns:
        categories.add("portfolio")

    candidate_name = any(token in name for token in (
        "kandidat", "candidate", "scanner", "trendwende", "edelmetalle",
        "short_scan", "short_setups", "langfrist_scan", "langfrist_bewertung",
        "setups(", "trading_radar",
    ))
    candidate_content = (
        ("technische kandidaten" in low or "jetzt technisch prüfen" in low
         or "crv" in columns and ("trigger" in columns or "einstieg" in columns))
    )
    # General project documentation mentioning a candidate is not sufficient:
    # require a report/source file or a scanner/candidate-like path.
    if candidate_name and (path.suffix.lower() in {".csv", ".tsv", ".json", ".xlsx", ".xls", ".txt", ".md"}):
        if ("test_" not in name and "readme" not in name and "forensik" not in name
                and "manifest" not in name):
            categories.add("candidates")
    if candidate_content:
        categories.add("candidates")

    event_name = any(token in name for token in (
        "event", "ereignis", "kalender", "fomc", "forexfactory", "gdelt",
    ))
    event_content = path.suffix.lower() == ".json" and (
        any(token in low for token in (
            '"event_date"', '"scheduled_event"', '"release_date"', '"event_time"',
            '"consensus"', '"previous"', '"actual"', '"country"',
        ))
    )
    if event_name or event_content:
        if "test_" not in name and "readme" not in name and "forensik" not in name:
            categories.add("events")
    return categories


def _risk_metrics(path: Path, meta: dict) -> dict | None:
    if "portfolio" not in _classify(path, meta, _read_text(path, 200_000)):
        return None
    columns = {c.casefold().strip(): c for c in meta.get("columns", [])}
    stop_key = next((columns[k] for k in ("stop", "stop_loss", "stop-loss") if k in columns), None)
    entry_key = next((columns[k] for k in ("einstieg", "einstiegskurs", "entry", "entry_price") if k in columns), None)
    direction_key = next((columns[k] for k in ("richtung", "side", "richtung (long/short)", "typ") if k in columns), None)
    qty_key = next((columns[k] for k in ("stückzahl", "stueckzahl", "menge", "quantity", "qty", "shares") if k in columns), None)
    result = {
        "source": _safe_relative(path),
        "stop_distance_per_unit": [],
        "monetary_risk": None,
        "monetary_risk_status": "NOT_CALCULABLE_MISSING_POSITION_SIZE",
        "invalid_stop_rows": [],
    }
    if not stop_key or not entry_key:
        result["monetary_risk_status"] = "NOT_CALCULABLE_MISSING_ENTRY_OR_STOP"
        return result
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192); handle.seek(0)
            try: dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error: dialect = csv.excel
            rows = list(csv.DictReader(handle, dialect=dialect))
    except (OSError, UnicodeError, csv.Error):
        result["monetary_risk_status"] = "NOT_CALCULABLE_SOURCE_READ_ERROR"
        return result

    monetary = 0.0
    monetary_complete = bool(qty_key)
    for index, row in enumerate(rows, start=2):
        def number(value):
            raw = str(value or "").strip().replace(" ", "").replace("’", "").replace("'", "")
            if not raw: return None
            if "," in raw and "." in raw:
                raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
            elif "," in raw: raw = raw.replace(",", ".")
            try:
                val = float(raw)
                return val if math.isfinite(val) else None
            except ValueError:
                return None
        entry, stop = number(row.get(entry_key)), number(row.get(stop_key))
        direction = str(row.get(direction_key, "") if direction_key else "").strip().casefold()
        if entry is None or stop is None:
            continue
        if direction in {"short", "sell", "shortposition", "short-position"}:
            invalid = stop < entry
            distance = stop - entry
        else:
            # Unknown direction is not silently treated as a verified long.
            invalid = stop >= entry if direction in {"long", "buy", "longposition", "long-position"} else None
            distance = entry - stop
        if invalid is True:
            result["invalid_stop_rows"].append({"row": index, "direction": direction, "entry": entry, "stop": stop})
            continue
        if invalid is None:
            result["stop_distance_per_unit"].append({"row": index, "distance": abs(distance), "status": "DIRECTION_UNKNOWN"})
        else:
            result["stop_distance_per_unit"].append({"row": index, "distance": distance, "status": "VALID"})
        if qty_key:
            qty = number(row.get(qty_key))
            if qty is None or invalid is None or invalid is True:
                monetary_complete = False
            else:
                monetary += distance * qty
    if result["invalid_stop_rows"]:
        result["monetary_risk_status"] = "INVALID_STOP_PRESENT"
    elif monetary_complete and qty_key:
        result["monetary_risk"] = round(monetary, 6)
        result["monetary_risk_status"] = "CALCULATED_FROM_AVAILABLE_ROWS"
    return result


def collect_snapshot(phase: str) -> dict:
    files = _all_files()
    entries = []
    for path in files:
        try:
            meta = _metadata(path)
        except OSError:
            continue
        text = _read_text(path)
        categories = sorted(_classify(path, meta, text))
        meta["categories"] = categories
        entries.append(meta)

    category_paths = {
        category: [item["path"] for item in entries if category in item["categories"]]
        for category in ("portfolio", "candidates", "events")
    }
    # Portfolio category requires the active backend portfolio source. Candidate
    # and event categories require at least one source each. Coverage is explicit.
    portfolio_found = any(Path(p).name.casefold() == "offene_positionen.csv" for p in category_paths["portfolio"])
    coverage = {
        "portfolio": {
            "status": "AVAILABLE" if portfolio_found else "MISSING",
            "required": ["Offene_Positionen.csv"],
            "found": category_paths["portfolio"],
            "complete": portfolio_found,
        },
        "candidates": {
            "status": "AVAILABLE" if category_paths["candidates"] else "MISSING",
            "required": ["mindestens eine Kandidaten-/Scannerquelle oder aktueller Trading-Radar-Bericht"],
            "found": category_paths["candidates"],
            "complete": bool(category_paths["candidates"]),
        },
        "events": {
            "status": "AVAILABLE" if category_paths["events"] else "MISSING",
            "required": ["mindestens eine Ereignis-/Kalenderquelle"],
            "found": category_paths["events"],
            "complete": bool(category_paths["events"]),
        },
    }
    all_complete = all(item["complete"] for item in coverage.values())
    baseline_complete = coverage["portfolio"]["complete"] and coverage["events"]["complete"]
    risk = []
    for path in files:
        try:
            meta = _metadata(path)
            metric = _risk_metrics(path, meta)
            if metric:
                risk.append(metric)
        except OSError:
            continue
    production_io_contract = {
        "portfolio_inputs": [
            "Offene_Positionen.csv (positionen_tracker.py / analyse.py)",
            "Google Drive spreadsheet: Offene_Positionen (positionen_tracker.py)",
            "Geschlossene Positionen_Initialbestand.csv (offene_positionen_check.py)",
        ],
        "portfolio_outputs": [
            "Offene Positionen+Check.csv (offene_positionen_check.py)",
            "Geschlossene Positionen.csv (offene_positionen_check.py)",
            "Drive sheet: Offene Positionen+Check",
        ],
        "candidate_inputs_and_outputs": [
            "Setups(<Datum>).csv / Briefing(<Datum>).txt (analyse.py / Trading_Radar_Alleinauswertung.py)",
            "Trendwende_Setups(<Datum>).csv / Trendwende_Briefing(<Datum>).txt (trendwende_scanner.py)",
            "Edelmetalle_Setups(<Datum>).csv / Edelmetalle_Briefing(<Datum>).txt (edelmetalle_scanner.py)",
            "Short_Setups(<Datum>).csv / Short_Briefing(<Datum>).txt (short_scanner.py)",
            "Langfrist_Bewertung(<Datum>).csv / Langfrist_Briefing(<Datum>).txt (langfrist_scanner.py)",
            "Auswertung(<Datum>).txt (Trading_Radar_Alleinauswertung.py)",
        ],
        "event_inputs_and_caches": [
            "fomc_termine.json (analyse.py)",
            ".macro_cache/forexfactory_calendar.json (makro_szenario.py)",
            ".macro_cache/geo_news_cache.json (makro_szenario.py)",
            "GDELT / LME runner outputs where emitted by their source modules",
        ],
        "supported_inventory_formats": sorted(SUPPORTED),
    }
    return {
        "schema_version": 2,
        "phase": phase,
        "production_io_contract": production_io_contract,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": ".",
        "overall_status": "AVAILABLE" if all_complete else ("BASELINE_CAPTURED" if phase == "start" and baseline_complete else "INCOMPLETE"),
        "coverage_complete": all_complete,
        "baseline_coverage_complete": baseline_complete,
        "coverage": coverage,
        "source_count": len(entries),
        "sources": entries,
        "risk": risk,
        "limitations": [
            "AVAILABLE requires portfolio, candidate and event coverage to be complete.",
            "Spreadsheet files are fingerprinted but not row-count parsed.",
            "Monetary risk is calculated only where entry, stop, direction and position size are available.",
            "Unknown position direction or missing position size prevents a complete monetary-risk claim.",
        ],
    }


def run(phase: str) -> int:
    snapshot = collect_snapshot(phase)
    target = START_FILE if phase == "start" else END_FILE
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if phase == "end":
        summary = {"schema_version": 2, "start": None, "end": snapshot}
        try:
            start = json.loads(START_FILE.read_text(encoding="utf-8"))
            summary["start"] = start
        except (OSError, json.JSONDecodeError):
            summary["start_error"] = "Start-Snapshot fehlt oder ist unlesbar."
            summary["overall_status"] = "INCOMPLETE"
        else:
            # The start snapshot is intentionally before scanners/report generation:
            # it must establish the portfolio baseline and event source, while the
            # end snapshot must have all three categories. A missing start candidate
            # source is therefore visible but does not make a successful end-to-end
            # inventory mathematically impossible.
            summary["overall_status"] = (
                "AVAILABLE"
                if start.get("baseline_coverage_complete") is True
                and snapshot.get("coverage_complete") is True
                else "INCOMPLETE"
            )
            summary["coverage_complete"] = summary["overall_status"] == "AVAILABLE"
            summary["source_changes"] = {
                phase_name: {
                    "added": sorted(set(snapshot["coverage"][phase_name]["found"]) - set(start["coverage"][phase_name]["found"])),
                    "removed": sorted(set(start["coverage"][phase_name]["found"]) - set(snapshot["coverage"][phase_name]["found"])),
                }
                for phase_name in ("portfolio", "candidates", "events")
            }
        SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"TRADING_RADAR_EVIDENCE_PHASE={phase}")
    print(f"TRADING_RADAR_EVIDENCE_STATUS={snapshot['overall_status']}")
    for category, detail in snapshot["coverage"].items():
        print(f"COVERAGE_{category.upper()}={detail['status']} | sources={len(detail['found'])}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "end"}:
        print("Usage: python trading_radar_run_evidence.py start|end", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
