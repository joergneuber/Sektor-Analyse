import json
from pathlib import Path

import pytest

from trading_radar_thesen import extract_thesis_payload, update_register


def payload(status="NEU"):
    return {"data_cutoff": "2026-10-21T18:00:00+02:00", "theses": [{
        "thesis_id": "macro-usd-001", "thesis": "Dollarthese", "horizon": "2-4 Wochen",
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
    assert second_doc["theses"][0]["status"] == "BESTAETIGT"


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
