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


def clean_macro_text(text: str, limit: int = 150) -> str:
    """Remove embedded article bodies and technical source metadata from report snippets."""
    text = normalize_text(text)
    # Article bodies were accidentally appended to macro rows and made unrelated
    # news appear to be a stock-specific macro assessment.
    text = re.split(r"\bARTICLE_TEXT\s*=", text, maxsplit=1, flags=re.I)[0]
    text = re.split(r"\bSOURCE\s*=", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\b(?:STATUS|SERIES|frequency|DATENTYP|HINWEIS|NFP-CHECK|NFP-VALIDIERUNG)\s*=.*", "", text, flags=re.I)
    text = normalize_text(text).strip(" |;,-")
    return text[:limit].rstrip()


def format_price(value: Any) -> str:
    """Render numeric price levels consistently to two decimals."""
    number = parse_float(value)
    if number is None:
        return str(value or "offen")
    return f"{number:.2f}"


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



# General causal macro scenarios. These are hypotheses, not automatic trade signals.
SCENARIO_RULES = [
    {"id": "GOLD_REAL_YIELDS", "label": "Gold: Zins-/Dollar-Korrektur",
     "patterns": r"(realzins|real.?yield).*(steig|hoch)|((steig|hoch).*(realzins|real.?yield))|(starker? dollar|usd.*stärk)",
     "assets": ["gold", "xau", "silber", "silver", "goldminen", "gold miners"],
     "thesis": "Zins-/Dollar-Druck kann Gold zunächst belasten; nach Entspannung mögliche Erholung prüfen."},
    {"id": "GOLD_EASING", "label": "Gold/Silber: Zins- oder Dollar-Entspannung",
     "patterns": r"(realzins|real.?yield|fed|leitzins|dollar|usd).*(fall|sink|schwäch)|((fall|sink|schwäch).*(realzins|real.?yield|dollar|usd))",
     "assets": ["gold", "xau", "silber", "silver", "goldminen", "gold miners"],
     "thesis": "Nachlassender Zins-/Dollar-Druck kann Edelmetalle stützen; technische Bestätigung abwarten."},
    {"id": "ENERGY_SUPPLY", "label": "Energie: Angebots-/Preisimpuls",
     "patterns": r"(öl|oil|gas|energie|energy).*(angebot|förder|produktion|preis|versorgung)|((angebot|förder|produktion|versorgung).*(öl|oil|gas|energie))",
     "assets": ["öl", "oil", "gas", "energie", "energy", "ölerzeuger", "oil producers", "energy stocks"],
     "thesis": "Angebots- oder Preisänderungen können Energieanlagen unterschiedlich beeinflussen; Richtung anhand bestätigter Daten klären."},
    {"id": "INFLATION_RATES", "label": "Inflation/Zinsen: Bewertungsrotation",
     "patterns": r"(inflation|preisdrück|teuerung).*(steig|fall|abkühl|beschleun)|((zins|yield|rendite).*(steig|fall|hoch|sink))",
     "assets": ["anleihen", "bonds", "duration", "growth", "technologie", "technology", "banken", "bank"],
     "thesis": "Inflations- und Zinsänderungen können Bewertungs- und Sektorrotation auslösen; Gewinner/Verlierer datenbasiert prüfen."},
    {"id": "RISK_OFF", "label": "Risk-off: defensive/Absicherungs-Chance",
     "patterns": r"(risikoaversion|risk.?off|marktstress|volatilität|credit.?spread|kreditrisik).*(steig|hoch|zunehm)|((steig|hoch|zunehm).*(volatilität|credit.?spread|kreditrisik))",
     "assets": ["gold", "xau", "defensiv", "utilities", "gesundheit", "healthcare", "staatsanleihen", "treasuries"],
     "thesis": "Bei zunehmendem Stress defensive oder absichernde Assets beobachten; Gegenbewegung und Liquiditätsrisiken beachten."},
    {"id": "CYCLICAL_RECOVERY", "label": "Konjunktur: zyklische Erholung",
     "patterns": r"(pmi|industrie|manufacturing|auftragseingang|konjunktur).*(erhol|verbesser|expansion|steig)|((erhol|verbesser|expansion|steig).*(pmi|industrie|manufacturing|auftragseingang|konjunktur))",
     "assets": ["zyklisch", "industrials", "industrie", "materialien", "materials", "transport", "logistik"],
     "thesis": "Frühe Konjunkturverbesserung kann zyklische Branchen begünstigen; Trend- und Nachfragebestätigung prüfen."},
]

def detect_opportunity_scenarios(macro_text: str, structure_text: str = "", candidates: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Find causal macro/structure hypotheses and map them to relevant candidate assets.

    The rule engine intentionally emits watch hypotheses only. It does not invent
    entry/stop/target levels or upgrade a candidate to an active trade.
    """
    text = normalize_text(macro_text + "\n" + structure_text)
    lines = [normalize_text(line) for line in (macro_text + "\n" + structure_text).splitlines() if normalize_text(line)]
    candidates = candidates or []
    found: list[dict[str, Any]] = []
    for rule in SCENARIO_RULES:
        evidence = [line for line in lines if re.search(rule["patterns"], line, re.I)]
        if not evidence:
            continue
        assets = rule["assets"]
        matched = []
        for item in candidates:
            identity = normalize_text(" ".join([company_label(item), ticker_of(item), first(item, "Sektor", "Sector", "Branche", "Industry"), thesis_of(item)]))
            if any(re.search(re.escape(asset), identity, re.I) for asset in assets):
                matched.append(item)
        found.append({"id": rule["id"], "label": rule["label"], "thesis": rule["thesis"],
                     "evidence": [clean_macro_text(x, 220) for x in evidence[:3]],
                     "assets": assets, "matched_candidates": matched,
                     "status": "FRÜHINDIKATION"})
    return found

def technical_entry_readiness(item: dict[str, Any]) -> dict[str, str]:
    """Describe readiness from sourced technical fields; never synthesize price levels.

    Entry/stop/target are copied from source. Entry readiness requires a valid
    source status, CRV1 >= 1, an entry and a completed confirmation field.
    """
    status, entry, stop, target = technical_trigger(item)
    crv = crv_of(item)
    confirmation = first(item, "Bestätigung", "Bestaetigung", "Confirmation", "Entry_Bestaetigung", "Trigger_Bestaetigt")
    source_status = status_of(item).upper()
    source_valid = bool(re.match(r"^VALIDE(?:\s+SETUP)?$", source_status))
    confirmed = bool(confirmation and re.search(r"(ja|true|bestätigt|bestaetigt|confirmed|close|schlusskurs)", confirmation, re.I))
    if not entry or entry.lower() in {"offen", "n.v.", "keine"}:
        readiness = "FRÜHINDIKATION – TECHNISCHE ZONE NICHT AUS QUELLE ABLEITBAR"
    elif crv is None or crv < 1:
        readiness = "EINSTIEGSZONE ERREICHT – CRV1 NICHT FREIGEGEBEN"
    elif source_valid and confirmed:
        readiness = "EINSTIEG BESTÄTIGT – TECHNISCHE REGELN ERFÜLLT"
    elif source_valid:
        readiness = "EINSTIEGSZONE ERREICHT – BESTÄTIGUNG OFFEN"
    else:
        readiness = "EINSTIEGSZONE ERREICHT – QUELLSTATUS/BESTÄTIGUNG PRÜFEN"
    return {"readiness": readiness, "entry": entry, "stop": stop, "target": target,
            "crv1": f"{crv:.2f}" if crv is not None else "offen",
            "confirmation": confirmation or "Keine abgeschlossene Bestätigung in Quelle"}

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


def macro_relevance(item: dict[str, Any], macro_text: str, structure_text: str) -> tuple[str, str]:
    """Return compact macro label and rationale only for evidence tied to title/sector."""
    name = first(item, "Name", "name", "Unternehmen", "Company")
    ticker = ticker_of(item)
    sector = first(item, "Sektor", "Sector", "Branche", "Industry")
    terms = [x for x in (name, ticker, sector) if x]
    if not terms:
        return "OFFEN", "Keine Aktie-/Sektorzuordnung in den Quelldaten."
    source_lines = [normalize_text(x) for x in (macro_text + "\n" + structure_text).splitlines() if normalize_text(x)]
    matches = [line for line in source_lines if any(re.search(re.escape(term), line, re.I) for term in terms)]
    if not matches:
        return "OFFEN", "Kein belegter direkter Makro-/Sektorbezug."
    joined = clean_macro_text(" ".join(matches[:2]), limit=150)
    if not joined:
        return "OFFEN", "Kein verwertbarer direkter Makro-/Sektorbezug."
    if re.search(r"(gegenwind|bear|negativ|risiko|belast|steigende? zins|schwäch)", joined, re.I):
        return "GEGENWIND", joined
    if re.search(r"(rückenwind|bull|positiv|profitiert|stärk|sinkende? zins|nachfrage steigt|steigende? nachfrage|investitionen nehmen zu|auftragseingang steigt|wachstum beschleunigt)", joined, re.I):
        return "RÜCKENWIND", joined
    return "NEUTRAL", joined


def technical_trigger(item: dict[str, Any]) -> tuple[str, str, str, str]:
    """Read levels/trigger from input only; never invent them."""
    entry = first(item, "Entry", "Einstieg", "Entry_Zone", "Einstiegszone", "Trigger", "Bedingung")
    stop = first(item, "Stop", "Stop_Loss", "SL")
    target = first(item, "TP1", "Ziel", "Target", "Kursziel")
    setup = setup_of(item)
    status = status_of(item).upper()
    crv = crv_of(item)
    valid_source_status = bool(re.match(r"^VALIDE(?:\s+SETUP)?$", status))
    valid = valid_source_status and crv is not None and crv >= 1 and bool(entry)
    if valid:
        decision = "AKTIV"
    elif re.search(r"(VORBEREIT|WATCH|BEOBACHT|IN ENTWICKLUNG|ENTWICKL)", status, re.I) or (setup and setup != "Setup nicht angegeben"):
        decision = "FRÜHINDIKATION" if not entry else "IN ENTWICKLUNG"
    else:
        decision = "OFFEN"
    return decision, entry or "offen", stop or "offen", target or "offen"


def build_report(folder: Path, output: Path | None = None, today: date | None = None) -> str:
    """Build a compact macro+technical trading decision sheet with explicit maturity status."""
    folder = Path(folder)
    today = today or date.today()
    output = output or folder / f"Investment_Radar_{today.isoformat()}.txt"
    macro = newest(folder, "Makro_Briefing(*).txt")
    structure = newest(folder, "Struktur_Trend_Briefing(*).txt")
    macro_text, structure_text = read_text(macro), read_text(structure)
    picks = make_candidate_pool(folder)
    rows = []
    for item in picks:
        ticker = ticker_of(item)
        label = company_label(item)
        setup = setup_of(item)
        crv = crv_of(item)
        status, entry, stop, target = technical_trigger(item)
        macro_status, macro_reason = macro_relevance(item, macro_text, structure_text)
        # Early opportunities stay visible; macro alone cannot turn them into an active trade.
        if status == "AKTIV" and macro_status == "GEGENWIND":
            decision = "AKTIV – MAKRO-GEGENWIND"
        elif status == "AKTIV":
            decision = "AKTIV"
        elif status in {"FRÜHINDIKATION", "IN ENTWICKLUNG"}:
            decision = status
        else:
            decision = "PRÜFEN"
        rows.append({"ticker": ticker, "label": label, "decision": decision, "setup": setup,
                    "crv": crv, "macro": macro_status, "macro_reason": macro_reason,
                    "entry": entry, "stop": stop, "target": target,
                    "technical_status": status, "source_status": status_of(item),
                    "thesis": thesis_of(item), "trigger": condition_of(item)})

    # Prioritize active trade candidates, then emerging opportunities, then CRV.
    rank = {"AKTIV": 0, "AKTIV – MAKRO-GEGENWIND": 1, "IN ENTWICKLUNG": 2, "FRÜHINDIKATION": 3, "PRÜFEN": 4}
    rows.sort(key=lambda r: (rank.get(r["decision"], 5), -(r["crv"] if r["crv"] is not None else -1)))
    lines = [
        f"INVESTMENT RADAR | {today.isoformat()}",
        "Makro + Technik | FRÜHINDIKATION = mögliche Chance; EINSTIEGSZONE ERREICHT = Zone berührt, Bestätigung offen; EINSTIEG BESTÄTIGT = technische Regeln erfüllt.",
        "Preise aus Quellen; Makro allein ist kein Einstiegssignal.",
        "",
    ]
    if not rows:
        lines.append("Keine Kandidaten mit verwertbaren Setup-/Thesedaten.")
    for r in rows:
        crv_text = f"{r['crv']:.2f}" if r["crv"] is not None else "offen"
        macro_reason = clean_macro_text(r["macro_reason"], limit=125) or "Bezug offen"
        lines.append(
            f"{r['decision']} | {r['label']} | {r['setup']} | CRV1 {crv_text}"
            + (f" | Quellstatus: {r['source_status']}" if r["source_status"] else "")
        )
        lines.append(
            f"Makro {r['macro']}: {macro_reason} | "
            f"Entry {format_price(r['entry'])} | Stop {format_price(r['stop'])} | Ziel {format_price(r['target'])}"
        )
        trigger = normalize_text(r["trigger"])
        if trigger and trigger != r["entry"] and trigger.lower() not in {"offen", "n.v.", "keine"}:
            lines.append(f"Trigger: {clean_macro_text(trigger, limit=120)}")
        lines.append("")
    scenarios = detect_opportunity_scenarios(macro_text, structure_text, picks)
    if scenarios:
        lines.extend(["", "FRÜHE CHANCEN | KAUSALKETTEN",
                     "Szenarien sind Hypothesen; technische Freigabe bleibt separat."])
        for scenario in scenarios:
            lines.append(f"{scenario['status']} | {scenario['label']}")
            evidence_text = " | ".join(scenario["evidence"]) if scenario["evidence"] else "Keine konkrete Evidenz aus den Briefings erkannt."
            # Preserve any source/date tokens present in the input evidence. Never invent a date/source.
            lines.append(f"1. DATENLAGE (Quelle/Datenstand aus Briefing): {evidence_text}")
            lines.append(f"2. KAUSALKETTE: {scenario['thesis']}")
            lines.append("3. ASSET-WIRKUNG: " + ", ".join(scenario["assets"]) + " (mögliche Betroffenheit; Richtung asset-spezifisch prüfen)")
            lines.append("4. FRÜHINDIKATION: Beobachtungsthese, noch kein Kaufsignal; Evidenz siehe Datenlage.")
            lines.append("5. TECHNISCHE BEREITSCHAFT: Einstieg/Stop/Ziel nur aus vorhandener technischer Quelle; Bestätigung separat prüfen.")
            lines.append("6. WIDERLEGUNG: These verwerfen, wenn Folgedaten die Ausgangsannahme nicht bestätigen oder die erwartete Asset-Wirkung ausbleibt.")
            if scenario["matched_candidates"]:
                for item in scenario["matched_candidates"][:8]:
                    readiness = technical_entry_readiness(item)
                    lines.append(f"Asset: {company_label(item)} | {readiness['readiness']} | Entry/Zonenquelle: {format_price(readiness['entry'])} | Stop: {format_price(readiness['stop'])} | Ziel: {format_price(readiness['target'])} | CRV1: {readiness['crv1']}")
                    lines.append(f"Bestätigung: {readiness['confirmation']}")
            else:
                lines.append("Technischer Einstieg: noch kein passender Setup-Datensatz zugeordnet; Zone/Entry/Stop/Ziel nicht erfinden. Asset-Universum bzw. technische Quellen ergänzen.")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
