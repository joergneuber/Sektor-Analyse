import pandas as pd
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("offene_positionen_check", ROOT / "offene_positionen_check.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def row(ticker, name, entry_date, entry, status="Gestoppt"):
    data = {c: "" for c in mod.HISTORY_HEADERS}
    data.update({
        "Ticker": ticker,
        "Name": name,
        "Einstiegsdatum": entry_date,
        "Einstieg": entry,
        "Status": status,
        "Ausstiegsdatum": "10.09.2026",
        "Ausstiegskurs": "60.00",
    })
    return data


def test_closed_history_is_permanent_and_never_replaced_by_smaller_snapshot():
    # Simuliert den kritischen JOST-Fall: Tab 2 enthält bereits 28 Trades,
    # der nächste Lauf liefert lokal aber nur den neuen/aktuellen Bestand.
    old = [mod.HISTORY_HEADERS,
           [row("JST.DE", "JOST Werke SE", "21.08.2026", "59.40")[c] for c in mod.HISTORY_HEADERS],
           [row("ABC.DE", "Existing Trade", "01.09.2026", "10.00")[c] for c in mod.HISTORY_HEADERS]]
    new = pd.DataFrame([row("NEW.DE", "New Trade", "10.09.2026", "20.00")], columns=mod.HISTORY_HEADERS)

    merged = mod.merge_closed_history(old, new)
    tickers = set(merged["Ticker"])
    assert "JST.DE" in tickers
    assert "ABC.DE" in tickers
    assert "NEW.DE" in tickers
    assert len(merged) == 3


def test_upsert_does_not_use_tab1_disappearance_as_a_close_event():
    source = (ROOT / "offene_positionen_check.py").read_text(encoding="utf-8-sig")
    active = source[source.index("def upsert_google_sheet"):source.index("def _remove_closed_from_source")]
    assert "extract_disappeared_open_positions(existing_open_rows, df)" not in active
    assert 'temp_open: "Offene Positionen+Check"' in active
    assert 'temp_closed: "Geschlossene Positionen"' not in active


def test_tracker_documents_permanent_archive():
    source = (ROOT / "positionen_tracker.py").read_text(encoding="utf-8-sig")
    assert "erscheint sie dauerhaft" in source
    assert "10 Werktage lang" not in source
