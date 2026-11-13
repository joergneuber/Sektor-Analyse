import pandas as pd
import pytest
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offene_positionen_check", ROOT / "offene_positionen_check.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def row(ticker, name, entry_date, entry, status="Gestoppt"):
    data = {c: "" for c in mod.HISTORY_HEADERS}
    data.update({
        "Ticker": ticker,
        "Name": name,
        "Einstiegsdatum": entry_date,
        "Einstieg": entry,
        "Status": status,
        "Ausstiegsdatum": "10.09.2026",
        "Ausstiegskurs": "60.00",
    })
    return data


def test_closed_history_is_permanent_and_never_replaced_by_smaller_snapshot():
    # Simuliert den kritischen JOST-Fall: Tab 2 enthält bereits 28 Trades,
    # der nächste Lauf liefert lokal aber nur den neuen/aktuellen Bestand.
    old = [mod.HISTORY_HEADERS,
           [row("JST.DE", "JOST Werke SE", "21.08.2026", "59.40")[c] for c in mod.HISTORY_HEADERS],
           [row("ABC.DE", "Existing Trade", "01.09.2026", "10.00")[c] for c in mod.HISTORY_HEADERS]]
    new = pd.DataFrame([row("NEW.DE", "New Trade", "10.09.2026", "20.00")], columns=mod.HISTORY_HEADERS)

    merged = mod.merge_closed_history(old, new)
    tickers = set(merged["Ticker"])
    assert "JST.DE" in tickers
    assert "ABC.DE" in tickers
    assert "NEW.DE" in tickers
    assert len(merged) == 3


def test_upsert_does_not_use_tab1_disappearance_as_a_close_event():
    source = (ROOT / "offene_positionen_check.py").read_text(encoding="utf-8-sig")
    active = source[source.index("def upsert_google_sheet"):source.index("def _remove_closed_from_source")]
    assert "extract_disappeared_open_positions(existing_open_rows, df)" not in active
    assert 'temp_open: "Offene Positionen+Check"' in active
    assert 'temp_closed: "Geschlossene Positionen"' not in active


def test_tracker_documents_permanent_archive():
    tracker_path = ROOT / "positionen_tracker.py"
    if not tracker_path.exists():
        pytest.skip("Unveränderte positionen_tracker.py ist im fokussierten ZIP nicht enthalten.")
    source = tracker_path.read_text(encoding="utf-8-sig")
    assert "erscheint sie dauerhaft" in source
    assert "10 Werktage lang" not in source


def test_productive_tab_order_is_explicitly_enforced():
    source = (ROOT / "offene_positionen_check.py").read_text(encoding="utf-8-sig")
    start = source.index("def _enforce_productive_tab_order")
    end = source.index("def _validate_output_before_upload", start)
    active = source[start:end]
    assert '"Offene Positionen+Check"' in active
    assert '"Geschlossene Positionen"' in active
    assert '"index": 0' in active
    assert '"index": 1' in active
    assert 'fields": "index"' in active
    assert 'titles != expected' in active


def test_merge_accepts_google_sheet_metadata_header_and_preserves_existing_duplicate():
    metadata = ["Geschlossene Positionen | historische Faktenbasis"] + [""] * (len(mod.HISTORY_HEADERS) - 1)
    old_row = row("JST.DE", "JOST Werke SE", "21.08.2026", "59.40")
    changed_duplicate = row("JST.DE", "JOST Werke SE", "21.08.2026", "59.40")
    changed_duplicate["Ausstiegsdatum"] = "11.09.2026"
    changed_duplicate["Ausstiegskurs"] = "61.00"
    new_row = row("NEW.DE", "New Trade", "10.09.2026", "20.00")

    old_sheet = [
        metadata,
        mod.HISTORY_HEADERS,
        [old_row[c] for c in mod.HISTORY_HEADERS],
    ]
    incoming = pd.DataFrame(
        [[changed_duplicate[c] for c in mod.HISTORY_HEADERS],
         [new_row[c] for c in mod.HISTORY_HEADERS]],
        columns=mod.HISTORY_HEADERS,
    )

    merged = mod.merge_closed_history(old_sheet, incoming)
    assert set(merged["Ticker"]) == {"JST.DE", "NEW.DE"}
    assert len(merged) == 2
    jst = merged.loc[merged["Ticker"] == "JST.DE"].iloc[0]
    assert jst["Ausstiegsdatum"] == "10.09.2026"
    assert jst["Ausstiegskurs"] == "60.00"


class _FakeSheetsValues:
    def __init__(self, owner):
        self.owner = owner

    def get(self, **kwargs):
        return _FakeRequest({"values": self.owner.history_rows})

    def append(self, **kwargs):
        values = kwargs["body"]["values"]
        self.owner.history_rows.extend(values)
        return _FakeRequest({"updates": {"updatedRows": len(values)}})


class _FakeSheetsSpreadsheets:
    def __init__(self, owner):
        self.owner = owner
        self._values = _FakeSheetsValues(owner)

    def get(self, **kwargs):
        return _FakeRequest({
            "sheets": [{
                "properties": {
                    "title": "Geschlossene Positionen",
                    "sheetId": 42,
                }
            }]
        })

    def values(self):
        return self._values


class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeSheets:
    def __init__(self, history_rows):
        self.history_rows = [list(r) for r in history_rows]
        self._spreadsheets = _FakeSheetsSpreadsheets(self)

    def spreadsheets(self):
        return self._spreadsheets


def _history_sheet_rows(*items):
    return [
        ["Geschlossene Positionen | historische Faktenbasis"] + [""] * (len(mod.HISTORY_HEADERS) - 1),
        list(mod.HISTORY_HEADERS),
        *[[item[c] for c in mod.HISTORY_HEADERS] for item in items],
    ]


def test_productive_history_path_keeps_two_nem_trades_separate(monkeypatch):
    monkeypatch.setattr(mod, "MINIMUM_PROTECTED_HISTORY_ROWS", 0)
    """Regression: zwei manuelle NEM-Trades dürfen im produktiven Append-Pfad
    nicht anhand des Tickers zu einer Position verschmolzen werden.
    """
    nem_1 = row("NEM", "Newmont Corporation", "01.09.2026", "130.00")
    nem_2 = row("NEM", "Newmont Corporation", "05.09.2026", "140.00")

    fake = _FakeSheets(_history_sheet_rows())
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)

    appended = mod._append_missing_history_rows(
        fake, "TEST-SPREADSHEET", pd.DataFrame(
            [[nem_1[c] for c in mod.HISTORY_HEADERS],
             [nem_2[c] for c in mod.HISTORY_HEADERS]],
            columns=mod.HISTORY_HEADERS,
        ),
    )

    assert appended == 2
    history = fake.history_rows[2:]
    assert len(history) == 2
    assert {r[mod.HISTORY_HEADERS.index("Einstieg")] for r in history} == {130.0, 140.0}
    assert {r[mod.HISTORY_HEADERS.index("Einstiegsdatum")] for r in history} == {
        "01.09.2026", "05.09.2026"
    }


def test_productive_history_path_allows_same_exit_for_two_nem_trades(monkeypatch):
    monkeypatch.setattr(mod, "MINIMUM_PROTECTED_HISTORY_ROWS", 0)
    """Regression: gleicher Ausstieg ist kein Identitätsmerkmal und darf zwei
    unterschiedliche NEM-Trades nicht zusammenführen.
    """
    nem_1 = row("NEM", "Newmont Corporation", "01.09.2026", "130.00")
    nem_2 = row("NEM", "Newmont Corporation", "05.09.2026", "140.00")
    nem_1["Ausstiegskurs"] = "110.00"
    nem_2["Ausstiegskurs"] = "110.00"

    fake = _FakeSheets(_history_sheet_rows())
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)

    appended = mod._append_missing_history_rows(
        fake, "TEST-SPREADSHEET", pd.DataFrame(
            [[nem_1[c] for c in mod.HISTORY_HEADERS],
             [nem_2[c] for c in mod.HISTORY_HEADERS]],
            columns=mod.HISTORY_HEADERS,
        ),
    )

    assert appended == 2
    history = fake.history_rows[2:]
    assert len(history) == 2
    exit_idx = mod.HISTORY_HEADERS.index("Ausstiegskurs")
    assert [r[exit_idx] for r in history] == [110.0, 110.0]


def test_productive_history_path_second_run_does_not_duplicate_nem_trades(monkeypatch):
    monkeypatch.setattr(mod, "MINIMUM_PROTECTED_HISTORY_ROWS", 0)
    """Regression: ein identischer zweiter Abschlusslauf darf keine Duplikate erzeugen."""
    nem_1 = row("NEM", "Newmont Corporation", "01.09.2026", "130.00")
    nem_2 = row("NEM", "Newmont Corporation", "05.09.2026", "140.00")

    fake = _FakeSheets(_history_sheet_rows(nem_1, nem_2))
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)

    incoming = pd.DataFrame(
        [[nem_1[c] for c in mod.HISTORY_HEADERS],
         [nem_2[c] for c in mod.HISTORY_HEADERS]],
        columns=mod.HISTORY_HEADERS,
    )

    appended = mod._append_missing_history_rows(fake, "TEST-SPREADSHEET", incoming)

    assert appended == 0
    assert len(fake.history_rows) == 4


def test_productive_history_path_survives_smaller_followup_snapshot(monkeypatch):
    monkeypatch.setattr(mod, "MINIMUM_PROTECTED_HISTORY_ROWS", 0)
    """Regression: ein kleinerer/leer werdender Folgesnapshot darf bestehende
    historische NEM-Trades nicht löschen.
    """
    nem_1 = row("NEM", "Newmont Corporation", "01.09.2026", "130.00")
    nem_2 = row("NEM", "Newmont Corporation", "05.09.2026", "140.00")

    fake = _FakeSheets(_history_sheet_rows(nem_1, nem_2))
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)

    # Der produktive Helper bekommt in diesem Lauf keinen geschlossenen NEM-Trade.
    incoming = pd.DataFrame(columns=mod.HISTORY_HEADERS)
    appended = mod._append_missing_history_rows(fake, "TEST-SPREADSHEET", incoming)

    assert appended == 0
    assert len(fake.history_rows) == 4
    tickers = [r[mod.HISTORY_HEADERS.index("Ticker")] for r in fake.history_rows[2:]]
    assert tickers == ["NEM", "NEM"]


def _protected_history_fixture(count=51):
    items = [
        row(f"BASE{i}.DE", f"Baseline Trade {i}", f"{1 + (i % 28):02d}.01.{2020 + i // 28}", str(10 + i))
        for i in range(count)
    ]
    return _history_sheet_rows(*items)


def test_archive_guard_rejects_history_below_51_without_appending(monkeypatch):
    fake = _FakeSheets(_protected_history_fixture(50))
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)
    incoming = pd.DataFrame([row("NEW.DE", "New Trade", "01.11.2026", "99")], columns=mod.HISTORY_HEADERS)

    import pytest
    with pytest.raises(RuntimeError, match="50 < 51"):
        mod._append_missing_history_rows(fake, "TEST-SPREADSHEET", incoming)
    assert len(fake.history_rows) == 52  # metadata + header + unchanged 50 rows


def test_archive_guard_accepts_51_and_appends_without_losing_existing_rows(monkeypatch):
    fake = _FakeSheets(_protected_history_fixture(51))
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)
    incoming = pd.DataFrame([row("NEW.DE", "New Trade", "01.11.2026", "99")], columns=mod.HISTORY_HEADERS)

    appended = mod._append_missing_history_rows(fake, "TEST-SPREADSHEET", incoming)
    assert appended == 1
    assert len(fake.history_rows) == 54  # metadata + header + 51 old + 1 new


def test_archive_guard_rejects_duplicate_existing_trade_keys(monkeypatch):
    duplicate = row("DUP.DE", "Duplicate Trade", "01.11.2026", "10")
    fixture = _protected_history_fixture(51)
    fixture.extend([[duplicate[c] for c in mod.HISTORY_HEADERS], [duplicate[c] for c in mod.HISTORY_HEADERS]])
    fake = _FakeSheets(fixture)
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)
    incoming = pd.DataFrame([row("NEW.DE", "New Trade", "02.11.2026", "99")], columns=mod.HISTORY_HEADERS)

    import pytest
    with pytest.raises(RuntimeError, match="doppelter Positionsschlüssel"):
        mod._append_missing_history_rows(fake, "TEST-SPREADSHEET", incoming)


def test_read_existing_history_propagates_google_read_errors():
    class BrokenRequest:
        def execute(self):
            raise OSError("simulierter Google-Lesefehler")
    class BrokenSpreadsheets:
        def get(self, **kwargs):
            return BrokenRequest()
    class BrokenSheets:
        def spreadsheets(self):
            return BrokenSpreadsheets()

    import pytest
    with pytest.raises(RuntimeError, match="konnte nicht zuverlässig gelesen werden"):
        mod.read_existing_history(BrokenSheets(), "TEST-SPREADSHEET")


def test_read_existing_history_rejects_missing_tab_for_existing_master():
    class Request:
        def execute(self):
            return {"sheets": [{"properties": {"title": "Offene Positionen+Check"}}]}
    class Spreadsheets:
        def get(self, **kwargs):
            return Request()
    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    import pytest
    with pytest.raises(RuntimeError, match="Tab 'Geschlossene Positionen' fehlt"):
        mod.read_existing_history(Sheets(), "TEST-SPREADSHEET")
    assert mod.read_existing_history(Sheets(), "TEST-SPREADSHEET", allow_missing=True) == []


def test_incoming_duplicate_trade_keys_abort_before_append(monkeypatch):
    fake = _FakeSheets(_protected_history_fixture(51))
    monkeypatch.setattr(mod, "_format_history_range", lambda *args, **kwargs: None)
    duplicate = row("NEW.DE", "New Trade", "03.11.2026", "99")
    incoming = pd.DataFrame(
        [[duplicate[c] for c in mod.HISTORY_HEADERS], [duplicate[c] for c in mod.HISTORY_HEADERS]],
        columns=mod.HISTORY_HEADERS,
    )
    import pytest
    with pytest.raises(RuntimeError, match="doppelter Positionsschlüssel in Eingangsdaten"):
        mod._append_missing_history_rows(fake, "TEST-SPREADSHEET", incoming)
    assert len(fake.history_rows) == 53


def test_master_selection_and_backup_retention_are_fail_closed():
    source = (ROOT / "offene_positionen_check.py").read_text(encoding="utf-8-sig")
    start = source.index("def upsert_google_sheet")
    end = source.index("def _position_key", start)
    active = source[start:end]
    selector_start = source.index("def _find_or_initialize_master")
    selector_end = source.index("def _finalize_new_master", selector_start)
    selector = source[selector_start:selector_end]
    assert "_find_or_initialize_master(" in active
    assert "if len(files) > 1" in selector
    assert 'return files[0]["id"], False, None' in selector
    assert "mimeType='{GOOGLE_SHEETS_MIME_TYPE}'" in selector
    assert 'item.get("mimeType") == GOOGLE_SHEETS_MIME_TYPE' in selector
    assert "_cleanup_backups(sheets, spreadsheet_id)" not in active
    assert "_cleanup_drive_backups(drive)" not in active
    assert "dauerhaft erhalten" in active


def test_existing_history_reader_never_converts_google_errors_to_empty_archive():
    import inspect
    source = inspect.getsource(mod.read_existing_history)
    assert "return []" in source  # only explicit first-run missing-tab allowance
    assert "raise RuntimeError" in source
    assert "return response.get(\"values\", [])" not in source


def test_source_sync_refuses_ambiguous_master_and_keeps_old_source_backup():
    source = (ROOT / "offene_positionen_check.py").read_text(encoding="utf-8-sig")
    start = source.index("def _sync_cleaned_source_to_drive")
    end = source.index("def _remove_closed_from_source", start)
    active = source[start:end]
    assert "if len(sheet_files) > 1" in active
    assert "es wird keine Datei willkürlich ausgewählt" in active
    assert "Drive-Quellbackup bleibt dauerhaft erhalten" in active
    assert "drive.files().delete(fileId=backup_id)" not in active


def test_upsert_aborts_on_multiple_same_named_master_sheets(monkeypatch):
    class Request:
        def execute(self):
            return {"files": [
                {"id": "master-1", "name": mod.DRIVE_NAME, "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
                {"id": "master-2", "name": mod.DRIVE_NAME, "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
            ]}
    class Files:
        def list(self, **kwargs):
            if "name contains" in kwargs.get("q", ""):
                return type("EmptyRequest", (), {"execute": lambda self: {"files": []}})()
            return Request()
        def create(self, **kwargs):
            raise AssertionError("Bei mehrdeutigem Master darf nichts erstellt werden.")
    class Drive:
        def files(self):
            return Files()
    def fake_build(service, version, credentials=None):
        return Drive() if service == "drive" else object()
    monkeypatch.setattr(mod, "build", fake_build, raising=False)

    import pytest
    open_df = pd.DataFrame([{"Ticker": "AAA", "Status": "Offen"}])
    closed_df = pd.DataFrame(columns=["Ticker", "Status"])
    with pytest.raises(RuntimeError, match="mehrere gleichnamige Master-Sheets"):
        mod.upsert_google_sheet(open_df, closed_df, creds=object())


def test_new_master_requires_complete_51_trade_initial_history():
    short_history = pd.DataFrame(
        [row(f"T{i}.DE", f"Trade {i}", f"{i+1:02d}.09.2026", str(10+i))
         for i in range(mod.MINIMUM_PROTECTED_HISTORY_ROWS - 1)],
        columns=mod.HISTORY_HEADERS,
    )
    with pytest.raises(RuntimeError, match="neuer Google-Master"):
        mod._validate_new_master_history(short_history)

    complete_history = pd.DataFrame(
        [row(f"T{i}.DE", f"Trade {i}", f"{i+1:02d}.09.2026", str(10+i))
         for i in range(mod.MINIMUM_PROTECTED_HISTORY_ROWS)],
        columns=mod.HISTORY_HEADERS,
    )
    assert mod._validate_new_master_history(complete_history) is None


def _recovery_fixture_frame(count=51, status="Gestoppt"):
    return pd.DataFrame(
        [row(f"REC{i}.DE", f"Recovery Trade {i}", f"{1 + (i % 28):02d}.01.{2020 + i // 28}", str(10 + i), status=status)
         for i in range(count)],
        columns=mod.HISTORY_HEADERS,
    )


def test_private_recovery_history_loads_51_records_from_verified_secret(monkeypatch):
    import base64
    import hashlib
    frame = _recovery_fixture_frame(51)
    raw = frame.to_csv(sep=";", index=False, encoding="utf-8").encode("utf-8")
    monkeypatch.setattr(mod, "RECOVERY_HISTORY_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv(mod.RECOVERY_HISTORY_ENV, base64.b64encode(raw).decode("ascii"))

    loaded = mod.load_initial_history("must-not-be-read.csv")

    assert len(loaded) == 51
    assert loaded["Ticker"].nunique() == 51
    assert set(loaded["Status"]) == {"Gestoppt"}


def test_private_recovery_history_accepts_base64_whitespace(monkeypatch):
    import base64
    import hashlib

    frame = _recovery_fixture_frame(51)
    raw = frame.to_csv(sep=";", index=False, encoding="utf-8").encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    # Zeilenumbrüche/Leerzeichen dürfen den Secret-Transport nicht beschädigen.
    wrapped = "  " + encoded[:64] + "\n" + encoded[64:128] + "  \n" + encoded[128:] + "  "
    monkeypatch.setattr(mod, "RECOVERY_HISTORY_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv(mod.RECOVERY_HISTORY_ENV, wrapped)

    loaded = mod.load_initial_history("must-not-be-read.csv")

    assert len(loaded) == 51
    assert loaded["Ticker"].nunique() == 51


def test_private_recovery_history_rejects_wrong_sha256(monkeypatch):
    import base64
    raw = _recovery_fixture_frame(51).to_csv(sep=";", index=False).encode("utf-8")
    monkeypatch.setenv(mod.RECOVERY_HISTORY_ENV, base64.b64encode(raw).decode("ascii"))
    monkeypatch.setattr(mod, "RECOVERY_HISTORY_SHA256", "0" * 64)

    with pytest.raises(RuntimeError, match="SHA-256"):
        mod.load_initial_history("must-not-be-read.csv")


def test_private_recovery_history_rejects_less_than_51_and_open_status(monkeypatch):
    import base64
    import hashlib
    short = _recovery_fixture_frame(50)
    raw = short.to_csv(sep=";", index=False).encode("utf-8")
    monkeypatch.setattr(mod, "RECOVERY_HISTORY_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setenv(mod.RECOVERY_HISTORY_ENV, base64.b64encode(raw).decode("ascii"))
    with pytest.raises(RuntimeError, match="weniger als"):
        mod.load_initial_history("must-not-be-read.csv")

    open_rows = _recovery_fixture_frame(51)
    open_rows.loc[0, "Status"] = "Offen"
    raw_open = open_rows.to_csv(sep=";", index=False).encode("utf-8")
    monkeypatch.setattr(mod, "RECOVERY_HISTORY_SHA256", hashlib.sha256(raw_open).hexdigest())
    monkeypatch.setenv(mod.RECOVERY_HISTORY_ENV, base64.b64encode(raw_open).decode("ascii"))
    with pytest.raises(RuntimeError, match="weniger als"):
        mod.load_initial_history("must-not-be-read.csv")


def test_swap_returns_the_exact_backup_title_used_in_batch_update(monkeypatch):
    class Request:
        def execute(self):
            return {"sheets": [
                {"properties": {"sheetId": 10, "title": "Offene Positionen+Check"}},
                {"properties": {"sheetId": 20, "title": "TMP_OFFEN"}},
            ]}
    class Spreadsheets:
        def get(self, **kwargs):
            return Request()
    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    captured = {}
    def fake_batch_update(sheets, spreadsheet_id, body, context):
        captured["body"] = body
    monkeypatch.setattr(mod, "_google_batch_update_with_retry", fake_batch_update)

    backup_titles = mod._swap_temp_tabs(
        Sheets(), "spreadsheet-id", {"TMP_OFFEN": "Offene Positionen+Check"}
    )

    actual_backup_title = next(
        item["updateSheetProperties"]["properties"]["title"]
        for item in captured["body"]["requests"]
        if item.get("updateSheetProperties", {}).get("properties", {}).get("sheetId") == 10
    )
    assert backup_titles == {"Offene Positionen+Check": actual_backup_title}
    assert actual_backup_title.startswith("_BACKUP_Offene Positionen+Check_")
    assert len(actual_backup_title.rsplit("_", 1)[-1]) == 20  # microsecond timestamp


def test_upsert_uses_backup_title_returned_by_swap_not_a_second_timestamp():
    source = (ROOT / "offene_positionen_check.py").read_text(encoding="utf-8-sig")
    active = source[source.index("def upsert_google_sheet"):source.index("def _position_key")]
    assert "backup_titles = _swap_temp_tabs(" in active
    assert 'backup_tab_title = backup_titles.get("Offene Positionen+Check")' in active
    assert "stamp = dt.datetime.now().strftime('%Y%m%d%H%M%S')" not in active


def test_master_selection_ignores_same_named_non_spreadsheet_files():
    drive = _FakeDrive([
        {"id": "word-id", "name": mod.DRIVE_NAME, "mimeType": "application/vnd.google-apps.document"},
        {"id": "master-id", "name": mod.DRIVE_NAME, "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
    ])

    spreadsheet_id, is_new, init_name = mod._find_or_initialize_master(
        drive, pd.DataFrame()
    )

    assert spreadsheet_id == "master-id"
    assert is_new is False
    assert init_name is None


def test_same_named_non_spreadsheet_does_not_count_as_productive_master():
    drive = _FakeDrive([
        {"id": "word-id", "name": mod.DRIVE_NAME, "mimeType": "application/vnd.google-apps.document"},
    ])

    with pytest.raises(RuntimeError, match="51"):
        mod._find_or_initialize_master(drive, pd.DataFrame())

    assert drive._files.updates == []


class _FakeDriveRequest:
    def __init__(self, value=None):
        self.value = value or {}
    def execute(self):
        return self.value


class _FakeDriveFiles:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.updates = []
    def list(self, **kwargs):
        q = kwargs.get("q", "")
        if "name='Offene Positionen+Check'" in q:
            return _FakeDriveRequest({"files": [x for x in self.items if x["name"] == mod.DRIVE_NAME]})
        if "name contains" in q:
            return _FakeDriveRequest({"files": [x for x in self.items if ".__INIT_" in x["name"]]})
        return _FakeDriveRequest({"files": self.items})
    def update(self, fileId, body, fields):
        self.updates.append((fileId, body["name"]))
        for item in self.items:
            if item["id"] == fileId:
                item["name"] = body["name"]
                break
        return _FakeDriveRequest({"id": fileId, "name": body["name"]})


class _FakeDrive:
    def __init__(self, items=None):
        self._files = _FakeDriveFiles(items)
    def files(self):
        return self._files


def test_new_master_is_published_only_after_init_and_verified_by_id():
    drive = _FakeDrive([{"id": "new-id", "name": "Offene Positionen+Check.__INIT_123", "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE}])

    mod._finalize_new_master(drive, "new-id", "Offene Positionen+Check.__INIT_123")

    assert drive._files.items[0]["name"] == mod.DRIVE_NAME
    assert drive._files.updates == [("new-id", mod.DRIVE_NAME)]


def test_new_master_finalize_rejects_competing_master_without_renaming_init():
    drive = _FakeDrive([
        {"id": "new-id", "name": "Offene Positionen+Check.__INIT_123", "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
        {"id": "other-id", "name": mod.DRIVE_NAME, "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
    ])

    with pytest.raises(RuntimeError, match="anderer gleichnamiger Master"):
        mod._finalize_new_master(drive, "new-id", "Offene Positionen+Check.__INIT_123")

    assert drive._files.items[0]["name"].startswith(mod.MASTER_INIT_PREFIX)
    assert drive._files.updates == []


def test_incomplete_master_initialization_blocks_another_master_creation():
    drive = _FakeDrive([{"id": "orphan-id", "name": "Offene Positionen+Check.__INIT_123", "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE}])

    with pytest.raises(RuntimeError, match="unvollständige Master-Erstinitialisierung"):
        mod._assert_no_incomplete_master_initializations(drive)


def test_existing_master_is_selected_even_if_orphan_init_exists():
    drive = _FakeDrive([
        {"id": "master-id", "name": mod.DRIVE_NAME, "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
        {"id": "orphan-id", "name": "Offene Positionen+Check.__INIT_123", "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
    ])

    spreadsheet_id, is_new, init_name = mod._find_or_initialize_master(
        drive, pd.DataFrame()
    )

    assert spreadsheet_id == "master-id"
    assert is_new is False
    assert init_name is None


def test_new_master_creation_uses_init_name_after_history_validation():
    class CreatingFiles(_FakeDriveFiles):
        def __init__(self):
            super().__init__([])
            self.created = []
        def create(self, body, fields):
            self.created.append(body)
            item = {"id": "created-id", "name": body["name"]}
            self.items.append(item)
            return _FakeDriveRequest({"id": item["id"], "name": item["name"]})

    class CreatingDrive:
        def __init__(self):
            self._files = CreatingFiles()
        def files(self):
            return self._files

    drive = CreatingDrive()
    spreadsheet_id, is_new, init_name = mod._find_or_initialize_master(
        drive, _recovery_fixture_frame(51)
    )

    assert spreadsheet_id == "created-id"
    assert is_new is True
    assert init_name.startswith(mod.MASTER_INIT_PREFIX)
    assert drive._files.created[0]["name"] == init_name


def test_orphan_init_blocks_only_new_master_creation():
    drive = _FakeDrive([
        {"id": "orphan-id", "name": "Offene Positionen+Check.__INIT_123", "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
    ])

    with pytest.raises(RuntimeError, match="unvollständige Master-Erstinitialisierung"):
        mod._find_or_initialize_master(drive, _recovery_fixture_frame(51))

    assert drive._files.updates == []


def test_new_master_finalize_ignores_same_named_non_spreadsheet_conflict():
    drive = _FakeDrive([
        {"id": "new-id", "name": "Offene Positionen+Check.__INIT_123", "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE},
        {"id": "word-id", "name": mod.DRIVE_NAME, "mimeType": "application/vnd.google-apps.document"},
    ])

    mod._finalize_new_master(drive, "new-id", "Offene Positionen+Check.__INIT_123")

    assert drive._files.items[0]["name"] == mod.DRIVE_NAME
    assert drive._files.items[1]["name"] == mod.DRIVE_NAME
    assert drive._files.updates == [("new-id", mod.DRIVE_NAME)]


def test_new_master_is_quarantined_if_final_drive_verification_fails():
    class FailingVerifyFiles(_FakeDriveFiles):
        def __init__(self):
            super().__init__([{"id": "new-id", "name": "Offene Positionen+Check.__INIT_123", "mimeType": mod.GOOGLE_SHEETS_MIME_TYPE}])
            self.list_calls = 0
        def list(self, **kwargs):
            q = kwargs.get("q", "")
            if "name='Offene Positionen+Check'" in q:
                self.list_calls += 1
                if self.list_calls == 1:
                    return _FakeDriveRequest({"files": []})
                raise OSError("simulierter Drive-Lesefehler nach Umbenennung")
            return super().list(**kwargs)
    class FailingVerifyDrive:
        def __init__(self):
            self._files = FailingVerifyFiles()
        def files(self):
            return self._files

    drive = FailingVerifyDrive()
    with pytest.raises(RuntimeError, match="Datei bleibt als"):
        mod._finalize_new_master(drive, "new-id", "Offene Positionen+Check.__INIT_123")

    assert drive._files.items[0]["name"].startswith(mod.MASTER_INIT_PREFIX + "FAILED_")
    assert len(drive._files.updates) == 2


def test_upsert_new_master_with_27_records_aborts_before_drive_create(monkeypatch):
    class Request:
        def execute(self):
            return {"files": []}
    class Files:
        def list(self, **kwargs):
            return Request()
        def create(self, **kwargs):
            raise AssertionError("Drive-Datei darf bei nur 27 Datensätzen nicht erstellt werden.")
    class Drive:
        def files(self):
            return Files()
    def fake_build(service, version, credentials=None):
        return Drive() if service == "drive" else object()
    monkeypatch.setattr(mod, "build", fake_build, raising=False)

    open_df = pd.DataFrame([{"Ticker": "AAA", "Status": "Offen"}])
    short_history = _recovery_fixture_frame(27)
    with pytest.raises(RuntimeError, match="27 < 51"):
        mod.upsert_google_sheet(open_df, short_history, creds=object())
