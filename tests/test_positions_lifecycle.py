import pandas as pd
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
    source = (ROOT / "positionen_tracker.py").read_text(encoding="utf-8-sig")
    assert "erscheint sie dauerhaft" in source
    assert "10 Werktage lang" not in source


def test_productive_tab_order_is_explicitly_enforced():
    source = (ROOT / "offene_positionen_check.py").read_text(encoding="utf-8-sig")
    start = source.index("def _enforce_productive_tab_order")
    end = source.index("def _cleanup_backups", start)
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
