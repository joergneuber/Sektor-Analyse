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
import subprocess
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
    "Geschlossene Positionen_Tab2.csv",
)
EVENT_PATHS = (
    "fomc_termine.json",
    "forexfactory_events.json",
    "forexfactory_calendar.json",
    "event_calendar.json",
)
SUPPORTED = {".csv", ".tsv", ".json", ".xlsx", ".xls", ".txt", ".md", ".sqlite", ".db"}


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


def _csv_dialect(sample: str):
    """Choose the dominant delimiter from the header before sniffing body text.

    Company names often contain commas, so csv.Sniffer can misclassify a
    semicolon-delimited export as comma-delimited. Header frequency is a more
    stable signal for these project exports.
    """
    first_line = sample.splitlines()[0] if sample.splitlines() else sample
    candidates = [",", ";", "\t", "|"]
    counts = {delimiter: first_line.count(delimiter) for delimiter in candidates}
    delimiter = max(candidates, key=lambda item: counts[item])
    if counts[delimiter] > 0:
        return csv.excel_tab if delimiter == "\t" else type("Dialect", (csv.Dialect,), {
            "delimiter": delimiter, "quotechar": '"', "doublequote": True,
            "skipinitialspace": False, "lineterminator": "\r\n", "quoting": csv.QUOTE_MINIMAL,
            "strict": False,
        })
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _csv_metadata(path: Path) -> dict:
    result = {"row_count": None, "columns": [], "read_error": None}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            dialect = _csv_dialect(sample)
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
            # Parse the complete file. _read_text intentionally caps ordinary
            # text scans, but applying that cap to JSON creates false corruption
            # reports whenever a valid cache exceeds one megabyte.
            with path.open("r", encoding="utf-8-sig") as handle:
                parsed = json.load(handle)
            item["json_top_level_type"] = type(parsed).__name__
            if isinstance(parsed, list):
                item["row_count"] = len(parsed)
            elif isinstance(parsed, dict):
                item["json_top_level_keys"] = sorted(str(k) for k in parsed.keys())[:100]
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
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
    name = path.name.casefold()
    closed_source = "geschlossene position" in name or "initialbestand" in name
    columns = {c.casefold().strip(): c for c in meta.get("columns", [])}
    stop_key = next((columns[k] for k in ("stop", "stop_loss", "stop-loss") if k in columns), None)
    entry_key = next((columns[k] for k in ("einstieg", "einstiegskurs", "entry", "entry_price") if k in columns), None)
    current_key = next((columns[k] for k in ("aktueller_kurs", "aktueller kurs", "current_price", "last_price", "kurs") if k in columns), None)
    direction_key = next((columns[k] for k in ("richtung", "side", "richtung (long/short)", "typ") if k in columns), None)
    result = {
        "source": _safe_relative(path),
        "scope": "CLOSED_HISTORY_EXCLUDED_FROM_OPEN_RISK" if closed_source else "OPEN_PORTFOLIO",
        "assumed_position_quantity_per_position": 1,
        "stop_distance_per_unit": [],
        "monetary_risk": None,
        "monetary_risk_status": "EXCLUDED_CLOSED_POSITION_HISTORY" if closed_source else "NOT_CALCULABLE_MISSING_ENTRY_OR_STOP",
        "invalid_stop_rows": [],
        "locked_in_profit_per_unit": [],
    }
    if closed_source:
        # Historical trades are inventoried, but must not contaminate current
        # open portfolio risk totals.
        return result
    if not stop_key or not entry_key:
        return result
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192); handle.seek(0)
            rows = list(csv.DictReader(handle, dialect=_csv_dialect(sample)))
    except (OSError, UnicodeError, csv.Error):
        result["monetary_risk_status"] = "NOT_CALCULABLE_SOURCE_READ_ERROR"
        return result

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

    monetary = 0.0
    complete = True
    counted = 0
    for index, row in enumerate(rows, start=2):
        ticker = str(row.get(columns.get("ticker", columns.get("symbol", "")), "") or "").strip()
        status_key = columns.get("status")
        status = str(row.get(status_key, "") if status_key else "").strip().casefold()
        if status and any(token in status for token in ("geschlossen", "verkauft", "gestoppt", "closed", "sold", "stopped")):
            continue
        entry, stop = number(row.get(entry_key)), number(row.get(stop_key))
        current = number(row.get(current_key)) if current_key else None
        direction = str(row.get(direction_key, "") if direction_key else "").strip().casefold()
        if entry is None or stop is None:
            complete = False
            continue
        is_short = direction in {"short", "sell", "shortposition", "short-position"}
        is_long = direction in {"long", "buy", "longposition", "long-position"}
        if not (is_long or is_short):
            result["stop_distance_per_unit"].append({"row": index, "ticker": ticker, "status": "DIRECTION_UNKNOWN"})
            complete = False
            continue
        # A trailing stop may cross the entry price. It is only invalid when it
        # is on the wrong side of the current market price (if known).
        invalid = current is not None and ((is_long and stop > current) or (is_short and stop < current))
        if invalid:
            result["invalid_stop_rows"].append({"row": index, "ticker": ticker, "direction": direction,
                                                "entry": entry, "current_price": current, "stop": stop,
                                                "reason": "STOP_WRONG_SIDE_OF_CURRENT_PRICE"})
            complete = False
            continue
        if is_long:
            risk = max(0.0, current - stop) if current is not None else max(0.0, entry - stop)
            locked = max(0.0, stop - entry)
        else:
            risk = max(0.0, stop - current) if current is not None else max(0.0, stop - entry)
            locked = max(0.0, entry - stop)
        result["stop_distance_per_unit"].append({"row": index, "ticker": ticker, "distance": round(risk, 6),
                                                 "status": "VALID", "basis": "CURRENT_PRICE_TO_STOP" if current is not None else "ENTRY_TO_STOP"})
        result["locked_in_profit_per_unit"].append({"row": index, "ticker": ticker, "amount": round(locked, 6)})
        monetary += risk  # fixed assumption: one unit per open position
        counted += 1
    result["positions_counted"] = counted
    if result["invalid_stop_rows"]:
        result["monetary_risk_status"] = "INVALID_STOP_RELATIVE_TO_CURRENT_PRICE"
    elif not complete:
        result["monetary_risk_status"] = "PARTIAL_MISSING_OR_UNVERIFIED_FIELDS"
    elif counted:
        result["monetary_risk"] = round(monetary, 6)
        result["monetary_risk_status"] = "CALCULATED_ASSUMING_ONE_UNIT_PER_POSITION"
    else:
        result["monetary_risk_status"] = "NOT_CALCULABLE_NO_VALID_OPEN_POSITIONS"
    return result


def _git_value(*args: str) -> str | None:
    try:
        value = subprocess.check_output(
            ["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL, text=True
        ).strip()
        return value or None
    except (OSError, subprocess.CalledProcessError):
        return None


def _run_identity() -> dict:
    commit = os.environ.get("GITHUB_SHA") or _git_value("rev-parse", "HEAD")
    return {
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "ref": os.environ.get("GITHUB_REF"),
        "commit_sha": commit,
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "workflow_name": os.environ.get("GITHUB_WORKFLOW"),
        "source_file_hashes": {
            name: sha256(ROOT / name)
            for name in (
                "Trading_Radar_Alleinauswertung.py",
                "trading_radar_neuauswertung.py",
                "trading_radar_report_gate.py",
                "trading_radar_market_first.py",
                "trading_radar_run_evidence.py",
                "trading_radar_thesen.py",
                "trading_radar_portfolio_performance.py",
                "makro_szenario.py",
                "offene_positionen_check.py",
                "historical_ohlcv.py",
                "historical_ohlcv_pending_queue.py",
                "einzel_check.py",
                "ohlcv_release_bundle.py",
                "scripts/get_gh_release_assets.sh",
                "scripts/restore_ohlcv_release_bundle.sh",
                ".github/workflows/main.yml",
                ".github/workflows/einzel_check.yml",
            )
            if (ROOT / name).is_file()
        },
        "gate_results": {
            key: os.environ.get(key)
            for key in (
                "TRADING_RADAR_CONTRACT_OUTCOME",
                "TRADING_RADAR_STEP_OUTCOME",
                "TRADING_RADAR_OHLCV_PERSISTENCE_GATE",
                "TRADING_RADAR_OHLCV_QUEUE_GATE",
                "TRADING_RADAR_MARKET_FIRST_GATE",
                "TRADING_RADAR_REPORT_GATE",
                "TRADING_RADAR_REPORT_GATE_OUTCOME",
                "TRADING_RADAR_FACHLICH_FINAL",
                "TRADING_RADAR_REPORT_GATE",
                "TRADING_RADAR_REPORT_GATE_OUTCOME",
            )
            if os.environ.get(key) is not None
        },
    }


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
        "schema_version": 3,
        "phase": phase,
        "run_identity": _run_identity(),
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
            "Risk calculations assume exactly one unit per open position; recorded quantities do not change this assumption.",
            "A trailing stop beyond entry is allowed; a stop is flagged only when it is on the wrong side of the current price when that price is available.",
            "Closed-position history is inventoried but excluded from current open portfolio risk.",
            "Unknown direction, missing entry/stop, or unavailable current price limits the strength of risk validation.",
        ],
    }


def finalize_gate_result() -> int:
    try:
        end = json.loads(END_FILE.read_text(encoding="utf-8"))
        summary = json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"TRADING_RADAR_EVIDENCE_FINALIZE=FAIL | {exc}", file=sys.stderr)
        return 1
    result = os.environ.get("TRADING_RADAR_REPORT_GATE", "").strip().casefold()
    final_status = os.environ.get("TRADING_RADAR_FACHLICH_FINAL", "").strip()
    if result not in {"success", "failure", "cancelled", "skipped"}:
        result = "missing"
    status = "PASS" if result == "success" else "FAIL"
    for payload in (end, summary.get("end", {})):
        identity = payload.setdefault("run_identity", {})
        gates = identity.setdefault("gate_results", {})
        for key in (
            "TRADING_RADAR_CONTRACT_OUTCOME",
            "TRADING_RADAR_STEP_OUTCOME",
            "TRADING_RADAR_OHLCV_PERSISTENCE_GATE",
            "TRADING_RADAR_OHLCV_QUEUE_GATE",
            "TRADING_RADAR_MARKET_FIRST_GATE",
        ):
            value = os.environ.get(key)
            if value is not None and value.strip():
                gates[key] = value.strip()
        gates["TRADING_RADAR_REPORT_GATE"] = status
        gates["TRADING_RADAR_REPORT_GATE_OUTCOME"] = result
        if final_status in {"FINAL", "NICHT_FINAL"}:
            gates["TRADING_RADAR_FACHLICH_FINAL"] = final_status
    summary["run_identity"] = end.get("run_identity", {})
    END_FILE.write_text(json.dumps(end, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"TRADING_RADAR_REPORT_GATE={status}")
    return 0



def validate_final_evidence() -> int:
    """Validate the final run-evidence artifact after the report-gate result is attached."""
    try:
        end = json.loads(END_FILE.read_text(encoding="utf-8"))
        summary = json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | {exc}", file=sys.stderr)
        return 1
    identity = end.get("run_identity") if isinstance(end, dict) else None
    if not isinstance(identity, dict):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | run_identity fehlt", file=sys.stderr)
        return 1
    # A run identity is useful only when it identifies one immutable GitHub
    # checkout and one workflow execution. Empty-but-equal values are not proof.
    identity_fields = ("repository", "ref", "commit_sha", "workflow_run_id", "workflow_run_attempt")
    missing_identity = [key for key in identity_fields if not str(identity.get(key) or "").strip()]
    if missing_identity:
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Laufidentität unvollständig: " + ", ".join(missing_identity), file=sys.stderr)
        return 1
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(identity.get("commit_sha") or ""), re.I):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Commit-SHA fehlt oder ist ungültig", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", str(identity.get("repository") or "")):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Repository-Identität ist ungültig", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[0-9]+", str(identity.get("workflow_run_id") or "")) or not re.fullmatch(r"[0-9]+", str(identity.get("workflow_run_attempt") or "")):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Workflow-Run-ID/Attempt ist ungültig", file=sys.stderr)
        return 1
    gates = identity.get("gate_results") or {}
    required = (
        "TRADING_RADAR_CONTRACT_OUTCOME", "TRADING_RADAR_STEP_OUTCOME",
        "TRADING_RADAR_OHLCV_PERSISTENCE_GATE", "TRADING_RADAR_OHLCV_QUEUE_GATE",
        "TRADING_RADAR_MARKET_FIRST_GATE",
        "TRADING_RADAR_REPORT_GATE", "TRADING_RADAR_REPORT_GATE_OUTCOME",
        "TRADING_RADAR_FACHLICH_FINAL",
    )
    missing = [key for key in required if not gates.get(key)]
    if missing:
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Gate-Ergebnisse fehlen: " + ", ".join(missing), file=sys.stderr)
        return 1
    # A failed report gate is a valid recorded outcome for a NICHT_FINAL run.
    # Evidence validation verifies completeness and truthful status; it must not
    # require a failed quality gate to be rewritten as success.
    report_gate_passed = (
        gates["TRADING_RADAR_REPORT_GATE"] == "PASS"
        and gates["TRADING_RADAR_REPORT_GATE_OUTCOME"] == "success"
    )
    report_gate_failed = (
        gates["TRADING_RADAR_REPORT_GATE"] == "FAIL"
        and gates["TRADING_RADAR_REPORT_GATE_OUTCOME"] in {"failure", "cancelled", "skipped"}
    )
    if not (report_gate_passed or (
        report_gate_failed and gates["TRADING_RADAR_FACHLICH_FINAL"] == "NICHT_FINAL"
    )):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Report-Gate-Ausgang widerspricht Fachstatus", file=sys.stderr)
        return 1
    if gates["TRADING_RADAR_FACHLICH_FINAL"] == "FINAL":
        expected = {
            "TRADING_RADAR_CONTRACT_OUTCOME": "success",
            "TRADING_RADAR_STEP_OUTCOME": "success",
            "TRADING_RADAR_OHLCV_PERSISTENCE_GATE": "PASS",
            "TRADING_RADAR_OHLCV_QUEUE_GATE": "PASS",
            "TRADING_RADAR_MARKET_FIRST_GATE": "PASS",
        }
        failed = [key for key, value in expected.items() if gates.get(key) != value]
        if failed:
            print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | FINAL trotz fehlgeschlagener Gates: " + ", ".join(failed), file=sys.stderr)
            return 1
    start = summary.get("start") if isinstance(summary, dict) else None
    end_payload = summary.get("end") if isinstance(summary, dict) else None
    if not isinstance(start, dict) or not isinstance(end_payload, dict):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Start-/End-Snapshot fehlt", file=sys.stderr)
        return 1
    start_id = start.get("run_identity") or {}
    end_id = end_payload.get("run_identity") or {}
    summary_identity = summary.get("run_identity") or {}
    if summary_identity != identity or end_id.get("gate_results") != gates:
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Zusammenfassender Laufnachweis widerspricht End-Snapshot/Gate-Ergebnis", file=sys.stderr)
        return 1
    if any(
        start_id.get(key) != identity.get(key) or end_id.get(key) != identity.get(key)
        for key in ("repository", "ref", "commit_sha", "workflow_run_id", "workflow_run_attempt")
    ):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Start/End/Gate-Nachweis nicht demselben Lauf zugeordnet", file=sys.stderr)
        return 1
    required_source_files = {
        "Trading_Radar_Alleinauswertung.py", "trading_radar_neuauswertung.py",
        "trading_radar_report_gate.py", "trading_radar_market_first.py",
        "trading_radar_run_evidence.py", "trading_radar_thesen.py",
        "trading_radar_portfolio_performance.py", "offene_positionen_check.py",
        "historical_ohlcv.py", "historical_ohlcv_pending_queue.py", "einzel_check.py",
        "ohlcv_release_bundle.py", "scripts/get_gh_release_assets.sh",
        "scripts/restore_ohlcv_release_bundle.sh",
        ".github/workflows/main.yml", ".github/workflows/einzel_check.yml",
    }
    end_code_hashes = identity.get("source_file_hashes")
    start_code_hashes = start_id.get("source_file_hashes")
    if not isinstance(end_code_hashes, dict) or not required_source_files.issubset(end_code_hashes):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Quellcode-Hashes unvollständig", file=sys.stderr)
        return 1
    if not isinstance(start_code_hashes, dict) or start_code_hashes != end_code_hashes:
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Quellcode-Hashes zwischen Start und Ende weichen ab", file=sys.stderr)
        return 1
    if any(not re.fullmatch(r"[0-9a-f]{64}", str(end_code_hashes.get(name) or ""), re.I) for name in required_source_files):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | mindestens ein Quellcode-Hash ist ungültig", file=sys.stderr)
        return 1
    sources = end_payload.get("sources")
    if not isinstance(sources, list) or not sources or any(
        not isinstance(item, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""), re.I)
        for item in sources
    ):
        print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=FAIL | Eingabedaten-Hashes fehlen", file=sys.stderr)
        return 1
    print("TRADING_RADAR_EVIDENCE_FINAL_CHECK=PASS | Run-ID, Commit, Quellen-Hashes und sämtliche Gate-Ergebnisse sind verknüpft")
    return 0


def run(phase: str) -> int:
    snapshot = collect_snapshot(phase)
    target = START_FILE if phase == "start" else END_FILE
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if phase == "end":
        summary = {"schema_version": 3, "start": None, "end": snapshot,
                   "run_identity": snapshot.get("run_identity", {})}
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
    if len(sys.argv) == 2 and sys.argv[1] == "gate-result":
        raise SystemExit(finalize_gate_result())
    if len(sys.argv) == 2 and sys.argv[1] == "validate-final":
        raise SystemExit(validate_final_evidence())
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "end"}:
        print("Usage: python trading_radar_run_evidence.py start|end|gate-result|validate-final", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
