import hashlib
import json

import pytest

from positions_drive_content import google_sheet_content_sha256


class Request:
    def __init__(self, value): self.value = value
    def execute(self, **kwargs): return self.value


class FakeSheets:
    def __init__(self, tabs): self.tabs = tabs
    def spreadsheets(self):
        outer = self
        class Spreadsheet:
            def get(self, **kwargs):
                return Request({"sheets": [{"properties": {"title": title, "index": i}} for i, (title, _) in enumerate(outer.tabs)]})
            class Values:
                def batchGet(self, **kwargs):
                    return Request({"valueRanges": [{"values": values} for _, values in outer.tabs]})
            def values(self): return self.Values()
        return Spreadsheet()


def test_fingerprint_is_stable_and_covers_all_tabs():
    one = FakeSheets([("Offene_Positionen", [["Ticker", "Status"], ["ABC", "Offen"]]), ("Notizen", [["ok"]])])
    two = FakeSheets([("Offene_Positionen", [["Ticker", "Status"], ["ABC", "Offen"]]), ("Notizen", [["ok"]])])
    assert google_sheet_content_sha256(one, "sheet-id") == google_sheet_content_sha256(two, "sheet-id")


def test_fingerprint_changes_when_any_cell_value_changes():
    before = FakeSheets([("Offene_Positionen", [["Ticker", "Status"], ["ABC", "Offen"]])])
    after = FakeSheets([("Offene_Positionen", [["Ticker", "Status"], ["ABC", "Gestoppt"]])])
    assert google_sheet_content_sha256(before, "sheet-id") != google_sheet_content_sha256(after, "sheet-id")


def test_fingerprint_changes_when_a_tab_is_added_or_renamed():
    before = FakeSheets([("Offene_Positionen", [["ABC"]])])
    after = FakeSheets([("Offene_Positionen", [["ABC"]]), ("Extra", [])])
    assert google_sheet_content_sha256(before, "sheet-id") != google_sheet_content_sha256(after, "sheet-id")


def test_fingerprint_fails_closed_when_sheet_has_no_tabs():
    with pytest.raises(RuntimeError, match="kein verifizierbares Tabellenblatt"):
        google_sheet_content_sha256(FakeSheets([]), "sheet-id")
