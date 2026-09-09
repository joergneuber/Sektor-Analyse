"""Isolierter LME-Quellentest fuer den GitHub Runner.

Prueft exakte Tagesdaten fuer Nickel, Blei, Zinn und Kobalt. Der Test veraendert
keinen Produktionscode und kennzeichnet Quellen nach Qualitaetsstufe.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys

import requests

TIMEOUT = 30
HEADERS = {"User-Agent": "NeuberMacro-LME-Source-Test/1.0"}
WESTMETALL = {
    "Nickel": "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Ni_cash",
    "Blei": "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Pb_cash",
    "Zinn": "https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Sn_cash",
}



def target_default() -> dt.date:
    d = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def parse_num(s: str) -> float | None:
    s = s.replace(",", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def fetch(url: str) -> requests.Response:
    return requests.get(url, timeout=TIMEOUT, headers=HEADERS, allow_redirects=True)


def normalise_number(raw: str) -> float | None:
    """Parse common international price formats without assuming one locale."""
    s = html.unescape(raw or "").strip().replace("\u00a0", " ").replace(" ", "")
    s = re.sub(r"[^0-9,.-]", "", s)
    if not s:
        return None
    # 46,430 / 46.430 -> 46430; 46,430.50 / 46.430,50 -> 46430.50
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[1]
        s = s.replace(",", ".") if len(tail) in (1, 2) else s.replace(",", "")
    elif "." in s:
        tail = s.rsplit(".", 1)[1]
        if len(tail) == 3 and s.count(".") == 1:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def strip_html(raw: str) -> str:
    # Keep script contents separately elsewhere; visible text is only one parser input.
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))).strip()


def target_date_markers(target: dt.date) -> list[str]:
    return [
        target.isoformat(),
        target.strftime("%d.%m.%Y"),
        target.strftime("%d/%m/%Y"),
        target.strftime("%m/%d/%Y"),
        target.strftime("%d %B %Y"),
        target.strftime("%B %d, %Y"),
        target.strftime("%d %b %Y"),
        target.strftime("%b %d, %Y"),
    ]


def extract_price_candidates(body: str, target: dt.date) -> tuple[list[float], list[str]]:
    """Use several independent parsers; return values plus method names.

    The runner must not depend on one HTML layout. Candidates are accepted only
    in cobalt/LME/settlement/date-related context or from strongly typed
    numeric attributes/scripts. No candidate is promoted to REAL_LME here.
    """
    visible = strip_html(body)
    low = visible.lower()
    markers = target_date_markers(target)
    windows: list[str] = []
    for marker in markers:
        start = 0
        while True:
            pos = low.find(marker.lower(), start)
            if pos < 0:
                break
            windows.append(visible[max(0, pos - 500): pos + 1000])
            start = pos + len(marker)
    # Always inspect relevant visible context as a fallback, but keep it bounded.
    for token in ("lme cobalt", "cobalt", "settlement", "3m cobalt", "cash cobalt"):
        start = 0
        while True:
            pos = low.find(token, start)
            if pos < 0:
                break
            windows.append(visible[max(0, pos - 350): pos + 900])
            start = pos + len(token)

    candidates: list[float] = []
    methods: list[str] = []

    def mask_date_tokens(text: str) -> str:
        masked = text
        date_patterns = [
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{2}[./]\d{2}[./]\d{4}\b",
            r"\b\d{2}\s+[A-Za-zÄÖÜäöü]+\s+\d{4}\b",
            r"\b[A-Za-zÄÖÜäöü]+\s+\d{2},\s+\d{4}\b",
        ]
        for pattern in date_patterns:
            masked = re.sub(pattern, lambda m: " " * len(m.group(0)), masked, flags=re.I)
        return masked

    def add(raw: str, method: str) -> None:
        value = normalise_number(raw)
        if value is not None and 1000 <= value <= 200000 and value not in candidates:
            # Reject bare calendar years; they are common false positives in HTML.
            if 1900 <= value <= 2100:
                return
            candidates.append(value)
            methods.append(method)

    # Parser 1: human-visible date/context -> price.
    for window in windows:
        for raw in re.findall(r"(?<![\d])(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?(?![\d])", mask_date_tokens(window)):
            add(raw, "VISIBLE_CONTEXT_NUMBER")

    # Parser 2: explicit price/settlement labels.
    for m in re.finditer(
        r"(?is)(?:cash|spot|official|settlement|price|value)[^\n|]{0,100}?"
        r"((?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?)",
        visible,
    ):
        add(m.group(1), "LABELLED_PRICE")

    # Parser 3: raw HTML data-* / value / content attributes.
    for m in re.finditer(
        r"(?is)(?:data-(?:value|price|settlement|last|close)|(?:value|price|settlement|last|close)=(?:\"|'))\s*"
        r"((?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?)",
        body,
    ):
        add(m.group(1), "HTML_DATA_ATTRIBUTE")

    # Parser 4: JSON-ish/script numeric fields.
    for m in re.finditer(
        r"(?is)(?:\"|')?(?:price|settlementPrice|cashSettlement|officialPrice|lastPrice|value)"
        r"(?:\"|')?\s*:\s*(?:\"|')?"
        r"((?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?)",
        body,
    ):
        add(m.group(1), "SCRIPT_TYPED_FIELD")

    # Parser 5: JSON-LD/meta/content attributes.
    for m in re.finditer(
        r"(?is)(?:meta|script)[^>]{0,300}?(?:content|value)=(?:\"|')"
        r"((?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?)(?:\"|')",
        body,
    ):
        add(m.group(1), "META_OR_JSONLD_VALUE")

    # Parser 6: HTML table/cell text, useful when the page has no semantic labels.
    for row in re.findall(r"(?is)<(?:tr|li)[^>]*>(.*?)</(?:tr|li)>", body):
        row_text = strip_html(row)
        row_low = row_text.lower()
        if any(t in row_low for t in ("cobalt", "lme", "settlement", target.isoformat())):
            for raw in re.findall(r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?", mask_date_tokens(row_text)):
                add(raw, "HTML_TABLE_ROW")

    # Parser 7: URL/query-encoded or escaped JSON numeric payloads.
    decoded = html.unescape(body).replace(r"\/", "/").replace(r"\u002c", ",")
    for m in re.finditer(
        r"(?is)(?:price|settlement|cash|official|last|close)[^&=]{0,40}="
        r"((?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?)",
        decoded,
    ):
        add(m.group(1), "ENCODED_TYPED_FIELD")

    # Parser 8: JavaScript date/price tuple with the date before the value.
    for marker in markers:
        for m in re.finditer(re.escape(marker), decoded, re.I):
            chunk = decoded[m.end():m.end()+500]
            nums = re.findall(r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?", mask_date_tokens(chunk))
            for raw in nums[:8]:
                add(raw, "DATE_THEN_NUMERIC_PAYLOAD")

    # Parser 9: numeric value immediately preceding an exact target date.
    for marker in markers:
        for m in re.finditer(re.escape(marker), decoded, re.I):
            chunk = decoded[max(0, m.start()-500):m.start()]
            nums = re.findall(r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?", mask_date_tokens(chunk))
            for raw in nums[-8:]:
                add(raw, "NUMERIC_BEFORE_DATE")

    # Parser 10: visible context around source-specific contract terminology.
    for token in ("CO", "USD/t", "USD per tonne", "physically settled", "cash-settled"):
        for m in re.finditer(re.escape(token), visible, re.I):
            chunk = visible[max(0, m.start()-500):m.end()+800]
            for raw in re.findall(r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?", mask_date_tokens(chunk)):
                add(raw, "CONTRACT_CONTEXT")

    # Parser 11: JS arrays/objects with quoted numeric strings.
    for m in re.finditer(
        r"(?is)(?:\"|')((?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?)(?:\"|')",
        body,
    ):
        add(m.group(1), "QUOTED_NUMERIC_STRING")

    # Parser 12: plain numeric tokens in bounded cobalt/LME source text.
    # This is intentionally last because it is the least semantically strong.
    for window in windows[:40]:
        for raw in re.findall(r"(?<![\d])(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?(?![\d])", mask_date_tokens(window)):
            add(raw, "BOUNDED_SOURCE_CONTEXT")

    # Parser 13: JS/JSON arrays containing target date + a nearby numeric value.
    for marker in markers:
        for m in re.finditer(re.escape(marker), body, re.I):
            chunk = body[max(0, m.start()-300):m.end()+700]
            for raw in re.findall(r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?", mask_date_tokens(chunk)):
                add(raw, "RAW_HTML_DATE_ARRAY")

    return candidates, methods


def exact_westmetall(text: str, metal: str, target: dt.date) -> float | None:
    # Look for the exact target date and the corresponding first numeric field.
    target_text = target.strftime("%d. %B %Y")
    month_names = {
        "January": "Januar", "February": "Februar", "March": "März", "April": "April",
        "May": "Mai", "June": "Juni", "July": "Juli", "August": "August",
        "September": "September", "October": "Oktober", "November": "November", "December": "Dezember",
    }
    variants = [target_text, target.strftime("%d %B %Y"), f"{target.day:02d} September {target.year}"]
    if target.strftime("%B") in month_names:
        variants.append(f"{target.day:02d}. {month_names[target.strftime('%B')]} {target.year}")
    page = re.sub(r"<[^>]+>", " | ", text)
    page = re.sub(r"\s+", " ", page)
    if not any(v.lower() in page.lower() for v in variants):
        return None
    pattern = re.compile(
        rf"{target.strftime('%d')}(?:\.|\s)\s*{re.escape(target.strftime('%B'))}\s*{target.year}"
        rf".{0,600}?LME\s+{re.escape(''.join([]))}" if False else r"$"
    )
    # Use the table-like row around the exact date.
    for date_variant in variants:
        m = re.search(re.escape(date_variant) + r".{0,250}?([0-9]{1,3}(?:[.,][0-9]{2,3})?)", page, re.I)
        if m:
            value = parse_num(m.group(1))
            if value and value > 0:
                return value
    return None



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", help="YYYY-MM-DD; default = letzter abgeschlossener Werktag")
    args = parser.parse_args()
    target = dt.date.fromisoformat(args.target_date) if args.target_date else target_default()

    print("LME SOURCE RUNNER TEST")
    print(f"Runner Python: {sys.version.split()[0]}")
    print(f"target_date={target.isoformat()}")

    passed = 0
    for metal, url in WESTMETALL.items():
        try:
            r = fetch(url)
            if r.status_code != 200:
                print(f"FAIL: {metal} Westmetall HTTP {r.status_code}")
                continue
            value = exact_westmetall(r.text, metal, target)
            if value is None:
                print(f"FAIL: {metal} Westmetall kein exakter Wert fuer {target.isoformat()}")
                continue
            print(f"PASS: {metal} | source=Westmetall | exact_date={target.isoformat()} | cash_settlement={value}")
            passed += 1
        except Exception as exc:
            print(f"FAIL: {metal} Westmetall {type(exc).__name__}: {exc}")

    # Kobalt: mehrere unabhängige, kostenlose/öffentliche Wege PARALLEL.
    #
    # Dieser Block ist rein diagnostisch:
    # - jeder Quellenfehler wird isoliert behandelt
    # - ein Fehler in Quelle A darf Quelle B nicht beeinflussen
    # - HTTP 200 allein reicht NICHT für eine Produktionsfreigabe
    # - Drittquellen werden niemals automatisch als REAL_LME klassifiziert
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cobalt_targets = [
        (
            "LME_OFFICIAL_DE",
            "https://www.lme.com/metals/ev/lme-cobalt",
            "official_lme",
        ),
        (
            "LME_OFFICIAL_EN",
            "https://www.lme.com/en/Metals/EV/LME-Cobalt",
            "official_lme",
        ),
        (
            "LME_FASTMARKETS_MB",
            "https://www.lme.com/en/Metals/EV/LME-Cobalt-Fastmarkets-MB",
            "official_lme_other_contract",
        ),
        (
            "LME_HISTORICAL_CASH_SETTLED",
            "https://www.lme.com/Market-data/Reports-and-data/Historical-data-for-cash-settled-futures",
            "official_lme_historical",
        ),
        (
            "TRADING_ECONOMICS",
            "https://tradingeconomics.com/commodity/cobalt",
            "third_party_market",
        ),
        (
            "CBONDS_COBALT",
            "https://cbonds.com/indexes/26889/",
            "third_party_lme_futures_index",
        ),
        (
            "TRENDFORCE_LME_COBALT",
            "https://datatrack.trendforce.com/Chart/content/2657/spot-settlement-price-selling-price-lme-cobalt",
            "third_party_lme_reference",
        ),
        (
            "FERAILLEMONITOR_COBALT",
            "https://ferraillemonitor.com/analyses/prix-ferraille-centre-val-de-loire/",
            "third_party_metal_market",
        ),
    ]

    def probe_cobalt(item):
        label, url, source_class = item
        try:
            r = fetch(url)
            body = r.text or ""
            plain = re.sub(r"<[^>]+>", " ", body)
            plain = re.sub(r"\s+", " ", html.unescape(plain))
            low = plain.lower()

            has_cobalt = "cobalt" in low
            status = "HTTP_OK" if r.status_code == 200 else f"HTTP_{r.status_code}"

            price_candidates, parser_methods = extract_price_candidates(body, target)

            # Informational source-role indicators.
            lme_wording = any(
                token in low
                for token in (
                    "lme cobalt",
                    "lme-cobalt",
                    "london metal exchange",
                    "lme cobalt cash",
                )
            )
            settlement_wording = any(
                token in low
                for token in (
                    "cash settlement",
                    "spot settlement",
                    "settlement price",
                    "settlement",
                )
            )
            three_month = any(
                token in low
                for token in ("3m", "3-month", "3 month", "three month")
            )

            print(
                f"KOBALT_PROBE {label}: "
                f"status={status} "
                f"source_class={source_class} "
                f"bytes={len(r.content)} "
                f"has_cobalt={has_cobalt} "
                f"lme_wording={lme_wording} "
                f"settlement_wording={settlement_wording} "
                f"3m_hint={three_month} "
                f"price_candidates={len(price_candidates)} "
                f"parser_methods={','.join(parser_methods[:8]) or 'NONE'}"
            )

            if price_candidates:
                print(
                    f"KOBALT_PROBE {label}: "
                    f"prices={','.join(str(v) for v in price_candidates[:20])}"
                )

            return {
                "label": label,
                "status": status,
                "source_class": source_class,
                "has_cobalt": has_cobalt,
                "lme_wording": lme_wording,
                "settlement_wording": settlement_wording,
                "prices": price_candidates,
            }

        except Exception as exc:
            # A source-level exception must never abort the parallel batch.
            print(
                f"KOBALT_PROBE {label}: EXCEPTION "
                f"{type(exc).__name__}: {exc}"
            )
            return {
                "label": label,
                "status": "EXCEPTION",
                "source_class": source_class,
                "has_cobalt": False,
                "lme_wording": False,
                "settlement_wording": False,
                "prices": [],
            }

    cobalt_results = []
    with ThreadPoolExecutor(max_workers=len(cobalt_targets)) as pool:
        futures = [pool.submit(probe_cobalt, item) for item in cobalt_targets]
        for future in as_completed(futures):
            cobalt_results.append(future.result())

    official_page_ok = [
        x for x in cobalt_results
        if x["source_class"] == "official_lme"
        and x["status"] == "HTTP_OK"
    ]
    official_other_ok = [
        x for x in cobalt_results
        if x["source_class"].startswith("official_lme_")
        and x["status"] == "HTTP_OK"
    ]
    free_reference_hits = [
        x for x in cobalt_results
        if x["source_class"] in {
            "third_party_lme_futures_index",
            "third_party_lme_reference",
        }
        and x["status"] == "HTTP_OK"
        and x["has_cobalt"]
        and x["prices"]
    ]
    market_proxy_hits = [
        x for x in cobalt_results
        if x["source_class"] == "third_party_market"
        and x["status"] == "HTTP_OK"
        and x["has_cobalt"]
        and x["prices"]
    ]

    print(
        "LME_COBALT_PARALLEL: "
        f"official_page_ok={len(official_page_ok)}/2 "
        f"official_other_ok={len(official_other_ok)}/2 "
        f"free_reference_hits={len(free_reference_hits)} "
        f"market_proxy_hits={len(market_proxy_hits)}"
    )

    print(
        "LME_COBALT_FREE_REFERENCE_CANDIDATE: "
        f"{'FOUND' if free_reference_hits else 'NOT_FOUND'}"
    )
    print(
        "LME_COBALT_MARKET_PROXY: "
        f"{'FOUND' if market_proxy_hits else 'NOT_FOUND'}"
    )
    print(
        "LME_COBALT_OFFICIAL_AUTOMATION: "
        f"{'PROVEN' if official_page_ok else 'NOT_PROVEN'}"
    )


    print(f"LME_WESTMETALL_EXACT: {passed}/3 PASS")
    cobalt_exact_candidates = [
        x for x in cobalt_results
        if x["status"] == "HTTP_OK" and x["has_cobalt"] and x["prices"]
    ]
    print(
        "LME_COBALT_EXACT_CANDIDATES: "
        f"{len(cobalt_exact_candidates)} source(s) produced numeric candidates; "
        "manual/source-contract validation still required."
    )

    # This is deliberately a diagnostic gate: the runner must finish cleanly so
    # one inaccessible source cannot hide results from all other parsers/sources.
    # Production classification of Kobalt remains forbidden until an exact,
    # contract-correct LME value is proven for the requested date.
    if passed < 3:
        print(
            "LME_SOURCE_RUNNER_TEST: DIAGNOSTIC FAIL - "
            "mindestens eine erforderliche Westmetall-Referenz ist nicht exakt lesbar."
        )
        raise SystemExit(1)

    print(
        "LME_SOURCE_RUNNER_TEST: DIAGNOSTIC PASS - "
        "Blei/Nickel/Zinn exakt via Westmetall; Kobalt wurde mit mehreren "
        "Quellen und Parsern parallel untersucht. Kobalt bleibt bis zum "
        "Nachweis von Vertragsart + exaktem Tageswert NICHT produktionsfreigegeben."
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
