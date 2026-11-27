"""Regression checks for the fail-closed OHLCV and Trading-Radar workflow contract."""
from pathlib import Path
import os
import subprocess
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"


def _step_block(text: str, start_name: str, next_name: str) -> str:
    start = text.index(f"- name: {start_name}")
    end = text.index(f"- name: {next_name}", start)
    return text[start:end]


def test_open_positions_get_a_bounded_refresh_before_scanners_without_weakening_quality():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    sync = _step_block(
        workflow,
        "Offene Positionen ins historische OHLCV-Universum übernehmen",
        "Offene Positionen + Check erstellen und in Drive aktualisieren",
    )
    verify = _step_block(
        workflow,
        "Offene OHLCV-Historien abschliessend qualitaetspruefen",
        "Live-Benchmark gegen MSCI World aktualisieren",
    )

    assert "continue-on-error: true" in sync
    assert "sync-open-positions --positions-file Offene_Positionen.csv --refresh-current-session-gaps" in sync
    assert "continue-on-error: true" in verify
    assert "for attempt in 1 2 3" in verify
    assert "STALE_HISTORY:|NO_STORED_BARS" in verify
    assert "HIST-OHLCV-SESSION-REFRESH: requested=[0-9]+ stale=[1-9][0-9]*" in verify
    assert "sleep 45" in verify
    assert 'exit "$positions_rc"' in verify
    assert workflow.index("- name: Offene Positionen ins historische OHLCV-Universum übernehmen") < workflow.index(
        "- name: Offene OHLCV-Historien abschliessend qualitaetspruefen"
    )


def test_report_gate_failure_is_non_blocking_for_job_but_remains_failure_in_evidence():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    report_check = _step_block(
        workflow,
        "Neue Trading-Radar technisch gültig und frisch verifizieren",
        "Trading-Radar Evidence nach Report-Gate finalisieren",
    )
    evidence = _step_block(
        workflow,
        "Trading-Radar Evidence nach Report-Gate finalisieren",
        "Trading-Radar Evidence final validieren",
    )

    assert "continue-on-error: true" in report_check
    assert "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL" in report_check
    assert "TRADING_RADAR_REPORT_MAIL_ELIGIBLE=true" in report_check
    assert "::warning::Trading-Radar bleibt NICHT_FINAL" in report_check
    assert "TRADING_RADAR_REPORT_GATE: ${{ steps.trading-radar-report-check.outcome }}" in evidence
    assert "steps.trading-radar-report-check.outcome == 'success'" in workflow


def test_gate_downgraded_nonfinal_report_is_emailed_but_never_regularly_uploaded():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    email = _step_block(
        workflow,
        "Trading-Radar per E-Mail versenden (FINAL oder klar markiertes NICHT_FINAL)",
        "Technischen Gemini-Fallback per E-Mail melden",
    )
    drive = _step_block(workflow, "Daten hochladen", "Trading-Radar per E-Mail versenden (FINAL oder klar markiertes NICHT_FINAL)")

    assert "steps.trading-radar-report-check.outputs.TRADING_RADAR_REPORT_MAIL_ELIGIBLE == 'true'" in email
    assert "steps.trading-radar-report-check.outputs.TRADING_RADAR_FACHLICH_FINAL == 'NICHT_FINAL'" in email
    assert "TRADING_RADAR_FACHLICH_FINAL: ${{ steps.trading-radar-report-check.outputs.TRADING_RADAR_FACHLICH_FINAL || 'NICHT_FINAL' }}" in email
    assert "steps.trading-radar-report-check.outcome == 'success'" in drive
    assert "steps.trading-radar.outputs.fachlich_final == 'FINAL'" in drive


def test_any_early_report_check_failure_exports_nicht_final():
    """Even a precondition failure must not leak the upstream FINAL status to evidence."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    report_check = _step_block(
        workflow,
        "Neue Trading-Radar technisch gültig und frisch verifizieren",
        "Trading-Radar Evidence nach Report-Gate finalisieren",
    )
    assert "trap report_check_exit EXIT" in report_check
    assert 'echo "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL" >> "$GITHUB_OUTPUT"' in report_check

    marker = "        run: |\n"
    assert marker in report_check
    script = report_check.split(marker, 1)[1]
    script = script.split("\n      - name:", 1)[0]
    script = textwrap.dedent(script)

    with tempfile.NamedTemporaryFile(mode="r+", encoding="utf-8") as output:
        env = os.environ.copy()
        env.update({
            "GITHUB_OUTPUT": output.name,
            "TRADING_RADAR_CONTRACT_OUTCOME": "failure",
            "TRADING_RADAR_STEP_OUTCOME": "success",
            "TRADING_RADAR_STATUS": "SUCCESS",
            "TRADING_RADAR_FACHLICH_FINAL": "FINAL",
            "TRADING_RADAR_DATEI": "",
        })
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output.seek(0)
        emitted = output.read()

    assert result.returncode == 1
    assert emitted.strip() == "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL"
    assert "TRADING_RADAR_REPORT_MAIL_ELIGIBLE=true" not in emitted


def test_report_gate_failure_downgrades_and_allows_mail_only_for_valid_report():
    """A real fresh report may be mailed as NICHT_FINAL after the gate rejects it."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    report_check = _step_block(
        workflow,
        "Neue Trading-Radar technisch gültig und frisch verifizieren",
        "Trading-Radar Evidence nach Report-Gate finalisieren",
    )
    marker = "        run: |\n"
    script = textwrap.dedent(report_check.split(marker, 1)[1].split("\n      - name:", 1)[0])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        started = root / ".trading_radar_run_started"
        report = root / "report.txt"
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python"
        fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_python.chmod(0o755)
        started.write_text("started\n", encoding="utf-8")
        os.utime(started, (1, 1))
        report.write_text("Report\nTRADING_RADAR_FACHLICH_FINAL=FINAL\n", encoding="utf-8")

        output = root / "github_output.txt"
        env = os.environ.copy()
        env.update({
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "GITHUB_OUTPUT": str(output),
            "TRADING_RADAR_CONTRACT_OUTCOME": "success",
            "TRADING_RADAR_STEP_OUTCOME": "success",
            "TRADING_RADAR_STATUS": "SUCCESS",
            "TRADING_RADAR_FACHLICH_FINAL": "FINAL",
            "TRADING_RADAR_DATEI": str(report),
        })
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            cwd=tmp,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        emitted = output.read_text(encoding="utf-8")
        downgraded_report = report.read_text(encoding="utf-8")

    assert result.returncode == 1
    assert "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL" in emitted
    assert "TRADING_RADAR_REPORT_MAIL_ELIGIBLE=true" in emitted
    assert "TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL" in downgraded_report
