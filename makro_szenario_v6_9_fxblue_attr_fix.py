#!/usr/bin/env python3
"""
Isolierter GitHub-Test:
ISM Services Official – Prüfung des offiziellen ISM-Reports.

Zweck:
- NUR die offizielle ISM-Services-Seite testen.
- Keine Änderung an Cache, Gate, LME, FRED oder Manufacturing.
- Für den Zielmonat werden genau vier Actual-Werte verlangt:
  PMI, New Orders, Employment, Prices.
- Fail-closed: Forecast/Previous/N/A/fehlende Werte dürfen nicht als Actual
  verwendet werden.

Exit codes:
  0 = GREEN: offizieller Report gefunden und alle 4 Actuals valide
  1 = RED: kein vollständiger Datensatz
"""

import re
import sys
import calendar
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


TARGET_YEAR = 2026
TARGET_MONTH = 7

BASE_URL = "https://www.ismworld.org"
TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def month_names(month: int) -> tuple[str, str]:
    return calendar.month_name[month].lower(), calendar.month_abbr[month].lower()


def clean_actual(value):
    """Fail-closed: nur ein expliziter numerischer Actual-Wert ist gültig."""
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "null", "none", "-", "--"}:
        return None

    # Keine Zahl aus einem beliebigen Forecast/Previous-String herausziehen.
    # Zulässig sind nur reine numerische Werte mit optionalem Vorzeichen
    # und optionalem Prozentzeichen.
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?\s*%?", text):
        return None

    text = text.replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def find_actual_in_table(table):
    """
    Sucht explizit nach einer Actual-Spalte.
    Niemals Position 2 verwenden, wenn die Spaltenüberschrift nicht
    eindeutig Actual ist.
    """
    rows = table.find_all("tr")
    if not rows:
        return None

    headers = []
    for cell in rows[0].find_all(["th", "td"]):
        headers.append(cell.get_text(" ", strip=True).lower())

    actual_index = None
    for i, header in enumerate(headers):
        if re.search(r"\bactual\b", header):
            actual_index = i
            break

    if actual_index is None:
        return None

    if len(rows) < 2:
        return None

    values = rows[1].find_all(["td", "th"])
    if actual_index >= len(values):
        return None

    return clean_actual(values[actual_index].get_text(" ", strip=True))


def extract_metric_from_text(text, patterns):
    """
    Nur für explizite 'Metric: value'-Darstellungen.
    Kein Herauspicken beliebiger Zahlen aus dem Umfeld.
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return clean_actual(match.group(1))
    return None


def parse_services(html):
    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(" ", strip=True)

    result = {
        "pmi": None,
        "new_orders": None,
        "employment": None,
        "prices": None,
    }

    # Diagnose: Tabellenstruktur
    tables = soup.find_all("table")
    print(f"TABLE_COUNT={len(tables)}")

    for idx, table in enumerate(tables, 1):
        value = find_actual_in_table(table)
        if value is not None:
            print(f"TABLE_{idx}_EXPLICIT_ACTUAL={value}")

    # Offizielle ISM-Seiten verwenden je nach Layout unterschiedliche
    # Text-/HTML-Strukturen. Die Muster bleiben bewusst explizit.
    patterns = {
        "pmi": [
            r"(?:Services\s+PMI|PMI)\s*[:\-]\s*([+-]?\d+(?:\.\d+)?)",
            r"(?:Services\s+PMI)\s+([+-]?\d+(?:\.\d+)?)",
        ],
        "new_orders": [
            r"(?:New\s+Orders)\s*[:\-]\s*([+-]?\d+(?:\.\d+)?)",
        ],
        "employment": [
            r"(?:Employment)\s*[:\-]\s*([+-]?\d+(?:\.\d+)?)",
        ],
        "prices": [
            r"(?:Prices(?:\s+Paid)?)\s*[:\-]\s*([+-]?\d+(?:\.\d+)?)",
        ],
    }

    for key, key_patterns in patterns.items():
        result[key] = extract_metric_from_text(full_text, key_patterns)

    # Zusätzliche Diagnose: relevante Textstellen ausgeben, aber keine
    # beliebigen numerischen Tokens als Actual interpretieren.
    print("RELEVANT_TEXT_LINES=")
    seen = set()
    for element in soup.find_all(["tr", "p", "li", "div"]):
        text = element.get_text(" ", strip=True)
        low = text.lower()
        if any(
            token in low
            for token in (
                "services pmi",
                "new orders",
                "employment",
                "prices paid",
            )
        ):
            if text and text not in seen:
                seen.add(text)
                print(f"  {text[:500]}")

    return result


def candidate_urls(year, month):
    long_name, short_name = month_names(month)
    # Offizielles ISM-Muster; Varianten werden diagnostisch getestet.
    return [
        f"{BASE_URL}/supply-management-news-and-reports/reports/ism-pmi-reports/services/{long_name}/",
        f"{BASE_URL}/supply-management-news-and-reports/reports/ism-pmi-reports/services/{short_name}/",
        f"{BASE_URL}/supply-management-news-and-reports/reports/ism-pmi-reports/services/{year}/{long_name}/",
    ]


def main():
    target = date(TARGET_YEAR, TARGET_MONTH, 1)
    long_name, short_name = month_names(TARGET_MONTH)

    print("=== ISM OFFICIAL SERVICES DIAGNOSE ===")
    print(f"TARGET={target.isoformat()}")
    print(f"TARGET_MONTH_LONG={long_name}")
    print(f"TARGET_MONTH_SHORT={short_name}")
    print()

    session = requests.Session()
    session.headers.update(HEADERS)

    successful_pages = []

    for url in candidate_urls(TARGET_YEAR, TARGET_MONTH):
        print(f"REQUEST={url}")
        try:
            response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            print(f"HTTP_STATUS={response.status_code}")
            print(f"FINAL_URL={response.url}")
            print(f"CONTENT_LENGTH={len(response.text)}")

            if response.history:
                print(
                    "REDIRECT_CHAIN="
                    + " -> ".join(
                        [str(r.status_code) for r in response.history]
                        + [str(response.status_code)]
                    )
                )

            if response.status_code == 200:
                successful_pages.append((url, response.text))
            print()
        except requests.RequestException as exc:
            print(f"REQUEST_ERROR={type(exc).__name__}: {exc}")
            print()

    if not successful_pages:
        print("RESULT=RED_NO_OFFICIAL_SERVICES_PAGE")
        return 1

    # Zuerst die Seiten mit dem stärksten Monatsbezug prüfen.
    selected = None
    for requested_url, html in successful_pages:
        low_url = requested_url.lower()
        if long_name in low_url or short_name in low_url:
            selected = (requested_url, html)
            break

    if selected is None:
        selected = successful_pages[0]

    requested_url, html = selected
    print(f"SELECTED_URL={requested_url}")
    print()

    # SSO/Paywall explizit diagnostizieren.
    low_html = html.lower()
    final_low = requested_url.lower()

    sso_markers = (
        "sso/login.aspx",
        "login.aspx",
        "sign in",
        "log in",
        "authentication",
    )
    sso_detected = any(marker in final_low or marker in low_html for marker in sso_markers)
    print(f"SSO_MARKER_DETECTED={sso_detected}")

    # Report-Monat nicht nur anhand der URL akzeptieren.
    month_evidence = (
        f"{long_name} {TARGET_YEAR}",
        f"{short_name} {TARGET_YEAR}",
        f"{long_name.capitalize()} {TARGET_YEAR}",
    )
    report_month_detected = any(item.lower() in low_html for item in month_evidence)
    print(f"REPORT_MONTH_TEXT_DETECTED={report_month_detected}")
    print()

    data = parse_services(html)

    print()
    print("=== EXTRACTED ACTUALS ===")
    for key in ("pmi", "new_orders", "employment", "prices"):
        print(f"{key.upper()}={data[key]}")

    complete = all(data[key] is not None for key in data)

    print()
    if sso_detected:
        print("RESULT=RED_OFFICIAL_SSO")
        return 1

    if not report_month_detected:
        print("RESULT=RED_OFFICIAL_MONTH_NOT_PROVEN")
        return 1

    if not complete:
        missing = [key for key, value in data.items() if value is None]
        print(f"MISSING={','.join(missing)}")
        print("RESULT=RED_OFFICIAL_SERVICES_INCOMPLETE")
        return 1

    print("RESULT=GREEN_OFFICIAL_SERVICES_COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
