#!/usr/bin/env python3
"""
Isolierter Test der gepatchten Produktionsdatei.

Es wird ausschließlich der bestehende
_ism_public_secondary_tradingeconomics(year, month, "services")
aufgerufen.

Zielmonat: Juli 2026
Erwartete reale TE-Services-Werte aus dem vorherigen Strukturtest:
PMI 54.10
New Orders 57.20
Employment 47.40
Prices 70.30
Release-Date 2026-08-05

Der Test verändert keine Produktionsdaten.
"""

import importlib.util
import sys
from pathlib import Path

TARGET_FILE = Path("makro_szenario_v5_9_7_te_pmi_release_fix.py")


def main():
    print("=== TE ISM SERVICES PRODUCTION-PATCH TEST ===")
    print(f"FILE={TARGET_FILE}")

    if not TARGET_FILE.is_file():
        print("RESULT=RED_FILE_NOT_FOUND")
        return 1

    spec = importlib.util.spec_from_file_location("macro_test", TARGET_FILE)
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

    print("CALL=services report_month=2026-07")
    try:
        result = fn(2026, 7, "services")
    except Exception as exc:
        print(f"FUNCTION_ERROR={type(exc).__name__}: {exc}")
        print("RESULT=RED_FUNCTION_EXCEPTION")
        return 1

    print(f"RESULT_OBJECT={result!r}")

    if not isinstance(result, dict):
        print("RESULT=RED_NO_COMPLETE_RESULT")
        return 1

    required = {
        "pmi": 54.10,
        "new_orders": 57.20,
        "employment": 47.40,
        "prices": 70.30,
    }

    failed = []
    for key, expected in required.items():
        actual = result.get(key)
        print(f"{key.upper()}={actual} EXPECTED={expected}")
        if actual is None or abs(float(actual) - expected) > 1e-9:
            failed.append(key)

    release = result.get("release_date")
    print(f"RELEASE_DATE={release} EXPECTED=2026-08-05")
    if release != "2026-08-05":
        failed.append("release_date")

    if result.get("status") != "REAL_PUBLIC_SECONDARY":
        failed.append("status")

    if failed:
        print(f"FAILED_FIELDS={','.join(failed)}")
        print("RESULT=RED_PRODUCTION_PATCH_NOT_CONFIRMED")
        return 1

    print("RESULT=GREEN_PRODUCTION_PATCH_CONFIRMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
