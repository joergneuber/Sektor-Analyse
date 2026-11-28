from __future__ import annotations

import sys
import types

sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

import pandas as pd
import pytest

import offene_positionen_check as opc
from scripts import normalize_history_performance as norm


def _baseline(count=51, *, zero_at=None):
    rows = []
    for i in range(count):
        entry = 100.0 + i
        pct = 0.45 if i == 0 else (-2.35 if i == 1 else 1.27 + i / 10)
        if zero_at == i:
            pct = 0.0
        direction = "Short" if i == count - 1 else "Long"
        close = entry * (1 - pct / 100 if direction == "Short" else 1 + pct / 100)
        actual = norm.expected_performance_points({
            "Einstieg": str(entry),
            "Ausstiegskurs": str(close),
            "Richtung": direction,
        })
        rows.append({
            "Ticker": f"T{i:03d}",
            "Name": f"Company {i}",
            "Sektor": "Test",
            "Markt": "US",
            "Waehrung": "USD",
            "Richtung": direction,
            "Ideen_Quelle": "Test",
            "Einstiegsdatum": (pd.Timestamp("2026-01-01") + pd.Timedelta(days=i)).strftime("%d.%m.%Y"),
            "Einstieg": str(entry),
            "Aktueller_Kurs": "",
            "Stop": "",
            "TP1": "",
            "TP2": "",
            "Status": "Verkauft",
            "Ausstiegsdatum": "02.02.2026",
            "Ausstiegskurs": str(close),
            "Performance_Seit_Einstieg%": str(round(actual / 100, 8)),
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


def _sheet_rows(frame, indices=None):
    selected = frame if indices is None else frame.iloc[indices]
    return [
        ["Geschlossene Positionen | Test"],
        list(opc.HISTORY_HEADERS),
        *selected.values.tolist(),
    ]


@pytest.mark.parametrize(
    "stored,expected,classification",
    [
        ("0,0045", 0.45, "ratio"),
        ("0,45", 0.45, "points"),
        ("-0,0235", -2.35, "ratio"),
        ("-2,35", -2.35, "points"),
    ],
)
def test_unit_classification_uses_expected_value_not_absolute_threshold(stored, expected, classification):
    assert norm.classify_performance_value(stored, expected) == classification


@pytest.mark.parametrize("stored,expected", [("0", 0), ("0,10", 1.0), ("7,00", 1.25)])
def test_ambiguous_or_unmatched_values_fail_closed(stored, expected):
    with pytest.raises(RuntimeError, match="Sicherheitsabbruch"):
        norm.classify_performance_value(stored, expected)


def test_independent_performance_calculation_handles_long_short_and_zero():
    assert norm.expected_performance_points({"Einstieg": "100", "Ausstiegskurs": "110", "Richtung": "Long"}) == 10
    assert norm.expected_performance_points({"Einstieg": "100", "Ausstiegskurs": "110", "Richtung": "Short"}) == -10
    assert norm.expected_performance_points({"Einstieg": "100", "Ausstiegskurs": "100", "Richtung": "Long"}) == 0


@pytest.mark.parametrize(
    "row",
    [
        {"Einstieg": "", "Ausstiegskurs": "100", "Richtung": "Long"},
        {"Einstieg": "0", "Ausstiegskurs": "100", "Richtung": "Long"},
        {"Einstieg": "100", "Ausstiegskurs": "", "Richtung": "Long"},
        {"Einstieg": "100", "Ausstiegskurs": "100", "Richtung": "Unknown"},
    ],
)
def test_invalid_trade_inputs_fail_closed(row):
    with pytest.raises(RuntimeError, match="Sicherheitsabbruch"):
        norm.expected_performance_points(row)


def test_complete_51_baseline_classifies_every_ratio_value():
    trusted = _baseline()
    plan = norm.build_normalization_plan(_sheet_rows(trusted), trusted)
    assert len(plan.ratio_keys) == 51
    assert plan.canonical_keys == []
    assert plan.missing_keys == []


def test_partial_baseline_and_later_additional_trades_are_preserved():
    trusted = _baseline()
    partial = trusted.iloc[:30].copy()
    extras = trusted.iloc[:2].copy()
    extras.loc[:, "Ticker"] = ["LATER1", "LATER2"]
    extras.loc[:, "Name"] = ["Later One", "Later Two"]
    extras.loc[:, "Ausstiegsdatum"] = ["01.11.2026", "02.11.2026"]
    extras.loc[:, "Ausstiegskurs"] = ["150.00", "160.00"]
    existing = pd.concat([partial, extras], ignore_index=True)
    plan = norm.build_normalization_plan(_sheet_rows(existing), trusted)
    assert len(plan.ratio_keys) == 30
    assert len(plan.missing_keys) == 21
    assert len(plan.extra_keys) == 2


def test_already_canonical_values_are_idempotently_recognized():
    trusted = _baseline()
    canonical = trusted.copy()
    for idx, row in canonical.iterrows():
        canonical.at[idx, "Performance_Seit_Einstieg%"] = str(
            norm.expected_performance_points(row.to_dict())
        )
    plan = norm.build_normalization_plan(_sheet_rows(canonical), trusted)
    assert plan.ratio_keys == []
    assert len(plan.canonical_keys) == 51
    assert plan.missing_keys == []


def test_non_performance_field_mismatch_fails_closed():
    trusted = _baseline()
    rows = _sheet_rows(trusted)
    rows[2][opc.HISTORY_HEADERS.index("Ausstiegskurs")] = "999"
    with pytest.raises(RuntimeError, match="außerhalb der Performance-Spalte"):
        norm.build_normalization_plan(rows, trusted)


def test_duplicate_baseline_key_fails_closed():
    trusted = _baseline()
    rows = _sheet_rows(trusted)
    rows.append(rows[2])
    with pytest.raises(RuntimeError, match="doppelte Positionsschlüssel"):
        norm.build_normalization_plan(rows, trusted)


def test_zero_performance_is_ambiguous_and_fails_closed():
    trusted = _baseline(zero_at=0)
    with pytest.raises(RuntimeError, match="mehrdeutig"):
        norm.build_normalization_plan(_sheet_rows(trusted), trusted)


def test_post_write_verification_accepts_only_q_changes():
    trusted = _baseline()
    before = _sheet_rows(trusted)
    plan = norm.build_normalization_plan(before, trusted)
    after = [list(row) for row in before]
    for key in plan.ratio_keys:
        row_number = plan.existing_by_key[key][0]
        after[row_number - 1][norm.PERFORMANCE_INDEX] = str(plan.expected_by_key[key])
    norm.verify_post_write(before, after, plan)


def test_post_write_verification_rejects_change_outside_q():
    trusted = _baseline()
    before = _sheet_rows(trusted)
    plan = norm.build_normalization_plan(before, trusted)
    after = [list(row) for row in before]
    after[2][1] = "tampered"
    with pytest.raises(RuntimeError, match="außerhalb Q geändert"):
        norm.verify_post_write(before, after, plan)


def test_backup_must_be_verified_before_returning():
    class Request:
        def __init__(self, payload):
            self.payload = payload
        def execute(self):
            return self.payload

    class Files:
        def copy(self, **kwargs):
            return Request({"id": "backup-1"})
        def get(self, **kwargs):
            return Request({
                "id": "backup-1",
                "name": kwargs["fileId"] and expected_name,
                "mimeType": opc.GOOGLE_SHEETS_MIME_TYPE,
                "trashed": False,
                "parents": [opc.FOLDER_ID],
            })

    class Drive:
        def files(self):
            return Files()

    # Freeze timestamp/random suffix indirectly by capturing the copy body.
    class CapturingFiles(Files):
        def copy(self, **kwargs):
            nonlocal expected_name
            expected_name = kwargs["body"]["name"]
            return Request({"id": "backup-1"})

    expected_name = ""
    backup = norm._backup_and_verify(type("D", (), {"files": lambda self: CapturingFiles()})(), "master-1")
    assert backup["id"] == "backup-1"


def test_backup_verification_failure_raises_before_any_sheet_write():
    class Request:
        def execute(self):
            return {"id": "backup-1"}
    class Files:
        def copy(self, **kwargs):
            return Request()
        def get(self, **kwargs):
            return type("R", (), {"execute": lambda self: {
                "id": "wrong-id", "name": "wrong", "mimeType": "text/plain",
                "trashed": False, "parents": [],
            }})()
    with pytest.raises(RuntimeError, match="Backup konnte nicht vollständig verifiziert"):
        norm._backup_and_verify(type("D", (), {"files": lambda self: Files()})(), "master-1")


def test_normal_archive_helper_canonicalizes_validated_ratio_and_points():
    row = {"Einstieg": "100", "Ausstiegskurs": "110", "Richtung": "Long",
           "Performance_Seit_Einstieg%": "0,10"}
    assert opc.canonical_closed_performance_points(row) == 10.0
    row["Performance_Seit_Einstieg%"] = "10,00"
    assert opc.canonical_closed_performance_points(row) == 10.0
    row["Performance_Seit_Einstieg%"] = "8,00"
    with pytest.raises(RuntimeError, match="weder Verhältniswert noch Prozentpunktwert"):
        opc.canonical_closed_performance_points(row)


def test_normal_append_writes_new_trade_performance_as_points(monkeypatch):
    existing = _baseline(1)
    existing_rows = _sheet_rows(existing)
    new = _baseline(2).iloc[[1]].copy()
    captured = {}

    class Request:
        def __init__(self, payload):
            self.payload = payload
        def execute(self):
            return self.payload

    class Values:
        def append(self, **kwargs):
            captured["append"] = kwargs
            return Request({"updates": {"updatedRows": 1}})

    class Spreadsheets:
        def get(self, **kwargs):
            return Request({"sheets": [{"properties": {
                "title": "Geschlossene Positionen", "sheetId": 7
            }}]})
        def values(self):
            return Values()

    sheets = type("Sheets", (), {"spreadsheets": lambda self: Spreadsheets()})()
    monkeypatch.setattr(opc, "read_existing_history", lambda *a, **k: existing_rows)
    monkeypatch.setattr(opc, "_google_batch_update_with_retry", lambda *a, **k: None)
    appended = opc._append_missing_history_rows(
        sheets, "master", new, allow_initialize=True
    )
    assert appended == 1
    q = opc.HISTORY_HEADERS.index("Performance_Seit_Einstieg%")
    assert captured["append"]["body"]["values"][0][q] == pytest.approx(
        norm.expected_performance_points(new.iloc[0].to_dict())
    )
    assert isinstance(captured["append"]["body"]["values"][0][q], float)


def test_normal_append_rejects_invalid_new_trade_before_sheet_append(monkeypatch):
    existing = _baseline(1)
    existing_rows = _sheet_rows(existing)
    new = _baseline(2).iloc[[1]].copy()
    new.loc[new.index[0], "Performance_Seit_Einstieg%"] = "999"
    appended_calls = []

    class Request:
        def execute(self):
            return {"sheets": [{"properties": {"title": "Geschlossene Positionen", "sheetId": 7}}]}
    class Values:
        def append(self, **kwargs):
            appended_calls.append(kwargs)
            return type("R", (), {"execute": lambda self: {"updates": {"updatedRows": 1}}})()
    class Spreadsheets:
        def get(self, **kwargs):
            return Request()
        def values(self):
            return Values()
    sheets = type("Sheets", (), {"spreadsheets": lambda self: Spreadsheets()})()
    monkeypatch.setattr(opc, "read_existing_history", lambda *a, **k: existing_rows)
    with pytest.raises(RuntimeError, match="weder Verhältniswert noch Prozentpunktwert"):
        opc._append_missing_history_rows(sheets, "master", new, allow_initialize=True)
    assert appended_calls == []


def test_mixed_partial_write_is_safely_reclassified_on_next_preflight():
    trusted = _baseline()
    rows = _sheet_rows(trusted)
    expected = norm.build_normalization_plan(rows, trusted)
    for key in expected.ratio_keys[:12]:
        sheet_row = expected.existing_by_key[key][0]
        rows[sheet_row - 1][norm.PERFORMANCE_INDEX] = str(expected.expected_by_key[key])
    retry = norm.build_normalization_plan(rows, trusted)
    assert len(retry.canonical_keys) == 12
    assert len(retry.ratio_keys) == 39
    assert retry.missing_keys == []


def test_second_preflight_after_normalization_is_noop():
    trusted = _baseline()
    before = _sheet_rows(trusted)
    first = norm.build_normalization_plan(before, trusted)
    after = [list(row) for row in before]
    for key in first.ratio_keys:
        sheet_row = first.existing_by_key[key][0]
        after[sheet_row - 1][norm.PERFORMANCE_INDEX] = str(first.expected_by_key[key])
    second = norm.build_normalization_plan(after, trusted)
    assert second.ratio_keys == []
    assert second.missing_keys == []
    assert len(second.canonical_keys) == 51


@pytest.mark.parametrize(
    "close,expected",
    [("101.234", 1.23), ("101.236", 1.24), ("98.764", -1.24)],
)
def test_rounding_matches_two_decimal_point_convention(close, expected):
    assert norm.expected_performance_points({
        "Einstieg": "100", "Ausstiegskurs": close, "Richtung": "Long"
    }) == expected


def test_run_never_attempts_a_sheet_write_when_backup_verification_fails(monkeypatch):
    trusted = _baseline()
    rows = _sheet_rows(trusted)
    plan = norm.build_normalization_plan(rows, trusted)
    monkeypatch.setattr(norm, "_load_trusted_history", lambda: trusted)
    monkeypatch.setattr(
        opc, "google_credentials",
        lambda: type("Creds", (), {"scopes": ["https://www.googleapis.com/auth/drive"]})(),
    )
    monkeypatch.setattr(opc, "build", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(norm, "_get_master", lambda drive: {"id": "master"})
    monkeypatch.setattr(norm, "_sheet_id", lambda sheets, spreadsheet_id: 7)
    monkeypatch.setattr(norm, "_read_history", lambda sheets, spreadsheet_id: rows)
    monkeypatch.setattr(
        norm, "_backup_and_verify",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backup verify failed")),
    )
    monkeypatch.setattr(
        norm, "_write_performance_cells",
        lambda *a, **k: pytest.fail("Sheet write called before verified backup"),
    )
    monkeypatch.setattr(
        norm, "_append_missing",
        lambda *a, **k: pytest.fail("Append called before verified backup"),
    )
    with pytest.raises(RuntimeError, match="backup verify failed"):
        norm.run(migrate=True)


def test_changed_baseline_identity_cannot_hide_as_an_unrelated_extra_trade():
    trusted = _baseline()
    rows = _sheet_rows(trusted)
    rows[2][opc.HISTORY_HEADERS.index("Ticker")] = "RENAMED"
    with pytest.raises(RuntimeError, match="mögliche Schlüsselbeschädigung"):
        norm.build_normalization_plan(rows, trusted)


def test_dedicated_workflow_is_manual_two_stage_and_read_only_permissions():
    import yaml
    workflow_path = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / ".github/workflows/positions-performance-normalization.yml"
    )
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"].keys()) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["modus"]["default"] == "READ_ONLY_PREFLIGHT"
    assert "NORMALIZE_PERFORMANCE" in inputs["modus"]["options"]
    source = workflow_path.read_text(encoding="utf-8")
    assert ".head_sha == $sha" in source
    assert "Der angegebene Run war kein reiner Read-only-Preflight." in source
    assert "python -m scripts.normalize_history_performance --preflight-only" in source
    assert "python -m scripts.normalize_history_performance --migrate" in source


def test_tracker_and_check_keep_percent_point_formula_for_long_and_short():
    tracker_source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "positionen_tracker.py"
    ).read_text(encoding="utf-8")
    assert "((einstieg_geschlossen - ausstiegskurs) / einstieg_geschlossen) * 100, 2" in tracker_source
    assert "((ausstiegskurs - einstieg_geschlossen) / einstieg_geschlossen) * 100, 2" in tracker_source
    assert opc.performance(100.0, 110.0, "Long") == 10.0
    assert opc.performance(100.0, 110.0, "Short") == -10.0
    assert norm.expected_performance_points({
        "Einstieg": "100", "Ausstiegskurs": "110", "Richtung": "Long"
    }) == 10.0
    assert norm.expected_performance_points({
        "Einstieg": "100", "Ausstiegskurs": "110", "Richtung": "Short"
    }) == -10.0


def test_read_only_run_uses_only_read_paths(monkeypatch, capsys):
    trusted = _baseline()
    rows = _sheet_rows(trusted)
    monkeypatch.setattr(norm, "_load_trusted_history", lambda: trusted)
    monkeypatch.setattr(
        opc, "google_credentials",
        lambda: type("Creds", (), {"scopes": ["https://www.googleapis.com/auth/drive"]})(),
    )
    monkeypatch.setattr(opc, "build", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(norm, "_get_master", lambda drive: {"id": "master"})
    monkeypatch.setattr(norm, "_sheet_id", lambda sheets, spreadsheet_id: 7)
    monkeypatch.setattr(norm, "_read_history", lambda sheets, spreadsheet_id: rows)
    monkeypatch.setattr(norm, "_backup_and_verify", lambda *a, **k: pytest.fail("Preflight must not create backup"))
    monkeypatch.setattr(norm, "_write_performance_cells", lambda *a, **k: pytest.fail("Preflight must not write Q"))
    monkeypatch.setattr(norm, "_append_missing", lambda *a, **k: pytest.fail("Preflight must not append"))
    norm.run(migrate=False)
    assert "PERFORMANCE_NORMALIZATION_PREFLIGHT=SUCCESS" in capsys.readouterr().out


def test_successful_migration_orders_backup_before_q_write_and_post_verifies(monkeypatch, capsys):
    trusted = _baseline()
    before = _sheet_rows(trusted)
    initial_plan = norm.build_normalization_plan(before, trusted)
    after = [list(row) for row in before]
    events = []
    for key in initial_plan.ratio_keys:
        sheet_row = initial_plan.existing_by_key[key][0]
        after[sheet_row - 1][norm.PERFORMANCE_INDEX] = str(initial_plan.expected_by_key[key])

    monkeypatch.setattr(norm, "_load_trusted_history", lambda: trusted)
    monkeypatch.setattr(
        opc, "google_credentials",
        lambda: type("Creds", (), {"scopes": ["https://www.googleapis.com/auth/drive"]})(),
    )
    monkeypatch.setattr(opc, "build", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(norm, "_get_master", lambda drive: {"id": "master"})
    monkeypatch.setattr(norm, "_sheet_id", lambda sheets, spreadsheet_id: 7)
    read_count = {"n": 0}
    def read_rows(sheets, spreadsheet_id):
        read_count["n"] += 1
        return before if read_count["n"] == 1 else after
    monkeypatch.setattr(norm, "_read_history", read_rows)
    monkeypatch.setattr(norm, "_backup_and_verify", lambda *a, **k: events.append("backup") or {"id": "backup"})
    def write_q(*a, **k):
        events.append("Q-write")
        return 51
    monkeypatch.setattr(norm, "_write_performance_cells", write_q)
    monkeypatch.setattr(norm, "_append_missing", lambda *a, **k: events.append("append") or 0)

    norm.run(migrate=True)
    assert events == ["backup", "Q-write", "append"]
    assert "PERFORMANCE_NORMALIZATION=SUCCESS" in capsys.readouterr().out


def test_private_baseline_performance_must_match_independent_recalculation():
    trusted = _baseline()
    trusted.loc[0, "Performance_Seit_Einstieg%"] = "9.99"
    with pytest.raises(RuntimeError, match="Baseline.*weder Verhältniswert noch Prozentpunktwert"):
        norm.build_normalization_plan(_sheet_rows(trusted), trusted)


def test_normalized_output_is_accepted_by_the_existing_recovery_planner():
    from scripts.restore_positions import plan_history_recovery
    trusted = _baseline()
    rows = _sheet_rows(trusted)
    plan = norm.build_normalization_plan(rows, trusted)
    normalized_rows = [list(row) for row in rows]
    for key in plan.ratio_keys:
        sheet_row = plan.existing_by_key[key][0]
        normalized_rows[sheet_row - 1][norm.PERFORMANCE_INDEX] = plan.expected_by_key[key]
    existing_keys, missing = plan_history_recovery(
        normalized_rows,
        trusted,
        headers=opc.HISTORY_HEADERS,
        key_fn=opc._history_key,
        minimum_count=len(trusted),
    )
    assert len(existing_keys) == 51
    assert missing == []


def test_performance_column_contract_is_exactly_google_sheets_q():
    assert len(opc.HISTORY_HEADERS) == 26
    assert opc.HISTORY_HEADERS[norm.PERFORMANCE_INDEX] == "Performance_Seit_Einstieg%"
    assert norm.PERFORMANCE_INDEX == 16


def test_missing_baseline_rows_append_in_points_and_existing_extra_trades_stay_untouched():
    trusted = _baseline()
    existing_baseline = trusted.iloc[:30].copy()
    extras = trusted.iloc[:2].copy()
    extras.loc[:, "Ticker"] = ["LATER1", "LATER2"]
    extras.loc[:, "Name"] = ["Later One", "Later Two"]
    extras.loc[:, "Ausstiegsdatum"] = ["01.11.2026", "02.11.2026"]
    extras.loc[:, "Ausstiegskurs"] = ["150.00", "160.00"]
    before_rows = _sheet_rows(pd.concat([existing_baseline, extras], ignore_index=True))
    plan = norm.build_normalization_plan(before_rows, trusted)
    captured = {}

    class Request:
        def __init__(self, payload):
            self.payload = payload
        def execute(self):
            return self.payload

    class Values:
        def append(self, **kwargs):
            captured["payload"] = kwargs["body"]["values"]
            return Request({"updates": {"updatedRows": len(captured["payload"])}})

    class Spreadsheets:
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    appended = norm._append_missing(Sheets(), "master", plan)
    assert appended == 21
    assert len(captured["payload"]) == 21
    after_rows = [list(row) for row in before_rows]
    for key in plan.ratio_keys:
        sheet_row = plan.existing_by_key[key][0]
        after_rows[sheet_row - 1][norm.PERFORMANCE_INDEX] = plan.expected_by_key[key]
    after_rows.extend(captured["payload"])
    norm.verify_post_write(before_rows, after_rows, plan)
    q_values = [row[norm.PERFORMANCE_INDEX] for row in captured["payload"]]
    assert all(isinstance(value, (int, float)) for value in q_values)


def _six_authorized_mismatch_baseline():
    """Build the exact six preflight identities with independently valid ratios."""
    trusted = _baseline()
    records = [
        (0, "Newmont Corporation", "NEM", "120.0", "12.05.2026", "131.58", "Long", 9.87, 9.65),
        (1, "Linde plc", "LIN", "478.2", "18.08.2026", "480.43", "Short", -0.46, -0.47),
        (2, "Gold Future", "GC=F", "4516.0", "20.08.2026", "4706.0", "Long", 4.47, 4.21),
        (3, "L'Air Liquide S.A.", "AI.PA", "179.0", "03.07.2026", "171.48", "Long", -4.63, -4.20),
        (4, "MTU Aero Engines", "MTX.DE", "361.8", "04.08.2026", "366.7", "Long", -1.11, 1.35),
        (5, "Taiwan Semiconductor Manufacturing Company Limited", "TSM", "414.0", "06.08.2026", "413.57", "Long", -1.54, -0.10),
    ]
    old_values = {}
    for idx, name, ticker, entry, entry_date, close, direction, old, expected in records:
        trusted.at[idx, "Name"] = name
        trusted.at[idx, "Ticker"] = ticker
        trusted.at[idx, "Einstieg"] = entry
        trusted.at[idx, "Einstiegsdatum"] = entry_date
        trusted.at[idx, "Ausstiegskurs"] = close
        trusted.at[idx, "Richtung"] = direction
        trusted.at[idx, "Performance_Seit_Einstieg%"] = str(expected / 100.0)
        old_values[ticker] = old
    rows = _sheet_rows(trusted)
    # Model the live state from the failing preflight: all unaffected baseline
    # rows are already canonical percentage points; only the six listed cells
    # retain their known-wrong values.
    for idx, row in trusted.iterrows():
        if idx >= len(records):
            rows[idx + 2][norm.PERFORMANCE_INDEX] = str(
                norm.expected_performance_points(row.to_dict())
            )
    for idx, *_ in enumerate(records):
        rows[idx + 2][norm.PERFORMANCE_INDEX] = str(records[idx][7])
    return trusted, rows, old_values


def test_only_six_explicitly_authorized_performance_mismatches_are_repairable():
    trusted, rows, old_values = _six_authorized_mismatch_baseline()
    plan = norm.build_normalization_plan(rows, trusted)
    assert len(plan.repair_keys) == 6
    assert plan.ratio_keys == []
    assert plan.missing_keys == []
    expected_by_ticker = {
        "NEM": 9.65,
        "LIN": -0.47,
        "GC=F": 4.21,
        "AI.PA": -4.20,
        "MTX.DE": 1.35,
        "TSM": -0.10,
    }
    assert {key[1]: plan.expected_by_key[key] for key in plan.repair_keys} == expected_by_ticker
    assert old_values == {"NEM": 9.87, "LIN": -0.46, "GC=F": 4.47,
                          "AI.PA": -4.63, "MTX.DE": -1.11, "TSM": -1.54}


def test_unlisted_performance_mismatch_still_fails_closed():
    trusted, rows, _ = _six_authorized_mismatch_baseline()
    rows[8][norm.PERFORMANCE_INDEX] = "99.99"  # unrelated baseline row
    with pytest.raises(RuntimeError, match="Performance-Preflight"):
        norm.build_normalization_plan(rows, trusted)


def test_authorized_identity_with_unexpected_old_value_still_fails_closed():
    trusted, rows, _ = _six_authorized_mismatch_baseline()
    rows[2][norm.PERFORMANCE_INDEX] = "9.88"  # NEM identity, but not the observed value
    with pytest.raises(RuntimeError, match="Performance-Preflight"):
        norm.build_normalization_plan(rows, trusted)


def test_repair_post_write_verification_allows_only_q_for_six_authorized_rows():
    trusted, before, _ = _six_authorized_mismatch_baseline()
    plan = norm.build_normalization_plan(before, trusted)
    after = [list(row) for row in before]
    for key in plan.repair_keys:
        sheet_row = plan.existing_by_key[key][0]
        after[sheet_row - 1][norm.PERFORMANCE_INDEX] = str(plan.expected_by_key[key])
    norm.verify_post_write(before, after, plan)
    tampered = [list(row) for row in after]
    tampered[2][1] = "tampered"
    with pytest.raises(RuntimeError, match="außerhalb Q geändert"):
        norm.verify_post_write(before, tampered, plan)


def test_repair_write_targets_only_q_cells_and_expected_values():
    trusted, rows, _ = _six_authorized_mismatch_baseline()
    plan = norm.build_normalization_plan(rows, trusted)
    captured = {}

    class Request:
        def execute(self):
            return {"totalUpdatedCells": 6}

    class Values:
        def batchUpdate(self, **kwargs):
            captured.update(kwargs)
            return Request()

    class Spreadsheets:
        def values(self):
            return Values()

    sheets = type("Sheets", (), {"spreadsheets": lambda self: Spreadsheets()})()
    assert norm._write_performance_cells(sheets, "master", plan) == 6
    data = captured["body"]["data"]
    assert len(data) == 6
    assert all(item["range"].startswith("'Geschlossene Positionen'!Q") for item in data)
    assert {float(item["values"][0][0]) for item in data} == {
        9.65, -0.47, 4.21, -4.20, 1.35, -0.10
    }


def test_migration_repairs_six_cells_only_after_verified_backup(monkeypatch):
    trusted, before, _ = _six_authorized_mismatch_baseline()
    live_rows = [list(row) for row in before]
    events = []
    monkeypatch.setattr(norm, "_load_trusted_history", lambda: trusted)
    monkeypatch.setattr(
        opc, "google_credentials",
        lambda: type("Creds", (), {"scopes": ["https://www.googleapis.com/auth/drive"]})(),
    )
    monkeypatch.setattr(opc, "build", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(norm, "_get_master", lambda drive: {"id": "master"})
    monkeypatch.setattr(norm, "_sheet_id", lambda sheets, spreadsheet_id: 7)
    monkeypatch.setattr(norm, "_read_history", lambda sheets, spreadsheet_id: live_rows)

    def verified_backup(*args, **kwargs):
        events.append("backup")
        return {"id": "verified-backup"}

    def write_cells(sheets, spreadsheet_id, plan):
        assert events == ["backup"]
        events.append("write")
        for key in plan.repair_keys:
            row_number = plan.existing_by_key[key][0]
            live_rows[row_number - 1][norm.PERFORMANCE_INDEX] = str(
                plan.expected_by_key[key]
            )
        return len(plan.repair_keys)

    def append_missing(*args, **kwargs):
        return 0

    monkeypatch.setattr(norm, "_backup_and_verify", verified_backup)
    monkeypatch.setattr(norm, "_write_performance_cells", write_cells)
    monkeypatch.setattr(norm, "_append_missing", append_missing)
    norm.run(migrate=True)
    assert events == ["backup", "write"]
    final_plan = norm.build_normalization_plan(live_rows, trusted)
    assert final_plan.repair_keys == []
    assert final_plan.ratio_keys == []
    assert len(final_plan.canonical_keys) == 51


def test_closed_trade_without_live_price_or_stored_performance_is_archived_from_exit_facts():
    row = {
        "Ticker": "1ANT.F",
        "Name": "Anthropic PBC",
        "Status": "Verkauft",
        "Richtung": "Long",
        "Einstiegsdatum": "01.10.2026",
        "Einstieg": "75,75",
        "Aktueller_Kurs": "",
        "Ausstiegsdatum": "19.11.2026",
        "Ausstiegskurs": "88,38",
        "Performance_Seit_Einstieg%": "",
    }
    assert opc.canonical_closed_performance_points(row) == 16.67

    closed = pd.DataFrame([row], columns=opc.HISTORY_HEADERS)
    prepared = opc._prepare_closed_history_for_archive(closed)
    assert prepared.iloc[0]["Performance_Seit_Einstieg%"] == 16.67
    assert prepared.iloc[0]["Aktueller_Kurs"] == ""


def test_closed_short_trade_without_live_price_uses_direction_correctly():
    row = {
        "Ticker": "SHORT.TEST",
        "Name": "Short Test",
        "Status": "Verkauft",
        "Richtung": "Short",
        "Einstiegsdatum": "01.10.2026",
        "Einstieg": "100",
        "Aktueller_Kurs": "",
        "Ausstiegsdatum": "19.11.2026",
        "Ausstiegskurs": "90",
        "Performance_Seit_Einstieg%": "",
    }
    assert opc.canonical_closed_performance_points(row) == 10.0


def test_incomplete_exit_date_fails_before_google_services_are_created(monkeypatch):
    row = {
        "Ticker": "1ANT.F",
        "Name": "Anthropic PBC",
        "Status": "Verkauft",
        "Richtung": "Long",
        "Einstiegsdatum": "01.10.2026",
        "Einstieg": "75.75",
        "Aktueller_Kurs": "",
        "Ausstiegsdatum": "",
        "Ausstiegskurs": "88.38",
        "Performance_Seit_Einstieg%": "",
    }
    closed = pd.DataFrame([row], columns=opc.HISTORY_HEADERS)
    opened = pd.DataFrame([{"Ticker": "OPEN.TEST", "Status": "Offen"}])

    def forbidden_build(*args, **kwargs):
        pytest.fail("Drive-/Sheets-Service darf vor lokaler Exit-Validierung nicht erstellt werden.")

    monkeypatch.setattr(opc, "build", forbidden_build, raising=False)
    with pytest.raises(RuntimeError, match=r"1ANT\.F – Ausstiegsdatum fehlt"):
        opc.upsert_google_sheet(opened, closed, creds=object())


def test_invalid_stored_closed_performance_is_still_rejected():
    row = {
        "Ticker": "1ANT.F",
        "Name": "Anthropic PBC",
        "Status": "Verkauft",
        "Richtung": "Long",
        "Einstiegsdatum": "01.10.2026",
        "Einstieg": "75.75",
        "Aktueller_Kurs": "",
        "Ausstiegsdatum": "19.11.2026",
        "Ausstiegskurs": "88.38",
        "Performance_Seit_Einstieg%": "99.00",
    }
    with pytest.raises(RuntimeError, match=r"1ANT\.F"):
        opc._prepare_closed_history_for_archive(
            pd.DataFrame([row], columns=opc.HISTORY_HEADERS)
        )


def test_expected_archive_preflight_runs_before_first_google_sheet_write():
    source = (__import__("pathlib").Path(opc.__file__).read_text(encoding="utf-8"))
    start = source.index("def upsert_google_sheet")
    end = source.index("def _remove_closed_from_source", start)
    active = source[start:end]
    assert active.index("_expected_history_records(") < active.index(".batchUpdate(")
    assert active.index("_validate_output_before_upload(") < active.index('build("drive"')


def test_zero_percent_closed_trade_accepts_stored_zero():
    row = {
        "Einstieg": "100",
        "Ausstiegskurs": "100",
        "Richtung": "Long",
        "Performance_Seit_Einstieg%": "0",
    }
    assert opc.canonical_closed_performance_points(row) == 0.0


def test_non_finite_positive_price_string_is_rejected():
    row = {
        "Einstieg": "inf",
        "Ausstiegskurs": "100",
        "Richtung": "Long",
        "Performance_Seit_Einstieg%": "",
    }
    with pytest.raises(RuntimeError, match="gültige endliche positive"):
        opc.canonical_closed_performance_points(row)


def test_closed_history_rejects_exit_before_entry():
    row = {
        "Ticker": "DATE.TEST",
        "Name": "Date Test",
        "Status": "Verkauft",
        "Richtung": "Long",
        "Einstiegsdatum": "20.11.2026",
        "Einstieg": "100",
        "Aktueller_Kurs": "",
        "Ausstiegsdatum": "19.11.2026",
        "Ausstiegskurs": "110",
        "Performance_Seit_Einstieg%": "",
    }
    closed = pd.DataFrame([row], columns=opc.HISTORY_HEADERS)
    with pytest.raises(RuntimeError, match="Ausstiegsdatum liegt vor dem Einstiegsdatum"):
        opc._prepare_closed_history_for_archive(closed)
