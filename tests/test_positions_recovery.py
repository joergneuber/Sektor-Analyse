from __future__ import annotations

import sys
import types

# The recovery planner is pure; the live Yahoo client is not needed by these tests.
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

import pandas as pd
import pytest

from scripts.restore_positions import (
    OPEN_SOURCE_PATH,
    load_recovery_manifest,
    plan_history_recovery,
    validate_open_recovery_source,
)
import offene_positionen_check as opc


def _trusted_history(count: int = 51) -> pd.DataFrame:
    rows = []
    for index in range(count):
        rows.append({
            "Ticker": f"T{index:03d}",
            "Name": f"Company {index}",
            "Sektor": "Test",
            "Markt": "US",
            "Waehrung": "USD",
            "Richtung": "Long",
            "Ideen_Quelle": "Trendfolge",
            "Einstiegsdatum": f"{index + 1:02d}.01.2026",
            "Einstieg": f"{index + 1},00",
            "Aktueller_Kurs": "",
            "Stop": "",
            "TP1": "",
            "TP2": "",
            "Status": "Gestoppt",
            "Ausstiegsdatum": "02.02.2026",
            "Ausstiegskurs": f"{index + 1},00",
            "Performance_Seit_Einstieg%": "",
            "TP_Hinweis": "",
            "Alert_Hinweis": "",
            "Produkt_Typ": "",
            "OS_WKN": "",
            "OS_Einstiegskurs": "",
            "OS_Aktueller_Kurs": "",
            "OS_Performance%": "",
            "OS_Quelle": "",
            "OS_Kurszeit": "",
        })
    return pd.DataFrame(rows, columns=opc.HISTORY_HEADERS)


def _sheet_rows(frame: pd.DataFrame, count: int) -> list[list]:
    return [
        ["Geschlossene Positionen | Test"],
        opc.HISTORY_HEADERS,
        *frame.iloc[:count].values.tolist(),
    ]


def test_verified_recovery_source_matches_manifest():
    manifest = load_recovery_manifest()
    frame = validate_open_recovery_source(OPEN_SOURCE_PATH, manifest=manifest)
    expected_count = manifest["open_positions"]["expected_count"]
    assert expected_count > 0
    assert len(frame) == expected_count
    assert frame["Status"].str.lower().eq("offen").all()


def test_open_recovery_count_is_manifest_driven_not_hardcoded(tmp_path):
    import hashlib
    manifest = load_recovery_manifest()
    original = validate_open_recovery_source(OPEN_SOURCE_PATH, manifest=manifest)
    smaller = original.iloc[:3].copy()
    source = tmp_path / "Offene_Positionen_Quelle_Test.csv"
    smaller.to_csv(source, sep=";", index=False, encoding="utf-8-sig")
    custom_manifest = {
        "open_positions": {
            "source_file": "recovery/Offene_Positionen_Quelle_Test.csv",
            "expected_count": 3,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    }
    validated = validate_open_recovery_source(source, manifest=custom_manifest)
    assert len(validated) == 3


def test_partial_history_appends_only_missing_baseline_rows():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 27)
    existing_keys, missing = plan_history_recovery(
        rows, trusted, headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key, minimum_count=len(trusted),
    )
    assert len(existing_keys) == 27
    assert len(missing) == 24
    assert {opc._history_key(row) for row in missing}.isdisjoint(existing_keys)


def test_complete_history_requires_no_append():
    trusted = _trusted_history()
    existing_keys, missing = plan_history_recovery(
        _sheet_rows(trusted, 51), trusted, headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key, minimum_count=len(trusted),
    )
    assert len(existing_keys) == 51
    assert missing == []



def test_history_count_is_manifest_driven_above_current_51(tmp_path):
    trusted = _trusted_history(53)
    existing_keys, missing = plan_history_recovery(
        _sheet_rows(trusted, 50),
        trusted,
        headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key,
        minimum_count=len(trusted),
    )
    assert len(existing_keys) == 50
    assert len(missing) == 3


def test_private_history_loader_accepts_manifest_verified_count_above_51():
    import base64
    import hashlib

    trusted = _trusted_history(53)
    payload = trusted.to_csv(sep=";", index=False, encoding="utf-8-sig").encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    loaded = opc._load_private_recovery_history(
        encoded,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        minimum_rows=53,
    )
    assert len(loaded) == 53

def test_partial_history_with_unknown_record_fails_closed():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 26)
    outsider = trusted.iloc[0].to_dict()
    outsider.update({"Ticker": "UNKNOWN", "Name": "Not in baseline"})
    rows.append([outsider.get(column, "") for column in opc.HISTORY_HEADERS])
    with pytest.raises(RuntimeError, match="weder eine Teilmenge"):
        plan_history_recovery(
            rows, trusted, headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key, minimum_count=len(trusted),
        )


def test_duplicate_existing_history_fails_closed():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 27)
    rows.append(rows[2])
    with pytest.raises(RuntimeError, match="doppelte Positionsschlüssel"):
        plan_history_recovery(
            rows, trusted, headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key, minimum_count=len(trusted),
        )


def test_existing_header_mismatch_fails_closed():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 10)
    rows[1] = list(rows[1])
    rows[1][0] = "WrongHeader"
    with pytest.raises(RuntimeError, match="Header stimmt nicht exakt"):
        plan_history_recovery(
            rows, trusted, headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key, minimum_count=len(trusted),
        )


def test_history_must_be_exactly_51_rows():
    trusted = _trusted_history(50)
    with pytest.raises(RuntimeError, match="exakt 51"):
        plan_history_recovery(
            _sheet_rows(trusted, 50), trusted, headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key, minimum_count=51,
        )


def test_mixed_incomplete_history_fails_closed():
    trusted = _trusted_history()
    mixed = trusted.iloc[:26].copy()
    outsider = trusted.iloc[0].to_dict()
    outsider.update({"Ticker": "UNKNOWN", "Name": "Not in baseline"})
    mixed = pd.concat([mixed, pd.DataFrame([outsider])], ignore_index=True)
    with pytest.raises(RuntimeError, match="weder eine Teilmenge"):
        plan_history_recovery(
            _sheet_rows(mixed, len(mixed)), trusted, headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key, minimum_count=len(trusted),
        )


def test_full_baseline_with_additional_closed_trades_is_preserved():
    trusted = _trusted_history()
    extra = trusted.iloc[0].to_dict()
    extra.update({"Ticker": "LATER", "Name": "Later additional trade"})
    existing = pd.concat([trusted, pd.DataFrame([extra])], ignore_index=True)
    existing_keys, missing = plan_history_recovery(
        _sheet_rows(existing, 52), trusted, headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key, minimum_count=len(trusted),
    )
    assert len(existing_keys) == 52
    assert missing == []


class _DriveListRequest:
    def __init__(self, files):
        self._files = files

    def execute(self):
        return {"files": self._files}


class _DriveFilesResource:
    def __init__(self, files):
        self._files = files

    def list(self, **kwargs):
        return _DriveListRequest(self._files)


class _DriveClient:
    def __init__(self, files):
        self._files = _DriveFilesResource(files)

    def files(self):
        return self._files


@pytest.mark.parametrize(
    "candidates",
    [
        [
            {"id": "sheet-1", "name": "Offene_Positionen", "mimeType": opc.GOOGLE_SHEETS_MIME_TYPE},
            {"id": "csv-1", "name": "Offene_Positionen.csv", "mimeType": "text/csv"},
        ],
        [
            {"id": "sheet-1", "name": "Offene_Positionen", "mimeType": opc.GOOGLE_SHEETS_MIME_TYPE},
            {"id": "sheet-2", "name": "Offene_Positionen.csv", "mimeType": opc.GOOGLE_SHEETS_MIME_TYPE},
        ],
        [
            {"id": "csv-1", "name": "Offene_Positionen.csv", "mimeType": "text/csv"},
            {"id": "csv-2", "name": "Offene_Positionen", "mimeType": "text/csv"},
        ],
    ],
)
def test_recovery_preflight_rejects_any_multiple_source_candidates(candidates):
    from scripts.restore_positions import _preflight_source_target

    with pytest.raises(RuntimeError, match="mehrere mögliche Offene_Positionen-Quelldateien"):
        _preflight_source_target(_DriveClient(candidates))


def test_recovery_preflight_accepts_exactly_one_source_candidate():
    from scripts.restore_positions import _preflight_source_target

    _preflight_source_target(_DriveClient([
        {"id": "sheet-1", "name": "Offene_Positionen", "mimeType": opc.GOOGLE_SHEETS_MIME_TYPE}
    ]))
    _preflight_source_target(_DriveClient([]))


def test_recovery_module_entrypoint_uses_project_root_without_pythonpath_injection(tmp_path):
    """The workflow's module entrypoint must import from repository root unaided."""
    import os
    import subprocess
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    (stub_dir / "yfinance.py").write_text("# import-only stub for CLI bootstrap test\\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop(opc.RECOVERY_HISTORY_ENV, None)
    # Deliberately do not add project_root to PYTHONPATH. Python -m resolves
    # scripts.* and project-root modules from cwd, as GitHub Actions does.
    env["PYTHONPATH"] = str(stub_dir)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.restore_positions"],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "Repository-Secret CLOSED_POSITIONS_INITIAL_HISTORY_B64 fehlt" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr



def test_preflight_only_is_read_only_and_checks_manifest_sources(monkeypatch, capsys):
    from types import SimpleNamespace
    from scripts import restore_positions as recovery

    manifest = load_recovery_manifest()
    trusted = _trusted_history()
    open_frame = validate_open_recovery_source(OPEN_SOURCE_PATH, manifest=manifest)

    class _Request:
        def execute(self):
            return {"files": []}

    class _Files:
        def list(self, **kwargs):
            return _Request()
        def create(self, **kwargs):
            raise AssertionError("Preflight darf Drive-Dateien nicht anlegen.")

    class _Drive:
        def files(self):
            return _Files()

    class _Sheets:
        pass

    creds = SimpleNamespace(scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    monkeypatch.setenv(opc.RECOVERY_HISTORY_ENV, "test-only-placeholder")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-only-placeholder")
    monkeypatch.setattr(recovery, "load_recovery_manifest", lambda: manifest)
    monkeypatch.setattr(recovery, "validate_open_recovery_source", lambda manifest: open_frame)
    monkeypatch.setattr(opc, "_load_private_recovery_history", lambda *args, **kwargs: trusted)
    monkeypatch.setattr(opc, "google_credentials", lambda: creds)
    monkeypatch.setattr(opc, "build", lambda api, version, credentials: _Drive() if api == "drive" else _Sheets(), raising=False)

    recovery.preflight_only()
    assert "POSITIONS_RECOVERY_PREFLIGHT=SUCCESS" in capsys.readouterr().out


def test_regular_position_check_uses_manifest_history_hash_and_count(tmp_path, monkeypatch):
    import json

    recovery_dir = tmp_path / "recovery"
    recovery_dir.mkdir()
    manifest = {
        "schema_version": 1,
        "closed_positions": {
            "sha256": "a" * 64,
            "expected_count": 53,
        },
        "safety": {
            "minimum_closed_positions": 51,
            "require_unique_position_keys": True,
            "require_exact_source_hashes": True,
            "append_only_history": True,
        },
    }
    (recovery_dir / "recovery_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(opc.RECOVERY_HISTORY_ENV, "manifest-test-secret")
    trusted = _trusted_history(53)
    captured = {}

    def fake_loader(payload, *, expected_sha256=None, minimum_rows=None):
        captured.update({
            "payload": payload,
            "expected_sha256": expected_sha256,
            "minimum_rows": minimum_rows,
        })
        return trusted

    monkeypatch.setattr(opc, "_load_private_recovery_history", fake_loader)
    loaded = opc.load_initial_history()
    assert len(loaded) == 53
    assert captured == {
        "payload": "manifest-test-secret",
        "expected_sha256": "a" * 64,
        "minimum_rows": 53,
    }


def test_preflight_requires_twelvedata_key_when_xauusd_is_in_source(monkeypatch):
    from scripts import restore_positions as recovery

    manifest = load_recovery_manifest()
    open_frame = validate_open_recovery_source(OPEN_SOURCE_PATH, manifest=manifest)
    monkeypatch.setenv(opc.RECOVERY_HISTORY_ENV, "test-only-placeholder")
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    monkeypatch.setattr(recovery, "load_recovery_manifest", lambda: manifest)
    monkeypatch.setattr(recovery, "validate_open_recovery_source", lambda manifest: open_frame)
    with pytest.raises(RuntimeError, match="TWELVEDATA_API_KEY fehlt"):
        recovery.preflight_only()

def test_manifest_rejects_closed_history_below_protected_minimum(tmp_path):
    import json
    manifest = load_recovery_manifest()
    manifest["closed_positions"]["expected_count"] = 50
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    from scripts.restore_positions import load_recovery_manifest as load_manifest
    with pytest.raises(RuntimeError, match="Mindestschutz"):
        load_manifest(path)



@pytest.mark.parametrize(
    "column,bad_value",
    [
        ("Ausstiegskurs", "99999,99"),
        ("Ausstiegsdatum", "31.12.2099"),
        ("Status", "Verkauft"),
        ("Sektor", "Falscher Sektor"),
        ("OS_WKN", "FALSCHE-WKN"),
    ],
)
def test_existing_same_key_but_altered_historical_value_fails_closed(column, bad_value):
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 51)
    index = opc.HISTORY_HEADERS.index(column)
    rows[2][index] = bad_value
    with pytest.raises(RuntimeError, match="weichen trotz identischer Positionsschlüssel"):
        plan_history_recovery(
            rows,
            trusted,
            headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key,
            minimum_count=len(trusted),
        )


def test_existing_history_accepts_equivalent_sheet_number_and_date_rendering():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 51)
    rows[2][opc.HISTORY_HEADERS.index("Einstieg")] = "1.00"
    rows[2][opc.HISTORY_HEADERS.index("Einstiegsdatum")] = "2026-01-01"
    rows[2][opc.HISTORY_HEADERS.index("Ausstiegskurs")] = "1.00"
    rows[2][opc.HISTORY_HEADERS.index("Ausstiegsdatum")] = "2026-02-02"
    existing_keys, missing = plan_history_recovery(
        rows,
        trusted,
        headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key,
        minimum_count=len(trusted),
    )
    assert len(existing_keys) == 51
    assert missing == []


def test_later_additional_closed_trade_is_preserved_without_being_compared_to_baseline():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 51)
    later = trusted.iloc[0].to_dict()
    later.update({
        "Ticker": "LATER",
        "Name": "Later Trade",
        "Einstiegsdatum": "01.03.2026",
        "Einstieg": "123,00",
        "Ausstiegskurs": "130,00",
    })
    rows.append([later.get(column, "") for column in opc.HISTORY_HEADERS])
    existing_keys, missing = plan_history_recovery(
        rows,
        trusted,
        headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key,
        minimum_count=len(trusted),
    )
    assert len(existing_keys) == 52
    assert missing == []


def test_preflight_rejects_drive_file_scope_without_full_drive_scope(monkeypatch):
    from types import SimpleNamespace
    from scripts import restore_positions as recovery

    manifest = load_recovery_manifest()
    open_frame = validate_open_recovery_source(OPEN_SOURCE_PATH, manifest=manifest)
    trusted = _trusted_history()
    monkeypatch.setenv(opc.RECOVERY_HISTORY_ENV, "placeholder")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "placeholder")
    monkeypatch.setattr(recovery, "load_recovery_manifest", lambda: manifest)
    monkeypatch.setattr(recovery, "validate_open_recovery_source", lambda manifest: open_frame)
    monkeypatch.setattr(opc, "_load_private_recovery_history", lambda *args, **kwargs: trusted)
    monkeypatch.setattr(
        opc, "google_credentials",
        lambda: SimpleNamespace(scopes=[
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/spreadsheets",
        ]),
    )
    with pytest.raises(RuntimeError, match="drive.file allein genügt nicht"):
        recovery.preflight_only()



def test_open_tab_live_verification_compares_stable_master_data_fields():
    from types import SimpleNamespace
    from scripts import restore_positions as recovery

    expected = validate_open_recovery_source(OPEN_SOURCE_PATH, manifest=load_recovery_manifest())
    source = expected.iloc[0].to_dict()
    actual = {column: "" for column in opc.HEADERS}
    for column in ("Ticker", "Name", "Markt", "Waehrung", "Einstiegsdatum", "Einstieg",
                   "Status", "OS_WKN", "OS_Einstiegskurs", "OS_Quelle", "OS_Kurszeit"):
        actual[column] = source.get(column, "")
    actual["Einstieg"] = "99999,99"
    actual_rows = []
    for _, source_row in expected.iterrows():
        row = {column: "" for column in opc.HEADERS}
        for column in ("Ticker", "Name", "Sektor", "Markt", "Waehrung", "Einstiegsdatum", "Einstieg",
                       "Status", "OS_WKN", "OS_Einstiegskurs", "OS_Quelle", "OS_Kurszeit"):
            row[column] = source_row.get(column, "")
        ticker = str(source_row.get("Ticker", "")).strip().upper()
        if ticker == "PPFD.SG":
            row["Sektor"] = "Edelmetalle / Silber"
        elif ticker == "GC=F":
            row["Sektor"] = "Edelmetalle / Gold (Future)"
        elif ticker in {"XAU/USD", "XAUUSD"}:
            row["Sektor"] = "Edelmetalle / Gold (Spot)"
        actual_rows.append(row)
    actual_rows[0] = actual

    class Request:
        def __init__(self, payload):
            self.payload = payload
        def execute(self):
            return self.payload

    class Values:
        def get(self, **kwargs):
            return Request({"values": [
                ["Metazeile"], list(opc.HEADERS),
                *[[row.get(column, "") for column in opc.HEADERS] for row in actual_rows],
            ]})

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {"title": "Offene Positionen+Check"}}]})
        def values(self):
            return Values()

    sheets = SimpleNamespace(spreadsheets=lambda: Spreadsheets())
    with pytest.raises(RuntimeError, match="Positionsstammdaten weichen"):
        recovery._verify_master_open_tab(sheets, "spreadsheet-id", expected)


def test_recovery_workflow_requires_prior_preflight_run_id_and_read_only_actions_scope():
    import yaml
    from pathlib import Path
    workflow = yaml.load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/positions-recovery.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "actions" in workflow["permissions"]
    assert workflow["permissions"]["actions"] == "read"
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert "preflight_run_id" in inputs
    source = (Path(__file__).resolve().parents[1] / ".github/workflows/positions-recovery.yml").read_text()
    assert "Der angegebene Run war kein reiner PREFLIGHT_ONLY-Lauf" in source
    assert ".head_sha == $sha" in source



def test_date_format_variants_do_not_hide_duplicate_historical_positions():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 51)
    duplicate = list(rows[2])
    duplicate[opc.HISTORY_HEADERS.index("Einstiegsdatum")] = "2026-01-01"
    rows.append(duplicate)
    with pytest.raises(RuntimeError, match="doppelte Positionsschlüssel nach Normalisierung"):
        plan_history_recovery(
            rows,
            trusted,
            headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key,
            minimum_count=len(trusted),
        )
