#!/usr/bin/env python3
"""Delete expired, application-marked positions-master backups from Drive."""
from __future__ import annotations

import json
import os

from positions_drive_backups import cleanup_expired_drive_backups

FOLDER_ID = "1BaKFsiqVVOP3uOrYDYXV4PPnFnWZBnjL"
PRODUCTION_REPOSITORY = "joergneuber/Sektor-Analyse"
ALLOWED_EVENTS = {"schedule", "workflow_dispatch"}


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
        mismatches.append(
            f"GITHUB_EVENT_NAME={event!r} (erlaubt: {', '.join(sorted(ALLOWED_EVENTS))})"
        )
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
        raise RuntimeError(f"Google-Drive-Bibliotheken fehlen: {exc}") from exc

    try:
        credentials = Credentials.from_authorized_user_info(token_info)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials.valid:
            raise RuntimeError("GDRIVE_TOKEN ist nach Laden/Refresh nicht gültig.")
    except Exception as exc:
        raise RuntimeError(f"GDRIVE_TOKEN konnte nicht verwendet werden: {exc}") from exc

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def run_cleanup(drive, folder_id: str = FOLDER_ID) -> dict:
    """Run cleanup only after validating the production GitHub Actions context."""
    assert_production_main_environment()
    result = cleanup_expired_drive_backups(drive, folder_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    # Guard before credential loading/service construction and before any API call.
    assert_production_main_environment()
    drive = _build_drive_service()
    result = run_cleanup(drive)
    print(
        "DRIVE-BACKUP-RETENTION abgeschlossen | "
        f"gelöscht={len(result['deleted_ids'])} | "
        f"übersprungen={len(result['skipped_ids'])} | "
        f"Aufbewahrung={result['retention_days']} Tage"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
