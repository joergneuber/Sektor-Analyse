from pathlib import Path
from trading_radar_neuauswertung import RADAR_INSTRUCTION, audit_market_first_content


def test_prompt_declares_market_first_and_portfolio_last():
    assert RADAR_INSTRUCTION.index("GESAMTMARKT UND MARKTREGIME") < RADAR_INSTRUCTION.index("RELATIVE STAERKE")
    assert RADAR_INSTRUCTION.index("RELATIVE STAERKE") < RADAR_INSTRUCTION.index("THESEN, SZENARIEN")
    assert RADAR_INSTRUCTION.index("THESEN, SZENARIEN") < RADAR_INSTRUCTION.index("TECHNISCHE KANDIDATEN")
    assert RADAR_INSTRUCTION.index("TECHNISCHE KANDIDATEN") < RADAR_INSTRUCTION.index("PORTFOLIO-AUSWIRKUNG")


def test_market_first_audit_flags_missing_rotation_and_regime():
    report = """1 · Trading-Radar
Portfolio war schwach.
2 · Thesen-Update
Keine Thesen.
3 · Ereignisradar
Ein Ereignis.
4 · Assetklassenvergleich
Keine Daten.
5 · Technische Kandidaten
Keine Kandidaten.
6 · Portfolio & Risiko
Viele Positionen.
7 · MSCI-Vergleich
Vergleich.
8 · Daten-/Prüfanhang
Daten."""
    errors = audit_market_first_content(report)
    assert any("Marktregime" in e for e in errors)
    assert any("Kapitalrotation" in e for e in errors)


def test_market_first_audit_accepts_market_led_structure():
    # Keep this acceptance fixture aligned with the production gate contract:
    # individually labeled market fields, a complete thesis record, and all
    # four required candidate groups explicitly represented (empty is valid).
    report = """### 1. Trading-Radar
Indizes/Marktbreite: S&P 500 and Nasdaq breadth weaken.
Zinsen/Renditen: US 10Y Treasury yield rises to 4.2%.
Dollar/Liquidität/Volatilität: DXY 102.3 and VIX 18.4 rise; liquidity remains tight.
Marktregime: risk-off.
Basisszenario: defensive market conditions.
### 2. Thesen-Update
These T1:
Gegenbelege: Earnings remain resilient despite weak breadth.
Alternative: sideways market if volatility fades.
Bestätigungstrigger: breadth improves across several sessions.
Invalidierungstrigger: S&P 500 falls below the last confirmed swing low.
### 3. Ereignisradar
FOMC.
### 4. Assetklassenvergleich
Relative Stärke zeigt Rotation aus Growth in Energie; Gewinner und Verlierer divergieren.
### 5. Technische Kandidaten
### JETZT TECHNISCH PRÜFEN
Keine Kandidaten.
### AUF TRIGGER WARTEN
Keine Kandidaten.
### EREIGNIS ABWARTEN
Keine Kandidaten.
### NICHT HANDELBAR
Keine Kandidaten.
### 6. Portfolio & Risiko
Relevante Auswirkungen.
### 7. MSCI-Vergleich
Vergleich.
### 8. Daten-/Prüfanhang
Daten."""
    assert audit_market_first_content(report) == []


def test_market_first_normalizer_adds_evidence_labels_and_explicit_empty_groups():
    from trading_radar_neuauswertung import normalize_market_first_report_structure, audit_market_first_content

    report = """1. Trading-Radar
Hauptaussage: Eine flächendeckende aggressive Rally ist nicht tragfähig; disziplinierte Kapitalrotation dominiert. US 10Y Treasury bei 5,0000%, 10Y TIPS bei 2,6600%.

**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**
- Index S&P 500 (^GSPC): letzter Schluss 7722.60; 1M +0.67%; 3M +1.61%.
- Index Nasdaq Composite (^IXIC): letzter Schluss 26477.81; 1M -1.19%; 3M +2.18%.
- Index Russell 2000 (^RUT): letzter Schluss 2818.87; 1M +0.39%; 3M -5.47%.
- Volatilität VIX: 17.40
- Dollar-Proxy DXY (DX-Y.NYB): 102.32
- Anleihe-ETF TLT 1M: +4.77%.
- Marktbreite-Proxy: 7/11 Sektor-ETFs über ihrem 50-Tage-Durchschnitt.

2. Thesen-Update
- Keine neue oder aktualisierte These; Thesenregister geprüft, keine Änderung.

3. Ereignisradar
- Keine relevanten Ereignisse im geprüften Zeitraum.

4. Assetklassenvergleich
Relative Stärke und Kapitalrotation zeigen selektive Gewinner und Verlierer.

5. Technische Kandidaten
Keine belastbaren technischen Kandidaten; keine Handelsfreigabe.

6. Portfolio & Risiko
Portfolio bleibt getrennt von der Marktregimebewertung.
"""
    normalized = normalize_market_first_report_structure(report)
    assert "Indizes/Marktbreite:" in normalized
    assert "Zinsen/Renditen:" in normalized
    assert "Dollar/Liquidität/Volatilität:" in normalized
    assert "Marktregime:" in normalized
    assert "Basisszenario:" in normalized
    assert normalized.count("JETZT TECHNISCH PRÜFEN: Keine Kandidaten") == 1
    assert audit_market_first_content(normalized) == []


def test_reusable_prompt_requires_explicit_scenario_status_and_stable_active_thesis_ids():
    assert "Status: ERFÜLLT" in RADAR_INSTRUCTION
    assert "Status: NICHT ERFÜLLT" in RADAR_INSTRUCTION
    assert "unter derselben thesis_id" in RADAR_INSTRUCTION
    assert "replaced_by" in RADAR_INSTRUCTION


def test_all_production_and_retry_prompts_share_one_canonical_thesis_status_contract():
    from pathlib import Path
    from trading_radar_thesen import CANONICAL_STATUSES, VALID_STATUS

    root = Path(__file__).resolve().parents[1]
    main_prompt = (root / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    expected = (
        "NEU", "BESTÄTIGT", "ABGESCHWÄCHT",
        "WARTET AUF EREIGNIS", "INVALIDIERT", "ERSETZT",
    )
    assert CANONICAL_STATUSES == frozenset(expected)
    assert VALID_STATUS == CANONICAL_STATUSES | {
        "BESTAETIGT", "ABGESCHWAECHT", "WARTET_AUF_EVENT"
    }

    canonical_pipe = "|".join(expected)
    assert main_prompt.count(canonical_pipe) == 2  # production JSON + bounded retry JSON
    assert "Zulässige Status ausschließlich exakt: " + ", ".join(expected) in main_prompt
    normalized_prompt = " ".join(RADAR_INSTRUCTION.split())
    assert "Nur diese Thesenstatus verwenden: " + ", ".join(expected) in normalized_prompt
    assert "VERSTAERKT|ABGESCHWAECHT|WARTET_AUF_EVENT|WIDERLEGT|ERSETZT|UNKLAR" not in main_prompt
    assert "VERSTAERKT|ABGESCHWAECHT|WARTET_AUF_EVENT|WIDERLEGT|ERSETZT|UNKLAR" not in RADAR_INSTRUCTION


def test_replaced_by_is_required_in_every_production_and_retry_json_schema():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    main_prompt = (root / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    assert main_prompt.count('"replaced_by":null') == 1
    assert main_prompt.count(r'\"replaced_by\":null') == 2
    assert "replaced_by ist immer vorhanden" in main_prompt
    assert "replaced_by ist Pflicht" in main_prompt
    assert '"replaced_by":null' in RADAR_INSTRUCTION
    assert "sonst null" in RADAR_INSTRUCTION


def test_shared_register_validator_rejects_noncanonical_lifecycle_states():
    from trading_radar_neuauswertung import validate_thesis_register

    def item(status, replaced_by=None, thesis_id="T-1"):
        return {
            "thesis_id": thesis_id,
            "version": 1,
            "updated_at": "2026-11-02T08:11:03+00:00",
            "data_cutoff": "2026-11-02T08:11:03+00:00",
            "thesis": "Belegte These",
            "horizon": "4 Wochen",
            "alternative": "Gegenhypothese",
            "evidence": ["Beleg"],
            "counter_evidence": ["Gegenbeleg"],
            "confidence": "mittel",
            "confirm_triggers": ["Trigger"],
            "invalidate_triggers": ["Invalidierung"],
            "event_dependencies": [],
            "asset_classes": ["Aktien"],
            "candidates": [],
            "status": status,
            "replaced_by": replaced_by,
        }

    for invalid_status in ("VERSTAERKT", "WIDERLEGT", "UNKLAR"):
        payload = {
            "data_cutoff": "2026-11-02T08:11:03+00:00",
            "theses": [item(invalid_status)],
        }
        assert any("unzulaessiger Status" in error for error in validate_thesis_register(payload))


def test_shared_register_validator_enforces_replaced_by_relationship():
    from trading_radar_neuauswertung import validate_thesis_register

    def item(thesis_id, status, replaced_by):
        return {
            "thesis_id": thesis_id,
            "version": 1,
            "updated_at": "2026-11-02T08:11:03+00:00",
            "data_cutoff": "2026-11-02T08:11:03+00:00",
            "thesis": "Belegte These",
            "horizon": "4 Wochen",
            "alternative": "Gegenhypothese",
            "evidence": ["Beleg"],
            "counter_evidence": ["Gegenbeleg"],
            "confidence": "mittel",
            "confirm_triggers": ["Trigger"],
            "invalidate_triggers": ["Invalidierung"],
            "event_dependencies": [],
            "asset_classes": ["Aktien"],
            "candidates": [],
            "status": status,
            "replaced_by": replaced_by,
        }

    old = item("T-OLD", "ERSETZT", "T-NEW")
    active = item("T-NEW", "BESTÄTIGT", None)
    payload = {
        "data_cutoff": "2026-11-02T08:11:03+00:00",
        "theses": [old, active],
    }
    assert validate_thesis_register(payload) == []

    active["status"] = "INVALIDIERT"
    errors = validate_thesis_register(payload)
    assert any("aktive Nachfolger-ID" in error for error in errors)


def test_shared_register_validator_migrates_legacy_missing_replaced_by_to_null():
    from trading_radar_neuauswertung import validate_thesis_register

    thesis = {
        "thesis_id": "T-LEGACY",
        "version": 1,
        "updated_at": "2026-11-02T08:11:03+00:00",
        "data_cutoff": "2026-11-02T08:11:03+00:00",
        "thesis": "Belegte These",
        "horizon": "4 Wochen",
        "alternative": "Gegenhypothese",
        "evidence": ["Beleg"],
        "counter_evidence": ["Gegenbeleg"],
        "confidence": "mittel",
        "confirm_triggers": ["Trigger"],
        "invalidate_triggers": ["Invalidierung"],
        "event_dependencies": [],
        "asset_classes": ["Aktien"],
        "candidates": [],
        "status": "BESTÄTIGT",
    }
    payload = {"data_cutoff": thesis["data_cutoff"], "theses": [thesis]}
    assert validate_thesis_register(payload) == []
    assert thesis["replaced_by"] is None


def test_production_and_normalizer_prompts_describe_the_parser_supported_grammar():
    from pathlib import Path
    from trading_radar_neuauswertung import RADAR_INSTRUCTION

    root = Path(__file__).resolve().parents[1]
    main_prompt = (root / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    for prompt in (main_prompt, RADAR_INSTRUCTION):
        assert "Marktbreite" in prompt
        assert "XLV/XLE" in prompt
        assert "UND" in prompt and "ODER" in prompt
        assert "UND hat Vorrang vor ODER" in prompt or "UND bindet stärker als ODER" in prompt
        assert "Fail-Closed" in prompt or "Fail-Closed-Gate" in prompt
        assert "Marktbreite 8/11" in prompt
        assert "XLV 3M +1.84%" in prompt
        assert "XLE 3M -3.55%" in prompt
        assert "Datenlücke: XLV 3M-Relative-Stärke fehlt" in prompt


def test_shared_register_validator_normalizes_documented_ascii_status_aliases():
    from trading_radar_neuauswertung import validate_thesis_register

    thesis = {
        "thesis_id": "T-ALIAS",
        "version": 1,
        "updated_at": "2026-11-02T08:11:03+00:00",
        "data_cutoff": "2026-11-02T08:11:03+00:00",
        "thesis": "Belegte These",
        "horizon": "4 Wochen",
        "alternative": "Gegenhypothese",
        "evidence": ["Beleg"],
        "counter_evidence": ["Gegenbeleg"],
        "confidence": "mittel",
        "confirm_triggers": ["Trigger"],
        "invalidate_triggers": ["Invalidierung"],
        "event_dependencies": [],
        "asset_classes": ["Aktien"],
        "candidates": [],
        "status": "BESTAETIGT",
        "replaced_by": None,
    }
    payload = {"data_cutoff": thesis["data_cutoff"], "theses": [thesis]}
    assert validate_thesis_register(payload) == []
    assert thesis["status"] == "BESTÄTIGT"



def test_production_and_retry_prompts_forbid_archived_thesis_id_reactivation():
    import ast

    project_root = Path(__file__).resolve().parents[1]
    production_path = project_root / "Trading_Radar_Alleinauswertung.py"
    production = production_path.read_text(encoding="utf-8")
    normalization = (project_root / "trading_radar_neuauswertung.py").read_text(encoding="utf-8")
    for source in (production, normalization):
        assert "archivierte thesis_id" in source
        assert "niemals reaktiv" in source
        assert "deterministischen thesis_id" in source or "deterministische thesis_id" in source

    # Check the actual multi-stage final synthesis prompt assignment, not just
    # an unrelated prompt elsewhere in the same source file.
    tree = ast.parse(production)
    final_prompt_assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "final_prompt" for target in node.targets)
    ]
    assert len(final_prompt_assignments) == 1
    final_prompt_source = ast.get_source_segment(production, final_prompt_assignments[0])
    assert "ARCHIVSPERRE" in final_prompt_source
    assert "führenden oder nachgestellten Leerzeichen" in final_prompt_source
    assert "Version 1" in final_prompt_source

    # Standalone prompt and structural retry retain the same permanent lock.
    assert "archivierte thesis_id sind endgültig gesperrt" in production
    assert "Eine bereits archivierte thesis_id ist endgültig gesperrt" in RADAR_INSTRUCTION



def test_text_only_thesis_fallback_fails_closed_for_replaced_without_same_payload_successor():
    from trading_radar_neuauswertung import ensure_structured_thesis_block

    report = """1. Trading-Radar
Hauptthese: Eine bestehende These wird ersetzt.
Zeithorizont: 4 Wochen
Konfidenz: mittel
Stärkste Belege:
- Quelle A belegt die These.
Wichtigste Gegenbelege:
- Quelle B spricht gegen die These.
Belegte Top-Profiteur-Kandidaten:
- Aktien
Positives Alternativszenario: Alternative A.
Negatives Alternativszenario: Alternative B.

2. Thesen-Update
Thesen-ID: T-ALT
Version: 2
Status: ERSETZT
Bestätigungstrigger: Nachfolger übernimmt die These.
Invalidierungstrigger: Nachfolger wird widerlegt.
Gegenbelege: Quelle B.

3. Ereignisradar
Keine relevanten Ereignisse.
"""
    recovered, changed = ensure_structured_thesis_block(report)
    assert changed is False
    assert "```TRADING_RADAR_THESEN_JSON" not in recovered
    assert "Status: ERSETZT" in recovered


def test_shared_thesis_register_validator_rejects_surrounding_id_whitespace():
    from trading_radar_neuauswertung import validate_thesis_register

    thesis = {
        "thesis_id": "T-1 ",
        "version": 1,
        "updated_at": "2026-11-02T08:11:03+00:00",
        "data_cutoff": "2026-11-02T08:11:03+00:00",
        "thesis": "Belegte These",
        "horizon": "4 Wochen",
        "alternative": "Gegenhypothese",
        "evidence": ["Beleg"],
        "counter_evidence": ["Gegenbeleg"],
        "confidence": "mittel",
        "confirm_triggers": ["Trigger"],
        "invalidate_triggers": ["Invalidierung"],
        "event_dependencies": [],
        "asset_classes": ["Aktien"],
        "candidates": [],
        "status": "NEU",
        "replaced_by": None,
    }
    errors = validate_thesis_register({
        "data_cutoff": "2026-11-02T08:11:03+00:00",
        "theses": [thesis],
    })
    assert any("umgebenden Leerzeichen" in error for error in errors)
