import csv
import datetime as dt
import sys
import types

import pytest

sys.modules.setdefault("yfinance", types.SimpleNamespace(Ticker=None))
import offene_positionen_check as checker


def _drive_open_rows():
    header = list(checker.HEADERS)
    row = [""] * len(header)
    values = {
        "Ticker": "EUNL.DE",
        "Name": "iShares Core MSCI World UCITS ETF",
        "Steuerungsart": "Buy & Hold",
        "Sektor": "ETF",
        "Richtung": "Long",
        "Status": "Offen",
        "Einstieg": "108,04",
        "Aktueller_Kurs": "125,82",
        "Einstiegsdatum": "02.02.2022",
    }
    for key, value in values.items():
        row[header.index(key)] = value
    title = ["Offene Positionen+Check | Stand 27.11.2026 04:25"] + [""] * (len(header) - 1)
    return [title, header, row]


def test_staging_reads_verified_drive_rows_without_replacing_consumer_csv(tmp_path):
    target = tmp_path / checker.OUTPUT_CSV
    target.write_text("KEEP;OLD;LOCAL\n", encoding="utf-8")

    staged, destination, count = checker._stage_verified_open_tab_csv(
        _drive_open_rows(), "drive-spreadsheet-id", str(target)
    )

    assert destination == str(target)
    assert count == 1
    assert target.read_text(encoding="utf-8") == "KEEP;OLD;LOCAL\n"
    assert __import__("os").path.exists(staged)
    with open(staged, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    assert rows[0]["Ticker"] == "EUNL.DE"
    assert rows[0]["Status"] == "Offen"

    checker._publish_staged_open_tab_csv(staged, destination)
    with open(target, "r", encoding="utf-8-sig", newline="") as handle:
        published = list(csv.DictReader(handle, delimiter=";"))
    assert published[0]["Ticker"] == "EUNL.DE"


def test_drive_readback_export_rejects_wrong_header_without_touching_existing_csv(tmp_path):
    target = tmp_path / checker.OUTPUT_CSV
    target.write_text("KEEP;OLD;FILE\n", encoding="utf-8")
    rows = _drive_open_rows()
    rows[1] = ["wrong"] * len(checker.HEADERS)

    with pytest.raises(RuntimeError, match="unerwarteten Header"):
        checker._stage_verified_open_tab_csv(rows, "drive-id", str(target))

    assert target.read_text(encoding="utf-8") == "KEEP;OLD;FILE\n"


def test_drive_readback_rejects_extra_data_columns_and_keeps_existing_csv(tmp_path):
    target = tmp_path / checker.OUTPUT_CSV
    target.write_text("KEEP;OLD;FILE\n", encoding="utf-8")
    rows = _drive_open_rows()
    rows[2].append("UNEXPECTED_DATA")

    with pytest.raises(RuntimeError, match="außerhalb des erwarteten Headers"):
        checker._stage_verified_open_tab_csv(rows, "drive-id", str(target))

    assert target.read_text(encoding="utf-8") == "KEEP;OLD;FILE\n"


def test_drive_readback_allows_empty_trailing_columns_and_ignores_empty_rows(tmp_path):
    rows = _drive_open_rows()
    rows[2].append(None)
    rows.append([None] * len(checker.HEADERS))
    target = tmp_path / checker.OUTPUT_CSV

    staged, destination, count = checker._stage_verified_open_tab_csv(
        rows, "drive-id", str(target)
    )

    assert destination == str(target)
    assert count == 1
    checker._publish_staged_open_tab_csv(staged, destination)


def test_typed_excel_values_are_serialized_from_drive_readback(tmp_path):
    rows = _drive_open_rows()
    header = list(checker.HEADERS)
    rows[2][header.index("Einstiegsdatum")] = dt.datetime(2022, 2, 2)
    rows[2][header.index("Einstieg")] = 108.04
    target = tmp_path / checker.OUTPUT_CSV

    staged, destination, _ = checker._stage_verified_open_tab_csv(
        rows, "drive-id", str(target)
    )
    with open(staged, "r", encoding="utf-8-sig", newline="") as handle:
        data = list(csv.DictReader(handle, delimiter=";"))
    assert data[0]["Einstiegsdatum"] == "2022-02-02 00:00:00"
    assert data[0]["Einstieg"] == "108.04"
    assert not target.exists()
    checker._publish_staged_open_tab_csv(staged, destination)


def test_drive_readback_rejects_blank_ticker_and_non_open_status(tmp_path):
    target = tmp_path / checker.OUTPUT_CSV
    target.write_text("KEEP;OLD;FILE\n", encoding="utf-8")
    rows = _drive_open_rows()
    rows[2][checker.HEADERS.index("Ticker")] = ""
    with pytest.raises(RuntimeError, match="ohne Ticker"):
        checker._stage_verified_open_tab_csv(rows, "drive-id", str(target))
    assert target.read_text(encoding="utf-8") == "KEEP;OLD;FILE\n"

    rows = _drive_open_rows()
    rows[2][checker.HEADERS.index("Status")] = "Verkauft"
    with pytest.raises(RuntimeError, match="nicht offenen Status"):
        checker._stage_verified_open_tab_csv(rows, "drive-id", str(target))
    assert target.read_text(encoding="utf-8") == "KEEP;OLD;FILE\n"


def test_github_actions_fails_closed_without_google_drive_credentials(monkeypatch, tmp_path):
    import pandas as pd

    monkeypatch.chdir(tmp_path)
    (tmp_path / checker.INPUT_FILE).write_text(
        "Ticker;Status\\nAAA;Offen\\n", encoding="utf-8-sig"
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(
        checker, "run_local",
        lambda *args: (
            pd.DataFrame(columns=checker.HEADERS),
            pd.DataFrame(columns=checker.HISTORY_HEADERS),
        ),
    )
    monkeypatch.setattr(checker, "google_credentials", lambda: None)
    monkeypatch.setattr(checker, "_load_drive_source_baseline", lambda *args: {"file_id": "master"})
    def require_drive_credentials(creds, baseline):
        assert creds is None
        raise RuntimeError("Drive-Readback benötigt gültige Google-Credentials.")
    monkeypatch.setattr(checker, "_verify_drive_source_baseline", require_drive_credentials)
    monkeypatch.setattr(
        checker, "upsert_google_sheet",
        lambda *args: pytest.fail("Ohne verifizierte Drive-Quelle darf kein Sheet-Upload starten."),
    )

    with pytest.raises(RuntimeError, match="gültige Google-Credentials"):
        checker.main()


def test_failed_atomic_publish_keeps_previous_consumer_csv(monkeypatch, tmp_path):
    target = tmp_path / checker.OUTPUT_CSV
    target.write_text("KEEP;OLD;FILE\\n", encoding="utf-8")
    staged, destination, _ = checker._stage_verified_open_tab_csv(
        _drive_open_rows(), "drive-id", str(target)
    )
    monkeypatch.setattr(
        checker.os, "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulierter Replace-Fehler")),
    )

    with pytest.raises(OSError, match="Replace-Fehler"):
        checker._publish_staged_open_tab_csv(staged, destination)

    assert target.read_text(encoding="utf-8") == "KEEP;OLD;FILE\\n"
    assert not __import__("os").path.exists(staged)


def _install_upsert_readback_harness(monkeypatch, tmp_path, *, fail_after_stage=False):
    """Run the real upsert orchestration against deterministic fake Sheets APIs."""
    import pandas as pd

    monkeypatch.chdir(tmp_path)
    spreadsheet_id = "google-master-id"
    header = list(checker.HEADERS)

    local_values = {column: "" for column in header}
    local_values.update({
        "Ticker": "EUNL.DE",
        "Name": "NAME_FROM_LOCAL_CALCULATION",
        "Richtung": "Long",
        "Status": "Offen",
    })
    local_df = pd.DataFrame([local_values], columns=header)

    # The Google readback deliberately differs from the calculated/local row.
    # The orchestration must export this verified Google row, not local_values.
    drive_row = [local_values[column] for column in header]
    drive_row[header.index("Name")] = "NAME_FROM_GOOGLE_SHEETS"
    drive_rows = [
        ["Offene Positionen+Check | Stand Integrationstest"] + [""] * (len(header) - 1),
        header,
        drive_row,
    ]

    history_header = list(checker.HISTORY_DISPLAY_HEADERS)
    history_row = [""] * len(history_header)
    if history_header:
        history_row[0] = "1"
    history_rows = [
        ["Geschlossene Positionen | Stand Integrationstest"] + [""] * (len(history_header) - 1),
        history_header,
        history_row,
    ]

    class _Request:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class _Values:
        def get(self, *, spreadsheetId, range):
            if "Offene Positionen+Check" in range:
                return _Request({"values": drive_rows})
            if "Geschlossene Positionen" in range:
                return _Request({"values": history_rows})
            raise AssertionError(f"Unerwarteter Sheets-Lesezugriff: {range}")

    class _Spreadsheets:
        def get(self, **kwargs):
            return _Request({"sheets": [
                {"properties": {"title": "Offene Positionen+Check"}},
                {"properties": {"title": "Geschlossene Positionen"}},
            ]})

        def batchUpdate(self, **kwargs):
            return _Request({})

        def values(self):
            return _Values()

    class _SheetsService:
        def spreadsheets(self):
            return _Spreadsheets()

    class _DriveService:
        pass

    monkeypatch.setattr(checker, "build", lambda service, *args, **kwargs: (
        _SheetsService() if service == "sheets" else _DriveService()
    ), raising=False)
    monkeypatch.setattr(
        checker, "_find_or_initialize_master",
        lambda drive, closed_df: (spreadsheet_id, False, "unused-init-name"),
    )
    monkeypatch.setattr(checker, "read_existing_history", lambda *args, **kwargs: history_rows)
    monkeypatch.setattr(checker, "MINIMUM_PROTECTED_HISTORY_ROWS", 0)
    monkeypatch.setattr(checker, "_history_key", lambda row: ("stable-history-row",))
    monkeypatch.setattr(checker, "_snapshot_google_sheet", lambda *args: None)
    monkeypatch.setattr(checker, "_write_sheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(checker, "_append_missing_history_rows", lambda *args, **kwargs: 0)
    monkeypatch.setattr(checker, "_sort_and_number_closed_history", lambda *args: 0)
    monkeypatch.setattr(checker, "_align_history_date_columns_best_effort", lambda *args: True)
    monkeypatch.setattr(
        checker, "_swap_temp_tabs",
        lambda *args, **kwargs: {"Offene Positionen+Check": "backup-open-tab"},
    )
    monkeypatch.setattr(checker, "_rollback_productive_swap", lambda *args: None)
    if fail_after_stage:
        def fail_tab_order(*args, **kwargs):
            raise RuntimeError("simulierter Fehler nach Readback-Staging")
        monkeypatch.setattr(checker, "_enforce_productive_tab_order", fail_tab_order)
    else:
        monkeypatch.setattr(checker, "_enforce_productive_tab_order", lambda *args: None)
    monkeypatch.setattr(checker, "_update_closed_history_timestamp", lambda *args: "ok")

    closed_df = pd.DataFrame(columns=checker.HISTORY_HEADERS)
    return local_df, closed_df, drive_row


def test_full_upsert_publishes_exact_google_readback_not_preexisting_local_csv(
    monkeypatch, tmp_path
):
    """End-to-end regression: the verified Sheets master is the CSV's source of truth."""
    target = tmp_path / checker.OUTPUT_CSV
    target.write_text("STALE_LOCAL_FILE;MUST_NOT_WIN\nold;content\n", encoding="utf-8")
    local_df, closed_df, drive_row = _install_upsert_readback_harness(monkeypatch, tmp_path)

    result = checker.upsert_google_sheet(local_df, closed_df, creds=object())

    assert result == "google-master-id"
    with open(target, "r", encoding="utf-8-sig", newline="") as handle:
        published = list(csv.DictReader(handle, delimiter=";"))
    assert len(published) == 1
    assert published[0]["Ticker"] == "EUNL.DE"
    assert published[0]["Name"] == "NAME_FROM_GOOGLE_SHEETS"
    assert published[0]["Name"] != local_df.iloc[0]["Name"]
    assert published[0]["Name"] == drive_row[checker.HEADERS.index("Name")]
    assert not list(tmp_path.glob(f"{checker.OUTPUT_CSV}.staged.*.tmp"))


def test_full_upsert_failure_after_readback_staging_does_not_replace_local_csv(
    monkeypatch, tmp_path
):
    """A later Google validation failure must leave the old consumer CSV untouched."""
    target = tmp_path / checker.OUTPUT_CSV
    original = "KEEP_EXISTING_LOCAL_CSV;DO_NOT_REPLACE\nold;content\n"
    target.write_text(original, encoding="utf-8")
    local_df, closed_df, _ = _install_upsert_readback_harness(
        monkeypatch, tmp_path, fail_after_stage=True
    )

    with pytest.raises(RuntimeError, match="simulierter Fehler nach Readback-Staging"):
        checker.upsert_google_sheet(local_df, closed_df, creds=object())

    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(f"{checker.OUTPUT_CSV}.staged.*.tmp"))


def _install_main_output_transaction_harness(monkeypatch, tmp_path, *, fail_upsert=False):
    """Exercise main() around its real local-output staging boundary."""
    import os
    import pandas as pd

    monkeypatch.chdir(tmp_path)
    target = tmp_path / checker.OUTPUT_CSV
    original = "KEEP_PREVIOUS_VERIFIED_READBACK;DO_NOT_OVERWRITE\nold;row\n"
    target.write_text(original, encoding="utf-8")

    output_values = {column: "" for column in checker.HEADERS}
    output_values.update({
        "Ticker": "EUNL.DE",
        "Name": "LOCAL_CALCULATION_ONLY",
        "Richtung": "Long",
        "Status": "Offen",
    })
    calculated_df = pd.DataFrame([output_values], columns=checker.HEADERS)
    closed_df = pd.DataFrame(columns=checker.HISTORY_HEADERS)

    observed = {"calculation_stage": None}

    def fake_run_local(input_file, output_csv):
        stage = os.path.abspath(output_csv)
        observed["calculation_stage"] = stage
        assert stage != str(target)
        assert os.path.dirname(stage) == str(tmp_path)
        # This is the exact write run_local() performs, but directed only to
        # the disposable calculation stage.
        with open(output_csv, "w", encoding="utf-8") as handle:
            handle.write("LOCAL_CALCULATION_ONLY\n")
        assert target.read_text(encoding="utf-8") == original
        return calculated_df, closed_df

    monkeypatch.setattr(checker, "_snapshot_source_file", lambda *args: None)
    monkeypatch.setattr(checker, "_discard_source_file_snapshot", lambda *args: None)
    monkeypatch.setattr(checker, "_restore_source_file_snapshot", lambda *args: None)
    monkeypatch.setattr(checker, "google_credentials", lambda: object())
    monkeypatch.setattr(
        checker, "_load_drive_source_baseline", lambda *args: {"file_id": "source-id"}
    )
    monkeypatch.setattr(checker, "_verify_drive_source_baseline", lambda *args: None)
    monkeypatch.setattr(
        checker, "run_local", fake_run_local
    )
    monkeypatch.setattr(
        checker, "backfill_missing_market_data_to_source", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        checker, "_assert_source_matches_known_snapshot_state", lambda *args: None
    )
    monkeypatch.setattr(
        checker, "_remove_closed_from_source", lambda *args, **kwargs: 0
    )

    if fail_upsert:
        def fake_upsert(*args, **kwargs):
            assert target.read_text(encoding="utf-8") == original
            raise RuntimeError("simulierter Google-Readback-Fehler")
    else:
        def fake_upsert(*args, **kwargs):
            # Simulate the real upsert's final action: publish only rows
            # obtained from the verified Google Sheets readback.
            staged, destination, count = checker._stage_verified_open_tab_csv(
                _drive_open_rows(), "google-master-id", checker.OUTPUT_CSV
            )
            checker._publish_staged_open_tab_csv(staged, destination)
            assert count == 1
            return "google-master-id"

    monkeypatch.setattr(checker, "upsert_google_sheet", fake_upsert)
    return target, original, observed


def test_main_keeps_local_calculation_separate_and_publishes_google_readback(
    monkeypatch, tmp_path
):
    """main() must not expose run_local's CSV; only Google readback becomes OUTPUT_CSV."""
    target, original, observed = _install_main_output_transaction_harness(
        monkeypatch, tmp_path
    )

    checker.main()

    assert observed["calculation_stage"]
    assert observed["calculation_stage"] != str(target)
    assert not __import__("os").path.exists(observed["calculation_stage"])
    with open(target, "r", encoding="utf-8-sig", newline="") as handle:
        published = list(csv.DictReader(handle, delimiter=";"))
    assert len(published) == 1
    assert published[0]["Name"] == "iShares Core MSCI World UCITS ETF"
    assert published[0]["Name"] != "LOCAL_CALCULATION_ONLY"
    assert target.read_text(encoding="utf-8") != original
    assert not list(tmp_path.glob(f".{checker.OUTPUT_CSV}.calculation.*.tmp"))


def test_main_failure_before_google_readback_preserves_previous_csv_byte_for_byte(
    monkeypatch, tmp_path
):
    """A failed Google readback must not leave run_local's calculated CSV in OUTPUT_CSV."""
    target, original, observed = _install_main_output_transaction_harness(
        monkeypatch, tmp_path, fail_upsert=True
    )

    with pytest.raises(RuntimeError, match="Google-/Drive-Ablauf fehlgeschlagen"):
        checker.main()

    assert observed["calculation_stage"]
    assert target.read_text(encoding="utf-8") == original
    assert not __import__("os").path.exists(observed["calculation_stage"])
    assert not list(tmp_path.glob(f".{checker.OUTPUT_CSV}.calculation.*.tmp"))
