"""Regression tests for the live Tab-2 display-header contract in analyse.py."""
import ast
import json
import os
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
ANALYSE_PATH = ROOT / "analyse.py"


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Credentials:
    expired = False
    refresh_token = None
    valid = True

    @staticmethod
    def from_authorized_user_info(_info):
        return _Credentials()


class _DriveFiles:
    def list(self, **_kwargs):
        return _Request({"files": [{"id": "spreadsheet-id"}]})


class _Drive:
    def files(self):
        return _DriveFiles()


class _SheetValues:
    def __init__(self, values):
        self._values = values

    def get(self, **_kwargs):
        return _Request({"values": self._values})


class _Spreadsheet:
    def __init__(self, values):
        self._values = values

    def get(self, **_kwargs):
        return _Request({
            "sheets": [{"properties": {"title": "Geschlossene Positionen"}}]
        })

    def values(self):
        return _SheetValues(self._values)


class _Sheets:
    def __init__(self, values):
        self._values = values

    def spreadsheets(self):
        return _Spreadsheet(self._values)


def _load_production_loader(values):
    """Execute only the production constants/loader, avoiding analyse.py imports."""
    tree = ast.parse(ANALYSE_PATH.read_text(encoding="utf-8"))
    wanted = {"GESCHLOSSENE_SHEET_NAME", "GESCHLOSSENE_SHEET_FOLDER_ID",
              "GESCHLOSSENE_TAB_NAME", "GESCHLOSSENE_HISTORY_HEADERS",
              "_canonicalize_geschlossene_history_header", "lade_geschlossene_historie_google"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted
            for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes.append(node)
    namespace = {
        "pd": pd,
        "os": os,
        "json": json,
        "Credentials": _Credentials,
        "GoogleRequest": object,
        "GOOGLE_HISTORY_AVAILABLE": True,
        "google_build": lambda service, *_args, **_kwargs: (
            _Drive() if service == "drive" else _Sheets(values)
        ),
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(ANALYSE_PATH), "exec"), namespace)
    return namespace


def _values(header):
    return [
        ["Geschlossene Positionen | historische Faktenbasis"],
        list(header),
        ["AAA", "Alpha AG"] + [""] * (len(header) - 2),
    ]


def test_live_tab2_loader_accepts_exact_display_header_with_nr_column(monkeypatch):
    # The producer writes HISTORY_DISPLAY_HEADERS = HISTORY_HEADERS + ["Nr."].
    namespace = _load_production_loader([])
    canonical = namespace["GESCHLOSSENE_HISTORY_HEADERS"]
    display = canonical + ["Nr."]
    values = _values(display)
    values[2][-1] = "1"
    namespace = _load_production_loader(values)
    monkeypatch.setenv("GDRIVE_TOKEN", "{}")

    result = namespace["lade_geschlossene_historie_google"]()

    assert list(result.columns) == canonical
    assert "Nr." not in result.columns
    assert result.iloc[0]["Ticker"] == "AAA"
    assert result.iloc[0]["Name"] == "Alpha AG"


def test_live_tab2_loader_still_accepts_canonical_header(monkeypatch):
    namespace = _load_production_loader([])
    canonical = namespace["GESCHLOSSENE_HISTORY_HEADERS"]
    namespace = _load_production_loader(_values(canonical))
    monkeypatch.setenv("GDRIVE_TOKEN", "{}")

    result = namespace["lade_geschlossene_historie_google"]()

    assert list(result.columns) == canonical
    assert result.iloc[0]["Ticker"] == "AAA"


def test_live_tab2_loader_rejects_unknown_header_and_reports_received_columns(monkeypatch):
    namespace = _load_production_loader([])
    canonical = namespace["GESCHLOSSENE_HISTORY_HEADERS"]
    invalid = list(canonical)
    invalid[0] = "Symbol"
    namespace = _load_production_loader(_values(invalid))
    monkeypatch.setenv("GDRIVE_TOKEN", "{}")

    with pytest.raises(RuntimeError, match="Erhalten=.*Symbol.*erwartet="):
        namespace["lade_geschlossene_historie_google"]()
