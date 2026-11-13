import ast
import hashlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_gate():
    spec = importlib.util.spec_from_file_location("repair_gate", ROOT / "trading_radar_report_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_name_normalizer(tmpdir):
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_trading_radar_name_ticker_normalisieren")
    namespace = {"os": os, "json": json, "re": __import__("re"), "__file__": str(Path(tmpdir) / "module.py")}
    Path(tmpdir, ".firma_name_cache.json").write_text(json.dumps({
        "GC=F": {"name": "Royal Gold Future", "missing": False},
        "RGLD": {"name": "Royal Gold Future, Inc.", "missing": False},
        "FRED": {"name": "FRED Fake Security", "missing": False},
    }), encoding="utf-8")
    exec(compile(ast.Module(body=[node], type_ignores=[]), "normalizer.py", "exec"), namespace)
    return namespace[node.name]


def test_name_normalizer_preserves_royal_gold_and_protects_acronyms():
    with tempfile.TemporaryDirectory() as tmp:
        normalize = load_name_normalizer(tmp)
        report = (
            "Top-Profiteur: Royal Gold, Inc. (RGLD)\n"
            "Makrodaten aus FRED; KGV-Aufschläge; OPEC-Beschlüsse.\n"
            "JETZT TECHNISCH PRÜFEN: keine Kandidaten\n"
            "AUF TRIGGER WARTEN: keine Kandidaten\n"
            "WARTEN"
        )
        result = normalize(report)
        assert "Royal Gold, Inc. (RGLD)" in result
        assert "Royal Gold Future (GC=F), Inc." not in result
        for token in ("FRED", "KGV", "OPEC", "JETZT", "AUF", "TRIGGER", "WARTEN"):
            assert f"Name nicht aufgelöst ({token})" not in result


def test_report_gate_rejects_conflicting_performance_duplicates():
    gate = load_gate()
    # Reuse a minimal valid field context to isolate M-01 duplicate semantics.
    text = (
        "7. MSCI-Vergleich\n"
        "Portfolio-Gesamtperformance: -10,04%\n"
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR\n"
        "Ø Positionsperformance: NICHT VERFÜGBAR\n"
    )
    errors = []
    gate._validate_m05_m10(text, errors)
    assert any("M-01" in error for error in errors)


def test_run_evidence_requires_commit_run_and_gate_binding():
    gate = load_gate()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "evidence.json"
        path.write_text(json.dumps({
             "start": {"run_identity": {"commit_sha": "a" * 40, "workflow_run_id": "41002340994"}},
             "end": {
                "run_identity": {"commit_sha": "a" * 40, "workflow_run_id": "41002340994"},
                "sources": [{"path": "input.txt", "sha256": "9" * 64}],
            },
            "run_identity": {
                 "repository": "owner/repo", "ref": "refs/heads/main",
                "commit_sha": "a" * 40,
                "workflow_run_id": "41002340994",
                "source_file_hashes": {
                    name: hashlib.sha256(name.encode()).hexdigest()
                    for name in (
                        "Trading_Radar_Alleinauswertung.py", "trading_radar_neuauswertung.py",
                        "trading_radar_market_first.py", "trading_radar_thesen.py",
                        "trading_radar_report_gate.py", "trading_radar_run_evidence.py",
                        "trading_radar_portfolio_performance.py", "makro_szenario.py", "offene_positionen_check.py",
                        "historical_ohlcv.py", "historical_ohlcv_pending_queue.py", "einzel_check.py",
                        "ohlcv_release_bundle.py", "scripts/get_gh_release_assets.sh",
                        "scripts/restore_ohlcv_release_bundle.sh",
                        ".github/workflows/main.yml", ".github/workflows/einzel_check.yml",
                    )
                },
                "gate_results": {
                    "TRADING_RADAR_CONTRACT_OUTCOME": "success",
                    "TRADING_RADAR_STEP_OUTCOME": "success",
                    "TRADING_RADAR_OHLCV_PERSISTENCE_GATE": "PASS",
            "TRADING_RADAR_OHLCV_QUEUE_GATE": "PASS",
                    "TRADING_RADAR_MARKET_FIRST_GATE": "PASS",
                    "TRADING_RADAR_REPORT_GATE": "PASS",
                    "TRADING_RADAR_REPORT_GATE_OUTCOME": "success",
                    "TRADING_RADAR_FACHLICH_FINAL": "FINAL",
                },
            }
        }), encoding="utf-8")
        assert gate.validate_run_evidence(path) == []


def test_run_evidence_rejects_missing_commit_sha():
    gate = load_gate()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "evidence.json"
        path.write_text(json.dumps({"run_identity": {"workflow_run_id": "42"}}), encoding="utf-8")
        errors = gate.validate_run_evidence(path)
        assert any("Commit-SHA" in error for error in errors)
        assert any("SHA256" in error for error in errors)


def test_evidence_finalizer_persists_report_gate_outcome(tmp_path, monkeypatch):
    import importlib.util
    module_path = ROOT / "trading_radar_run_evidence.py"
    spec = importlib.util.spec_from_file_location("repair_evidence", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    end_file = tmp_path / "end.json"
    summary_file = tmp_path / "summary.json"
    end_file.write_text(json.dumps({"run_identity": {"commit_sha": "a"*40, "workflow_run_id": "77"}}), encoding="utf-8")
    summary_file.write_text(json.dumps({"end": {"run_identity": {"commit_sha": "a"*40, "workflow_run_id": "77"}}}), encoding="utf-8")
    monkeypatch.setattr(module, "END_FILE", end_file)
    monkeypatch.setattr(module, "SUMMARY_FILE", summary_file)
    monkeypatch.setenv("TRADING_RADAR_REPORT_GATE", "success")
    assert module.finalize_gate_result() == 0
    end = json.loads(end_file.read_text(encoding="utf-8"))
    assert end["run_identity"]["gate_results"]["TRADING_RADAR_REPORT_GATE"] == "PASS"


def test_section_parser_does_not_treat_numbered_list_items_as_section_headings():
    gate = load_gate()
    text = (
        "1. Trading-Radar\n"
        "1. Erstes Listenitem.\n"
        "2. Zweites Listenitem bleibt in Abschnitt 1.\n"
        "Index S&P 500: 1M +1%; 3M +2%.\n"
        "2. Thesen-Update\n"
        "Thesenregistervergleich folgt."
    )
    body = gate._section_body(text, 1, "Trading-Radar")
    assert "2. Zweites Listenitem bleibt in Abschnitt 1." in body
    assert "Thesenregistervergleich folgt." not in body


def test_report_gate_requires_treasury_conflict_to_be_open_or_reconciled():
    gate = load_gate()
    spec = importlib.util.spec_from_file_location("report_fixture_mod", ROOT / "tests/test_trading_radar_report_gate.py")
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    report = fixture_module.report_fixture().replace(
        "Index S&P 500: 1M +1,0%; 3M +2,0%.",
        "US 10Y Treasury (Makroquelle): 5,00%.\nUS 10Y Treasury (Livequelle): 4,99%.\nIndex S&P 500: 1M +1,0%; 3M +2,0%.",
    )
    errors = gate.validate_report(report)
    assert any("M-07" in error for error in errors)
    report = report.replace(
        "US 10Y Treasury (Livequelle): 4,99%.",
        "US 10Y Treasury (Livequelle): 4,99%.\n"
        "QUELLENKONFLIKT: OFFEN | 10Y Treasury 5,00% vs. 4,99%; Quellen-/Zeitstempelabgleich ausstehend."
    )
    # An open-conflict label is not a substitute for provenance of each source row.
    assert any("M-07" in error for error in gate.validate_report(report))
    report = report.replace(
        "US 10Y Treasury (Makroquelle): 5,00%.",
        "US 10Y Treasury (Makroquelle): 5,00% | Quelle: FRED DGS10 | Abrufzeit: 2026-10-30T04:30:00Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag."
    ).replace(
        "US 10Y Treasury (Livequelle): 4,99%.",
        "US 10Y Treasury (Livequelle): 4,99% | Quelle: Yahoo Finance ^TNX | Abrufzeit: 2026-10-30T04:35:10Z | Datenstichtag: 2026-10-29 | Einheit: % | Verzögerung: 1 Börsentag."
    )
    assert not any("M-07" in error for error in gate.validate_report(report))


def test_run_identity_hashes_workflow_configuration():
    import importlib.util
    module_path = ROOT / "trading_radar_run_evidence.py"
    spec = importlib.util.spec_from_file_location("repair_evidence_config_hash", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = module._run_identity()
    assert identity["source_file_hashes"][".github/workflows/main.yml"] == hashlib.sha256(
        (ROOT / ".github/workflows/main.yml").read_bytes()
    ).hexdigest()


def test_report_gate_accepts_proven_empty_active_thesis_register():
    gate = load_gate()
    text = (
        "2. Thesen-Update\n"
        "Thesen-ID: T-1 | Version: 1 | Status: INVALIDIERT. Alle Thesen sind archiviert; keine aktive These verbleibt.\n"
        "THESENREGISTER-VERGLEICH: PASS\n"
        "VORHER-SHA256: " + "a" * 64 + "\n"
        "NACHHER-SHA256: " + "b" * 64 + "\n"
        "THESENREGISTER-VERGLEICH-ID: 03ad73fb6916f8c1d10706e17fd4e1a260ead1c02fb21797d8316756cc4f0c96\n"
        "VORHER_THESEN_COUNT: 1\nVORHER_AKTIVE_THESEN: 1\nNACHHER_THESEN_COUNT: 1\nNACHHER_AKTIVE_THESEN: 0\n"
        "VORHER-THESIS-ID: T-1\nVORHER-REGISTER-VERSION: 1\nVORHER-THESIS-ACTIVE: YES\n"
        "THESEN_NEU: KEINE\n"
        "THESEN_AKTUALISIERT: KEINE\n"
        "THESEN_UNVERAENDERT: KEINE\n"
        "THESEN_ARCHIVIERT: T-1\n"
        "THESIS_ID: T-1\nREGISTER-VERSION: 1\nTHESIS_ACTIVE: NO\n"
        "7. MSCI-Vergleich\n"
        "Portfolio-Gesamtperformance: NICHT VERFÜGBAR\n"
        "Ø Positionsperformance: NICHT VERFÜGBAR\n"
    )
    errors = []
    gate._validate_m05_m10(text, errors)
    assert not any("M-05:" in error for error in errors)


def test_report_gate_requires_all_three_scenario_triggers():
    import importlib.util
    spec = importlib.util.spec_from_file_location("scenario_fixture_mod", ROOT / "tests/test_trading_radar_report_gate.py")
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    gate = load_gate()
    report = fixture_module.report_fixture().replace(
        "Invalidierungstrigger: Bruch 200-Tage-Linie.",
        "Invalidierungshinweis: Bruch 200-Tage-Linie.",
    )
    errors = gate.validate_report(report)
    assert any("M-09-SZENARIO: Basisszenario" in error for error in errors)


def test_thesis_register_evidence_is_inserted_inside_section_two(tmp_path):
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in {"_trading_radar_thesenregister_nachweis_text", "_trading_radar_thesenregister_nachweis_einfuegen"}]
    node = next(n for n in nodes if n.name == "_trading_radar_thesenregister_nachweis_einfuegen")
    namespace = {"os": os, "re": __import__("re"), "tempfile": __import__("tempfile")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "thesis_evidence.py", "exec"), namespace)
    report_path = tmp_path / "radar.txt"
    report_path.write_text(
        "1. Trading-Radar\nMarktregime und Kontext.\n"
        "2. Thesen-Update\nThese mit Belegen.\n"
        "3. Ereignisradar\nTermin und Quelle.\n"
        "4. Assetklassenvergleich\nVergleichsdaten.\n",
        encoding="utf-8",
    )
    namespace[node.name](report_path, [
        "THESENREGISTER-VERGLEICH: PASS",
        "VORHER-SHA256: " + "a" * 64,
        "NACHHER-SHA256: " + "b" * 64,
        "THESIS_ID: T-1",
        "VERSION: 2",
    ])
    result = report_path.read_text(encoding="utf-8")
    assert result.index("THESENREGISTER-VERGLEICH: PASS") < result.index("3. Ereignisradar")
    assert result.index("THESENREGISTER-VERGLEICH: PASS") > result.index("2. Thesen-Update")



def test_watchlist_zero_state_requires_reason():
    gate = load_gate()
    text = (
        "1. Trading-Radar\n" + "Marktbild und Kontext ausreichend dokumentiert.\n"
        "2. Thesen-Update\nKeine neue These.\n"
        "3. Ereignisradar\nKeine relevanten Ereignisse.\n"
        "4. Assetklassenvergleich\nRelative-Stärke-Matrix / Kapitalrotationsvergleich\n"
        "Datenstichtag: 2026-10-30. Fenster: 1M und 3M. Gewinner: A. Verlierer: B. Divergenzen: C.\n"
        "Indizes: 1M +1%; 3M +2%. Regionen: 1M +1%; 3M +2%. Sektoren: 1M +1%; 3M +2%. Anlageklassen: 1M +1%; 3M +2%.\n"
        "5. Technische Kandidaten\nKandidatenquellen: 2 Quellen.\n"
        "BESTÄTIGTE SETUPS: Keine bestätigten Setups. Begründung: keine bestätigten Signale.\n"
        "WATCHLIST: Keine Watchlist-Einträge.\n"
        "VERWORFENE KANDIDATEN: Keine verworfenen Kandidaten. Begründung: keine formal geprüften Kandidaten.\n"
    )
    errors = []
    gate._validate_m05_m10(text, errors)
    assert any("Nullmeldung für 'Watchlist'" in error for error in errors)


def test_relative_strength_gate_requires_each_comparison_group():
    gate = load_gate()
    text = (
        "4. Assetklassenvergleich\nRelative-Stärke-Matrix / Kapitalrotationsvergleich\n"
        "Datenstichtag: 2026-10-30. Fenster 1M und 3M. Gewinner: A. Verlierer: B. Divergenzen: C.\n"
        "Regionen: 1M +1%; 3M +2%. Sektoren: 1M +1%; 3M +2%. Anlageklassen: 1M +1%; 3M +2%.\n"
    )
    errors = []
    gate._validate_m05_m10(text, errors)
    assert any("Indizes fehlen" in error for error in errors)


def test_run_evidence_hashes_portfolio_performance_and_position_export_code():
    spec = importlib.util.spec_from_file_location(
        "repair_evidence_hashes", ROOT / "trading_radar_run_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hashes = module._run_identity()["source_file_hashes"]
    for name in ("trading_radar_portfolio_performance.py", "offene_positionen_check.py", "makro_szenario.py"):
        assert hashes[name] == hashlib.sha256((ROOT / name).read_bytes()).hexdigest()


def test_tab2_performance_export_preserves_all_rows_and_currency_fields(tmp_path):
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_write_tab2_performance_csv")
    namespace = {
        "os": os, "csv": __import__("csv"), "tempfile": tempfile,
        "hashlib": hashlib, "Path": Path,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "tab2_export.py", "exec"), namespace)
    headers = ["Ticker", "Name", "Waehrung", "Richtung", "Einstiegsdatum",
               "Einstieg", "Ausstiegsdatum", "Ausstiegskurs", "Status"]
    selected = [
        (3, {"Ticker": "AAA", "Name": "Alpha", "Waehrung": "EUR", "Richtung": "Long",
             "Einstiegsdatum": "01.01.2026", "Einstieg": 100, "Ausstiegsdatum": "02.01.2026",
             "Ausstiegskurs": 110, "Status": "Verkauft"}),
        (4, {"Ticker": "BBB", "Name": "Beta", "Waehrung": "EUR", "Richtung": "Short",
             "Einstiegsdatum": "03.01.2026", "Einstieg": 200, "Ausstiegsdatum": "04.01.2026",
             "Ausstiegskurs": 190, "Status": "Gestoppt"}),
    ]
    target = tmp_path / "Geschlossene Positionen_Tab2.csv"
    result = namespace[node.name](headers, selected, str(target))
    assert result["count"] == 2
    assert len(result["sha256"]) == 64
    import csv
    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Ticker"] for row in rows] == ["AAA", "BBB"]
    assert [row["Waehrung"] for row in rows] == ["EUR", "EUR"]


def test_tab2_performance_export_accepts_missing_currency_column(tmp_path):
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_write_tab2_performance_csv")
    namespace = {
        "os": os, "csv": __import__("csv"), "tempfile": tempfile,
        "hashlib": hashlib, "Path": Path,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "tab2_export.py", "exec"), namespace)
    headers = ["Ticker", "Einstiegsdatum", "Einstieg", "Ausstiegsdatum",
               "Ausstiegskurs", "Status", "Richtung"]
    result = namespace[node.name](headers, [], str(tmp_path / "tab2.csv"))
    assert result["count"] == 0
    assert len(result["sha256"]) == 64



def test_report_gate_accepts_equal_weighted_average_and_rejects_sum(tmp_path):
    gate = load_gate()
    from trading_radar_portfolio_performance import calculate_portfolio_performance, format_performance_block
    source = tmp_path / "positions.csv"
    source.write_text(
        "Ticker;Richtung;Einstiegsdatum;Einstieg;Aktueller_Kurs;Status;Ausstiegskurs\n"
        "AAA;Long;01.01.2026;100;105;Offen;\n"
        "BBB;Long;02.01.2026;100;102;Verkauft;102\n",
        encoding="utf-8",
    )
    text = "7. MSCI-Vergleich\n" + format_performance_block(
        calculate_portfolio_performance([source])
    ) + "\n"
    errors = []
    gate._validate_m05_m10(text, errors)
    assert not any("M-01" in error for error in errors)

    summed = text.replace(
        "Portfolio-Gesamtperformance: +3,50 %",
        "Portfolio-Gesamtperformance: +7,00 %",
    ).replace(
        "gleichgewichteten arithmetischen Mittelwert",
        "Summe der prozentualen Einzelrenditen",
    ).replace(
        "geteilt durch Anzahl eindeutiger Trades",
        "als Prozentpunkte aufsummiert",
    )
    errors = []
    gate._validate_m05_m10(summed, errors)
    assert any("M-01" in error for error in errors)


def test_final_evidence_validator_requires_report_gate_and_matching_run(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "repair_evidence_final", ROOT / "trading_radar_run_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    end_file = tmp_path / "end.json"
    summary_file = tmp_path / "summary.json"
    source_files = (
        "Trading_Radar_Alleinauswertung.py", "trading_radar_neuauswertung.py",
        "trading_radar_report_gate.py", "trading_radar_market_first.py",
        "trading_radar_run_evidence.py", "trading_radar_thesen.py",
        "trading_radar_portfolio_performance.py", "offene_positionen_check.py",
        "historical_ohlcv.py", "historical_ohlcv_pending_queue.py", "einzel_check.py",
        "ohlcv_release_bundle.py", "scripts/get_gh_release_assets.sh",
        "scripts/restore_ohlcv_release_bundle.sh",
        ".github/workflows/main.yml", ".github/workflows/einzel_check.yml",
    )
    source_hashes = {name: "c" * 64 for name in source_files}
    common_identity = {
        "repository": "owner/repository", "ref": "refs/heads/main",
        "commit_sha": "a" * 40, "workflow_run_id": "123",
        "workflow_run_attempt": "1", "source_file_hashes": source_hashes,
    }
    identity = {
        **common_identity,
        "gate_results": {
            "TRADING_RADAR_CONTRACT_OUTCOME": "success",
            "TRADING_RADAR_STEP_OUTCOME": "success",
            "TRADING_RADAR_OHLCV_PERSISTENCE_GATE": "PASS",
            "TRADING_RADAR_OHLCV_QUEUE_GATE": "PASS",
            "TRADING_RADAR_MARKET_FIRST_GATE": "PASS",
            "TRADING_RADAR_REPORT_GATE": "PASS",
            "TRADING_RADAR_REPORT_GATE_OUTCOME": "success",
            "TRADING_RADAR_FACHLICH_FINAL": "FINAL",
        },
    }
    start = {"run_identity": dict(common_identity)}
    end = {"run_identity": identity, "sources": [{"sha256": "b"*64}]}
    end_file.write_text(json.dumps(end), encoding="utf-8")
    summary_file.write_text(json.dumps({"start": start, "end": end, "run_identity": identity}), encoding="utf-8")
    monkeypatch.setattr(module, "END_FILE", end_file)
    monkeypatch.setattr(module, "SUMMARY_FILE", summary_file)
    assert module.validate_final_evidence() == 0



def test_name_normalizer_restores_previously_wrapped_status_and_macro_words():
    with tempfile.TemporaryDirectory() as tmp:
        normalize = load_name_normalizer(tmp)
        report = (
            "Name nicht aufgelöst (FRED)-Makrodaten.\n"
            "Name nicht aufgelöst (KGV)-Aufschläge.\n"
            "Name nicht aufgelöst (OPEC)+-Beschlüsse.\n"
            "Name nicht aufgelöst (JETZT) TECHNISCH PRÜFEN: keine Kandidaten.\n"
            "Name nicht aufgelöst (AUF) TRIGGER Name nicht aufgelöst (WARTEN): keine Kandidaten.\n"
        )
        result = normalize(report)
        for expected in ("FRED-Makrodaten", "KGV-Aufschläge", "OPEC+-Beschlüsse",
                         "JETZT TECHNISCH PRÜFEN", "AUF TRIGGER WARTEN"):
            assert expected in result
        assert "Name nicht aufgelöst (" not in result



def test_candidate_source_inventory_parses_rows_instead_of_only_listing_files(tmp_path):
    source = (ROOT / "Trading_Radar_Alleinauswertung.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_trading_radar_kandidatenquellen_absichern")
    namespace = {"os": os, "re": __import__("re")}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "candidate_helper.py", "exec"), namespace)
    csv_path = tmp_path / "scanner_kandidaten.csv"
    csv_path.write_text(
        "Ticker;Name;Status;Trigger;Einstieg;Stop;TP1\n"
        "ABC;Alpha Beta AG;confirmed;Schlusskurs ueber 105;100;95;110\n"
        "XYZ;Xylon AG;watch;Ruecklauf an 50-Tage-Linie;20;18;24\n"
        "BAD;Bad Corp;rejected;;10;8;12\n",
        encoding="utf-8",
    )
    report = "5. Technische Kandidaten\nKeine Kandidaten.\n\n6. Portfolio & Risiko\n"
    result = namespace["_trading_radar_kandidatenquellen_absichern"](
        report, {"scanner": str(csv_path)}
    )
    assert "Alpha Beta AG (ABC)" in result and "BESTÄTIGTE SETUPS" in result
    assert "Xylon AG (XYZ)" in result and "WATCHLIST" in result
    assert "Bad Corp (BAD)" in result and "VERWORFENE KANDIDATEN" in result
    assert "Quellzeile: 2" in result and "Quellzeile: 3" in result
