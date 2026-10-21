#!/usr/bin/env python3
"""Investment Radar: combine existing setup, macro and structure inputs into a dated thesis report."""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPORT_RE = re.compile(r"Investment_Radar_(\d{4}-\d{2}-\d{2})\.txt$")


def newest(folder: Path, pattern: str) -> Path | None:
    files = sorted(folder.glob(pattern), key=lambda p: p.name, reverse=True)
    return files[0] if files else None


def read_csv(path: Path | None) -> list[dict[str, str]]:
    if not path:
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        return list(csv.DictReader(f, dialect=dialect))


def read_json(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_text(path: Path | None) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace") if path else ""


def first(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def company_label(item: dict[str, Any]) -> str:
    name = first(item, "Name", "name", "Unternehmen", "Company")
    ticker = first(item, "Ticker", "ticker", "Symbol")
    if name and ticker:
        return f"{name} ({ticker})"
    return ticker or name or "Unternehmen nicht zugeordnet"


def ticker_of(item: dict[str, Any]) -> str:
    return first(item, "Ticker", "ticker", "Symbol").upper()


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(" ", "")
    if not text or text.lower() in {"n.v.", "nv", "none", "null", "-", "na", "n/a"}:
        return None
    # Inputs are commonly German-formatted (1,25) or US-formatted (1.25).
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def status_of(item: dict[str, Any]) -> str:
    return first(item, "trade_story_status", "Status", "Status2", "status") or "Status nicht eindeutig"


def setup_of(item: dict[str, Any]) -> str:
    return first(item, "Setup_Typ", "Setup", "Signal", "Strategie") or "Setup nicht angegeben"


def crv_of(item: dict[str, Any]) -> float | None:
    return parse_float(first(item, "CRV1", "CRV"))


def thesis_of(item: dict[str, Any]) -> str:
    return first(item, "These", "Trade_Story", "trade_story", "Begründung", "Kommentar", "Einordnung")


def condition_of(item: dict[str, Any]) -> str:
    return first(item, "Bedingung", "Trigger", "Next_Step", "Hinweis", "Weiter beobachten")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def evidence_lines(text: str, patterns: str) -> list[str]:
    result = []
    for raw in text.splitlines():
        line = normalize_text(raw.strip(" -•\t"))
        if line and re.search(patterns, line, re.I):
            result.append(line[:500])
    return result


def macro_and_structure_view(macro_text: str, structure_text: str) -> tuple[list[str], list[str], list[str]]:
    """Return report lines, bullish/confirming evidence and risk/counter evidence."""
    view: list[str] = []
    positive: list[str] = []
    negative: list[str] = []
    for label, text in (("Makro", macro_text), ("Marktstruktur/Trend", structure_text)):
        if not text.strip():
            view.append(f"- {label}: Quelle fehlt.")
            continue
        lines = [normalize_text(x.strip(" -•\t")) for x in text.splitlines() if normalize_text(x.strip(" -•\t"))]
        relevant = [x for x in lines if re.search(
            r"(STATUS\s*=|trend|risiko|bull|bear|long|short|gold|zins|inflation|sektor|rotation|unterstütz|widerstand|break|über|unter|china|öl|energy)", x, re.I)]
        view.append(f"- {label}: " + (" | ".join(x[:250] for x in relevant[:5]) if relevant else "keine klar interpretierbaren Richtungsdaten im Briefing."))
        for line in relevant:
            if re.search(r"(bull|long|positiv|bestätig|über.*(trend|widerstand|durchschnitt)|risk.?on|sinkende? zins)", line, re.I):
                positive.append(f"{label}: {line}")
            if re.search(r"(bear|short|risiko|negativ|unter.*(trend|unterstütz|durchschnitt)|risk.?off|steigende? zins|invalid|unavailable|fehl)", line, re.I):
                negative.append(f"{label}: {line}")
    return view, positive, negative


def make_candidate_pool(folder: Path) -> list[dict[str, Any]]:
    sources = [
        newest(folder, "Trade_Story_Universum(*).json"),
        newest(folder, "Setups(*).csv"),
        newest(folder, "Edelmetalle_Setups(*).csv"),
        newest(folder, "Trendwende_Setups(*).csv"),
        newest(folder, "Short_Setups(*).csv"),
    ]
    pool: list[dict[str, Any]] = []
    story_data = read_json(sources[0])
    story_candidates = story_data.get("candidates", [])
    if isinstance(story_candidates, list):
        pool.extend(dict(x) for x in story_candidates if isinstance(x, dict))
    for path in sources[1:]:
        pool.extend(dict(x) for x in read_csv(path) if isinstance(x, dict))

    unique: dict[str, dict[str, Any]] = {}
    for item in pool:
        ticker = ticker_of(item)
        if not ticker:
            continue
        if ticker not in unique:
            unique[ticker] = item
        else:
            # Merge complementary records without overwriting already supplied evidence.
            for key, value in item.items():
                if not unique[ticker].get(key) and value:
                    unique[ticker][key] = value

    excluded = {"UNGÜLTIG", "INVALID", "AUSGESCHLOSSEN", "BLOCKIERT", "KEIN SETUP"}
    picks = []
    for item in unique.values():
        status = status_of(item).upper()
        if status in excluded:
            continue
        has_setup_evidence = any(first(item, key) for key in (
            "Setup_Typ", "Setup", "These", "Trade_Story", "trade_story", "CRV1", "CRV"))
        if has_setup_evidence or status.upper() == "VALIDE SETUP":
            picks.append(item)
    return picks


def interpret_candidate(item: dict[str, Any], macro_positive: list[str], macro_negative: list[str]) -> tuple[str, list[str]]:
    """Conservative, evidence-labelled assessment; no fabricated price levels or signals."""
    status = status_of(item)
    crv = crv_of(item)
    thesis = thesis_of(item)
    condition = condition_of(item)
    setup = setup_of(item)
    evidence: list[str] = []
    if thesis:
        evidence.append(f"Quelldatensatz: {thesis[:500]}")
    if condition:
        evidence.append(f"Quelle nennt als Bedingung: {condition[:350]}")

    if status.upper() == "VALIDE SETUP":
        assessment = "Setup-Quelle markiert den Kandidaten als valide; das ist eine technische Vorauswahl, keine eigenständige Entry-Freigabe."
    elif re.search(r"VORBEREIT|WATCH|BEOBACHT", status, re.I):
        assessment = "Vorbereiteter Kandidat: erst bei bestätigtem Trigger bzw. belastbarer Strukturentwicklung weiter aufwerten."
    else:
        assessment = f"Status '{status}' ist nicht eindeutig genug für eine Freigabe; Setupdaten zunächst verifizieren."

    if crv is None:
        assessment += " CRV1 fehlt; Chance/Risiko ist nicht prüfbar."
    elif crv < 1:
        assessment += f" CRV1 {crv:g} liegt unter 1; nach dem Radarfilter keine aussichtsreiche Freigabe."
    else:
        assessment += f" CRV1 {crv:g} erfüllt rechnerisch die Mindestschwelle 1, sagt allein aber nichts über Trefferwahrscheinlichkeit oder Kontextqualität."

    # Cross-check sector/asset context only when a usable sector label exists.
    sector = first(item, "Sektor", "Sector", "Branche", "Industry")
    context_terms = [x for x in macro_positive + macro_negative if sector and re.search(re.escape(sector), x, re.I)]
    if context_terms:
        evidence.append("Makro-/Sektorbezug: " + " | ".join(context_terms[:2]))
        assessment += " Es liegt ein expliziter Sektorbezug im Briefing vor; Details stehen unter Kontextsignalen."
    elif macro_negative:
        evidence.append("Allgemeiner Gegenwind im Makro-/Strukturbriefing vorhanden; keine ticker- oder sektorspezifische Zuordnung belegt.")
        assessment += " Das allgemeine Marktumfeld enthält Gegenwindsignale, deren direkte Wirkung auf diesen Titel nicht belegt ist."
    elif macro_positive:
        evidence.append("Allgemeine positive Kontextsignale im Briefing vorhanden; kein titelbezogener Zusammenhang belegt.")
        assessment += " Positive Kontextsignale sind vorhanden, aber nicht als unmittelbare Bestätigung dieses Titels nachgewiesen."
    else:
        evidence.append("Keine belastbare Zuordnung von Makro-/Strukturkontext zu diesem Titel möglich.")
        assessment += " Makro-/Strukturbezug bleibt mangels verwertbarer oder zuordenbarer Daten offen."
    evidence.insert(0, f"Setup: {setup}; Status-Einordnung siehe Kandidatenzeile")
    return assessment, evidence


def parse_report_snapshot(path: Path) -> dict[str, dict[str, str]]:
    """Read candidate identity and the actual assessment from a prior report."""
    snapshot: dict[str, dict[str, str]] = {}
    if not path.exists():
        return snapshot
    section = False
    current_ticker = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("2. AUSSICHTSREICHE TRADECHANCEN"):
            section = True
            continue
        if section and re.match(r"\s*\d+\.", line):
            break
        if not section:
            continue
        match = re.match(r"\s*-\s*(.+?)\s*:\s*(.*?)\.\s*Status:\s*(.*?)\.\s*CRV1:\s*(.+?)\s*$", line)
        if match:
            label, setup, status, crv = (x.strip() for x in match.groups())
            crv = crv.rstrip(".").strip()
            ticker_match = re.search(r"\(([^()]+)\)\s*$", label)
            current_ticker = ticker_match.group(1).strip().upper() if ticker_match else label.upper()
            snapshot[current_ticker] = {"label": label, "setup": setup, "status": status, "crv": crv, "assessment": ""}
            continue
        assessment_match = re.match(r"\s*Einschätzung:\s*(.*)", line)
        if current_ticker and assessment_match:
            snapshot[current_ticker]["assessment"] = assessment_match.group(1).strip()
    return snapshot


def find_previous_report(output: Path, input_folder: Path, today: date) -> Path | None:
    candidates: dict[str, Path] = {}
    for folder in {output.parent, input_folder}:
        if folder.exists():
            for path in folder.glob("Investment_Radar_*.txt"):
                match = REPORT_RE.search(path.name)
                if match and match.group(1) < today.isoformat():
                    candidates[match.group(1)] = path
    if not candidates:
        return None
    return candidates[max(candidates)]


def build_report(folder: Path, output: Path | None = None, today: date | None = None) -> str:
    folder = Path(folder)
    today = today or date.today()
    output = output or folder / f"Investment_Radar_{today.isoformat()}.txt"

    macro = newest(folder, "Makro_Briefing(*).txt")
    structure = newest(folder, "Struktur_Trend_Briefing(*).txt")
    bitcoin = newest(folder, "Trade_Story_Bitcoin(*).json")
    macro_text, structure_text = read_text(macro), read_text(structure)
    context_lines, positive, negative = macro_and_structure_view(macro_text, structure_text)

    picks = make_candidate_pool(folder)
    picks.sort(key=lambda item: (
        status_of(item).upper() == "VALIDE SETUP",
        crv_of(item) if crv_of(item) is not None else -999,
        company_label(item).casefold(),
    ), reverse=True)

    previous_path = find_previous_report(output, folder, today)
    previous = parse_report_snapshot(previous_path) if previous_path else {}
    current: dict[str, dict[str, str]] = {}
    candidate_sections: list[str] = []
    for item in picks:
        ticker = ticker_of(item)
        label = company_label(item)
        setup = setup_of(item)
        status = status_of(item)
        crv_value = crv_of(item)
        crv_text = first(item, "CRV1", "CRV") or "n.v."
        assessment, evidence = interpret_candidate(item, positive, negative)
        current[ticker] = {"label": label, "setup": setup, "status": status, "crv": crv_text, "assessment": assessment}
        candidate_sections.extend([
            f"- {label}: {setup}. Status: {status}. CRV1: {crv_text}.",
            f"  Einschätzung: {assessment}",
        ])
        if evidence:
            candidate_sections.extend(f"  Belege/Einordnung: {x}" for x in evidence)

    lines = [
        f"INVESTMENT RADAR | {today.isoformat()}",
        "Fokus: begründete Chancenbewertung mit Datenstatus; Radar ist keine automatische Entry-Freigabe.",
        "", "1. MAKRO- UND STRUKTURBLICK",
    ]
    lines.extend(context_lines or ["- Makro-/Strukturbriefing fehlt."])
    if positive:
        lines += ["", "Bestätigende Kontextsignale:"] + [f"- {x}" for x in positive[:8]]
    if negative:
        lines += ["", "Gegenwind / Risiken:"] + [f"- {x}" for x in negative[:8]]

    lines += ["", "2. AUSSICHTSREICHE TRADECHANCEN"]
    lines.extend(candidate_sections or ["- Keine ausreichend belegten Kandidaten in den verfügbaren Quellen."])

    lines += ["", "3. VERÄNDERUNG ZUM VORTAG"]
    if not previous_path:
        lines.append("- Kein früherer datierter Radarbericht gefunden; Tagesvergleich startet mit diesem Snapshot.")
    else:
        lines.append(f"- Vergleichsbasis: {previous_path.name}")
        new_tickers = sorted(set(current) - set(previous))
        gone_tickers = sorted(set(previous) - set(current))
        shared = sorted(set(current) & set(previous))
        if new_tickers:
            lines.append("Neue Kandidaten: " + "; ".join(current[t]["label"] for t in new_tickers))
        if gone_tickers:
            lines.append("Nicht mehr gelistet: " + "; ".join(previous[t]["label"] for t in gone_tickers))
        changes = []
        for ticker in shared:
            old, new = previous[ticker], current[ticker]
            diffs = []
            for key, title in (("status", "Status"), ("setup", "Setup"), ("crv", "CRV1")):
                if normalize_text(old.get(key, "")) != normalize_text(new.get(key, "")):
                    diffs.append(f"{title}: '{old.get(key, 'n.v.')}' → '{new.get(key, 'n.v.')}'")
            old_thesis = normalize_text(old.get("assessment", ""))
            new_thesis = normalize_text(new.get("assessment", ""))
            if old_thesis and new_thesis and old_thesis != new_thesis:
                diffs.append(f"These/Einschätzung: '{old_thesis[:220]}' → '{new_thesis[:220]}'")
            if diffs:
                changes.append(f"{new['label']}: " + "; ".join(diffs))
        lines.extend(f"Änderung: {x}" for x in changes)
        if not (new_tickers or gone_tickers or changes):
            lines.append("- Keine Änderung bei Kandidaten, Status, Setup oder CRV1 im verfügbaren Berichtssnapshot erkannt.")

    lines += ["", "4. THESENVERLAUF / WAS DIE THESE ÄNDERT"]
    thesis_changes = []
    if previous_path:
        for ticker in sorted(set(current) | set(previous)):
            if ticker not in previous:
                thesis_changes.append(f"Neu: {current[ticker]['label']} – erstmals im Radar; These anhand der heutigen Quellen prüfen.")
            elif ticker not in current:
                thesis_changes.append(f"Entfallen: {previous[ticker]['label']} – heute nicht mehr in den Kandidatenquellen.")
            else:
                old, new = previous[ticker], current[ticker]
                changed = (old.get("status") != new.get("status") or old.get("crv") != new.get("crv")
                           or old.get("setup") != new.get("setup")
                           or normalize_text(old.get("assessment", "")) != normalize_text(new.get("assessment", "")))
                if changed:
                    thesis_changes.append(f"Aktualisiert: {new['label']} – Status, Setup, CRV oder konkrete Einschätzung gegenüber Vortag verändert; Details in Abschnitt 3.")
    if thesis_changes:
        lines.extend(f"- {x}" for x in thesis_changes)
    elif previous_path:
        lines.append("- Im Berichtssnapshot keine Kandidatenänderung als Thesenwechsel erkennbar; das ist kein Beweis für unveränderte Kurs-/Makrothese.")
    else:
        lines.append("- Für echte Verlaufsaussagen wird ein vorheriger datierter Bericht benötigt. Dieser erste Snapshot dient als Vergleichsbasis.")
    lines += [
        "- Quellenlücken oder nicht tickerbezogene Makrohinweise werden ausdrücklich als Unsicherheit behandelt; daraus wird keine Kursrichtung abgeleitet.",
        "- Kandidaten sind Radar-Chancen, keine automatische Order- oder Entry-Empfehlung.",
    ]

    btc_data = read_json(bitcoin)
    if btc_data:
        btc_lines = []
        for key in ("pi_cycle_bottom", "sma50w"):
            value = btc_data.get(key)
            if isinstance(value, dict) and (value.get("message") or value.get("state")):
                btc_lines.append(f"{key}: {value.get('message') or value.get('state')}")
        if btc_lines:
            lines += ["", "Bitcoin-Kontext:"] + [f"- {x}" for x in btc_lines]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, help="Ordner mit den Radar-Eingabedateien")
    parser.add_argument("--output", help="Berichtspfad; standardmäßig datierter Bericht im Input-Ordner")
    args = parser.parse_args()
    folder = Path(args.input_dir)
    output = Path(args.output) if args.output else folder / f"Investment_Radar_{date.today().isoformat()}.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_report(folder, output=output), encoding="utf-8")
    print(f"Investment Radar Bericht geschrieben: {output}")


if __name__ == "__main__":
    main()
