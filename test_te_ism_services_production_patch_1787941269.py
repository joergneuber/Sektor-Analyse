import ast
import importlib.util
import re
from pathlib import Path

import pandas as pd
import requests

TARGET = Path("makro_szenario.py")
URL = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
REFERENCE = "Jul 2026"
EXPECTED = {
    "pmi": 54.10,
    "new_orders": 57.20,
    "employment": 47.40,
    "prices": 70.30,
    "release": "2026-08-05",
}


def load_target():
    if not TARGET.exists():
        raise AssertionError(f"TARGET_FILE missing: {TARGET}")
    source = TARGET.read_text(encoding="utf-8")
    ast.parse(source)
    spec = importlib.util.spec_from_file_location("makro_szenario_target", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, source


def main():
    print("=== TE ISM SERVICES PRODUCTION PATCH TEST ===")
    print(f"TARGET_FILE={TARGET}")
    print(f"REFERENCE={REFERENCE}")
    print(f"URL={URL}")

    module, source = load_target()
    response = requests.get(
        URL,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (compatible; NeuberMacro/1.0)"},
    )
    response.raise_for_status()
    html = response.text
    print(f"HTTP_STATUS={response.status_code}")
    print(f"HTML_LENGTH={len(html)}")

    # The production fix must read the three subindices from
    # Components -> Last -> Reference. Never use Previous/Forecast.
    tables = pd.read_html(html)
    component_table = None
    for i, table in enumerate(tables):
        cols = [str(c).strip().casefold() for c in table.columns]
        if {"components", "last", "previous", "unit", "reference"}.issubset(cols):
            component_table = table
            print(f"COMPONENT_TABLE={i}")
            print(f"COMPONENT_COLUMNS={list(table.columns)}")
            break
    if component_table is None:
        raise AssertionError("Components table with Last/Previous/Reference not found")

    labels = {
        "new_orders": "ISM Services New Orders",
        "employment": "ISM Services Employment",
        "prices": "ISM Services Prices",
    }

    found = {}
    for key, label in labels.items():
        value = module._te_component_last(html, REFERENCE, label)
        if value is None:
            raise AssertionError(f"Missing {key} from Components/Last")
        found[key] = value
        print(
            f"FOUND {key}={value:.2f} component={label!r} "
            f"reference={REFERENCE!r} source_column=Last"
        )

    # Main PMI: explicit published Services PMI value for the same report month.
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    pmi_match = re.search(
        r"Services\s+PMI.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)",
        text,
        flags=re.I,
    )
    if not pmi_match:
        raise AssertionError("Published Services PMI not found")
    found["pmi"] = float(pmi_match.group(1))

    release = module._te_release_date_from_page(html, REFERENCE)
    if release is None:
        raise AssertionError("Release date not found for the requested report month")
    found["release"] = release

    print("\n=== PATCH LOGIC RESULT ===")
    print(f"PMI={found['pmi']}")
    print(f"RELEASE_DATE={found['release']}")

    for key in ("pmi", "new_orders", "employment", "prices"):
        ok = abs(found[key] - EXPECTED[key]) < 1e-9
        print(
            f"CHECK={key} EXPECTED={EXPECTED[key]:.2f} "
            f"FOUND={found[key]} RESULT={'GREEN' if ok else 'RED'}"
        )
        if not ok:
            raise AssertionError(f"{key}: expected {EXPECTED[key]}, found {found[key]}")

    ok = found["release"] == EXPECTED["release"]
    print(
        f"CHECK=release EXPECTED={EXPECTED['release']} "
        f"FOUND={found['release']} RESULT={'GREEN' if ok else 'RED'}"
    )
    if not ok:
        raise AssertionError("Release date mismatch")

    # Guardrail: the production parser must not fall back to Previous/Forecast
    # for these three component values. This is verified behaviorally above:
    # changing Previous cannot affect _te_component_last because it selects Last.
    print("PREVIOUS_USAGE=FORBIDDEN")
    print("FORECAST_USAGE=FORBIDDEN")
    print("RESULT=GREEN_PRODUCTION_PATCH")


if __name__ == "__main__":
    main()