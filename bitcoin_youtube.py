"""Bitcoin Trading DE YouTube source for the Gemini BTC briefing.

The source is qualitative only. It never creates or changes a trading signal.
The BTC 50W-SMA calculation remains the authoritative market signal.

Pipeline:
YouTube channel feed -> new video -> transcript -> relevance filter ->
compact source briefing -> Gemini -> state marked processed after successful
Gemini output.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import requests


CHANNEL_URL = "https://www.youtube.com/@bitcointradingde"
CHANNEL_NAME = "Bitcoin Trading DE"
TRANSCRIPT_URL_TEMPLATE = "https://youtube-transcript.ai/transcript/{video_id}.txt"

STATE_DIR = Path(__file__).resolve().parent / ".bitcoin_youtube_state"
STATE_FILE = STATE_DIR / "state.json"
BRIEFING_FILE = Path(__file__).resolve().parent / "Bitcoin_Trading_DE_Briefing.txt"

REQUEST_TIMEOUT = 30
MAX_NEW_RELEVANT_VIDEOS = 3
MAX_TRANSCRIPT_CHARS = 18000

# High-signal terms get more weight; general BTC terms only establish context.
HIGH_RELEVANCE_TERMS = (
    "50 wochen", "50w", "50-week", "50 week", "50w sma", "50 wochen sma",
    "trendwechsel", "trendwechsel", "bullmarkt", "bärenmarkt", "baerenmarkt",
    "bärenmarktstart", "baerenmarktstart", "bullrun", "bear market",
    "breakout", "breakdown", "support", "widerstand", "widerstandszone",
    "unterstützung", "unterstuetzung", "boden", "top", "trend",
)
GENERAL_RELEVANCE_TERMS = (
    "bitcoin", "btc", "bitcoinpreis", "btc/usd", "chartanalyse",
    "chartanalyse", "technical analysis", "technische analyse",
    "rsi", "macd", "moving average", "gleitender durchschnitt",
    "etf", "on-chain", "liquidität", "liquiditaet",
)

_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def _http_get(url: str) -> requests.Response:
    return requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Neuber-BTC-Research/1.0)"},
    )


def _load_state() -> dict[str, Any]:
    try:
        if STATE_FILE.exists():
            with STATE_FILE.open("r", encoding="utf-8") as f:
                value = json.load(f)
            if isinstance(value, dict):
                value.setdefault("initialized", False)
                value.setdefault("processed_video_ids", [])
                value.setdefault("pending_video_ids", [])
                return value
    except Exception as exc:
        print(f"WARNUNG: Bitcoin-Trading-DE-State konnte nicht gelesen werden: {exc}")
    return {
        "initialized": False,
        "processed_video_ids": [],
        "pending_video_ids": [],
    }


def _save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def _resolve_channel_id() -> str:
    # Resolve the stable UC... channel ID from the public handle page.
    response = _http_get(CHANNEL_URL)
    response.raise_for_status()
    text = response.text

    patterns = (
        r'"channelId":"(UC[a-zA-Z0-9_-]{20,})"',
        r'"externalId":"(UC[a-zA-Z0-9_-]{20,})"',
        r'"browseId":"(UC[a-zA-Z0-9_-]{20,})"',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    raise RuntimeError("YouTube-Kanal-ID konnte aus dem öffentlichen Kanal nicht ermittelt werden.")


def _fetch_feed(channel_id: str) -> list[dict[str, str]]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = _http_get(url)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    entries: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=_ATOM_NS).strip()
        title = entry.findtext("atom:title", default="", namespaces=_ATOM_NS).strip()
        published = entry.findtext("atom:published", default="", namespaces=_ATOM_NS).strip()
        link_el = entry.find("atom:link", _ATOM_NS)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        if video_id:
            entries.append({
                "video_id": video_id,
                "title": html.unescape(title),
                "published": published,
                "url": link or f"https://www.youtube.com/watch?v={video_id}",
            })
    entries.sort(key=lambda x: x.get("published", ""), reverse=True)
    return entries


def _fetch_transcript(video_id: str) -> str:
    url = TRANSCRIPT_URL_TEMPLATE.format(video_id=video_id)
    response = _http_get(url)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        raise RuntimeError("Transcript ist leer.")
    if "Transcript:" not in text:
        raise RuntimeError("Antwort sieht nicht wie das erwartete Transcript aus.")
    return text


def _clean_transcript(text: str) -> str:
    # Remove HTML entities, excessive blank lines and repeated consecutive
    # transcript lines caused by automatic-caption extraction.
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    previous = None
    repeat_count = 0
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        # Transcript timestamps can differ even when the caption text is
        # repeated several times by the extractor. Compare the spoken text
        # without the leading [mm:ss] marker.
        compare_line = re.sub(r"^\[[0-9:.]+\]\s*", "", line).strip()
        if compare_line == previous:
            repeat_count += 1
            if repeat_count >= 1:
                continue
        else:
            repeat_count = 0
        lines.append(line)
        previous = compare_line

    cleaned = "\n".join(lines)
    # Keep the beginning and keyword-relevant context if the transcript is long.
    if len(cleaned) <= MAX_TRANSCRIPT_CHARS:
        return cleaned

    lower = cleaned.lower()
    positions = []
    for term in HIGH_RELEVANCE_TERMS + GENERAL_RELEVANCE_TERMS:
        start = 0
        while True:
            pos = lower.find(term, start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + len(term)
    positions = sorted(set(positions))

    chunks = []
    if positions:
        for pos in positions[:20]:
            a = max(0, pos - 700)
            b = min(len(cleaned), pos + 1400)
            chunks.append(cleaned[a:b])
    else:
        chunks.append(cleaned[:6000])

    selected = []
    seen = set()
    for chunk in chunks:
        key = chunk[:250]
        if key not in seen:
            selected.append(chunk)
            seen.add(key)
    compact = "\n...\n".join(selected)
    return compact[:MAX_TRANSCRIPT_CHARS]


def _relevance_score(title: str, transcript: str) -> tuple[int, list[str]]:
    haystack = f"{title}\n{transcript}".lower()
    score = 0
    hits: list[str] = []

    for term in HIGH_RELEVANCE_TERMS:
        if term in haystack:
            score += 3
            hits.append(term)

    for term in GENERAL_RELEVANCE_TERMS:
        if term in haystack:
            score += 1
            hits.append(term)

    # A video must clearly concern Bitcoin/BTC, then pass the relevance threshold.
    btc_context = any(term in haystack for term in ("bitcoin", "btc", "btc/usd"))
    if not btc_context:
        return 0, []

    return score, list(dict.fromkeys(hits))


def _write_briefing(items: list[dict[str, Any]]) -> None:
    lines = [
        "BITCOIN TRADING DE – EXTERNE BTC-INFORMATIONSQUELLE",
        "",
        "Quelle: Bitcoin Trading DE",
        f"Kanal: {CHANNEL_URL}",
        "",
        "WICHTIGE REGEL:",
        "Diese Quelle ist ausschließlich qualitative Information.",
        "Sie verändert niemals BTC-Kursdaten, die 50W-SMA-Berechnung, CRV, Setup-Scores, Filter oder Handelsentscheidungen.",
        "Gemini soll die externe Einschätzung nur mit dem objektiven BTC/USD-50W-SMA-Status abgleichen.",
        "",
    ]

    if not items:
        lines.append("Keine neuen relevanten Videos seit dem letzten Lauf.")
    else:
        for item in items:
            lines.extend([
                "------------------------------------------------------------",
                f"Titel: {item['title']}",
                f"Veröffentlicht: {item['published']}",
                f"Video: {item['url']}",
                f"Relevanztreffer: {', '.join(item['hits'])}",
                "",
                "Transcript / relevante Transcript-Passagen:",
                item["transcript"],
                "",
            ])

    BRIEFING_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"Bitcoin-Trading-DE-Briefing: {BRIEFING_FILE}")


def prepare_bitcoin_youtube_context() -> list[str]:
    """Fetch new relevant videos and prepare a Gemini input file.

    The returned IDs remain pending until mark_bitcoin_youtube_processed()
    is called after a successful Gemini output.
    """
    state = _load_state()
    channel_id = _resolve_channel_id()
    entries = _fetch_feed(channel_id)
    if not entries:
        _write_briefing([])
        return []

    processed = set(state.get("processed_video_ids", []))
    pending = set(state.get("pending_video_ids", []))

    if not state.get("initialized"):
        candidates = entries[:1]  # Bootstrap: do not ingest the channel history.
        state["initialized"] = True
    else:
        candidates = [
            entry for entry in entries
            if entry["video_id"] not in processed and entry["video_id"] not in pending
        ]

    relevant: list[dict[str, Any]] = []
    pending_ids = list(pending)

    for entry in candidates:
        if len(relevant) >= MAX_NEW_RELEVANT_VIDEOS:
            break
        try:
            transcript = _fetch_transcript(entry["video_id"])
            cleaned = _clean_transcript(transcript)
            score, hits = _relevance_score(entry["title"], cleaned)
        except Exception as exc:
            print(f"WARNUNG: Transcript für {entry['video_id']} konnte nicht verarbeitet werden: {exc}")
            continue  # Retry on the next regular run.

        if score >= 4:
            relevant.append({
                **entry,
                "transcript": cleaned,
                "score": score,
                "hits": hits,
            })
            if entry["video_id"] not in pending_ids:
                pending_ids.append(entry["video_id"])
        else:
            processed.add(entry["video_id"])

    state["processed_video_ids"] = sorted(processed)[-200:]
    state["pending_video_ids"] = sorted(set(pending_ids))
    _save_state(state)
    _write_briefing(relevant)
    print(
        f"Bitcoin Trading DE: {len(relevant)} neue relevante Videos, "
        f"{len(state['processed_video_ids'])} bereits verarbeitet."
    )
    return [item["video_id"] for item in relevant]


def mark_bitcoin_youtube_processed(video_ids: list[str] | None) -> None:
    """Mark pending videos as processed only after Gemini output succeeded."""
    if not video_ids:
        return
    state = _load_state()
    pending = set(state.get("pending_video_ids", []))
    processed = set(state.get("processed_video_ids", []))
    for video_id in video_ids:
        pending.discard(video_id)
        processed.add(video_id)
    state["pending_video_ids"] = sorted(pending)
    state["processed_video_ids"] = sorted(processed)[-200:]
    _save_state(state)


if __name__ == "__main__":
    ids = prepare_bitcoin_youtube_context()
    print(f"Pending Video-IDs: {ids}")
