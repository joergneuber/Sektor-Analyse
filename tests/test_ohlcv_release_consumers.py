from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from ohlcv_release_bundle import create_bundle

ROOT = Path(__file__).resolve().parents[1]
RESTORE_SCRIPT = ROOT / "scripts" / "restore_ohlcv_release_bundle.sh"
CONSUMER_WORKFLOWS = (
    ".github/workflows/us_provider_coverage_iex_sip.yml",
    ".github/workflows/trendwende_backtest.yml",
    ".github/workflows/us_split_adjustment_diagnostic.yml",
    ".github/workflows/us_provider_coverage.yml",
    ".github/workflows/diagnose_historical_ohlcv_release.yml",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _db(path: Path, marker: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE bars (ticker TEXT, close REAL)")
        connection.execute("INSERT INTO bars VALUES (?, ?)", (marker, 123.0))


def _bundle(tmp_path: Path, label: str, marker: str) -> Path:
    db = tmp_path / f"{label}.sqlite"
    universe = tmp_path / f"{label}.json"
    _db(db, marker)
    universe.write_text(json.dumps({"version": 1, "tickers": [marker + ".DE"]}), encoding="utf-8")
    target = tmp_path / f"historical_ohlcv_bundle_{label}.zip"
    create_bundle(db, universe, target)
    return target


def _run_restore(tmp_path: Path, assets: list[dict], files: dict[str, Path], second_assets: list[dict] | None = None, allow_legacy: bool = False, assets_json: str | None = None):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "get_gh_release_assets.sh", tmp_path / "scripts" / "get_gh_release_assets.sh")
    for name, source in files.items():
        shutil.copy2(source, remote / name)

    gh = bindir / "gh"
    gh.write_text(r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "api" ]]; then
  if [[ " $* " == *" --paginate "* ]]; then
    if [[ -n "${TEST_ASSET_PAGES_SECOND:-}" && -f "$TEST_API_COUNTER" ]]; then
      printf '%s' "$TEST_ASSET_PAGES_SECOND"
    else
      touch "$TEST_API_COUNTER"
      printf '%s' "$TEST_ASSET_PAGES"
    fi
  else
    printf '%s' '{"id":123}'
  fi
elif [[ "$1 $2" == "release download" ]]; then
  shift 2
  name=""
  target="."
  while (($#)); do
    case "$1" in
      --pattern) name="$2"; shift 2 ;;
      --dir) target="$2"; shift 2 ;;
      --repo) shift 2 ;;
      *) shift ;;
    esac
  done
  [[ -f "$TEST_REMOTE_DIR/$name" ]]
  cp "$TEST_REMOTE_DIR/$name" "$target/$name"
else
  echo "unexpected gh args: $*" >&2
  exit 30
fi
""", encoding="utf-8")
    gh.chmod(0o755)

    fake_python = bindir / "python"
    fake_python.write_text(r"""#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == "ohlcv_release_bundle.py" ]] || exit 40
command="$2"
shift 2
if [[ "$command" == "extract" ]]; then
  bundle=""; db="historical_ohlcv.sqlite"; universe="historical_ohlcv_universe.json"
  while (($#)); do
    case "$1" in
      --bundle) bundle="$2"; shift 2 ;;
      --db) db="$2"; shift 2 ;;
      --universe) universe="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  unzip -p "$bundle" historical_ohlcv.sqlite > "$db"
  unzip -p "$bundle" historical_ohlcv_universe.json > "$universe"
elif [[ "$command" == "restore-files" ]]; then
  source_db=""; source_universe=""; db="historical_ohlcv.sqlite"; universe="historical_ohlcv_universe.json"
  while (($#)); do
    case "$1" in
      --source-db) source_db="$2"; shift 2 ;;
      --source-universe) source_universe="$2"; shift 2 ;;
      --db) db="$2"; shift 2 ;;
      --universe) universe="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  cp "$source_db" "$db"
  cp "$source_universe" "$universe"
elif [[ "$command" == "verify-files" ]]; then
  db="historical_ohlcv.sqlite"; universe="historical_ohlcv_universe.json"
  while (($#)); do
    case "$1" in
      --db) db="$2"; shift 2 ;;
      --universe) universe="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  [[ -s "$db" && -s "$universe" ]]
else
  exit 41
fi
""", encoding="utf-8")
    fake_python.chmod(0o755)

    release_assets = []
    for asset in assets:
        item = dict(asset)
        source = remote / item["name"]
        if source.is_file():
            item.setdefault("digest", "sha256:" + _sha(source))
            item.setdefault("size", source.stat().st_size)
            item.setdefault("state", "uploaded")
        release_assets.append(item)
    second_release_assets = []
    for asset in (second_assets or []):
        item = dict(asset)
        source = remote / item["name"]
        if source.is_file():
            item.setdefault("digest", "sha256:" + _sha(source))
            item.setdefault("size", source.stat().st_size)
            item.setdefault("state", "uploaded")
        second_release_assets.append(item)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "NMM_ALLOW_OHLCV_LEGACY_MIGRATION": "1" if allow_legacy else "0",
        "GITHUB_REPOSITORY": "joergneuber/Sektor-Analyse",
        "TEST_REMOTE_DIR": str(remote),
        "TEST_ASSET_PAGES": assets_json if assets_json is not None else json.dumps([release_assets]),
        "TEST_ASSET_PAGES_SECOND": json.dumps([second_release_assets]) if second_assets is not None else "",
        "TEST_API_COUNTER": str(tmp_path / "api-counter"),
        "NMM_HISTORICAL_OHLCV_DB": "historical_ohlcv.sqlite",
        "NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE": "historical_ohlcv_universe.json",
    })
    return subprocess.run(
        ["bash", str(RESTORE_SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def _read_marker(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT ticker FROM bars").fetchone()[0]


def test_all_release_consumers_use_the_shared_verified_restore():
    for relative in CONSUMER_WORKFLOWS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "scripts/restore_ohlcv_release_bundle.sh" in text, relative
        assert "gh release download historical-ohlcv" not in text, relative
        assert "historical_ohlcv_bundle_" not in text, relative
        assert "contents: read" in text, relative
        assert "contents: write" not in text, relative


def test_einzel_check_no_longer_writes_legacy_universe_asset():
    text = (ROOT / ".github/workflows/einzel_check.yml").read_text(encoding="utf-8")
    assert "gh release upload historical-ohlcv" not in text
    assert "historical_ohlcv_universe.json" not in text
    assert "contents: read" in text
    assert "contents: write" not in text
    assert "actions/upload-artifact@v6" in text


def test_restore_prefers_newest_valid_bundle_and_extracts_pair(tmp_path):
    older = _bundle(tmp_path, "older", "OLD")
    newer = _bundle(tmp_path, "newer", "NEW")
    assets = [
        {"name": older.name, "created_at": "2026-11-10T10:00:00Z"},
        {"name": newer.name, "created_at": "2026-11-12T10:00:00Z"},
    ]
    result = _run_restore(tmp_path, assets, {older.name: older, newer.name: newer})
    assert result.returncode == 0, result.stderr
    assert _read_marker(tmp_path / "historical_ohlcv.sqlite") == "NEW"
    assert json.loads((tmp_path / "historical_ohlcv_universe.json").read_text())["tickers"] == ["NEW.DE"]
    assert "OHLCV_RELEASE_RESTORE=PASS" in result.stdout


def test_restore_falls_back_to_previous_bundle_if_latest_is_corrupt(tmp_path):
    older = _bundle(tmp_path, "older", "OLD")
    corrupt = tmp_path / "historical_ohlcv_bundle_corrupt.zip"
    corrupt.write_bytes(b"not-a-zip")
    assets = [
        {"name": older.name, "created_at": "2026-11-10T10:00:00Z"},
        {"name": corrupt.name, "created_at": "2026-11-12T10:00:00Z"},
    ]
    result = _run_restore(tmp_path, assets, {older.name: older, corrupt.name: corrupt})
    assert result.returncode == 0, result.stderr
    assert _read_marker(tmp_path / "historical_ohlcv.sqlite") == "OLD"


def test_restore_refreshes_release_metadata_if_bundle_appears_during_legacy_prune_race(tmp_path):
    bundle = _bundle(tmp_path, "published_during_restore", "FRESH")
    second_assets = [{
        "name": bundle.name,
        "created_at": "2026-11-13T00:00:00Z",
        "digest": "sha256:" + _sha(bundle),
    }]
    result = _run_restore(tmp_path, [], {bundle.name: bundle}, second_assets=second_assets)
    assert result.returncode == 0, result.stderr
    assert _read_marker(tmp_path / "historical_ohlcv.sqlite") == "FRESH"
    assert "OHLCV_RELEASE_RESTORE=PASS" in result.stdout


def test_restore_uses_only_digest_verified_legacy_pair_during_migration(tmp_path):
    db = tmp_path / "legacy.sqlite"
    universe = tmp_path / "legacy.json"
    _db(db, "LEGACY")
    universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z"},
    ]
    result = _run_restore(tmp_path, assets, {
        "historical_ohlcv.sqlite": db,
        "historical_ohlcv_universe.json": universe,
    }, allow_legacy=True)
    assert result.returncode == 0, result.stderr
    assert _read_marker(tmp_path / "historical_ohlcv.sqlite") == "LEGACY"
    assert json.loads((tmp_path / "historical_ohlcv_universe.json").read_text())["tickers"] == ["LEGACY.DE"]
    assert "legacy-pair=remote-digest-verified" in result.stdout


def test_restore_rejects_legacy_pair_with_digest_mismatch(tmp_path):
    db = tmp_path / "legacy.sqlite"
    universe = tmp_path / "legacy.json"
    _db(db, "LEGACY")
    universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z",
         "digest": "sha256:" + "0" * 64},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z"},
    ]
    result = _run_restore(tmp_path, assets, {
        "historical_ohlcv.sqlite": db,
        "historical_ohlcv_universe.json": universe,
    }, allow_legacy=True)
    assert result.returncode != 0
    assert not (tmp_path / "historical_ohlcv.sqlite").exists()


def test_restore_rejects_legacy_pair_with_timestamp_skew_over_120_seconds(tmp_path):
    db = tmp_path / "legacy.sqlite"
    universe = tmp_path / "legacy.json"
    _db(db, "LEGACY")
    universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-10T11:00:00Z"},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:00:00Z"},
    ]
    result = _run_restore(tmp_path, assets, {
        "historical_ohlcv.sqlite": db,
        "historical_ohlcv_universe.json": universe,
    }, allow_legacy=True)
    assert result.returncode != 0
    assert "mehr als 120 Sekunden" in result.stdout



def test_legacy_fallback_uses_transactional_restore_files_command():
    script = RESTORE_SCRIPT.read_text(encoding="utf-8")
    assert "ohlcv_release_bundle.py restore-files" in script
    assert "--source-db" in script
    assert "--source-universe" in script
    assert 'install -m 0644 "$tmp_dir/historical_ohlcv.sqlite" "$db_path"' not in script


def test_shared_asset_listing_helper_uses_paginated_assets_api():
    text = (ROOT / "scripts/get_gh_release_assets.sh").read_text(encoding="utf-8")
    assert "releases/${release_id}/assets?per_page=100" in text
    assert "--paginate --slurp" in text
    assert "[ .[][]" in text
    assert "duplicate names" in text
    assert ".size | type == \"number\"" in text


def test_legacy_restore_requires_explicit_operator_flag(tmp_path):
    db = tmp_path / "legacy.sqlite"
    universe = tmp_path / "legacy.json"
    _db(db, "LEGACY")
    universe.write_text('{"version":1,"tickers":["LEGACY.DE"]}', encoding="utf-8")
    assets = [
        {"name": "historical_ohlcv.sqlite", "updated_at": "2026-11-12T11:31:07Z"},
        {"name": "historical_ohlcv_universe.json", "updated_at": "2026-11-12T11:31:08Z"},
    ]
    result = _run_restore(tmp_path, assets, {
        "historical_ohlcv.sqlite": db,
        "historical_ohlcv_universe.json": universe,
    })
    assert result.returncode != 0
    assert "Legacy-Fallback gesperrt" in (result.stdout + result.stderr)
    assert not (tmp_path / "historical_ohlcv.sqlite").exists()


def test_all_restore_workflows_expose_explicit_legacy_flag():
    for relative in (
        ".github/workflows/main.yml",
        ".github/workflows/us_provider_coverage_iex_sip.yml",
        ".github/workflows/trendwende_backtest.yml",
        ".github/workflows/us_split_adjustment_diagnostic.yml",
        ".github/workflows/us_provider_coverage.yml",
        ".github/workflows/diagnose_historical_ohlcv_release.yml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "NMM_ALLOW_OHLCV_LEGACY_MIGRATION" in text, relative


def test_restore_rejects_bundle_with_remote_size_over_github_limit(tmp_path):
    bundle = _bundle(tmp_path, "oversized_metadata", "OVERSIZE")
    assets = [{
        "name": bundle.name,
        "created_at": "2026-11-12T10:00:00Z",
        "size": 2 * 1024 * 1024 * 1024,
    }]
    result = _run_restore(tmp_path, assets, {bundle.name: bundle})
    assert result.returncode != 0
    assert "GitHub-Grenze" in result.stdout
    assert not (tmp_path / "historical_ohlcv.sqlite").exists()


def test_restore_rejects_download_size_different_from_remote_metadata(tmp_path):
    bundle = _bundle(tmp_path, "size_mismatch", "SIZE")
    assets = [{
        "name": bundle.name,
        "created_at": "2026-11-12T10:00:00Z",
        "size": bundle.stat().st_size + 1,
    }]
    result = _run_restore(tmp_path, assets, {bundle.name: bundle})
    assert result.returncode != 0
    assert "Größe von den Release-Metadaten ab" in result.stdout
    assert not (tmp_path / "historical_ohlcv.sqlite").exists()


def test_restore_finds_bundle_on_second_paginated_asset_page(tmp_path):
    bundle = _bundle(tmp_path, "second_page", "PAGE2")
    asset = {
        "name": bundle.name,
        "created_at": "2026-11-13T00:00:00Z",
        "digest": "sha256:" + _sha(bundle),
        "size": bundle.stat().st_size,
        "state": "uploaded",
    }
    pages = json.dumps([[], [asset]])
    result = _run_restore(tmp_path, [], {bundle.name: bundle}, assets_json=pages)
    assert result.returncode == 0, result.stderr
    assert _read_marker(tmp_path / "historical_ohlcv.sqlite") == "PAGE2"


def test_only_main_on_main_branch_can_write_the_historical_ohlcv_release():
    main = (ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8")
    assert "if: github.ref == 'refs/heads/main'" in main
    assert main.count('gh release upload historical-ohlcv "$bundle_name"') == 1
    for path in ROOT.glob(".github/workflows/*.yml"):
        text = path.read_text(encoding="utf-8")
        if path.name != "main.yml":
            assert "gh release upload historical-ohlcv" not in text, path.name
