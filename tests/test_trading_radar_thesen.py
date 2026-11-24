import json
from pathlib import Path

import pytest

from trading_radar_thesen import extract_thesis_payload, update_register, synchronize_report_versions


def payload(status="BESTÄTIGT"):
    return {"data_cutoff": "2026-10-21T18:00:00+02:00", "theses": [{
        "thesis_id": "macro-usd-001", "thesis": "Dollarthese", "horizon": "2-4 Wochen",
        "alternative": "DXY stabilisiert sich trotz Renditedifferenz.",
        "evidence": ["Renditedifferenz"], "counter_evidence": ["Risiko"],
        "confidence": "mittel", "confirm_triggers": ["DXY bestaetigt"],
        "invalidate_triggers": ["DXY bricht"], "event_dependencies": [],
        "asset_classes": ["Gold"], "candidates": [], "status": status, "replaced_by": None,
    }]}


def report(data):
    return "Auswertung\n```TRADING_RADAR_THESEN_JSON\n" + json.dumps(data) + "\n```"


def test_no_block_does_not_create_register(tmp_path):
    path = tmp_path / "register.json"
    result = update_register("normaler Bericht", str(path))
    assert result["updated"] is False
    assert not path.exists()


def test_register_is_written_and_versions_stable_ids(tmp_path):
    path = tmp_path / "register.json"
    first = update_register(report(payload("NEU")), str(path))
    assert first["updated"] is True and first["count"] == 1
    first_doc = json.loads(path.read_text())
    assert first_doc["theses"][0]["version"] == 1
    created = first_doc["theses"][0]["created_at"]
    second = update_register(report(payload("BESTAETIGT")), str(path))
    second_doc = json.loads(path.read_text())
    assert second["updated"] is True
    assert second_doc["theses"][0]["version"] == 2
    assert second_doc["theses"][0]["created_at"] == created
    assert second_doc["theses"][0]["status"] == "BESTÄTIGT"


def test_invalid_payload_does_not_overwrite_existing_register(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload()), str(path))
    before = path.read_bytes()
    invalid = payload()
    invalid["theses"][0].pop("invalidate_triggers")
    with pytest.raises(ValueError):
        update_register(report(invalid), str(path))
    assert path.read_bytes() == before


def test_duplicate_ids_are_rejected():
    data = payload()
    data["theses"].append(dict(data["theses"][0]))
    with pytest.raises(ValueError, match="Doppelte"):
        update_register(report(data), "/tmp/should-not-be-written.json")


def test_missing_data_cutoff_does_not_write_or_use_processing_time(tmp_path):
    path = tmp_path / "register.json"
    result = update_register(report({"theses": payload()["theses"]}), str(path))
    assert result == {"updated": False, "reason": "kein Daten-Cutoff aus den verwendeten Daten"}
    assert not path.exists()


def test_missing_cutoff_leaves_existing_register_unchanged(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload()), str(path))
    before = path.read_bytes()
    result = update_register(report({"theses": payload()["theses"]}), str(path))
    assert result["updated"] is False
    assert path.read_bytes() == before


def test_naive_cutoff_is_rejected(tmp_path):
    data = payload()
    data["data_cutoff"] = "2026-10-21T18:00:00"
    with pytest.raises(ValueError, match="Zeitzone"):
        update_register(report(data), str(tmp_path / "register.json"))


def test_invalid_cutoff_is_rejected(tmp_path):
    data = payload()
    data["data_cutoff"] = "gestern"
    with pytest.raises(ValueError, match="ISO-8601"):
        update_register(report(data), str(tmp_path / "register.json"))


def test_identical_thesis_does_not_increment_version(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload()), str(path))
    first_doc = json.loads(path.read_text())
    first_updated = first_doc["theses"][0]["updated_at"]
    update_register(report(payload()), str(path))
    second_doc = json.loads(path.read_text())
    assert second_doc["theses"][0]["version"] == 1
    assert second_doc["theses"][0]["updated_at"] == first_updated
    assert second_doc["theses"][0]["last_seen_at"] >= first_updated


def test_alternative_change_increments_thesis_version(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload()), str(path))
    changed = payload()
    changed["theses"][0]["alternative"] = "Die defensive Rotation wird vollständig zurückgenommen."
    result = update_register(report(changed), str(path))
    saved = json.loads(path.read_text())
    assert result["updated_theses"] == ["macro-usd-001"]
    assert saved["theses"][0]["version"] == 2
    assert saved["theses"][0]["alternative"] == changed["theses"][0]["alternative"]


def test_missing_alternative_is_rejected_without_register_overwrite(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload()), str(path))
    before = path.read_bytes()
    invalid = payload()
    invalid["theses"][0].pop("alternative")
    with pytest.raises(ValueError, match="alternative"):
        update_register(report(invalid), str(path))
    assert path.read_bytes() == before



def test_ascii_status_alias_is_canonicalized_before_persistence(tmp_path):
    path = tmp_path / "register.json"
    result = update_register(report(payload("BESTAETIGT")), str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result["updated"] is True
    assert saved["theses"][0]["status"] == "BESTÄTIGT"


def test_report_versions_are_synchronized_to_persisted_register_versions():
    report_text = """2. Thesen-Update
- Thesen-ID: T-1
  - Version: 12
- Thesen-ID: T-2
  - Version: 2

```TRADING_RADAR_THESEN_JSON
{"data_cutoff":"2026-10-21T18:00:00+02:00","theses":[
{"thesis_id":"T-1","version":12,"status":"BESTÄTIGT"},
{"thesis_id":"T-2","version":2,"status":"NEU"}
]}
```

3. Ereignisradar
"""
    result = synchronize_report_versions(report_text, {
        "thesis_versions": [
            {"thesis_id": "T-1", "version": 1, "active": True},
            {"thesis_id": "T-2", "version": 10, "active": True},
        ]
    })
    assert "- Version: 1" in result
    assert "- Version: 10" in result
    payload = extract_thesis_payload(result)
    assert [item["version"] for item in payload["theses"]] == [1, 10]


def test_register_comparison_generates_real_before_after_audit(tmp_path):
    import hashlib
    import json

    path = tmp_path / "register.json"
    first = update_register(report(payload()), str(path))
    assert first["comparison_status"] == "BASELINE_MISSING"
    assert first["previous_sha256"] is None
    assert first["current_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first["comparison_sha256"] is None

    before_bytes = path.read_bytes()
    before_sha = hashlib.sha256(before_bytes).hexdigest()
    second = update_register(report(payload()), str(path))
    after_bytes = path.read_bytes()
    after_sha = hashlib.sha256(after_bytes).hexdigest()

    assert second["comparison_status"] == "PASS"
    assert second["previous_sha256"] == before_sha
    assert second["current_sha256"] == after_sha
    assert second["previous_thesis_versions"] == [
        {"thesis_id": "macro-usd-001", "version": 1, "active": True,
         "status": "BESTÄTIGT", "replaced_by": None}
    ]
    assert second["thesis_versions"] == [
        {"thesis_id": "macro-usd-001", "version": 1, "active": True,
         "status": "BESTÄTIGT", "replaced_by": None}
    ]
    expected_payload = {
        "previous_sha256": before_sha,
        "current_sha256": after_sha,
        "schema": "trading-radar-thesis-register-comparison-v1",
    }
    expected_id = hashlib.sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert second["comparison_sha256"] == expected_id
    assert second["previous_thesis_count"] == 1
    assert second["previous_active_count"] == 1
    assert second["current_thesis_count"] == 1
    assert second["active_count"] == 1


def test_invalid_persisted_baseline_is_rejected_without_overwrite(tmp_path):
    path = tmp_path / "register.json"
    invalid_baseline = {
        "schema_version": 1,
        "theses": [{
            "thesis_id": "macro-usd-001",
            "version": 0,
            "active": True,
        }],
    }
    original = json.dumps(invalid_baseline).encode("utf-8")
    path.write_bytes(original)
    with pytest.raises(ValueError, match="positive Version"):
        update_register(report(payload()), str(path))
    assert path.read_bytes() == original



@pytest.mark.parametrize(
    "payload_value",
    [
        {"schema_version": True, "theses": [{"thesis_id": "T", "version": 1, "active": True}]},
        {"schema_version": 1, "theses": [{"thesis_id": "T", "version": 1, "active": "false"}]},
        {"schema_version": 1, "theses": [{"thesis_id": "T", "version": 1, "active": 0}]},
    ],
)
def test_register_snapshot_rejects_non_strict_schema_and_active_types(payload_value):
    from trading_radar_thesen import _validate_register_snapshot

    with pytest.raises(ValueError):
        _validate_register_snapshot(payload_value, label="Test")


def test_register_change_lists_reflect_new_update_unchanged_and_archive_transitions(tmp_path):
    path = tmp_path / "register.json"
    first_payload = payload()
    first = update_register(report(first_payload), str(path))
    assert first["comparison_status"] == "BASELINE_MISSING"

    replacement = payload()
    replacement["theses"][0]["thesis_id"] = "macro-usd-002"
    replacement["theses"][0]["status"] = "NEU"
    explicit_transition = payload("ERSETZT")
    explicit_transition["theses"][0]["replaced_by"] = "macro-usd-002"
    replacement["theses"].append(explicit_transition["theses"][0])
    second = update_register(report(replacement), str(path))
    assert second["new_theses"] == ["macro-usd-002"]
    assert second["archived_theses"] == ["macro-usd-001"]
    assert second["updated_theses"] == ["macro-usd-001"]
    assert second["unchanged_theses"] == []
    saved = json.loads(path.read_text())
    old = next(item for item in saved["theses"] if item["thesis_id"] == "macro-usd-001")
    assert old["active"] is False and old["status"] == "ERSETZT"
    assert old["replaced_by"] == "macro-usd-002"

    confirmed = json.loads(json.dumps(replacement))
    # Archived IDs stay in the register history but are not reintroduced into
    # the current report payload.
    confirmed["theses"] = [
        item for item in confirmed["theses"] if item["thesis_id"] == "macro-usd-002"
    ]
    confirmed["theses"][0]["status"] = "BESTÄTIGT"
    third = update_register(report(confirmed), str(path))
    assert third["new_theses"] == []
    assert third["archived_theses"] == []
    assert third["updated_theses"] == ["macro-usd-002"]
    assert third["unchanged_theses"] == []  # archived history is retained, not re-emitted as current input


def test_omitted_active_thesis_is_carried_forward_unchanged_without_archiving(tmp_path):
    path = tmp_path / "register.json"
    first = update_register(report(payload()), str(path))
    before = json.loads(path.read_text(encoding="utf-8"))
    old = dict(before["theses"][0])
    replacement = payload("NEU")
    replacement["theses"][0]["thesis_id"] = "new-id"
    second = update_register(report(replacement), str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    carried = next(item for item in saved["theses"] if item["thesis_id"] == "macro-usd-001")

    assert second["updated"] is True
    assert second["new_theses"] == ["new-id"]
    assert second["unchanged_theses"] == ["macro-usd-001"]
    assert second["archived_theses"] == []
    assert carried["active"] is True
    assert carried["status"] == old["status"]
    assert carried["version"] == old["version"]
    assert carried["thesis"] == old["thesis"]
    assert second["carried_forward_theses"][0]["thesis_id"] == "macro-usd-001"



def test_omitted_active_neu_thesis_is_carried_forward_without_reclassifying_it_as_new():
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "register.json"
        first_payload = payload("NEU")
        first_payload["theses"][0]["thesis_id"] = "new-id-1"
        first = update_register(report(first_payload), str(path))
        assert first["comparison_status"] == "BASELINE_MISSING"

        second_payload = payload("NEU")
        second_payload["theses"][0]["thesis_id"] = "new-id-2"
        second = update_register(report(second_payload), str(path))
        saved = json.loads(path.read_text(encoding="utf-8"))
        carried = next(item for item in saved["theses"] if item["thesis_id"] == "new-id-1")

        assert second["comparison_status"] == "PASS"
        assert second["new_theses"] == ["new-id-2"]
        assert second["unchanged_theses"] == ["new-id-1"]
        assert carried["active"] is True
        assert carried["status"] == "NEU"
        assert carried["version"] == 1

def test_synchronize_report_versions_adds_carried_forward_active_thesis_to_json_and_compact_view():
    import re
    import tempfile

    from trading_radar_thesen import synchronize_report_versions

    def report_with_compact_register(data):
        thesis = data["theses"][0]
        compact = (
            "Thesenregister – kompakte Übersicht:\n"
            f"- {thesis['thesis_id']} | Version 1 | Status {thesis['status']} | Konfidenz {thesis['confidence']}\n"
            f"  - Datenstichtag: {data['data_cutoff']}\n"
            f"  - Aktualisiert: {data['data_cutoff']}\n"
            f"  - These: {thesis['thesis']}\n"
            f"  - Horizont: {thesis['horizon']}\n"
            f"  - Alternative: {thesis['alternative']}\n"
            f"  - Belege: {'; '.join(thesis['evidence'])}\n"
            f"  - Gegenbelege: {'; '.join(thesis['counter_evidence'])}\n"
            f"  - Bestätigungstrigger: {'; '.join(thesis['confirm_triggers'])}\n"
            f"  - Invalidierungstrigger: {'; '.join(thesis['invalidate_triggers'])}\n"
            "  - Ereignisabhängigkeiten: keine\n"
            f"  - Assetklassen: {'; '.join(thesis['asset_classes'])}\n"
            f"  - Verknüpfte Kandidaten: {'; '.join(thesis['candidates']) or 'keine'}\n"
        )
        return (
            "1. Trading-Radar\nMarktregime: neutral.\n"
            "2. Thesen-Update\n"
            + report(data) + "\n" + compact
            + "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
        )

    with tempfile.TemporaryDirectory() as directory:
        register_path = Path(directory) / "register.json"
        initial = payload()
        update_register(report(initial), str(register_path))
        replacement = payload("NEU")
        replacement["theses"][0]["thesis_id"] = "new-id"
        result = update_register(report(replacement), str(register_path))
        synchronized = synchronize_report_versions(report_with_compact_register(replacement), result)
        match = re.search(r"```TRADING_RADAR_THESEN_JSON\s*(.*?)\s*```", synchronized, re.S)
        assert match
        current = json.loads(match.group(1))
        ids = {item["thesis_id"] for item in current["theses"]}
        assert ids == {"new-id", "macro-usd-001"}
        compact = synchronized.split("Thesenregister – kompakte Übersicht:", 1)[1].split("3. Ereignisradar", 1)[0]
        assert "- Thesen-ID: new-id | Version 1 | Status NEU" in compact
        assert "- Thesen-ID: macro-usd-001 | Version 1 | Status BESTÄTIGT" in compact
        assert "  - These: Dollarthese" in compact
        from trading_radar_neuauswertung import validate_thesis_ids_match_report
        assert validate_thesis_ids_match_report(synchronized, current) == []


def test_synchronize_report_versions_recognizes_single_character_compact_id_once():
    import re

    carried = {
        "thesis_id": "T",
        "status": "BESTÄTIGT",
        "confidence": "mittel",
        "data_cutoff": "2026-11-20",
        "updated_at": "2026-11-20",
        "thesis": "Einzelzeichen-These",
        "horizon": "2 Wochen",
        "alternative": "Alternative",
        "evidence": ["Beleg"],
        "counter_evidence": ["Gegenbeleg"],
        "confirm_triggers": ["Trigger"],
        "invalidate_triggers": ["Invalidierung"],
        "event_dependencies": [],
        "asset_classes": ["Aktien"],
        "candidates": [],
        "replaced_by": None,
    }
    report_text = (
        "2. Thesen-Update\n"
        "Thesenregister – kompakte Übersicht:\n"
        "- Thesen-ID: T | Version 1 | Status BESTÄTIGT | Konfidenz mittel\n"
        "3. Ereignisradar\n"
    )
    register_result = {
        "thesis_versions": [{"thesis_id": "T", "version": 2}],
        "carried_forward_theses": [carried],
    }

    synchronized = synchronize_report_versions(report_text, register_result)

    compact = synchronized.split("Thesenregister – kompakte Übersicht:", 1)[1]
    compact = compact.split("3. Ereignisradar", 1)[0]
    assert len(re.findall(r"(?m)^- Thesen-ID: T \\| Version ", compact)) == 1
    assert "- Thesen-ID: T | Version 2 | Status BESTÄTIGT" in compact
    assert "Einzelzeichen-These" not in compact


def test_replaced_thesis_requires_existing_successor(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload()), str(path))
    before = path.read_bytes()
    invalid = payload("ERSETZT")
    invalid["theses"][0]["replaced_by"] = "missing-successor"
    with pytest.raises(ValueError, match="replaced_by"):
        update_register(report(invalid), str(path))
    assert path.read_bytes() == before


def test_replaced_thesis_cannot_point_to_inactive_successor(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload()), str(path))
    before = path.read_bytes()
    old = payload("ERSETZT")["theses"][0]
    old["replaced_by"] = "successor"
    successor = payload("INVALIDIERT")["theses"][0]
    successor["thesis_id"] = "successor"
    with pytest.raises(ValueError, match="aktiven Nachfolger-ID"):
        update_register(report({"data_cutoff": payload()["data_cutoff"], "theses": [old, successor]}), str(path))
    assert path.read_bytes() == before


def test_existing_thesis_cannot_be_reclassified_as_new(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload("BESTÄTIGT")), str(path))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="darf nicht erneut Status NEU"):
        update_register(report(payload("NEU")), str(path))
    assert path.read_bytes() == before


def test_legacy_widerlegt_status_is_rejected_instead_of_remaining_active():
    with pytest.raises(ValueError, match="ungueltiger Status"):
        update_register(report(payload("WIDERLEGT")), "/tmp/should-not-be-written.json")


def test_archived_thesis_id_cannot_be_reactivated(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload("BESTÄTIGT")), str(path))
    invalidated = payload("INVALIDIERT")
    update_register(report(invalidated), str(path))
    before = path.read_bytes()

    with pytest.raises(ValueError, match="Archivierte These.*nicht reaktiviert"):
        update_register(report(payload("BESTÄTIGT")), str(path))
    assert path.read_bytes() == before


def test_archived_thesis_can_be_followed_by_a_new_neu_id(tmp_path):
    path = tmp_path / "register.json"
    update_register(report(payload("BESTÄTIGT")), str(path))
    update_register(report(payload("INVALIDIERT")), str(path))
    successor = payload("NEU")
    successor["theses"][0]["thesis_id"] = "macro-usd-002"
    result = update_register(report(successor), str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    archived = next(item for item in saved["theses"] if item["thesis_id"] == "macro-usd-001")
    active = next(item for item in saved["theses"] if item["thesis_id"] == "macro-usd-002")
    assert archived["active"] is False
    assert active["active"] is True and active["status"] == "NEU"
    assert result["new_theses"] == ["macro-usd-002"]


def test_replaced_by_is_normalized_to_null_for_legacy_non_replaced_theses():
    from trading_radar_thesen import validate_theses

    missing = payload()
    missing["theses"][0].pop("replaced_by")
    validate_theses(missing)
    assert missing["theses"][0]["replaced_by"] is None

    replaced_without_successor = payload("ERSETZT")
    replaced_without_successor["theses"][0].pop("replaced_by")
    with pytest.raises(ValueError, match="replaced_by"):
        validate_theses(replaced_without_successor)

    invalid_null = payload()
    invalid_null["theses"][0]["replaced_by"] = "some-successor"
    with pytest.raises(ValueError, match="muss.*null"):
        validate_theses(invalid_null)


def test_replaced_by_must_reference_a_nonterminal_thesis_in_same_payload():
    from trading_radar_thesen import validate_theses

    old = payload("ERSETZT")["theses"][0]
    old["replaced_by"] = "successor"
    successor = payload("INVALIDIERT")["theses"][0]
    successor["thesis_id"] = "successor"
    with pytest.raises(ValueError, match="aktive Nachfolger-ID"):
        validate_theses({"theses": [old, successor]})


def test_legacy_archived_status_is_preserved_but_replaced_by_is_migrated_to_null(tmp_path):
    path = tmp_path / "register.json"
    legacy = {
        "schema_version": 1,
        "updated_at": "2026-11-02T08:11:03+00:00",
        "theses": [{
            "thesis_id": "legacy-archived-001",
            "version": 1,
            "active": False,
            "status": "WIDERLEGT",
            "archived_at": "2026-11-01T08:00:00+00:00",
        }],
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    current = payload("NEU")
    current["theses"][0]["thesis_id"] = "new-thesis-002"
    update_register(report(current), str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    archived = next(item for item in saved["theses"] if item["thesis_id"] == "legacy-archived-001")
    assert archived["status"] == "WIDERLEGT"
    assert archived["active"] is False
    assert archived["replaced_by"] is None


@pytest.mark.parametrize("bad_id", [" macro-usd-001", "macro-usd-001 "])
def test_thesis_id_with_surrounding_whitespace_is_rejected(tmp_path, bad_id):
    invalid = payload("NEU")
    invalid["theses"][0]["thesis_id"] = bad_id
    with pytest.raises(ValueError, match="umgebenden Leerzeichen"):
        update_register(report(invalid), str(tmp_path / "register.json"))


@pytest.mark.parametrize("bad_id", ["macro-usd-001 ", " macro-usd-001"])
def test_archived_thesis_cannot_be_reintroduced_with_whitespace_variant(tmp_path, bad_id):
    path = tmp_path / "register.json"
    update_register(report(payload("BESTÄTIGT")), str(path))
    update_register(report(payload("INVALIDIERT")), str(path))
    before = path.read_bytes()

    variant = payload("NEU")
    variant["theses"][0]["thesis_id"] = bad_id
    with pytest.raises(ValueError, match="umgebenden Leerzeichen"):
        update_register(report(variant), str(path))
    assert path.read_bytes() == before


def test_persisted_register_snapshot_rejects_thesis_id_with_surrounding_whitespace():
    from trading_radar_thesen import _validate_register_snapshot

    invalid = {
        "schema_version": 1,
        "theses": [{"thesis_id": "T-1 ", "version": 1, "active": True}],
    }
    with pytest.raises(ValueError, match="umgebenden Leerzeichen"):
        _validate_register_snapshot(invalid, label="Test")


def test_legacy_omission_archives_are_migrated_without_fabricating_terminal_status(tmp_path):
    path = tmp_path / "register.json"
    active = dict(payload("BESTÄTIGT")["theses"][0])
    active["version"] = 1
    active["active"] = True
    legacy = dict(active)
    legacy.update({
        "thesis_id": "legacy-omitted-001",
        "active": False,
        "status": "BESTÄTIGT",
        "replaced_by": None,
        "archived_at": "2026-11-22T18:43:35+00:00",
        "archive_reason": "Im aktuellen Thesenblock nicht enthalten; nicht stillschweigend gelöscht.",
    })
    path.write_text(json.dumps({"schema_version": 1, "theses": [active, legacy]}), encoding="utf-8")

    result = update_register(report(payload("BESTÄTIGT")), str(path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    migrated = next(item for item in saved["theses"] if item["thesis_id"] == "legacy-omitted-001")

    assert result["updated"] is True
    assert migrated["active"] is False
    assert migrated["status"] == "ARCHIVIERT"
    assert migrated["historical_status"] == "BESTÄTIGT"  # original status remains auditable
    assert migrated["replaced_by"] is None  # no successor is invented
    assert migrated["legacy_archive"] is True
    assert migrated["archive_reason"] == (
        "Legacy-Archivierung durch frühere Auslassungslogik; keine explizite "
        "Invalidierung oder Ersetzung belegt."
    )


def test_inactive_nonterminal_thesis_without_exact_legacy_marker_is_rejected(tmp_path):
    path = tmp_path / "register.json"
    inactive = dict(payload("BESTÄTIGT")["theses"][0])
    inactive.update({"version": 1, "active": False, "archive_reason": "irgendein Grund"})
    path.write_text(json.dumps({"schema_version": 1, "theses": [inactive]}), encoding="utf-8")
    with pytest.raises(ValueError, match="nichtterminale These.*darf nicht archiviert sein"):
        update_register(report(payload("BESTÄTIGT")), str(path))


def test_thesis_id_match_deduplicates_primary_and_compact_views_and_ignores_register_evidence():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report

    payload_data = {"theses": [
        {"thesis_id": "THESIS-01"},
        {"thesis_id": "THESIS-02"},
    ]}
    report_text = (
        "2. Thesen-Update\n"
        "These 1: Defensive Rotation\n"
        "Thesen-ID: THESIS-01\n"
        "Status: BESTÄTIGT\n"
        "Thesenregister – kompakte Übersicht:\n"
        "- Thesen-ID: THESIS-01 | Version 1 | Status BESTÄTIGT | Konfidenz mittel\n"
        "- Thesen-ID: THESIS-02 | Version 1 | Status NEU | Konfidenz mittel\n"
        "THESENREGISTER-VERGLEICH: PASS\n"
        "THESIS_ID: THESIS-01\nTHESIS_ID: THESIS-02\n"
        "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
    )

    assert validate_thesis_ids_match_report(report_text, payload_data) == []


def test_thesis_id_match_rejects_duplicate_records_inside_compact_register():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report

    payload_data = {"theses": [{"thesis_id": "THESIS-01"}]}
    report_text = (
        "2. Thesen-Update\n"
        "Thesenregister – kompakte Übersicht:\n"
        "- Thesen-ID: THESIS-01 | Version 1 | Status BESTÄTIGT | Konfidenz mittel\n"
        "- Thesen-ID: THESIS-01 | Version 1 | Status BESTÄTIGT | Konfidenz mittel\n"
        "THESENREGISTER-VERGLEICH: PASS\nTHESIS_ID: THESIS-01\n"
        "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
    )

    findings = validate_thesis_ids_match_report(report_text, payload_data)
    assert findings and "doppelte These-IDs" in findings[0]


def test_thesis_id_match_requires_explicit_label_for_hyphenated_compact_ids():
    from trading_radar_neuauswertung import validate_thesis_ids_match_report

    payload_data = {"theses": [{"thesis_id": "THESIS-01"}]}
    report_text = (
        "2. Thesen-Update\n"
        "Thesenregister – kompakte Übersicht:\n"
        "- THESIS-01 | Version 1 | Status BESTÄTIGT | Konfidenz mittel\n"
        "THESENREGISTER-VERGLEICH: PASS\n"
        "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
    )

    findings = validate_thesis_ids_match_report(report_text, payload_data)
    assert findings and "keine eindeutigen These-IDs" in findings[0]
