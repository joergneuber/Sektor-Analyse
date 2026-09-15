import re
import sys
import urllib.request
import json

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Controlled semantic mapping: aliases are deliberately explicit.
RULES = {
    "ADP": {
        "aliases": ["adp employment", "adp weekly employment", "adp national employment"],
        "priority": "HIGH", "focus": "US_LABOR_MARKET",
        "fed": "HIGH", "wall_street": "HIGH",
    },
    "NFP": {
        "aliases": ["non-farm", "non farm", "nonfarm", "non-farm payroll", "nonfarm payroll"],
        "priority": "VERY_HIGH", "focus": "US_LABOR_MARKET",
        "fed": "VERY_HIGH", "wall_street": "VERY_HIGH",
    },
    "CPI": {
        "aliases": ["consumer price index", "cpi"],
        "priority": "VERY_HIGH", "focus": "US_INFLATION",
        "fed": "VERY_HIGH", "wall_street": "VERY_HIGH",
    },
    "PPI": {
        "aliases": ["producer price index", "ppi"],
        "priority": "HIGH", "focus": "US_INFLATION",
        "fed": "HIGH", "wall_street": "HIGH",
    },
    "JOLTS": {
        "aliases": ["jolts", "job openings"],
        "priority": "HIGH", "focus": "US_LABOR_MARKET",
        "fed": "HIGH", "wall_street": "HIGH",
    },
    "PCE": {
        "aliases": ["personal consumption expenditures", "pce price", "pce"],
        "priority": "VERY_HIGH", "focus": "US_INFLATION",
        "fed": "VERY_HIGH", "wall_street": "VERY_HIGH",
    },
    "GDP": {
        "aliases": ["gross domestic product", "gdp"],
        "priority": "HIGH", "focus": "US_GROWTH",
        "fed": "HIGH", "wall_street": "HIGH",
    },
    "ISM": {
        "aliases": ["ism manufacturing", "ism services", "ism manufacturing pmi", "ism services pmi"],
        "priority": "HIGH", "focus": "US_GROWTH",
        "fed": "HIGH", "wall_street": "HIGH",
    },
    "JOBLESS_CLAIMS": {
        "aliases": ["initial jobless claims", "unemployment claims", "jobless claims", "continuing claims"],
        "priority": "HIGH", "focus": "US_LABOR_MARKET",
        "fed": "HIGH", "wall_street": "HIGH",
    },
    "FOMC": {
        "aliases": ["federal funds rate", "fomc", "fomc statement",
                    "fomc press conference", "economic projections"],
        "priority": "VERY_HIGH", "focus": "US_MONETARY_POLICY",
        "fed": "VERY_HIGH", "wall_street": "VERY_HIGH",
    },
}

# Negative rules prevent broad aliases from creating false positives.
NEGATIVE = {
    "NFP": ["adp"],
    "CPI": ["cpi expectations"],
    "PPI": ["ppi expectations"],
    "GDP": ["gdpnow"],
}

def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()

def text(e):
    return norm(" ".join(str(e.get(k, "")) for k in
        ("title", "event", "category", "name", "description")))

def is_us(e):
    return norm(e.get("country")) in {"usd", "us", "usa", "united states",
                                      "united states of america"}

def classify(e):
    t = text(e)
    hits = []
    for name, rule in RULES.items():
        if any(a in t for a in rule["aliases"]):
            if not any(x in t for x in NEGATIVE.get(name, [])):
                hits.append(name)
    # FOMC has precedence when a rate decision also contains generic wording.
    if "FOMC" in hits:
        return "FOMC"
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return "AMBIGUOUS:" + ",".join(hits)
    return None

def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Sektor-Analyse-Normalizer-Test/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        events = json.loads(r.read().decode("utf-8"))

    us = [e for e in events if isinstance(e, dict) and is_us(e)]
    print(f"HTTP_STATUS={r.status}")
    print(f"TOTAL_EVENTS={len(events)}")
    print(f"US_EVENTS={len(us)}")

    counts = {k: 0 for k in RULES}
    ambiguous = []
    unmapped = []

    print("\n=== CLASSIFIED USD EVENTS ===")
    for e in us:
        c = classify(e)
        title = e.get("title", e.get("event", ""))
        print(f"{title!r} -> {c or 'UNMAPPED'}")
        if c and c.startswith("AMBIGUOUS:"):
            ambiguous.append((title, c))
        elif c:
            counts[c] += 1
        else:
            unmapped.append(title)

    print("\n=== COUNTS ===")
    for k, v in counts.items():
        print(f"{k}={v}")

    print("\n=== AMBIGUOUS ===")
    for item in ambiguous:
        print(item)

    print("\n=== UNMAPPED ===")
    for item in unmapped:
        print(item)

    # Synthetic regression cases: verifies the semantic rules even if the event
    # is not present in this week's live feed.
    cases = [
        ("ADP Weekly Employment Change", "ADP"),
        ("Non-Farm Employment Change", "NFP"),
        ("Nonfarm Payrolls", "NFP"),
        ("CPI y/y", "CPI"),
        ("Core CPI y/y", "CPI"),
        ("PPI m/m", "PPI"),
        ("JOLTS Job Openings", "JOLTS"),
        ("PCE Price Index", "PCE"),
        ("Gross Domestic Product q/q", "GDP"),
        ("ISM Manufacturing PMI", "ISM"),
        ("Unemployment Claims", "JOBLESS_CLAIMS"),
        ("Initial Jobless Claims", "JOBLESS_CLAIMS"),
        ("Federal Funds Rate", "FOMC"),
        ("FOMC Statement", "FOMC"),
        ("FOMC Press Conference", "FOMC"),
        ("FOMC Economic Projections", "FOMC"),
        ("ADP Employment Change", "ADP"),
    ]

    print("\n=== SYNTHETIC REGRESSION CASES ===")
    failures = 0
    for title, expected in cases:
        actual = classify({"country": "USD", "title": title})
        ok = actual == expected
        print(f"{'PASS' if ok else 'FAIL'} | {title!r} -> {actual!r} | expected={expected!r}")
        failures += not ok

    # False-positive cases.
    negatives = [
        ("ADP Employment Change", "NFP"),
        ("GDPNow", "GDP"),
        ("CPI Expectations", "CPI"),
    ]
    print("\n=== FALSE-POSITIVE GUARD ===")
    for title, forbidden in negatives:
        actual = classify({"country": "USD", "title": title})
        ok = actual != forbidden
        print(f"{'PASS' if ok else 'FAIL'} | {title!r} -> {actual!r} | forbidden={forbidden!r}")
        failures += not ok

    print(f"\nSYNTHETIC_FAILURES={failures}")
    print("RESULT=PASS" if failures == 0 else "RESULT=FAIL")
    return 0 if failures == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
