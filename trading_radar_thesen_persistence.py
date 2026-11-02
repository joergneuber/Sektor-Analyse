#!/usr/bin/env python3
"""Restore and verify the Trading Radar thesis register across GitHub Actions runs.

The Actions cache is the fast path. A per-run Actions artifact is the recovery
source when the cache is absent or invalid. Artifacts are retained for 90 days
by the workflow; this is durable across normal runner lifetimes, not permanent
archival storage.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import zipfile

REGISTER_DEFAULT = ".trading_radar_thesen.json"
MANIFEST_SUFFIX = ".manifest.json"
ARTIFACT_PREFIX = "trading-radar-thesen-register-"
MAX_REGISTER_BYTES = 5 * 1024 * 1024
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def manifest_path(register_path: Path) -> Path:
    return register_path.with_name(register_path.name + MANIFEST_SUFFIX)


def parse_and_validate_register(data: bytes) -> dict:
    if not data or len(data) > MAX_REGISTER_BYTES:
        raise ValueError("Thesenregister fehlt, ist leer oder überschreitet die Größenbegrenzung")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("theses"), list):
        raise ValueError("Thesenregister besitzt kein Objekt mit theses[]")
    if type(payload.get("schema_version")) is not int:
        raise ValueError("Thesenregister besitzt keine numerische schema_version")
    seen = set()
    active = 0
    for index, thesis in enumerate(payload["theses"]):
        if not isinstance(thesis, dict):
            raise ValueError(f"These[{index}] ist kein Objekt")
        thesis_id = thesis.get("thesis_id")
        if not isinstance(thesis_id, str) or not thesis_id.strip():
            raise ValueError(f"These[{index}] besitzt keine thesis_id")
        if thesis_id in seen:
            raise ValueError(f"Doppelte thesis_id im Register: {thesis_id}")
        seen.add(thesis_id)
        if type(thesis.get("version")) is not int or thesis["version"] < 1:
            raise ValueError(f"These {thesis_id} besitzt keine gültige Version")
        if thesis.get("active", True):
            active += 1
    if not payload["theses"] or active < 1:
        raise ValueError("Thesenregister enthält keine aktive These; Snapshot wird nicht persistiert")
    return payload


def build_manifest(register_bytes: bytes, register: dict, *, origin: str) -> dict:
    return {
        "manifest_schema_version": 1,
        "register_file": REGISTER_DEFAULT,
        "register_sha256": sha256_bytes(register_bytes),
        "register_schema_version": register["schema_version"],
        "thesis_count": len(register["theses"]),
        "active_thesis_count": sum(1 for item in register["theses"] if item.get("active", True)),
        "artifact_created_at": utc_now(),
        "origin": origin,
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "branch": os.environ.get("GITHUB_REF_NAME"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "commit_sha": os.environ.get("GITHUB_SHA"),
    }


def validate_manifest_against_register(
    manifest: dict,
    register: dict,
    register_bytes: bytes,
    *,
    source_run_id: str | None = None,
    expected_repo: str | None = None,
    expected_branch: str | None = None,
    require_origin_metadata: bool = False,
) -> None:
    """Validate register integrity and, for Artifacts, its exact GitHub origin.

    Local cache compatibility may tolerate absent origin fields because older
    cache entries predate the sidecar. Artifact restores are never legacy:
    repository, branch and source run ID must all be present and match exactly.
    """
    if not isinstance(manifest, dict) or manifest.get("manifest_schema_version") != 1:
        raise ValueError("Manifest hat ein ungültiges Schema")
    if manifest.get("register_sha256") != sha256_bytes(register_bytes):
        raise ValueError("Manifest-Hash stimmt nicht mit Register überein")
    if manifest.get("register_schema_version") != register.get("schema_version"):
        raise ValueError("Manifest und Register besitzen unterschiedliche Schema-Versionen")
    expected_thesis_count = len(register["theses"])
    expected_active_count = sum(1 for item in register["theses"] if item.get("active", True))
    if manifest.get("thesis_count") != expected_thesis_count:
        raise ValueError("Manifest thesis_count stimmt nicht mit Register überein")
    if manifest.get("active_thesis_count") != expected_active_count:
        raise ValueError("Manifest active_thesis_count stimmt nicht mit Register überein")

    repo = manifest.get("repository")
    branch = manifest.get("branch")
    run_id = manifest.get("run_id")
    if require_origin_metadata:
        if not isinstance(expected_repo, str) or not expected_repo.strip():
            raise ValueError("Erwartetes Repository fehlt für Artifact-Prüfung")
        if not isinstance(expected_branch, str) or not expected_branch.strip():
            raise ValueError("Erwarteter Branch fehlt für Artifact-Prüfung")
        if not isinstance(source_run_id, str) or not source_run_id.strip():
            raise ValueError("Erwartete Run-ID fehlt für Artifact-Prüfung")
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("Artifact-Manifest enthält keine Repository-Angabe")
        if repo != expected_repo:
            raise ValueError("Artifact-Manifest stammt aus einem anderen Repository")
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError("Artifact-Manifest enthält keine Branch-Angabe")
        if branch != expected_branch:
            raise ValueError("Artifact-Manifest stammt aus einem anderen Branch")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("Artifact-Manifest enthält keine Run-ID")
        if run_id != str(source_run_id):
            raise ValueError("Artifact-Manifest-Run-ID stimmt nicht mit dem Artifact-Lauf überein")
    else:
        # Local cache path: tolerate absent metadata for legacy sidecars, but
        # reject any present value that conflicts with the current context.
        if expected_repo and repo not in (None, expected_repo):
            raise ValueError("Manifest stammt aus einem anderen Repository")
        if expected_branch and branch not in (None, expected_branch):
            raise ValueError("Manifest stammt aus einem anderen Branch")
        if source_run_id is not None and run_id not in (None, str(source_run_id)):
            raise ValueError("Manifest-Run-ID stimmt nicht mit dem Artifact-Lauf überein")


def verify_local_snapshot(
    register_path: Path,
    *,
    expected_repo: str | None = None,
    expected_branch: str | None = None,
) -> tuple[bool, str]:
    if not register_path.is_file():
        return False, "REGISTER_MISSING"
    try:
        raw = register_path.read_bytes()
        register = parse_and_validate_register(raw)
    except Exception as exc:
        return False, f"REGISTER_INVALID:{type(exc).__name__}:{exc}"
    sidecar = manifest_path(register_path)
    if sidecar.exists():
        try:
            manifest = json.loads(sidecar.read_text(encoding="utf-8"))
            validate_manifest_against_register(
                manifest,
                register,
                raw,
                expected_repo=expected_repo,
                expected_branch=expected_branch,
            )
        except Exception as exc:
            message = str(exc)
            if "Hash stimmt" in message:
                return False, "MANIFEST_HASH_MISMATCH"
            if "Schema-Versionen" in message:
                return False, "MANIFEST_REGISTER_SCHEMA_MISMATCH"
            return False, f"MANIFEST_INVALID:{type(exc).__name__}:{exc}"
    else:
        # One-time compatibility path for a valid register produced before the
        # integrity sidecar existed. Do not pretend its original run is known.
        manifest = build_manifest(raw, register, origin="legacy_cache_revalidated")
        manifest["run_id"] = None
        manifest["commit_sha"] = None
        atomic_write(sidecar, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return True, "CACHE_LEGACY_REVALIDATED"
    return True, "CACHE_VERIFIED"


def api_json(url: str, token: str) -> dict:
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "trading-radar-thesis-persistence",
    })
    with urlopen(request, timeout=30) as response:
        data = response.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("GitHub-API-Antwort überschreitet Größenbegrenzung")
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Unerwartetes GitHub-API-Antwortformat")
    return payload


def find_latest_register_artifact(repo: str, branch: str, token: str, current_run_id: str | None) -> list[tuple[str, str]]:
    # Liefert alle passenden Kandidaten in absteigender Erstellungszeit.
    # command_restore prueft sie einzeln und faellt bei defekten ZIPs/Manifests
    # auf den jeweils älteren Kandidaten zurück.
    candidates: list[tuple[str, str, str]] = []
    for page in range(1, 6):
        query = urlencode({"branch": branch, "status": "completed", "per_page": 100, "page": page})
        runs_url = f"https://api.github.com/repos/{repo}/actions/workflows/main.yml/runs?{query}"
        runs_payload = api_json(runs_url, token)
        runs = runs_payload.get("workflow_runs", [])
        if not isinstance(runs, list):
            raise ValueError("GitHub lieferte keine workflow_runs-Liste")
        if not runs:
            break
        runs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        for run in runs:
            if not isinstance(run, dict) or run.get("status") != "completed":
                continue
            run_id = str(run.get("id", ""))
            if not run_id or run_id == str(current_run_id or ""):
                continue
            if run.get("path") != ".github/workflows/main.yml" or run.get("head_branch") != branch:
                continue
            artifacts_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
            artifacts_payload = api_json(artifacts_url, token)
            artifacts = artifacts_payload.get("artifacts", [])
            if not isinstance(artifacts, list):
                continue
            matching = [
                item for item in artifacts
                if isinstance(item, dict)
                and str(item.get("name", "")).startswith(ARTIFACT_PREFIX)
                and not item.get("expired", False)
                and item.get("archive_download_url")
            ]
            for item in matching:
                candidates.append((str(item.get("created_at", run.get("created_at", ""))),
                                   str(item["archive_download_url"]), run_id))
    unique_candidates = {}
    for created_at, url, run_id in candidates:
        unique_candidates[(url, run_id)] = max(created_at, unique_candidates.get((url, run_id), ""))
    ordered = sorted(
        ((created_at, url, run_id) for (url, run_id), created_at in unique_candidates.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    return [(url, run_id) for _, url, run_id in ordered]


class StripAuthorizationOnRedirect(HTTPRedirectHandler):
    """Do not forward the GitHub token to the external artifact-storage host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            for header_name in list(redirected.headers):
                if header_name.lower() in {"authorization", "proxy-authorization"}:
                    redirected.remove_header(header_name)
            for header_name in list(redirected.unredirected_hdrs):
                if header_name.lower() in {"authorization", "proxy-authorization"}:
                    redirected.remove_header(header_name)
        return redirected


def download_artifact(url: str, token: str) -> bytes:
    request = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "trading-radar-thesis-persistence",
    })
    # GitHub redirects artifact downloads to object storage. The bearer token
    # must be stripped before following that cross-host redirect.
    opener = build_opener(StripAuthorizationOnRedirect())
    with opener.open(request, timeout=60) as response:
        data = response.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Thesenregister-Artifact überschreitet Größenbegrenzung")
    return data


def restore_from_artifact(
    register_path: Path,
    archive_bytes: bytes,
    source_run_id: str,
    *,
    expected_repo: str,
    expected_branch: str,
) -> None:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        files_by_name = {}
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            basename = PurePosixPath(name).name
            if basename in {register_path.name, manifest_path(register_path).name}:
                if basename in files_by_name:
                    raise ValueError(f"Mehrdeutiger Artifact-Inhalt: {basename}")
                info = archive.getinfo(name)
                if info.file_size > MAX_REGISTER_BYTES:
                    raise ValueError(f"Artifact-Datei zu groß: {basename}")
                files_by_name[basename] = archive.read(name)
    register_bytes = files_by_name.get(register_path.name)
    manifest_bytes = files_by_name.get(manifest_path(register_path).name)
    if register_bytes is None or manifest_bytes is None:
        raise ValueError("Artifact enthält nicht beide Thesenregister-Dateien")
    register = parse_and_validate_register(register_bytes)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    validate_manifest_against_register(
        manifest,
        register,
        register_bytes,
        source_run_id=source_run_id,
        expected_repo=expected_repo,
        expected_branch=expected_branch,
        require_origin_metadata=True,
    )
    atomic_write(register_path, register_bytes)
    atomic_write(manifest_path(register_path), manifest_bytes)
    print(f"THESENREGISTER_ARTIFACT_RUN={source_run_id}")


def command_restore(args) -> int:
    path = Path(args.register)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "")
    valid, status = verify_local_snapshot(
        path,
        expected_repo=repo or None,
        expected_branch=branch or None,
    )
    if valid:
        print(f"TRADING_RADAR_THESEN_RESTORE={status}")
        return 0
    if path.exists():
        path.unlink()
    sidecar = manifest_path(path)
    if sidecar.exists():
        sidecar.unlink()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not repo or not branch or not token:
        print(f"TRADING_RADAR_THESEN_RESTORE=BASELINE_MISSING ({status}; GitHub-API-Kontext fehlt)")
        return 0
    try:
        artifacts = find_latest_register_artifact(repo, branch, token, os.environ.get("GITHUB_RUN_ID"))
        if not artifacts:
            print(f"TRADING_RADAR_THESEN_RESTORE=BASELINE_MISSING ({status}; kein gültiges Artifact gefunden)")
            return 0
        failures = []
        for url, source_run_id in artifacts:
            try:
                archive_bytes = download_artifact(url, token)
                restore_from_artifact(
                    path,
                    archive_bytes,
                    source_run_id,
                    expected_repo=repo,
                    expected_branch=branch,
                )
                print(f"TRADING_RADAR_THESEN_RESTORE=ARTIFACT_RESTORED|run={source_run_id}")
                return 0
            except Exception as exc:
                failures.append(f"run={source_run_id}:{type(exc).__name__}:{exc}")
                print(f"TRADING_RADAR_THESEN_ARTIFACT_SKIPPED=run={source_run_id}|{type(exc).__name__}:{exc}")
        detail = "; ".join(failures[-5:])
        print(f"TRADING_RADAR_THESEN_RESTORE=BASELINE_MISSING (kein gültiger Snapshot; geprüft={len(artifacts)}; {detail})")
        return 0
    except Exception as exc:
        print(f"TRADING_RADAR_THESEN_RESTORE=BASELINE_MISSING (Artifact-Suche fehlgeschlagen: {type(exc).__name__}: {exc})")
        return 0


def command_prepare(args) -> int:
    path = Path(args.register)
    if not path.is_file():
        print("TRADING_RADAR_THESEN_SNAPSHOT=SKIPPED_REGISTER_MISSING")
        return 0
    raw = path.read_bytes()
    register = parse_and_validate_register(raw)
    manifest = build_manifest(raw, register, origin="current_run")
    atomic_write(manifest_path(path), (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"TRADING_RADAR_THESEN_SNAPSHOT=READY|sha256={manifest['register_sha256']}|active={manifest['active_thesis_count']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("restore", "prepare"))
    parser.add_argument("--register", default=REGISTER_DEFAULT)
    args = parser.parse_args()
    return command_restore(args) if args.command == "restore" else command_prepare(args)


if __name__ == "__main__":
    raise SystemExit(main())
