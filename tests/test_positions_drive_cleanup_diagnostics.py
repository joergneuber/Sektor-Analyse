import datetime as dt
import json

from positions_drive_backups import (
    BACKUP_CREATED_KEY,
    BACKUP_PREFIX,
    BACKUP_PURPOSE_KEY,
    BACKUP_PURPOSE_VALUE,
    cleanup_expired_drive_backups,
)
from scripts import cleanup_positions_drive_backups as cleanup_script


class Request:
    def __init__(self, callback):
        self.callback = callback

    def execute(self, **kwargs):
        return self.callback()


class FakeDrive:
    def __init__(self, records):
        self.records = {record["id"]: dict(record) for record in records}

    def files(self):
        outer = self

        class Files:
            def list(self, **kwargs):
                return Request(lambda: {"files": [dict(x) for x in outer.records.values()]})

            def get(self, fileId, fields=None):
                return Request(lambda: dict(outer.records[fileId]))

            def update(self, fileId, body, fields=None):
                def update_now():
                    outer.records[fileId].update(body)
                    return dict(outer.records[fileId])
                return Request(update_now)

        return Files()


def _backup(file_id, created_at, **overrides):
    record = {
        "id": file_id,
        "name": f"{BACKUP_PREFIX}20261001T000000Z_{file_id}",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": ["folder"],
        "appProperties": {
            BACKUP_PURPOSE_KEY: BACKUP_PURPOSE_VALUE,
            BACKUP_CREATED_KEY: created_at,
        },
        "trashed": False,
        "version": "1",
        "modifiedTime": "2026-10-01T00:00:00Z",
    }
    record.update(overrides)
    return record


def test_skip_reasons_distinguish_not_expired_from_invalid_marker_and_mime():
    now = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.timezone.utc)
    records = [
        _backup("fresh", "2026-11-20T00:00:00Z"),
        _backup("bad-marker", "2026-10-01T00:00:00Z", appProperties={BACKUP_CREATED_KEY: "2026-10-01T00:00:00Z"}),
        _backup("bad-mime", "2026-10-01T00:00:00Z", mimeType="application/vnd.google-apps.shortcut"),
    ]
    drive = FakeDrive(records)

    result = cleanup_expired_drive_backups(drive, "folder", now=now)

    assert result["trashed_ids"] == []
    assert set(result["skipped_ids"]) == {"fresh", "bad-marker", "bad-mime"}
    assert result["skipped_reason_counts"] == {
        "BACKUP_MARKER_MISMATCH": 1,
        "MIME_TYPE_NOT_ALLOWED": 1,
        "RETENTION_NOT_EXPIRED": 1,
    }
    assert all(record["trashed"] is False for record in drive.records.values())


def test_public_summary_exposes_only_aggregate_skip_reason_counts():
    result = {
        "retention_days": 14,
        "cutoff_utc": "2026-11-14T12:00:00Z",
        "candidate_count": 2,
        "trashed_ids": [],
        "skipped_ids": ["secret-id-1", "secret-id-2"],
        "skipped_reason_counts": {"RETENTION_NOT_EXPIRED": 2},
    }
    summary = cleanup_script._public_summary(result)
    rendered = json.dumps(summary)
    assert summary["moved_to_trash_count"] == 0
    assert summary["skipped_count"] == 2
    assert summary["skipped_reason_counts"] == {"RETENTION_NOT_EXPIRED": 2}
    assert "secret-id" not in rendered


def test_zero_trash_with_normal_skips_remains_successful_report(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    summary = {
        "retention_days": 14,
        "cutoff_utc": "2026-11-14T12:00:00Z",
        "candidate_count": 2,
        "moved_to_trash_count": 0,
        "skipped_count": 2,
        "skipped_reason_counts": {"RETENTION_NOT_EXPIRED": 2},
    }
    report = cleanup_script._write_report("success", summary)
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["summary"]["moved_to_trash_count"] == 0
    assert payload["summary"]["skipped_reason_counts"] == {"RETENTION_NOT_EXPIRED": 2}


def test_main_returns_zero_when_all_candidates_are_normally_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "joergneuber/Sektor-Analyse")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(cleanup_script, "_build_drive_service", lambda: object())
    result = {
        "retention_days": 14,
        "cutoff_utc": "2026-11-14T12:00:00Z",
        "candidate_count": 2,
        "trashed_ids": [],
        "skipped_ids": ["private-a", "private-b"],
        "skipped_reason_counts": {"RETENTION_NOT_EXPIRED": 2},
    }
    monkeypatch.setattr(cleanup_script, "cleanup_expired_drive_backups", lambda drive, folder: result)

    exit_code = cleanup_script.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / cleanup_script.REPORT_NAME).read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["summary"]["moved_to_trash_count"] == 0
    assert payload["summary"]["skipped_reason_counts"] == {"RETENTION_NOT_EXPIRED": 2}
    output = capsys.readouterr().out
    assert "Skip-Gründe" in output
    assert "private-a" not in output
    assert "private-b" not in output


def test_skip_reason_reports_revision_change_detected_on_second_read():
    now = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.timezone.utc)
    drive = FakeDrive([_backup("changed", "2026-10-01T00:00:00Z")])
    files = drive.files()
    original_get = files.get
    reads = {"count": 0}

    def get(fileId, fields=None):
        reads["count"] += 1
        if reads["count"] == 1:
            drive.records[fileId]["version"] = "2"
            drive.records[fileId]["modifiedTime"] = "2026-11-28T11:59:00Z"
        return original_get(fileId, fields)

    files.get = get
    drive.files = lambda: files

    result = cleanup_expired_drive_backups(drive, "folder", now=now)

    assert result["trashed_ids"] == []
    assert result["skipped_ids"] == ["changed"]
    assert result["skipped_reason_counts"] == {"FILE_REVISION_CHANGED_SINCE_LIST": 1}
    assert drive.records["changed"]["trashed"] is False
