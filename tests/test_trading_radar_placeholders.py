from trading_radar_placeholders import is_placeholder_value


def test_shared_placeholder_classifier_covers_m05_values_and_annotations():
    placeholders = ["", "-", "—", "NICHT VERFÜGBAR", "NICHT VERFÜGBAR (keine Quelle)", "NICHT ANGEGEBEN", "NICHT ERMITTELT", "KEINE DATEN", "KEINE KONKRETEN DATEN", "NICHT BELEGT", "NICHT SEPARAT AUSGEWIESEN", "NICHT VORHANDEN", "NICHT VERIFIZIERBAR", "FEHLT", "UNBEKANNT", "UNKLAR", "TBD", "TBA", "TODO", "XXX", "N/A", "NA", "PLACEHOLDER", "PLATZHALTER"]
    assert all(is_placeholder_value(value) for value in placeholders)


def test_shared_placeholder_classifier_preserves_valid_values_and_narrative():
    valid_values = ["T-2026-01", "Version 2", "BESTÄTIGT", "NICHT VERFÜGBARKEIT verbessert sich", "Daten verfügbar; keine Lücken", "Keine Datenlücke im aktuellen Bericht"]
    assert not any(is_placeholder_value(value) for value in valid_values)


def test_shared_placeholder_classifier_is_whitespace_and_case_insensitive():
    assert is_placeholder_value("  nicht   angegeben. ")
    assert is_placeholder_value("Nicht verfügbar (Begründung: Quelle fehlt)")


def test_normalizer_uses_shared_placeholder_classifier_for_annotated_alternative():
    import json
    from trading_radar_neuauswertung import normalize_market_first_report_structure

    thesis_id = "m05-placeholder-test"
    alternative = "Restriktive Finanzierungskonditionen belasten zyklische Aktien stärker."
    report = (
        "1. Trading-Radar\nMarktregime: konstruktiv.\n"
        "2. Thesen-Update\nThesen-ID: " + thesis_id + "\n"
        "Alternative: NICHT VERFÜGBAR (im Rohbericht nicht angegeben)\n"
        "Status: BESTÄTIGT\nGegenbelege: Gegenbeleg dokumentiert.\n"
        "Bestätigungstrigger: Renditen steigen.\nInvalidierungstrigger: Renditen fallen.\n"
        "```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps({"schema_version": 1, "theses": [{"thesis_id": thesis_id, "alternative": alternative}]}, ensure_ascii=False)
        + "\n```\n3. Ereignisradar\nKeine Ereignisse.\n"
    )
    normalized = normalize_market_first_report_structure(report)
    assert "Alternative: " + alternative in normalized


def test_shared_placeholder_rules_are_scoped_to_m05_and_do_not_expand_m09():
    from trading_radar_neuauswertung import audit_market_first_content

    report = (
        "1. Trading-Radar\nMarktregime: konstruktiv.\n"
        "2. Thesen-Update\n"
        "These 1:\nThesen-ID: T-1\n"
        "Gegenbelege: NICHT ANGEGEBEN (keine Quellenangabe)\n"
        "Alternative: eine plausible Gegenhypothese\n"
        "Bestätigungstrigger: Renditen steigen\n"
        "Invalidierungstrigger: Renditen fallen\n"
        "3. Ereignisradar\nKeine Ereignisse.\n"
        "4. Assetklassenvergleich\nVergleich.\n"
        "5. Technische Kandidaten\n"
        "JETZT TECHNISCH PRÜFEN:\n"
        "Asset: AAA\n"
        "Trigger: NICHT ANGEGEBEN (keine Quelle)\n"
        "Gegenargument: Trendstruktur bleibt intakt\n"
        "Regelstatus: valide\n"
        "Statusgruppe: JETZT TECHNISCH PRÜFEN\n"
        "AUF TRIGGER WARTEN:\nKeine Kandidaten\n"
        "EREIGNIS ABWARTEN:\nKeine Kandidaten\n"
        "NICHT HANDELBAR:\nKeine Kandidaten\n"
        "6. Portfolio & Risiko\nPortfolio.\n"
        "7. MSCI-Vergleich\nBenchmark.\n"
        "8. Daten-/Prüfanhang\nPrüfung.\n"
    )

    placeholder = "NICHT ANGEGEBEN (keine Quellenangabe)"
    replacements = {
        "Gegenbelege": ("Gegenbelege: " + placeholder, "Gegenbelege: " + placeholder),
        "Alternative": ("Alternative: eine plausible Gegenhypothese", "Alternative: " + placeholder),
        "Bestätigungstrigger": ("Bestätigungstrigger: Renditen steigen", "Bestätigungstrigger: " + placeholder),
        "Invalidierungstrigger": ("Invalidierungstrigger: Renditen fallen", "Invalidierungstrigger: " + placeholder),
    }
    for field, (old_value, new_value) in replacements.items():
        field_report = report.replace(old_value, new_value, 1)
        errors = audit_market_first_content(field_report)
        assert any(
            f"These 1: Pflichtfeld '{field}' fehlt oder ist unbelegt" in error
            for error in errors
        ), (field, errors)

    errors = audit_market_first_content(report)
    assert not any(
        "Kandidat 1: Pflichtfeld 'Trigger' fehlt oder ist unbelegt" in error
        for error in errors
    ), errors


def test_m05_normalizer_does_not_match_placeholder_ids_to_structured_theses():
    import json
    from trading_radar_neuauswertung import normalize_market_first_report_structure

    report = (
        "1. Trading-Radar\nMarktregime: konstruktiv.\n"
        "2. Thesen-Update\nThesen-ID: NICHT ANGEGEBEN\nAlternative: NICHT ANGEGEBEN\n"
        "```TRADING_RADAR_THESEN_JSON\n"
        + json.dumps({
            "schema_version": 1,
            "theses": [{
                "thesis_id": "NICHT ANGEGEBEN",
                "version": 1,
                "status": "BESTÄTIGT",
                "alternative": "JSON alternative must not be injected",
            }],
        })
        + "\n```\n3. Ereignisradar\nKeine Ereignisse.\n"
    )
    normalized = normalize_market_first_report_structure(report)
    assert "Alternative: NICHT ANGEGEBEN" in normalized
    assert "Alternative: JSON alternative must not be injected" not in normalized
