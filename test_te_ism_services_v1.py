#!/usr/bin/env python3
"""
ISOLIERTER TEST: Trading Economics / ISM Services
Prueft den aktuellen Hauptcode gegen den Juli-2026-Report.

WICHTIG:
- TARGET_FILE ist der aktuelle Produktionscode im Repository.
- Dieser Test veraendert keine Produktionsdaten.
- Forecast/Previous werden nicht akzeptiert.
"""

from pathlib import Path
import importlib.util
import sys

TARGET_FILE = Path("makro_szenario_.py")

EXPECTED = {
    "pmi": 54.10,
    "new_orders": 57.20,
    "employment": 47.40,
    "prices": 70.30,
}
EXPECTED_RELEASE = "2026-08-05"


def main():
    print("=== ISOLATED TE ISM SERVICES TEST ===")
    print(f"TARGET_FILE={TARGET_FILE}")

    if not TARGET_FILE.is_file():
        print("RESULT=RED_FILE_NOT_FOUND")
        return 1

    spec = importlib.util.spec_from_file_location("macro_under_test", TARGET_FILE)
    if spec is None or spec.loader is None:
        print("RESULT=RED_IMPORT_SPEC")
        return 1

    module = importlib.util.module_from_spec(spec)

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        print(f"IMPORT_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_IMPORT")
        return 1

    fn = getattr(module, "_ism_public_secondary_tradingeconomics", None)

    if fn is None:
        print("RESULT=RED_FUNCTION_MISSING")
        return 1

    print("CALL=_ism_public_secondary_tradingeconomics(2026, 7, 'services')")

    try:
        result = fn(2026, 7, "services")
    except Exception as exc:
        print(f"FUNCTION_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_FUNCTION_EXCEPTION")
        return 1

    print(f"RESULT_OBJECT={result!r}")

    if not isinstance(result, dict):
        print("RESULT=RED_NO_RESULT")
        return 1

    failed = []

    for key, expected in EXPECTED.items():
        actual = result.get(key)
        print(f"{key.upper()}={actual} EXPECTED={expected}")

        try:
            if actual is None or abs(float(actual) - expected) > 1e-9:
                failed.append(key)
        except (TypeError, ValueError):
            failed.append(key)

    release = result.get("release_date")
    print(f"RELEASE_DATE={release} EXPECTED={EXPECTED_RELEASE}")

    if release != EXPECTED_RELEASE:
        failed.append("release_date")

    status = result.get("status")
    print(f"STATUS={status} EXPECTED=REAL_PUBLIC_SECONDARY")

    if status != "REAL_PUBLIC_SECONDARY":
        failed.append("status")

    if failed:
        print("FAILED_FIELDS=" + ",".join(failed))
        print("RESULT=RED_TE_ISM_SERVICES")
        return 1

    print("RESULT=GREEN_TE_ISM_SERVICES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
