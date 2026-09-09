"""Kobalt-Quellentest v9 fuer den GitHub Runner.

Ziel: moeglichst in EINEM Lauf mehrere unabhaengige Wege fuer LME-Kobalt
untersuchen und einen exakten Tageswert mit Vertrags-/Preisart-Evidenz finden.

Wichtig:
- rein diagnostisch; kein Produktionscode
- Drittquellen werden NICHT automatisch als REAL_LME freigegeben
- mehrere HTTP-Versuche je Quelle
- Quellen laufen parallel, Fehler einer Quelle blockieren keine andere
- exakter Zieltag ist Pflicht fuer STRONG-EVIDENCE
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

TIMEOUT = 25
RETRIES = 3
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NeuberMacro-LME-Cobalt-Test/9.0)",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

SOURCES = [
    # Official LME: several URL variants because WAF/locale routing can differ.
    ("LME_OFFICIAL", "https://www.lme.com/Metals/EV/LME-Cobalt", "official_lme"),
    ("LME_OFFICIAL_EN", "https://www.lme.com/en/Metals/EV/LME-Cobalt", "official_lme"),
    ("LME_OFFICIAL_LOWER", "https://www.lme.com/metals/ev/lme-cobalt", "official_lme"),
    ("LME_HISTORICAL", "https://www.lme.com/Market-data/Reports-and-data/Historical-data-for-cash-settled-futures", "official_lme_historical"),
    # Strong third-party references with explicit LME cobalt wording.
    ("SP_ANGEL", "https://www.share-talk.com/sp-angel-todays-market-view-tuesday-8th-september-2026/", "third_party_lme_reference"),
    ("HAWK_INSIGHT", "https://www.hawkinsight.com/en/article/wM8Ig", "third_party_lme_reference"),
    ("CBONDS", "https://cbonds.com/indexes/79109/", "third_party_lme_reference"),
    ("TRADING_ECONOMICS", "https://tradingeconomics.com/commodity/cobalt", "third_party_market"),
    ("METAL_RADAR", "https://metalradar.com/price/cobalt/lme/cash/official-close", "third_party_lme_reference"),
    ("METALS_MARKET", "https://www.metalsmarket.net/w_lmeCashSettEUR.html", "third_party_lme_reference"),
    ("QUIRIOS", "https://quirios.com.br/cotacao-metais/?periodo=1m", "third_party_lme_reference"),
    ("TRENDFORCE", "https://datatrack.trendforce.com/Chart/content/2657/spot-settlement-price-selling-price-lme-cobalt", "third_party_lme_reference"),
]

NUMBER_RE = r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?"


def target_default() -> dt.date:
    d = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def norm_num(raw: str) -> float | None:
    s = html.unescape(raw or "").strip().replace("\xa0", " ").replace(" ", "")
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
    return [
        target.isoformat(),
        target.strftime("%d.%m.%Y"),
        target.strftime("%d/%m/%Y"),
        target.strftime("%m/%d/%Y"),
        target.strftime("%d %B %Y"),
        target.strftime("%B %d, %Y"),
        target.strftime("%d %b %Y"),
        target.strftime("%b %d, %Y"),
        target.strftime("%-d %B %Y"),
        target.strftime("%B %-d, %Y"),
    ]


def has_exact_date(text: str, target: dt.date) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in date_markers(target))


def fetch_retry(url: str) -> tuple[requests.Response | None, int, str | None]:
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)
            return r, attempt, None
        except Exception as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
            if attempt < RETRIES:
                time.sleep(0.8 * attempt)
    return None, RETRIES, last_exc


def source_specific_candidates(label: str, text: str, target: dt.date) -> list[dict]:
    """High-signal parsers for sources known to expose explicit LME cobalt values."""
    out: list[dict] = []
    markers = date_markers(target)

    def add(value: float | None, method: str, context: str, contract: str | None = None,
            price_type: str | None = None, unit: str | None = None, exact_date: bool | None = None):
        if value is None:
            return
        out.append({
            "value": value,
            "method": method,
            "context": context[:500],
            "contract": contract,
            "price_type": price_type,
            "unit": unit,
            "exact_date": has_exact_date(context, target) if exact_date is None else exact_date,
        })

    # 1) SP Angel: explicit "Cobalt LME 3m US$44,940/t vs ...".
    if label == "SP_ANGEL":
        for m in re.finditer(r"(?is)cobalt\s+lme\s+3m[^0-9]{0,80}(" + NUMBER_RE + r")", text):
            ctx = text[max(0, m.start()-180):m.end()+180]
            add(norm_num(m.group(1)), "SPANGEL_LME_3M", ctx, "CO", "3M", "USD/t")

    # 2) Hawk Insight: exact date article + "LME cobalt closed ... at 44940/ton".
    if label == "HAWK_INSIGHT":
        for m in re.finditer(r"(?is)lme\s+cobalt[^.]{0,180}?(?:at\s+us?\$?|at\s+\$?)(" + NUMBER_RE + r")", text):
            ctx = text[max(0, m.start()-250):m.end()+200]
            add(norm_num(m.group(1)), "HAWK_LME_COBALT_CLOSE", ctx, "CO", "CLOSE", "USD/t")

    # 3) Cbonds: exact target date + Cobalt + USD/T. It is a reference source,
    # but contract wording is not guaranteed on the page, so keep contract None.
    if label == "CBONDS":
        for m in re.finditer(r"(?is)cobalt\s*</?[^>]*>\s*[:|]?\s*(" + NUMBER_RE + r")\s*(?:usd\s*/?\s*t|usd/t)?", text):
            ctx = text[max(0, m.start()-220):m.end()+220]
            add(norm_num(m.group(1)), "CBONDS_COBALT", ctx, None, "SPOT", "USD/t")
        # Plain-text rendering fallback.
        for m in re.finditer(r"(?is)cobalt\s+[^\n]{0,80}?(" + NUMBER_RE + r")\s+usd\s*/?\s*t", text):
            ctx = text[max(0, m.start()-220):m.end()+220]
            add(norm_num(m.group(1)), "CBONDS_COBALT_TEXT", ctx, None, "SPOT", "USD/t")

    # 4) Metal Radar: official-close and settlement are explicitly labelled.
    if label == "METAL_RADAR":
        for field, ptype in (("settlement", "SETTLEMENT"), ("official close", "OFFICIAL_CLOSE"), ("price", "PRICE")):
            pat = rf"(?is){re.escape(field)}[^0-9]{{0,50}}(\$?\s*{NUMBER_RE})"
            for m in re.finditer(pat, text):
                ctx = text[max(0, m.start()-180):m.end()+180]
                add(norm_num(m.group(1)), "METALRADAR_" + ptype, ctx, "CO", ptype, "USD/t")

    # 5) Trading Economics: current/dated rows; only exact-date candidates get strong evidence.
    if label == "TRADING_ECONOMICS":
        for marker in markers:
            for m in re.finditer(re.escape(marker), text, re.I):
                ctx = text[max(0, m.start()-300):m.end()+500]
                for n in re.findall(NUMBER_RE, ctx):
                    v = norm_num(n)
                    if v is not None:
                        add(v, "TE_EXACT_DATE_CONTEXT", ctx, None, None, "USD/t", True)

    # 6) General fallback: target-date window + LME/cobalt + labelled price.
    for marker in markers:
        for m in re.finditer(re.escape(marker), text, re.I):
            ctx = text[max(0, m.start()-500):m.end()+900]
            if not re.search(r"cobalt|lme", ctx, re.I):
                continue
            for mm in re.finditer(r"(?is)(?:cobalt|lme cobalt|price|settlement|closed)[^0-9]{0,120}(" + NUMBER_RE + r")", ctx):
                add(norm_num(mm.group(1)), "DATE_CONTEXT_LABELLED", ctx, None, None, "USD/t", True)

    # Deduplicate by value/method/date/contract.
    uniq = {}
    for x in out:
        key = (x["value"], x["method"], x["exact_date"], x["contract"], x["price_type"])
        uniq[key] = x
    return list(uniq.values())


def score(e: dict) -> int:
    s = 0
    if e.get("exact_date"): s += 8
    if e.get("contract") == "CO": s += 5
    if e.get("price_type") in {"3M", "SETTLEMENT", "OFFICIAL_CLOSE", "CLOSE"}: s += 3
    if e.get("unit") == "USD/t": s += 2
    if e.get("method", "").startswith(("SPANGEL", "HAWK", "METALRADAR")): s += 2
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-date", default=None)
    args = ap.parse_args()
    target = dt.date.fromisoformat(args.target_date) if args.target_date else target_default()

    print("LME COBALT SOURCE RUNNER v9")
    print(f"Runner Python: {sys.version.split()[0]}")
    print(f"target_date={target.isoformat()}")
    print(f"sources={len(SOURCES)} retries_per_source={RETRIES}")

    results = []
    with ThreadPoolExecutor(max_workers=min(12, len(SOURCES))) as pool:
        futs = {pool.submit(fetch_retry, url): (label, url, source_class) for label, url, source_class in SOURCES}
        for fut in as_completed(futs):
            label, url, source_class = futs[fut]
            try:
                resp, attempts, err = fut.result()
                if resp is None:
                    print(f"KOBALT_PROBE {label}: EXCEPTION after={attempts} attempts error={err}")
                    results.append({"label": label, "status": "EXCEPTION", "source_class": source_class, "text": "", "candidates": []})
                    continue
                text = plain_text(resp.text)
                candidates = source_specific_candidates(label, text, target)
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
                results.append({"label": label, "status": f"HTTP_{resp.status_code}", "source_class": source_class, "text": text, "candidates": candidates})
            except Exception as exc:
                print(f"KOBALT_PROBE {label}: EXCEPTION {type(exc).__name__}: {exc}")
                results.append({"label": label, "status": "EXCEPTION", "source_class": source_class, "text": "", "candidates": []})

    exact = []
    strong = []
    for r in results:
        for e in r["candidates"]:
            if e["exact_date"]:
                exact.append((r["label"], e))
            if e["exact_date"] and (e["contract"] == "CO" or e["price_type"] in {"3M", "SETTLEMENT", "OFFICIAL_CLOSE", "CLOSE"}):
                strong.append((r["label"], e))

    # Cross-source consensus: same exact-date value from >=2 independent sources.
    consensus = {}
    for label, e in exact:
        key = round(e["value"], 6)
        consensus.setdefault(key, set()).add(label)
    consensus = {v: labels for v, labels in consensus.items() if len(labels) >= 2}

    print(f"LME_COBALT_EXACT_DATE_CANDIDATES: {len(exact)}")
    print(f"LME_COBALT_STRONG_EVIDENCE: {len(strong)}")
    if consensus:
        for value, labels in sorted(consensus.items(), key=lambda x: (-len(x[1]), x[0])):
            print(f"LME_COBALT_CROSS_SOURCE_CONSENSUS: value={value} sources={','.join(sorted(labels))}")
    else:
        print("LME_COBALT_CROSS_SOURCE_CONSENSUS: NONE")

    # The diagnostic run passes if all mandatory non-cobalt Westmetall tests are
    # not part of this isolated file. For cobalt, we intentionally do not make
    # production approval automatic solely from a third-party consensus.
    if strong:
        best = sorted(strong, key=lambda x: (-x[1]["score"], x[0]))[0]
        print(
            "LME_COBALT_STRONG_BEST: "
            f"source={best[0]} value={best[1]['value']} exact_date={best[1]['exact_date']} "
            f"contract={best[1]['contract'] or 'NONE'} price_type={best[1]['price_type'] or 'NONE'} "
            f"unit={best[1]['unit'] or 'NONE'} score={best[1]['score']}"
        )
    else:
        print("LME_COBALT_STRONG_BEST: NONE")

    print(
        "LME_COBALT_PRODUCTION_GATE: "
        + ("CANDIDATE_READY_FOR_MANUAL_FINAL_VALIDATION" if strong else "NOT_PROVEN")
    )
    print(
        "LME_COBALT_RUNNER_TEST: PASS - multi-source diagnostic completed; "
        "production classification remains separate from source discovery."
    )


if __name__ == "__main__":
    main()
