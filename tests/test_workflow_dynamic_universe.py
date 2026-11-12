"""Regression tests for the persistent Einzel-Check OHLCV universe workflow."""
from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MAIN_WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"


def _section(text: str, start: str, end: str) -> str:
    a = text.index(start)
    b = text.index(end, a)
    return text[a:b]


def test_dynamic_universe_is_not_restored_from_or_saved_to_actions_cache():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    restore = _section(
        text,
        "- name: Historische OHLCV-Datenbank wiederherstellen",
        "- name: Historische OHLCV-Datenbank und dynamisches Universum aus Dauerarchiv wiederherstellen",
    )
    save = _section(
        text,
        "- name: Finale OHLCV-Datenbank im Actions-Cache sichern",
        "- name: Finale OHLCV-Datenbank und Universum im Dauerarchiv sichern und SHA-256 verifizieren",
    )
    assert "historical_ohlcv_universe.json" not in restore
    # Das Universum wird ausschließlich aus dem autoritativen Release geladen.
    # Es darf keinen separaten Universe-Cache geben.
    assert "historical_ohlcv_universe.json" not in save


def _restore_shell() -> str:
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    restore = _section(
        text,
        "- name: Historische OHLCV-Datenbank und dynamisches Universum aus Dauerarchiv wiederherstellen",
        "- name: Historische OHLCV-Datenbank aktualisieren",
    )
    lines = restore.splitlines()
    run_index = lines.index("        run: |")
    script_lines = []
    for line in lines[run_index + 1:]:
        if line and not line.startswith("          "):
            break
        script_lines.append(line[10:] if line.startswith("          ") else "")
    return "\n".join(script_lines)


def _run_restore(tmp_path, *, release_list, assets, bootstrap="0", fail_download="0"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -e
case "$1 $2" in
  "release list") printf '%s' "$TEST_RELEASE_LIST" ;;
  "release view") printf '%s' "$TEST_RELEASE_ASSETS" ;;
  "release download")
    if [[ "$*" == *"historical_ohlcv_universe.json"* ]]; then
      if [[ "$TEST_FAIL_DOWNLOAD" == "1" ]]; then exit 9; fi
      printf '{"version":1,"tickers":["KEPT.DE"]}\\n' > historical_ohlcv_universe.json
    fi
    ;;
  *) echo "unexpected gh command: $*" >&2; exit 10 ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    (tmp_path / "historical_ohlcv.sqlite").write_bytes(b"db-placeholder")
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "TEST_RELEASE_LIST": release_list,
        "TEST_RELEASE_ASSETS": assets,
        "TEST_FAIL_DOWNLOAD": fail_download,
        "NMM_ALLOW_OHLCV_UNIVERSE_BOOTSTRAP": bootstrap,
    })
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _restore_shell()],
        cwd=tmp_path, env=env, text=True, capture_output=True,
    )
    return result


def test_dynamic_universe_restore_fails_closed_on_api_or_download_errors():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    restore = _section(
        text,
        "- name: Historische OHLCV-Datenbank und dynamisches Universum aus Dauerarchiv wiederherstellen",
        "- name: Historische OHLCV-Datenbank aktualisieren",
    )
    assert "NMM_ALLOW_OHLCV_UNIVERSE_BOOTSTRAP" in restore
    assert 'gh release list --limit 1000 --json tagName' in restore
    assert 'gh release view historical-ohlcv --json assets' in restore
    assert 'gh release download historical-ohlcv --pattern historical_ohlcv_universe.json' in restore
    assert 'test -s historical_ohlcv_universe.json' in restore
    dynamic_restore = restore[restore.index("rm -f historical_ohlcv_universe.json"):]
    assert 'jq -e' in dynamic_restore
    assert 'exit 1' in dynamic_restore


def test_existing_release_without_universe_asset_aborts_by_default(tmp_path):
    result = _run_restore(
        tmp_path,
        release_list='[{"tagName":"historical-ohlcv"}]',
        assets='{"assets":[{"name":"historical_ohlcv.sqlite"}]}',
    )
    assert result.returncode != 0
    assert "historical_ohlcv_universe.json fehlt" in result.stdout
    assert not (tmp_path / "historical_ohlcv_universe.json").exists()


def test_malformed_release_list_never_triggers_bootstrap(tmp_path):
    result = _run_restore(
        tmp_path, release_list="not-json", assets='{"assets":[]}', bootstrap="1"
    )
    assert result.returncode != 0
    assert not (tmp_path / "historical_ohlcv_universe.json").exists()


def test_malformed_release_assets_never_triggers_bootstrap(tmp_path):
    result = _run_restore(
        tmp_path,
        release_list='[{"tagName":"historical-ohlcv"}]',
        assets="not-json",
        bootstrap="1",
    )
    assert result.returncode != 0
    assert not (tmp_path / "historical_ohlcv_universe.json").exists()


def test_existing_release_without_universe_asset_allows_explicit_bootstrap(tmp_path):
    result = _run_restore(
        tmp_path,
        release_list='[{"tagName":"historical-ohlcv"}]',
        assets='{"assets":[{"name":"historical_ohlcv.sqlite"}]}',
        bootstrap="1",
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "historical_ohlcv_universe.json").exists()


def test_first_release_bootstrap_requires_explicit_switch(tmp_path):
    result = _run_restore(tmp_path, release_list="[]", assets='{"assets":[]}')
    assert result.returncode != 0
    assert "Release existiert nicht" in result.stdout


def test_first_release_bootstrap_succeeds_with_explicit_switch(tmp_path):
    result = _run_restore(
        tmp_path, release_list="[]", assets='{"assets":[]}', bootstrap="1"
    )
    assert result.returncode == 0, result.stderr


def test_existing_universe_asset_is_downloaded_and_nonempty(tmp_path):
    result = _run_restore(
        tmp_path,
        release_list='[{"tagName":"historical-ohlcv"}]',
        assets='{"assets":[{"name":"historical_ohlcv_universe.json"}]}',
    )
    assert result.returncode == 0, result.stderr
    content = (tmp_path / "historical_ohlcv_universe.json").read_text()
    assert "KEPT.DE" in content


def test_universe_asset_download_failure_aborts(tmp_path):
    result = _run_restore(
        tmp_path,
        release_list='[{"tagName":"historical-ohlcv"}]',
        assets='{"assets":[{"name":"historical_ohlcv_universe.json"}]}',
        fail_download="1",
    )
    assert result.returncode != 0



def test_open_positions_are_synced_after_tracker_and_before_technical_check():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    tracker = text.index("run: python positionen_tracker.py")
    sync = text.index("run: python historical_ohlcv.py sync-open-positions --positions-file Offene_Positionen.csv")
    technical_check = text.index("run: python offene_positionen_check.py")
    assert tracker < sync < technical_check
    assert "Offene_Positionen.csv" in text[sync - 400:sync + 200]



def test_ohlcv_regression_tests_are_executed_by_pytest_in_ci():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    section = _section(
        text,
        "- name: Historische OHLCV Regressionstests",
        "# ============================================================\n      # EXTERNER DATENQUELLEN-TEST",
    )
    assert "pytest -q" in section
    assert "tests/test_historical_ohlcv.py" in section
    assert "tests/test_offene_positionen_check_history_source.py" in section
    assert "tests/test_workflow_dynamic_universe.py" in section
    assert "python tests/test_historical_ohlcv.py" not in section


def test_final_open_position_verification_runs_after_technical_fallback():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    technical = text.index("run: python offene_positionen_check.py")
    verify = text.index("run: python historical_ohlcv.py verify-open-positions --positions-file Offene_Positionen.csv")
    benchmark = text.index("run: python performance_benchmark.py")
    assert technical < verify < benchmark



def _archive_shell() -> str:
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    archive = _section(
        text,
        "- name: Finale OHLCV-Datenbank und Universum im Dauerarchiv sichern und SHA-256 verifizieren",
        "- name: Trading-Radar-Laufmarker setzen",
    )
    lines = archive.splitlines()
    run_index = lines.index("        run: |")
    script_lines = []
    for line in lines[run_index + 1:]:
        if line and not line.startswith("          "):
            break
        script_lines.append(line[10:] if line.startswith("          ") else "")
    return "\n".join(script_lines)


def _run_archive(
    tmp_path,
    *,
    release_missing="0",
    fail_create="0",
    fail_upload="0",
    fail_upload_asset="",
    skip_remote_asset="",
    tamper_asset="",
    fail_api="0",
    invalid_api_json="0",
    invalid_digest_asset="",
    missing_digest_asset="",
    universe_payload='{"version":1,"tickers":["KEPT.DE"]}\n',
    missing_local_asset="",
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
command="$1 $2"
shift 2
case "$command" in
  "release view")
    [[ "$TEST_RELEASE_MISSING" != "1" ]]
    ;;
  "release create")
    [[ "$TEST_FAIL_CREATE" != "1" ]]
    ;;
  "release upload")
    [[ "$TEST_FAIL_UPLOAD" != "1" ]] || exit 21
    for asset in "$@"; do
      case "$asset" in
        *.sqlite|*.json)
          [[ "$asset" != "$TEST_FAIL_UPLOAD_ASSET" ]] || exit 23
          [[ "$asset" != "$TEST_SKIP_REMOTE_ASSET" ]] || continue
          cp "$asset" "$TEST_REMOTE_DIR/$(basename "$asset")"
          if [[ "$(basename "$asset")" == "$TEST_TAMPER_ASSET" ]]; then
            printf 'tampered\n' >> "$TEST_REMOTE_DIR/$(basename "$asset")"
          fi
          ;;
      esac
    done
    ;;
  "api repos/joergneuber/Sektor-Analyse/releases/tags/historical-ohlcv")
    [[ "$TEST_FAIL_API" != "1" ]] || exit 22
    if [[ "$TEST_INVALID_API_JSON" == "1" ]]; then
      printf 'not-json'
      exit 0
    fi
    printf '{"assets":['
    separator=""
    for asset in historical_ohlcv_universe.json historical_ohlcv.sqlite; do
      if [[ "$asset" != "$TEST_SKIP_REMOTE_ASSET" && -f "$TEST_REMOTE_DIR/$asset" ]]; then
        digest="$(sha256sum "$TEST_REMOTE_DIR/$asset" | awk '{print $1}')"
        if [[ "$asset" == "$TEST_MISSING_DIGEST_ASSET" ]]; then
          printf '%s{"name":"%s"}' "$separator" "$asset"
        else
          if [[ "$asset" == "$TEST_INVALID_DIGEST_ASSET" ]]; then digest="not-a-valid-sha256"; fi
          printf '%s{"name":"%s","digest":"sha256:%s"}' "$separator" "$asset" "$digest"
        fi
        separator=","
      fi
    done
    printf ']}'
    ;;
  *) echo "unexpected gh command: $command $*" >&2; exit 30 ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    (tmp_path / "historical_ohlcv.sqlite").write_bytes(b"sqlite-database-payload")
    if missing_local_asset != "historical_ohlcv_universe.json":
        (tmp_path / "historical_ohlcv_universe.json").write_text(
            universe_payload, encoding="utf-8"
        )
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env['PATH']}",
        "GITHUB_REPOSITORY": "joergneuber/Sektor-Analyse",
        "TEST_REMOTE_DIR": str(remote_dir),
        "TEST_RELEASE_MISSING": release_missing,
        "TEST_FAIL_CREATE": fail_create,
        "TEST_FAIL_UPLOAD": fail_upload,
        "TEST_FAIL_UPLOAD_ASSET": fail_upload_asset,
        "TEST_SKIP_REMOTE_ASSET": skip_remote_asset,
        "TEST_TAMPER_ASSET": tamper_asset,
        "TEST_FAIL_API": fail_api,
        "TEST_INVALID_API_JSON": invalid_api_json,
        "TEST_INVALID_DIGEST_ASSET": invalid_digest_asset,
        "TEST_MISSING_DIGEST_ASSET": missing_digest_asset,
    })
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _archive_shell()],
        cwd=tmp_path, env=env, text=True, capture_output=True,
    )
    return result, remote_dir


def test_exactly_one_final_ohlcv_cache_and_release_persistence_path_exists():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    assert "Historische OHLCV-Datenbank nach Positionsabgleich im Actions-Cache speichern" not in text
    assert "Historische OHLCV-Datenbank und Universum final dauerhaft archivieren" not in text
    assert text.count("- name: Finale OHLCV-Datenbank im Actions-Cache sichern") == 1
    assert text.count("- name: Finale OHLCV-Datenbank und Universum im Dauerarchiv sichern und SHA-256 verifizieren") == 1
    assert text.count("gh release upload historical-ohlcv") == 2
    assert "nmm-historical-ohlcv-final-v1-" not in text
    cache_section = _section(
        text,
        "- name: Finale OHLCV-Datenbank im Actions-Cache sichern",
        "- name: Finale OHLCV-Datenbank und Universum im Dauerarchiv sichern und SHA-256 verifizieren",
    )
    assert "if: ${{ success() }}" in cache_section
    assert "continue-on-error" not in cache_section
    assert "nmm-historical-ohlcv-v1-${{ runner.os }}-${{ github.ref_name }}-${{ github.run_id }}-${{ github.run_attempt }}" in cache_section


def test_final_sync_final_verification_and_persistence_are_strictly_ordered():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    final_sync = text.index("- name: Finale OHLCV-Synchronisierung und Coverage")
    final_verify = text.index("- name: Finale offene OHLCV-Historien nach Synchronisierung qualitaetspruefen")
    cache_save = text.index("- name: Finale OHLCV-Datenbank im Actions-Cache sichern")
    release_archive = text.index("- name: Finale OHLCV-Datenbank und Universum im Dauerarchiv sichern und SHA-256 verifizieren")
    radar = text.index("- name: Trading-Radar-Laufmarker setzen")
    assert final_sync < final_verify < cache_save < release_archive < radar
    assert text.rindex("run: python historical_ohlcv.py verify-open-positions --positions-file Offene_Positionen.csv") < cache_save
    verify_section = _section(
        text,
        "- name: Finale offene OHLCV-Historien nach Synchronisierung qualitaetspruefen",
        "- name: Finale OHLCV-Datenbank im Actions-Cache sichern",
    )
    assert "run: python historical_ohlcv.py verify-open-positions --positions-file Offene_Positionen.csv" in verify_section
    assert "continue-on-error" not in verify_section


def test_archive_step_is_blocking_and_checks_both_release_asset_digests():
    text = MAIN_WORKFLOW.read_text(encoding="utf-8")
    section = _section(
        text,
        "- name: Finale OHLCV-Datenbank und Universum im Dauerarchiv sichern und SHA-256 verifizieren",
        "- name: Trading-Radar-Laufmarker setzen",
    )
    assert "continue-on-error" not in section
    assert "if: ${{ success() }}" in section
    assert "set -euo pipefail" in section
    assert "sha256sum" in section
    assert "gh api" in section
    assert "gh release download" not in section
    assert section.index("historical_ohlcv_universe.json --clobber") < section.index(
        "historical_ohlcv.sqlite --clobber"
    )


def test_archive_upload_and_remote_sha256_verification_succeed(tmp_path):
    result, remote_dir = _run_archive(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "OHLCV-Release-Verifikation PASS: historical_ohlcv.sqlite" in result.stdout
    assert "OHLCV-Release-Verifikation PASS: historical_ohlcv_universe.json" in result.stdout
    assert (remote_dir / "historical_ohlcv.sqlite").read_bytes() == (
        tmp_path / "historical_ohlcv.sqlite"
    ).read_bytes()
    assert (remote_dir / "historical_ohlcv_universe.json").read_bytes() == (
        tmp_path / "historical_ohlcv_universe.json"
    ).read_bytes()


def test_archive_upload_failure_aborts(tmp_path):
    result, _ = _run_archive(tmp_path, fail_upload="1")
    assert result.returncode != 0


def test_archive_database_upload_failure_keeps_new_universe_asset(tmp_path):
    result, remote_dir = _run_archive(
        tmp_path, fail_upload_asset="historical_ohlcv.sqlite"
    )
    assert result.returncode != 0
    assert (remote_dir / "historical_ohlcv_universe.json").exists()
    assert not (remote_dir / "historical_ohlcv.sqlite").exists()


def test_archive_release_metadata_api_failure_aborts(tmp_path):
    result, _ = _run_archive(tmp_path, fail_api="1")
    assert result.returncode != 0


def test_archive_malformed_release_metadata_json_aborts(tmp_path):
    result, _ = _run_archive(tmp_path, invalid_api_json="1")
    assert result.returncode != 0


def test_archive_invalid_remote_sha256_digest_aborts(tmp_path):
    result, _ = _run_archive(
        tmp_path, invalid_digest_asset="historical_ohlcv.sqlite"
    )
    assert result.returncode != 0
    assert "ungueltiger SHA-256-Digest" in result.stdout


def test_archive_missing_remote_sha256_digest_aborts(tmp_path):
    result, _ = _run_archive(
        tmp_path, missing_digest_asset="historical_ohlcv_universe.json"
    )
    assert result.returncode != 0


def test_archive_missing_remote_database_asset_aborts(tmp_path):
    result, _ = _run_archive(
        tmp_path, skip_remote_asset="historical_ohlcv.sqlite"
    )
    assert result.returncode != 0


def test_archive_missing_remote_universe_asset_aborts(tmp_path):
    result, _ = _run_archive(
        tmp_path, skip_remote_asset="historical_ohlcv_universe.json"
    )
    assert result.returncode != 0


def test_archive_remote_hash_mismatch_aborts(tmp_path):
    result, _ = _run_archive(
        tmp_path, tamper_asset="historical_ohlcv_universe.json"
    )
    assert result.returncode != 0
    assert "SHA-256 stimmt" in result.stdout


def test_archive_database_hash_mismatch_aborts(tmp_path):
    result, _ = _run_archive(tmp_path, tamper_asset="historical_ohlcv.sqlite")
    assert result.returncode != 0
    assert "SHA-256 stimmt" in result.stdout


def test_archive_release_creation_failure_aborts(tmp_path):
    result, _ = _run_archive(
        tmp_path, release_missing="1", fail_create="1"
    )
    assert result.returncode != 0


def test_archive_accepts_legacy_list_universe_format(tmp_path):
    result, _ = _run_archive(
        tmp_path, universe_payload='["KEPT.DE"]\n'
    )
    assert result.returncode == 0, result.stderr


def test_archive_rejects_invalid_universe_json(tmp_path):
    result, _ = _run_archive(tmp_path, universe_payload='{"version":1,"tickers":"BAD"}')
    assert result.returncode != 0


def test_archive_requires_both_local_assets(tmp_path):
    result, _ = _run_archive(
        tmp_path, missing_local_asset="historical_ohlcv_universe.json"
    )
    assert result.returncode != 0
