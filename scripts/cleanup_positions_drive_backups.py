#!/usr/bin/env python3
"""Move expired, application-marked positions-master backups to Drive trash."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from positions_drive_backups import cleanup_expired_drive_backups

FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"
PRODUCTION_REPOSITORY = "joergneuber/Sektor-Analyse"
ALLOWED_EVENTS = {"schedule", "workflow_dispatch"}
REPORT_NAME = "positions_drive_backup_cleanup_report.json"


def assert_production_main_environment(environ=None) -> None:
    """Fail closed unless invoked by an allowed event on the production main ref."""
    env = os.environ if environ is None else environ
    required = {
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_NAME": "main",
        "GITHUB_REPOSITORY": PRODUCTION_REPOSITORY,
    }
    mismatches = [
        f"{key}={env.get(key)!r} (erwartet {expected!r})"
        for key, expected in required.items()
        if env.get(key) != expected
    ]
    event = env.get("GITHUB_EVENT_NAME")
    if event not in ALLOWED_EVENTS:
        mismatches.append(f"GITHUB_EVENT_NAME={event!r} (erlaubt: {', '.join(sorted(ALLOWED_EVENTS))})")
    if mismatches:
        raise RuntimeError(
            "Sicherheitsabbruch: Drive-Backup-Cleanup ist ausschließlich auf "
            "joergneuber/Sektor-Analyse refs/heads/main für schedule/workflow_dispatch "
            "freigegeben; " + "; ".join(mismatches)
        )


def _build_drive_service():
    token = os.environ.get("GDRIVE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GDRIVE_TOKEN fehlt; Backup-Bereinigung wird fail-closed abgebrochen.")
    try:
        token_info = json.loads(token)
    except Exception as exc:
        raise RuntimeError("GDRIVE_TOKEN ist kein gültiges JSON.") from exc
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except Exception as exc:
        raise RuntimeError("Google-Drive-Bibliotheken fehlen; Bereinigung wird abgebrochen.") from exc
    try:
        credentials = Credentials.from_authorized_user_info(token_info)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials.valid:
            raise RuntimeError("GDRIVE_TOKEN ist nach Laden/Refresh nicht gültig.")
    except Exception as exc:
        raise RuntimeError("GDRIVE_TOKEN konnte nicht verwendet werden; Bereinigung wird abgebrochen.") from exc
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _public_summary(result: dict) -> dict:
    """Expose aggregate counts only; never publish Drive IDs or file names."""
    return {
        "retention_days": int(result["retention_days"]),
        "cutoff_utc": str(result["cutoff_utc"]),
        "candidate_count": int(result["candidate_count"]),
        "moved_to_trash_count": len(result["trashed_ids"]),
        "skipped_count": len(result["skipped_ids"]),
        # Reason codes are aggregate-only: no Drive IDs or filenames are exposed.
        "skipped_reason_counts": {
            str(reason): int(count)
            for reason, count in sorted((result.get("skipped_reason_counts") or {}).items())
        },
    }


def _write_report(status: str, summary: dict | None = None, error_type: str | None = None) -> Path:
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    report_path = workspace / REPORT_NAME
    payload = {
        "status": status,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "action": "move_expired_backups_to_google_drive_trash",
        "permanent_delete_performed": False,
        "summary": summary or {},
    }
    if error_type:
        payload["error_type"] = error_type
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path


def run_cleanup(drive, folder_id: str = FOLDER_ID) -> dict:
    """Run cleanup only after validating the production GitHub Actions context."""
    assert_production_main_environment()
    result = cleanup_expired_drive_backups(drive, folder_id)
    print(json.dumps(_public_summary(result), ensure_ascii=False, indent=2))
    return result


def main() -> int:
    # Guard before credential loading/service construction and before any API call.
    try:
        assert_production_main_environment()
        drive = _build_drive_service()
        result = run_cleanup(drive)
        summary = _public_summary(result)
        _write_report("success", summary)
        print(
            "DRIVE-BACKUP-RETENTION abgeschlossen | "
            f"in Papierkorb verschoben={summary['moved_to_trash_count']} | "
            f"übersprungen={summary['skipped_count']} | "
            f"Skip-Gründe={json.dumps(summary['skipped_reason_counts'], ensure_ascii=False, sort_keys=True)} | "
            f"Aufbewahrung={summary['retention_days']} Tage | endgültig gelöscht=0"
        )
        return 0
    except Exception as exc:
        _write_report("failed", error_type=type(exc).__name__)
        print(f"DRIVE-BACKUP-RETENTION FEHLER | Fehlertyp={type(exc).__name__} | endgültig gelöscht=0")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
