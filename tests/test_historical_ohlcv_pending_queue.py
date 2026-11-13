from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import zipfile
from unittest.mock import patch

import pytest

import historical_ohlcv_pending_queue as queue


def _run(run_id="12345", attempt=1, branch="main", sha="a" * 40):
    return {
        "id": int(run_id), "run_attempt": attempt, "head_branch": branch,
        "head_sha": sha, "path": ".github/workflows/einzel_check.yml@refs/heads/main",
        "status": "completed", "conclusion": "failure", "created_at": "2026-11-13T10:00:00Z",
        "updated_at": "2026-11-13T10:10:00Z",
    }


def _archive(payload):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("historical_ohlcv_pending_queue.json", json.dumps(payload))
    return output.getvalue()


def _payload(run=None, tickers=None, producer_outcome="success"):
    run = run or _run()
    return {
        "schema_version": 1,
        "repository": "joergneuber/Sektor-Analyse",
        "workflow_path": ".github/workflows/einzel_check.yml",
        "workflow_run_id": str(run["id"]),
        "workflow_run_attempt": run["run_attempt"],
        "branch": run["head_branch"],
        "head_sha": run["head_sha"],
        "created_at_utc": "2026-11-13T10:00:00+00:00",
        "source": "einzel_check",
        "producer_outcome": producer_outcome,
        "producer_step_outcome": "success" if producer_outcome == "success" else "failure",
        "queue_errors": [],
        "tickers": list(tickers if tickers is not None else ["AAPL"]),
    }


def test_init_register_and_finalize_are_atomic_and_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "joergneuber/Sektor-Analyse/.github/workflows/einzel_check.yml@refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_REPOSITORY", "joergneuber/Sektor-Analyse")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    path = tmp_path / "queue.json"

    created = queue.init_queue(path)
    assert created["workflow_path"] == ".github/workflows/einzel_check.yml"
    assert created["workflow_run_attempt"] == 2
    assert created["tickers"] == []
    assert queue.register_pending_ticker("aapl", path) is True
    assert queue.register_pending_ticker("AAPL", path) is False
    assert queue.register_pending_ticker("ABB", path) is False
    finalized = queue.finalize_queue("success", path)
    assert finalized["producer_outcome"] == "success"
    assert finalized["tickers"] == ["AAPL"]
    with pytest.raises(RuntimeError, match="finalisiert"):
        queue.register_pending_ticker("MSFT", path)


def test_finalize_queue_uses_step_output_as_independent_registration_failure_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "joergneuber/Sektor-Analyse/.github/workflows/einzel_check.yml@refs/heads/main")
    monkeypatch.setenv("NMM_PENDING_QUEUE_REGISTRATION_FAILED", "true")
    monkeypatch.setenv("NMM_PENDING_QUEUE_ERROR_TICKER", "MSFT")
    path = tmp_path / "queue.json"
    queue.init_queue(path)
    finalized = queue.finalize_queue("success", path)
    assert finalized["producer_outcome"] == "success"
    assert finalized["queue_errors"][0]["ticker"] == "MSFT"


def test_local_run_preserves_direct_universe_registration_without_queue_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    path = tmp_path / "queue.json"
    registered = []
    monkeypatch.setattr(queue, "register_ticker", lambda ticker: registered.append(ticker) or True)
    assert queue.register_pending_ticker("AAPL", path) is True
    assert registered == ["AAPL"]
    assert not path.exists()



def test_main_workflow_registers_observation_ticker_directly_not_into_unconsumed_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "67890")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "joergneuber/Sektor-Analyse/.github/workflows/main.yml@refs/heads/main")
    registered = []
    path = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "register_ticker", lambda ticker: registered.append(ticker) or True)

    assert queue.register_pending_ticker("AAPL", path) is True
    assert registered == ["AAPL"]
    assert not path.exists()


def test_only_einzel_check_workflow_ref_enables_pending_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "67890")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "joergneuber/Sektor-Analyse/.github/workflows/einzel_check.yml@refs/heads/main")
    registered = []
    path = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "register_ticker", lambda ticker: registered.append(ticker) or True)

    assert queue.register_pending_ticker("AAPL", path) is True
    assert registered == []
    assert json.loads(path.read_text(encoding="utf-8"))["tickers"] == ["AAPL"]

def test_archive_manifest_must_match_exact_run_branch_attempt_and_success():
    run = _run(attempt=2)
    payload = _payload(run, ["AAPL", "MSFT"])
    decoded = queue._read_queue_archive(
        _archive(payload), "historical-ohlcv-pending-12345-2", run,
        "main", "joergneuber/Sektor-Analyse",
    )
    assert decoded["tickers"] == ["AAPL", "MSFT"]

    for field, value in (("branch", "other"), ("head_sha", "b" * 40), ("producer_outcome", "failure"), ("workflow_run_id", "999")):
        bad = dict(payload)
        bad[field] = value
        with pytest.raises(ValueError, match="Manifest"):
            queue._read_queue_archive(
                _archive(bad), "historical-ohlcv-pending-12345-2", run,
                "main", "joergneuber/Sektor-Analyse",
            )


def test_archive_rejects_duplicate_invalid_and_excluded_tickers():
    run = _run()
    for tickers in (["AAPL", "AAPL"], ["AAPL", "ABB"], ["AAPL", "bad ticker"]):
        with pytest.raises(ValueError):
            queue._read_queue_archive(
                _archive(_payload(run, tickers)), "historical-ohlcv-pending-12345-1",
                run, "main", "joergneuber/Sektor-Analyse",
            )


def test_archive_exposes_registration_errors_as_recoverable_tickers():
    run = _run()
    payload = _payload(run, ["AAPL"])
    payload["queue_errors"] = [{"ticker": "MSFT", "error_type": "OSError", "message": "write failed"}]
    decoded = queue._read_queue_archive(
        _archive(payload), "historical-ohlcv-pending-12345-1", run,
        "main", "joergneuber/Sektor-Analyse",
    )
    assert decoded["tickers"] == ["AAPL"]
    assert decoded["recovery_tickers"] == ["MSFT"]


def test_import_merges_queue_once_and_persists_receipt_in_sqlite(tmp_path, monkeypatch):
    run = _run()
    archive_bytes = _archive(_payload(run, ["AAPL", "MSFT"]))
    db = tmp_path / "historical_ohlcv.sqlite"
    universe = tmp_path / "historical_ohlcv_universe.json"
    universe.write_text(json.dumps({"version": 1, "tickers": ["BASE.DE"]}), encoding="utf-8")
    status_path = tmp_path / "status.json"
    monkeypatch.setattr(queue, "STATUS_FILE", status_path)

    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", return_value=[{"steps": [
             {"name": queue.SCANNER_STEP_NAME, "conclusion": "success"},
             {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
         ]}]), \
         patch.object(queue, "_list_run_artifacts", return_value=[{
             "name": "historical-ohlcv-pending-12345-1", "expired": False,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/1/zip",
         }]), \
         patch.object(queue, "_download_artifact", return_value=archive_bytes):
        first = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )
        second = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )

    assert first["status"] == "COMPLETE"
    assert first["runs_imported"] == 1
    assert first["tickers_added"] == 2
    assert second["status"] == "COMPLETE"
    assert second["runs_already_imported"] == 1
    assert second["runs_imported"] == 0
    assert second["tickers_seen"] == 2
    assert json.loads(universe.read_text(encoding="utf-8"))["tickers"] == ["AAPL", "BASE.DE", "MSFT"]
    with sqlite3.connect(db) as connection:
        receipt_count = connection.execute(f"SELECT COUNT(*) FROM {queue.RECEIPT_TABLE}").fetchone()[0]
    assert receipt_count == 1


def test_import_marks_missing_artifact_as_failed_not_empty_queue(tmp_path, monkeypatch):
    run = _run()
    db = tmp_path / "historical_ohlcv.sqlite"
    universe = tmp_path / "historical_ohlcv_universe.json"
    universe.write_text(json.dumps({"version": 1, "tickers": ["BASE.DE"]}), encoding="utf-8")
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")
    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", return_value=[{"steps": [
             {"name": queue.SCANNER_STEP_NAME, "conclusion": "success"},
             {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
         ]}]), \
         patch.object(queue, "_list_run_artifacts", return_value=[]):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )
    assert result["status"] == "FAILED"
    assert any("QUEUE_ARTIFACT_COUNT_0" in error for error in result["errors"])


def test_failed_scanner_with_skipped_artifact_is_explicitly_incomplete(tmp_path, monkeypatch):
    run = _run()
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")
    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", return_value=[{"steps": [
             {"name": queue.SCANNER_STEP_NAME, "conclusion": "failure"},
             {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "skipped"},
         ]}]), \
         patch.object(queue, "_list_run_artifacts") as artifacts:
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=tmp_path / "historical_ohlcv.sqlite",
            universe_path=tmp_path / "historical_ohlcv_universe.json",
        )
    assert result["status"] == "FAILED"
    assert any("QUEUE_ARTIFACT_UPLOAD_SKIPPED" in error for error in result["errors"])
    artifacts.assert_not_called()


def test_queue_enabled_detection_skips_pre_migration_runs():
    with patch.object(queue, "_api_json", return_value={"encoding": "base64", "content": "bmFtZTogb2xk"}):
        assert queue._workflow_has_queue("owner/repo", "a" * 40, "token", {}) is False
    marker = queue.QUEUE_WORKFLOW_MARKER.encode("utf-8")
    import base64
    with patch.object(queue, "_api_json", return_value={"encoding": "base64", "content": base64.b64encode(marker).decode("ascii")}):
        assert queue._workflow_has_queue("owner/repo", "b" * 40, "token", {}) is True


def test_workflow_wires_single_writer_queue_and_graded_failure_semantics():
    root = Path(__file__).resolve().parents[1]
    single = (root / ".github/workflows/einzel_check.yml").read_text(encoding="utf-8")
    main = (root / ".github/workflows/main.yml").read_text(encoding="utf-8")
    radar = (root / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert "contents: write" not in single
    assert "if: github.ref == 'refs/heads/main'" in single
    trendwende = (root / ".github/workflows/trendwende_backtest.yml").read_text(encoding="utf-8")
    assert "contents: read" in trendwende
    assert "contents: write" not in trendwende
    assert "gh release upload" not in single
    assert "actions/upload-artifact@v6" in single
    assert "Pending-Queue als Run-Artifact sichern" in single
    assert queue.QUEUE_WORKFLOW_MARKER in single
    assert main.index("OHLCV Pending-Queues aus Einzel-Checks importieren") < main.index("Historische OHLCV-Datenbank aktualisieren")
    assert "if: github.ref == 'refs/heads/main'" in main
    assert main.count('gh release upload historical-ohlcv "$bundle_name"') == 1
    assert "if: always()" in main[main.index("Finale OHLCV-Synchronisierung, Coverage und Positionsprüfung"):main.index("Finale OHLCV-Datenbank und Universum atomar")]
    assert "OHLCV_QUEUE_IMPORT" in main
    assert "tests/test_historical_ohlcv_pending_queue.py" in main
    assert "        env:\n        run:" not in main
    final_queue = main.index("- name: OHLCV Pending-Queues nach Scannerläufen erneut importieren")
    final_sync = main.index("- name: Finale OHLCV-Synchronisierung, Coverage und Positionsprüfung")
    final_queue_section = main[final_queue:final_sync]
    assert "NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE: historical_ohlcv_universe.json" in final_queue_section
    assert "NMM_HISTORICAL_OHLCV_UNIVERSE:" not in final_queue_section
    radar_step_index = main.index("- name: Trading-Radar erstellen")
    trendwende_scan = main.index("- name: Trendwende-Scan ausführen")
    assert trendwende_scan < final_queue < final_sync < radar_step_index
    assert "--after-scanners" in main
    assert "OHLCV_QUEUE_IMPORT: ${{ steps.ohlcv-pending-queue-final.outputs.status" in main
    assert 'queue_gate_status = "PASS" if queue_import_status == "COMPLETE" else "FAIL"' in radar


def test_failed_scan_with_queue_registration_error_is_not_treated_as_no_pending_tickers(tmp_path, monkeypatch):
    run = _run()
    payload = _payload(run, ["AAPL"], producer_outcome="failure")
    payload["queue_errors"] = [{"ticker": "MSFT", "error_type": "OSError", "message": "append failed"}]
    archive_bytes = _archive(payload)
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")
    db = tmp_path / "historical_ohlcv.sqlite"
    universe = tmp_path / "historical_ohlcv_universe.json"
    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", return_value=[{"steps": [
             {"name": queue.SCANNER_STEP_NAME, "conclusion": "failure"},
             {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
         ]}]), \
         patch.object(queue, "_list_run_artifacts", return_value=[{
             "name": "historical-ohlcv-pending-12345-1", "expired": False,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/1/zip",
         }]), \
         patch.object(queue, "_download_artifact", return_value=archive_bytes) as download:
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )
        repeated = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )
    assert result["status"] == "PARTIAL"
    assert result["runs_imported"] == 1
    assert result["recovered_registration_errors"] == 1
    assert repeated["status"] == "PARTIAL"
    assert repeated["runs_already_imported"] == 1
    assert repeated["tickers_seen"] == 2
    assert repeated["recovered_registration_errors"] == 1
    assert download.call_count == 1
    with sqlite3.connect(db) as connection:
        stored_outcome = connection.execute(
            f"SELECT producer_outcome FROM {queue.RECEIPT_TABLE} WHERE run_id='12345' AND run_attempt=1"
        ).fetchone()[0]
    assert stored_outcome == "failure"
    assert json.loads(universe.read_text(encoding="utf-8"))["tickers"] == ["AAPL", "MSFT"]


def test_import_processes_each_successful_run_attempt_independently(tmp_path, monkeypatch):
    run = _run(attempt=2)
    payload1 = _payload(run, ["AAPL"])
    payload1["workflow_run_attempt"] = 1
    payload2 = _payload(run, ["MSFT"])
    payload2["workflow_run_attempt"] = 2
    archive1, archive2 = _archive(payload1), _archive(payload2)
    db = tmp_path / "historical_ohlcv.sqlite"
    universe = tmp_path / "historical_ohlcv_universe.json"
    universe.write_text(json.dumps({"version": 1, "tickers": ["BASE.DE"]}), encoding="utf-8")
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")

    def jobs(_repo, _run_id, attempt, _token):
        return [{"steps": [
            {"name": queue.SCANNER_STEP_NAME, "conclusion": "success"},
            {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
        ]}]

    def artifacts(_repo, _run_id, _token):
        return [
            {"name": "historical-ohlcv-pending-12345-1", "expired": False,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/1/zip"},
            {"name": "historical-ohlcv-pending-12345-2", "expired": False,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/2/zip"},
        ]

    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", side_effect=jobs), \
         patch.object(queue, "_list_run_artifacts", side_effect=artifacts), \
         patch.object(queue, "_download_artifact", side_effect=[archive1, archive2]):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )
    assert result["status"] == "COMPLETE"
    assert result["runs_imported"] == 2
    assert json.loads(universe.read_text(encoding="utf-8"))["tickers"] == ["AAPL", "BASE.DE", "MSFT"]


def test_latest_successful_attempt_supersedes_earlier_failed_attempt(tmp_path, monkeypatch):
    run = _run(attempt=2)
    payload1 = _payload(run, ["AAPL"], producer_outcome="failure")
    payload1["workflow_run_attempt"] = 1
    payload2 = _payload(run, ["MSFT"], producer_outcome="success")
    payload2["workflow_run_attempt"] = 2
    archive1, archive2 = _archive(payload1), _archive(payload2)
    db = tmp_path / "historical_ohlcv.sqlite"
    universe = tmp_path / "historical_ohlcv_universe.json"
    universe.write_text(json.dumps({"version": 1, "tickers": []}), encoding="utf-8")
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")

    def jobs(_repo, _run_id, attempt, _token):
        scan = "failure" if attempt == 1 else "success"
        return [{"steps": [
            {"name": queue.SCANNER_STEP_NAME, "conclusion": scan},
            {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
        ]}]

    def artifacts(_repo, _run_id, _token):
        return [
            {"name": "historical-ohlcv-pending-12345-1", "expired": False,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/1/zip"},
            {"name": "historical-ohlcv-pending-12345-2", "expired": False,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/2/zip"},
        ]

    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", side_effect=jobs), \
         patch.object(queue, "_list_run_artifacts", side_effect=artifacts), \
         patch.object(queue, "_download_artifact", side_effect=[archive1, archive2]):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )
        repeated = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )
    assert result["status"] == "COMPLETE"
    assert result["runs_imported"] == 2
    assert repeated["status"] == "COMPLETE"
    assert repeated["runs_already_imported"] == 2
    assert json.loads(universe.read_text(encoding="utf-8"))["tickers"] == ["AAPL", "MSFT"]



def test_later_successful_attempt_supersedes_earlier_missing_artifact(tmp_path, monkeypatch):
    run = _run(attempt=2)
    payload2 = _payload(run, ["MSFT"], producer_outcome="success")
    payload2["workflow_run_attempt"] = 2
    archive2 = _archive(payload2)
    db = tmp_path / "historical_ohlcv.sqlite"
    universe = tmp_path / "historical_ohlcv_universe.json"
    universe.write_text(json.dumps({"version": 1, "tickers": []}), encoding="utf-8")
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")

    def jobs(_repo, _run_id, attempt, _token):
        return [{"steps": [
            {"name": queue.SCANNER_STEP_NAME, "conclusion": "success"},
            {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
        ]}]

    def artifacts(_repo, _run_id, _token):
        return [{
            "name": "historical-ohlcv-pending-12345-2", "expired": False,
            "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/2/zip",
        }]

    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", side_effect=jobs), \
         patch.object(queue, "_list_run_artifacts", side_effect=artifacts), \
         patch.object(queue, "_download_artifact", return_value=archive2):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=db, universe_path=universe,
        )

    assert result["status"] == "COMPLETE"
    assert result["errors"] == []
    assert any("attempt=1:QUEUE_ARTIFACT_COUNT_0" in item for item in result["superseded_attempt_errors"])
    assert json.loads(universe.read_text(encoding="utf-8"))["tickers"] == ["MSFT"]

def test_receipts_survive_authoritative_release_bundle_round_trip(tmp_path):
    from ohlcv_release_bundle import create_bundle, extract_bundle

    db = tmp_path / "historical_ohlcv.sqlite"
    universe = tmp_path / "historical_ohlcv_universe.json"
    universe.write_text(json.dumps({"version": 1, "tickers": ["AAPL"]}), encoding="utf-8")
    queue._ensure_receipts(db)
    queue._record_receipt(db, "12345", 1, b"queue-archive", 1, producer_outcome="success")
    bundle = tmp_path / "historical_ohlcv_bundle.zip"
    create_bundle(db, universe, bundle)

    restored_db = tmp_path / "restored.sqlite"
    restored_universe = tmp_path / "restored_universe.json"
    extract_bundle(bundle, restored_db, restored_universe)
    with sqlite3.connect(restored_db) as connection:
        row = connection.execute(
            f"SELECT run_id, run_attempt, producer_outcome FROM {queue.RECEIPT_TABLE}"
        ).fetchone()
    assert row == ("12345", 1, "success")


def test_receipt_schema_migrates_existing_receipt_table(tmp_path):
    db = tmp_path / "historical_ohlcv.sqlite"
    queue.init_db(db) if hasattr(queue, "init_db") else None
    with sqlite3.connect(db) as connection:
        connection.execute(
            f"CREATE TABLE {queue.RECEIPT_TABLE} ("
            "run_id TEXT NOT NULL, run_attempt INTEGER NOT NULL, queue_sha256 TEXT NOT NULL, "
            "imported_at_utc TEXT NOT NULL, ticker_count INTEGER NOT NULL, "
            "PRIMARY KEY (run_id, run_attempt))"
        )
    queue._ensure_receipts(db)
    with sqlite3.connect(db) as connection:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({queue.RECEIPT_TABLE})")}
    assert {"producer_outcome", "recovered_error_count"}.issubset(columns)


def test_workflow_run_pagination_reads_every_page():
    from datetime import date
    from urllib.parse import parse_qs, urlsplit

    pages = [
        {"total_count": 201, "workflow_runs": [{"id": i} for i in range(100)]},
        {"workflow_runs": [{"id": i} for i in range(100, 200)]},
        {"workflow_runs": [{"id": 200}]},
    ]
    with patch.object(queue, "_api_json", side_effect=pages) as api:
        runs = queue._list_run_window(
            "joergneuber/Sektor-Analyse", "main", "token", date(2026, 11, 1),
            date(2026, 11, 30), status="completed",
        )
    assert len(runs) == 201
    assert [parse_qs(urlsplit(call.args[0]).query)["page"][0] for call in api.call_args_list] == ["1", "2", "3"]


def test_artifact_and_job_pagination_read_every_page():
    from urllib.parse import parse_qs, urlsplit

    artifact_pages = [
        {"total_count": 101, "artifacts": [{"id": i} for i in range(100)]},
        {"total_count": 101, "artifacts": [{"id": 100}]},
    ]
    with patch.object(queue, "_api_json", side_effect=artifact_pages) as api:
        artifacts = queue._list_run_artifacts("joergneuber/Sektor-Analyse", "12345", "token")
    assert len(artifacts) == 101
    assert [parse_qs(urlsplit(call.args[0]).query)["page"][0] for call in api.call_args_list] == ["1", "2"]

    job_pages = [
        {"total_count": 101, "jobs": [{"id": i} for i in range(100)]},
        {"total_count": 101, "jobs": [{"id": 100}]},
    ]
    with patch.object(queue, "_api_json", side_effect=job_pages) as api:
        jobs = queue._list_run_jobs("joergneuber/Sektor-Analyse", "12345", 1, "token")
    assert len(jobs) == 101
    assert [parse_qs(urlsplit(call.args[0]).query)["page"][0] for call in api.call_args_list] == ["1", "2"]


def test_expired_queue_artifact_is_not_treated_as_empty(tmp_path, monkeypatch):
    run = _run()
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")
    with patch.object(queue, "_list_recent_runs", return_value=[run]), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", return_value=[{"steps": [
             {"name": queue.SCANNER_STEP_NAME, "conclusion": "success"},
             {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
         ]}]), \
         patch.object(queue, "_list_run_artifacts", return_value=[{
             "name": "historical-ohlcv-pending-12345-1", "expired": True,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/1/zip",
         }]):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=tmp_path / "historical_ohlcv.sqlite",
            universe_path=tmp_path / "historical_ohlcv_universe.json",
        )
    assert result["status"] == "FAILED"
    assert any("QUEUE_ARTIFACT_EXPIRED_OR_UNKNOWN" in error for error in result["errors"])


def test_final_sync_workflow_shell_emits_complete_partial_and_failed(tmp_path):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/main.yml").read_text(encoding="utf-8")
    start = workflow.index("      - name: Finale OHLCV-Synchronisierung, Coverage und Positionsprüfung\n")
    end = workflow.index("      - name: Finale OHLCV-Datenbank und Universum atomar", start)
    section = workflow[start:end]
    lines = section.splitlines()
    run_index = lines.index("        run: |")
    shell_lines = []
    for line in lines[run_index + 1:]:
        if line and not line.startswith("          "):
            break
        shell_lines.append(line[10:] if line.startswith("          ") else "")
    script = "\n".join(shell_lines)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text(r'''#!/usr/bin/env bash
set -u
case "$*" in
  *" update"*) if [ "${CREATE_FILES:-0}" -eq 1 ]; then printf x > historical_ohlcv.sqlite; printf x > historical_ohlcv_universe.json; fi; exit "${UPDATE_RC:-0}" ;;
  *" coverage "*) exit "${COVERAGE_RC:-0}" ;;
  *" verify-open-positions "*) exit "${POSITIONS_RC:-0}" ;;
esac
exit 99
''', encoding="utf-8")
    fake_python.chmod(0o755)

    def run_case(name, **values):
        work = tmp_path / name
        work.mkdir()
        output = work / "github_output"
        env = os.environ.copy()
        env.update({"PATH": f"{bin_dir}:{env['PATH']}", "GITHUB_OUTPUT": str(output)})
        env.update({key: str(value) for key, value in values.items()})
        completed = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=work, env=env, text=True, capture_output=True)
        assert completed.returncode == 0, completed.stderr
        return output.read_text(encoding="utf-8")

    assert "status=complete" in run_case("complete", CREATE_FILES=1)
    assert "status=partial" in run_case("partial", CREATE_FILES=1, UPDATE_RC=1)
    assert "status=failed" in run_case("failed", UPDATE_RC=1)


def test_archive_rejects_extra_files_path_traversal_and_oversized_members():
    run = _run()
    payload = json.dumps(_payload(run)).encode("utf-8")
    cases = []
    extra = io.BytesIO()
    with zipfile.ZipFile(extra, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("historical_ohlcv_pending_queue.json", payload)
        zf.writestr("unexpected.txt", b"extra")
    cases.append(extra.getvalue())
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("../historical_ohlcv_pending_queue.json", payload)
    cases.append(traversal.getvalue())
    oversized = io.BytesIO()
    with zipfile.ZipFile(oversized, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("historical_ohlcv_pending_queue.json", b"x" * (queue.MAX_QUEUE_BYTES + 1))
    cases.append(oversized.getvalue())
    for data in cases:
        with pytest.raises(ValueError):
            queue._read_queue_archive(
                data, "historical-ohlcv-pending-12345-1", run,
                "main", "joergneuber/Sektor-Analyse",
            )


def test_active_queue_producer_blocks_complete_status(tmp_path, monkeypatch):
    active = _run()
    active["status"] = "in_progress"
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")

    def runs(_repo, _branch, _token, _lookback, status="completed"):
        return [active] if status == "in_progress" else []

    with patch.object(queue, "_list_recent_runs", side_effect=runs), \
         patch.object(queue, "_workflow_has_queue", return_value=True):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=tmp_path / "historical_ohlcv.sqlite",
            universe_path=tmp_path / "historical_ohlcv_universe.json",
        )
    assert result["status"] == "FAILED"
    assert result["active_runs_detected"] == 1
    assert any("QUEUE_PRODUCER_STILL_ACTIVE" in error for error in result["errors"])


def test_new_tickers_imported_after_scanners_force_partial_status(tmp_path, monkeypatch):
    run = _run()
    payload = _payload(run, ["AAPL"])
    archive_bytes = _archive(payload)
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")

    def runs(_repo, _branch, _token, _lookback, status="completed"):
        return [] if status == "in_progress" else [run]

    with patch.object(queue, "_list_recent_runs", side_effect=runs), \
         patch.object(queue, "_workflow_has_queue", return_value=True), \
         patch.object(queue, "_list_run_jobs", return_value=[{"steps": [
             {"name": queue.SCANNER_STEP_NAME, "conclusion": "success"},
             {"name": queue.ARTIFACT_STEP_NAME, "conclusion": "success"},
         ]}]), \
         patch.object(queue, "_list_run_artifacts", return_value=[{
             "name": "historical-ohlcv-pending-12345-1", "expired": False,
             "archive_download_url": "https://api.github.com/repos/joergneuber/Sektor-Analyse/actions/artifacts/1/zip",
         }]), \
         patch.object(queue, "_download_artifact", return_value=archive_bytes):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=tmp_path / "historical_ohlcv.sqlite",
            universe_path=tmp_path / "historical_ohlcv_universe.json",
            after_scanners=True,
        )
    assert result["status"] == "PARTIAL"
    assert result["tickers_added"] == 1
    assert any("LATE_QUEUE_TICKERS_ADDED_AFTER_SCANNERS=1" in error for error in result["errors"])


def test_cli_accepts_after_scanners_phase(monkeypatch):
    import sys
    captured = {}
    monkeypatch.setattr(sys, "argv", ["historical_ohlcv_pending_queue.py", "import", "--lookback-days", "90", "--after-scanners"])
    monkeypatch.setattr(queue, "import_pending_queues", lambda **kwargs: captured.update(kwargs) or {"status": "COMPLETE"})
    assert queue.main() == 0
    assert captured["lookback_days"] == 90
    assert captured["after_scanners"] is True


@pytest.mark.parametrize("active_status", ["queued", "in_progress"])
def test_active_run_api_query_uses_all_nonterminal_status_filters(active_status):
    from datetime import date
    from urllib.parse import parse_qs, urlsplit

    with patch.object(queue, "_api_json", return_value={"total_count": 0, "workflow_runs": []}) as api:
        assert queue._list_run_window(
            "joergneuber/Sektor-Analyse", "main", "token", date(2026, 11, 13),
            date(2026, 11, 13), status=active_status,
        ) == []
    query = parse_qs(urlsplit(api.call_args.args[0]).query)
    assert query["status"] == [active_status]


def test_queued_queue_producer_blocks_complete_status(tmp_path, monkeypatch):
    queued = _run()
    queued["status"] = "queued"
    monkeypatch.setattr(queue, "STATUS_FILE", tmp_path / "status.json")

    def runs(_repo, _branch, _token, _lookback, status="completed"):
        return [queued] if status == "queued" else []

    with patch.object(queue, "_list_recent_runs", side_effect=runs), \
         patch.object(queue, "_workflow_has_queue", return_value=True):
        result = queue.import_pending_queues(
            repo="joergneuber/Sektor-Analyse", branch="main", token="test-token",
            db_path=tmp_path / "historical_ohlcv.sqlite",
            universe_path=tmp_path / "historical_ohlcv_universe.json",
        )
    assert result["status"] == "FAILED"
    assert result["active_runs_detected"] == 1
    assert any("QUEUE_PRODUCER_STILL_ACTIVE_QUEUED" in error for error in result["errors"])
