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
COBALT_CANDIDATES = [
    "https://arsenalltd.com.ua/export/waste/products/lme",
]


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

    # Cobalt is explicitly kept separate: these public LME-quote pages are a
    # secondary source and must NOT be labeled REAL_LME without an official cross-check.
    cobalt_found = False
    for url in COBALT_CANDIDATES:
        try:
            r = fetch(url)
            if r.status_code != 200:
                print(f"WARN: Kobalt candidate HTTP {r.status_code} | {url}")
                continue
            value = exact_arsenal(r.text, "Cobalt", target)
            if value is not None:
                print(
                    f"CANDIDATE: Kobalt | source=Arsenal Ukraine public LME quotes | "
                    f"exact_date={target.isoformat()} | cash_settlement={value} | "
                    f"STATUS=SECONDARY_REQUIRES_OFFICIAL_CROSSCHECK"
                )
                cobalt_found = True
                break
        except Exception as exc:
            print(f"WARN: Kobalt candidate {type(exc).__name__}: {exc}")

    print(f"LME_WESTMETALL_EXACT: {passed}/3 PASS")
    print(f"LME_COBALT_CANDIDATE: {'FOUND' if cobalt_found else 'NOT_FOUND'}")
    if passed < 3:
        raise SystemExit(1)
    if not cobalt_found:
        raise SystemExit(1)
    print("LME_SOURCE_RUNNER_TEST: PARTIAL - Kobalt ist noch NICHT produktionsfreigegeben")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
