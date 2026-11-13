#!/usr/bin/env python3
"""Durable hand-off of validated Einzel-Check tickers to the main OHLCV writer.

The Einzel-Check writes one immutable Actions artifact per workflow run. The
main workflow discovers queue-enabled runs on its own branch, verifies the
producer step and artifact provenance, imports tickers idempotently, and stores
receipts inside the authoritative SQLite bundle. Missing/expired/corrupt queue
artifacts are explicit incomplete outcomes; they are never treated as empty.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from historical_ohlcv import (
    DEFAULT_DB,
    DEFAULT_DYNAMIC_UNIVERSE,
    EXCLUDED_TICKERS,
    _normalize_market_ticker,
    init_db,
    register_ticker,
)

SCHEMA_VERSION = 1
QUEUE_FILE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_PENDING_QUEUE", "historical_ohlcv_pending_queue.json"))
STATUS_FILE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_PENDING_STATUS", "historical_ohlcv_pending_queue_status.json"))
DB_FILE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_DB", str(DEFAULT_DB)))
UNIVERSE_FILE = Path(os.environ.get("NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE", str(DEFAULT_DYNAMIC_UNIVERSE)))
WORKFLOW_PATH = ".github/workflows/einzel_check.yml"
ARTIFACT_PREFIX = "historical-ohlcv-pending-"
QUEUE_WORKFLOW_MARKER = "NMM_OHLCV_PENDING_QUEUE_SCHEMA: 1"
ARTIFACT_STEP_NAME = "Pending-Queue als Run-Artifact sichern"
SCANNER_STEP_NAME = "Ticker gegen alle drei Strategien prüfen"
MAX_API_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_QUEUE_BYTES = 1024 * 1024
MAX_RUNS_PER_SEARCH = 1000
RECEIPT_TABLE = "historical_ohlcv_pending_queue_receipts"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _context() -> dict:
    # This module is called only by einzel_check.yml. GITHUB_WORKFLOW_REF
    # includes the repository prefix on GitHub, so store the canonical repo-
    # relative path instead of comparing the raw environment value.
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "workflow_path": WORKFLOW_PATH,
        "workflow_run_id": str(os.environ.get("GITHUB_RUN_ID", "")),
        "workflow_run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1") or "1"),
        "branch": os.environ.get("GITHUB_REF_NAME", ""),
        "head_sha": os.environ.get("GITHUB_SHA", ""),
        "created_at_utc": _utc_now(),
        "source": "einzel_check",
        "producer_outcome": "pending",
        "queue_errors": [],
        "tickers": [],
    }


def init_queue(path: Path = QUEUE_FILE) -> dict:
    payload = _context()
    _atomic_json(path, payload)
    print(f"OHLCV_PENDING_QUEUE_INIT=READY|run={payload['workflow_run_id']}|branch={payload['branch']}")
    return payload


def register_pending_ticker(ticker: str, path: Path = QUEUE_FILE) -> bool:
    """Queue one validated GitHub ticker; preserve direct registration for local runs."""
    normalized = _normalize_market_ticker(ticker)
    if not normalized or normalized in EXCLUDED_TICKERS or len(normalized) > 30 or any(ch.isspace() for ch in normalized):
        print(f"OHLCV_PENDING_QUEUE_REJECTED={str(ticker)[:80]}")
        return False
    workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].strip()
    is_queue_producer = bool(
        workflow_ref
        and workflow_ref.endswith("/" + WORKFLOW_PATH)
        and os.environ.get("GITHUB_RUN_ID")
    )
    if not is_queue_producer:
        # Only einzel_check.yml has an artifact producer/importer contract.
        # Other Actions workflows (especially main.yml) must keep registering
        # directly into the shared universe; GITHUB_RUN_ID alone is not enough.
        print("OHLCV_PENDING_QUEUE_DIRECT_REGISTER=" + normalized)
        return bool(register_ticker(normalized))
    if not path.is_file():
        init_queue(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Pending-Queue ist nicht lesbar; Registrierung gesperrt: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Pending-Queue besitzt ein ungueltiges Schema")
    if payload.get("producer_outcome") != "pending":
        raise RuntimeError("Pending-Queue ist bereits finalisiert; weitere Ticker werden nicht angehaengt")
    tickers = payload.get("tickers")
    if not isinstance(tickers, list):
        raise RuntimeError("Pending-Queue enthaelt keine Tickerliste")
    existing = set(tickers)
    if normalized in existing:
        return False
    tickers.append(normalized)
    payload["tickers"] = sorted(existing | {normalized})
    _atomic_json(path, payload)
    print(f"OHLCV_PENDING_QUEUE_REGISTERED={normalized}")
    return True


def record_queue_error(ticker: str, exc: Exception, path: Path = QUEUE_FILE) -> None:
    """Persist a registration failure so an empty/partial queue cannot look complete."""
    if not path.is_file():
        init_queue(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Pending-Queue besitzt ein ungueltiges Schema; Queue-Fehler kann nicht protokolliert werden")
    errors = payload.setdefault("queue_errors", [])
    if not isinstance(errors, list):
        raise RuntimeError("Pending-Queue-Fehlerliste ist ungueltig")
    errors.append({
        "ticker": str(ticker or "")[:40],
        "error_type": type(exc).__name__,
        "message": str(exc).replace("\r", " ").replace("\n", " ")[:300],
        "recorded_at_utc": _utc_now(),
    })
    _atomic_json(path, payload)


def finalize_queue(outcome: str, path: Path = QUEUE_FILE) -> dict:
    normalized_outcome = str(outcome or "").strip().casefold()
    if normalized_outcome not in {"success", "failure", "cancelled", "skipped"}:
        normalized_outcome = "failure"
    if not path.is_file():
        init_queue(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Pending-Queue besitzt ein ungueltiges Schema")
    payload["producer_outcome"] = "success" if normalized_outcome == "success" else "failure"
    payload.setdefault("queue_errors", [])
    if os.environ.get("NMM_PENDING_QUEUE_REGISTRATION_FAILED", "").strip().casefold() == "true":
        failed_ticker = os.environ.get("NMM_PENDING_QUEUE_ERROR_TICKER", "").strip()[:40]
        existing_errors = payload.get("queue_errors", [])
        if not any(isinstance(item, dict) and str(item.get("ticker", "")).upper() == failed_ticker.upper() for item in existing_errors):
            existing_errors.append({
                "ticker": failed_ticker,
                "error_type": "QueueRegistrationError",
                "message": "Registrierungsfehler wurde im Einzel-Check-StepOutput markiert; erneuter Import erforderlich",
                "recorded_at_utc": _utc_now(),
            })
        payload["queue_errors"] = existing_errors
    payload["producer_step_outcome"] = normalized_outcome
    payload["finalized_at_utc"] = _utc_now()
    tickers = payload.get("tickers")
    if not isinstance(tickers, list) or any(not isinstance(x, str) for x in tickers):
        raise RuntimeError("Pending-Queue enthaelt eine ungueltige Tickerliste")
    payload["tickers"] = sorted(set(tickers))
    _atomic_json(path, payload)
    print(f"OHLCV_PENDING_QUEUE_FINALIZED={payload['producer_outcome']}|tickers={len(payload['tickers'])}")
    return payload


def _api_json(url: str, token: str) -> dict:
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nmm-historical-ohlcv-pending-queue",
    })
    with urlopen(request, timeout=30) as response:
        raw = response.read(MAX_API_BYTES + 1)
    if len(raw) > MAX_API_BYTES:
        raise ValueError("GitHub-API-Antwort ueberschreitet die Groessenbegrenzung")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub-API lieferte kein JSON-Objekt")
    return payload


class _StripAuthorizationOnRedirect(HTTPRedirectHandler):
    """Never forward the GitHub bearer token to artifact object storage."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            for key in list(redirected.headers):
                if key.lower() in {"authorization", "proxy-authorization"}:
                    redirected.remove_header(key)
            for key in list(redirected.unredirected_hdrs):
                if key.lower() in {"authorization", "proxy-authorization"}:
                    redirected.remove_header(key)
        return redirected


def _download_artifact(url: str, token: str) -> bytes:
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "nmm-historical-ohlcv-pending-queue",
    })
    with build_opener(_StripAuthorizationOnRedirect()).open(request, timeout=60) as response:
        data = response.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Pending-Queue-Artifact ueberschreitet die Groessenbegrenzung")
    return data


def _workflow_path_from_run(value: str) -> str:
    return str(value or "").split("@", 1)[0]


def _list_run_window(repo: str, branch: str, token: str, start, end, status: str = "completed") -> list[dict]:
    query = urlencode({"branch": branch, "status": status, "created": f"{start.isoformat()}..{end.isoformat()}", "per_page": 100, "page": 1})
    url = f"https://api.github.com/repos/{repo}/actions/workflows/einzel_check.yml/runs?{query}"
    first = _api_json(url, token)
    total = first.get("total_count")
    runs_first = first.get("workflow_runs")
    if type(total) is not int or not isinstance(runs_first, list):
        raise ValueError("Workflow-Run-Abfrage besitzt keine gueltige total_count/workflow_runs-Antwort")
    if total >= MAX_RUNS_PER_SEARCH:
        if start >= end:
            raise ValueError(f"Mehr als {MAX_RUNS_PER_SEARCH} Einzel-Check-Runs an einem Tag; API-Suchlimit erreicht")
        midpoint = start + (end - start) // 2
        return _list_run_window(repo, branch, token, start, midpoint, status) + _list_run_window(repo, branch, token, midpoint + timedelta(days=1), end, status)
    collected = list(runs_first)
    pages = (total + 99) // 100
    for page in range(2, pages + 1):
        page_query = urlencode({"branch": branch, "status": status, "created": f"{start.isoformat()}..{end.isoformat()}", "per_page": 100, "page": page})
        page_payload = _api_json(f"https://api.github.com/repos/{repo}/actions/workflows/einzel_check.yml/runs?{page_query}", token)
        page_runs = page_payload.get("workflow_runs")
        if not isinstance(page_runs, list) or not page_runs:
            raise ValueError(f"Workflow-Run-Pagination unvollstaendig: Seite {page} fehlt")
        collected.extend(page_runs)
    if len(collected) != total:
        raise ValueError(f"Workflow-Run-Pagination unvollstaendig: erwartet={total}, erhalten={len(collected)}")
    return collected


def _list_recent_runs(repo: str, branch: str, token: str, lookback_days: int = 90, status: str = "completed") -> list[dict]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days)
    all_runs = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=29))
        all_runs.extend(_list_run_window(repo, branch, token, cursor, window_end, status))
        cursor = window_end + timedelta(days=1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    unique = {}
    for run in all_runs:
        if not isinstance(run, dict) or run.get("id") is None:
            continue
        created_raw = str(run.get("created_at") or "")
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            unique[str(run["id"])] = run
    return sorted(unique.values(), key=lambda item: (str(item.get("created_at", "")), int(item.get("id", 0))))


def _workflow_has_queue(repo: str, head_sha: str, token: str, cache: dict[str, bool]) -> bool:
    if head_sha in cache:
        return cache[head_sha]
    query = urlencode({"ref": head_sha})
    payload = _api_json(f"https://api.github.com/repos/{repo}/contents/{WORKFLOW_PATH}?{query}", token)
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise ValueError(f"Workflow-Datei am Commit {head_sha} kann nicht als Base64 gelesen werden")
    raw = base64.b64decode(payload["content"], validate=False)
    content = raw.decode("utf-8")
    # The explicit schema marker is intentionally independent of step names
    # and action versions. If a queue-enabled workflow accidentally loses its
    # upload/scanner step, it must be detected as incomplete, not as legacy.
    has_queue = QUEUE_WORKFLOW_MARKER in content
    cache[head_sha] = has_queue
    return has_queue


def _list_run_jobs(repo: str, run_id: str, attempt: int, token: str) -> list[dict]:
    query = urlencode({"per_page": 100, "page": 1})
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?{query}"
    payload = _api_json(url, token)
    total = payload.get("total_count")
    jobs = payload.get("jobs")
    if type(total) is not int or not isinstance(jobs, list):
        raise ValueError(f"Job-Abfrage fuer Run {run_id} ist unvollstaendig")
    if total > 1000:
        raise ValueError(f"Run {run_id} hat mehr als 1000 Jobs; Queue-Pruefung nicht vollstaendig")
    collected = list(jobs)
    for page in range(2, (total + 99) // 100 + 1):
        page_payload = _api_json(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}", token)
        page_jobs = page_payload.get("jobs")
        if not isinstance(page_jobs, list) or not page_jobs:
            raise ValueError(f"Job-Pagination fuer Run {run_id} unvollstaendig")
        collected.extend(page_jobs)
    if len(collected) != total:
        raise ValueError(f"Job-Pagination fuer Run {run_id}: erwartet={total}, erhalten={len(collected)}")
    return collected


def _step_conclusions(jobs: list[dict]) -> dict[str, str]:
    found = {}
    for job in jobs:
        for step in job.get("steps", []) if isinstance(job, dict) else []:
            if isinstance(step, dict) and isinstance(step.get("name"), str):
                found[step["name"]] = str(step.get("conclusion") or step.get("status") or "unknown").casefold()
    return found


def _list_run_artifacts(repo: str, run_id: str, token: str) -> list[dict]:
    collected = []
    for page in range(1, 11):
        query = urlencode({"per_page": 100, "page": page})
        payload = _api_json(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts?{query}", token)
        total = payload.get("total_count")
        artifacts = payload.get("artifacts")
        if type(total) is not int or not isinstance(artifacts, list):
            raise ValueError(f"Artifact-Abfrage fuer Run {run_id} ist unvollstaendig")
        collected.extend(artifacts)
        if len(collected) >= total:
            break
        if not artifacts:
            raise ValueError(f"Artifact-Pagination fuer Run {run_id} unvollstaendig")
    else:
        raise ValueError(f"Mehr als 1000 Artifacts fuer Run {run_id}; API-Suchlimit erreicht")
    if len(collected) != total:
        raise ValueError(f"Artifact-Pagination fuer Run {run_id}: erwartet={total}, erhalten={len(collected)}")
    return collected


def _read_queue_archive(data: bytes, artifact_name: str, run: dict, branch: str, repo: str, *, require_success: bool = True) -> dict:
    if not data or len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Queue-Artifact ist leer oder zu gross")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        matching = [info for info in files if info.filename == QUEUE_FILE.name]
        if len(files) != 1 or len(matching) != 1:
            raise ValueError("Queue-Artifact muss genau eine Pending-Queue-JSON-Datei enthalten")
        info = matching[0]
        if info.file_size <= 0 or info.file_size > MAX_QUEUE_BYTES or info.compress_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Pending-Queue-JSON ist leer oder ueberschreitet die Groessenbegrenzung")
        # Read with a hard decompressed-byte cap. Do not call testzip() first:
        # that would decompress an untrusted member before applying the cap.
        with archive.open(info, "r") as member:
            raw = member.read(MAX_QUEUE_BYTES + 1)
        if len(raw) > MAX_QUEUE_BYTES:
            raise ValueError("Pending-Queue-JSON ueberschreitet die dekomprimierte Groessenbegrenzung")
    payload = json.loads(raw.decode("utf-8"))
    expected_attempt = int(run.get("run_attempt") or 1)
    checks = {
        "schema_version": payload.get("schema_version") == SCHEMA_VERSION,
        "repository": payload.get("repository") == repo,
        "workflow_path": _workflow_path_from_run(payload.get("workflow_path", "")) == WORKFLOW_PATH,
        "workflow_run_id": str(payload.get("workflow_run_id")) == str(run.get("id")),
        "workflow_run_attempt": payload.get("workflow_run_attempt") == expected_attempt,
        "branch": payload.get("branch") == branch == run.get("head_branch"),
        "head_sha": payload.get("head_sha") == run.get("head_sha"),
        "producer_outcome": payload.get("producer_outcome") in ({"success"} if require_success else {"success", "failure"}),
        "source": payload.get("source") == "einzel_check",
        "artifact_name": artifact_name == f"{ARTIFACT_PREFIX}{run.get('id')}-{expected_attempt}",
    }
    failed = [key for key, valid in checks.items() if not valid]
    if failed:
        raise ValueError("Queue-Manifest stimmt nicht mit GitHub-Run/Artifact ueberein: " + ", ".join(failed))
    producer_step_outcome = str(payload.get("producer_step_outcome") or "").casefold()
    if payload.get("producer_outcome") == "success" and producer_step_outcome != "success":
        raise ValueError("Queue-Manifest Producer-Step-Outcome widerspricht success")
    if payload.get("producer_outcome") == "failure" and producer_step_outcome not in {"failure", "cancelled", "skipped"}:
        raise ValueError("Queue-Manifest Producer-Step-Outcome widerspricht failure")
    try:
        queue_created = datetime.fromisoformat(str(payload.get("created_at_utc") or "").replace("Z", "+00:00"))
        run_created = datetime.fromisoformat(str(run.get("created_at") or "").replace("Z", "+00:00"))
        run_updated_raw = str(run.get("updated_at") or "")
        run_updated = datetime.fromisoformat(run_updated_raw.replace("Z", "+00:00")) if run_updated_raw else None
        if queue_created.tzinfo is None or run_created.tzinfo is None:
            raise ValueError("Zeitstempel ohne UTC-Offset")
        if queue_created < run_created - timedelta(minutes=5):
            raise ValueError("Queue wurde vor dem zugehoerigen Workflow-Run erstellt")
        if run_updated and (run_updated.tzinfo is None or queue_created > run_updated + timedelta(minutes=5)):
            raise ValueError("Queue wurde nach Abschluss des zugehoerigen Workflow-Runs erstellt")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Queue-Manifest-Zeitstempel sind ungueltig: {exc}") from exc
    queue_errors = payload.get("queue_errors", [])
    if not isinstance(queue_errors, list):
        raise ValueError("Queue-Manifest enthaelt eine ungueltige queue_errors-Liste")
    recovery_tickers = []
    for item in queue_errors:
        if not isinstance(item, dict):
            raise ValueError("Queue-Manifest enthaelt einen ungueltigen Registrierungsfehler")
        recovery_ticker = _normalize_market_ticker(item.get("ticker"))
        if not recovery_ticker or recovery_ticker in EXCLUDED_TICKERS or len(recovery_ticker) > 30 or any(ch.isspace() for ch in recovery_ticker):
            raise ValueError(f"Queue-Fehler enthaelt einen ungueltigen/ausgeschlossenen Ticker: {item.get('ticker')!r}")
        recovery_tickers.append(recovery_ticker)
    payload["recovery_tickers"] = sorted(set(recovery_tickers))
    tickers = payload.get("tickers")
    if not isinstance(tickers, list):
        raise ValueError("Queue-Manifest enthaelt keine Tickerliste")
    normalized = []
    for raw_ticker in tickers:
        ticker = _normalize_market_ticker(raw_ticker)
        if not ticker or ticker in EXCLUDED_TICKERS or len(ticker) > 30 or any(ch.isspace() for ch in ticker):
            raise ValueError(f"Queue enthaelt einen ungueltigen/ausgeschlossenen Ticker: {raw_ticker!r}")
        normalized.append(ticker)
    if len(normalized) != len(set(normalized)):
        raise ValueError("Queue enthaelt doppelte Ticker")
    payload["tickers"] = sorted(normalized)
    return payload


def _ensure_receipts(db_path: Path) -> None:
    init_db(db_path)
    with sqlite3.connect(str(db_path), timeout=60) as connection:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} ("
            "run_id TEXT NOT NULL, run_attempt INTEGER NOT NULL, queue_sha256 TEXT NOT NULL, "
            "imported_at_utc TEXT NOT NULL, ticker_count INTEGER NOT NULL, "
            "producer_outcome TEXT NOT NULL DEFAULT 'success', "
            "recovered_error_count INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (run_id, run_attempt))"
        )
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({RECEIPT_TABLE})")}
        if "producer_outcome" not in columns:
            connection.execute(
                f"ALTER TABLE {RECEIPT_TABLE} ADD COLUMN producer_outcome TEXT NOT NULL DEFAULT 'success'"
            )
        if "recovered_error_count" not in columns:
            connection.execute(
                f"ALTER TABLE {RECEIPT_TABLE} ADD COLUMN recovered_error_count INTEGER NOT NULL DEFAULT 0"
            )


def _receipt_info(db_path: Path, run_id: str, attempt: int) -> dict | None:
    with sqlite3.connect(str(db_path), timeout=60) as connection:
        row = connection.execute(
            f"SELECT producer_outcome, ticker_count, recovered_error_count FROM {RECEIPT_TABLE} "
            "WHERE run_id=? AND run_attempt=?",
            (str(run_id), int(attempt)),
        ).fetchone()
    if row is None:
        return None
    return {
        "producer_outcome": str(row[0]).casefold(),
        "ticker_count": int(row[1]),
        "recovered_error_count": int(row[2]),
    }


def _receipt_outcome(db_path: Path, run_id: str, attempt: int) -> str | None:
    receipt = _receipt_info(db_path, run_id, attempt)
    return receipt["producer_outcome"] if receipt else None


def _receipt_exists(db_path: Path, run_id: str, attempt: int) -> bool:
    return _receipt_info(db_path, run_id, attempt) is not None


def _record_receipt(
    db_path: Path, run_id: str, attempt: int, raw: bytes, ticker_count: int,
    producer_outcome: str = "success", recovered_error_count: int = 0,
) -> None:
    normalized_outcome = str(producer_outcome or "failure").casefold()
    if normalized_outcome not in {"success", "failure", "cancelled", "skipped"}:
        normalized_outcome = "failure"
    with sqlite3.connect(str(db_path), timeout=60) as connection:
        connection.execute(
            f"INSERT OR IGNORE INTO {RECEIPT_TABLE} "
            "(run_id, run_attempt, queue_sha256, imported_at_utc, ticker_count, producer_outcome, recovered_error_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(run_id), int(attempt), hashlib.sha256(raw).hexdigest(), _utc_now(),
                int(ticker_count), normalized_outcome, max(0, int(recovered_error_count)),
            ),
        )


def import_pending_queues(
    *, repo: str | None = None, branch: str | None = None, token: str | None = None,
    db_path: Path = DB_FILE, universe_path: Path = UNIVERSE_FILE, lookback_days: int = 90,
    after_scanners: bool = False,
) -> dict:
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    branch = branch or os.environ.get("GITHUB_REF_NAME", "")
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    result = {
        "schema_version": 1, "status": "FAILED", "repository": repo, "branch": branch,
        "checked_at_utc": _utc_now(), "lookback_days": int(lookback_days),
        "runs_scanned": 0, "runs_eligible": 0, "runs_imported": 0,
        "runs_already_imported": 0, "tickers_seen": 0, "tickers_added": 0,
        "active_runs_scanned": 0, "active_runs_detected": 0,
        "recovered_registration_errors": 0, "after_scanners": bool(after_scanners),
        "errors": [], "imported_runs": [],
    }
    try:
        if not repo or not branch or not token:
            raise ValueError("Repository, Branch oder GitHub-Token fuer Pending-Queue fehlen")
        _ensure_receipts(db_path)
        workflow_cache: dict[str, bool] = {}
        active_run_ids = set()
        active_run_observed_ids = set()

        def scan_active_queue_runs() -> None:
            # Queued runs matter as much as in-progress runs: a producer waiting
            # for a GitHub runner has not uploaded its queue yet.
            try:
                for active_status in ("queued", "in_progress"):
                    active_runs = _list_recent_runs(
                        repo, branch, token, lookback_days, status=active_status
                    )
                    for active_run in active_runs:
                        if active_run.get("status") != active_status:
                            continue
                        observed_id = str(active_run.get("id", ""))
                        if observed_id:
                            active_run_observed_ids.add(observed_id)
                    result["active_runs_scanned"] = len(active_run_observed_ids)
                    for active_run in active_runs:
                        if active_run.get("status") != active_status:
                            continue
                        if _workflow_path_from_run(active_run.get("path", "")) != WORKFLOW_PATH or active_run.get("head_branch") != branch:
                            continue
                        active_id = str(active_run.get("id", ""))
                        if not active_id or active_id in active_run_ids:
                            continue
                        active_sha = str(active_run.get("head_sha") or "")
                        if not re.fullmatch(r"[0-9a-f]{40,64}", active_sha, re.I):
                            result["errors"].append(f"run={active_id}:ACTIVE_HEAD_SHA_INVALID")
                            active_run_ids.add(active_id)
                            continue
                        if _workflow_has_queue(repo, active_sha, token, workflow_cache):
                            active_run_ids.add(active_id)
                            result["active_runs_detected"] += 1
                            result["errors"].append(f"run={active_id}:QUEUE_PRODUCER_STILL_ACTIVE_{active_status.upper()}")
            except Exception as exc:
                result["errors"].append(f"ACTIVE_QUEUE_SCAN_{type(exc).__name__}:{exc}")

        # A producer already running when this import checkpoint begins may
        # finish later. Do not declare the queue complete while it is active.
        scan_active_queue_runs()
        runs = _list_recent_runs(repo, branch, token, lookback_days, status="completed")
        result["runs_scanned"] = len(runs)
        for run in runs:
            if _workflow_path_from_run(run.get("path", "")) != WORKFLOW_PATH:
                continue
            if run.get("head_branch") != branch or run.get("status") != "completed":
                continue
            head_sha = str(run.get("head_sha") or "")
            if not re.fullmatch(r"[0-9a-f]{40,64}", head_sha, re.I):
                result["errors"].append(f"run={run.get('id')}:HEAD_SHA_INVALID")
                continue
            try:
                if not _workflow_has_queue(repo, head_sha, token, workflow_cache):
                    continue  # Pre-migration run: no queue artifact was expected.
                result["runs_eligible"] += 1
                run_id = str(run.get("id"))
                latest_attempt = int(run.get("run_attempt") or 1)
                if latest_attempt < 1 or latest_attempt > 100:
                    result["errors"].append(f"run={run_id}:RUN_ATTEMPT_INVALID_{latest_attempt}")
                    continue
                for attempt in range(1, latest_attempt + 1):
                    stored_receipt = _receipt_info(db_path, run_id, attempt)
                    if stored_receipt is not None:
                        result["runs_already_imported"] += 1
                        result["tickers_seen"] += stored_receipt["ticker_count"]
                        result["recovered_registration_errors"] += stored_receipt["recovered_error_count"]
                        stored_outcome = stored_receipt["producer_outcome"]
                        # A failed/cancelled/skipped latest attempt remains a
                        # durable incomplete state in the SQLite bundle. Earlier
                        # failed attempts are superseded by a later completed attempt.
                        if attempt == latest_attempt and stored_outcome != "success":
                            result["errors"].append(
                                f"run={run_id}:attempt={attempt}:SCANNER_STEP_{stored_outcome.upper()}_RECEIPT"
                            )
                        continue
                    attempt_run = dict(run)
                    attempt_run["run_attempt"] = attempt
                    jobs = _list_run_jobs(repo, run_id, attempt, token)
                    steps = _step_conclusions(jobs)
                    scan_outcome = steps.get(SCANNER_STEP_NAME)
                    artifact_step_outcome = steps.get(ARTIFACT_STEP_NAME)
                    if scan_outcome is None:
                        result["errors"].append(f"run={run_id}:attempt={attempt}:SCANNER_STEP_NOT_FOUND")
                        continue
                    if scan_outcome in {"success", "failure", "cancelled", "skipped"} and artifact_step_outcome != "success":
                        result["errors"].append(f"run={run_id}:attempt={attempt}:QUEUE_ARTIFACT_UPLOAD_{str(artifact_step_outcome or 'missing').upper()}")
                        continue
                    if artifact_step_outcome != "success":
                        result["errors"].append(f"run={run_id}:attempt={attempt}:QUEUE_ARTIFACT_UPLOAD_NOT_SUCCESS")
                        continue
                    artifacts = _list_run_artifacts(repo, run_id, token)
                    expected_name = f"{ARTIFACT_PREFIX}{run_id}-{attempt}"
                    matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == expected_name]
                    if len(matches) != 1:
                        result["errors"].append(f"run={run_id}:attempt={attempt}:QUEUE_ARTIFACT_COUNT_{len(matches)}")
                        continue
                    artifact = matches[0]
                    if artifact.get("expired") is not False:
                        result["errors"].append(f"run={run_id}:attempt={attempt}:QUEUE_ARTIFACT_EXPIRED_OR_UNKNOWN")
                        continue
                    download_url = artifact.get("archive_download_url")
                    parsed_download_url = urlsplit(download_url) if isinstance(download_url, str) else None
                    expected_artifact_path_prefix = f"/repos/{repo}/actions/artifacts/"
                    if (
                        parsed_download_url is None
                        or parsed_download_url.scheme != "https"
                        or parsed_download_url.hostname != "api.github.com"
                        or not parsed_download_url.path.startswith(expected_artifact_path_prefix)
                        or not parsed_download_url.path.endswith("/zip")
                    ):
                        result["errors"].append(f"run={run_id}:attempt={attempt}:QUEUE_ARTIFACT_URL_INVALID")
                        continue
                    try:
                        archive_bytes = _download_artifact(download_url, token)
                        payload = _read_queue_archive(
                            archive_bytes, expected_name, attempt_run, branch, repo,
                            require_success=(scan_outcome == "success"),
                        )
                        expected_producer_outcome = "success" if scan_outcome == "success" else "failure"
                        if payload.get("producer_outcome") != expected_producer_outcome:
                            raise ValueError("Producer-Outcome widerspricht dem Scanner-Step")
                        tickers = sorted(set(payload["tickers"]) | set(payload.get("recovery_tickers", [])))
                        added = 0
                        for ticker in tickers:
                            if register_ticker(ticker, universe_path):
                                added += 1
                        # Receipt is written only after every ticker was accepted.
                        # Persist the producer outcome so a failed latest attempt
                        # cannot look COMPLETE on the next main run merely because
                        # its partial ticker payload was already imported.
                        _record_receipt(
                            db_path, run_id, attempt, archive_bytes, len(tickers),
                            producer_outcome=scan_outcome,
                            recovered_error_count=len(payload.get("queue_errors", [])),
                        )
                        if attempt == latest_attempt and scan_outcome != "success":
                            result["errors"].append(
                                f"run={run_id}:attempt={attempt}:SCANNER_STEP_{scan_outcome.upper()}_PARTIAL_QUEUE_IMPORTED"
                            )
                        result["runs_imported"] += 1
                        result["tickers_seen"] += len(tickers)
                        result["tickers_added"] += added
                        result["recovered_registration_errors"] = result.get("recovered_registration_errors", 0) + len(payload.get("queue_errors", []))
                        result["imported_runs"].append({"run_id": run_id, "run_attempt": attempt, "ticker_count": len(tickers), "tickers_added": added, "recovered_registration_errors": len(payload.get("queue_errors", []))})
                    except Exception as exc:
                        result["errors"].append(f"run={run_id}:attempt={attempt}:QUEUE_IMPORT_{type(exc).__name__}:{exc}")
                # A successful latest attempt supersedes transient failures of
                # earlier attempts, including attempts whose artifact was never
                # uploaded and therefore have no SQLite receipt to deduplicate.
                latest_receipt = _receipt_info(db_path, run_id, latest_attempt)
                if latest_receipt is not None and latest_receipt["producer_outcome"] == "success":
                    prefix = f"run={run_id}:attempt="
                    superseded = []
                    retained = []
                    for error in result["errors"]:
                        match = re.match(re.escape(prefix) + r"(\d+):(.*)", error)
                        if match and int(match.group(1)) < latest_attempt and (
                            match.group(2) == "SCANNER_STEP_NOT_FOUND"
                            or match.group(2) == "QUEUE_ARTIFACT_COUNT_0"
                            or match.group(2).startswith("QUEUE_ARTIFACT_UPLOAD_")
                        ):
                            superseded.append(error)
                        else:
                            retained.append(error)
                    result["errors"] = retained
                    if superseded:
                        result.setdefault("superseded_attempt_errors", []).extend(superseded)
            except Exception as exc:
                result["errors"].append(f"run={run.get('id')}:QUEUE_RUN_CHECK_{type(exc).__name__}:{exc}")
        scan_active_queue_runs()
        if after_scanners and result["tickers_added"] > 0:
            result["errors"].append(f"LATE_QUEUE_TICKERS_ADDED_AFTER_SCANNERS={result['tickers_added']}")
        result["status"] = "COMPLETE" if not result["errors"] else ("PARTIAL" if result["runs_imported"] or result["runs_already_imported"] else "FAILED")
    except Exception as exc:
        result["errors"].append(f"QUEUE_SCAN_{type(exc).__name__}:{exc}")
        result["status"] = "FAILED"
    _atomic_json(STATUS_FILE, result)
    print(
        "OHLCV_PENDING_QUEUE_IMPORT="
        f"{result['status']}|scanned={result['runs_scanned']}|eligible={result['runs_eligible']}|"
        f"imported={result['runs_imported']}|already={result['runs_already_imported']}|"
        f"tickers_added={result['tickers_added']}|active_runs={result['active_runs_detected']}|"
        f"recovered_queue_errors={result['recovered_registration_errors']}|errors={len(result['errors'])}"
    )
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"status={result['status']}\n")
            handle.write(f"runs_scanned={result['runs_scanned']}\n")
            handle.write(f"runs_imported={result['runs_imported']}\n")
            handle.write(f"tickers_added={result['tickers_added']}\n")
            handle.write(f"active_runs_detected={result['active_runs_detected']}\n")
            handle.write(f"errors={len(result['errors'])}\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "finalize", "import"))
    parser.add_argument("--outcome", default="")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--after-scanners", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        init_queue()
        return 0
    if args.command == "finalize":
        finalize_queue(args.outcome)
        return 0
    import_pending_queues(lookback_days=args.lookback_days, after_scanners=args.after_scanners)
    # Import failures are an explicit NICHT_FINAL gate, not a reason to suppress
    # the diagnostic report for otherwise usable independent market data.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
