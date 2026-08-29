import ast
import importlib.util
import json
import sys
import types
import importlib.util
from pathlib import Path

import requests

TARGET = Path("makro_szenario.py")
REFERENCE_YEAR = 2026
REFERENCE_MONTH = 7
REFERENCE = "Jul 2026"
EXPECTED = {
    "pmi": 54.10,
    "new_orders": 57.20,
    "employment": 47.40,
    "prices": 70.30,
}
EXPECTED_RELEASE = "2026-08-05"

RESULTS = []


def report(name, status, detail=""):
    RESULTS.append((name, status, detail))
    suffix = f" | {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")


def safe(name, fn):
    try:
        value = fn()
        report(name, "GREEN")
        return value
    except Exception as exc:
        report(name, "RED", f"{type(exc).__name__}: {exc}")
        return None


def load_target():
    # The diagnostic never calls yfinance. Provide an import-only stub when
    # the optional production dependency is absent in the local environment.
    # GitHub Actions can still install the real dependency for the main run.
    if importlib.util.find_spec("yfinance") is None:
        sys.modules["yfinance"] = types.ModuleType("yfinance")
    if not TARGET.exists():
        raise FileNotFoundError(f"TARGET_FILE missing: {TARGET}")
    source = TARGET.read_text(encoding="utf-8")
    ast.parse(source)
    spec = importlib.util.spec_from_file_location("makro_szenario_target", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, source


def cache_diagnostic(module):
    cache_file = Path(getattr(module, "MACRO_CACHE_FILE", ".macro_cache/macro_cache.json"))
    print(f"CACHE_FILE={cache_file}")
    if not cache_file.exists():
        report("CACHE_PRESENT", "INFO", "kein lokaler Cache im Testlauf vorhanden")
        return None

    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception as exc:
        report("CACHE_READ", "RED", f"{type(exc).__name__}: {exc}")
        return None

    report("CACHE_READ", "GREEN")
    candidates = []
    ism = data.get("ism", {}) if isinstance(data, dict) else {}
    if isinstance(ism, dict):
        candidates.extend(ism.values())
    entries = data.get("ism_entries", []) if isinstance(data, dict) else []
    if isinstance(entries, list):
        candidates.extend(entries)

    matching = []
    for entry in candidates:
        payload = entry.get("data") if isinstance(entry, dict) and isinstance(entry.get("data"), dict) else entry
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind", entry.get("kind") if isinstance(entry, dict) else None)
        year = payload.get("year", entry.get("year") if isinstance(entry, dict) else None)
        month = payload.get("month", entry.get("month") if isinstance(entry, dict) else None)
        if kind == "services" and str(year) == str(REFERENCE_YEAR) and str(month) == str(REFERENCE_MONTH):
            matching.append(payload)

    if not matching:
        report("CACHE_SERVICES_JUL2026", "INFO", "kein passender Cache-Eintrag gefunden")
        return None

    payload = matching[0]
    print("CACHE_SERVICES_PAYLOAD=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    validator = getattr(module, "_ism_cache_entry_valid", None)
    if validator is None:
        report("CACHE_VALIDATOR_PRESENT", "RED", "_ism_cache_entry_valid fehlt")
    else:
        try:
            valid = validator(payload, "services", REFERENCE_YEAR, REFERENCE_MONTH)
            report("CACHE_VALIDATION", "GREEN" if valid else "RED", f"validator={valid}")
        except Exception as exc:
            report("CACHE_VALIDATION", "RED", f"{type(exc).__name__}: {exc}")
    return payload


def direct_te(module):
    url = "https://tradingeconomics.com/united-states/non-manufacturing-pmi"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; NeuberMacro/1.0)"})
    report("TE_MAIN_HTTP", "GREEN" if r.ok else "RED", f"status={r.status_code} length={len(r.text)}")
    if not r.ok:
        return None
    html = r.text

    values = {}
    component_labels = {
        "new_orders": "ISM Services New Orders",
        "employment": "ISM Services Employment",
        "prices": "ISM Services Prices",
    }
    helper = getattr(module, "_te_component_last", None)
    if helper is None:
        report("TE_COMPONENT_HELPER", "RED", "_te_component_last fehlt")
    else:
        for key, label in component_labels.items():
            try:
                value = helper(html, REFERENCE, label)
                if value is None:
                    raise ValueError("kein Wert")
                values[key] = float(value)
                report(f"TE_COMPONENT_{key}", "GREEN", f"value={values[key]:.2f} source=Components/Last")
            except Exception as exc:
                report(f"TE_COMPONENT_{key}", "RED", f"{type(exc).__name__}: {exc}")

    text = module.re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=module.re.I | module.re.S)
    text = module.re.sub(r"<[^>]+>", " ", text)
    text = module.re.sub(r"\s+", " ", text).strip()
    match = module.re.search(r"Services\s+PMI.*?(?:registered|at)\s+([0-9]{2}(?:\.[0-9])?)", text, flags=module.re.I)
    if match:
        values["pmi"] = float(match.group(1))
        report("TE_PMI", "GREEN", f"value={values['pmi']:.2f}")
    else:
        report("TE_PMI", "RED", "published Services PMI nicht gefunden")

    release_helper = getattr(module, "_te_release_date_from_page", None)
    if release_helper is None:
        report("TE_RELEASE_HELPER", "RED", "_te_release_date_from_page fehlt")
    else:
        try:
            release = release_helper(html, REFERENCE)
            report("TE_RELEASE", "GREEN" if release else "RED", f"release={release}")
            values["release"] = release
        except Exception as exc:
            report("TE_RELEASE", "RED", f"{type(exc).__name__}: {exc}")

    for key, expected in EXPECTED.items():
        if key not in values:
            report(f"TE_EXPECTED_{key}", "RED", "Wert fehlt")
        else:
            ok = abs(values[key] - expected) < 1e-9
            report(f"TE_EXPECTED_{key}", "GREEN" if ok else "RED", f"expected={expected:.2f} found={values[key]:.2f}")
    if "release" in values:
        report("TE_EXPECTED_RELEASE", "GREEN" if values["release"] == EXPECTED_RELEASE else "RED", f"expected={EXPECTED_RELEASE} found={values['release']}")
    return html


def production_te_path(module):
    fn = getattr(module, "_ism_public_secondary_tradingeconomics", None)
    if fn is None:
        report("PROD_TE_FUNCTION_PRESENT", "RED", "Funktion fehlt")
        return None
    try:
        result = fn(REFERENCE_YEAR, REFERENCE_MONTH, "services")
    except Exception as exc:
        report("PROD_TE_FUNCTION", "RED", f"{type(exc).__name__}: {exc}")
        return None

    if not result:
        report("PROD_TE_RESULT", "RED", "Funktion liefert None/leer")
        return None

    report("PROD_TE_RESULT", "GREEN", json.dumps(result, ensure_ascii=False, sort_keys=True))
    for key, expected in EXPECTED.items():
        actual = result.get(key)
        ok = actual is not None and abs(float(actual) - expected) < 1e-9
        report(f"PROD_TE_{key}", "GREEN" if ok else "RED", f"expected={expected:.2f} found={actual}")
    release = result.get("release_date")
    report("PROD_TE_RELEASE", "GREEN" if release == EXPECTED_RELEASE else "RED", f"expected={EXPECTED_RELEASE} found={release}")
    return result


def fallback_path(module):
    fn = getattr(module, "_ism_public_secondary_fxblue_services", None)
    if fn is None:
        report("FXBLUE_FUNCTION_PRESENT", "RED", "Funktion fehlt")
        return None
    try:
        result = fn(REFERENCE_YEAR, REFERENCE_MONTH)
        if not result:
            report("FXBLUE_RESULT", "INFO", "kein vollstaendiger Fallback-Datensatz")
            return None
        report("FXBLUE_RESULT", "GREEN", json.dumps(result, ensure_ascii=False, sort_keys=True))
        return result
    except Exception as exc:
        report("FXBLUE_RESULT", "RED", f"{type(exc).__name__}: {exc}")
        return None


def main():
    print("=== TE ISM SERVICES SEQUENTIAL DIAGNOSTIC ===")
    print(f"TARGET_FILE={TARGET}")
    print(f"REFERENCE={REFERENCE}")
    print("MODE=NON_ABORTING")
    print("RULE=fehlende Daten werden protokolliert; der Test laeuft bis zum Ende")

    module = safe("TARGET_SYNTAX_AND_IMPORT", load_target)
    if module is None:
        print("IMPORT_FAILED: weitere produktionsnahe Checks koennen nicht ausgefuehrt werden")
    else:
        mod, _source = module
        safe("CACHE_DIAGNOSTIC", lambda: cache_diagnostic(mod))
        safe("DIRECT_TE_DIAGNOSTIC", lambda: direct_te(mod))
        safe("PRODUCTION_TE_DIAGNOSTIC", lambda: production_te_path(mod))
        safe("FXBLUE_FALLBACK_DIAGNOSTIC", lambda: fallback_path(mod))

    print("\n=== FINAL DIAGNOSTIC SUMMARY ===")
    for name, status, detail in RESULTS:
        print(f"{status:5} {name} {detail}")
    print("RESULT=DIAGNOSTIC_COMPLETE")
    print("EXIT_POLICY=0 (diagnostic never aborts because a data source is unavailable)")


if __name__ == "__main__":
    main()
