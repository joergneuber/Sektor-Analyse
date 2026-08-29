#!/usr/bin/env python3
"""
Großer, NICHT ABBRECHENDER Sammeltest für makro_szenario.py.

Ziel:
- möglichst viele Informationen in EINEM Lauf sammeln
- Fehler einzelner Prüfungen abfangen
- niemals wegen einer einzelnen fehlenden Datenquelle abbrechen
- keine Datenwerte in die Produktion schreiben
- am Ende immer Exit-Code 0 liefern
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import re
import sys
import traceback
from pathlib import Path
from io import StringIO

TARGET = Path("makro_szenario.py")
CACHE = Path(".macro_cache/macro_cache.json")
TE_URLS = {
    "pmi": "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
    "new_orders": "https://tradingeconomics.com/united-states/ism-non-manufacturing-new-orders",
    "employment": "https://tradingeconomics.com/united-states/ism-non-manufacturing-employment",
    "prices": "https://tradingeconomics.com/united-states/ism-non-manufacturing-prices",
}
REFERENCE_CASES = [(2026, 7), (2026, 6), (2026, 5)]
EXPECTED_JULY = {"pmi": 54.10, "new_orders": 57.20, "employment": 47.40, "prices": 70.30}
EXPECTED_RELEASE = "2026-08-05"

RESULTS = []
MODULE = None


def report(name, ok=None, detail=""):
    if ok is True:
        status = "GREEN"
    elif ok is False:
        status = "RED"
    else:
        status = "INFO"
    RESULTS.append((name, status, detail))
    print(f"{name}={status}" + (f" | {detail}" if detail else ""))


def safe(name, fn):
    try:
        return fn()
    except Exception as exc:
        report(name, False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc(limit=2)
        return None


def load_production_module():
    global MODULE
    spec = importlib.util.spec_from_file_location("makro_szenario_master_test", TARGET)
    if spec is None or spec.loader is None:
        raise RuntimeError("import spec could not be created")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    MODULE = module
    return module


def inspect_source(source):
    tree = ast.parse(source)
    funcs = sorted(
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    report("TARGET_AST", True, f"functions={len(funcs)}")
    print("FUNCTIONS=" + ",".join(funcs))

    relevant = [
        "_te_calendar_actual", "_te_text_actual",
        "_te_release_month_matches_reference",
        "_ism_public_secondary_tradingeconomics",
        "_ism_public_secondary_fxblue_services",
        "_ism_extract_official",
        "_ism_cache_entry_valid", "_ism_cache_get_valid",
        "_ism_fetch", "ism_snapshot",
    ]
    for name in relevant:
        hits = len(re.findall(rf"\b{re.escape(name)}\b", source))
        report(f"SOURCE_{name}", hits > 0, f"hits={hits}")

    for forbidden in ("Previous", "Forecast", "Consensus", "TEForecast"):
        hits = [
            (i + 1, line.strip())
            for i, line in enumerate(source.splitlines())
            if re.search(rf"\b{re.escape(forbidden)}\b", line, re.I)
        ]
        report(f"SOURCE_{forbidden.upper()}_REFERENCES", True, f"lines={len(hits)}")
        for line_no, line in hits[:20]:
            print(f"  {forbidden}@{line_no}: {line[:500]}")


def inspect_cache():
    if not CACHE.is_file():
        report("CACHE_FILE", None, "not present")
        return
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    report("CACHE_JSON", True, f"type={type(data).__name__}")
    if isinstance(data, dict):
        print("CACHE_TOP_KEYS=" + ",".join(map(str, data.keys())))

    matches = []

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                p = f"{path}.{key}" if path else str(key)
                if re.search(r"ism|service", p, re.I):
                    matches.append((p, value))
                walk(value, p)
        elif isinstance(obj, list):
            for i, value in enumerate(obj):
                walk(value, f"{path}[{i}]")

    walk(data)
    report("CACHE_ISM_MATCHES", True, f"matches={len(matches)}")
    for path, value in matches[:40]:
        print("CACHE_ENTRY=" + path + " => " + json.dumps(value, ensure_ascii=False)[:3000])


def fetch_te_pages():
    try:
        import requests
    except Exception as exc:
        report("REQUESTS_IMPORT", False, str(exc))
        return {}

    pages = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for kind, url in TE_URLS.items():
        def one(kind=kind, url=url):
            response = requests.get(url, headers=headers, timeout=30)
            report(f"TE_HTTP_{kind}", response.status_code == 200,
                   f"status={response.status_code}; final_url={response.url}; bytes={len(response.content)}")
            pages[kind] = response.text
        safe(f"TE_FETCH_{kind}", one)
    return pages


def inspect_te_html(pages):
    try:
        import pandas as pd
    except Exception as exc:
        report("PANDAS_IMPORT", False, str(exc))
        return

    for kind, html in pages.items():
        if not html:
            continue
        out = Path(f".te_services_{kind}_debug.html")
        safe(f"TE_SAVE_{kind}", lambda html=html, out=out: out.write_text(html, encoding="utf-8"))

        low = html.lower()
        for needle in [
            "actual", "previous", "forecast", "consensus",
            "new orders", "employment", "prices",
            "54.1", "57.2", "47.4", "70.3",
            "jul 2026", "july 2026", "2026-08-05",
        ]:
            count = len(re.findall(re.escape(needle.lower()), low))
            report(f"TE_TEXT_{kind}_{re.sub(r'[^A-Za-z0-9]+','_',needle).strip('_').upper()}",
                   None, f"count={count}")

        def tables():
            tables = pd.read_html(StringIO(html))
            report(f"TE_TABLES_{kind}", True, f"count={len(tables)}")
            for i, df in enumerate(tables):
                text = df.astype(str).to_string(index=False)
                if i < 8 or re.search(
                    r"actual|previous|forecast|consensus|new orders|employment|prices|services|july|jul",
                    text, re.I
                ):
                    print(f"TABLE_{kind}_{i}_SHAPE={df.shape}")
                    print(f"TABLE_{kind}_{i}_COLUMNS={[str(c) for c in df.columns]}")
                    print(text[:5000])
        safe(f"TE_TABLE_FORENSIC_{kind}", tables)


def inspect_functions():
    if MODULE is None:
        return

    names = [
        "_te_calendar_actual", "_te_text_actual",
        "_te_release_month_matches_reference",
        "_ism_public_secondary_tradingeconomics",
        "_ism_public_secondary_fxblue_services",
        "_ism_extract_official",
        "_ism_cache_entry_valid", "_ism_cache_get_valid",
        "_ism_fetch", "ism_snapshot",
    ]
    for name in names:
        fn = getattr(MODULE, name, None)
        report(f"FUNC_PRESENT_{name}", callable(fn))
        if callable(fn):
            safe(
                f"FUNC_SIGNATURE_{name}",
                lambda fn=fn, name=name: report(
                    f"FUNC_SIGNATURE_OK_{name}", True, str(inspect.signature(fn))
                ),
            )


def run_production_calls():
    if MODULE is None:
        return

    # Die Aufrufe sind absichtlich defensiv: nur Funktionen mit der
    # tatsächlich vorhandenen Signatur werden ausgeführt.
    fn = getattr(MODULE, "_ism_public_secondary_tradingeconomics", None)
    if callable(fn):
        for year, month in REFERENCE_CASES:
            safe(
                f"TE_SECONDARY_SERVICES_{year}_{month}",
                lambda year=year, month=month: print(
                    "RESULT _ism_public_secondary_tradingeconomics",
                    year, month, "services =>", repr(fn(year, month, "services"))
                ),
            )

    fn = getattr(MODULE, "_ism_public_secondary_fxblue_services", None)
    if callable(fn):
        for year, month in REFERENCE_CASES:
            safe(
                f"FXBLUE_SERVICES_{year}_{month}",
                lambda year=year, month=month: print(
                    "RESULT _ism_public_secondary_fxblue_services",
                    year, month, "=>", repr(fn(year, month))
                ),
            )

    fn = getattr(MODULE, "_ism_fetch", None)
    if callable(fn):
        for year, month in REFERENCE_CASES:
            safe(
                f"ISM_FETCH_SERVICES_{year}_{month}",
                lambda year=year, month=month: print(
                    "RESULT _ism_fetch", year, month, "services =>",
                    repr(fn("services", year, month))
                ),
            )

    fn = getattr(MODULE, "ism_snapshot", None)
    if callable(fn):
        for args in [("services",), ("manufacturing",)]:
            safe(
                "ISM_SNAPSHOT_" + "_".join(args),
                lambda args=args: print(
                    "RESULT ism_snapshot", args, "=>", repr(fn(*args))
                ),
            )


def main():
    print("=== LARGE NON-ABORTING TE / ISM SERVICES MASTER TEST ===")
    print(f"TARGET={TARGET}")
    print(f"REFERENCE_CASES={REFERENCE_CASES}")
    print(f"EXPECTED_JULY={EXPECTED_JULY}")
    print(f"EXPECTED_RELEASE={EXPECTED_RELEASE}")
    print("RULE=NO SINGLE TEST MAY ABORT THE RUN")

    report("TARGET_EXISTS", TARGET.is_file())
    if not TARGET.is_file():
        print("RESULT=DIAGNOSTIC_COMPLETE")
        print("EXIT_POLICY=0")
        return

    source = safe("READ_TARGET", lambda: TARGET.read_text(encoding="utf-8"))
    if source is not None:
        safe("SOURCE_INSPECTION", lambda: inspect_source(source))

    safe("CACHE_INSPECTION", inspect_cache)

    safe("PRODUCTION_IMPORT", load_production_module)
    safe("FUNCTION_INSPECTION", inspect_functions)

    pages = safe("TE_PAGE_COLLECTION", fetch_te_pages) or {}
    safe("TE_HTML_FORENSIC", lambda: inspect_te_html(pages))

    safe("PRODUCTION_FUNCTION_CALLS", run_production_calls)

    print("\n=== FINAL SUMMARY ===")
    green = red = info = 0
    for name, status, detail in RESULTS:
        print(f"{name}: {status}" + (f" | {detail}" if detail else ""))
        if status == "GREEN":
            green += 1
        elif status == "RED":
            red += 1
        else:
            info += 1

    print(f"SUMMARY_GREEN={green}")
    print(f"SUMMARY_RED={red}")
    print(f"SUMMARY_INFO={info}")
    print("RESULT=DIAGNOSTIC_COMPLETE")
    print("EXIT_POLICY=0")

    # Absichtlich 0: Der Sammeltest soll Informationen sammeln,
    # nicht wegen einer einzelnen Datenquelle den gesamten Workflow stoppen.
    sys.exit(0)


if __name__ == "__main__":
    main()
