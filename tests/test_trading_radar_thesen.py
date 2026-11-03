import json
from pathlib import Path

import pytest

from trading_radar_thesen import extract_thesis_payload, update_register, synchronize_report_versions


def payload(status="NEU"):
    return {"data_cutoff": "2026-10-21T18:00:00+02:00", "theses": [{
        "thesis_id": "macro-usd-001", "thesis": "Dollarthese", "horizon": "2-4 Wochen",
        "alternative": "DXY stabilisiert sich trotz Renditedifferenz.",
        "evidence": ["Renditedifferenz"], "counter_evidence": ["Risiko"],
        "confidence": "mittel", "confirm_triggers": ["DXY bestaetigt"],
        "invalidate_triggers": ["DXY bricht"], "event_dependencies": [],
        "asset_classes": ["Gold"], "candidates": [], "status": status,
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
    first = update_register(report(payload()), str(path))
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
        {"thesis_id": "macro-usd-001", "version": 1, "active": True}
    ]
    assert second["thesis_versions"] == [
        {"thesis_id": "macro-usd-001", "version": 1, "active": True}
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
    second = update_register(report(replacement), str(path))
    assert second["new_theses"] == ["macro-usd-002"]
    assert second["archived_theses"] == ["macro-usd-001"]
    assert second["updated_theses"] == []
    assert second["unchanged_theses"] == []

    third = update_register(report(replacement), str(path))
    assert third["new_theses"] == []
    assert third["archived_theses"] == []
    assert third["updated_theses"] == []
    assert third["unchanged_theses"] == ["macro-usd-002"]
