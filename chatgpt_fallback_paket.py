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
import csv
import re
import io
import math
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

VERBINDLICHE ARBEITSREIHENFOLGE:
1. Lies zuerst CHATGPT_FALLBACK_BERICHTSSTRUKTUR.txt. Diese Datei legt die
   konkrete Schreibweise und Reihenfolge des Fallback-Berichts fest.
2. Lies danach die vollständige Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md.
   Sie bleibt die übergeordnete Master-Spezifikation und hat bei inhaltlichen
   oder strukturellen Konflikten immer Vorrang.
3. Verwende ausschließlich die tatsächlich beigefügten aktuellen Daten.

VERBINDLICH:
1. Die Master-Anweisung ist vollständig einzuhalten.
2. Den Positionsabgleich im FALLBACK_MANIFEST.txt verbindlich beachten.
   Bei Abweichungen keinen Bestand stillschweigend als aktuell bezeichnen.
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


REPORT_WRITING_SPEC = r"""CHATGPT-FALLBACK-BERICHTSSTRUKTUR
KONKRETE SCHREIB- UND GLIEDERUNGSANWEISUNG

ZWECK UND RANGFOLGE
Diese Datei ist die konkrete Arbeitsanweisung für die Erstellung des vollständigen
Trading-Radar-Berichts im technischen Gemini-Fallback.

Verbindliche Rangfolge:
1. Sicherung_Gemini_Engine_Trading-Setups_Automatisierung.md
   = übergeordnete und inhaltlich bindende Master-Spezifikation.
2. Diese Datei
   = konkrete Schreib-, Reihenfolge- und Strukturhilfe für den Fallback-Bericht.
3. CHATGPT_FALLBACK_ANWEISUNG.txt
   = Ausführungsauftrag für den Fallback.

Bei einem Widerspruch gilt immer die Master-Spezifikation. Diese Strukturhilfe
darf keine Regel, Kennzahl, Filterbedingung oder fachliche Bedeutung der
Master-Spezifikation ändern.

DATENBASIS
Verwende ausschließlich die im ZIP enthaltenen Original-Eingabedateien sowie
GEMINI_INPUT_MANIFEST.json und FALLBACK_MANIFEST.txt.
Keine externen Daten, keine Web-Recherche und keine Werte aus früheren Berichten
als Ersatz für fehlende aktuelle Werte verwenden.
Originaldateien nicht umschreiben, bereinigen, zusammenführen oder neu erzeugen.
Werte, Namen, Ticker, Einheiten, Datenstände und Statusangaben aus der jeweiligen
Quelle übernehmen. Fehlende Werte als fehlend kennzeichnen oder gemäß
Master-Spezifikation weglassen. Niemals schätzen oder erfinden.

AUSGABEFORMAT
Erstelle genau einen vollständigen Bericht als Klartext (.txt).
Keine Markdown-Syntax: keine Markdown-Überschriften mit #, keine Sternchen für
Fett/Kursiv, keine Markdown-Tabellen und keine Markdown-Aufzählungen mit "-"
oder "*". Verwende gut lesbare GROSSGESCHRIEBENE Abschnittsüberschriften,
Leerzeilen und einfache Label: Wert-Zeilen. Einfache Aufzählungszeichen wie
"•" sind nur dort zu verwenden, wo die Master-Spezifikation Listen verlangt.
Keine zusätzlichen Vorbemerkungen, keine separate Zusammenfassung außerhalb
der vorgeschriebenen Berichtsstruktur.

VERBINDLICHE REIHENFOLGE DES BERICHTS

DOKUMENTENKOPF
Neuber Macro & Markets
Datum der Auswertung: TT.MM.JJJJ
Tägliche Markt- und Setup-Auswertung

Vor dem Titel steht nichts.
Eine Zeile "Handelstag (Datenstand dieser Auswertung): ..." und eine Zeile
"Erstellt am: ..." nur übernehmen, wenn sie in den Eingabedateien ausdrücklich
vorhanden sind. Nicht aus Dateizeitstempeln oder Kursdaten ableiten.

BLICK AUF WICHTIGE INDIZES
Europa, USA und Asien getrennt aufführen. Jeden in der Quelle vorhandenen Index
einzeln mit Datenstand, letztem Handelstag und YTD-Wert wiedergeben.
Keine Regionaldurchschnitte bilden. Keine fehlenden Punktestände, Zeitpunkte
oder Datenstände ergänzen. Laufende Tageswerte klar von abgeschlossenen
Schlusskursen unterscheiden.

LIVE-PERFORMANCE VS. MSCI WORLD
Nur erstellen, wenn Benchmark_Live.txt im Paket vorhanden ist.
Die dortigen Kennzahlen und Bezeichnungen wörtlich übernehmen.
EUNL.DE nicht als eigene Systemposition einrechnen. Out-/Underperformance
als Prozentpunkte kennzeichnen. Fehlt die Datei, diesen Block vollständig
weglassen und nicht aus anderen Dateien rekonstruieren.

KURZ-ZUSAMMENFASSUNG
Immer in dieser Reihenfolge:
SOFORT BEACHTEN:
Nur unmittelbar relevante, in den Quelldateien belegte Fakten nennen, darunter
Stop-/TP-Ereignisse, Positionen nahe am Stop, Earnings bei bestehenden
Positionen/Setups und außergewöhnliche Marktbewegungen. Ereignis und betroffene
offene Position direkt miteinander verknüpfen, wenn die Daten das belegen.
Keine Handlungsempfehlung.

WATCHLIST:
Nur die nach Master-Spezifikation zulässigen manuellen Watchlists und
Beinahe-Kandidaten aufnehmen. Höchstgrenzen, Reihenfolge und Ausnahmen der
Master-Spezifikation beachten. Keine eigene Rangfolge oder Bewertung erzeugen.
Neue valide Setups je Kategorie nennen; auch eine Anzahl von 0 ausdrücklich
ausweisen. Bereits offene Positionen zählen nicht als neue Setups.

RISIKO-WATCH
Die vier in der Master-Spezifikation definierten Prüfungen durchführen:
Sektorkonzentration, ungeschützte Positionen, Fast-Stop-Positionen und
Event-Häufung innerhalb des vorgegebenen Zeitfensters.
Nur belegte Treffer konkret nennen. Wenn keine Prüfung einen akuten Punkt ergibt,
den vorgeschriebenen Satz "Risiko-Watch: keine akuten Punkte" ausgeben.
Keine neue Kennzahl oder Handlungsempfehlung erfinden.

WOCHENAUSBLICK
Die in der Master-Spezifikation festgelegten 3–5 relevanten Termine aus den
vorhandenen Daten nennen. FOMC- und Earnings-Termine nur übernehmen, wenn sie
in den Quelldateien stehen und in das definierte Zeitfenster fallen.
Langfrist-Bewertung nur erwähnen, wenn der entsprechende aktuelle Abschnitt
tatsächlich beigefügt ist. Wenn keine bekannten Termine vorliegen, den in der
Master-Spezifikation vorgeschriebenen Leersatz verwenden.

HAUPTAUSWERTUNG
Die Hauptabschnitte fortlaufend und ohne Nummernlücken in genau dieser Reihenfolge
ausgeben:

1. MARKTUMFELD & GLOBALE RISIKOLAGE
Markt-, Index-, Zins-, Volatilitäts-, FX-, Rohstoff- und Makrokontext anhand der
vorhandenen Daten. Relevante Zusammenhänge und Gegenargumente nur aus den
beigefügten Quellen darstellen. Keine Werte aus anderen Abschnitten stillschweigend
übertragen, wenn deren Datenstand abweicht.

2. MAKRO-ZUKUNFTSSZENARIO
Das aktuelle Makro-Szenario-Gate, den Datenqualitätsstatus und die vorhandenen
Makro-Indikatoren aus dem aktuellen Makro-Datenpaket übernehmen.
ADP berücksichtigen, wenn vorhanden. Lithium TE nur mit dem tatsächlich
gelieferten Wert, Datum und Einheit aufführen; niemals mit dem LIT-Proxy
gleichsetzen. Keine fehlenden Reihen oder Werte rekonstruieren.

3. TRENDFOLGE-SETUPS
Nur die nach Master-Spezifikation validen Setups ausgeben.
Setup-Karten und Werte exakt aus den zugehörigen Dateien übernehmen.
ACHTUNG-Watchlist, Beinahe-Kandidaten und Funnel-Engstellen entsprechend den
dort definierten Regeln einordnen. Offene Positionen nicht als Neueinstieg zählen.

4. TRENDWENDE-SETUPS
Nur die nach Master-Spezifikation gültigen Trendwende-Setups ausgeben.
Divergenz-Watchlist und CRV-Beinahe-Kandidaten sind unterschiedliche Stufen
und dürfen nicht zusammengelegt oder verwechselt werden. Fehlende TP-/Stop-Werte
nicht ergänzen, wenn die Quelle sie strukturell nicht liefert.

5. HEBELTRADER-SETUPS
Die eigenständige Hebeltrader-Logik und die in der Quelle angegebene Schwelle
beachten. Alle gelieferten Kriterien einschließlich erfüllter und nicht
erfüllter Kriterien ausweisen. Die Einzel-Check-Beobachtungsliste ausschließlich
aus ihrer eigenen JSON-Datei übernehmen. Statuswerte sind keine Handelsfreigabe.

6. SHORT-SETUPS
Nur valide Short-Setups nach den bestehenden Scanner-Regeln ausgeben.
Short-spezifische Daten und Watchlists nicht mit Long-Setups vermischen.

7. EDELMETALLE-SETUPS
Gold, Silber, Platin und Palladium anhand der gelieferten Daten und der
Master-Regeln getrennt behandeln. Trendfolge- und Short-Logik nicht vermischen.
Bei Engstellen die in der Quelle genannten Metallnamen statt bloßer Anzahlen
verwenden, sofern die Master-Spezifikation dies verlangt.

8. LANGFRIST-BEWERTUNG
Nur aufnehmen, wenn die aktuelle Langfrist-Datei bzw. der zugehörige
Langfrist-Abschnitt tatsächlich im Paket vorhanden ist. Andernfalls vollständig
weglassen; keine Laufzeit oder Aktualität vermuten.

OFFENE POSITIONEN
Die vorhandene Positionsquelle unverändert als Datenbasis behandeln.
Den Positionsabgleich im FALLBACK_MANIFEST.txt beachten:
- Drive-Checkquelle und Backend-CSV bleiben getrennte Quellen.
- Bei Abweichungen keinen Bestand stillschweigend als aktuell festlegen.
- Bei fehlender Backend-CSV ausdrücklich "NICHT VERGLEICHBAR" berücksichtigen.
- Keine Positionen aus beiden Quellen zu einem künstlichen Mischbestand
  zusammenführen.
- Die im ZIP enthaltene Original-Drive-Check-CSV nicht verändern.
Der Abgleich verwendet den vereinfachten eindeutigen Positionsschlüssel der
normalen Positionsverwaltung: normalisierter Name, Ticker, numerisch
normalisierter Einstieg und Einstiegsdatum. Gezählt werden ausschließlich
Zeilen mit Status "Offen". Gleiche Ticker mit unterschiedlichen Einstiegsdaten
bleiben getrennte Positionen. Identische doppelte Positionsschlüssel führen
wie in der normalen Verarbeitung zu einem Sicherheitsabbruch.

GESCHLOSSENE POSITIONEN (LETZTE 10 WERKTAGE)
Nur die im bereitgestellten Positions-/Historienmaterial belegten Abgänge im
von der Master-Spezifikation definierten Zeitraum ausgeben. Automatisch
gestoppte und manuell verkaufte Positionen entsprechend der Quelldaten
unterscheiden. Keine fehlenden Ausstiegsdaten rekonstruieren.

METHODIK & LESEHILFE
Als letzten, unnummerierten Abschnitt ausgeben.
Datenqualität, Quellenstatus, Reichweitenhinweis, Glossar und die in der
Master-Spezifikation verlangten methodischen Hinweise aufnehmen.
Keine neue Quelle oder neue Kennzahl ergänzen.

ABSCHLUSSPRÜFUNG VOR AUSGABE
Vor dem Ausgeben den vollständigen Bericht gegen die Master-Spezifikation prüfen:
1. Reihenfolge und alle Pflichtabschnitte korrekt?
2. Optionale Abschnitte nur bei tatsächlich vorhandenen Eingabedaten?
3. Alle Zahlen, Kurse, Datenstände, Einheiten, Namen und Ticker quellengetreu?
4. Keine Werte aus unterschiedlichen Datenständen vermischt?
5. Keine fehlenden Werte ergänzt oder erfunden?
6. Setup-Status und Discovery strikt getrennt?
7. Watchlist-Grenzen, Reihenfolgen und Ausnahmen eingehalten?
8. Positionsabgleich korrekt und ohne stillschweigende Bestandsauswahl dargestellt?
9. Klartextformat ohne Markdown eingehalten?
10. Risikohinweis, Methodik, Glossar und Reichweitenhinweis gemäß Master enthalten?

AUSGABE
Gib ausschließlich den vollständigen Bericht als Klartext aus.
Keine Testprotokolle, keine technischen Paketdetails und keine zusätzliche
Erklärung außerhalb des Berichts.
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


def _manifest_entry(manifest: dict, logical_name: str):
    """Findet einen Manifest-Eintrag über logischen Namen oder Dateinamen."""
    wanted = logical_name.casefold()
    for entry in manifest.get("files", []):
        logical = str(entry.get("logical_name") or "").strip().casefold()
        filename = Path(str(entry.get("filename") or entry.get("path") or "")).name.casefold()
        if logical == wanted or filename == wanted:
            return entry
    return None


def _entry_path(entry: dict) -> Path:
    raw = entry.get("path") or entry.get("filename")
    if not raw:
        raise ValueError("Manifest-Eintrag enthält weder path noch filename")
    path = Path(str(raw))
    return path if path.is_absolute() else ROOT / path


def _parse_position_number(value: object) -> float | None:
    """Spiegelt parse_number() aus offene_positionen_check.py fuer Zahlenwerte."""
    if value is None:
        return None
    # parse_number() behandelt Python-Zahlen und numpy.number getrennt vom
    # Textpfad und verwirft dort alle nicht-endlichen Werte. Die numpy-
    # Erkennung erfolgt ohne numpy-Import, damit das Fallback stdlib-only bleibt.
    is_numpy_number = any(
        base.__module__.split(".", 1)[0] == "numpy" and base.__name__ == "number"
        for base in type(value).__mro__
    )
    if isinstance(value, (int, float)) or is_numpy_number:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except Exception:
            return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    text = text.replace("%", "").replace("€", "").replace("$", "").strip()
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return None


def _position_key(row: dict, columns: dict[str, str], line_number: int) -> tuple:
    """Port des bestehenden _position_key() aus offene_positionen_check.py."""
    name = re.sub(r"\s+", " ", str(row.get(columns["name"], "")).strip()).casefold()
    ticker = str(row.get(columns["ticker"], "")).strip().upper()
    entry_raw = str(row.get(columns["entry"], "")).strip()
    date = str(row.get(columns["date"], "")).strip()
    if not name or not ticker or not entry_raw or not date:
        raise ValueError(
            f"Zeile {line_number}: Positionsschlüssel nicht bildbar; "
            "Name, Ticker, Einstieg und Einstiegsdatum müssen vorhanden sein."
        )
    entry = _parse_position_number(entry_raw)
    if entry is None:
        raise ValueError(
            f"Zeile {line_number}: Einstieg ist nicht numerisch: {entry_raw!r}"
        )
    return (name, ticker, round(float(entry), 10), date)


def _read_open_positions(path: Path, label: str) -> set[tuple]:
    """Liest offene Positionen nach denselben Identitätsregeln wie die Positionsverwaltung."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
        if not raw.strip():
            raise ValueError("CSV ist vollständig leer")
        try:
            dialect = csv.Sniffer().sniff(raw[:8192], delimiters=";,|\t")
            reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
        except csv.Error:
            header = raw.splitlines()[0]
            delimiter = max((";", ",", "|", "\t"), key=header.count)
            reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("CSV enthält keine Kopfzeile")

        def norm(value):
            return "".join(ch for ch in str(value or "").strip().casefold() if ch.isalnum())

        fields = {norm(name): name for name in reader.fieldnames if name}
        columns = {
            "ticker": next((fields[key] for key in ("ticker", "yahooticker", "symbol") if key in fields), None),
            "name": fields.get("name"),
            "entry": fields.get("einstieg"),
            "date": fields.get("einstiegsdatum"),
            "status": fields.get("status"),
        }
        missing = [key for key, value in columns.items() if value is None]
        if missing:
            labels = {"ticker": "Ticker", "name": "Name", "entry": "Einstieg",
                      "date": "Einstiegsdatum", "status": "Status"}
            raise ValueError("Positionsspalten fehlen: " + ", ".join(labels[key] for key in missing))

        closed_statuses = {
            "geschlossen", "closed", "verkauft", "sold", "ausgestoppt",
            "gestoppt", "stopped", "beendet", "exit", "exited",
        }
        positions: set[tuple] = set()
        for line_number, row in enumerate(reader, start=2):
            if not any(str(value or "").strip() for value in row.values()):
                continue
            ticker = str(row.get(columns["ticker"]) or "").strip().upper()
            # Die feste Anleitungsvorlage ist keine Position und wird wie im
            # normalen Positionscheck ausdrücklich übersprungen.
            if ticker == "ANLEITUNG":
                continue
            status = str(row.get(columns["status"]) or "").strip().casefold()
            if status == "offen":
                key = _position_key(row, columns, line_number)
                if key in positions:
                    raise ValueError(
                        f"Zeile {line_number}: doppelter identischer Positionsschlüssel "
                        f"für {ticker}; normale Positionsverwaltung würde ebenfalls abbrechen."
                    )
                positions.add(key)
            elif status in closed_statuses:
                continue
            else:
                raise ValueError(
                    f"Zeile {line_number}: leerer oder unbekannter Status "
                    f"'{row.get(columns['status']) or ''}' für {ticker or 'ohne Ticker'}"
                )
        return positions
    except Exception as exc:
        raise SystemExit(f"FEHLER: {label} nicht sicher prüfbar ({path.name}): {exc}")


def _format_position_keys(positions: set[tuple]) -> str:
    if not positions:
        return "keine"
    return "; ".join(
        f"{ticker} | {name} | Einstieg={entry:.10g} | Einstiegsdatum={date}"
        for name, ticker, entry, date in sorted(positions, key=lambda item: (item[1], item[3], item[0], item[2]))
    )


def _positions_reconciliation(manifest: dict) -> tuple[str, list[Path]]:
    """Vergleicht Drive-Checkquelle und optionale Backend-CSV über vollständige Positionsschlüssel."""
    check_entry = _manifest_entry(manifest, "Offene Positionen+Check.csv")
    backend_entry = _manifest_entry(manifest, "Offene_Positionen.csv")
    if not check_entry:
        raise SystemExit(
            "FEHLER: Die verpflichtende Drive-Checkquelle "
            "'Offene Positionen+Check.csv' fehlt im Gemini-Input-Manifest. "
            "Fallback-Paket wird nicht erstellt."
        )

    try:
        check_path = _entry_path(check_entry)
        if not check_path.is_file():
            raise FileNotFoundError(check_path)
        check_positions = _read_open_positions(check_path, "Drive-Checkbestand")
    except Exception as exc:
        raise SystemExit(f"FEHLER: Drive-Checkquelle nicht verfügbar: {exc}")

    created_at = manifest.get("created_at") or "nicht im Manifest dokumentiert"
    drive_name = (
        check_entry.get("drive_name")
        or check_entry.get("filename")
        or check_path.name
    )
    drive_id = check_entry.get("drive_file_id") or "im Gemini-Input-Manifest nicht gespeichert"
    drive_modified = check_entry.get("drive_modified_time") or "im Gemini-Input-Manifest nicht gespeichert"
    drive_retrieved = check_entry.get("drive_retrieved_at")
    local_mtime = dt.datetime.fromtimestamp(
        check_path.stat().st_mtime, dt.timezone.utc
    ).isoformat()

    lines = [
        "POSITIONSABGLEICH – Fallback-Sicherheitsprüfung",
        "",
        "IDENTITÄTSREGEL: Name normalisiert + Ticker + numerischer Einstieg + Einstiegsdatum.",
        "STATUSREGEL: Ausschließlich Status 'Offen' zählt; ANLEITUNG-Zeile wird ignoriert.",
        "",
        f"Drive-Quelle laut Gemini-Input-Manifest: {drive_name}",
        f"Logischer Quellenname: {check_entry.get('logical_name') or 'nicht separat gespeichert'}",
        f"Drive-Datei-ID: {drive_id}",
        f"Drive-modifiedTime: {drive_modified}",
        (
            f"Drive-Abrufzeitpunkt (explizite Metadaten): {drive_retrieved}"
            if drive_retrieved
            else "Drive-Abrufzeitpunkt: im Quellmanifest nicht separat erfasst"
        ),
        f"Gemini-Input-Snapshot erstellt (UTC): {created_at}",
        f"Lokaler Datei-Zeitstempel (UTC, kein belegter Drive-Abrufzeitpunkt): {local_mtime}",
        f"Datei im Runner: {check_path.name}",
        f"SHA-256 Drive-Checkquelle: {sha256(check_path)}",
        f"Offene Positionen Drive-Checkquelle: {len(check_positions)}",
        f"Positionsschlüssel Drive-Checkquelle: {_format_position_keys(check_positions)}",
        "",
    ]

    position_files = [check_path]
    if not backend_entry:
        lines.extend([
            "ERGEBNIS: NICHT VERGLEICHBAR – Backend-CSV fehlt im Gemini-Input-Manifest.",
            "Die Drive-Checkquelle wird unverändert als Check-Snapshot beigefügt.",
            "Es wird keine Aussage getroffen, dass ein zweiter Bestand identisch oder abweichend ist.",
        ])
    else:
        try:
            backend_path = _entry_path(backend_entry)
            if not backend_path.is_file():
                raise FileNotFoundError(backend_path)
            backend_positions = _read_open_positions(backend_path, "Backend-Bestand")
        except Exception as exc:
            raise SystemExit(
                "FEHLER: Backend-CSV ist im Manifest enthalten, aber nicht sicher prüfbar: "
                f"{exc}"
            )
        position_files.append(backend_path)
        only_check = check_positions - backend_positions
        only_backend = backend_positions - check_positions
        lines.extend([
            f"Backend-Datei: {backend_path.name}",
            f"SHA-256 Backend-CSV: {sha256(backend_path)}",
            f"Offene Positionen Backend-CSV: {len(backend_positions)}",
            f"Positionsschlüssel Backend-CSV: {_format_position_keys(backend_positions)}",
            "",
        ])
        if only_check or only_backend:
            lines.extend([
                "ERGEBNIS: ABWEICHUNG – KEIN BESTAND WIRD AUTOMATISCH ALS AKTUELL AUSGEWÄHLT.",
                f"Nur/zusätzlich in Drive-Checkquelle: {_format_position_keys(only_check)}",
                f"Nur/zusätzlich in Backend-CSV: {_format_position_keys(only_backend)}",
                "Die Drive-Checkquelle wird als Check-Snapshot vollständig beigefügt; "
                "die Backend-CSV bleibt eine getrennte Vergleichsquelle.",
            ])
        else:
            lines.extend([
                "ERGEBNIS: POSITIONSBESTÄNDE IDENTISCH (anhand des vollständigen Positionsschlüssels).",
                "Die Identität bezieht sich auf Name, Ticker, Einstieg und Einstiegsdatum; "
                "andere Positionsfelder und die Aktualität werden dadurch nicht bestätigt.",
            ])
    return "\n".join(lines) + "\n", position_files

def build(output: Path) -> Path:
    files = collect_files()
    source_manifest = json.loads((ROOT / ".gemini_input_manifest.json").read_text(encoding="utf-8"))
    positions_report, position_files = _positions_reconciliation(source_manifest)
    files = sorted(set(files + position_files), key=lambda p: p.name.lower())
    master_path = ROOT / source_manifest.get("master_instruction", MASTER.name)
    if not master_path.is_file():
        raise SystemExit("FEHLER: Im Gemini-Manifest referenzierte Master-Anweisung nicht gefunden.")
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

    # Der Positionsabgleich steht im bereits vorhandenen Manifest; es wird
    # keine zusätzliche Positionsdatei erzeugt. Die Original-CSV-Dateien
    # werden separat und bytegetreu aus den Manifest-Eingaben übernommen.
    manifest.extend(["", positions_report.rstrip()])

    # Vor dem Verpacken sicherstellen, dass keine Eingabedatei seit dem
    # Gemini-Upload stillschweigend verändert wurde.
    expected = {}
    for entry in source_manifest.get("files", []):
        raw = entry.get("path") or entry.get("filename")
        if raw:
            expected[Path(str(raw)) if Path(str(raw)).is_absolute()
                    else (ROOT / str(raw)).resolve()] = entry.get("sha256")
    master_path = ROOT / source_manifest.get("master_instruction", MASTER.name)
    master_expected = source_manifest.get("master_sha256")
    if master_expected and sha256(master_path) != master_expected:
        raise SystemExit("FEHLER: Gemini-Master-Anweisung seit Manifest-Erstellung veraendert.")
    for p in files:
        if p.resolve() == master_path.resolve():
            continue
        expected_hash = expected.get(p.resolve())
        if not expected_hash:
            raise SystemExit(
                f"FEHLER: Kein SHA-256-Eintrag im Gemini-Input-Manifest für: {p}"
            )
        if expected_hash != sha256(p):
            raise SystemExit(f"FEHLER: Gemini-Eingabedatei seit Manifest-Erstellung veraendert: {p}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)
        zf.write(ROOT / ".gemini_input_manifest.json", arcname="GEMINI_INPUT_MANIFEST.json")
        zf.writestr("CHATGPT_FALLBACK_ANWEISUNG.txt", FALLBACK_INSTRUCTION)
        zf.writestr("CHATGPT_FALLBACK_BERICHTSSTRUKTUR.txt", REPORT_WRITING_SPEC)
        zf.writestr("FALLBACK_MANIFEST.txt", "\n".join(manifest) + "\n")

    print(f"CHATGPT_FALLBACK_PAKET={output}")
    print(f"CHATGPT_FALLBACK_DATEIEN={len(files)}")
    return output

if __name__ == "__main__":
    today = dt.date.today().isoformat()
    build(ROOT / f"CHATGPT_FALLBACK_{today}.zip")
