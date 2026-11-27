import datetime as dt
from pathlib import Path

import pytest

from positions_drive_backups import (
    BACKUP_CREATED_KEY,
    BACKUP_PREFIX,
    BACKUP_PURPOSE_KEY,
    BACKUP_PURPOSE_VALUE,
    DriveBackupArchiveMutationError,
    archive_drive_backup,
    cleanup_expired_drive_backups,
)


class _Request:
    def __init__(self, callback):
        self._callback = callback

    def execute(self, **kwargs):
        return self._callback()


class _FakeDrive:
    def __init__(self, files):
        self.data = {item["id"]: dict(item) for item in files}
        self.deleted = []
        self.mutate_after_list = None

    def files(self):
        outer = self

        class Files:
            def get(self, fileId, fields=None):
                return _Request(lambda: dict(outer.data[fileId]))

            def update(self, fileId, body, fields=None):
                def update_now():
                    outer.data[fileId].update(body)
                    return dict(outer.data[fileId])
                return _Request(update_now)

            def list(self, q=None, pageSize=None, pageToken=None, fields=None):
                def list_now():
                    found = [
                        dict(item) for item in outer.data.values()
                        if item.get("trashed") is not True
                        and BACKUP_PURPOSE_KEY in (item.get("appProperties") or {})
                        and (item.get("appProperties") or {}).get(BACKUP_PURPOSE_KEY) == BACKUP_PURPOSE_VALUE
                    ]
                    if outer.mutate_after_list:
                        outer.mutate_after_list(outer)
                    return {"files": found}
                return _Request(list_now)

            def delete(self, fileId):
                def delete_now():
                    outer.deleted.append(fileId)
                    outer.data[fileId]["trashed"] = True
                    return {}
                return _Request(delete_now)

        return Files()


def _backup(file_id, created, *, name=None, marked=True, folder="folder"):
    props = {BACKUP_CREATED_KEY: created}
    if marked:
        props[BACKUP_PURPOSE_KEY] = BACKUP_PURPOSE_VALUE
    return {
        "id": file_id,
        "name": name or f"{BACKUP_PREFIX}{file_id}",
        "parents": [folder],
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "appProperties": props,
        "trashed": False,
    }


def test_archive_renames_and_marks_master_without_deleting_it():
    drive = _FakeDrive([{
        "id": "master-1", "name": "Offene_Positionen",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["folder"], "appProperties": {"existing": "keep"},
        "trashed": False,
    }])
    now = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)

    result = archive_drive_backup(drive, "master-1", "folder", now=now)

    assert result["name"].startswith(BACKUP_PREFIX + "20261126T120000Z_")
    assert result["appProperties"]["existing"] == "keep"
    assert result["appProperties"][BACKUP_PURPOSE_KEY] == BACKUP_PURPOSE_VALUE
    assert result["appProperties"][BACKUP_CREATED_KEY] == "2026-11-26T12:00:00Z"
    assert drive.deleted == []
    assert drive.data["master-1"]["trashed"] is False


def test_archive_refuses_file_outside_expected_folder():
    drive = _FakeDrive([{
        "id": "master-1", "name": "Offene_Positionen",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["other-folder"], "appProperties": {}, "trashed": False,
    }])
    with pytest.raises(RuntimeError, match="erwarteten Ordner"):
        archive_drive_backup(drive, "master-1", "folder")
    assert drive.deleted == []


def test_cleanup_deletes_only_marked_backups_older_than_14_full_days():
    now = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
    expired = _backup("expired", "2026-11-12T11:59:59Z")
    boundary = _backup("boundary", "2026-11-12T12:00:00Z")
    fresh = _backup("fresh", "2026-11-12T12:00:01Z")
    unmarked = _backup("unmarked", "2026-10-01T00:00:00Z", marked=False)
    wrong_folder = _backup("wrong-folder", "2026-10-01T00:00:00Z", folder="elsewhere")
    wrong_name = _backup("wrong-name", "2026-10-01T00:00:00Z", name="manual-copy")
    drive = _FakeDrive([expired, boundary, fresh, unmarked, wrong_folder, wrong_name])

    result = cleanup_expired_drive_backups(drive, "folder", now=now)

    assert set(drive.deleted) == {"expired", "boundary"}
    assert set(result["deleted_ids"]) == {"expired", "boundary"}
    assert drive.data["fresh"]["trashed"] is False
    assert drive.data["unmarked"]["trashed"] is False
    assert drive.data["wrong-folder"]["trashed"] is False
    assert drive.data["wrong-name"]["trashed"] is False


def test_cleanup_skips_backup_changed_after_candidate_listing():
    now = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
    drive = _FakeDrive([_backup("changed", "2026-10-01T00:00:00Z")])

    def mutate(outer):
        outer.data["changed"]["name"] = "manual-renamed-file"

    drive.mutate_after_list = mutate
    result = cleanup_expired_drive_backups(drive, "folder", now=now)

    assert drive.deleted == []
    assert result["skipped_ids"] == ["changed"]
    assert drive.data["changed"]["trashed"] is False


def test_cleanup_requires_timezone_aware_creation_timestamp():
    now = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
    drive = _FakeDrive([_backup("invalid", "2026-10-01T00:00:00")])
    result = cleanup_expired_drive_backups(drive, "folder", now=now)
    assert drive.deleted == []
    assert result["skipped_ids"] == ["invalid"]


def test_cleanup_rejects_zero_or_negative_retention():
    drive = _FakeDrive([])
    with pytest.raises(ValueError, match="mindestens 1"):
        cleanup_expired_drive_backups(drive, "folder", retention_days=0)


def test_cleaned_source_sync_archives_old_master_and_runs_retention_cleanup(tmp_path, monkeypatch):
    import hashlib
    import offene_positionen_check as checker

    source = tmp_path / "Offene_Positionen.csv"
    source.write_text("Ticker;Status\nABC;Offen\n", encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    baseline = {"file_id": "old-master", "version": "10"}
    monkeypatch.setattr(checker, "_verify_drive_source_baseline_with_service", lambda *a, **k: baseline)
    monkeypatch.setattr(
        checker, "cleanup_expired_drive_backups",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulierter Retention-Loeschfehler")),
    )

    class Request:
        def __init__(self, callback):
            self.callback = callback
        def execute(self, **kwargs):
            return self.callback()

    class DriveFiles:
        def __init__(self):
            self.data = {
                "old-master": {
                    "id": "old-master", "name": "Offene_Positionen",
                    "mimeType": checker.GOOGLE_SHEETS_MIME_TYPE,
                    "parents": [checker.FOLDER_ID], "appProperties": {}, "trashed": False,
                    "version": "10", "modifiedTime": "2026-11-26T12:00:00Z",
                }
            }
            self.deleted = []
        def get(self, fileId, fields=None):
            return Request(lambda: dict(self.data[fileId]))
        def list(self, q=None, **kwargs):
            def result():
                if "appProperties has" in (q or ""):
                    matches = [x for x in self.data.values() if (x.get("appProperties") or {}).get(BACKUP_PURPOSE_KEY) == BACKUP_PURPOSE_VALUE and not x.get("trashed")]
                elif "name='Offene_Positionen'" in (q or ""):
                    matches = [x for x in self.data.values() if x.get("name") == "Offene_Positionen" and not x.get("trashed")]
                else:
                    matches = [x for x in self.data.values() if x.get("name") in {"Offene_Positionen", "Offene_Positionen.csv"} and not x.get("trashed")]
                return {"files": [dict(x) for x in matches]}
            return Request(result)
        def create(self, body, fields=None):
            def result():
                file_id = "new-master"
                self.data[file_id] = {
                    "id": file_id, "name": body["name"],
                    "mimeType": body["mimeType"], "parents": list(body["parents"]),
                    "appProperties": {}, "trashed": False,
                }
                return {"id": file_id, "name": body["name"], "mimeType": body["mimeType"]}
            return Request(result)
        def update(self, fileId, body, fields=None):
            def result():
                self.data[fileId].update(body)
                return dict(self.data[fileId])
            return Request(result)
        def delete(self, fileId):
            def result():
                self.deleted.append(fileId)
                self.data[fileId]["trashed"] = True
                return {}
            return Request(result)

    class SheetsValues:
        def __init__(self, parent):
            self.parent = parent
        def update(self, spreadsheetId, range, valueInputOption, body):
            return Request(lambda: self.parent.values_store.__setitem__(spreadsheetId, body["values"]) or {})
        def get(self, spreadsheetId, range, valueRenderOption=None):
            return Request(lambda: {"values": self.parent.values_store.get(spreadsheetId, [])})

    class SheetsSpreadsheets:
        def __init__(self):
            self.values_store = {}
        def get(self, spreadsheetId, fields=None):
            return Request(lambda: {"sheets": [{"properties": {"sheetId": 1, "title": "Sheet1"}}]})
        def values(self):
            return SheetsValues(self)

    class SheetsService:
        def __init__(self):
            self._spreadsheets = SheetsSpreadsheets()
        def spreadsheets(self):
            return self._spreadsheets

    class DriveService:
        def __init__(self):
            self._files = DriveFiles()
        def files(self):
            return self._files

    drive = DriveService()
    sheets = SheetsService()
    monkeypatch.setattr(checker, "build", lambda name, version, credentials=None: drive if name == "drive" else sheets, raising=False)

    checker._sync_cleaned_source_to_drive(
        str(source), creds=object(), expected_drive_state=baseline,
        expected_local_sha256=source_hash,
    )

    archived = drive._files.data["old-master"]
    assert archived["name"].startswith(BACKUP_PREFIX)
    assert archived["appProperties"][BACKUP_PURPOSE_KEY] == BACKUP_PURPOSE_VALUE
    assert drive._files.data["new-master"]["name"] == "Offene_Positionen"
    assert drive._files.deleted == []


def test_scheduled_cleanup_entrypoint_uses_fixed_retention_policy(monkeypatch):
    import scripts.cleanup_positions_drive_backups as cleanup_script
    calls = []
    expected = {
        "retention_days": 14,
        "deleted_ids": ["old-backup"],
        "skipped_ids": [],
        "candidate_count": 1,
        "cutoff_utc": "2026-11-12T12:00:00Z",
    }
    monkeypatch.setattr(
        cleanup_script, "cleanup_expired_drive_backups",
        lambda drive, folder_id: calls.append((drive, folder_id)) or expected,
    )
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "joergneuber/Sektor-Analyse")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    drive = object()
    result = cleanup_script.run_cleanup(drive, "folder-id")
    assert result == expected
    assert calls == [(drive, "folder-id")]


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_cleanup_entrypoint_allows_only_production_main(monkeypatch, event):
    import scripts.cleanup_positions_drive_backups as cleanup

    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "joergneuber/Sektor-Analyse")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    cleanup.assert_production_main_environment()


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REF", "refs/heads/feature/test"),
        ("GITHUB_REF_NAME", "feature/test"),
        ("GITHUB_REPOSITORY", "attacker/fork"),
        ("GITHUB_EVENT_NAME", "pull_request"),
    ],
)
def test_cleanup_entrypoint_fails_closed_outside_production_main(monkeypatch, key, value):
    import scripts.cleanup_positions_drive_backups as cleanup

    env = {
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_NAME": "main",
        "GITHUB_REPOSITORY": "joergneuber/Sektor-Analyse",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }
    env[key] = value
    with pytest.raises(RuntimeError, match="ausschließlich"):
        cleanup.assert_production_main_environment(env)


def test_cleanup_entrypoint_fails_closed_when_branch_context_is_missing():
    import scripts.cleanup_positions_drive_backups as cleanup

    with pytest.raises(RuntimeError, match="ausschließlich"):
        cleanup.assert_production_main_environment({})


def test_run_cleanup_checks_branch_before_any_delete(monkeypatch):
    import scripts.cleanup_positions_drive_backups as cleanup

    monkeypatch.delenv("GITHUB_REF", raising=False)
    called = []
    class Drive:
        def files(self):
            called.append("files")
            raise AssertionError("Drive darf vor Branch-Prüfung nicht angesprochen werden")

    with pytest.raises(RuntimeError, match="ausschließlich"):
        cleanup.run_cleanup(Drive())
    assert called == []


def test_archive_refuses_external_change_between_initial_read_and_update():
    drive = _FakeDrive([{
        "id": "master-1", "name": "Offene_Positionen",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["folder"], "appProperties": {}, "trashed": False,
        "version": "10", "modifiedTime": "2026-11-26T12:00:00Z",
    }])
    original_files = drive.files
    files_instance = original_files()
    original_get = files_instance.get
    calls = {"count": 0}

    def get(fileId, fields=None):
        calls["count"] += 1
        if calls["count"] == 2:
            drive.data[fileId]["version"] = "11"
            drive.data[fileId]["modifiedTime"] = "2026-11-26T12:01:00Z"
        return original_get(fileId, fields)

    files_instance.get = get
    drive.files = lambda: files_instance

    with pytest.raises(RuntimeError, match="extern verändert"):
        archive_drive_backup(drive, "master-1", "folder")
    assert drive.data["master-1"]["name"] == "Offene_Positionen"


def test_cleanup_skips_candidate_when_drive_version_changes_after_listing():
    now = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
    candidate = _backup("version-changed", "2026-10-01T00:00:00Z")
    candidate["version"] = "21"
    candidate["modifiedTime"] = "2026-11-26T10:00:00Z"
    drive = _FakeDrive([candidate])

    def mutate(outer):
        outer.data["version-changed"]["version"] = "22"
        outer.data["version-changed"]["modifiedTime"] = "2026-11-26T11:00:00Z"

    drive.mutate_after_list = mutate
    result = cleanup_expired_drive_backups(drive, "folder", now=now)

    assert drive.deleted == []
    assert result["skipped_ids"] == ["version-changed"]
    assert drive.data["version-changed"]["trashed"] is False


def test_archive_rejects_drive_folder_before_any_update():
    drive = _FakeDrive([{
        "id": "folder-object", "name": "Offene_Positionen",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["folder"], "appProperties": {}, "trashed": False,
    }])
    with pytest.raises(RuntimeError, match="MIME-Typ"):
        archive_drive_backup(drive, "folder-object", "folder")
    assert drive.data["folder-object"]["name"] == "Offene_Positionen"
    assert drive.deleted == []


def test_archive_compares_caller_revision_on_first_metadata_read():
    drive = _FakeDrive([{
        "id": "master-1", "name": "Offene_Positionen",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["folder"], "appProperties": {}, "trashed": False,
        "version": "11", "modifiedTime": "2026-11-26T12:01:00Z",
        "md5Checksum": "changed",
    }])
    expected = {
        "file_id": "master-1", "version": "10",
        "modified_time": "2026-11-26T12:00:00Z",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "md5_checksum": "original",
    }
    with pytest.raises(RuntimeError, match="freigegebenen Quellrevision"):
        archive_drive_backup(drive, "master-1", "folder", expected_revision=expected)
    assert drive.data["master-1"]["name"] == "Offene_Positionen"


def test_cleanup_skips_folder_even_when_marked_and_expired():
    now = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
    folder = _backup("marked-folder", "2026-10-01T00:00:00Z")
    folder["mimeType"] = "application/vnd.google-apps.folder"
    drive = _FakeDrive([folder])
    result = cleanup_expired_drive_backups(drive, "folder", now=now)
    assert drive.deleted == []
    assert result["skipped_ids"] == ["marked-folder"]
    assert drive.data["marked-folder"]["trashed"] is False


def test_cleanup_skips_unknown_mime_type():
    now = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc)
    shortcut = _backup("shortcut", "2026-10-01T00:00:00Z")
    shortcut["mimeType"] = "application/vnd.google-apps.shortcut"
    drive = _FakeDrive([shortcut])
    result = cleanup_expired_drive_backups(drive, "folder", now=now)
    assert drive.deleted == []
    assert result["skipped_ids"] == ["shortcut"]


def test_ambiguous_archive_rename_is_reconciled_by_remote_ids():
    import offene_positionen_check as checker

    old_id = "old-master"
    temp_id = "temp-master"
    backup_name = f"{BACKUP_PREFIX}20261127T120000Z_{old_id}"
    drive = _FakeDrive([
        {
            "id": old_id, "name": backup_name,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": ["folder"],
            "appProperties": {BACKUP_PURPOSE_KEY: BACKUP_PURPOSE_VALUE},
            "trashed": False,
        },
        {
            "id": temp_id, "name": "Offene_Positionen.__SYNC_20261127120000",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": ["folder"], "appProperties": {}, "trashed": False,
        },
    ])

    checker._reconcile_failed_drive_source_swap(drive, old_id, temp_id)

    assert drive.data[old_id]["name"] == "Offene_Positionen"
    assert drive.data[old_id]["trashed"] is False
    assert drive.data[temp_id]["trashed"] is True


def test_ambiguous_swap_with_new_master_already_active_preserves_both_files():
    import offene_positionen_check as checker

    old_id = "old-master"
    temp_id = "new-master"
    drive = _FakeDrive([
        {
            "id": old_id, "name": f"{BACKUP_PREFIX}20261127T120000Z_{old_id}",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": ["folder"],
            "appProperties": {BACKUP_PURPOSE_KEY: BACKUP_PURPOSE_VALUE},
            "trashed": False,
        },
        {
            "id": temp_id, "name": "Offene_Positionen",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": ["folder"], "appProperties": {}, "trashed": False,
        },
    ])

    with pytest.raises(RuntimeError, match="bereits aktiviert"):
        checker._reconcile_failed_drive_source_swap(drive, old_id, temp_id)

    assert drive.data[old_id]["name"].startswith(BACKUP_PREFIX)
    assert drive.data[temp_id]["name"] == "Offene_Positionen"
    assert drive.deleted == []


def test_tracker_passes_download_revision_into_archive_helper():
    tracker_source = (Path(__file__).parents[1] / "positionen_tracker.py").read_text(encoding="utf-8")
    assert "expected_revision=expected_old_revision" in tracker_source


def test_cleaned_source_sync_passes_remote_baseline_into_archive_helper():
    checker_source = (Path(__file__).parents[1] / "offene_positionen_check.py").read_text(encoding="utf-8")
    assert "expected_revision=expected_drive_state" in checker_source


def test_archive_marks_update_request_errors_as_mutation_ambiguous():
    class _UpdateMayHaveAppliedDrive(_FakeDrive):
        def files(self):
            outer = self

            class Files:
                def get(self, fileId, fields=None):
                    return _Request(lambda: dict(outer.data[fileId]))

                def update(self, fileId, body, fields=None):
                    def update_then_timeout():
                        # Simulate Google applying the rename while the client
                        # loses the response.
                        outer.data[fileId].update(body)
                        raise TimeoutError("response lost after remote mutation")
                    return _Request(update_then_timeout)

            return Files()

    drive = _UpdateMayHaveAppliedDrive([{
        "id": "master-1",
        "name": "Offene_Positionen",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["folder"],
        "appProperties": {},
        "trashed": False,
    }])

    with pytest.raises(DriveBackupArchiveMutationError) as captured:
        archive_drive_backup(drive, "master-1", "folder")

    assert captured.value.mutation_started is True
    assert captured.value.file_id == "master-1"
    assert drive.data["master-1"]["name"].startswith(BACKUP_PREFIX)
    assert drive.deleted == []


def test_archive_accepts_timestamp_only_difference_with_matching_version():
    drive = _FakeDrive([{
        "id": "master-1", "name": "Offene_Positionen",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["folder"], "appProperties": {}, "trashed": False,
        "version": "10", "modifiedTime": "2026-11-26T12:01:00Z",
        "md5Checksum": "original",
    }])
    expected = {
        "file_id": "master-1", "version": "10",
        "modified_time": "2026-11-26T12:00:00Z",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "md5_checksum": "original",
    }
    result = archive_drive_backup(drive, "master-1", "folder", expected_revision=expected)
    assert result["name"].startswith("Offene_Positionen.__BACKUP_")
