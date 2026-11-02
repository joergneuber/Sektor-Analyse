import io
import json
import zipfile

import pytest

import trading_radar_thesen_persistence as persistence


def _register_bytes():
    return json.dumps({
        "schema_version": 1,
        "updated_at": "2026-11-02T08:10:34+00:00",
        "theses": [{
            "thesis_id": "THESIS_TEST",
            "version": 1,
            "active": True,
            "thesis": "Testthese",
        }],
    }).encode("utf-8")


def _artifact_bytes(register_bytes=None):
    raw = register_bytes or _register_bytes()
    register = persistence.parse_and_validate_register(raw)
    manifest = persistence.build_manifest(raw, register, origin="current_run")
    # Artifact fixtures must model the required GitHub provenance fields.
    manifest.update({"repository": "owner/repo", "branch": "main", "run_id": "12345"})
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".trading_radar_thesen.json", raw)
        archive.writestr(
            ".trading_radar_thesen.json.manifest.json",
            json.dumps(manifest).encode("utf-8"),
        )
    return archive_buffer.getvalue()


def test_prepare_creates_hash_manifest_and_verifies(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"
    path.write_bytes(_register_bytes())

    assert persistence.command_prepare(type("Args", (), {"register": str(path)})()) == 0
    manifest = json.loads(persistence.manifest_path(path).read_text(encoding="utf-8"))

    assert manifest["register_sha256"] == persistence.sha256_bytes(path.read_bytes())
    assert manifest["active_thesis_count"] == 1
    assert persistence.verify_local_snapshot(path) == (True, "CACHE_VERIFIED")


def test_valid_legacy_cache_gets_sidecar_without_claiming_original_run(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"
    path.write_bytes(_register_bytes())

    valid, status = persistence.verify_local_snapshot(path)

    assert valid is True
    assert status == "CACHE_LEGACY_REVALIDATED"
    manifest = json.loads(persistence.manifest_path(path).read_text(encoding="utf-8"))
    assert manifest["origin"] == "legacy_cache_revalidated"
    assert manifest["run_id"] is None


def test_manifest_hash_mismatch_is_rejected(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"
    path.write_bytes(_register_bytes())
    persistence.command_prepare(type("Args", (), {"register": str(path)})())
    path.write_bytes(_register_bytes() + b" ")

    assert persistence.verify_local_snapshot(path) == (False, "MANIFEST_HASH_MISMATCH")


def test_artifact_restore_verifies_and_writes_both_files(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"

    persistence.restore_from_artifact(
        path, _artifact_bytes(), "12345", expected_repo="owner/repo", expected_branch="main"
    )

    assert path.exists()
    assert persistence.manifest_path(path).exists()
    assert persistence.verify_local_snapshot(path) == (True, "CACHE_VERIFIED")


def test_artifact_restore_rejects_tampered_register(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"
    valid_archive = _artifact_bytes()
    tampered_archive = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(valid_archive), "r") as source:
        with zipfile.ZipFile(tampered_archive, "w", zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                content = source.read(name)
                if name == ".trading_radar_thesen.json":
                    content += b" "
                target.writestr(name, content)
    with pytest.raises(ValueError, match="Hash stimmt"):
        persistence.restore_from_artifact(
            path, tampered_archive.getvalue(), "12345", expected_repo="owner/repo", expected_branch="main"
        )
    assert not path.exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repository", None, "keine Repository-Angabe"),
        ("repository", "other/repo", "anderen Repository"),
        ("branch", None, "keine Branch-Angabe"),
        ("branch", "develop", "anderen Branch"),
        ("run_id", None, "keine Run-ID"),
        ("run_id", "99999", "Run-ID stimmt nicht"),
    ],
)
def test_artifact_restore_requires_exact_repository_branch_and_run_id(tmp_path, field, value, message):
    path = tmp_path / ".trading_radar_thesen.json"
    raw = _register_bytes()
    register = persistence.parse_and_validate_register(raw)
    manifest = persistence.build_manifest(raw, register, origin="current_run")
    manifest.update({"repository": "owner/repo", "branch": "main", "run_id": "12345"})
    manifest[field] = value
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(path.name, raw)
        archive.writestr(persistence.manifest_path(path).name, json.dumps(manifest).encode("utf-8"))

    with pytest.raises(ValueError, match=message):
        persistence.restore_from_artifact(
            path, archive_buffer.getvalue(), "12345", expected_repo="owner/repo", expected_branch="main"
        )
    assert not path.exists()


def test_artifact_restore_requires_expected_context(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"
    with pytest.raises(ValueError, match="Erwartetes Repository fehlt"):
        persistence.restore_from_artifact(
            path, _artifact_bytes(), "12345", expected_repo="", expected_branch="main"
        )


def test_empty_or_inactive_register_is_not_persistable():
    empty = json.dumps({"schema_version": 1, "theses": []}).encode()
    with pytest.raises(ValueError, match="keine aktive These"):
        persistence.parse_and_validate_register(empty)

    inactive = json.dumps({
        "schema_version": 1,
        "theses": [{"thesis_id": "T1", "version": 1, "active": False}],
    }).encode()
    with pytest.raises(ValueError, match="keine aktive These"):
        persistence.parse_and_validate_register(inactive)


def test_restore_uses_valid_cache_without_calling_github_api(tmp_path, monkeypatch):
    path = tmp_path / ".trading_radar_thesen.json"
    path.write_bytes(_register_bytes())
    persistence.command_prepare(type("Args", (), {"register": str(path)})())

    def fail(*args, **kwargs):
        raise AssertionError("GitHub API darf bei gueltigem Cache nicht aufgerufen werden")

    monkeypatch.setattr(persistence, "find_latest_register_artifact", fail)
    assert persistence.command_restore(type("Args", (), {"register": str(path)})()) == 0


def test_restore_falls_back_to_latest_artifact_when_cache_missing(tmp_path, monkeypatch):
    path = tmp_path / ".trading_radar_thesen.json"
    archive_bytes = _artifact_bytes()
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "99999")
    monkeypatch.setattr(
        persistence,
        "find_latest_register_artifact",
        lambda *args, **kwargs: [("https://example.invalid/artifact.zip", "12345")],
    )
    monkeypatch.setattr(persistence, "download_artifact", lambda *args, **kwargs: archive_bytes)

    assert persistence.command_restore(type("Args", (), {"register": str(path)})()) == 0
    assert persistence.verify_local_snapshot(path) == (True, "CACHE_VERIFIED")
    assert path.exists()


def test_workflow_uses_cache_then_verified_artifact_fallback_and_uploads_hidden_files():
    from pathlib import Path

    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")
    assert "actions/cache/restore@v5" in workflow
    assert "continue-on-error: true" in workflow
    assert "python trading_radar_thesen_persistence.py restore" in workflow
    assert "python trading_radar_thesen_persistence.py prepare" in workflow
    assert "actions/cache/save@v5" in workflow
    assert "actions/upload-artifact@v6" in workflow
    assert "trading-radar-thesen-register-${{ github.run_id }}" in workflow
    assert "include-hidden-files: true" in workflow
    assert "retention-days: 90" in workflow
    assert ".trading_radar_thesen.json.manifest.json" in workflow


def test_latest_artifact_lookup_ignores_running_current_and_other_branch_runs(monkeypatch):
    calls = []

    def fake_api_json(url, token):
        calls.append(url)
        if "/workflows/main.yml/runs?" in url:
            return {"workflow_runs": [
                {"id": 999, "status": "in_progress", "path": ".github/workflows/main.yml", "head_branch": "main", "created_at": "2026-11-02"},
                {"id": 998, "status": "completed", "path": ".github/workflows/main.yml", "head_branch": "dev", "created_at": "2026-11-01"},
                {"id": 997, "status": "completed", "path": ".github/workflows/main.yml", "head_branch": "main", "created_at": "2026-10-31"},
            ]}
        if "/actions/runs/997/artifacts?" in url:
            return {"artifacts": [
                {"name": "unrelated-artifact", "expired": False, "archive_download_url": "https://example.invalid/no.zip"},
                {"name": persistence.ARTIFACT_PREFIX + "997", "expired": True, "archive_download_url": "https://example.invalid/expired.zip"},
                {"name": persistence.ARTIFACT_PREFIX + "997", "expired": False, "archive_download_url": "https://example.invalid/valid.zip"},
            ]}
        raise AssertionError(f"Unerwarteter API-Endpunkt: {url}")

    monkeypatch.setattr(persistence, "api_json", fake_api_json)
    result = persistence.find_latest_register_artifact("owner/repo", "main", "token", "999")

    assert result == [("https://example.invalid/valid.zip", "997")]
    assert any("/actions/runs/997/artifacts?" in url for url in calls)


def test_artifact_redirect_does_not_forward_github_bearer_token():
    from urllib.request import Request

    original = Request(
        "https://api.github.com/repos/owner/repo/actions/artifacts/1/zip",
        headers={"Authorization": "Bearer sensitive-token", "Accept": "application/vnd.github+json"},
    )
    redirected = persistence.StripAuthorizationOnRedirect().redirect_request(
        original, None, 302, "Found", {}, "https://artifact-storage.example.invalid/signed.zip"
    )

    assert redirected.full_url.startswith("https://artifact-storage.example.invalid/")
    assert not any(name.lower() == "authorization" for name in redirected.headers)
    assert not any(name.lower() == "authorization" for name in redirected.unredirected_hdrs)


def test_restore_skips_corrupt_newest_artifact_and_uses_older_valid_snapshot(tmp_path, monkeypatch):
    path = tmp_path / ".trading_radar_thesen.json"
    valid_archive = _artifact_bytes()
    # The older candidate has run ID 100; the embedded manifest must identify it.
    raw = _register_bytes()
    register = persistence.parse_and_validate_register(raw)
    manifest = persistence.build_manifest(raw, register, origin="current_run")
    manifest.update({"repository": "owner/repo", "branch": "main", "run_id": "100"})
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(".trading_radar_thesen.json", raw)
        archive.writestr(".trading_radar_thesen.json.manifest.json", json.dumps(manifest).encode("utf-8"))
    valid_archive = archive_buffer.getvalue()
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "99999")
    monkeypatch.setattr(
        persistence,
        "find_latest_register_artifact",
        lambda *args, **kwargs: [
            ("https://example.invalid/corrupt.zip", "200"),
            ("https://example.invalid/older-valid.zip", "100"),
        ],
    )

    def download(url, token):
        if url.endswith("corrupt.zip"):
            return b"not-a-zip"
        return valid_archive

    monkeypatch.setattr(persistence, "download_artifact", download)
    assert persistence.command_restore(type("Args", (), {"register": str(path)})()) == 0
    assert persistence.verify_local_snapshot(path) == (True, "CACHE_VERIFIED")


def test_manifest_thesis_counts_are_checked(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"
    path.write_bytes(_register_bytes())
    persistence.command_prepare(type("Args", (), {"register": str(path)})())
    sidecar = persistence.manifest_path(path)
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    manifest["active_thesis_count"] = 99
    sidecar.write_text(json.dumps(manifest), encoding="utf-8")

    valid, status = persistence.verify_local_snapshot(path)
    assert valid is False
    assert "active_thesis_count" in status


@pytest.mark.parametrize(
    ("field", "wrong_value", "expected_repo", "expected_branch", "message"),
    [
        ("repository", "someone/else", "owner/repo", "main", "Repository"),
        ("branch", "wrong-branch", "owner/repo", "main", "Branch"),
    ],
)
def test_local_cache_rejects_wrong_present_origin_metadata(
    tmp_path, field, wrong_value, expected_repo, expected_branch, message
):
    path = tmp_path / ".trading_radar_thesen.json"
    path.write_bytes(_register_bytes())
    assert persistence.command_prepare(type("Args", (), {"register": str(path)})()) == 0

    sidecar = persistence.manifest_path(path)
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    # Keep the other origin field valid so each case isolates one mismatch.
    manifest["repository"] = expected_repo
    manifest["branch"] = expected_branch
    manifest[field] = wrong_value
    sidecar.write_text(json.dumps(manifest), encoding="utf-8")

    valid, status = persistence.verify_local_snapshot(
        path, expected_repo=expected_repo, expected_branch=expected_branch
    )

    assert valid is False
    assert message in status


def test_local_legacy_cache_allows_absent_origin_metadata_with_expected_context(tmp_path):
    path = tmp_path / ".trading_radar_thesen.json"
    path.write_bytes(_register_bytes())
    assert persistence.command_prepare(type("Args", (), {"register": str(path)})()) == 0

    sidecar = persistence.manifest_path(path)
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    manifest.pop("repository", None)
    manifest.pop("branch", None)
    sidecar.write_text(json.dumps(manifest), encoding="utf-8")

    assert persistence.verify_local_snapshot(
        path, expected_repo="owner/repo", expected_branch="main"
    ) == (True, "CACHE_VERIFIED")


def test_command_restore_passes_current_repository_and_branch_to_local_validation(
    tmp_path, monkeypatch
):
    path = tmp_path / ".trading_radar_thesen.json"
    captured = {}

    def fake_verify(register_path, *, expected_repo=None, expected_branch=None):
        captured.update(
            register_path=register_path,
            expected_repo=expected_repo,
            expected_branch=expected_branch,
        )
        return True, "CACHE_VERIFIED"

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setattr(persistence, "verify_local_snapshot", fake_verify)

    assert persistence.command_restore(type("Args", (), {"register": str(path)})()) == 0
    assert captured["expected_repo"] == "owner/repo"
    assert captured["expected_branch"] == "main"
