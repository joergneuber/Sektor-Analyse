"""Regression tests for the immutable, integrity-checked OHLCV release workflow."""
from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from ohlcv_release_bundle import create_bundle

ROOT = Path(__file__).resolve().parents[1]
MAIN_WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
BUNDLE_TOOL = ROOT / "ohlcv_release_bundle.py"


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end, text.index(start))]


def _run_block(section: str) -> str:
    lines = section.splitlines()
    run_index = lines.index("        run: |")
    script_lines = []
    for line in lines[run_index + 1:]:
        if line and not line.startswith("          "):
            break
        script_lines.append(line[10:] if line.startswith("          ") else "")
    return "\n".join(script_lines)


def _restore_shell() -> str:
    return _run_block(_section(
        MAIN_WORKFLOW.read_text(encoding="utf-8"),
        "- name: Historische OHLCV-Datenbank und dynamisches Universum aus Dauerarchiv wiederherstellen",
        "- name: Historische OHLCV-Datenbank aktualisieren",
    ))


def _archive_shell() -> str:
    return _run_block(_section(
        MAIN_WORKFLOW.read_text(encoding="utf-8"),
        "- name: Finale OHLCV-Datenbank und Universum atomar im Dauerarchiv sichern und verifizieren",
        "- name: Trading-Radar-Laufmarker setzen",
    ))


def _make_db(path: Path, marker: str = "original") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS bars (ticker TEXT, date TEXT, close REAL)")
        connection.execute("INSERT INTO bars VALUES (?, ?, ?)", (marker, "2026-11-12", 123.45))


def _make_bundle(directory: Path, name: str, marker: str) -> Path:
    db = directory / f"{name}.sqlite"
    universe = directory / f"{name}.json"
    _make_db(db, marker)
    universe.write_text(json.dumps({"version": 1, "tickers": [marker + ".DE"]}), encoding="utf-8")
    bundle = directory / f"historical_ohlcv_bundle_{name}.zip"
    create_bundle(db, universe, bundle)
    return bundle


def _run_restore(tmp_path: Path, assets: list[dict], remote_assets: dict[str, Path], *, bootstrap="0", release_list_json=None, assets_json=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "api" ]]; then
  if [[ " $* " == *" --paginate "* ]]; then
    printf '%s' "$TEST_RELEASE_ASSETS"
  else
    printf '%s' '{"id":123}'
  fi
  exit 0
fi
case "$1 $2" in
  "release list") printf '%s' "$TEST_RELEASE_LIST" ;;
  "release download")
    shift 2
    pattern=""
    target="."
    while (($#)); do
      case "$1" in
        --pattern) pattern="$2"; shift 2 ;;
        --dir) target="$2"; shift 2 ;;
        --clobber) shift ;;
        --repo) shift 2 ;;
        *) shift ;;
      esac
    done
    [[ -n "$pattern" && -f "$TEST_REMOTE_DIR/$pattern" ]]
    cp "$TEST_REMOTE_DIR/$pattern" "$target/$pattern"
    ;;
  *) echo "unexpected gh command: $*" >&2; exit 30 ;;
esac
""", encoding="utf-8")
    gh.chmod(0o755)
    python_shim = bin_dir / "python"
    python_shim.write_text('#!/usr/bin/env bash\nexec "$PYTHON_REAL" -S "$@"\n', encoding="utf-8")
    python_shim.chmod(0o755)
    remote = tmp_path / "remote"
    remote.mkdir()
    (tmp_path / "ohlcv_release_bundle.py").write_bytes(BUNDLE_TOOL.read_bytes())
    shutil.copy2(ROOT / "ohlcv_ticker.py", tmp_path / "ohlcv_ticker.py")
    (tmp_path / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "get_gh_release_assets.sh", tmp_path / "scripts" / "get_gh_release_assets.sh")
    for name, source in remote_assets.items():
        (remote / name).write_bytes(source.read_bytes())
    assets_with_digests = []
    for asset in assets:
        item = dict(asset)
        remote_file = remote / item["name"]
        if remote_file.is_file():
            import hashlib
            item.setdefault("digest", "sha256:" + hashlib.sha256(remote_file.read_bytes()).hexdigest())
            item.setdefault("size", remote_file.stat().st_size)
            item.setdefault("state", "uploaded")
        assets_with_digests.append(item)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "PYTHON_REAL": sys.executable,
        "GITHUB_REPOSITORY": "joergneuber/Sektor-Analyse",
        "TEST_RELEASE_LIST": release_list_json if release_list_json is not None else json.dumps([{"tagName": "historical-ohlcv"}]),
        "TEST_RELEASE_ASSETS": assets_json if assets_json is not None else json.dumps([assets_with_digests]),
        "TEST_REMOTE_DIR": str(remote),
        "NMM_ALLOW_OHLCV_UNIVERSE_BOOTSTRAP": bootstrap,
        "NMM_ALLOW_OHLCV_LEGACY_MIGRATION": os.environ.get("TEST_LEGACY_MIGRATION", "0"),
    })
    (tmp_path / "historical_ohlcv.sqlite").write_bytes(b"stale-cache-placeholder")
    (tmp_path / "historical_ohlcv_universe.json").write_text('{"version":1,"tickers":["STALE"]}', encoding="utf-8")
    for suffix in ("-wal", "-shm", "-journal"):
        (tmp_path / f"historical_ohlcv.sqlite{suffix}").write_bytes(b"stale-sidecar")
    return subprocess.run(["bash", "-euo", "pipefail", "-c", _restore_shell()], cwd=tmp_path, env=env, text=True, capture_output=True)


def test_release_is_only_durable_ohlcv_store_and_no_ohlcv_cache_is_used():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    assert "Historische OHLCV-Datenbank wiederherstellen" not in text
    assert "historical_ohlcv.sqlite*" not in text
    assert "ohlcv-final-cache-save" not in text
    assert "OHLCV_CACHE_PERSISTENCE:" not in text
    assert "gh release upload historical-ohlcv \"$bundle_name\"" in text
    assert 'gh release upload historical-ohlcv "$bundle_name" --clobber' not in _archive_shell()


def test_restore_prefers_newest_valid_single_bundle_and_replaces_cache_pair(tmp_path):
    older = _make_bundle(tmp_path, "older", "OLD")
    newer = _make_bundle(tmp_path, "newer", "NEW")
    assets = [
        {"name": older.name, "created_at": "2026-11-10T10:00:00Z"},
        {"name": newer.name, "created_at": "2026-11-12T10:00:00Z"},
    ]
    result = _run_restore(tmp_path, assets, {older.name: older, newer.name: newer})
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "historical_ohlcv.sqlite") as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "NEW"
    assert json.loads((tmp_path / "historical_ohlcv_universe.json").read_text())["tickers"] == ["NEW.DE"]
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (tmp_path / f"historical_ohlcv.sqlite{suffix}").exists()


def test_restore_falls_back_to_previous_bundle_when_latest_is_corrupt(tmp_path):
    valid = _make_bundle(tmp_path, "valid", "GOOD")
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    assets = [
        {"name": valid.name, "created_at": "2026-11-10T10:00:00Z"},
        {"name": "historical_ohlcv_bundle_bad.zip", "created_at": "2026-11-12T10:00:00Z"},
    ]
    result = _run_restore(tmp_path, assets, {valid.name: valid, "historical_ohlcv_bundle_bad.zip": corrupt})
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "historical_ohlcv.sqlite") as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "GOOD"


def test_restore_legacy_two_asset_pair_is_checked_before_use(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    _make_db(legacy_db, "LEGACY")
    legacy_universe = tmp_path / "legacy.json"
    legacy_universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z"},
    ]
    os.environ["TEST_LEGACY_MIGRATION"] = "1"
    try:
        result = _run_restore(tmp_path, assets, {
            "historical_ohlcv.sqlite": legacy_db,
            "historical_ohlcv_universe.json": legacy_universe,
        })
    finally:
        os.environ.pop("TEST_LEGACY_MIGRATION", None)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "historical_ohlcv.sqlite") as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "LEGACY"
    assert "Legacy-OHLCV-Migration explizit freigegeben" in result.stdout


def test_restore_does_not_automatically_migrate_legacy_pair(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    _make_db(legacy_db, "LEGACY")
    legacy_universe = tmp_path / "legacy.json"
    legacy_universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z"},
    ]
    result = _run_restore(tmp_path, assets, {
        "historical_ohlcv.sqlite": legacy_db,
        "historical_ohlcv_universe.json": legacy_universe,
    })
    assert result.returncode != 0
    assert "Legacy-Migration" in result.stdout or "OHLCV-Assets sind im Release vorhanden" in result.stdout


def test_restore_legacy_migration_requires_remote_digests_even_with_opt_in(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    _make_db(legacy_db, "LEGACY")
    legacy_universe = tmp_path / "legacy.json"
    legacy_universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z", "digest": None},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z", "digest": None},
    ]
    os.environ["TEST_LEGACY_MIGRATION"] = "1"
    try:
        result = _run_restore(tmp_path, assets, {
            "historical_ohlcv.sqlite": legacy_db,
            "historical_ohlcv_universe.json": legacy_universe,
        })
    finally:
        os.environ.pop("TEST_LEGACY_MIGRATION", None)
    assert result.returncode != 0
    assert "Remote-Digests" in result.stdout


def test_restore_rejects_legacy_assets_uploaded_far_apart(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    _make_db(legacy_db, "LEGACY")
    legacy_universe = tmp_path / "legacy.json"
    legacy_universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-10T11:00:00Z"},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:00:00Z"},
    ]
    os.environ["TEST_LEGACY_MIGRATION"] = "1"
    try:
        result = _run_restore(tmp_path, assets, {
            "historical_ohlcv.sqlite": legacy_db,
            "historical_ohlcv_universe.json": legacy_universe,
        })
    finally:
        os.environ.pop("TEST_LEGACY_MIGRATION", None)
    assert result.returncode != 0
    assert "mehr als 120 Sekunden" in result.stdout


def test_restore_rejects_remote_digest_mismatch_and_uses_previous_bundle(tmp_path):
    older = _make_bundle(tmp_path, "older_digest", "OLD")
    newer = _make_bundle(tmp_path, "newer_digest", "NEW")
    assets = [
        {"name": older.name, "created_at": "2026-11-10T10:00:00Z"},
        {"name": newer.name, "created_at": "2026-11-12T10:00:00Z",
         "digest": "sha256:" + ("0" * 64)},
    ]
    result = _run_restore(tmp_path, assets, {older.name: older, newer.name: newer})
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "historical_ohlcv.sqlite") as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "OLD"


def test_restore_skips_bundle_without_remote_digest_and_uses_previous(tmp_path):
    older = _make_bundle(tmp_path, "older_missing_digest", "OLD")
    newer = _make_bundle(tmp_path, "newer_missing_digest", "NEW")
    assets = [
        {"name": older.name, "created_at": "2026-11-10T10:00:00Z"},
        {"name": newer.name, "created_at": "2026-11-12T10:00:00Z", "digest": None},
    ]
    result = _run_restore(tmp_path, assets, {older.name: older, newer.name: newer})
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / "historical_ohlcv.sqlite") as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "OLD"


def test_restore_fails_closed_if_release_has_no_valid_snapshot(tmp_path):
    result = _run_restore(tmp_path, [], {})
    assert result.returncode != 0
    assert "Kein remote-digest-verifiziertes gemeinsames OHLCV-Bundle" in result.stdout


def test_restore_rejects_malformed_release_list_even_with_bootstrap_enabled(tmp_path):
    result = _run_restore(tmp_path, [], {}, bootstrap="1", release_list_json="not-json")
    assert result.returncode != 0


def test_restore_rejects_malformed_release_asset_metadata_even_with_bootstrap_enabled(tmp_path):
    result = _run_restore(tmp_path, [], {}, bootstrap="1", assets_json="not-json")
    assert result.returncode != 0


def test_restore_rejects_legacy_pair_when_universe_asset_is_missing(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    _make_db(legacy_db, "LEGACY")
    result = _run_restore(tmp_path, [{"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"}], {"historical_ohlcv.sqlite": legacy_db})
    assert result.returncode != 0
    assert "OHLCV-Assets sind im Release vorhanden" in result.stdout


def test_restore_blocks_bootstrap_when_legacy_pair_is_incomplete(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    _make_db(legacy_db, "LEGACY")
    result = _run_restore(tmp_path, [{"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"}], {"historical_ohlcv.sqlite": legacy_db}, bootstrap="1")
    assert result.returncode != 0
    assert "OHLCV-Assets sind im Release vorhanden" in result.stdout


def test_restore_blocks_bootstrap_over_complete_legacy_pair_without_migration_opt_in(tmp_path):
    legacy_db = tmp_path / "legacy.sqlite"
    legacy_universe = tmp_path / "legacy.json"
    _make_db(legacy_db, "LEGACY")
    legacy_universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z"},
    ]
    os.environ.pop("TEST_LEGACY_MIGRATION", None)
    result = _run_restore(tmp_path, assets, {
        "historical_ohlcv.sqlite": legacy_db,
        "historical_ohlcv_universe.json": legacy_universe,
    }, bootstrap="1")
    assert result.returncode != 0
    assert "OHLCV-Assets sind im Release vorhanden" in result.stdout
    assert "expliziter Erstaufbau ist aktiviert" not in result.stdout


def test_restore_blocks_bootstrap_when_bundle_asset_exists_but_is_corrupt(tmp_path):
    corrupt = tmp_path / "historical_ohlcv_bundle_corrupt.zip"
    corrupt.write_bytes(b"not-a-valid-zip")
    result = _run_restore(
        tmp_path,
        [{"name": corrupt.name, "created_at": "2026-11-12T10:00:00Z"}],
        {corrupt.name: corrupt},
        bootstrap="1",
    )
    assert result.returncode != 0
    assert "OHLCV-Assets sind im Release vorhanden" in result.stdout


def test_restore_legacy_download_failure_is_blocking(tmp_path):
    result = _run_restore(
        tmp_path,
        [
            {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"},
            {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z"},
        ],
        {},
    )
    assert result.returncode != 0


def test_restore_shell_fails_closed_on_release_api_and_bundle_download_errors():
    restore = _restore_shell()
    assert 'gh release list --limit 1000 --json tagName' in restore
    assert 'bash scripts/get_gh_release_assets.sh "$GITHUB_REPOSITORY" historical-ohlcv' in restore
    assert 'python ohlcv_release_bundle.py extract --bundle' in restore
    assert 'remote_digest' in restore and 'sha256sum "$bundle_name"' in restore
    assert 'NMM_ALLOW_OHLCV_LEGACY_MIGRATION' in restore
    assert '[ "$release_bundle_count" -gt 0 ]' in restore
    assert '[ "$legacy_db_present" = "true" ]' in restore
    assert '[ "$legacy_universe_present" = "true" ]' in restore
    assert 'OHLCV-Assets sind im Release vorhanden' in restore
    assert 'exit 1' in restore


def test_final_open_position_verification_remains_after_technical_fallback():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    technical = text.index("run: python offene_positionen_check.py")
    verify = text.index("run: python historical_ohlcv.py verify-open-positions --positions-file Offene_Positionen.csv")
    benchmark = text.index("run: python performance_benchmark.py")
    assert technical < verify < benchmark


def test_ci_runs_existing_ohlcv_and_new_bundle_regressions():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    section = _section(text, "- name: Historische OHLCV Regressionstests", "# ============================================================\n      # EXTERNER DATENQUELLEN-TEST")
    for path in (
        "tests/test_historical_ohlcv.py",
        "tests/test_offene_positionen_check_history_source.py",
        "tests/test_ohlcv_release_bundle.py",
        "tests/test_ohlcv_release_consumers.py",
        "tests/test_workflow_dynamic_universe.py",
    ):
        assert path in section


def test_first_release_bootstrap_succeeds_only_when_explicitly_enabled(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\nif [[ "$1 $2" == "release list" ]]; then echo "[]"; else exit 2; fi\n', encoding="utf-8")
    gh.chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "NMM_ALLOW_OHLCV_UNIVERSE_BOOTSTRAP": "1"})
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", _restore_shell()], cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_restore_bootstrap_requires_explicit_switch(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\nif [[ "$1 $2" == "release list" ]]; then echo "[]"; else exit 2; fi\n', encoding="utf-8")
    gh.chmod(0o755)
    env = os.environ.copy()
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "NMM_ALLOW_OHLCV_UNIVERSE_BOOTSTRAP": "0"})
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", _restore_shell()], cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "Erstaufbau ist gesperrt" in result.stdout


def _run_archive(tmp_path: Path, *, fail_upload="0", seed_previous=False, seed_legacy=False, seed_count=0, seed_corrupt=False, bad_digest=False, missing_digest=False, fail_api=False, malformed_api=False, release_missing=False, fail_create=False, corrupt_db=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    (tmp_path / "ohlcv_release_bundle.py").write_bytes(BUNDLE_TOOL.read_bytes())
    shutil.copy2(ROOT / "ohlcv_ticker.py", tmp_path / "ohlcv_ticker.py")
    (tmp_path / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "get_gh_release_assets.sh", tmp_path / "scripts" / "get_gh_release_assets.sh")
    gh = bin_dir / "gh"
    gh.write_text(r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "api" ]]; then
  if [[ " $* " == *" --paginate "* ]]; then
    [[ "$TEST_FAIL_API" != "1" ]] || exit 22
    if [[ "$TEST_MALFORMED_API" == "1" ]]; then printf 'not-json'; exit 0; fi
    printf '[['; sep=""
    for f in "$TEST_REMOTE_DIR"/*; do
      [[ -f "$f" ]] || continue
      name="$(basename "$f")"
      digest="$(sha256sum "$f" | awk '{print $1}')"
      size="$(stat -c '%s' "$f")"
      created_at="2026-11-10T12:00:00Z"
      [[ "$name" != "historical_ohlcv_bundle_987654_1.zip" ]] || created_at="2026-11-12T12:00:00Z"
      [[ "$TEST_BAD_DIGEST" != "1" ]] || digest="0000000000000000000000000000000000000000000000000000000000000000"
      if [[ "$TEST_MISSING_DIGEST" == "1" && "$name" == "historical_ohlcv_bundle_987654_1.zip" ]]; then
        printf '%s{"name":"%s","size":%s,"state":"uploaded","created_at":"%s"}' "$sep" "$name" "$size" "$created_at"
      else
        printf '%s{"name":"%s","size":%s,"state":"uploaded","digest":"sha256:%s","created_at":"%s"}' "$sep" "$name" "$size" "$digest" "$created_at"
      fi
      sep=,
    done
    printf ']]'
  else
    printf '{"id":123}'
  fi
  exit 0
fi
command="$1 $2"
shift 2
case "$command" in
  "release view") [[ "$TEST_RELEASE_MISSING" != "1" ]] ;;
  "release create") [[ "$TEST_FAIL_CREATE" != "1" ]] ;;
  "release upload")
    [[ "$TEST_FAIL_UPLOAD" != "1" ]] || exit 21
    shift
    cp "$1" "$TEST_REMOTE_DIR/$(basename "$1")"
    ;;
  "release download")
    pattern=""
    while (($#)); do
      case "$1" in
        --pattern) pattern="$2"; shift 2 ;;
        --dir) target="$2"; shift 2 ;;
        --clobber) shift ;;
        *) shift ;;
      esac
    done
    [[ -f "$TEST_REMOTE_DIR/$pattern" ]] || exit 23
    mkdir -p "$target"
    cp "$TEST_REMOTE_DIR/$pattern" "$target/$pattern"
    ;;
  "release delete-asset") shift; rm -f "$TEST_REMOTE_DIR/$1" ;;
  *) echo "unexpected gh command: $command $*" >&2; exit 30 ;;
esac
""", encoding="utf-8")
    gh.chmod(0o755)
    python_shim = bin_dir / "python"
    python_shim.write_text('#!/usr/bin/env bash\nexec "$PYTHON_REAL" -S "$@"\n', encoding="utf-8")
    python_shim.chmod(0o755)
    if seed_previous:
        previous = _make_bundle(tmp_path, "previous", "PREVIOUS")
        (remote / "historical_ohlcv_bundle_previous_1.zip").write_bytes(previous.read_bytes())
    for index in range(seed_count):
        previous = _make_bundle(tmp_path, f"previous_{index}", f"PREVIOUS{index}")
        (remote / f"historical_ohlcv_bundle_previous_{index}.zip").write_bytes(previous.read_bytes())
    if seed_corrupt:
        (remote / "historical_ohlcv_bundle_corrupt_1.zip").write_bytes(b"corrupt zip payload")
    if seed_legacy:
        legacy_db = tmp_path / "legacy.sqlite"
        _make_db(legacy_db, "LEGACY")
        (remote / "historical_ohlcv.sqlite").write_bytes(legacy_db.read_bytes())
        (remote / "historical_ohlcv_universe.json").write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    db = tmp_path / "historical_ohlcv.sqlite"
    if corrupt_db:
        db.write_bytes(b"not a sqlite database")
    else:
        _make_db(db, "CURRENT")
    (tmp_path / "historical_ohlcv_universe.json").write_text('{"version":1,"tickers":["CURRENT.DE"]}', encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "PYTHON_REAL": sys.executable,
        "GITHUB_REPOSITORY": "joergneuber/Sektor-Analyse",
        "GITHUB_RUN_ID": "987654",
        "GITHUB_RUN_ATTEMPT": "1",
        "TEST_REMOTE_DIR": str(remote),
        "TEST_FAIL_UPLOAD": fail_upload,
        "TEST_BAD_DIGEST": "1" if bad_digest else "0",
        "TEST_MISSING_DIGEST": "1" if missing_digest else "0",
        "TEST_FAIL_API": "1" if fail_api else "0",
        "TEST_MALFORMED_API": "1" if malformed_api else "0",
        "TEST_RELEASE_MISSING": "1" if release_missing else "0",
        "TEST_FAIL_CREATE": "1" if fail_create else "0",
    })
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", _archive_shell()], cwd=tmp_path, env=env, text=True, capture_output=True)
    return result, remote


def test_archive_uploads_one_immutable_bundle_and_verifies_remote_digest(tmp_path):
    result, remote = _run_archive(tmp_path)
    assert result.returncode == 0, result.stderr
    bundles = list(remote.glob("historical_ohlcv_bundle_*.zip"))
    assert len(bundles) == 1
    assert "OHLCV-Release-Verifikation PASS" in result.stdout
    with zipfile.ZipFile(bundles[0]) as archive:
        assert set(archive.namelist()) == {"historical_ohlcv.sqlite", "historical_ohlcv_universe.json", "manifest.json"}


def test_failed_new_upload_keeps_previous_release_snapshot(tmp_path):
    result, remote = _run_archive(tmp_path, fail_upload="1", seed_previous=True)
    assert result.returncode != 0
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 1


def test_remote_digest_failure_keeps_previous_snapshot_and_does_not_prune(tmp_path):
    result, remote = _run_archive(tmp_path, seed_previous=True, bad_digest=True)
    assert result.returncode != 0
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 2


def test_missing_remote_digest_keeps_previous_snapshot_and_does_not_prune(tmp_path):
    result, remote = _run_archive(tmp_path, seed_previous=True, missing_digest=True)
    assert result.returncode != 0
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 2


def test_first_bundle_keeps_legacy_pair_until_second_verified_bundle_exists(tmp_path):
    result, remote = _run_archive(tmp_path, seed_legacy=True)
    assert result.returncode == 0, result.stderr
    assert (remote / "historical_ohlcv.sqlite").exists()
    assert (remote / "historical_ohlcv_universe.json").exists()
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 1


def test_cleanup_preserves_legacy_and_all_bundle_assets_until_two_valid_versions_exist(tmp_path):
    result, remote = _run_archive(tmp_path, seed_legacy=True, seed_corrupt=True)
    assert result.returncode == 0, result.stderr
    assert (remote / "historical_ohlcv.sqlite").exists()
    assert (remote / "historical_ohlcv_universe.json").exists()
    assert (remote / "historical_ohlcv_bundle_corrupt_1.zip").exists()
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 2


def test_legacy_pair_removed_only_after_two_verified_bundles_exist(tmp_path):
    result, remote = _run_archive(tmp_path, seed_previous=True, seed_legacy=True)
    assert result.returncode == 0, result.stderr
    assert not (remote / "historical_ohlcv.sqlite").exists()
    assert not (remote / "historical_ohlcv_universe.json").exists()
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 2


def test_successful_publish_keeps_only_current_and_one_rollback_bundle(tmp_path):
    result, remote = _run_archive(tmp_path, seed_count=3)
    assert result.returncode == 0, result.stderr
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 2


def test_cleanup_keeps_two_valid_bundles_and_does_not_count_corrupt_newer_asset(tmp_path):
    result, remote = _run_archive(tmp_path, seed_previous=True, seed_corrupt=True)
    assert result.returncode == 0, result.stderr
    bundles = list(remote.glob("historical_ohlcv_bundle_*.zip"))
    assert len(bundles) == 2
    assert all("corrupt" not in item.name for item in bundles)


def test_release_api_failure_aborts_after_upload_without_pruning(tmp_path):
    result, remote = _run_archive(tmp_path, seed_previous=True, fail_api=True)
    assert result.returncode != 0
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 2


def test_malformed_release_api_json_aborts_without_pruning(tmp_path):
    result, remote = _run_archive(tmp_path, seed_previous=True, malformed_api=True)
    assert result.returncode != 0
    assert len(list(remote.glob("historical_ohlcv_bundle_*.zip"))) == 2


def test_release_creation_failure_blocks_archive_upload(tmp_path):
    result, remote = _run_archive(tmp_path, release_missing=True, fail_create=True)
    assert result.returncode != 0
    assert not list(remote.glob("*.zip"))


def test_sqlite_integrity_failure_blocks_release_upload(tmp_path):
    result, remote = _run_archive(tmp_path, corrupt_db=True)
    assert result.returncode != 0
    assert not list(remote.glob("*.zip"))


def test_release_step_runs_full_integrity_check_and_reports_persistence_failure_without_suppressing_radar():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    section = _section(
        text,
        "- name: Finale OHLCV-Datenbank und Universum atomar im Dauerarchiv sichern und verifizieren",
        "- name: Trading-Radar-Laufmarker setzen",
    )
    assert "if: ${{ always() && hashFiles('historical_ohlcv.sqlite') != '' && hashFiles('historical_ohlcv_universe.json') != '' }}" in section
    assert "continue-on-error: true" in section
    assert "ohlcv_release_bundle.py create" in section
    assert "ohlcv_release_bundle.py verify --bundle" in section
    assert "valid_bundle_names" in section
    assert "zwei inhaltlich und remote verifizierte Bundles" in section
    assert "PRAGMA integrity_check" in (ROOT / "ohlcv_release_bundle.py").read_text(encoding="utf-8")
    assert 'gh release upload historical-ohlcv "$bundle_name" --clobber' not in section
    assert "gh release delete-asset" in section
    assert 'bash scripts/get_gh_release_assets.sh "$GITHUB_REPOSITORY" historical-ohlcv' in section
    assert "MAX_RELEASE_ASSET_BYTES=$((2 * 1024 * 1024 * 1024 - 1))" in section
    assert "bundle_size=\"$(stat -c '%s' \"$bundle_name\")\"" in section
    assert section.index("bundle_size=") < section.index('gh release upload historical-ohlcv "$bundle_name"')
    # The workflow continues to produce a diagnostic report, while the Radar
    # receives the failed step outcome and must downgrade the report to NICHT_FINAL.
    assert "OHLCV_RELEASE_PERSISTENCE:" in text
    assert "continue-on-error: true" in section


def test_final_sync_verification_archive_and_radar_order_is_preserved():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    queue_import = text.index("- name: OHLCV Pending-Queues aus Einzel-Checks importieren")
    initial_update = text.index("- name: Historische OHLCV-Datenbank aktualisieren")
    final_sync = text.index("- name: Finale OHLCV-Synchronisierung, Coverage und Positionsprüfung")
    release_archive = text.index("- name: Finale OHLCV-Datenbank und Universum atomar im Dauerarchiv sichern und verifizieren")
    radar = text.index("- name: Trading-Radar-Laufmarker setzen")
    assert queue_import < initial_update < final_sync < release_archive < radar
    final_sync_section = _section(
        text,
        "- name: Finale OHLCV-Synchronisierung, Coverage und Positionsprüfung",
        "- name: Finale OHLCV-Datenbank und Universum atomar im Dauerarchiv sichern und verifizieren",
    )
    assert "if: always()" in final_sync_section
    assert "continue-on-error: true" in final_sync_section
    assert "python historical_ohlcv.py update --strict" in final_sync_section
    assert "update_rc=$?" in final_sync_section
    assert "coverage_rc=$?" in final_sync_section
    assert "positions_rc=$?" in final_sync_section
    assert 'sync_status="complete"' in final_sync_section
    assert 'sync_status="partial"' in final_sync_section
    assert 'sync_status="failed"' in final_sync_section
    assert "OHLCV_QUEUE_IMPORT:" in text


def test_open_positions_are_synced_after_tracker_and_before_technical_check():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    tracker = text.index("run: python positionen_tracker.py")
    sync = text.index("run: python historical_ohlcv.py sync-open-positions --positions-file Offene_Positionen.csv")
    technical_check = text.index("run: python offene_positionen_check.py")
    assert tracker < sync < technical_check


def test_ohlcv_bundle_regressions_are_executed_in_ci():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    section = _section(text, "- name: Historische OHLCV Regressionstests", "# ============================================================\n      # EXTERNER DATENQUELLEN-TEST")
    assert "tests/test_ohlcv_release_bundle.py" in section
    assert "tests/test_workflow_dynamic_universe.py" in section
