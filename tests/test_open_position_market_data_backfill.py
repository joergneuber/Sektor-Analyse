import sys
from pathlib import Path
import shutil
import types
import os

import pytest

import pandas as pd

# The source-backfill tests do not call Yahoo; keep module import independent
# from optional runtime integrations in the test environment.
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

import offene_positionen_check as checker


@pytest.fixture(autouse=True)
def _mock_remote_baseline_for_legacy_main_tests(monkeypatch):
    # Main-flow tests below isolate local transaction behavior. Remote revision
    # capture/validation has dedicated tests in this file.
    monkeypatch.setattr(
        checker, "_load_drive_source_baseline",
        lambda source: {
            "file_id": "fixture-drive-id", "version": "1",
            "modified_time": "2026-11-26T00:00:00.000Z",
            "mime_type": "application/vnd.google-apps.spreadsheet",
            "md5_checksum": "",
        },
    )
    monkeypatch.setattr(checker, "_verify_drive_source_baseline", lambda *a, **k: {})


def _source_row(ticker, entry, quote="", performance=""):
    return {
        "Ticker": ticker,
        "Name": f"{ticker} Instrument",
        "Sektor": "",
        "Markt": "EU",
        "Waehrung": "EUR",
        "Richtung": "Long",
        "Einstiegsdatum": "23.11.2026",
        "Einstieg": f"{entry:.2f}".replace(".", ","),
        "Aktueller_Kurs": quote,
        "Stop": "1,00",
        "TP1": "100,00",
        "TP2": "120,00",
        "Status": "Offen",
        "Performance_Seit_Einstieg%": performance,
    }


def _checked_row(source_row, quote, performance):
    row = dict(source_row)
    row["Aktueller_Kurs"] = quote
    row["Performance_Seit_Einstieg%"] = performance
    return row


def _write_source(path, rows):
    pd.DataFrame(rows).to_csv(
        path, sep=";", index=False, encoding="utf-8-sig"
    )


def test_backfill_fills_only_missing_quote_and_performance_cells(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    missing = _source_row("LI7U.DE", 9.15)
    existing = _source_row("SPYI.DE", 11.24, "11,14", "-0,89")
    _write_source(source_file, [missing, existing])

    checked = pd.DataFrame([
        _checked_row(missing, 8.26, -9.75),
        # Deliberately different: existing source values must win.
        _checked_row(existing, 99.99, 777.77),
    ])

    changed = checker.backfill_missing_market_data_to_source(
        str(source_file), checked
    )
    result = checker.read_positions(str(source_file))

    assert changed == 1
    li7u = result.loc[result["Ticker"] == "LI7U.DE"].iloc[0]
    spyi = result.loc[result["Ticker"] == "SPYI.DE"].iloc[0]
    assert li7u["Aktueller_Kurs"] == "8,26"
    assert li7u["Performance_Seit_Einstieg%"] == "-9,73"
    assert spyi["Aktueller_Kurs"] == "11,14"
    assert spyi["Performance_Seit_Einstieg%"] == "-0,89"
    # Trade facts are not changed by the market-data backfill.
    for ticker, row in (("LI7U.DE", li7u), ("SPYI.DE", spyi)):
        assert row["Einstieg"] == ("9,15" if ticker == "LI7U.DE" else "11,24")
        assert row["Stop"] == "1,00"
        assert row["TP1"] == "100,00"
        assert row["TP2"] == "120,00"
        assert row["Status"] == "Offen"


def test_backfill_preserves_original_file_permissions(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    source = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [source])
    os.chmod(source_file, 0o640)

    checked = pd.DataFrame([_checked_row(source, 8.26, -9.75)])
    assert checker.backfill_missing_market_data_to_source(
        str(source_file), checked
    ) == 1

    assert os.stat(source_file).st_mode & 0o777 == 0o640


def test_backfill_matches_same_ticker_positions_by_full_trade_identity(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    first = _source_row("VNA.DE", 17.07)
    second = _source_row("VNA.DE", 16.00)
    _write_source(source_file, [first, second])

    checked = pd.DataFrame([
        _checked_row(first, 17.36, 1.70),
        _checked_row(second, 17.36, 8.50),
    ])
    assert checker.backfill_missing_market_data_to_source(
        str(source_file), checked
    ) == 2

    result = checker.read_positions(str(source_file))
    assert result["Aktueller_Kurs"].tolist() == ["17,36", "17,36"]
    assert result["Performance_Seit_Einstieg%"].tolist() == ["1,70", "8,50"]


def test_backfill_refuses_to_overwrite_source_changed_after_initial_read(tmp_path, monkeypatch):
    source_file = tmp_path / "Offene_Positionen.csv"
    source = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [source])
    external_bytes = source_file.read_bytes() + b"\n# external edit\n"
    real_copy2 = shutil.copy2
    calls = {"count": 0}

    def external_edit_then_copy(src, dst, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            source_file.write_bytes(external_bytes)
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", external_edit_then_copy)
    checked = pd.DataFrame([_checked_row(source, 8.26, -9.75)])
    with pytest.raises(RuntimeError, match="änderte sich seit dem Einlesen"):
        checker.backfill_missing_market_data_to_source(str(source_file), checked)

    assert source_file.read_bytes() == external_bytes


def test_backfill_does_not_write_when_check_has_no_valid_quote(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    source = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [source])
    before = source_file.read_bytes()

    checked = pd.DataFrame([_checked_row(source, "", "")])
    assert checker.backfill_missing_market_data_to_source(
        str(source_file), checked
    ) == 0
    assert source_file.read_bytes() == before


def test_backfill_rejects_ambiguous_duplicate_position_keys(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    source = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [source, source])

    checked = pd.DataFrame([_checked_row(source, 8.26, -9.75)])
    try:
        checker.backfill_missing_market_data_to_source(
            str(source_file), checked
        )
    except RuntimeError as exc:
        assert "doppelte Positionsschlüssel" in str(exc)
    else:
        raise AssertionError("Doppelte Positionsschlüssel wurden nicht abgewiesen.")



@pytest.mark.parametrize("failure_stage", ["credentials", "sheet_update", "source_sync"])
def test_main_rolls_back_local_backfill_when_google_update_fails(
    tmp_path, monkeypatch, failure_stage
):
    source_file = tmp_path / "Offene_Positionen.csv"
    original = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [original])
    os.chmod(source_file, 0o640)
    original_bytes = source_file.read_bytes()

    output = pd.DataFrame([{"Ticker": "LI7U.DE"}])
    history = pd.DataFrame()
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (output, history))

    def mutate_source(*args, **kwargs):
        source_file.write_bytes(original_bytes + b"\n# local backfill mutation")
        if kwargs.get("snapshot_path"):
            checker._register_snapshot_source_state(
                kwargs["snapshot_path"], checker._sha256_file(str(source_file)),
                os.stat(source_file).st_mode & 0o777,
            )
        return 1

    monkeypatch.setattr(checker, "backfill_missing_market_data_to_source", mutate_source)
    if failure_stage == "credentials":
        def fail_credentials():
            raise RuntimeError("simulierter Credential-Fehler")
        monkeypatch.setattr(checker, "google_credentials", fail_credentials)
    else:
        monkeypatch.setattr(checker, "google_credentials", lambda: object())
        if failure_stage == "sheet_update":
            def fail_sheet_update(*args, **kwargs):
                raise RuntimeError("simulierter Sheets-Update-Fehler")
            monkeypatch.setattr(checker, "upsert_google_sheet", fail_sheet_update)
        else:
            monkeypatch.setattr(checker, "upsert_google_sheet", lambda *a, **k: None)
            monkeypatch.setattr(checker, "_remove_closed_from_source", lambda *a, **k: 0)
            def fail_source_sync(*args, **kwargs):
                raise RuntimeError("simulierter Drive-Quell-Sync-Fehler")
            monkeypatch.setattr(checker, "_sync_cleaned_source_to_drive", fail_source_sync)

    with pytest.raises(RuntimeError, match="simulierter"):
        checker.main()

    assert source_file.read_bytes() == original_bytes
    assert os.stat(source_file).st_mode & 0o777 == 0o640
    snapshots = list(tmp_path.glob(".*.google_rollback_*.bak"))
    if failure_stage == "credentials":
        assert snapshots == []
    else:
        assert len(snapshots) == 1
        assert snapshots[0].read_bytes() == original_bytes


def test_main_aborts_before_local_or_remote_work_when_source_snapshot_is_missing(tmp_path, monkeypatch):
    missing = tmp_path / "Offene_Positionen.csv"
    monkeypatch.setattr(checker, "INPUT_FILE", str(missing))
    monkeypatch.setattr(
        checker, "run_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("run_local must not start")),
    )
    monkeypatch.setattr(
        checker, "google_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("Google must not start")),
    )
    with pytest.raises(FileNotFoundError, match="Pflicht-Snapshot"):
        checker.main()


def test_restore_rejects_missing_snapshot_argument():
    with pytest.raises(RuntimeError, match="Rollback-Snapshot fehlt"):
        checker._restore_source_file_snapshot(None, "missing.csv")


def test_snapshot_is_mandatory_and_missing_source_fails_closed(tmp_path):
    missing = tmp_path / "missing.csv"
    with pytest.raises(FileNotFoundError, match="Pflicht-Snapshot"):
        checker._snapshot_source_file(str(missing))


def test_restore_keeps_original_snapshot_when_post_replace_hash_check_fails(tmp_path, monkeypatch):
    source = tmp_path / "Offene_Positionen.csv"
    original = b"ticker;status\nABC;Offen\n"
    source.write_bytes(original)
    os.chmod(source, 0o640)
    snapshot = Path(checker._snapshot_source_file(str(source)))
    source.write_bytes(b"mutated\n")
    checker._register_snapshot_source_state(
        str(snapshot), checker._sha256_file(str(source)), os.stat(source).st_mode & 0o777
    )

    real_hash = checker._sha256_file
    calls = {"source_after_replace": 0}

    def fail_only_post_replace(path):
        if str(path) == str(source) and source.read_bytes() == original:
            calls["source_after_replace"] += 1
            if calls["source_after_replace"] == 1:
                return "0" * 64
        return real_hash(path)

    monkeypatch.setattr(checker, "_sha256_file", fail_only_post_replace)
    with pytest.raises(RuntimeError, match="Snapshot bleibt erhalten"):
        checker._restore_source_file_snapshot(str(snapshot), str(source))

    assert snapshot.exists()
    assert snapshot.read_bytes() == original
    # The restored source may be correct even though the simulated post-check failed.
    assert source.read_bytes() == original


def test_restore_rejects_snapshot_tampering_against_independent_original_digest(tmp_path):
    source = tmp_path / "Offene_Positionen.csv"
    original = b"Ticker;Status\nABC;Offen\n"
    source.write_bytes(original)
    snapshot = Path(checker._snapshot_source_file(str(source)))
    source.write_bytes(b"local mutation\n")
    snapshot.write_bytes(b"tampered snapshot\n")

    with pytest.raises(RuntimeError, match="persistenten SHA-256"):
        checker._restore_source_file_snapshot(str(snapshot), str(source))

    assert source.read_bytes() == b"local mutation\n"
    assert snapshot.read_bytes() == b"tampered snapshot\n"


def test_main_reports_drive_sync_attempt_when_remove_closed_raises_inside_sync(
    tmp_path, monkeypatch, capsys
):
    source_file = tmp_path / "Offene_Positionen.csv"
    original = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [original])
    original_bytes = source_file.read_bytes()
    output = pd.DataFrame([{"Ticker": "LI7U.DE"}])
    history = pd.DataFrame()
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (output, history))
    monkeypatch.setattr(checker, "backfill_missing_market_data_to_source", lambda *a, **k: 0)
    monkeypatch.setattr(checker, "google_credentials", lambda: object())
    monkeypatch.setattr(checker, "upsert_google_sheet", lambda *a, **k: "verified-master-id")

    def fail_during_drive_sync(*args, **kwargs):
        kwargs["sync_status"]["attempted"] = True
        raise RuntimeError("Drive sync failed inside remove-closed")

    monkeypatch.setattr(checker, "_remove_closed_from_source", fail_during_drive_sync)
    with pytest.raises(RuntimeError, match="lokaler Rollback erfolgreich"):
        checker.main()

    assert source_file.read_bytes() == original_bytes
    output_text = capsys.readouterr().out
    assert "Synchronisierung wurde begonnen, aber nicht als erfolgreich bestätigt" in output_text
    assert "Drive sync failed inside remove-closed" in output_text


def test_main_reports_local_only_rollback_after_sheets_commit_and_drive_sync_failure(tmp_path, monkeypatch, capsys):
    source_file = tmp_path / "Offene_Positionen.csv"
    original = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [original])
    original_bytes = source_file.read_bytes()
    output = pd.DataFrame([{"Ticker": "LI7U.DE"}])
    history = pd.DataFrame()
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (output, history))

    def mutate_source(*args, **kwargs):
        source_file.write_bytes(original_bytes + b"\n# backfilled")
        if kwargs.get("snapshot_path"):
            checker._register_snapshot_source_state(
                kwargs["snapshot_path"], checker._sha256_file(str(source_file)),
                os.stat(source_file).st_mode & 0o777,
            )
        return 1

    monkeypatch.setattr(checker, "backfill_missing_market_data_to_source", mutate_source)
    monkeypatch.setattr(checker, "google_credentials", lambda: object())
    monkeypatch.setattr(checker, "upsert_google_sheet", lambda *a, **k: "verified-master-id")
    monkeypatch.setattr(checker, "_remove_closed_from_source", lambda *a, **k: 0)
    monkeypatch.setattr(
        checker, "_sync_cleaned_source_to_drive",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Drive sync failed after Sheets commit")),
    )

    with pytest.raises(RuntimeError, match="lokaler Rollback erfolgreich"):
        checker.main()

    assert source_file.read_bytes() == original_bytes
    output_text = capsys.readouterr().out
    assert "Master-ID: verified-master-id" in output_text
    assert "nimmt den bereits bestätigten Sheets-Stand nicht zurück" in output_text
    assert "kein vollständiger Google-Sheets-/Drive-Rollback" in output_text


def test_snapshot_cleanup_failure_does_not_raise_after_success(tmp_path, monkeypatch):
    snapshot = tmp_path / "rollback.bak"
    snapshot.write_bytes(b"verified")

    def fail_remove(path):
        raise OSError("simulierter Cleanup-Fehler")

    monkeypatch.setattr(checker.os, "remove", fail_remove)
    checker._discard_source_file_snapshot(str(snapshot))
    assert snapshot.read_bytes() == b"verified"


def test_main_keeps_verified_snapshot_when_rollback_itself_fails(
    tmp_path, monkeypatch
):
    source_file = tmp_path / "Offene_Positionen.csv"
    original = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [original])
    original_bytes = source_file.read_bytes()
    output = pd.DataFrame([{"Ticker": "LI7U.DE"}])
    history = pd.DataFrame()
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (output, history))

    def mutate_source(*args, **kwargs):
        source_file.write_bytes(original_bytes + b"\n# mutated")
        if kwargs.get("snapshot_path"):
            checker._register_snapshot_source_state(
                kwargs["snapshot_path"], checker._sha256_file(str(source_file)),
                os.stat(source_file).st_mode & 0o777,
            )
        return 1

    monkeypatch.setattr(checker, "backfill_missing_market_data_to_source", mutate_source)
    monkeypatch.setattr(checker, "google_credentials", lambda: object())
    monkeypatch.setattr(
        checker, "upsert_google_sheet",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Sheets failed")),
    )
    real_replace = checker.os.replace
    def fail_only_source_restore(src, dst):
        if str(dst) == str(source_file):
            raise OSError("simulierter Restore-Fehler")
        return real_replace(src, dst)
    monkeypatch.setattr(checker.os, "replace", fail_only_source_restore)

    with pytest.raises(RuntimeError, match="Snapshot bleibt erhalten") as exc:
        checker.main()

    snapshot_path = Path(str(exc.value).split("Notfallkopie): ", 1)[1].split(". Ursache:", 1)[0])
    assert snapshot_path.exists()
    assert snapshot_path.read_bytes() == original_bytes
    assert source_file.read_bytes() != original_bytes


def test_main_discards_rollback_snapshot_only_after_successful_source_sync(
    tmp_path, monkeypatch
):
    source_file = tmp_path / "Offene_Positionen.csv"
    _write_source(source_file, [_source_row("LI7U.DE", 9.15)])
    original_bytes = source_file.read_bytes()
    calls = []
    output = pd.DataFrame([{"Ticker": "LI7U.DE"}])
    history = pd.DataFrame()
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (output, history))

    def mutate_source(*args, **kwargs):
        source_file.write_bytes(original_bytes + b"\n# committed backfill")
        if kwargs.get("snapshot_path"):
            checker._register_snapshot_source_state(
                kwargs["snapshot_path"], checker._sha256_file(str(source_file)),
                os.stat(source_file).st_mode & 0o777,
            )
        return 1

    monkeypatch.setattr(checker, "backfill_missing_market_data_to_source", mutate_source)
    monkeypatch.setattr(checker, "google_credentials", lambda: object())
    monkeypatch.setattr(checker, "upsert_google_sheet", lambda *a, **k: calls.append("check-sheet"))
    monkeypatch.setattr(checker, "_remove_closed_from_source", lambda *a, **k: 0)
    monkeypatch.setattr(checker, "_sync_cleaned_source_to_drive", lambda *a, **k: calls.append("source-drive"))

    checker.main()

    assert calls == ["check-sheet", "source-drive"]
    assert source_file.read_bytes() != original_bytes
    assert not list(tmp_path.glob(".*.google_rollback_*.bak"))


def test_main_syncs_backfilled_source_when_no_current_closed_row_is_removed(monkeypatch, tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    _write_source(source_file, [_source_row("LI7U.DE", 9.15)])
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    calls = []
    output = pd.DataFrame([{"Ticker": "LI7U.DE"}])
    history = pd.DataFrame()

    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (output, history))
    monkeypatch.setattr(
        checker, "backfill_missing_market_data_to_source",
        lambda *a, **k: 1,
    )
    monkeypatch.setattr(checker, "google_credentials", lambda: object())
    monkeypatch.setattr(checker, "upsert_google_sheet", lambda *a, **k: calls.append("check-upload"))
    monkeypatch.setattr(
        checker, "_remove_closed_from_source",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        checker, "_sync_cleaned_source_to_drive",
        lambda *a, **k: calls.append("source-sync"),
    )

    checker.main()
    assert calls == ["check-upload", "source-sync"]


def test_main_does_not_duplicate_source_sync_after_closed_position_removal(monkeypatch, tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    _write_source(source_file, [_source_row("LI7U.DE", 9.15)])
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    calls = []
    output = pd.DataFrame([{"Ticker": "LI7U.DE"}])
    history = pd.DataFrame([{"Ticker": "OLD.DE"}])

    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (output, history))
    monkeypatch.setattr(
        checker, "backfill_missing_market_data_to_source",
        lambda *a, **k: 1,
    )
    monkeypatch.setattr(checker, "google_credentials", lambda: object())
    monkeypatch.setattr(checker, "upsert_google_sheet", lambda *a, **k: calls.append("check-upload"))
    monkeypatch.setattr(
        checker, "_remove_closed_from_source",
        lambda *a, **k: 1,
    )
    monkeypatch.setattr(
        checker, "_sync_cleaned_source_to_drive",
        lambda *a, **k: calls.append("source-sync"),
    )

    checker.main()
    assert calls == ["check-upload"]



def test_backfill_rejects_check_output_with_extra_position(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    source = _source_row("LI7U.DE", 9.15)
    extra = _source_row("SPYI.DE", 11.24)
    _write_source(source_file, [source])

    checked = pd.DataFrame([
        _checked_row(source, 8.26, -9.75),
        _checked_row(extra, 11.14, -0.89),
    ])
    try:
        checker.backfill_missing_market_data_to_source(
            str(source_file), checked
        )
    except RuntimeError as exc:
        assert "nicht denselben Satz offener Positionsschlüssel" in str(exc)
    else:
        raise AssertionError("Zusätzliche Check-Position wurde nicht abgewiesen.")


def test_backfill_preserves_backup_when_readback_and_restore_both_fail(
    tmp_path, monkeypatch
):
    source_file = tmp_path / "Offene_Positionen.csv"
    source = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [source])
    original_bytes = source_file.read_bytes()
    checked = pd.DataFrame([_checked_row(source, 8.26, -9.75)])

    real_read_positions = checker.read_positions
    source_read_count = {"count": 0}

    def fail_post_replace_readback(path):
        if str(path) == str(source_file):
            source_read_count["count"] += 1
            if source_read_count["count"] == 2:
                raise OSError("simulierter Read-back-Fehler")
        return real_read_positions(path)

    real_replace = checker.os.replace
    replace_count = {"count": 0}

    def fail_restore(src, dst):
        replace_count["count"] += 1
        if replace_count["count"] == 2:
            raise OSError("simulierter Restore-Fehler")
        return real_replace(src, dst)

    monkeypatch.setattr(checker, "read_positions", fail_post_replace_readback)
    monkeypatch.setattr(checker.os, "replace", fail_restore)

    try:
        checker.backfill_missing_market_data_to_source(str(source_file), checked)
    except RuntimeError as exc:
        message = str(exc)
        assert "Original-Backup bleibt erhalten" in message
        backup_path = Path(message.split("unter '", 1)[1].split("'", 1)[0])
        assert backup_path.exists()
        assert backup_path.read_bytes() == original_bytes
    else:
        raise AssertionError("Fehlgeschlagener Restore wurde nicht gemeldet.")

    assert source_file.exists()
    assert source_read_count["count"] == 2
    assert replace_count["count"] == 2


def test_backfill_rejects_empty_check_output_when_source_has_open_positions(
    tmp_path
):
    source_file = tmp_path / "Offene_Positionen.csv"
    _write_source(source_file, [_source_row("LI7U.DE", 9.15)])
    before = source_file.read_bytes()

    try:
        checker.backfill_missing_market_data_to_source(
            str(source_file), pd.DataFrame()
        )
    except RuntimeError as exc:
        assert "Check-Ausgabe ist leer" in str(exc)
        assert "offene Positionen enthält" in str(exc)
    else:
        raise AssertionError(
            "Leere Check-Ausgabe bei offenen Quellpositionen wurde akzeptiert."
        )

    assert source_file.read_bytes() == before


def test_backfill_accepts_empty_check_output_only_when_source_has_no_open_positions(
    tmp_path
):
    source_file = tmp_path / "Offene_Positionen.csv"
    columns = [
        "Ticker", "Name", "Einstiegsdatum", "Einstieg", "Status",
        "Aktueller_Kurs", "Performance_Seit_Einstieg%",
    ]
    pd.DataFrame(columns=columns).to_csv(
        source_file, sep=";", index=False, encoding="utf-8-sig"
    )

    assert checker.backfill_missing_market_data_to_source(
        str(source_file), pd.DataFrame()
    ) == 0


def test_backfill_does_not_restore_or_trust_partial_backup_copy(
    tmp_path, monkeypatch
):
    source_file = tmp_path / "Offene_Positionen.csv"
    source = _source_row("LI7U.DE", 9.15)
    _write_source(source_file, [source])
    original_bytes = source_file.read_bytes()
    checked = pd.DataFrame([_checked_row(source, 8.26, -9.75)])

    def fail_after_partial_copy(src, dst):
        with open(src, "rb") as source_handle:
            partial = source_handle.read(20)
        with open(dst, "wb") as backup_handle:
            backup_handle.write(partial)
        raise OSError("simulierter Copy-Fehler nach Teilkopie")

    monkeypatch.setattr(shutil, "copy2", fail_after_partial_copy)

    try:
        checker.backfill_missing_market_data_to_source(
            str(source_file), checked
        )
    except OSError as exc:
        assert "Teilkopie" in str(exc)
    else:
        raise AssertionError("Fehlerhafte Backup-Teilschreibung wurde ignoriert.")

    # A failed backup copy must never be used as a restore source.
    assert source_file.read_bytes() == original_bytes
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "Offene_Positionen.csv"
    ]

def test_closed_position_removal_preserves_original_file_permissions(tmp_path, monkeypatch):
    source_file = tmp_path / "Offene_Positionen.csv"
    instruction = _source_row("ANLEITUNG", 0.0)
    instruction["Status"] = "Anleitung"
    open_row = _source_row("LI7U.DE", 9.15, "8,26", "-9,75")
    closed_row = _source_row("SPYI.DE", 11.24, "11,14", "-0,89")
    closed_row["Status"] = "verkauft"
    _write_source(source_file, [instruction, open_row, closed_row])
    os.chmod(source_file, 0o640)
    closed_df = pd.DataFrame([closed_row])
    monkeypatch.setattr(checker, "_sync_cleaned_source_to_drive", lambda *a, **k: None)

    assert checker._remove_closed_from_source(
        str(source_file), closed_df, drive_creds=object()
    ) == 1
    assert os.stat(source_file).st_mode & 0o777 == 0o640
    result = checker.read_positions(str(source_file))
    assert result["Ticker"].tolist() == ["ANLEITUNG", "LI7U.DE"]



def test_snapshot_manifest_survives_process_state_reset_and_restores_known_state(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    original = b"Ticker;Status\nABC;Offen\n"
    source_file.write_bytes(original)
    snapshot = checker._snapshot_source_file(str(source_file))
    manifest_path = Path(snapshot + ".manifest.json")
    assert manifest_path.exists()
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["original_sha256"] == checker._sha256_file(snapshot)
    assert manifest["allowed_source_states"] == [{
        "sha256": checker._sha256_file(source_file),
        "mode": manifest["source_mode"],
    }]

    # Simulate a fresh process: no in-memory registry exists in V4.
    source_file.write_bytes(b"Ticker;Status\nABC;Gestoppt\n")
    checker._register_snapshot_source_state(
        snapshot, checker._sha256_file(source_file), manifest["source_mode"]
    )
    checker._restore_source_file_snapshot(snapshot, str(source_file))
    assert source_file.read_bytes() == original
    assert Path(snapshot).exists()
    assert manifest_path.exists()


def test_outer_rollback_refuses_to_overwrite_external_local_edit(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    original = b"Ticker;Status\nABC;Offen\n"
    source_file.write_bytes(original)
    snapshot = checker._snapshot_source_file(str(source_file))
    source_file.write_bytes(b"Ticker;Status\nABC;Offen\n# manual edit\n")

    with pytest.raises(RuntimeError, match="Rollback-Konflikt"):
        checker._restore_source_file_snapshot(snapshot, str(source_file))

    assert source_file.read_bytes().endswith(b"# manual edit\n")
    assert Path(snapshot).exists()
    assert Path(snapshot + ".manifest.json").exists()


def test_drive_revision_check_rejects_remote_edit(tmp_path):
    baseline = {
        "file_id": "source-id", "version": "17",
        "modified_time": "2026-11-26T19:00:00.000Z",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "md5_checksum": "",
    }

    class Request:
        def execute(self):
            return {
                "id": "source-id", "version": "18",
                "modifiedTime": "2026-11-26T19:05:00.000Z",
                "mimeType": baseline["mime_type"], "trashed": False,
            }

    class Files:
        def get(self, **kwargs):
            return Request()

    class Drive:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="extern geändert"):
        checker._verify_drive_source_baseline_with_service(Drive(), baseline)


def test_drive_revision_check_accepts_exact_baseline():
    baseline = {
        "file_id": "source-id", "version": "17",
        "modified_time": "2026-11-26T19:00:00.000Z",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "md5_checksum": "",
    }

    class Request:
        def execute(self):
            return {
                "id": "source-id", "version": "17",
                "modifiedTime": "2026-11-26T19:00:00.000Z",
                "mimeType": baseline["mime_type"], "trashed": False,
            }

    class Files:
        def get(self, **kwargs):
            return Request()

    class Drive:
        def files(self):
            return Files()

    result = checker._verify_drive_source_baseline_with_service(Drive(), baseline)
    assert result["version"] == "17"


def test_main_keeps_external_local_edit_when_later_sheets_step_fails(tmp_path, monkeypatch):
    source_file = tmp_path / "Offene_Positionen.csv"
    _write_source(source_file, [_source_row("LI7U.DE", 9.15)])
    original = source_file.read_bytes()
    external = original + b"\n# manual edit during workflow\n"
    monkeypatch.setattr(checker, "INPUT_FILE", str(source_file))
    monkeypatch.setattr(checker, "run_local", lambda *a, **k: (pd.DataFrame(), pd.DataFrame()))
    monkeypatch.setattr(checker, "google_credentials", lambda: object())

    def external_edit(*args, **kwargs):
        source_file.write_bytes(external)
        return 0

    monkeypatch.setattr(checker, "backfill_missing_market_data_to_source", external_edit)
    monkeypatch.setattr(
        checker, "upsert_google_sheet",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulierter Sheets-Fehler")),
    )

    with pytest.raises(RuntimeError, match="Rollback-Konflikt"):
        checker.main()

    assert source_file.read_bytes() == external
    assert list(tmp_path.glob(".*.google_rollback_*.bak"))
    assert list(tmp_path.glob(".*.google_rollback_*.bak.manifest.json"))


def test_backfill_refuses_new_quote_when_existing_performance_would_be_inconsistent(
    tmp_path,
):
    source_file = tmp_path / "Offene_Positionen.csv"
    source_row = _source_row("TEST.DE", 100.00, "", "5,00")
    _write_source(source_file, [source_row])
    original_bytes = source_file.read_bytes()
    checked = pd.DataFrame([_checked_row(source_row, "110,00", "10,00")])

    with pytest.raises(RuntimeError, match="passt nicht zum neu zu speichernden Kurs"):
        checker.backfill_missing_market_data_to_source(str(source_file), checked)

    # Fail closed: neither the new quote nor a silently overwritten performance
    # value may reach the source file.
    assert source_file.read_bytes() == original_bytes


def test_backfill_accepts_existing_performance_when_it_matches_new_quote(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    source_row = _source_row("TEST.DE", 100.00, "", "10,00")
    _write_source(source_file, [source_row])
    checked = pd.DataFrame([_checked_row(source_row, "110,00", "999,00")])

    assert checker.backfill_missing_market_data_to_source(
        str(source_file), checked
    ) == 1
    result = checker.read_positions(str(source_file)).iloc[0]

    assert result["Aktueller_Kurs"] == "110,00"
    assert result["Performance_Seit_Einstieg%"] == "10,00"


def test_backfill_recomputes_performance_from_preserved_source_quote(tmp_path):
    source_file = tmp_path / "Offene_Positionen.csv"
    source_row = _source_row("TEST.DE", 100.00, "90,00", "")
    _write_source(source_file, [source_row])
    checked = pd.DataFrame([_checked_row(source_row, "110,00", "10,00")])

    assert checker.backfill_missing_market_data_to_source(
        str(source_file), checked
    ) == 1
    result = checker.read_positions(str(source_file)).iloc[0]
    assert result["Aktueller_Kurs"] == "90,00"
    assert result["Performance_Seit_Einstieg%"] == "-10,00"


@pytest.mark.parametrize(
    "direction,stop,expected",
    [("Long", "90,00", "-10,00"), ("Short", "110,00", "10,00")],
)
def test_backfill_calculates_performance_using_direction_and_stored_quote(
    tmp_path, direction, stop, expected
):
    source_file = tmp_path / "Offene_Positionen.csv"
    source_row = _source_row("TEST.DE", 100.00, "90,00", "")
    source_row["Richtung"] = direction
    source_row["Stop"] = stop
    _write_source(source_file, [source_row])
    checked = pd.DataFrame([_checked_row(source_row, "110,00", "999,00")])

    checker.backfill_missing_market_data_to_source(str(source_file), checked)
    result = checker.read_positions(str(source_file)).iloc[0]
    assert result["Performance_Seit_Einstieg%"] == expected


def test_backfill_rollback_preserves_external_edit_after_atomic_replace(
    tmp_path, monkeypatch
):
    source_file = tmp_path / "Offene_Positionen.csv"
    row = _source_row("TEST.DE", 100.00)
    _write_source(source_file, [row])
    external_bytes = source_file.read_bytes() + b"\\n# external edit after replace\\n"
    real_replace = checker.os.replace
    injected = {"done": False}

    def replace_then_external_edit(src, dst):
        result = real_replace(src, dst)
        if str(dst) == str(source_file) and not injected["done"]:
            injected["done"] = True
            source_file.write_bytes(external_bytes)
        return result

    monkeypatch.setattr(checker.os, "replace", replace_then_external_edit)
    checked = pd.DataFrame([_checked_row(row, "95,00", "-5,00")])
    with pytest.raises(RuntimeError, match="Rollback abgebrochen"):
        checker.backfill_missing_market_data_to_source(str(source_file), checked)

    assert source_file.read_bytes() == external_bytes
    assert list(tmp_path.glob(".*.bak"))


def test_remove_closed_rollback_preserves_external_edit_after_atomic_replace(
    tmp_path, monkeypatch
):
    source_file = tmp_path / "Offene_Positionen.csv"
    instruction = {
        "Ticker": "ANLEITUNG", "Name": "Hinweis", "Einstiegsdatum": "",
        "Einstieg": "", "Stop": "", "Status": "",
    }
    closed = _source_row("CLOSED.DE", 100.00)
    closed["Status"] = "gestoppt"
    _write_source(source_file, [instruction, closed])
    external_bytes = source_file.read_bytes() + b"\\n# external edit after replace\\n"
    real_replace = checker.os.replace
    injected = {"done": False}

    def replace_then_external_edit(src, dst):
        result = real_replace(src, dst)
        if str(dst) == str(source_file) and not injected["done"]:
            injected["done"] = True
            source_file.write_bytes(external_bytes)
        return result

    monkeypatch.setattr(checker.os, "replace", replace_then_external_edit)
    with pytest.raises(RuntimeError, match="Rollback abgebrochen"):
        checker._remove_closed_from_source(
            str(source_file), pd.DataFrame([closed])
        )

    assert source_file.read_bytes() == external_bytes
    assert list(tmp_path.glob(".*.bak_*"))


def test_local_backup_unlink_failure_after_drive_commit_does_not_roll_back(tmp_path, monkeypatch, capsys):
    source_file = tmp_path / "Offene_Positionen.csv"
    instruction = _source_row("ANLEITUNG", 0.0)
    instruction["Status"] = "Anleitung"
    open_row = _source_row("LI7U.DE", 9.15, "8,26", "-9,75")
    closed_row = _source_row("SPYI.DE", 11.24, "11,14", "-0,89")
    closed_row["Status"] = "verkauft"
    _write_source(source_file, [instruction, open_row, closed_row])
    original_remove = os.remove
    sync_calls = []
    monkeypatch.setattr(
        checker, "_sync_cleaned_source_to_drive",
        lambda *a, **k: sync_calls.append("committed"),
    )

    def fail_backup_unlink(path):
        if ".bak_" in str(path):
            raise PermissionError("simulierter lokaler Backup-Unlink-Fehler")
        return original_remove(path)

    monkeypatch.setattr(checker.os, "remove", fail_backup_unlink)
    removed = checker._remove_closed_from_source(
        str(source_file), pd.DataFrame([closed_row]), drive_creds=object()
    )

    assert removed == 1
    assert sync_calls == ["committed"]
    result = checker.read_positions(str(source_file))
    assert result["Ticker"].tolist() == ["ANLEITUNG", "LI7U.DE"]
    retained = list(tmp_path.glob("*.bak_*")) + list(tmp_path.glob(".*.bak_*"))
    assert retained, "lokales Transaktionsbackup muss für manuelle Bereinigung erhalten bleiben"
    assert "lokale Transaktionsbackup" in capsys.readouterr().out


def test_drive_revision_check_allows_timestamp_only_difference(capsys):
    baseline = {
        "file_id": "source-id", "version": "17",
        "modified_time": "2026-11-26T19:00:00.000Z",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "md5_checksum": "",
    }

    class Request:
        def execute(self):
            return {
                "id": "source-id", "version": "17",
                "modifiedTime": "2026-11-26T19:05:00.000Z",
                "mimeType": baseline["mime_type"], "trashed": False,
            }

    class Files:
        def get(self, **kwargs):
            return Request()

    class Drive:
        def files(self):
            return Files()

    assert checker._verify_drive_source_baseline_with_service(Drive(), baseline)["version"] == "17"
    assert "DRIVE-HINWEIS" in capsys.readouterr().out


def test_drive_revision_check_reports_changed_protected_fields():
    baseline = {
        "file_id": "source-id", "version": "17",
        "modified_time": "2026-11-26T19:00:00.000Z",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "md5_checksum": "",
    }

    class Request:
        def execute(self):
            return {
                "id": "source-id", "version": "18",
                "modifiedTime": baseline["modified_time"],
                "mimeType": baseline["mime_type"], "trashed": False,
            }

    class Files:
        def get(self, **kwargs):
            return Request()

    class Drive:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="version: erwartet='17', aktuell='18'"):
        checker._verify_drive_source_baseline_with_service(Drive(), baseline)
