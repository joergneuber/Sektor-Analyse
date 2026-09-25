#!/usr/bin/env python3
"""Erzeugt bei Gemini-Ausfall ein kostenloses, manuell in ChatGPT nutzbares Fallback-Paket.

Die Datei verwendet ausschließlich die Python-Standardbibliothek. Sie ruft keine
LLM-API auf und verändert keine Trading-/Setup-Dateien.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
MASTER = ROOT / "Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md"

DATA_PATTERNS = [
    "Briefing(*).txt", "briefing.txt",
    "Setups(*).csv",
    "Trade_Story_Setup_Rohuniversum(*).csv",
    "Trade_Story_Bitcoin(*).json",
    "Performance(*).csv", "Performance_EU(*).csv",
    "Offene Positionen+Check.csv", "Offene_Positionen.csv",
    "Offene_Positionen(*).csv",
    "Trendwende_Setups(*).csv", "Trendwende_Briefing(*).txt",
    "Short_Setups(*).csv", "Short_Briefing(*).txt",
    "Einzel_Check_Aufstiege(*).txt", "Einzel_Check_A_Meldungen(*).txt",
    "hebeltrader_einzel_check.json", "einzel_check_historie.jsonl",
    "Edelmetalle_Setups(*).csv", "Edelmetalle_Briefing(*).txt",
    "Langfrist_Bewertung(*).csv", "Langfrist_Briefing(*).txt",
    "Makro_Briefing(*).txt",
    "Struktur_Trend_Briefing(*).txt",
    "Bitcoin_Trading_DE_Briefing.txt",
    "Gold_Trading_DE_Briefing.txt",
    "Silber_Trading_DE_Briefing.txt",
    "Benchmark_Live.txt",
    "Trade_Story_Universum(*).json",
    "einzel_check_beobachtung.json",
]

FALLBACK_INSTRUCTION = """CHATGPT-FALLBACK-ANWEISUNG

Zweck:
Erzeuge aus den im Paket enthaltenen aktuellen Eingabedateien die vollständige
Auswertung entsprechend der beigefügten Master-Anweisung. Dieses Paket wird
nur verwendet, wenn Gemini technisch nicht verfügbar war.

VERBINDLICH:
1. Die Master-Anweisung ist vollständig einzuhalten.
2. Ausschließlich die tatsächlich beigefügten aktuellen Daten verwenden.
3. Keine Zahlen, Kurse, Termine, Quellenwerte oder Statusangaben erfinden.
4. Makro-Szenario-Gate und Makro-Datenqualität exakt aus dem aktuellen
   Makro-Datenpaket übernehmen.
5. Lithium TE: Falls vorhanden, Wert, Datum und Einheit ausschließlich aus dem
   aktuellen Datenpaket übernehmen. Es gibt keinen festen Sollwert in dieser
   Anweisung. Lithium TE niemals mit dem LIT-Proxy gleichsetzen.
6. ADP berücksichtigen, sofern im aktuellen Datenpaket vorhanden.
7. Für Tradingideen sowohl Chancen-Konvergenz als auch Risiko-/Gegensignal-
   Konvergenz prüfen. Ziel ist die frühe Identifikation potenziell profitabler
   Tradingideen. Mehrere unabhängige Datenebenen müssen die These stützen;
   Gegensignale sind ausdrücklich zu nennen.
8. Eine Discovery ist kein valides Setup, kein Kauf und kein Entry. Die
   bestehenden technischen Setup-Regeln bleiben maßgeblich.
9. Keine eigenen Scores, künstlichen Wahrscheinlichkeiten oder Crash-
   Prognosen erzeugen.
10. Vor der finalen Ausgabe intern die zehn Pre-Output-Checks der
    Master-Anweisung durchführen.
11. Gib anschließend nur die vollständige Auswertung gemäß Master-Anweisung aus.
"""

def _matches(path: Path, pattern: str) -> bool:
    # pathlib.PurePath.match unterstützt * auch in Dateinamen und bleibt
    # unabhängig von Shell-/Glob-Eigenheiten.
    return path.name == pattern or path.match(pattern)

def collect_files():
    """Liest den exakten Eingabebestand des Gemini-Laufs aus dem Manifest."""
    manifest_path = ROOT / ".gemini_input_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(
            "FEHLER: .gemini_input_manifest.json fehlt. "
            "Fallback wird nicht aus einer separaten Dateimusterliste rekonstruiert."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"FEHLER: Gemini-Input-Manifest nicht lesbar: {exc}")

    paths = []
    master_path = ROOT / manifest.get("master_instruction", MASTER.name)
    if master_path.is_file():
        paths.append(master_path)
    else:
        raise SystemExit("FEHLER: Im Gemini-Manifest referenzierte Master-Anweisung fehlt.")

    for entry in manifest.get("files", []):
        raw = entry.get("path") or entry.get("filename")
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        if not p.is_file():
            raise SystemExit(
                f"FEHLER: Im Gemini-Manifest referenzierte Eingabedatei fehlt: {p}"
            )
        paths.append(p)

    return sorted(set(paths), key=lambda p: p.name.lower())

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def build(output: Path) -> Path:
    files = collect_files()
    if not MASTER.is_file():
        raise SystemExit("FEHLER: Master-Anweisung nicht gefunden.")
    if not files:
        raise SystemExit("FEHLER: Keine Eingabedateien gefunden.")

    date = dt.date.today().isoformat()
    manifest = [
        "CHATGPT-FALLBACK-MANIFEST",
        f"DATUM={date}",
        f"DATEIEN={len(files)}",
        "QUELLE=GEMINI-INPUT-MANIFEST",
        "",
    ]
    for p in files:
        manifest.append(f"{p.name}\t{p.stat().st_size}\t{sha256(p)}")

    # Vor dem Verpacken sicherstellen, dass keine Eingabedatei seit dem
    # Gemini-Upload stillschweigend verändert wurde.
    source_manifest = json.loads((ROOT / ".gemini_input_manifest.json").read_text(encoding="utf-8"))
    expected = {entry.get("path") or entry.get("filename"): entry.get("sha256") for entry in source_manifest.get("files", [])}
    master_expected = source_manifest.get("master_sha256")
    if master_expected and sha256(MASTER) != master_expected:
        raise SystemExit("FEHLER: Gemini-Master-Anweisung seit Manifest-Erstellung veraendert.")
    for p in files:
        if p == MASTER:
            continue
        key = str(p)
        if key in expected and expected[key] != sha256(p):
            raise SystemExit(f"FEHLER: Gemini-Eingabedatei seit Manifest-Erstellung veraendert: {p}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)
        zf.write(ROOT / ".gemini_input_manifest.json", arcname="GEMINI_INPUT_MANIFEST.json")
        zf.writestr("CHATGPT_FALLBACK_ANWEISUNG.txt", FALLBACK_INSTRUCTION)
        zf.writestr("FALLBACK_MANIFEST.txt", "\n".join(manifest) + "\n")

    print(f"CHATGPT_FALLBACK_PAKET={output}")
    print(f"CHATGPT_FALLBACK_DATEIEN={len(files)}")
    return output

if __name__ == "__main__":
    today = dt.date.today().isoformat()
    build(ROOT / f"CHATGPT_FALLBACK_{today}.zip")
