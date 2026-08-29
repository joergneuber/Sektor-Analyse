"""Gezielter Makro-Validierungslauf nach ISM/LME/TE-Reparatur.

Laeuft bis zum Ende weiter, auch wenn einzelne Quellen fehlschlagen.
Es werden nur die problematischen Makro-Beschaffungswege getestet.
"""
import datetime as dt
import importlib.util
import traceback

SPEC = importlib.util.spec_from_file_location("macro", "makro_szenario.py")
macro = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macro)

def run(label, fn):
    print(f"\n===== {label} =====")
    try:
        value = fn()
        print(value)
        print(f"RESULT {label}=COMPLETED")
        return value
    except Exception as exc:
        print(f"RESULT {label}=ERROR {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return None

# Freitag 28.08.2026 ist der verlangte Vortages-Datenstand fuer das Montag-Briefing.
today = dt.date(2026, 8, 29)
target_y, target_m = 2026, 7

def check_ism(kind):
    d = macro._ism_fetch(kind, target_y, target_m)
    if not d:
        print(f"ISM {kind}: KEIN DATENSATZ")
        return
    print(f"ISM {kind}: source={d.get('source_type')} reference={d.get('reference')}")
    fields = macro._ism_target_maps(kind).keys()
    for key in fields:
        print(f"  {key}={d.get(key, 'NICHT VERFUEGBAR')}")
    missing = [k for k in fields if d.get(k) is None]
    print(f"  FEHLENDE_FELDER={missing or 'KEINE'}")

run("ISM_MANUFACTURING_JULY_2026", lambda: check_ism("manufacturing"))
run("ISM_SERVICES_JULY_2026", lambda: check_ism("services"))
run("LME_TE_28_08_2026", lambda: print("\n".join(macro.lme_snapshot(today))))
run("SP_GLOBAL_SERVICES", lambda: print(macro.spglobal_services_snapshot(today)))

print("\n===== MAKRO-VALIDIERUNG GESAMT =====")
print("Alle Einzelpruefungen wurden bis zum Ende ausgefuehrt.")
print("Wichtig: Dieser Workflow startet KEINE Gemini-Auswertung.")
