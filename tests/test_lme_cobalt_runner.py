"""Kobalt-Quellentest v11 – forensisch gehärteter Diagnose-Runner.

Ziel:
- mehrere unabhängige Quellen in EINEM Lauf prüfen
- exakten Zieltag erzwingen
- LME-Kobalt-Vertrags-/Preisart nur akzeptieren, wenn sie im selben
  Evidenzkontext wie Datum und Wert steht
- False Positives aus fremden Metallen/Indizes vermeiden

Rein diagnostisch. Keine Produktionsfreigabe.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

TIMEOUT = 25
RETRIES = 3
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NeuberMacro-LME-Cobalt-Test/11.0)",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

# Quellen mit unterschiedlichen Evidenzwegen. SP_ANGEL ist ein datierter
# Artikel und wird deshalb nur für seinen tatsächlich bekannten Tag gewertet.
SOURCES = [
    ("LME_OFFICIAL", "https://www.lme.com/Metals/EV/LME-Cobalt", "official_lme", None),
    ("LME_OFFICIAL_EN", "https://www.lme.com/en/Metals/EV/LME-Cobalt", "official_lme", None),
    ("LME_OFFICIAL_LOWER", "https://www.lme.com/metals/ev/lme-cobalt", "official_lme", None),
    ("LME_HISTORICAL", "https://www.lme.com/Market-data/Reports-and-data/Historical-data-for-cash-settled-futures", "official_lme_historical", None),
    ("SP_ANGEL", "https://www.share-talk.com/sp-angel-todays-market-view-tuesday-8th-september-2026/", "third_party_lme_reference", dt.date(2026, 9, 8)),
    ("HAWK_INSIGHT", "https://www.hawkinsight.com/en/article/wM8Ig", "third_party_lme_reference", None),
    ("CBONDS", "https://cbonds.com/indexes/79109/", "third_party_lme_reference", None),
    ("TRADING_ECONOMICS", "https://tradingeconomics.com/commodity/cobalt", "third_party_market", None),
    ("METAL_RADAR", "https://metalradar.com/price/cobalt/lme/cash/official-close", "third_party_lme_reference", None),
    ("METALS_MARKET", "https://www.metalsmarket.net/w_lmeCashSettEUR.html", "third_party_lme_reference", None),
    ("QUIRIOS", "https://quirios.com.br/cotacao-metais/?periodo=1m", "third_party_lme_reference", None),
    ("TRENDFORCE", "https://datatrack.trendforce.com/Chart/content/2657/spot-settlement-price-selling-price-lme-cobalt", "third_party_lme_reference", None),
]

NUMBER_RE = r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?"


def target_default() -> dt.date:
    d = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def norm_num(raw: str) -> float | None:
    s = html.unescape(raw or "").strip().replace("\xa0", " ")
    s = re.sub(r"[^0-9,.-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[1]
        s = s.replace(",", ".") if len(tail) in (1, 2) else s.replace(",", "")
    elif "." in s and len(s.rsplit(".", 1)[1]) == 3 and s.count(".") == 1:
        s = s.replace(".", "")
    try:
        v = float(s)
        return v if 1000 <= v <= 200000 else None
    except ValueError:
        return None


def plain_text(body: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body or ""))).strip()


def date_markers(target: dt.date) -> list[str]:
    # ISO + common rendered forms. %-d is not portable on Windows.
    return [
        target.isoformat(),
        target.strftime("%d.%m.%Y"),
        target.strftime("%d/%m/%Y"),
        target.strftime("%m/%d/%Y"),
        target.strftime("%d %B %Y"),
        target.strftime("%B %d, %Y"),
        target.strftime("%d %b %Y"),
        target.strftime("%b %d, %Y"),
        f"{target.day} {target.strftime('%B %Y')}",
        f"{target.strftime('%B')} {target.day}, {target.year}",
    ]


def has_exact_date(text: str, target: dt.date) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in date_markers(target))


def fetch_retry(url: str) -> tuple[requests.Response | None, int, str | None]:
    last_exc = None
    last_status = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)
            last_status = r.status_code
            # A 403/429/5xx can be transient at the edge; actually retry those.
            if r.status_code not in {403, 429} and r.status_code < 500:
                return r, attempt, None
            if attempt < RETRIES:
                time.sleep(0.8 * attempt)
            else:
                return r, attempt, None
        except Exception as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
            if attempt < RETRIES:
                time.sleep(0.8 * attempt)
    return None, RETRIES, last_exc or f"HTTP_{last_status}"


def add_candidate(out, value, method, context, target, contract=None,
                   price_type=None, unit=None, source_date=None):
    if value is None:
        return
    exact_date = has_exact_date(context, target)
    # source_date is stronger than a page-wide date mention.
    if source_date is not None:
        exact_date = exact_date and source_date == target
    out.append({
        "value": value,
        "method": method,
        "context": context[:700],
        "contract": contract,
        "price_type": price_type,
        "unit": unit,
        "exact_date": exact_date,
    })


def source_specific_candidates(label: str, body: str, target: dt.date, fixed_date: dt.date | None) -> list[dict]:
    """Only emit candidates with a tight cobalt/date/value relationship."""
    text = plain_text(body)
    out = []

    # 1) SP Angel – explicit known wording:
    #    Cobalt LME 3m US$44,940/t ...
    #    The article itself is dated 08 Sep 2026; never reuse it for another
    #    target date.
    if label == "SP_ANGEL":
        if fixed_date != target:
            return out
        pat = rf"(?is)cobalt\s+lme\s+3m[^0-9]{{0,80}}(?:us?\$|\$)?\s*({NUMBER_RE})\s*/?\s*t"
        for m in re.finditer(pat, text):
            ctx = text[max(0, m.start()-220):m.end()+220]
            add_candidate(out, norm_num(m.group(1)), "SPANGEL_EXPLICIT_LME_3M",
                          ctx, target, "CO", "3M", "USD/t", fixed_date)

    # 2) Hawk Insight – require LME cobalt + close/closed + value.
    if label == "HAWK_INSIGHT":
        pat = rf"(?is)lme\s+cobalt[^.{{}}]{{0,220}}?(?:closed|close|settlement)[^0-9]{{0,100}}(?:us?\$|\$)?\s*({NUMBER_RE})\s*/?\s*t"
        for m in re.finditer(pat, text):
            ctx = text[max(0, m.start()-260):m.end()+260]
            if has_exact_date(ctx, target):
                add_candidate(out, norm_num(m.group(1)), "HAWK_EXPLICIT_LME_COBALT_CLOSE",
                              ctx, target, "CO", "CLOSE", "USD/t")

    # 3) Cbonds – do NOT scan all numbers around a date.
    #    Candidate must be directly attached to "Cobalt", with the target date
    #    in the same tight window. This prevents 3337/16695/etc. from other
    #    instruments on the page becoming "cobalt" values.
    if label == "CBONDS":
        patterns = [
            rf"(?is)(?:{re.escape(target.strftime('%d/%m/%Y'))}|{re.escape(target.strftime('%Y-%m-%d'))}|{re.escape(target.strftime('%d.%m.%Y'))})[^.{{}}]{{0,220}}?cobalt[^0-9]{{0,80}}({NUMBER_RE})\s*(?:usd\s*/?\s*t|usd/t)",
            rf"(?is)cobalt[^0-9]{{0,80}}({NUMBER_RE})\s*(?:usd\s*/?\s*t|usd/t)[^.{{}}]{{0,220}}?(?:{re.escape(target.strftime('%d/%m/%Y'))}|{re.escape(target.strftime('%Y-%m-%d'))}|{re.escape(target.strftime('%d.%m.%Y'))})",
        ]
        for pat in patterns:
            for m in re.finditer(pat, text):
                ctx = text[max(0, m.start()-250):m.end()+250]
                # Recheck date + cobalt adjacency in the actual context.
                if has_exact_date(ctx, target) and re.search(r"cobalt", ctx, re.I):
                    # Extract the number from the cobalt-local part, not from
                    # the entire context.
                    cm = re.search(r"(?is)cobalt[^0-9]{0,80}(" + NUMBER_RE + r")\s*(?:usd\s*/?\s*t|usd/t)", ctx)
                    if cm:
                        local = ctx[ctx.lower().find("cobalt"):cm.end()]
                        # Never cross another metal/instrument label. Cbonds pages
                        # contain many rows in one rendered text block.
                        if re.search(r"(?i)\b(?:nickel|zinc|lead|tin|copper|aluminium|aluminum|gold|silver|platinum|palladium)\b", local):
                            continue
                        add_candidate(out, norm_num(cm.group(1)), "CBONDS_TARGET_DATE_COBALT_ROW",
                                      ctx, target, None, "SPOT", "USD/t")

    # 4) Trading Economics – date-local block, but value must be immediately
    #    tied to a cobalt label; never accept every number in the date block.
    if label == "TRADING_ECONOMICS":
        for marker in date_markers(target):
            for m in re.finditer(re.escape(marker), text, re.I):
                ctx = text[max(0, m.start()-120):m.end()+350]
                for cm in re.finditer(r"(?is)cobalt[^0-9]{0,100}(?:us?\$|\$)?\s*(" + NUMBER_RE + r")\s*(?:usd\s*/?\s*t|/t|ton)?", ctx):
                    add_candidate(out, norm_num(cm.group(1)), "TE_TARGET_DATE_COBALT_LOCAL",
                                  ctx, target, None, None, "USD/t")

    # 5) Explicitly labelled official-close/settlement sources.
    if label == "METAL_RADAR":
        for field, ptype in (("settlement", "SETTLEMENT"),
                             ("official close", "OFFICIAL_CLOSE"),
                             ("price", "PRICE")):
            pat = rf"(?is)cobalt[^.{{}}]{{0,160}}{re.escape(field)}[^0-9]{{0,70}}(?:\$|us?\$)?\s*({NUMBER_RE})"
            for m in re.finditer(pat, text):
                ctx = text[max(0, m.start()-250):m.end()+250]
                if has_exact_date(ctx, target):
                    add_candidate(out, norm_num(m.group(1)), "METALRADAR_" + ptype,
                                  ctx, target, "CO", ptype, "USD/t")

    # 6) Generic fallback is deliberately restrictive: no generic number
    #    harvesting. A value must follow "Cobalt" and the same context must
    #    contain the exact target date.
    for marker in date_markers(target):
        for dm in re.finditer(re.escape(marker), text, re.I):
            ctx = text[max(0, dm.start()-250):dm.end()+450]
            cm = re.search(r"(?is)cobalt[^0-9]{0,100}(?:lme[^0-9]{0,60})?(?:cash|3m|3-month|settlement|close|price)?[^0-9]{0,40}(?:us?\$|\$)?\s*(" + NUMBER_RE + r")\s*(?:usd\s*/?\s*t|/t)", ctx)
            if cm:
                local = ctx[ctx.lower().find("cobalt"):cm.end()]
                if re.search(r"(?i)\b(?:nickel|zinc|lead|tin|copper|aluminium|aluminum|gold|silver|platinum|palladium)\b", local):
                    continue
                add_candidate(out, norm_num(cm.group(1)), "STRICT_DATE_COBALT_VALUE",
                              ctx, target, None, None, "USD/t")

    # Deduplicate.
    uniq = {}
    for e in out:
        key = (e["value"], e["method"], e["exact_date"], e["contract"], e["price_type"])
        uniq[key] = e
    return list(uniq.values())


def score(e: dict) -> int:
    s = 0
    if e.get("exact_date"):
        s += 8
    if e.get("contract") == "CO":
        s += 5
    if e.get("price_type") in {"3M", "SETTLEMENT", "OFFICIAL_CLOSE", "CLOSE"}:
        s += 3
    if e.get("unit") == "USD/t":
        s += 2
    if e.get("method", "").startswith(("SPANGEL", "HAWK", "METALRADAR")):
        s += 2
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-date", default=None)
    args = ap.parse_args()
    target = dt.date.fromisoformat(args.target_date) if args.target_date else target_default()

    print("LME COBALT SOURCE RUNNER v12")
    print(f"Runner Python: {sys.version.split()[0]}")
    print(f"target_date={target.isoformat()}")
    print(f"sources={len(SOURCES)} retries_per_source={RETRIES}")

    results = []
    with ThreadPoolExecutor(max_workers=min(12, len(SOURCES))) as pool:
        futs = {pool.submit(fetch_retry, url): (label, url, source_class, fixed_date)
                for label, url, source_class, fixed_date in SOURCES}
        for fut in as_completed(futs):
            label, url, source_class, fixed_date = futs[fut]
            try:
                resp, attempts, err = fut.result()
                if resp is None:
                    print(f"KOBALT_PROBE {label}: EXCEPTION after={attempts} attempts error={err}")
                    results.append({"label": label, "status": "EXCEPTION", "source_class": source_class, "text": "", "candidates": []})
                    continue
                text = plain_text(resp.text)
                candidates = source_specific_candidates(label, resp.text, target, fixed_date)
                print(
                    f"KOBALT_PROBE {label}: status=HTTP_{resp.status_code} attempts={attempts} "
                    f"bytes={len(resp.content)} has_cobalt={'cobalt' in text.lower()} "
                    f"has_exact_date={has_exact_date(text,target)} candidates={len(candidates)}"
                )
                for e in candidates[:12]:
                    e["score"] = score(e)
                    print(
                        f"KOBALT_EVIDENCE {label}: value={e['value']} exact_date={e['exact_date']} "
                        f"contract={e['contract'] or 'NONE'} price_type={e['price_type'] or 'NONE'} "
                        f"unit={e['unit'] or 'NONE'} score={e['score']} method={e['method']}"
                    )
                results.append({"label": label, "status": f"HTTP_{resp.status_code}",
                                "source_class": source_class, "text": text, "candidates": candidates})
            except Exception as exc:
                print(f"KOBALT_PROBE {label}: EXCEPTION {type(exc).__name__}: {exc}")
                results.append({"label": label, "status": "EXCEPTION", "source_class": source_class, "text": "", "candidates": []})

    exact = []
    strong = []
    for r in results:
        for e in r["candidates"]:
            if e["exact_date"]:
                exact.append((r["label"], e))
            if e["exact_date"] and (
                e["contract"] == "CO"
                and e["price_type"] in {"3M", "SETTLEMENT", "OFFICIAL_CLOSE", "CLOSE"}
            ):
                strong.append((r["label"], e))

    consensus = {}
    for label, e in exact:
        consensus.setdefault(round(e["value"], 6), set()).add(label)
    consensus = {v: labels for v, labels in consensus.items() if len(labels) >= 2}

    print(f"LME_COBALT_EXACT_DATE_CANDIDATES: {len(exact)}")
    print(f"LME_COBALT_STRONG_EVIDENCE: {len(strong)}")
    if consensus:
        for value, labels in sorted(consensus.items(), key=lambda x: (-len(x[1]), x[0])):
            print(f"LME_COBALT_CROSS_SOURCE_CONSENSUS: value={value} sources={','.join(sorted(labels))}")
    else:
        print("LME_COBALT_CROSS_SOURCE_CONSENSUS: NONE")

    if strong:
        best = sorted(strong, key=lambda x: (-score(x[1]), x[0]))[0]
        print(
            "LME_COBALT_STRONG_BEST: "
            f"source={best[0]} value={best[1]['value']} exact_date={best[1]['exact_date']} "
            f"contract={best[1]['contract'] or 'NONE'} price_type={best[1]['price_type'] or 'NONE'} "
            f"unit={best[1]['unit'] or 'NONE'} score={score(best[1])}"
        )
    else:
        print("LME_COBALT_STRONG_BEST: NONE")

    print(
        "LME_COBALT_PRODUCTION_GATE: "
        + ("CANDIDATE_READY_FOR_MANUAL_FINAL_VALIDATION" if strong else "NOT_PROVEN")
    )
    print(
        "LME_COBALT_RUNNER_TEST: PASS - multi-source diagnostic completed; "
        "strict date/value coupling applied; production classification remains separate."
    )


if __name__ == "__main__":
    main()
