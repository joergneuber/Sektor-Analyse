"""Additional qualitative YouTube market sources for Gold and Silver.

The existing Bitcoin Trading DE implementation in bitcoin_youtube.py remains
unchanged. This module reuses its feed/transcript helpers for Gold/Silver and
adds separate qualitative briefings without touching any trading calculations.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.request
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



def _fetch_channel_entries() -> list[dict[str, Any]]:
    """Fetch the shared Bitcoin Trading DE channel page without the RSS endpoint."""
    url = f"{btc.CHANNEL_URL.rstrip('/')}/videos"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            ),
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        page = response.read().decode("utf-8", errors="replace")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _runs_text(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(
                run.get("text", "")
                for run in runs
                if isinstance(run, dict) and isinstance(run.get("text"), str)
            )
        return ""

    def _published_text(value: dict[str, Any]) -> str:
        for key in ("publishedTimeText", "publishedTime"):
            text = _runs_text(value.get(key))
            if text:
                return text
        return ""

    def _add_entry(video_id: str, title: str, published: str = "") -> None:
        if not video_id or video_id in seen or not title:
            return
        entries.append({
            "video_id": video_id,
            "title": btc.html.unescape(title),
            "published": published,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
        seen.add(video_id)

    # Preferred path: parse YouTube's embedded initial data when available.
    markers = (
        "var ytInitialData = ",
        "ytInitialData = ",
        '"ytInitialData":',
    )
    start = -1
    marker = ""
    for candidate in markers:
        start = page.find(candidate)
        if start >= 0:
            marker = candidate
            break

    if start >= 0:
        start += len(marker)
        depth = 0
        in_string = False
        escaped = False
        end = None
        for pos in range(start, len(page)):
            char = page[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = pos + 1
                    break

        if end is not None:
            try:
                data = json.loads(page[start:end])

                def walk(value: Any) -> None:
                    if isinstance(value, dict):
                        video_id = value.get("videoId")
                        title = _runs_text(value.get("title"))
                        if isinstance(video_id, str) and title:
                            _add_entry(video_id, title, _published_text(value))
                        for child in value.values():
                            walk(child)
                    elif isinstance(value, list):
                        for child in value:
                            walk(child)

                walk(data)
            except json.JSONDecodeError:
                pass

    # Fallback for current YouTube page variants where no usable
    # ytInitialData/video renderer tree is embedded in the response.
    if not entries:
        video_matches = list(re.finditer(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"', page))
        for match in video_matches:
            video_id = match.group(1)
            window_start = max(0, match.start() - 3500)
            window_end = min(len(page), match.end() + 3500)
            window = page[window_start:window_end]

            title_match = re.search(
                r'"title"\s*:\s*\{\s*"runs"\s*:\s*\[\s*\{\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"',
                window,
            )
            if not title_match:
                title_match = re.search(
                    r'"title"\s*:\s*\{\s*"simpleText"\s*:\s*"((?:\\.|[^"\\])*)"',
                    window,
                )
            if not title_match:
                continue

            try:
                title = json.loads(f'"{title_match.group(1)}"')
            except json.JSONDecodeError:
                title = title_match.group(1)

            published = ""
            published_match = re.search(
                r'"publishedTimeText"\s*:\s*\{\s*"simpleText"\s*:\s*"((?:\\.|[^"\\])*)"',
                window,
            )
            if published_match:
                try:
                    published = json.loads(f'"{published_match.group(1)}"')
                except json.JSONDecodeError:
                    published = published_match.group(1)

            _add_entry(video_id, title, published)

    if not entries:
        raise RuntimeError(
            "YouTube-Kanalseite wurde abgerufen, aber es konnten keine Videos "
            "aus der aktuellen Seitenstruktur extrahiert werden."
        )

    print(f"YouTube-Kanal abgerufen: {len(entries)} Videos gefunden.")
    return entries


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
            lines.append("Keine neuen relevanten Videos verarbeitet")
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
    """Fetch the shared channel once and independently classify videos for Gold/Silver."""
    entries = _fetch_channel_entries()

    # Reuse the existing Bitcoin relevance/state/briefing logic unchanged.
    # Only its feed lookup is supplied by the shared channel fetch above.
    original_fetch_feed = btc._fetch_feed
    try:
        btc._fetch_feed = lambda _channel_id: entries
        btc_ids = btc.prepare_bitcoin_youtube_context()
    finally:
        btc._fetch_feed = original_fetch_feed

    state = _load_state()
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
    print(
        "YouTube-Marktquellen: "
        f"neu/geprüft={len(candidates)}, "
        + ", ".join(f"{k}={len(items_by_market[k])}" for k in MARKETS)
    )
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
