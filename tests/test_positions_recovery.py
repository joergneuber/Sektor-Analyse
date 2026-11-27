from __future__ import annotations

import sys
import types

# The recovery planner is pure; the live Yahoo client is not needed by these tests.
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

import pandas as pd
import pytest

import scripts.restore_positions as recovery
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
            "Einstiegsdatum": (pd.Timestamp("2026-01-01") + pd.Timedelta(days=index)).strftime("%d.%m.%Y"),
            "Einstieg": f"{index + 1},00",
            "Aktueller_Kurs": "",
            "Stop": "",
            "TP1": "",
            "TP2": "",
            "Status": "Gestoppt",
            "Ausstiegsdatum": "02.02.2026",
            "Ausstiegskurs": f"{(index + 1) * 1.1:.2f}",
            "Performance_Seit_Einstieg%": "0,10",
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
    """Erzeugt Google-Sheets-Zeilen ohne typisierte Pandas-Zellmutationen.

    Google Sheets liefert formatierte Zellwerte als Strings. Die Berechnung
    erfolgt deshalb auf Python-Dictionaries und wird anschließend explizit
    als String serialisiert. Das ist auch mit Pandas 3.x StringDtype kompatibel.
    """
    live_rows = frame.iloc[:count].to_dict(orient="records")
    for row in live_rows:
        entry = opc.parse_number(row.get("Einstieg"))
        close = opc.parse_number(row.get("Ausstiegskurs"))
        direction = str(row.get("Richtung", "Long")).strip().lower()
        row["Performance_Seit_Einstieg%"] = str(
            round(opc.performance(entry, close, direction), 2)
        )
    return [
        ["Geschlossene Positionen | Test"],
        opc.HISTORY_HEADERS,
        *[
            [row.get(column, "") for column in opc.HISTORY_HEADERS]
            for row in live_rows
        ],
    ]


def test_sheet_rows_supports_string_dtype_without_mutating_source():
    trusted = _trusted_history().astype("str")
    before = trusted.copy(deep=True)

    rows = _sheet_rows(trusted, 1)

    performance_index = opc.HISTORY_HEADERS.index("Performance_Seit_Einstieg%")
    assert isinstance(rows[2][performance_index], str)
    pd.testing.assert_frame_equal(trusted, before)


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
    outsider["Performance_Seit_Einstieg%"] = "10.0"
    rows.append([outsider.get(column, "") for column in opc.HISTORY_HEADERS])
    with pytest.raises(RuntimeError, match="mögliche Schlüsselbeschädigung"):
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


def test_mixed_incomplete_history_with_later_trade_is_appendable():
    trusted = _trusted_history()
    mixed = trusted.iloc[:26].copy()
    outsider = trusted.iloc[0].to_dict()
    outsider.update({
        "Ticker": "LATER", "Name": "Later additional trade",
        "Ausstiegsdatum": "01.11.2026", "Ausstiegskurs": "150,00",
        "Performance_Seit_Einstieg%": "0,10",
    })
    mixed = pd.concat([mixed, pd.DataFrame([outsider])], ignore_index=True)
    existing_keys, missing = plan_history_recovery(
        _sheet_rows(mixed, len(mixed)), trusted, headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key, minimum_count=len(trusted),
    )
    assert len(existing_keys) == 27
    assert len(missing) == 25


def test_full_baseline_with_additional_closed_trades_is_preserved():
    trusted = _trusted_history()
    extra = trusted.iloc[0].to_dict()
    extra.update({
        "Ticker": "LATER", "Name": "Later additional trade",
        "Ausstiegsdatum": "01.11.2026", "Ausstiegskurs": "150,00",
    })
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
    (stub_dir / "yfinance.py").write_text("# import-only stub for CLI bootstrap test\n", encoding="utf-8")

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



@pytest.mark.parametrize(
    "scopes",
    [
        ["https://www.googleapis.com/auth/drive"],
        [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    ],
    ids=["full-drive-scope-alone", "drive-and-sheets-scopes"],
)
def test_preflight_only_is_read_only_and_checks_manifest_sources(monkeypatch, capsys, scopes):
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

    creds = SimpleNamespace(scopes=scopes)
    monkeypatch.setenv(recovery.opc.RECOVERY_HISTORY_ENV, "test-only-placeholder")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "test-only-placeholder")
    monkeypatch.setattr(recovery, "load_recovery_manifest", lambda: manifest)
    monkeypatch.setattr(recovery, "validate_open_recovery_source", lambda manifest: open_frame)
    monkeypatch.setattr(recovery.opc, "_load_private_recovery_history", lambda *args, **kwargs: trusted)
    monkeypatch.setattr(recovery.opc, "google_credentials", lambda: creds)
    monkeypatch.setattr(recovery.opc, "build", lambda api, version, credentials: _Drive() if api == "drive" else _Sheets(), raising=False)

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
    rows[2][opc.HISTORY_HEADERS.index("Ausstiegskurs")] = "1.10"
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
    monkeypatch.setenv(recovery.opc.RECOVERY_HISTORY_ENV, "placeholder")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "placeholder")
    monkeypatch.setattr(recovery, "load_recovery_manifest", lambda: manifest)
    monkeypatch.setattr(recovery, "validate_open_recovery_source", lambda manifest: open_frame)
    monkeypatch.setattr(recovery.opc, "_load_private_recovery_history", lambda *args, **kwargs: trusted)
    monkeypatch.setattr(
        recovery.opc, "google_credentials",
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


def test_recovery_requires_existing_master_performance_to_be_canonical_points():
    trusted = _trusted_history()
    rows = _sheet_rows(trusted, 51)
    rows[2][opc.HISTORY_HEADERS.index("Performance_Seit_Einstieg%")] = "0,10"
    with pytest.raises(RuntimeError, match="Sheet=ratio"):
        plan_history_recovery(
            rows, trusted, headers=opc.HISTORY_HEADERS,
            key_fn=opc._history_key, minimum_count=len(trusted),
        )


def test_recovery_accepts_private_ratio_source_after_live_sheet_is_normalized():
    trusted = _trusted_history()
    existing_keys, missing = plan_history_recovery(
        _sheet_rows(trusted, 51), trusted, headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key, minimum_count=len(trusted),
    )
    assert len(existing_keys) == 51
    assert missing == []


def test_partial_baseline_with_real_later_trade_is_appendable_and_preserved():
    trusted = _trusted_history()
    partial = trusted.iloc[:26].copy()
    later = trusted.iloc[0].to_dict()
    later.update({
        "Ticker": "LATER", "Name": "Later additional trade",
        "Einstiegsdatum": "01.11.2026", "Einstieg": "150,00",
        "Ausstiegskurs": "165,00", "Ausstiegsdatum": "10.11.2026",
        "Performance_Seit_Einstieg%": "0,10",
    })
    existing = pd.concat([partial, pd.DataFrame([later])], ignore_index=True)
    existing_keys, missing = plan_history_recovery(
        _sheet_rows(existing, len(existing)), trusted,
        headers=opc.HISTORY_HEADERS, key_fn=opc._history_key,
        minimum_count=len(trusted),
    )
    assert len(existing_keys) == 27
    assert len(missing) == 25


def test_recovery_history_date_normalization_rejects_invalid_and_timezone_values():
    import pytest
    from scripts.restore_positions import _normalize_history_value

    for value in ("2026-31-01", "31.02.2026", "2026-08-01T00:00:00Z",
                  "2026-08-01T00:00:00+02:00"):
        with pytest.raises(ValueError):
            _normalize_history_value("Einstiegsdatum", value)


def test_recovery_history_date_normalization_matches_serial_and_iso():
    from scripts.restore_positions import _normalize_history_value

    expected = ("date", "2026-08-01")
    assert _normalize_history_value("Einstiegsdatum", "46235") == expected
    assert _normalize_history_value("Einstiegsdatum", "01.08.2026") == expected
    assert _normalize_history_value("Einstiegsdatum", "2026-08-01") == expected


def test_open_positions_reader_preserves_validated_stop_in_output_schema(tmp_path):
    import offene_positionen_check as checker

    source = tmp_path / "open_with_stop.csv"
    source.write_text(
        "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
        "LSUG.MI;Sugar 2x;Offen;18.03.2026;2,06;1,90\n",
        encoding="utf-8-sig",
    )
    result = checker.read_positions(str(source))
    assert result.loc[0, "Stop"] == "1,90"
    assert "Stop" in checker.HEADERS
    assert "Stop" in checker.NUMERIC_COLUMNS


def test_open_positions_reader_rejects_excel_date_serial_in_price_field(tmp_path):
    import offene_positionen_check as checker

    source = tmp_path / "Offene_Positionen.csv"
    source.write_text(
        "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
        "LSUG.MI;Sugar 2x;Offen;18.03.2026;46175;2,06\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError, match="Excel-Datumsseriennummer"):
        checker.read_positions(str(source))


def test_position_tracker_rejects_date_serials_at_open_position_source_boundary(monkeypatch):
    import importlib.util
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    names = [
        "yfinance", "google", "google.oauth2", "google.oauth2.credentials",
        "google.auth", "google.auth.transport", "google.auth.transport.requests",
        "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
        "alpaca", "alpaca.data", "alpaca.data.historical", "alpaca.data.enums",
        "alpaca.data.requests", "alpaca.data.timeframe", "os_kurse",
    ]
    previous = {name: sys.modules.get(name) for name in names}
    modules = {name: types.ModuleType(name) for name in names}
    modules["yfinance"].Ticker = object
    modules["google.oauth2.credentials"].Credentials = object
    modules["google.auth.transport.requests"].Request = object
    modules["googleapiclient.discovery"].build = lambda *a, **k: None
    modules["googleapiclient.http"].MediaIoBaseDownload = object
    modules["googleapiclient.http"].MediaIoBaseUpload = object
    modules["alpaca.data.historical"].StockHistoricalDataClient = object
    modules["alpaca.data.historical"].CryptoHistoricalDataClient = object
    modules["alpaca.data.enums"].Adjustment = object
    modules["alpaca.data.requests"].StockBarsRequest = object
    modules["alpaca.data.requests"].CryptoBarsRequest = object
    modules["alpaca.data.timeframe"].TimeFrame = object
    modules["os_kurse"].aktualisiere_optionsscheine = lambda *a, **k: None
    try:
        sys.modules.update(modules)
        spec = importlib.util.spec_from_file_location(
            "positionen_tracker_serial_guard_test", root / "positionen_tracker.py"
        )
        tracker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tracker)
        import pandas as pd

        for entry, stop in ((46175, 2.06), (2.06, 46175.5)):
            invalid = pd.DataFrame({
                "Ticker": ["LSUG.MI"], "Name": ["Sugar 2x"], "Status": ["Offen"],
                "Einstiegsdatum": ["18.03.2026"], "Einstieg": [entry], "Stop": [stop],
            })
            with pytest.raises(ValueError, match="Excel-Datumsseriennummer"):
                tracker.validate_position_source_schema(invalid)

        crypto = pd.DataFrame({
            "Ticker": ["BTC-USD"], "Name": ["Bitcoin"], "Status": ["Offen"],
            "Einstiegsdatum": ["18.03.2026"], "Einstieg": [92000], "Stop": [80000],
        })
        assert len(tracker.validate_position_source_schema(crypto)) == 1
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module



def _load_tracker_for_drive_import_tests(monkeypatch):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    names = [
        "yfinance", "google", "google.oauth2", "google.oauth2.credentials",
        "google.auth", "google.auth.transport", "google.auth.transport.requests",
        "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
        "alpaca", "alpaca.data", "alpaca.data.historical", "alpaca.data.enums",
        "alpaca.data.requests", "alpaca.data.timeframe", "os_kurse",
    ]
    modules = {name: types.ModuleType(name) for name in names}
    modules["yfinance"].Ticker = object
    modules["google.oauth2.credentials"].Credentials = object
    modules["google.auth.transport.requests"].Request = object
    modules["googleapiclient.discovery"].build = lambda *a, **k: None
    modules["googleapiclient.http"].MediaIoBaseUpload = object
    modules["alpaca.data.historical"].StockHistoricalDataClient = object
    modules["alpaca.data.historical"].CryptoHistoricalDataClient = object
    modules["alpaca.data.enums"].Adjustment = object
    modules["alpaca.data.requests"].StockBarsRequest = object
    modules["alpaca.data.requests"].CryptoBarsRequest = object
    modules["alpaca.data.timeframe"].TimeFrame = object
    modules["os_kurse"].aktualisiere_optionsscheine = lambda *a, **k: None

    class FakeDownload:
        def __init__(self, buffer, request):
            self.buffer = buffer
            self.request = request

        def next_chunk(self):
            self.buffer.write(self.request.content)
            return None, True

    modules["googleapiclient.http"].MediaIoBaseDownload = FakeDownload
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "positionen_tracker_drive_schema_test", root / "positionen_tracker.py"
    )
    tracker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tracker)
    return tracker


def test_drive_positions_loader_rejects_truncated_ticker_status_only_csv(monkeypatch):
    from types import SimpleNamespace

    tracker = _load_tracker_for_drive_import_tests(monkeypatch)

    class Files:
        def export(self, **kwargs):
            return SimpleNamespace(content=b"Ticker,Status\\nLSUG.MI,Offen\n")

    class Service:
        def files(self):
            return Files()

    with pytest.raises(RuntimeError, match="nicht sicher gelesen"):
        tracker.lade_positionen_herunter(Service(), "existing-id", tracker.SHEET_MIME)


def test_drive_positions_loader_accepts_legacy_core_schema_and_adds_optional_columns(monkeypatch):
    from types import SimpleNamespace

    tracker = _load_tracker_for_drive_import_tests(monkeypatch)

    class Files:
        def export(self, **kwargs):
            return SimpleNamespace(content=(
                b"Ticker,Name,Status,Einstiegsdatum,Einstieg,Stop\n"
                b"LSUG.MI,Lyxor ETF,Offen,2026-06-02,2.06,2.06\n"
            ))

    class Service:
        def files(self):
            return Files()

    frame = tracker.lade_positionen_herunter(Service(), "existing-id", tracker.SHEET_MIME)
    assert list(frame.columns) == tracker.SPALTEN
    assert frame.loc[0, "Ticker"] == "LSUG.MI"
    assert frame.loc[0, "Einstieg"] == 2.06
    assert frame.loc[0, "Stop"] == 2.06
    assert frame.loc[0, "TP1"] == ""


def test_position_source_schema_allows_closed_legacy_rows_without_entry_date(monkeypatch):
    tracker = _load_tracker_for_drive_import_tests(monkeypatch)
    import pandas as pd

    frame = pd.DataFrame({
        "Ticker": ["LSUG.MI", "OLD.DE"],
        "Name": ["Lyxor ETF", "Legacy"],
        "Status": ["Offen", "Verkauft"],
        "Einstiegsdatum": ["2026-06-02", ""],
        "Einstieg": ["2.06", "10.00"],
        "Stop": ["2.06", "9.00"],
    })
    assert len(tracker.validate_position_source_schema(frame)) == 2


def test_position_source_schema_rejects_open_rows_without_entry_date(monkeypatch):
    tracker = _load_tracker_for_drive_import_tests(monkeypatch)
    import pandas as pd

    frame = pd.DataFrame({
        "Ticker": ["LSUG.MI"], "Name": ["Lyxor ETF"], "Status": ["Offen"],
        "Einstiegsdatum": [""], "Einstieg": ["2.06"], "Stop": ["2.06"],
    })
    with pytest.raises(ValueError, match="Einstiegsdatum"):
        tracker.validate_position_source_schema(frame)


def test_open_positions_reader_rejects_truncated_schema_at_entry(tmp_path):
    import offene_positionen_check as checker

    source = tmp_path / "truncated.csv"
    source.write_text("Ticker;Status\nLSUG.MI;Offen\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="unvollständiges Positionsschema"):
        checker.read_positions(str(source))


def test_open_positions_reader_accepts_supported_closed_history_schema(tmp_path):
    import offene_positionen_check as checker

    source = tmp_path / "legacy_history.csv"
    source.write_text(
        "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
        "OLD.DE;Legacy Position;Gestoppt;;46175;46175,5\n",
        encoding="utf-8-sig",
    )
    result = checker.read_positions(str(source))
    assert len(result) == 1
    assert result.iloc[0]["Status"] == "Gestoppt"
    assert result.iloc[0]["Einstiegsdatum"] == ""


def test_open_positions_reader_requires_entry_and_stop_columns(tmp_path):
    import offene_positionen_check as checker

    for missing in ("Einstieg", "Stop"):
        columns = ["Ticker", "Name", "Status", "Einstiegsdatum", "Einstieg", "Stop"]
        columns.remove(missing)
        values = {"Ticker": "TEST.DE", "Name": "Test AG", "Status": "Offen",
                  "Einstiegsdatum": "2026-01-02", "Einstieg": "10,00", "Stop": "9,00"}
        source = tmp_path / f"missing_{missing}.csv"
        source.write_text(";".join(columns) + "\n" + ";".join(values[c] for c in columns) + "\n", encoding="utf-8-sig")
        with pytest.raises(ValueError, match="Pflichtspalten fehlen"):
            checker.read_positions(str(source))


def test_open_positions_reader_rejects_empty_or_non_numeric_required_prices(tmp_path):
    import offene_positionen_check as checker

    for entry, stop in (("", "9,00"), ("10,00", ""), ("x", "9,00"), ("10,00", "x")):
        source = tmp_path / f"invalid_{len(list(tmp_path.iterdir()))}.csv"
        source.write_text(
            "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
            f"TEST.DE;Test AG;Offen;2026-01-02;{entry};{stop}\n",
            encoding="utf-8-sig",
        )
        with pytest.raises(ValueError):
            checker.read_positions(str(source))


def test_open_positions_reader_rejects_integer_and_fractional_excel_serial_prices(tmp_path):
    import offene_positionen_check as checker

    for value in (46175, "46175", "46175,5"):
        source = tmp_path / f"serial_{str(value).replace(',', '_')}.csv"
        source.write_text(
            "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
            f"TEST.DE;Test AG;Offen;2026-01-02;{value};9,00\n",
            encoding="utf-8-sig",
        )
        with pytest.raises(ValueError, match="Excel-Datumsseriennummer"):
            checker.read_positions(str(source))


def test_open_positions_reader_keeps_instruction_and_closed_legacy_rows_compatible(tmp_path):
    import offene_positionen_check as checker

    source = tmp_path / "legacy_and_instruction.csv"
    source.write_text(
        "Ticker;Name;Status\n"
        "ANLEITUNG;Hinweis;Offen\n"
        "OLD.DE;Geschlossene Altposition;Verkauft\n",
        encoding="utf-8-sig",
    )
    result = checker.read_positions(str(source))
    assert len(result) == 2
    assert checker.select_open_positions(result).empty


def test_open_positions_reader_accepts_complete_valid_open_position(tmp_path):
    import offene_positionen_check as checker

    source = tmp_path / "valid_open.csv"
    source.write_text(
        "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
        "TEST.DE;Test AG;Offen;2026-01-02;10,00;9,00\n",
        encoding="utf-8-sig",
    )
    result = checker.read_positions(str(source))
    assert len(checker.select_open_positions(result)) == 1


def test_open_positions_reader_rejects_placeholder_ticker_or_name(tmp_path):
    import offene_positionen_check as checker

    for ticker, name in (("-", "Test AG"), ("TEST.DE", "NICHT VERFÜGBAR")):
        source = tmp_path / f"placeholder_{len(list(tmp_path.iterdir()))}.csv"
        source.write_text(
            "Ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
            f"{ticker};{name};Offen;2026-01-02;10,00;9,00\n",
            encoding="utf-8-sig",
        )
        with pytest.raises(ValueError, match="Pflichtwert"):
            checker.read_positions(str(source))


def test_open_positions_reader_rejects_duplicate_headers(tmp_path):
    import offene_positionen_check as checker

    source = tmp_path / "duplicate_headers.csv"
    source.write_text(
        "Ticker;ticker;Name;Status;Einstiegsdatum;Einstieg;Stop\n"
        "TEST.DE;TEST.DE;Test AG;Offen;2026-01-02;10,00;9,00\n",
        encoding="utf-8-sig",
    )
    with pytest.raises(ValueError, match="doppelte Spaltennamen"):
        checker.read_positions(str(source))


def test_recovery_local_calculation_uses_temp_file_and_preserves_consumer_csv(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "Offene Positionen+Check.csv"
    original = b"PREVIOUS_VERIFIED_READBACK\n"
    target.write_bytes(original)
    # Patch the exact module object captured by restore_positions. The
    # lifecycle tests load a fresh offene_positionen_check module object under
    # the same sys.modules key, so a separate alias can point at another object.
    monkeypatch.setattr(recovery.opc, "OUTPUT_CSV", str(target))
    observed = {}

    def fake_run_local(input_file, output_csv):
        observed["input_file"] = input_file
        observed["output_csv"] = output_csv
        assert output_csv != str(target)
        with open(output_csv, "wb") as handle:
            handle.write(b"LOCAL_RECOVERY_CALCULATION\n")
        assert target.read_bytes() == original
        return "calculated-frame", "history-frame"

    monkeypatch.setattr(recovery.opc, "run_local", fake_run_local)
    result = recovery._run_local_with_staged_output("Offene_Positionen.csv")

    assert result == ("calculated-frame", "history-frame")
    assert observed["input_file"] == "Offene_Positionen.csv"
    assert observed["output_csv"] != str(target)
    assert target.read_bytes() == original
    assert not __import__("os").path.exists(observed["output_csv"])
    assert not list(tmp_path.glob(".Offene Positionen+Check.csv.recovery-calculation.*.tmp"))


def test_recovery_local_calculation_failure_preserves_consumer_csv_and_cleans_stage(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "Offene Positionen+Check.csv"
    original = b"PREVIOUS_VERIFIED_READBACK\n"
    target.write_bytes(original)
    # Keep the monkeypatch anchored to restore_positions' imported module
    # object, not the separately imported alias replaced by lifecycle tests.
    monkeypatch.setattr(recovery.opc, "OUTPUT_CSV", str(target))
    observed = {}

    def failing_run_local(input_file, output_csv):
        observed["output_csv"] = output_csv
        with open(output_csv, "wb") as handle:
            handle.write(b"PARTIAL_LOCAL_CALCULATION\n")
        raise RuntimeError("simulierter Recovery-Berechnungsfehler")

    monkeypatch.setattr(recovery.opc, "run_local", failing_run_local)
    with pytest.raises(RuntimeError, match="Recovery-Berechnungsfehler"):
        recovery._run_local_with_staged_output("Offene_Positionen.csv")

    assert target.read_bytes() == original
    assert not __import__("os").path.exists(observed["output_csv"])
    assert not list(tmp_path.glob(".Offene Positionen+Check.csv.recovery-calculation.*.tmp"))
