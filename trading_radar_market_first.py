"""Deterministic market-data and event normalization helpers for Trading Radar.

These helpers do not infer financial facts. They normalize source rows into one
canonical record and aggregate explicit source statuses without conflating them.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

MISSING_TOKENS = {"", "-", "—", "n/a", "na", "nicht verfügbar", "nicht verfuegbar",
                  "unbekannt", "fehlt", "unklar", "none", "null"}
OHLCV_STATUSES = {"VALID", "MISSING", "STALE", "INVALID"}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).strip().split())
    return None if value.casefold() in MISSING_TOKENS else value


def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one event. Missing fields are represented once as null/status."""
    out = {
        "event_id": _clean(event.get("event_id") or event.get("id")),
        "name": _clean(event.get("name") or event.get("event") or event.get("kennzahl")),
        "date": _clean(event.get("date") or event.get("datum")),
        "time": _clean(event.get("time") or event.get("uhrzeit")),
        "timezone": _clean(event.get("timezone") or event.get("zeitzone")),
        "period": _clean(event.get("period") or event.get("berichtszeitraum")),
        "previous": _clean(event.get("previous") or event.get("last_value") or event.get("letzter_wert")),
        "consensus": _clean(event.get("consensus") or event.get("konsens")),
        "actual": _clean(event.get("actual") or event.get("istwert")),
        "source": _clean(event.get("source") or event.get("quelle")),
    }
    missing = [key for key in ("name", "date", "source") if out[key] is None]
    out["status"] = "INCOMPLETE" if missing else "READY"
    out["missing_required"] = missing
    out["time_verified"] = bool(out["time"] and out["timezone"])
    return out


def deduplicate_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate events by stable ID, else by normalized name/date/source."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in events:
        item = normalize_event(raw)
        key = (item["event_id"] or "", item["name"] or "", item["date"] or "", item["source"] or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def classify_ohlcv_row(row: Mapping[str, Any], *, cutoff: datetime) -> str:
    """Classify a symbol row as VALID/MISSING/STALE/INVALID using explicit inputs."""
    values = [row.get(k) for k in ("open", "high", "low", "close", "volume", "timestamp")]
    if any(v is None or (isinstance(v, str) and v.strip().casefold() in MISSING_TOKENS) for v in values):
        return "MISSING"
    try:
        numbers = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
        stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError):
        return "INVALID"
    if stamp.tzinfo is None:
        return "INVALID"
    op, hi, lo, close, vol = numbers
    if min(op, hi, lo, close) <= 0 or vol < 0 or hi < max(op, lo, close) or lo > min(op, hi, close):
        return "INVALID"
    if stamp > cutoff:
        return "INVALID"  # future-dated data is invalid for this cutoff
    if (cutoff - stamp).total_seconds() > 7 * 24 * 3600:
        return "STALE"
    return "VALID"


def summarize_ohlcv_status(statuses: Iterable[str]) -> dict[str, int]:
    """Count each explicit OHLCV status exactly once; unknown values fail closed."""
    counts = {status: 0 for status in sorted(OHLCV_STATUSES)}
    for status in statuses:
        if status not in OHLCV_STATUSES:
            raise ValueError(f"Unbekannter OHLCV-Status: {status!r}")
        counts[status] += 1
    counts["TOTAL"] = sum(counts.values())
    return counts
