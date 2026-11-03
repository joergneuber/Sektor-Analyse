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
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BLOCK_RE = re.compile(r"```TRADING_RADAR_THESEN_JSON\s*(.*?)\s*```", re.S)
REQUIRED = {
    "thesis_id", "thesis", "horizon", "alternative", "evidence", "counter_evidence",
    "confidence", "confirm_triggers", "invalidate_triggers",
    "event_dependencies", "asset_classes", "candidates", "status",
}
VALID_STATUS = {
    # Zielarchitektur v2.1 (Unicode-Schreibweise) plus Altregister-Kompatibilität.
    "NEU", "BESTÄTIGT", "ABGESCHWÄCHT", "WARTET AUF EREIGNIS",
    "INVALIDIERT", "ERSETZT",
    "BESTAETIGT", "VERSTAERKT", "ABGESCHWAECHT", "WARTET_AUF_EVENT",
    "WIDERLEGT", "UNKLAR",
}


STATUS_ALIASES = {
    "BESTAETIGT": "BESTÄTIGT",
    "ABGESCHWAECHT": "ABGESCHWÄCHT",
    "WARTET_AUF_EVENT": "WARTET AUF EREIGNIS",
}


def canonicalize_thesis_status(value: Any) -> str:
    """Store one canonical Unicode spelling for known legacy ASCII aliases."""
    raw = re.sub(r"\\s+", " ", str(value or "").strip()).upper()
    return STATUS_ALIASES.get(raw, raw)


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
        thesis["status"] = canonicalize_thesis_status(thesis["status"])
        if thesis["status"] not in VALID_STATUS:
            raise ValueError(f"These {tid}: ungueltiger Status {thesis['status']!r}")
        for field in ("evidence", "counter_evidence", "confirm_triggers", "invalidate_triggers", "event_dependencies", "asset_classes", "candidates"):
            if not isinstance(thesis[field], list):
                raise ValueError(f"These {tid}: {field} muss eine Liste sein")
        for field in ("thesis", "horizon", "alternative", "confidence"):
            if not isinstance(thesis[field], str) or not thesis[field].strip():
                raise ValueError(f"These {tid}: {field} darf nicht leer sein")
        for field in ("evidence", "counter_evidence", "confirm_triggers", "invalidate_triggers", "asset_classes"):
            if not thesis[field] or any(not isinstance(item, str) or not item.strip() for item in thesis[field]):
                raise ValueError(f"These {tid}: {field} benötigt konkrete, nichtleere Einträge")
        checked.append(thesis)
    return checked



def synchronize_report_versions(report: str, register_result: dict[str, Any]) -> str:
    """Align visible and JSON thesis versions with the versions actually persisted."""
    versions = {
        str(item.get("thesis_id")): int(item["version"])
        for item in register_result.get("thesis_versions", [])
        if isinstance(item, dict)
        and item.get("thesis_id")
        and isinstance(item.get("version"), int)
        and not isinstance(item.get("version"), bool)
        and item.get("version", 0) >= 1
    }
    if not versions:
        return report

    text = str(report or "")
    block_match = BLOCK_RE.search(text)
    if block_match:
        try:
            payload = json.loads(block_match.group(1))
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("theses"), list):
            changed = False
            for thesis in payload["theses"]:
                if not isinstance(thesis, dict):
                    continue
                version = versions.get(str(thesis.get("thesis_id", "")))
                if version is not None and thesis.get("version") != version:
                    thesis["version"] = version
                    changed = True
            if changed:
                replacement = "```TRADING_RADAR_THESEN_JSON\n" + json.dumps(
                    payload, ensure_ascii=False, indent=2
                ) + "\n```"
                text = text[:block_match.start()] + replacement + text[block_match.end():]

    section2 = re.search(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?2\s*[.·]\s*Thesen-Update[^\r\n]*$",
        text,
    )
    section3 = re.search(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?3\s*[.·]\s*Ereignisradar[^\r\n]*$",
        text,
    )
    if not section2 or not section3 or section2.start() >= section3.start():
        return text
    body_start, body_end = section2.end(), section3.start()
    body = text[body_start:body_end]
    markers = list(re.finditer(
        r"(?im)^[ \t]*(?:[-*•][ \t]*)?(?:\*\*|__)?Thesen-ID(?:\*\*|__)?\s*:\s*([^\r\n]+)",
        body,
    ))
    for index, marker in reversed(list(enumerate(markers))):
        thesis_id = marker.group(1).strip()
        version = versions.get(thesis_id)
        if version is None:
            continue
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        block = body[marker.start():end]
        updated_block, count = re.subn(
            r"(?im)^([ \t]*(?:[-*•][ \t]*)?(?:\*\*|__)?Version(?:\*\*|__)?\s*:\s*)\d+",
            lambda match: match.group(1) + str(version),
            block,
            count=1,
        )
        if count:
            body = body[:marker.start()] + updated_block + body[end:]
    return text[:body_start] + body + text[body_end:]

def _validate_register_snapshot(payload: Any, *, label: str) -> list[dict[str, Any]]:
    """Validate a persisted register before it can be used as comparison baseline."""
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
    ):
        raise ValueError(f"{label}: ungueltiges Register-Schema")
    theses = payload.get("theses")
    if not isinstance(theses, list) or not theses:
        raise ValueError(f"{label}: theses[] fehlt oder ist leer")
    seen = set()
    active_count = 0
    for index, item in enumerate(theses):
        if not isinstance(item, dict):
            raise ValueError(f"{label}: These[{index}] ist kein Objekt")
        thesis_id = item.get("thesis_id")
        version = item.get("version")
        if not isinstance(thesis_id, str) or not thesis_id.strip():
            raise ValueError(f"{label}: These[{index}] besitzt keine thesis_id")
        if thesis_id in seen:
            raise ValueError(f"{label}: doppelte thesis_id {thesis_id}")
        seen.add(thesis_id)
        if type(version) is not int or version < 1:
            raise ValueError(f"{label}: {thesis_id} besitzt keine positive Version")
        active = item.get("active", True)
        if type(active) is not bool:
            raise ValueError(f"{label}: {thesis_id} besitzt keinen booleschen active-Status")
        if active:
            active_count += 1
    return [
        {"thesis_id": item["thesis_id"], "version": item["version"],
         "active": item.get("active", True)}
        for item in theses
    ]


def _register_comparison_sha256(previous_sha256: str, current_sha256: str) -> str:
    payload = {
        "previous_sha256": previous_sha256,
        "current_sha256": current_sha256,
        "schema": "trading-radar-thesis-register-comparison-v1",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    previous_exists = path.is_file()
    previous_sha256 = None
    previous_thesis_versions: list[dict[str, Any]] = []
    previous_thesis_count = 0
    previous_active_count = 0
    # An absent register is not evidence that the historical thesis set was
    # empty. The first substantive snapshot is a bootstrap only, never PASS.
    if not previous_exists and not theses:
        return {
            "updated": False,
            "reason": "Vorzustand fehlt; leeres theses[] darf nicht als leeres Register-Baseline ausgegeben werden",
            "comparison_status": "BASELINE_MISSING",
        }
    if previous_exists:
        previous_bytes = path.read_bytes()
        previous_sha256 = hashlib.sha256(previous_bytes).hexdigest()
        try:
            previous = json.loads(previous_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bestehendes Register ist nicht lesbares JSON: {exc}") from exc
        previous_thesis_versions = _validate_register_snapshot(
            previous, label="Vorzustand"
        )
        previous_thesis_count = len(previous_thesis_versions)
        previous_active_count = sum(1 for item in previous_thesis_versions if item["active"])
    old_by_id = {item.get("thesis_id"): item for item in previous["theses"] if isinstance(item, dict)}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    current = []
    new_ids: list[str] = []
    updated_ids: list[str] = []
    unchanged_ids: list[str] = []
    archived_ids: list[str] = []
    current_ids = {item["thesis_id"] for item in theses}
    for item in theses:
        old = old_by_id.get(item["thesis_id"], {})
        entry = dict(item)
        # Versionen repraesentieren fachliche Aenderungen, nicht jeden Lauf.
        # Vergleich nur ueber den inhaltlichen Thesen-Payload; Laufzeitfelder
        # und Daten-Cutoff werden separat aktualisiert.
        content_fields = (
            "thesis", "expected_movement", "horizon", "time_horizon", "alternative", "evidence",
            "counter_evidence", "confidence", "confirm_triggers",
            "invalidate_triggers", "event_dependencies", "asset_classes",
            "candidates", "status",
        )
        changed = not old or any(old.get(field) != entry.get(field) for field in content_fields)
        old_version = int(old.get("version", 0) or 0)
        entry["version"] = old_version + 1 if changed else max(1, old_version)
        if not old:
            new_ids.append(entry["thesis_id"])
        elif changed:
            updated_ids.append(entry["thesis_id"])
        else:
            unchanged_ids.append(entry["thesis_id"])
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
            archived["archived_at"] = now
            archived["archive_reason"] = "Im aktuellen Thesenblock nicht enthalten; nicht stillschweigend gelöscht."
            if old.get("active", True) is True:
                archived_ids.append(str(old_id))
            current.append(archived)
    merged = {"schema_version": 1, "updated_at": now, "theses": current}
    _validate_register_snapshot(merged, label="Nachzustand")
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
    current_bytes = path.read_bytes()
    current_payload = json.loads(current_bytes.decode("utf-8"))
    current_thesis_versions = _validate_register_snapshot(
        current_payload, label="Nachzustand"
    )
    current_sha256 = hashlib.sha256(current_bytes).hexdigest()
    comparison_sha256 = (
        _register_comparison_sha256(previous_sha256, current_sha256)
        if previous_exists and previous_sha256 else None
    )
    return {
        "updated": True, "count": len(current), "path": str(path),
        "comparison_status": "PASS" if comparison_sha256 else "BASELINE_MISSING",
        "previous_sha256": previous_sha256,
        "current_sha256": current_sha256,
        "comparison_sha256": comparison_sha256,
        "previous_thesis_count": previous_thesis_count,
        "previous_active_count": previous_active_count,
        "previous_thesis_versions": previous_thesis_versions,
        "current_thesis_count": len(current_thesis_versions),
        "active_count": sum(1 for item in current_thesis_versions if item["active"]),
        "new_theses": sorted(new_ids),
        "updated_theses": sorted(updated_ids),
        "unchanged_theses": sorted(unchanged_ids),
        "archived_theses": sorted(archived_ids),
        "thesis_versions": current_thesis_versions,
    }
