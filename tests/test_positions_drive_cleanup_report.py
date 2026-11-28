import json

from scripts import cleanup_positions_drive_backups as cleanup_script


def test_public_summary_never_exposes_drive_ids_or_file_names():
    result = {
        "retention_days": 14,
        "cutoff_utc": "2026-11-12T12:00:00Z",
        "trashed_ids": ["private-drive-file-id"],
        "skipped_ids": ["another-private-id"],
        "candidate_count": 2,
    }
    summary = cleanup_script._public_summary(result)
    rendered = json.dumps(summary)
    assert "private-drive-file-id" not in rendered
    assert "another-private-id" not in rendered
    assert summary["moved_to_trash_count"] == 1
    assert summary["skipped_count"] == 1


def test_report_explicitly_records_reversible_trash_action_and_no_permanent_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    path = cleanup_script._write_report(
        "success",
        {
            "retention_days": 14,
            "cutoff_utc": "2026-11-12T12:00:00Z",
            "candidate_count": 2,
            "moved_to_trash_count": 1,
            "skipped_count": 1,
        },
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["action"] == "move_expired_backups_to_google_drive_trash"
    assert payload["permanent_delete_performed"] is False
    assert payload["summary"]["moved_to_trash_count"] == 1


def test_failed_report_contains_error_class_but_not_exception_message(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    path = cleanup_script._write_report("failed", error_type="HttpError")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error_type"] == "HttpError"
    assert "file-id" not in path.read_text(encoding="utf-8")


def test_main_fails_closed_and_writes_sanitized_failure_report(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("GITHUB_REF", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    assert cleanup_script.main() == 1
    report = json.loads((tmp_path / cleanup_script.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error_type"] == "RuntimeError"
    assert report["permanent_delete_performed"] is False
