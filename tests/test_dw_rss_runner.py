#!/usr/bin/env python3
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from pathlib import Path

DW_RSS_URL = "https://rss.dw.com/syndication/feeds/VAS_DE_NeuseelandNews.32453-copypaste.html"
TIMEOUT = 30
MIN_FULL_TEXT = 250

def clean_fragment(value):
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n\n", value)
    return value.strip()

def field(block, names):
    wanted = {n.lower().replace(":", "").replace("-", "").replace("_", "").replace(" ", "") for n in names}
    # Handle XML-ish tags, namespaced tags, and label/value forms.
    patterns = [
        r"<\s*([A-Za-z0-9_:.-]+)\s*>\s*(.*?)\s*<\s*/\s*\1\s*>",
        r"<\s*([A-Za-z0-9_:.-]+)\s*/\s*>\s*(.*?)\s*(?=\n[A-Za-z])",
    ]
    for pat in patterns:
        for m in re.finditer(pat, block, flags=re.I | re.S):
            tag = m.group(1)
            norm = tag.lower().replace(":", "").replace("-", "").replace("_", "").replace(" ", "")
            if norm in wanted:
                return clean_fragment(m.group(2))
    # Label-based DW presentation fallback: "Full text\n..." until next known field.
    labels = "|".join(re.escape(x) for x in names)
    m = re.search(rf"(?im)^\s*(?:{labels})\s*[:\-]?\s*\n(.*?)(?=^\s*(?:Id|Date|Title|Short title|Teaser|Short teaser|Full text|Author|Item URL)\s*[:\-]?\s*$|\Z)", block, flags=re.S)
    return clean_fragment(m.group(1)) if m else ""

def parse_items(raw):
    items = re.findall(r"<item\b[^>]*>(.*?)</item\s*>", raw, flags=re.I | re.S)
    if items:
        return items
    # Label-based fallback: split on Item N / Id boundaries, without assuming XML validity.
    blocks = re.split(r"(?im)^\s*Item\s+\d+\s*$", raw)
    return [b for b in blocks[1:] if re.search(r"(?im)^\s*(?:Id|Title|Date)\b", b)]

def extract(raw):
    results = []
    for block in parse_items(raw):
        title = field(block, ["title"])
        date = field(block, ["date", "pubDate", "published"])
        teaser = field(block, ["teaser", "short teaser", "description"])
        full = field(block, ["full text", "fulltext", "full article", "fullarticle", "article text", "articletext", "content:encoded", "contentencoded", "encoded"])
        url = field(block, ["item url", "itemurl", "url", "link"])
        if not title and not full and not teaser:
            continue
        status = "FULL_TEXT" if len(full) >= MIN_FULL_TEXT else ("TEASER_ONLY" if teaser else "UNAVAILABLE")
        results.append({
            "title": title,
            "date": date,
            "url": url,
            "teaser": teaser,
            "full_text": full,
            "content_status": status,
            "teaser_length": len(teaser),
            "full_text_length": len(full),
        })
    return results

def main():
    started = time.time()
    req = Request(DW_RSS_URL, headers={"User-Agent": "Sektor-Analyse-DW-Test/1.0"})
    with urlopen(req, timeout=TIMEOUT) as r:
        raw_bytes = r.read()
        status = getattr(r, "status", 200)
        content_type = r.headers.get("Content-Type", "")
    raw = raw_bytes.decode("utf-8", errors="replace")
    items = extract(raw)

    print(f"DW HTTP STATUS={status}")
    print(f"DW CONTENT_TYPE={content_type}")
    print(f"DW RESPONSE_LENGTH={len(raw_bytes)}")
    print(f"DW ITEMS_FOUND={len(items)}")
    for i, item in enumerate(items[:5], 1):
        print(f"ITEM_{i}_TITLE={item['title'][:180]}")
        print(f"ITEM_{i}_DATE={item['date']}")
        print(f"ITEM_{i}_URL={item['url']}")
        print(f"ITEM_{i}_TEASER_LENGTH={item['teaser_length']}")
        print(f"ITEM_{i}_FULL_TEXT_LENGTH={item['full_text_length']}")
        print(f"ITEM_{i}_CONTENT_STATUS={item['content_status']}")

    report = {
        "url": DW_RSS_URL,
        "http_status": status,
        "content_type": content_type,
        "response_length": len(raw_bytes),
        "items_found": len(items),
        "items": items[:5],
        "elapsed_seconds": round(time.time() - started, 3),
    }
    Path("dw_extracted_articles.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path("dw_raw_response.txt").write_text(raw, encoding="utf-8")

    full = [x for x in items if x["content_status"] == "FULL_TEXT" and x["full_text_length"] > x["teaser_length"]]
    if status != 200:
        raise SystemExit("FAIL: DW HTTP status is not 200")
    if not items:
        raise SystemExit("FAIL: no DW articles/items extracted")
    if not full:
        raise SystemExit("FAIL: no article with verified FULL_TEXT longer than teaser")
    print(f"DW VERIFIED_FULL_TEXT_ITEMS={len(full)}")
    print("DW ISOLATED TEST=PASS")

if __name__ == "__main__":
    main()
