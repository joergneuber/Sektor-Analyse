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
        "- name: Historische OHLCV-Datenbank speichern",
        "# Zusaetzlich wird der aktuelle Research-Bestand",
    )
    assert "historical_ohlcv_universe.json" not in restore
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
