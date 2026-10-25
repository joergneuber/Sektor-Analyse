"""Validierung und persistente Ablage strukturierter Trading-Radar-Thesen.

Die Langtext-Historie bleibt unverändert. Dieses Register ist ein kompakter,
maschinenlesbarer Zusatz und wird nur aus einem explizit markierten JSON-Block
in einer echten Gemini-Auswertung aktualisiert.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BLOCK_RE = re.compile(r"```TRADING_RADAR_THESEN_JSON\s*(.*?)\s*```", re.S)
REQUIRED = {
    "thesis_id", "thesis", "horizon", "evidence", "counter_evidence",
    "confidence", "confirm_triggers", "invalidate_triggers",
    "event_dependencies", "asset_classes", "candidates", "status",
}
VALID_STATUS = {
    "NEU", "BESTAETIGT", "VERSTAERKT", "ABGESCHWAECHT", "WARTET_AUF_EVENT",
    "WIDERLEGT", "ERSETZT", "UNKLAR",
}


def extract_thesis_payload(report: str) -> dict[str, Any] | None:
    """Liest den optionalen, klar abgegrenzten JSON-Block; sonst None."""
    match = BLOCK_RE.search(report or "")
    if not match:
        return None
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict) or not isinstance(payload.get("theses"), list):
        raise ValueError("TRADING_RADAR_THESEN_JSON muss ein Objekt mit theses[] sein")
    return payload


def validate_theses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    theses = payload.get("theses")
    if not isinstance(theses, list):
        raise ValueError("theses muss eine Liste sein")
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, thesis in enumerate(theses):
        if not isinstance(thesis, dict):
            raise ValueError(f"These {index}: Eintrag muss ein Objekt sein")
        missing = REQUIRED - thesis.keys()
        if missing:
            raise ValueError(f"These {index}: Pflichtfelder fehlen: {', '.join(sorted(missing))}")
        tid = thesis["thesis_id"]
        if not isinstance(tid, str) or not tid.strip():
            raise ValueError(f"These {index}: thesis_id muss ein nichtleerer Text sein")
        if tid in seen:
            raise ValueError(f"Doppelte thesis_id im Lauf: {tid}")
        seen.add(tid)
        if thesis["status"] not in VALID_STATUS:
            raise ValueError(f"These {tid}: ungueltiger Status {thesis['status']!r}")
        for field in ("evidence", "counter_evidence", "confirm_triggers", "invalidate_triggers", "event_dependencies", "asset_classes", "candidates"):
            if not isinstance(thesis[field], list):
                raise ValueError(f"These {tid}: {field} muss eine Liste sein")
        if not isinstance(thesis["thesis"], str) or not thesis["thesis"].strip():
            raise ValueError(f"These {tid}: thesis darf nicht leer sein")
        checked.append(thesis)
    return checked


def update_register(report: str, register_path: str, *, data_cutoff: str | None = None) -> dict[str, Any]:
    """Validiert und schreibt Register atomar; ohne Block bleibt Datei unangetastet."""
    payload = extract_thesis_payload(report)
    if payload is None:
        return {"updated": False, "reason": "kein strukturierter Thesenblock"}
    cutoff = data_cutoff or payload.get("data_cutoff")
    if not isinstance(cutoff, str) or not cutoff.strip():
        # A processing timestamp is not evidence of the market-data cutoff.
        # Fail closed and leave the existing register untouched.
        return {"updated": False, "reason": "kein Daten-Cutoff aus den verwendeten Daten"}
    try:
        parsed_cutoff = datetime.fromisoformat(cutoff.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("data_cutoff muss ein ISO-8601-Zeitstempel sein") from exc
    if parsed_cutoff.tzinfo is None:
        raise ValueError("data_cutoff muss eine Zeitzone enthalten")
    cutoff = parsed_cutoff.isoformat(timespec="seconds")
    theses = validate_theses(payload)
    path = Path(register_path)
    previous: dict[str, Any] = {"schema_version": 1, "theses": []}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            previous = json.load(handle)
        if not isinstance(previous, dict) or not isinstance(previous.get("theses"), list):
            raise ValueError("bestehendes Register hat kein gueltiges theses[]-Schema")
    old_by_id = {item.get("thesis_id"): item for item in previous["theses"] if isinstance(item, dict)}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    current = []
    current_ids = {item["thesis_id"] for item in theses}
    for item in theses:
        old = old_by_id.get(item["thesis_id"], {})
        entry = dict(item)
        # Versionen repraesentieren fachliche Aenderungen, nicht jeden Lauf.
        # Vergleich nur ueber den inhaltlichen Thesen-Payload; Laufzeitfelder
        # und Daten-Cutoff werden separat aktualisiert.
        content_fields = (
            "thesis", "expected_movement", "horizon", "time_horizon", "evidence",
            "counter_evidence", "confidence", "confirm_triggers",
            "invalidate_triggers", "event_dependencies", "asset_classes",
            "candidates", "status",
        )
        changed = not old or any(old.get(field) != entry.get(field) for field in content_fields)
        old_version = int(old.get("version", 0) or 0)
        entry["version"] = old_version + 1 if changed else max(1, old_version)
        entry["created_at"] = old.get("created_at", now)
        entry["updated_at"] = now if changed else old.get("updated_at", now)
        entry["data_cutoff"] = cutoff
        entry["active"] = True
        entry["last_seen_at"] = now
        current.append(entry)
    # Nicht mehr ausgegebene Thesen werden archiviert, nicht still geloescht.
    for old_id, old in old_by_id.items():
        if old_id not in current_ids:
            archived = dict(old)
            archived["active"] = False
            current.append(archived)
    merged = {"schema_version": 1, "updated_at": now, "theses": current}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            json.dump(merged, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"updated": True, "count": len(current), "path": str(path)}
