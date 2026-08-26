"""Additional qualitative YouTube market sources for Gold and Silver.

The existing Bitcoin Trading DE implementation in bitcoin_youtube.py remains
unchanged. This module reuses its feed/transcript helpers for Gold/Silver and
adds separate qualitative briefings without touching any trading calculations.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import bitcoin_youtube as btc

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".youtube_market_sources_state"
STATE_FILE = STATE_DIR / "state.json"
INCLUDED_FILE = STATE_DIR / "included_for_gemini.json"
MAX_NEW_RELEVANT_VIDEOS = 3
MAX_TRANSCRIPT_CHARS = 18000

GENERAL_RELEVANCE_TERMS = (
    "chartanalyse", "technical analysis", "technische analyse", "rsi", "macd",
    "moving average", "gleitender durchschnitt", "liquidität", "liquiditaet",
    "trend", "breakout", "breakdown", "support", "widerstand", "boden", "top",
)

MARKETS: dict[str, dict[str, Any]] = {
    "gold": {
        "display": "Gold Trading DE",
        "briefing": BASE_DIR / "Gold_Trading_DE_Briefing.txt",
        "context_terms": ("gold", "goldpreis", "gold price", "xau", "xau/usd", "xauusd"),
        "high_terms": (
            "gold", "goldpreis", "gold price", "xau", "xau/usd", "xauusd",
            "allzeithoch", "hoch", "konsolidierung", "korrektur", "support",
            "widerstand", "ausbruch", "trendwechsel", "bullrun", "bärenmarkt",
            "baerenmarkt", "ziel", "prognose",
        ),
        "rule": "Sie darf Gold-Kursdaten, technische Berechnungen, CRV, Setup-Scores, Filter oder Handelsentscheidungen nicht ersetzen.",
    },
    "silver": {
        "display": "Silber Trading DE",
        "briefing": BASE_DIR / "Silber_Trading_DE_Briefing.txt",
        "context_terms": ("silver", "silber", "silberpreis", "silver price", "xag", "xag/usd", "xagusd"),
        "high_terms": (
            "silver", "silber", "silberpreis", "silver price", "xag", "xag/usd", "xagusd",
            "allzeithoch", "hoch", "konsolidierung", "korrektur", "support",
            "widerstand", "ausbruch", "trendwechsel", "bullrun", "bärenmarkt",
            "baerenmarkt", "ziel", "prognose",
        ),
        "rule": "Sie darf Silber-Kursdaten, technische Berechnungen, CRV, Setup-Scores, Filter oder Handelsentscheidungen nicht ersetzen.",
    },
}


def _load_state() -> dict[str, Any]:
    try:
        if STATE_FILE.exists():
            value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("initialized", False)
                value.setdefault("processed_video_ids", [])
                value.setdefault("pending_by_market", {k: [] for k in MARKETS})
                return value
    except Exception as exc:
        print(f"WARNUNG: YouTube-Marktquellen-State konnte nicht gelesen werden: {exc}")
    return {
        "initialized": False,
        "processed_video_ids": [],
        "pending_by_market": {k: [] for k in MARKETS},
    }


def _save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _clean_transcript(text: str) -> str:
    text = btc.html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    previous = None
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        spoken = re.sub(r"^\[[0-9:.]+\]\s*", "", line).strip()
        if spoken == previous:
            continue
        lines.append(line)
        previous = spoken
    cleaned = "\n".join(lines)
    if len(cleaned) <= MAX_TRANSCRIPT_CHARS:
        return cleaned

    lower = cleaned.lower()
    positions: list[int] = []
    for config in MARKETS.values():
        for term in config["high_terms"] + GENERAL_RELEVANCE_TERMS:
            start = 0
            while True:
                pos = lower.find(term, start)
                if pos < 0:
                    break
                positions.append(pos)
                start = pos + len(term)
    chunks = [cleaned[max(0, pos - 700): min(len(cleaned), pos + 1400)]
              for pos in sorted(set(positions))[:25]]
    if not chunks:
        return cleaned[:6000]
    selected: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        key = chunk[:250]
        if key not in seen:
            selected.append(chunk)
            seen.add(key)
    return "\n...\n".join(selected)[:MAX_TRANSCRIPT_CHARS]


def _score_market(title: str, transcript: str, config: dict[str, Any]) -> tuple[int, list[str]]:
    haystack = f"{title}\n{transcript}".lower()
    context_hits = [term for term in config["context_terms"] if term in haystack]
    if not context_hits:
        return 0, []
    score = 0
    hits: list[str] = []
    for term in config["high_terms"]:
        if term in haystack:
            score += 3
            hits.append(term)
    for term in GENERAL_RELEVANCE_TERMS:
        if term in haystack:
            score += 1
            hits.append(term)
    return score, list(dict.fromkeys(hits))


def _write_briefings(items_by_market: dict[str, list[dict[str, Any]]]) -> None:
    for market, config in MARKETS.items():
        items = items_by_market.get(market, [])
        lines = [
            f"{config['display'].upper()} – EXTERNE YOUTUBE-INFORMATIONSQUELLE",
            "",
            f"Quelle: {btc.CHANNEL_NAME}",
            f"Kanal: {btc.CHANNEL_URL}",
            f"Erstellt: {dt.datetime.now().astimezone().strftime('%d.%m.%Y %H:%M %Z')}",
            "",
            "WICHTIGE REGEL:",
            "Diese Quelle ist ausschließlich qualitative Information.",
            config["rule"],
            "",
        ]
        if not items:
            lines.append("Keine neuen relevanten Videos seit dem letzten Lauf.")
        else:
            for item in items:
                lines += [
                    "------------------------------------------------------------",
                    f"Titel: {item['title']}",
                    f"Veröffentlicht: {item['published']}",
                    f"Video: {item['url']}",
                    f"Relevanztreffer: {', '.join(item['hits'])}",
                    "",
                    "Transcript / relevante Transcript-Passagen:",
                    item["transcript"],
                    "",
                ]
        config["briefing"].write_text("\n".join(lines), encoding="utf-8")


def prepare_youtube_market_context() -> dict[str, list[str]]:
    """Keep Bitcoin processing unchanged and prepare additional Gold/Silver context."""
    # Preserve the existing Bitcoin Trading DE implementation byte-for-byte.
    btc_ids = btc.prepare_bitcoin_youtube_context()

    state = _load_state()
    entries = btc._fetch_feed(btc._resolve_channel_id())
    processed = set(state.get("processed_video_ids", []))
    pending_by_market = {k: set(v) for k, v in state.get("pending_by_market", {}).items()}
    pending_by_market = {k: pending_by_market.get(k, set()) for k in MARKETS}

    if not entries:
        _write_briefings({k: [] for k in MARKETS})
        INCLUDED_FILE.write_text(json.dumps({"bitcoin": btc_ids, **{k: [] for k in MARKETS}}, indent=2), encoding="utf-8")
        return {"bitcoin": btc_ids, "gold": [], "silver": []}

    if not state.get("initialized"):
        candidates = entries[:1]
        state["initialized"] = True
    else:
        candidates = [e for e in entries if e["video_id"] not in processed]

    items_by_market: dict[str, list[dict[str, Any]]] = {k: [] for k in MARKETS}
    for entry in candidates:
        if all(len(items_by_market[k]) >= MAX_NEW_RELEVANT_VIDEOS for k in MARKETS):
            break
        try:
            transcript = _clean_transcript(btc._fetch_transcript(entry["video_id"]))
        except Exception as exc:
            print(f"WARNUNG: Transcript für {entry['video_id']} konnte für Gold/Silber nicht verarbeitet werden: {exc}")
            continue

        matched_any = False
        for market, config in MARKETS.items():
            score, hits = _score_market(entry["title"], transcript, config)
            if score >= 4:
                matched_any = True
                pending_by_market[market].add(entry["video_id"])
                if len(items_by_market[market]) < MAX_NEW_RELEVANT_VIDEOS:
                    items_by_market[market].append({**entry, "transcript": transcript, "score": score, "hits": hits})
        if not matched_any:
            processed.add(entry["video_id"])

    state["processed_video_ids"] = sorted(processed)[-200:]
    state["pending_by_market"] = {k: sorted(v) for k, v in pending_by_market.items()}
    _save_state(state)
    _write_briefings(items_by_market)

    included = {k: [x["video_id"] for x in items_by_market[k]] for k in MARKETS}
    included["bitcoin"] = btc_ids
    INCLUDED_FILE.write_text(json.dumps(included, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"bitcoin": btc_ids, **{k: [x["video_id"] for x in items_by_market[k]] for k in MARKETS}}


def mark_youtube_market_context_processed() -> None:
    """Commit Bitcoin plus Gold/Silver pending IDs only after Gemini succeeds."""
    btc_ids: list[str] = []
    included = {}
    if INCLUDED_FILE.exists():
        try:
            included = json.loads(INCLUDED_FILE.read_text(encoding="utf-8"))
        except Exception:
            included = {}
    btc_ids = included.get("bitcoin", []) or []
    # The Bitcoin file remains authoritative for its own state semantics.
    if btc_ids:
        btc.mark_bitcoin_youtube_processed(btc_ids)

    state = _load_state()
    processed = set(state.get("processed_video_ids", []))
    pending_by_market = {k: set(v) for k, v in state.get("pending_by_market", {}).items()}
    for market in MARKETS:
        for video_id in included.get(market, []) or []:
            pending_by_market.setdefault(market, set()).discard(video_id)
            processed.add(video_id)
    state["pending_by_market"] = {k: sorted(v) for k, v in pending_by_market.items()}
    state["processed_video_ids"] = sorted(processed)[-200:]
    _save_state(state)


if __name__ == "__main__":
    result = prepare_youtube_market_context()
    print(f"YouTube-Marktquellen vorbereitet: {result}")
