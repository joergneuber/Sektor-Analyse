import json

from trading_radar_neuauswertung import (
    audit_market_first_content,
    normalize_market_first_report_structure,
)


THESIS_ID = "marktregime-zinsen-2026"
ALTERNATIVE = (
    "Anhaltend restriktive Zinspolitik der Federal Reserve führt zu einer "
    "abrupten Ausweitung der Kreditaufschläge im Hochzinsbereich."
)


def _report(theses, header="These-Name nicht aufgelöst (ID): " + THESIS_ID,
            existing_alternative=None, include_compact=True):
    fields = [
        "Status: BESTÄTIGT",
        "Gegenbelege: Sinkende Renditen an kurzen Fronten bei gleichzeitig robuster US-Konjunktur.",
    ]
    if existing_alternative is not None:
        fields.append("Alternative: " + existing_alternative)
    fields.extend([
        "Bestätigungstrigger: 10-jährige US-Staatsanleihen verbleiben über 5.00 Prozent.",
        "Invalidierungstrigger: Nachhaltiger Rückgang des 10-jährigen US-Treasury-Zins unter 4.50 Prozent.",
    ])
    compact = ""
    if include_compact:
        compact = (
            "\nThesenregister – kompakte Übersicht:\n"
            f"- {THESIS_ID} | Version 1 | Status BESTÄTIGT | Konfidenz mittel\n"
            "  - These: Das makroökonomische Umfeld bleibt restriktiv und stabil.\n"
            f"  - Alternative: {ALTERNATIVE}\n"
            "  - Gegenbelege: Sinkende Renditen an kurzen Fronten.\n"
            "  - Bestätigungstrigger: Renditen bleiben hoch.\n"
            "  - Invalidierungstrigger: Renditen fallen nachhaltig.\n"
        )
    payload = {"schema_version": 1, "theses": theses}
    return (
        "1. Trading-Radar\n"
        "Indizes/Marktbreite: S&P 500 1.2%; Nasdaq 1.4%; Russell 0.8%\n"
        "Zinsen/Renditen: US Treasury Rendite 5.04%\n"
        "Dollar/Liquidität/Volatilität: DXY 101.2; VIX 18.4\n"
        "Marktregime: Restriktives Zinsregime mit robuster Konjunktur.\n"
        "Basisszenario: Robuste US-Konjunktur bei restriktiver Geldpolitik.\n"
        "2. Thesen-Update\n"
        + header + "\n"
        + "\n".join(fields) + "\n"
        + compact
        + "```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```\n"
        "3. Ereignisradar\nKeine Ereignisse.\n"
        "4. Assetklassenvergleich\nRelative Stärke und Kapitalrotation: Aktien stärker als Anleihen.\n"
        "5. Technische Kandidaten\n"
        "JETZT TECHNISCH PRÜFEN: Keine Kandidaten\n"
        "AUF TRIGGER WARTEN: Keine Kandidaten\n"
        "EREIGNIS ABWARTEN: Keine Kandidaten\n"
        "NICHT HANDELBAR: Keine Kandidaten\n"
        "6. Portfolio & Risiko\nRisiko kontrolliert.\n"
        "7. MSCI-Vergleich\nBenchmark stabil.\n"
        "8. Daten-/Prüfanhang\nDaten vollständig.\n"
    )


def _thesis(alternative=ALTERNATIVE, thesis_id=THESIS_ID):
    return {
        "thesis_id": thesis_id,
        "alternative": alternative,
        "counter_evidence": ["Sinkende Renditen an kurzen Fronten."],
        "confirm_triggers": ["Renditen bleiben hoch."],
        "invalidate_triggers": ["Renditen fallen nachhaltig."],
    }


def test_run1327_missing_primary_alternative_is_promoted_by_exact_thesis_id():
    report = _report([_thesis()])
    normalized = normalize_market_first_report_structure(report)
    primary = normalized.split("Thesenregister – kompakte Übersicht:", 1)[0]
    assert "Alternative: " + ALTERNATIVE in primary
    assert "MARKT-FIRST: These 1: Pflichtfeld 'Alternative'" not in "\n".join(
        audit_market_first_content(normalized)
    )


def test_run1327_does_not_invent_alternative_when_register_field_is_missing():
    report = _report([_thesis(alternative="")])
    normalized = normalize_market_first_report_structure(report)
    primary = normalized.split("Thesenregister – kompakte Übersicht:", 1)[0]
    assert "Alternative:" not in primary
    assert any(
        "Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in finding
        for finding in audit_market_first_content(normalized)
    )


def test_run1327_alternative_is_never_borrowed_from_a_different_thesis_id():
    report = _report([_thesis(thesis_id="another-thesis")])
    normalized = normalize_market_first_report_structure(report)
    primary = normalized.split("Thesenregister – kompakte Übersicht:", 1)[0]
    assert "Alternative: " + ALTERNATIVE not in primary
    assert any(
        "Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in finding
        for finding in audit_market_first_content(normalized)
    )


def test_existing_primary_alternative_is_not_overwritten():
    original = "Already documented alternative."
    report = _report([_thesis()], existing_alternative=original)
    normalized = normalize_market_first_report_structure(report)
    primary = normalized.split("Thesenregister – kompakte Übersicht:", 1)[0]
    assert "Alternative: " + original in primary
    assert "Alternative: " + ALTERNATIVE not in primary


def test_compact_register_these_label_is_not_a_second_primary_record():
    report = _report([_thesis()], include_compact=True)
    # Without normalization the primary record is incomplete; the compact
    # appendix's complete field set must not mask that defect.
    findings = audit_market_first_content(report)
    assert any("Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in x for x in findings)
    assert not any("These 2:" in x for x in findings)


def test_primary_alternative_survives_readable_report_compaction():
    import Trading_Radar_Alleinauswertung as production

    report = _report([_thesis()])
    normalized = normalize_market_first_report_structure(report)
    compacted = production._trading_radar_lesefassung_komprimieren(normalized)
    primary = compacted.split("Thesenregister – kompakte Übersicht:", 1)[0]
    assert "Alternative: " + ALTERNATIVE in primary
    assert not any(
        "Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in finding
        for finding in audit_market_first_content(compacted)
    )


def test_compact_register_is_excluded_from_primary_record_boundaries():
    report = _report([_thesis()], include_compact=True)
    # Make the secondary rendering incomplete as well: it must not become a
    # second independently audited thesis record.
    report = report.replace("  - Alternative: " + ALTERNATIVE + "\n", "", 1)
    findings = audit_market_first_content(report)
    alternative_findings = [
        item for item in findings if "Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in item
    ]
    assert alternative_findings == [
        "MARKT-FIRST: These 1: Pflichtfeld 'Alternative' fehlt oder ist unbelegt"
    ]


def test_thesis_id_before_these_field_is_one_record_and_receives_its_alternative():
    report = _report([_thesis()], header="Thesen-ID: " + THESIS_ID)
    report = report.replace(
        "Thesen-ID: " + THESIS_ID + "\nStatus:",
        "Thesen-ID: " + THESIS_ID + "\n"
        "These: Das Zinsregime bleibt restriktiv.\nStatus:",
        1,
    )
    normalized = normalize_market_first_report_structure(report)
    primary = normalized.split("Thesenregister – kompakte Übersicht:", 1)[0]
    assert "Thesen-ID: " + THESIS_ID in primary
    assert "Alternative: " + ALTERNATIVE in primary
    assert primary.index("Alternative: " + ALTERNATIVE) < primary.index(
        "These: Das Zinsregime bleibt restriktiv."
    )
    assert not any(
        "Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in finding
        for finding in audit_market_first_content(normalized)
    )


def test_compaction_keeps_section_offsets_valid_when_duplicate_precedes_section_two():
    import Trading_Radar_Alleinauswertung as production

    report = _report([_thesis()])
    report = report.replace(
        "2. Thesen-Update\n",
        "Alternative: " + ALTERNATIVE + "\n"
        "2. Thesen-Update\n",
        1,
    )
    normalized = normalize_market_first_report_structure(report)
    compacted = production._trading_radar_lesefassung_komprimieren(normalized)

    assert "1. Trading-Radar" in compacted
    assert "2. Thesen-Update" in compacted
    assert "3. Ereignisradar" in compacted
    assert "2. Thesen-Update3. Ereignisradar" not in compacted
    primary = compacted.split("Thesenregister – kompakte Übersicht:", 1)[0]
    assert "Alternative: " + ALTERNATIVE in primary
    assert not any(
        "Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in finding
        for finding in audit_market_first_content(compacted)
    )


def test_pipe_delimited_metadata_after_thesis_id_does_not_break_alternative_lookup():
    header = (
        "Thesen-ID: " + THESIS_ID
        + " | Version: 2 | Status: BESTÄTIGT | Konfidenz: mittel"
    )
    report = _report([_thesis()], header=header)
    normalized = normalize_market_first_report_structure(report)
    primary = normalized.split("Thesenregister – kompakte Übersicht:", 1)[0]

    assert "Thesen-ID: " + THESIS_ID + " | Version: 2" in primary
    assert "Alternative: " + ALTERNATIVE in primary
    assert not any(
        "Pflichtfeld 'Alternative' fehlt oder ist unbelegt" in finding
        for finding in audit_market_first_content(normalized)
    )

