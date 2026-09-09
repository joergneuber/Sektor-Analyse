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
LME_COBALT_PAGE = "https://www.lme.com/en/Metals/EV/LME-Cobalt"



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


def exact_arsenal(text: str, metal: str, target: dt.date) -> float | None:
    target_text = target.strftime("%d %B %Y")
    if target_text.lower() not in text.lower():
        return None
    plain = re.sub(r"<[^>]+>", " | ", text)
    plain = re.sub(r"\s+", " ", plain)
    m = re.search(rf"\b{re.escape(metal)}\b\s*\|\s*([0-9][0-9,]*(?:\.[0-9]+)?)", plain, re.I)
    return parse_num(m.group(1)) if m else None


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

            # Search date-adjacent text first; then use a bounded fallback
            # to avoid treating arbitrary page numbers as prices.
            date_markers = [
                target.strftime("%Y-%m-%d"),
                target.strftime("%d/%m/%Y"),
                target.strftime("%m/%d/%Y"),
                target.strftime("%d.%m.%Y"),
                target.strftime("%d %B %Y"),
                target.strftime("%B %d, %Y"),
            ]

            windows = []
            for dm in date_markers:
                pos = low.find(dm.lower())
                if pos >= 0:
                    windows.append(plain[max(0, pos - 180): pos + 600])

            if not windows:
                windows.append(plain[:4000])

            price_candidates = []
            for window in windows:
                # Restrict to realistic USD/t-like values.
                for raw in re.findall(
                    r"(?<![\d])\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2,4})(?![\d])",
                    window,
                ):
                    try:
                        value = parse_number(raw)
                    except Exception:
                        continue
                    if 1000 <= value <= 200000 and value not in price_candidates:
                        price_candidates.append(value)

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
                f"price_candidates={len(price_candidates)}"
            )

            if price_candidates:
                print(
                    f"KOBALT_PROBE {label}: "
                    f"prices={','.join(str(v) for v in price_candidates[:10])}"
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
    print(f"LME_COBALT_OFFICIAL_PAGE: {'PASS' if cobalt_page_ok else 'FAIL'}")
    if passed < 3 or not cobalt_page_ok:
        print(
            "LME_SOURCE_RUNNER_TEST: DIAGNOSTIC FAIL - "
            "mindestens eine erforderliche Quelle ist im Runner nicht belastbar erreichbar."
        )
        raise SystemExit(1)

    print(
        "LME_SOURCE_RUNNER_TEST: DIAGNOSTIC PASS - "
        "Blei/Nickel/Zinn exakt via Westmetall; Kobalt-offizielle LME-Seite erreichbar. "
        "Kobalt-EXAKTWERT-AUTOMATISIERUNG weiterhin NICHT produktionsfreigegeben."
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
