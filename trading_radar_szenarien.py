"""Deterministic scenario construction from measured market indicators."""
from __future__ import annotations

import datetime
import math
import re

from trading_radar_report_gate import _evaluate_scenario_entry


def trading_radar_szenarien_ergänzen(text: str) -> str:
    """Build all three scenarios from measurable inputs and derive their status.

    A missing narrative horizon is not a missing market indicator. It is stated
    transparently without inventing a duration, while the measurable entry
    conditions are still evaluated. Missing metrics used by a condition remain
    NICHT ABLEITBAR and are named explicitly.
    """
    if not isinstance(text, str) or not text:
        return text
    section = re.search(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?1\s*[.·]\s*Trading-Radar[^\r\n]*$", text,
    )
    if not section:
        return text
    tail = text[section.end():]
    next_section = re.search(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?2\s*[.·]\s*Thesen-Update[^\r\n]*$", tail,
    )
    if not next_section:
        return text
    end = section.end() + next_section.start()
    body = text[section.end():end]
    rotation = re.search(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?4\s*[.·]\s*Assetklassenvergleich[^\r\n]*$", text,
    )
    rotation_body = ""
    if rotation:
        rotation_tail = text[rotation.end():]
        next_rotation = re.search(
            r"(?im)^[ \t]*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten[^\r\n]*$",
            rotation_tail,
        )
        rotation_body = rotation_tail[:next_rotation.start()] if next_rotation else rotation_tail
    cutoff_match = re.search(
        r"(?im)^[ \t]*(?:Gemeinsamer\s+)?Datenstichtag\s*[:=]\s*"
        r"(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?\s*$",
        rotation_body,
    )
    common_cutoff = cutoff_match.group(1) if cutoff_match else None
    if common_cutoff is not None:
        try:
            datetime.date.fromisoformat(common_cutoff)
        except ValueError:
            common_cutoff = None

    body = re.sub(
        r"(?im)^\s*(?:Positives Alternativszenario|Negatives Alternativszenario):[^\r\n]*(?:\r?\n(?!\s*\d+\s*[.·])[^\r\n]*)*",
        "", body,
    )

    def number(pattern: str, source_text: str) -> float | None:
        match = re.search(pattern, source_text, re.I | re.M)
        if not match:
            return None
        try:
            value = float(match.group(1).replace(",", "."))
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    breadth_match = re.search(
        r"(?i)(?:Marktbreite(?:-Proxy)?[^:\r\n]*:\s*|Quantitative Marktbreite:\s*)(\d+)\s*/\s*(\d+)",
        body,
    )
    breadth = None
    if breadth_match:
        numerator, denominator = map(int, breadth_match.groups())
        if denominator > 0 and 0 <= numerator <= denominator:
            breadth = (numerator, denominator)
    xlv = number(
        r"(?i)(?:Gesundheit-ETF|XLV)\s*\(XLV\)\s*1M\s*[+-]?[0-9]+(?:[.,][0-9]+)?%?\s*;\s*3M\s*([+-]?[0-9]+(?:[.,][0-9]+)?)%",
        rotation_body,
    )
    xle = number(
        r"(?i)(?:Energie-ETF|XLE)\s*\(XLE\)\s*1M\s*[+-]?[0-9]+(?:[.,][0-9]+)?%?\s*;\s*3M\s*([+-]?[0-9]+(?:[.,][0-9]+)?)%",
        rotation_body,
    )
    vix = number(r"(?im)^\s*[-*•]?\s*Volatilität VIX:\s*([0-9]+(?:[.,][0-9]+)?)", body)
    horizon_match = re.search(r"(?im)^\s*(?:[-*•]\s*)?Zeithorizont:\s*([^\r\n]+)", body)
    horizon = (
        horizon_match.group(1).strip() if horizon_match
        else "nicht festgelegt (kein belastbarer Zeitraum aus den Quellen ableitbar)"
    )

    baseline = re.search(
        r"(?im)^([ \t]*(?:[-*•]\s*)?(?:\*\*)?Basisszenario(?:\*\*)?\s*:[^\r\n]*)$",
        body,
    )
    breadth_label = f"{breadth[0]}/{breadth[1]}" if breadth is not None else None
    breadth_indicator = breadth_label if breadth_label is not None else "NICHT VERFÜGBAR"
    xlv_label = f"{xlv:+.2f}%" if xlv is not None else "NICHT VERFÜGBAR"
    xle_label = f"{xle:+.2f}%" if xle is not None else "NICHT VERFÜGBAR"
    vix_label = f"{vix:.2f}" if vix is not None else "NICHT VERFÜGBAR"
    cutoff_label = common_cutoff if common_cutoff is not None else "NICHT VERFÜGBAR"
    indicators = (
        f"Marktbreite {breadth_indicator}; Gesundheit-ETF (XLV) 3M {xlv_label}; "
        f"Energie-ETF (XLE) 3M {xle_label}; Gemeinsamer Datenstichtag: {cutoff_label}."
    )

    breadth_condition = (
        f"Marktbreite mindestens {breadth_label}"
        if breadth_label is not None
        else "Marktbreite muss messbar vorliegen"
    )
    baseline_entry = (
        f"{breadth_condition} und "
        "Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen positive 3M-Relative-Stärke auf"
    )
    positive_entry = baseline_entry
    negative_entry = (
        (
            f"Marktbreite unter {breadth_label} oder "
            if breadth_label is not None else ""
        )
        + "Gesundheit-ETF (XLV) bzw. Energie-ETF (XLE) weist "
        "eine nicht-positive 3M-Relative-Stärke auf"
    )

    missing_names = {
        "MARKTBREITE": "Marktbreite",
        "XLV": "XLV 3M-Relative-Stärke",
        "XLE": "XLE 3M-Relative-Stärke",
    }

    def scenario_status(display: str, expression: str) -> tuple[str, str]:
        evaluation = _evaluate_scenario_entry(expression, indicators)
        missing_texts = [
            missing_names.get(item, item) for item in evaluation.missing_metrics
        ]
        if breadth is None:
            missing_texts.append("Marktbreite")
        if xlv is None:
            missing_texts.append("XLV 3M-Relative-Stärke")
        if xle is None:
            missing_texts.append("XLE 3M-Relative-Stärke")
        if common_cutoff is None:
            missing_texts.append("gemeinsamer Datenstichtag")
        if evaluation.value is None or common_cutoff is None:
            status = "NICHT ABLEITBAR"
        elif display == "Negatives Alternativszenario" and evaluation.value:
            status, gap = "TRIGGER AUSGELÖST", ""
        else:
            status = "ERFÜLLT" if evaluation.value else "NICHT ERFÜLLT"
        gap = (
            " Datenlücke: " + ", ".join(dict.fromkeys(missing_texts)) + " fehlt."
            if missing_texts else ""
        )
        return status, gap

    all_primary_metrics_missing = breadth is None and xlv is None and xle is None
    unavailable_summary = (
        "NICHT ABLEITBAR – Datenlücke: Marktbreite, XLV 3M-Relative-Stärke, "
        "XLE 3M-Relative-Stärke"
        if all_primary_metrics_missing else None
    )
    baseline_text = baseline.group(1).split(":", 1)[1].strip() if baseline else ""
    if not baseline_text or re.match(r"(?i)^NICHT ABLEITBAR\b", baseline_text):
        regime_match = re.search(r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?Marktregime(?:\*\*)?\s*:\s*([^\r\n]+)", body)
        baseline_text = regime_match.group(1).strip() if regime_match else "Fortsetzung der gemessenen Marktbreite- und Sektorstruktur"
    baseline_text = baseline_text.rstrip(". ")
    baseline_text = re.sub(r"(?i)\bXLV\b(?!\s*\()", "Gesundheit-ETF (XLV)", baseline_text)
    baseline_text = re.sub(r"(?i)\bXLE\b(?!\s*\()", "Energie-ETF (XLE)", baseline_text)
    if unavailable_summary:
        baseline_text = unavailable_summary

    base_status, base_gap = scenario_status("Basisszenario", baseline_entry)
    positive_status, positive_gap = scenario_status("Positives Alternativszenario", positive_entry)
    negative_status, negative_gap = scenario_status("Negatives Alternativszenario", negative_entry)

    breadth_invalidation = (
        f"Marktbreite fällt unter {breadth_label}"
        if breadth_label is not None else "Marktbreite ist nicht messbar"
    )
    breadth_recovery = (
        f"Marktbreite erreicht wieder mindestens {breadth_label}"
        if breadth_label is not None else "Marktbreite wird wieder messbar"
    )
    common_fields = (
        f"Zeithorizont: {horizon}. "
        f"Indikatoren: {indicators} VIX {vix_label}. "
    )
    baseline_block = (
        f"Basisszenario: {baseline_text}. Eintrittsbedingung: {baseline_entry}. "
        f"Status: {base_status}. Bestätigungstrigger: {breadth_condition}; "
        "Gesundheit-ETF (XLV) und Energie-ETF (XLE) bleiben über 0,00%. "
        f"Invalidierungstrigger: {breadth_invalidation} oder "
        "Gesundheit-ETF (XLV) bzw. Energie-ETF (XLE) fällt auf 0,00% oder darunter. "
        + common_fields + base_gap + " Konsequenz: Marktregime weiter beobachten; technische Einzelfreigaben "
        "bleiben den Scanner- und CRV-Regeln vorbehalten."
    )
    positive = (
        "Positives Alternativszenario: "
        + (unavailable_summary + ". " if unavailable_summary else
           "Fortsetzung der beobachteten relativen Stärke von Gesundheit-ETF (XLV) und Energie-ETF (XLE). ")
        + f"Eintrittsbedingung: {positive_entry}. Status: {positive_status}. "
        "Bestätigungstrigger: Die drei Bedingungen bleiben zum nächsten gemeinsamen "
        f"Tagesdatenstichtag erfüllt. Invalidierungstrigger: {breadth_invalidation} "
        "oder Gesundheit-ETF (XLV) bzw. Energie-ETF (XLE) fällt auf 0,00% oder darunter. "
        + common_fields + positive_gap + " Konsequenz: keine technische Handelsfreigabe; Kandidaten bleiben den separaten "
        "Scanner- und CRV-Regeln unterworfen."
    )
    negative = (
        "Negatives Alternativszenario: "
        + (unavailable_summary + ". " if unavailable_summary else
           "Abschwächung der aktuellen Sektorrotation. ")
        + f"Eintrittsbedingung: {negative_entry}. Status: {negative_status}. "
        "Bestätigungstrigger: mindestens eine dieser gemessenen Bedingungen wird am "
        "nächsten gemeinsamen Tagesdatenstichtag bestätigt. "
        f"Invalidierungstrigger: {breadth_recovery} "
        "und Gesundheit-ETF (XLV) sowie Energie-ETF (XLE) weisen erneut positive 3M-Werte auf. "
        + common_fields + negative_gap + " Konsequenz: keine neuen technischen Freigaben aus dem Szenario ableiten; "
        "bestehende Positionen nach den jeweiligen Stop-Regeln führen."
    )
    addition = baseline_block + "\n\n" + positive + "\n\n" + negative + "\n\n"
    body = re.sub(
        r"(?im)^[ \t]*(?:[-*•]\s*)?(?:\*\*)?Basisszenario(?:\*\*)?\s*:[^\r\n]*$", "", body
    )
    body = body.rstrip() + "\n\n" + addition
    return text[:section.end()] + body + text[end:]
