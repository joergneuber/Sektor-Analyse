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


def test_cobalt_semantic_row_guard() -> None:
    """Regression: neighbouring Aluminum values must never become Cobalt."""
    fixture = """
    <table>
      <tr><td>Aluminum</td><td>09/09/2026</td><td>3362.05</td></tr>
      <tr><td>Cobalt</td><td>09/09/2026</td><td>44940</td></tr>
    </table>
    """
    values, methods = extract_price_candidates(fixture, dt.date(2026, 9, 9))
    if 3362.05 in values or 44940.0 not in values:
        raise AssertionError(
            f"Kobalt-Semantikschutz fehlgeschlagen: values={values} methods={methods}"
        )
    print("PASS: Kobalt-Semantikschutz | Nachbarwert Aluminium=3362.05 ausgeschlossen | Kobalt=44940.00 erkannt")


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
    """Multi-parser extraction with strict semantic context validation.

    A numeric token is NOT a valid cobalt candidate merely because it occurs on
    a cobalt/LME page. The parser requires a meaningful relationship to cobalt,
    LME, the target date, a price/settlement label, or a typed price field.
    This deliberately suppresses IDs, page numbers, dates and unrelated values.
    """
    visible = strip_html(body)
    low = visible.lower()
    decoded = html.unescape(body).replace(r"\/", "/").replace(r"\u002c", ",")
    markers = target_date_markers(target)

    candidates: list[float] = []
    methods: list[str] = []

    number_re = r"(?:\d{1,3}(?:[.,]\d{3})+|\d{4,6})(?:[.,]\d{1,2})?"

    def mask_dates(text: str) -> str:
        patterns = (
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{2}[./]\d{2}[./]\d{4}\b",
            r"\b\d{2}\s+[A-Za-zÄÖÜäöü]+\s+\d{4}\b",
            r"\b[A-Za-zÄÖÜäöü]+\s+\d{2},\s+\d{4}\b",
        )
        out = text
        for pattern in patterns:
            out = re.sub(pattern, lambda m: " " * len(m.group(0)), out, flags=re.I)
        return out

    def valid(value: float | None) -> bool:
        return value is not None and 1000 <= value <= 200000 and not (1900 <= value <= 2100)

    def add(raw: str, method: str, context: str = "") -> None:
        value = normalise_number(raw)
        if not valid(value):
            return
        ctx = context.lower()
        # Hard semantic guard: prevent generic page numbers/IDs from becoming
        # candidates unless a price/settlement or cobalt/LME relationship exists.
        semantic = (
            "cobalt" in ctx
            or "lme" in ctx
            or "settlement" in ctx
            or "official price" in ctx
            or "price" in ctx
            or "usd/t" in ctx
            or "usd per tonne" in ctx
            or "cash" in ctx
        )
        if not semantic:
            return
        if value not in candidates:
            candidates.append(value)
            methods.append(method)

    # 1) Exact target-date + cobalt-specific visible context.
    # If an HTML table exposes a Cobalt row for the target date, that row is
    # authoritative for this parser and the broader page-context parser is
    # skipped. This prevents neighbouring metals from being mixed into Cobalt.
    cobalt_row_found = False
    for row_html in re.findall(r"<tr\b[^>]*>.*?</tr>", body, flags=re.I | re.S):
        row_visible = strip_html(row_html)
        row_low = row_visible.lower()
        if "cobalt" not in row_low:
            continue
        if not any(marker.lower() in row_low for marker in markers):
            continue
        masked_row = mask_dates(row_visible)
        row_values = []
        for nm in re.finditer(number_re, masked_row):
            local_context = row_visible[max(0, nm.start() - 160): min(len(row_visible), nm.end() + 160)]
            value = normalise_number(nm.group(0))
            if valid(value):
                row_values.append((nm.group(0), local_context))
        if row_values:
            cobalt_row_found = True
            for raw, local_context in row_values:
                add(raw, "EXACT_DATE_COBALT_ROW", local_context)

    if not cobalt_row_found:
        # No explicit Cobalt table row: require the candidate itself to be
        # semantically close to the Cobalt label.
        for marker in markers:
            pos = 0
            while True:
                hit = low.find(marker.lower(), pos)
                if hit < 0:
                    break
                window_start = max(0, hit - 300)
                window_end = min(len(visible), hit + 700)
                window = visible[window_start:window_end]
                masked = mask_dates(window)
                for nm in re.finditer(number_re, masked):
                    number_start = window_start + nm.start()
                    number_end = window_start + nm.end()
                    local_context = visible[max(0, number_start - 220): min(len(visible), number_end + 220)]
                    if "cobalt" in local_context.lower():
                        add(nm.group(0), "EXACT_DATE_COBALT_CONTEXT", local_context)
                pos = hit + len(marker)

    # 2) Explicit labelled price/settlement, but only when cobalt/LME is nearby.
    for m in re.finditer(
        rf"(?is)(?:cobalt|lme).{{0,220}}?(?:cash|spot|official|settlement|price)"
        rf".{{0,120}}?({number_re})",
        visible,
    ):
        add(m.group(1), "COBALT_LABELLED_PRICE", m.group(0))
    for m in re.finditer(
        rf"(?is)(?:cash|spot|official|settlement|price).{{0,120}}?"
        rf"({number_re}).{{0,220}}?(?:cobalt|lme)",
        visible,
    ):
        add(m.group(1), "LABELLED_PRICE_COBALT_CONTEXT", m.group(0))

    # 3) HTML data-* attributes, only when the surrounding tag/context names
    # cobalt, LME, settlement or price explicitly.
    for m in re.finditer(
        rf"(?is)<[^>]*(?:cobalt|lme|settlement|price)[^>]*"
        rf"(?:data-(?:value|price|settlement|last|close)|(?:value|price|settlement|last|close))"
        rf"\s*=\s*[\"']({number_re})[\"'][^>]*>",
        body,
    ):
        add(m.group(1), "HTML_TYPED_ATTRIBUTE", m.group(0))

    # 4) Typed JSON/JavaScript fields with cobalt/LME in the same bounded object.
    typed_pattern = rf"(?is)(?:[\"']?(?:price|settlementPrice|cashSettlement|officialPrice|lastPrice)[\"']?)"
    for m in re.finditer(
        typed_pattern + rf"\s*:\s*[\"']?({number_re})[\"']?",
        decoded,
    ):
        context = decoded[max(0, m.start() - 450): m.end() + 450]
        if re.search(r"cobalt|lme|settlement|cash", context, re.I):
            add(m.group(1), "SCRIPT_TYPED_PRICE", context)

    # 5) JSON-LD/meta values with explicit cobalt/LME/price context.
    for m in re.finditer(
        rf"(?is)(?:meta|script)[^>]*(?:cobalt|lme|price|settlement)[^>]*"
        rf"(?:content|value)\s*=\s*[\"']({number_re})[\"']",
        body,
    ):
        add(m.group(1), "META_JSONLD_PRICE", m.group(0))

    # 6) HTML table rows: exact target date AND cobalt/LME/settlement in the
    # same row. This is much safer than scanning arbitrary table numbers.
    for row in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", body):
        row_text = strip_html(row)
        row_low = row_text.lower()
        if not any(x in row_low for x in ("cobalt", "lme", "settlement", "price")):
            continue
        if not any(marker.lower() in row_low for marker in markers):
            continue
        for raw in re.findall(number_re, mask_dates(row_text)):
            add(raw, "EXACT_DATE_TABLE_ROW", row_text)

    # 7) Query/encoded fields: price-like key + cobalt/LME in nearby payload.
    if not cobalt_row_found:
        for m in re.finditer(
            rf"(?is)(?:price|settlement|cash|official|last|close)[^&=]{{0,40}}="
            rf"({number_re})",
            decoded,
        ):
            context = decoded[max(0, m.start() - 400): m.end() + 400]
            if re.search(r"cobalt|lme|settlement|cash", context, re.I):
                add(m.group(1), "ENCODED_TYPED_PRICE", context)

    # 8) Date/value tuple in scripts. The target date must be adjacent to a
    # price-like label OR the tuple must sit in an explicit cobalt/LME context.
    if not cobalt_row_found:
        for marker in markers:
            for m in re.finditer(re.escape(marker), decoded, re.I):
                chunk = decoded[max(0, m.start() - 180): m.end() + 450]
                if not re.search(r"cobalt|lme|price|settlement|cash", chunk, re.I):
                    continue
                for raw in re.findall(number_re, mask_dates(chunk)):
                    add(raw, "DATE_PRICE_TUPLE", chunk)

    # 9) Contract-specific parser. CO must be a real token, and cobalt/LME or
    # physical/cash-settled wording must occur nearby. Bare substring 'co' is
    # deliberately forbidden because it creates huge false-positive counts.
    if not cobalt_row_found:
        for m in re.finditer(r"\bCO\b", visible, re.I):
            chunk = visible[max(0, m.start() - 350): m.end() + 650]
            if not re.search(r"cobalt|lme|physically settled|cash-settled|usd\s*/?\s*t", chunk, re.I):
                continue
            for raw in re.findall(number_re, mask_dates(chunk)):
                add(raw, "CONTRACT_CO_CONTEXT", chunk)

    # 10) USD/t parser. A value is accepted only when USD/t and cobalt/LME are
    # part of the same bounded context. This is the strongest generic fallback.
    if not cobalt_row_found:
        for m in re.finditer(r"(?is)(?:usd\s*/\s*t|usd\s+per\s+tonne|us\$\s*/\s*t)", visible):
            chunk = visible[max(0, m.start() - 300): m.end() + 500]
            if not re.search(r"cobalt|lme", chunk, re.I):
                continue
            for raw in re.findall(number_re, mask_dates(chunk)):
                add(raw, "USD_T_CONTEXT", chunk)

    return candidates, methods


def candidate_evidence(body: str, value: float, target: dt.date) -> dict:
    """Classify one numeric candidate by explicit evidence, not page proximity alone."""
    visible = strip_html(body)
    value_forms = {
        f"{value:g}",
        f"{value:,.0f}",
        f"{value:,.2f}",
        f"{value:.0f}",
        f"{value:.2f}",
    }
    positions = []
    low = visible.lower()
    for form in value_forms:
        start = 0
        while True:
            pos = low.find(form.lower(), start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + max(1, len(form))
    if not positions:
        return {
            "value": value, "date_exact": False, "contract": None,
            "price_type": None, "unit": None, "source_context": "",
            "confidence": "LOW", "evidence": "VALUE_NOT_LOCATED"
        }

    # Evaluate the occurrence with the strongest explicit semantic context.
    best = None
    for pos in positions:
        chunk = visible[max(0, pos-500):pos+500]
        lowc = chunk.lower()
        date_exact = any(m.lower() in lowc for m in target_date_markers(target))
        contract = "CO" if re.search(r"\bCO\b", chunk, re.I) else None
        if not contract and re.search(r"physically settled", chunk, re.I):
            contract = "CO?"
        if re.search(r"fastmarkets|cash-settled", chunk, re.I):
            contract = "CB" if re.search(r"fastmarkets", chunk, re.I) else contract
        price_type = None
        if re.search(r"official\s+(?:price|settlement)|official price", chunk, re.I):
            price_type = "OFFICIAL"
        elif re.search(r"cash\s+(?:settlement|price)|cash", chunk, re.I):
            price_type = "CASH"
        elif re.search(r"3\s*-?\s*month|3m", chunk, re.I):
            price_type = "3M"
        elif re.search(r"spot\s+settlement|spot", chunk, re.I):
            price_type = "SPOT"
        unit = None
        if re.search(r"USD\s*/\s*T|US\$\s*/\s*T|USD\s+per\s+tonne|USD/T", chunk, re.I):
            unit = "USD/t"
        score = sum((
            5 if date_exact else 0,
            4 if contract == "CO" else 0,
            3 if price_type in {"OFFICIAL", "CASH", "SPOT", "3M"} else 0,
            2 if unit == "USD/t" else 0,
            2 if re.search(r"cobalt", chunk, re.I) else 0,
            1 if re.search(r"london metal exchange|\bLME\b", chunk, re.I) else 0,
        ))
        if best is None or score > best[0]:
            best = (score, date_exact, contract, price_type, unit, chunk)

    score, date_exact, contract, price_type, unit, chunk = best
    if score >= 12 and date_exact and contract == "CO" and unit == "USD/t":
        confidence = "HIGH"
    elif score >= 8 and date_exact and (contract == "CO" or price_type in {"OFFICIAL", "CASH", "SPOT", "3M"}):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    return {
        "value": value,
        "date_exact": date_exact,
        "contract": contract,
        "price_type": price_type,
        "unit": unit,
        "source_context": re.sub(r"\s+", " ", chunk).strip()[:900],
        "confidence": confidence,
        "evidence": f"score={score}",
    }

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




def test_cobalt_multi_variant_fixtures() -> None:
    """All cobalt parser variants run in one deterministic regression batch."""
    target = dt.date(2026, 9, 9)
    lme_fixture = """
    <html><body>
      <h1>LME Cobalt</h1>
      <div>Contract code: CO</div><div>Price quotation: US$ per tonne</div>
      <div>Trading SUMMARY Prices in US$ Data valid for 09 Sep 2026 10 Sep 2026</div>
    </body></html>
    """
    metalsmarket_fixture = """
    <html><body>
      <h1>LME CASH SETTLEMENTS</h1>
      <div>CASH | USD | 09/09/2026</div>
      <table>
        <tr><th>METAL</th><th>BID</th><th>ASK</th></tr>
        <tr><td>ALUM</td><td>3351.50</td><td>3352.00</td></tr>
        <tr><td>COBALT</td><td>42750.00</td><td>43250.00</td></tr>
      </table>
    </body></html>
    """
    cbonds_fixture = """
    <html><body>
      <div>Cobalt futures contract ... physically settled ... cobalt (CO)</div>
      <table>
        <tr><td>Aluminum</td><td>3,362.05 USD/T</td><td>09/09/2026</td></tr>
        <tr><td>Cobalt</td><td>44,940 USD/T</td><td>08/09/2026</td></tr>
      </table>
    </body></html>
    """
    # Variant A: direct LME contract/date identity. No price is invented when
    # the public page exposes only the dynamic widget shell.
    lme_candidates, _ = extract_price_candidates(lme_fixture, target)
    if lme_candidates:
        raise AssertionError(f"LME shell parser must not invent a price: {lme_candidates}")
    if not re.search(r"\bCO\b", strip_html(lme_fixture)):
        raise AssertionError("LME contract CO fixture not recognized")

    # Variant B: exact Cobalt row from an LME cash-settlement reference page.
    mm = extract_metalsmarket_cash_settlement(metalsmarket_fixture, target)
    if mm != {"bid": 42750.0, "ask": 43250.0, "mid": 43000.0}:
        raise AssertionError(f"MetalsMarket parser regression failed: {mm}")

    # Variant C: exact Cobalt row from CBONDS. The adjacent Aluminum value must
    # never be assigned to Cobalt, and a one-day date mismatch must be visible.
    cb = extract_cbonds_cobalt(cbonds_fixture, target)
    if cb["values"] != []:
        raise AssertionError(f"CBONDS target-date parser accepted stale row: {cb}")
    cb_prev = extract_cbonds_cobalt(cbonds_fixture, dt.date(2026, 9, 8))
    if cb_prev["values"] != [44940.0] or cb_prev["contract"] != "CO":
        raise AssertionError(f"CBONDS Cobalt CO regression failed: {cb_prev}")

    print("PASS: Kobalt-Multi-Variant-Fixtures | LME shell + MetalsMarket bid/ask + CBONDS CO/date guard")


def extract_metalsmarket_cash_settlement(body: str, target: dt.date) -> dict[str, float | str | None]:
    """Parse the exact-date USD cash-settlement table without cross-row leakage."""
    visible = strip_html(body)
    target_markers = {target.strftime("%d/%m/%Y"), target.strftime("%m/%d/%Y"), target.isoformat()}
    if not any(m in visible for m in target_markers):
        return {"bid": None, "ask": None, "mid": None}
    for row in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", body):
        row_text = strip_html(row)
        if not re.search(r"\bCOBALT\b", row_text, re.I):
            continue
        nums = [normalise_number(x) for x in re.findall(r"\d[\d,.]*", row_text)]
        nums = [x for x in nums if x is not None and 1000 <= x <= 200000]
        if len(nums) >= 2:
            return {"bid": nums[0], "ask": nums[1], "mid": (nums[0] + nums[1]) / 2.0}
    return {"bid": None, "ask": None, "mid": None}


def extract_cbonds_cobalt(body: str, target: dt.date) -> dict[str, object]:
    """Extract only a same-row Cobalt value and preserve the CO contract context."""
    for row in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", body):
        row_text = strip_html(row)
        if not re.search(r"\bCobalt\b", row_text, re.I):
            continue
        if not any(m.lower() in row_text.lower() for m in target_date_markers(target)):
            continue
        values = []
        for raw in re.findall(r"\d[\d,.]*", row_text):
            value = normalise_number(raw)
            if value is not None and 1000 <= value <= 200000:
                values.append(value)
        # Never accept a neighboring Aluminum row or a page-wide value.
        return {"values": values[:1], "contract": "CO" if re.search(r"cobalt.*\(co\)|\(co\).*cobalt", strip_html(body), re.I) else None}
    return {"values": [], "contract": "CO" if re.search(r"cobalt.*\(co\)|\(co\).*cobalt", strip_html(body), re.I) else None}


def select_cobalt_price(source_records: list[dict]) -> dict:
    """Select a price without hiding disagreement; official beats secondary."""
    official = [r for r in source_records if r.get("quality") == "REAL_OFFICIAL" and r.get("price") is not None]
    if official:
        return {"price": official[0]["price"], "status": "REAL_OFFICIAL", "source": official[0]["source"]}
    validated = [r for r in source_records if r.get("quality") == "VALIDATED_SECONDARY" and r.get("price") is not None]
    if validated:
        return {"price": validated[0]["price"], "status": "VALIDATED_SECONDARY", "source": validated[0]["source"]}
    available = [r for r in source_records if r.get("price") is not None]
    if available:
        # Best available value is deliberately exposed together with its degraded
        # quality; it is never silently promoted to an official LME value.
        best = sorted(available, key=lambda r: r.get("score", 0), reverse=True)[0]
        return {"price": best["price"], "status": "SECONDARY_UNCONFIRMED", "source": best["source"]}
    return {"price": None, "status": "UNVERIFIED", "source": None}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-date", help="YYYY-MM-DD; default = letzter abgeschlossener Werktag")
    args = parser.parse_args()
    target = dt.date.fromisoformat(args.target_date) if args.target_date else target_default()

    print("LME SOURCE RUNNER TEST")
    print(f"Runner Python: {sys.version.split()[0]}")
    print(f"target_date={target.isoformat()}")

    test_cobalt_semantic_row_guard()
    test_cobalt_multi_variant_fixtures()

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
            "METALSMARKET_CASH_USD",
            "https://www.metalsmarket.net/w_lmeCashSett.html",
            "secondary_lme_cash_reference",
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

            # Source-specific Variant: MetalsMarket exposes an exact-date USD
            # cash table with separate Cobalt BID/ASK. Use the same-row parser
            # and derive the midpoint only as a clearly labelled secondary
            # reference; never call it an official LME settlement automatically.
            metalsmarket = extract_metalsmarket_cash_settlement(body, target)
            if label == "METALSMARKET_CASH_USD" and metalsmarket.get("mid") is not None:
                price_candidates = [float(metalsmarket["mid"])]
                parser_methods = ["METALSMARKET_EXACT_COBALT_BID_ASK_MID"]

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

            evidence = [candidate_evidence(body, v, target) for v in price_candidates]
            strong = [e for e in evidence if e["confidence"] in {"HIGH", "MEDIUM"} and e["date_exact"]]

            if evidence:
                for e in evidence[:20]:
                    print(
                        f"KOBALT_EVIDENCE {label}: value={e['value']} "
                        f"date_exact={e['date_exact']} contract={e['contract'] or 'NONE'} "
                        f"price_type={e['price_type'] or 'NONE'} unit={e['unit'] or 'NONE'} "
                        f"confidence={e['confidence']} {e['evidence']}"
                    )

            return {
                "label": label,
                "status": status,
                "source_class": source_class,
                "has_cobalt": has_cobalt,
                "lme_wording": lme_wording,
                "settlement_wording": settlement_wording,
                "prices": price_candidates,
                "evidence": evidence,
                "strong_evidence": strong,
                "metalsmarket": metalsmarket,
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
                "evidence": [],
                "strong_evidence": [],
                "metalsmarket": {"bid": None, "ask": None, "mid": None},
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

    # One-run price result: expose the best exact-date price we can prove,
    # while preserving provenance/quality. Official LME always wins; otherwise
    # a same-day secondary cash reference is usable but explicitly degraded.
    price_records = []
    for source in cobalt_results:
        if source.get("label") == "METALSMARKET_CASH_USD" and source.get("metalsmarket", {}).get("mid") is not None:
            price_records.append({
                "source": source["label"],
                "price": source["metalsmarket"]["mid"],
                "quality": "SECONDARY_UNCONFIRMED",
                "score": 90,
            })
        for e in source.get("strong_evidence", []):
            if e.get("contract") == "CO" and e.get("unit") == "USD/t":
                price_records.append({
                    "source": source["label"],
                    "price": e["value"],
                    "quality": "REAL_OFFICIAL" if source["source_class"].startswith("official_lme") else "VALIDATED_SECONDARY",
                    "score": 100 if source["source_class"].startswith("official_lme") else 70,
                })
    cobalt_price = select_cobalt_price(price_records)
    print(
        "LME_COBALT_BEST_AVAILABLE: "
        f"price={cobalt_price['price'] if cobalt_price['price'] is not None else 'NONE'} "
        f"status={cobalt_price['status']} "
        f"source={cobalt_price['source'] or 'NONE'}"
    )

    print(f"LME_WESTMETALL_EXACT: {passed}/{len(WESTMETALL)} PASS")
    cobalt_exact_candidates = [
        x for x in cobalt_results
        if x["status"] == "HTTP_OK" and x["has_cobalt"] and x["prices"]
    ]
    cobalt_strong_candidates = [
        x for x in cobalt_results
        if x.get("strong_evidence")
    ]
    print(
        "LME_COBALT_NUMERIC_CANDIDATES: "
        f"{len(cobalt_exact_candidates)} source(s) produced numeric candidates."
    )
    print(
        "LME_COBALT_STRONG_EVIDENCE: "
        f"{len(cobalt_strong_candidates)} source(s) produced target-date + contract/price evidence."
    )
    if cobalt_strong_candidates:
        for source in cobalt_strong_candidates:
            for e in source["strong_evidence"][:5]:
                print(
                    "LME_COBALT_STRONG: "
                    f"source={source['label']} value={e['value']} "
                    f"date={e['date_exact']} contract={e['contract']} "
                    f"price_type={e['price_type']} unit={e['unit']} "
                    f"confidence={e['confidence']}"
                )

    # This is deliberately a diagnostic gate: the runner must finish cleanly so
    # one inaccessible source cannot hide results from all other parsers/sources.
    # Production classification of Kobalt remains forbidden until an exact,
    # contract-correct LME value is proven for the requested date.
    if passed < len(WESTMETALL):
        print(
            "LME_SOURCE_RUNNER_TEST: DIAGNOSTIC FAIL - "
            "mindestens eine der konfigurierten Westmetall-Referenzen ist nicht exakt lesbar."
        )
        raise SystemExit(1)

    print(
        "LME_SOURCE_RUNNER_TEST: DIAGNOSTIC PASS - "
        "Blei/Nickel/Zinn exakt via Westmetall; Kobalt-Multi-Variant-Runner "
        "absolviert alle Parserpfade in einem Lauf. Preis, Quelle, Datum, "
        "Kontrakt und Qualitaetsstatus werden getrennt ausgewiesen."
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
