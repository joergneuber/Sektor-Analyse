"""Retention-managed backups for the production positions Drive master.

Only files explicitly marked by this application are eligible for automatic
cleanup. Backups are retained for at least 14 full days from archive time.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

BACKUP_RETENTION_DAYS = 14
BACKUP_PURPOSE_KEY = "nmm_backup_purpose"
BACKUP_PURPOSE_VALUE = "positions-master-v1"
BACKUP_CREATED_KEY = "nmm_backup_created_at"
BACKUP_ORIGINAL_NAME_KEY = "nmm_backup_original_name"
BACKUP_PREFIX = "Offene_Positionen.__BACKUP_"
# Strict allowlist: the positions master is a native Google Sheet or a known
# tabular export. Folders, shortcuts, and unknown MIME types are never archived
# or deleted by this retention mechanism.
BACKUP_ALLOWED_MIME_TYPES = {
    "application/vnd.google-apps.spreadsheet",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class DriveBackupArchiveMutationError(RuntimeError):
    """An error after the Drive rename request may have mutated the source.

    Callers must preserve both files and reconcile remote state; deleting the
    newly uploaded master is unsafe once the rename request has been sent.
    """

    def __init__(self, message: str, *, file_id: str):
        super().__init__(message)
        self.file_id = str(file_id)
        self.mutation_started = True


def _utc_now(value: dt.datetime | None = None) -> dt.datetime:
    current = value or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc)


def _parse_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def archive_drive_backup(
    drive: Any,
    file_id: str,
    folder_id: str,
    *,
    now: dt.datetime | None = None,
    prefix: str = BACKUP_PREFIX,
    expected_revision: dict | None = None,
) -> dict:
    """Rename and mark an existing Drive file as a retention-managed backup.

    ``expected_revision`` is the revision captured by the caller before its
    transaction. It is checked against the *first* metadata read here, closing
    the gap between the caller's validation and this helper's initial read.
    """
    current = _utc_now(now)
    metadata = drive.files().get(
        fileId=file_id,
        fields="id,name,mimeType,parents,appProperties,trashed,version,modifiedTime,headRevisionId,md5Checksum",
    ).execute()
    if metadata.get("trashed"):
        raise RuntimeError(f"Drive-Backup kann nicht archiviert werden: Datei {file_id} ist im Papierkorb.")
    mime_type = str(metadata.get("mimeType") or "")
    if mime_type not in BACKUP_ALLOWED_MIME_TYPES:
        raise RuntimeError(
            f"Drive-Backup kann nicht archiviert werden: MIME-Typ {mime_type!r} "
            "ist nicht freigegeben (Ordner/Shortcuts/unbekannte Typen sind ausgeschlossen)."
        )
    if expected_revision is not None:
        expected_fields = {
            "id": expected_revision.get("file_id", expected_revision.get("id")),
            "version": expected_revision.get("version"),
            # With a version guard, modifiedTime is diagnostic only. If no
            # version was supplied, retain the legacy timestamp precondition.
            **({"modifiedTime": expected_revision.get("modified_time", expected_revision.get("modifiedTime"))}
               if expected_revision.get("version") is None else {}),
            "mimeType": expected_revision.get("mime_type", expected_revision.get("mimeType")),
            "md5Checksum": expected_revision.get("md5_checksum", expected_revision.get("md5Checksum")),
        }
        if any(
            expected is not None and str(metadata.get(field) or "") != str(expected)
            for field, expected in expected_fields.items()
        ):
            raise RuntimeError(
                f"Drive-Backup {file_id} entspricht nicht mehr der vom Aufrufer "
                "freigegebenen Quellrevision; Archivierung wird vor der Mutation abgebrochen."
            )
    parents = metadata.get("parents") or []
    if str(folder_id) not in {str(parent) for parent in parents}:
        raise RuntimeError(f"Drive-Backup kann nicht archiviert werden: Datei {file_id} liegt nicht im erwarteten Ordner.")

    stamp = current.strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"{prefix}{stamp}_{file_id}"
    properties = dict(metadata.get("appProperties") or {})
    properties.update({
        BACKUP_PURPOSE_KEY: BACKUP_PURPOSE_VALUE,
        BACKUP_CREATED_KEY: current.isoformat(timespec="seconds").replace("+00:00", "Z"),
        BACKUP_ORIGINAL_NAME_KEY: str(metadata.get("name") or ""),
    })
    # Re-read immediately before mutation. Drive API v3 documents no
    # conditional revision/version precondition for files.update; this narrows
    # but cannot eliminate the server-side race window.
    latest = drive.files().get(
        fileId=file_id,
        fields="id,name,mimeType,parents,appProperties,trashed,version,modifiedTime,headRevisionId,md5Checksum",
    ).execute()
    fingerprint_fields = (
        "id", "name", "mimeType", "parents", "appProperties", "trashed",
        "version", "modifiedTime", "headRevisionId", "md5Checksum",
    )
    if any(latest.get(field) != metadata.get(field) for field in fingerprint_fields):
        raise RuntimeError(
            f"Drive-Backup {file_id} wurde während der Archivierung extern verändert; "
            "Umbenennung wird fail-closed abgebrochen."
        )
    # From this point onward the server may have applied the rename even if
    # the response is lost. Mark every error as mutation-ambiguous so callers
    # never delete the newly uploaded master based on an unconfirmed state.
    try:
        updated = drive.files().update(
            fileId=file_id,
            body={"name": backup_name, "appProperties": properties},
            fields="id,name,parents,appProperties",
        ).execute()
    except Exception as exc:
        raise DriveBackupArchiveMutationError(
            f"Drive-Backup {file_id}: Fehler nach Beginn der Umbenennung; "
            "Remote-Zustand ist nicht sicher bestätigbar.",
            file_id=file_id,
        ) from exc
    try:
        verified = (
            isinstance(updated, dict)
            and str(updated.get("id")) == str(file_id)
            and updated.get("name") == backup_name
            and (updated.get("appProperties") or {}).get(BACKUP_PURPOSE_KEY)
            == BACKUP_PURPOSE_VALUE
        )
        if not verified:
            raise ValueError("Drive lieferte keine passende Bestätigung für Name und Backup-Markierung.")
    except Exception as exc:
        raise DriveBackupArchiveMutationError(
            f"Drive-Backup {file_id} konnte nach dem Umbenennen nicht verifiziert werden.",
            file_id=file_id,
        ) from exc
    return updated


def _trash_and_verify(drive: Any, file_id: str) -> bool:
    """Move one file to Google Drive trash and verify the persisted state.

    Never call ``files.delete``: Drive's ``files.update`` with ``trashed=True``
    is reversible during Google's trash-retention window. If the update
    response is lost, reconcile the exact file before deciding whether it
    succeeded.
    """
    files = drive.files()
    try:
        files.update(
            fileId=file_id,
            body={"trashed": True},
            fields="id,trashed",
        ).execute()
    except Exception:
        # The server may have applied the update even though the response was
        # lost. Treat only a confirmed remote trash state as success.
        latest = files.get(fileId=file_id, fields="id,trashed").execute()
        if (
            isinstance(latest, dict)
            and str(latest.get("id")) == str(file_id)
            and latest.get("trashed") is True
        ):
            return True
        raise

    # A successful HTTP response alone is not sufficient evidence that the
    # requested state persisted; the follow-up read is authoritative.
    latest = files.get(fileId=file_id, fields="id,trashed").execute()
    if not (
        isinstance(latest, dict)
        and str(latest.get("id")) == str(file_id)
        and latest.get("trashed") is True
    ):
        raise RuntimeError(
            f"Drive-Backup {file_id} konnte nicht sicher in den Papierkorb verschoben werden."
        )
    return True


def cleanup_expired_drive_backups(
    drive: Any,
    folder_id: str,
    *,
    now: dt.datetime | None = None,
    retention_days: int = BACKUP_RETENTION_DAYS,
) -> dict:
    """Move only application-marked backups older than the retention period to trash.

    The candidate is fetched again immediately before mutation. Any missing,
    changed, unmarked, or malformed candidate is skipped. Expired files are
    moved to Google Drive trash, never permanently deleted by this helper.
    API failures raise; callers must treat cleanup as best-effort and never
    roll back a successful positions-master replacement because cleanup failed.
    """
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ValueError("retention_days muss mindestens 1 sein und eine positive ganze Zahl enthalten.")
    current = _utc_now(now)
    cutoff = current - dt.timedelta(days=retention_days)
    query = (
        f"'{folder_id}' in parents and trashed=false and "
        f"appProperties has {{ key='{BACKUP_PURPOSE_KEY}' and value='{BACKUP_PURPOSE_VALUE}' }}"
    )
    candidates = []
    page_token = None
    while True:
        response = drive.files().list(
            q=query,
            pageSize=1000,
            pageToken=page_token,
            fields="nextPageToken,files(id,name,mimeType,parents,appProperties,trashed,version,modifiedTime,headRevisionId,md5Checksum)",
        ).execute()
        candidates.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    trashed: list[str] = []
    skipped: list[str] = []
    skipped_reason_counts: dict[str, int] = {}

    def skip(file_id: str, reason: str) -> None:
        skipped.append(file_id or "<unknown>")
        skipped_reason_counts[reason] = skipped_reason_counts.get(reason, 0) + 1

    for candidate in candidates:
        file_id = str(candidate.get("id") or "")
        name = str(candidate.get("name") or "")
        props = candidate.get("appProperties") or {}
        created = _parse_utc(props.get(BACKUP_CREATED_KEY))
        # Record one deterministic, non-sensitive reason per skipped file.
        # IDs and filenames stay in the private in-memory result only.
        if not file_id:
            skip("", "MISSING_FILE_ID")
            continue
        if not name.startswith(BACKUP_PREFIX):
            skip(file_id, "NAME_PREFIX_MISMATCH")
            continue
        if str(folder_id) not in {str(parent) for parent in candidate.get("parents", [])}:
            skip(file_id, "WRONG_PARENT_FOLDER")
            continue
        if props.get(BACKUP_PURPOSE_KEY) != BACKUP_PURPOSE_VALUE:
            skip(file_id, "BACKUP_MARKER_MISMATCH")
            continue
        if candidate.get("mimeType") not in BACKUP_ALLOWED_MIME_TYPES:
            skip(file_id, "MIME_TYPE_NOT_ALLOWED")
            continue
        if created is None:
            skip(file_id, "BACKUP_TIMESTAMP_INVALID")
            continue
        if created > cutoff:
            skip(file_id, "RETENTION_NOT_EXPIRED")
            continue

        # Re-read the exact candidate to avoid moving a file that was moved,
        # renamed, or had its retention marker changed after list().
        latest = drive.files().get(
            fileId=file_id,
            fields="id,name,mimeType,parents,appProperties,trashed,version,modifiedTime,headRevisionId,md5Checksum",
        ).execute()
        latest_props = latest.get("appProperties") or {}
        latest_created = _parse_utc(latest_props.get(BACKUP_CREATED_KEY))
        if latest.get("trashed"):
            skip(file_id, "ALREADY_TRASHED_ON_RECHECK")
            continue
        if latest.get("name") != name:
            skip(file_id, "NAME_CHANGED_SINCE_LIST")
            continue
        if str(folder_id) not in {str(parent) for parent in latest.get("parents", [])}:
            skip(file_id, "PARENT_CHANGED_SINCE_LIST")
            continue
        if latest_props.get(BACKUP_PURPOSE_KEY) != BACKUP_PURPOSE_VALUE:
            skip(file_id, "BACKUP_MARKER_CHANGED_SINCE_LIST")
            continue
        if latest.get("mimeType") not in BACKUP_ALLOWED_MIME_TYPES or latest.get("mimeType") != candidate.get("mimeType"):
            skip(file_id, "MIME_TYPE_CHANGED_OR_NOT_ALLOWED")
            continue
        if latest_created is None:
            skip(file_id, "BACKUP_TIMESTAMP_INVALID_ON_RECHECK")
            continue
        if latest_created > cutoff or latest_created != created:
            skip(file_id, "BACKUP_TIMESTAMP_CHANGED_OR_NOT_EXPIRED")
            continue
        if any(
            latest.get(field) != candidate.get(field)
            for field in ("version", "modifiedTime", "headRevisionId", "md5Checksum")
        ):
            skip(file_id, "FILE_REVISION_CHANGED_SINCE_LIST")
            continue

        if _trash_and_verify(drive, file_id):
            trashed.append(file_id)

    return {
        "retention_days": retention_days,
        "cutoff_utc": cutoff.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "trashed_ids": trashed,
        "skipped_ids": skipped,
        "skipped_reason_counts": dict(sorted(skipped_reason_counts.items())),
        "candidate_count": len(candidates),
    }
