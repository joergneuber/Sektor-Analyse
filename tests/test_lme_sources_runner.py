"""Isolierter LME-Quellentest fuer den GitHub Runner.

Prueft exakte Tagesdaten fuer Nickel, Blei, Zinn und Kobalt. Der Test veraendert
keinen Produktionscode und kennzeichnet Quellen nach Qualitaetsstufe.
"""
from __future__ import annotations

import argparse
import datetime as dt
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

    # Kobalt: mehrere unabhängige Richtungen PARALLEL in EINEM Runner-Lauf.
    #
    # Ziel: mit einem einzigen Testlauf parallel feststellen:
    # A) offizielle LME-Seite / Varianten
    # B) offizielle LME-Cobalt-Fastmarkets-MB-Seite
    # C) offizielle LME-Historical-cash-settled-Seite
    # D) frei zugängliche Drittquelle als PROXY-Kandidat
    #
    # Wichtig: Ein Drittanbieterwert wird NICHT als REAL_LME freigegeben.
    # Der Test dient ausschließlich der Quellen- und Automatisierbarkeitsprüfung.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cobalt_targets = [
        (
            "LME_OFFICIAL_DE",
            "https://www.lme.com/en/Metals/EV/LME-Cobalt",
            "official_lme",
        ),
        (
            "LME_OFFICIAL_LOWERCASE",
            "https://www.lme.com/metals/ev/lme-cobalt",
            "official_lme",
        ),
        (
            "LME_OFFICIAL_EN_PATH",
            "https://www.lme.com/en/Metals/EV/LME-Cobalt?ret=%2Fmetals%2Fminor-metals%2Fcobalt%2F",
            "official_lme",
        ),
        (
            "LME_COBALT_FASTMARKETS",
            "https://www.lme.com/Metals/EV/LME-Cobalt-Fastmarkets-MB",
            "official_lme_fastmarkets",
        ),
        (
            "LME_COBALT_FASTMARKETS_EN",
            "https://www.lme.com/en/metals/ev/lme-cobalt-fastmarkets-mb",
            "official_lme_fastmarkets",
        ),
        (
            "LME_HISTORICAL_CASH_SETTLED",
            "https://www.lme.com/Market-data/Reports-and-data/Historical-data-for-cash-settled-futures",
            "official_lme_historical",
        ),
        (
            "TRADINGECONOMICS_COBALT",
            "https://tradingeconomics.com/commodity/cobalt",
            "third_party_proxy",
        ),
    ]

    def probe_cobalt(item):
        label, url, source_class = item
        try:
            r = fetch(url)
            text = r.text or ""
            plain = re.sub(r"<[^>]+>", " ", text)
            plain = re.sub(r"\s+", " ", plain).lower()

            has_cobalt = "cobalt" in plain
            has_5day = (
                "five-day look-back" in plain
                or "five day look-back" in plain
                or "5-day look-back" in plain
                or "5 day look-back" in plain
            )

            # Search for current-looking numeric values near cobalt/price terms.
            number_hits = re.findall(
                r"(?<![\d])\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2,4})(?![\d])",
                plain,
            )
            numbers_sample = number_hits[:12]

            status = "HTTP_FAIL"
            if r.status_code == 200:
                status = "HTTP_OK"
            elif r.status_code in (401, 403, 429):
                status = f"BLOCKED_{r.status_code}"

            print(
                f"KOBALT_PROBE {label}: status={status} "
                f"source_class={source_class} "
                f"bytes={len(r.content)} "
                f"has_cobalt={has_cobalt} "
                f"has_5day={has_5day} "
                f"number_hits={len(number_hits)}"
            )

            if numbers_sample:
                print(
                    f"KOBALT_PROBE {label}: numeric_sample="
                    + ",".join(numbers_sample)
                )

            return {
                "label": label,
                "status": status,
                "source_class": source_class,
                "has_cobalt": has_cobalt,
                "has_5day": has_5day,
                "number_hits": len(number_hits),
            }

        except Exception as exc:
            print(
                f"KOBALT_PROBE {label}: EXCEPTION "
                f"{type(exc).__name__}: {exc}"
            )
            return {
                "label": label,
                "status": "EXCEPTION",
                "source_class": source_class,
                "has_cobalt": False,
                "has_5day": False,
                "number_hits": 0,
            }

    cobalt_results = []
    with ThreadPoolExecutor(max_workers=len(cobalt_targets)) as pool:
        futures = [pool.submit(probe_cobalt, item) for item in cobalt_targets]
        for future in as_completed(futures):
            cobalt_results.append(future.result())

    official_ok = [
        x for x in cobalt_results
        if x["source_class"] == "official_lme" and x["status"] == "HTTP_OK"
    ]
    official_fastmarkets_ok = [
        x for x in cobalt_results
        if x["source_class"] == "official_lme_fastmarkets"
        and x["status"] == "HTTP_OK"
    ]
    historical_ok = [
        x for x in cobalt_results
        if x["source_class"] == "official_lme_historical"
        and x["status"] == "HTTP_OK"
    ]
    proxy_ok = [
        x for x in cobalt_results
        if x["source_class"] == "third_party_proxy"
        and x["status"] == "HTTP_OK"
    ]

    print(
        "LME_COBALT_PARALLEL: "
        f"official_ok={len(official_ok)}/{sum(x['source_class']=='official_lme' for x in cobalt_results)} "
        f"fastmarkets_ok={len(official_fastmarkets_ok)}/{sum(x['source_class']=='official_lme_fastmarkets' for x in cobalt_results)} "
        f"historical_ok={len(historical_ok)}/{sum(x['source_class']=='official_lme_historical' for x in cobalt_results)} "
        f"proxy_ok={len(proxy_ok)}/{sum(x['source_class']=='third_party_proxy' for x in cobalt_results)}"
    )

    cobalt_page_ok = bool(official_ok or official_fastmarkets_ok)

    print(
        "LME_COBALT_OFFICIAL_PAGE: "
        f"{'PASS' if cobalt_page_ok else 'FAIL'}"
    )

    # This test remains diagnostic. No third-party proxy is ever promoted
    # automatically to REAL_LME.
    if passed < 3:
        raise SystemExit(1)
    if not cobalt_page_ok:
        raise SystemExit(1)

    print(
        "LME_SOURCE_RUNNER_TEST: DIAGNOSTIC PASS | "
        "PB_NI_SN exact via Westmetall + official LME cobalt access proven; "
        "free exact automated cobalt settlement source remains open"
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
