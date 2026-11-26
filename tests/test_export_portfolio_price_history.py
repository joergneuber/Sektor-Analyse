from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from export_portfolio_price_history import export, read_prices, select_relevant_positions


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE instruments (ticker TEXT PRIMARY KEY, first_date TEXT, last_date TEXT, row_count INTEGER, last_update_utc TEXT, source TEXT);
        CREATE TABLE bars (ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL, source TEXT, PRIMARY KEY(ticker,date));
        INSERT INTO instruments VALUES ('AAA.DE','2026-08-07','2026-11-26',2,'2026-11-26T20:00:00Z','yfinance');
        INSERT INTO instruments VALUES ('EUNL.DE','2026-08-07','2026-11-26',2,'2026-11-26T20:00:00Z','yfinance');
        INSERT INTO bars VALUES ('AAA.DE','2026-08-07',10,11,9,10.5,10.4,100,'yfinance');
        INSERT INTO bars VALUES ('AAA.DE','2026-11-26',11,12,10,11.5,11.3,120,'stooq');
        INSERT INTO bars VALUES ('EUNL.DE','2026-08-07',128,129,127,128.53,128.1,1000,'yfinance');
        INSERT INTO bars VALUES ('EUNL.DE','2026-11-26',125,126,124,125.745,125.2,1100,'yfinance');
        """)


def _positions() -> pd.DataFrame:
    return pd.DataFrame([
        {"Ticker":"AAA.DE","Name":"Altposition","Status":"Offen","Einstiegsdatum":"01.05.2026","Richtung":"Long","Einstieg":"9,50","Aktueller_Kurs":"11,5"},
        {"Ticker":"BBB","Name":"Vorher geschlossen","Status":"Verkauft","Einstiegsdatum":"01.05.2026","Ausstiegsdatum":"06.08.2026","Ausstiegskurs":"12","Richtung":"Long","Einstieg":"10"},
        {"Ticker":"CCC","Name":"Nach Systemstart","Status":"Gestoppt","Einstiegsdatum":"10.08.2026","Ausstiegsdatum":"20.08.2026","Ausstiegskurs":"15","Richtung":"Long","Einstieg":"14"},
        {"Ticker":"EUNL.DE","Name":"ETF held","Status":"Offen","Einstiegsdatum":"02.02.2022","Richtung":"Long","Einstieg":"108,04","Aktueller_Kurs":"125,745"},
        {"Ticker":"BABA","Name":"Alibaba OS","Status":"Offen","Einstiegsdatum":"07.05.2026","Richtung":"Long","Produkt_Typ":"Optionsschein","OS_WKN":"PM0216","OS_Einstiegskurs":"46055,00"},
    ])


def test_selects_only_positions_active_since_system_start_and_keeps_held_eunl():
    selected, issues = select_relevant_positions(_positions(), pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date())
    tickers = [x['ticker'] for x in selected]
    assert 'AAA.DE' in tickers
    assert 'CCC' in tickers
    assert 'BBB' not in tickers
    assert tickers.count('EUNL.DE') == 1
    assert not issues


def test_options_are_not_exported_as_underlying_price_series():
    selected, _ = select_relevant_positions(_positions(), pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date())
    baba = next(x for x in selected if x['ticker'] == 'BABA')
    assert baba['ohlcv_required'] is False
    assert baba['os_wkn'] == 'PM0216'
    assert 'Basiswert-OHLCV nicht' in baba['note']


def test_read_prices_returns_raw_and_adjusted_close_with_bar_source(tmp_path):
    db = tmp_path / 'prices.sqlite'
    _db(db)
    rows, instruments = read_prices(db, ['AAA.DE'], pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date())
    assert len(rows) == 2
    assert rows[0]['close'] == 10.5
    assert rows[0]['adj_close'] == 10.4
    assert rows[1]['source'] == 'stooq'
    assert instruments[0]['ticker'] == 'AAA.DE'


def test_export_writes_coverage_and_does_not_claim_quality_gate(tmp_path):
    db = tmp_path / 'prices.sqlite'
    _db(db)
    summary = export(db, _positions(), pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date(), tmp_path / 'out')
    assert summary['calculation_performed'] is False
    assert summary['quality_gate_performed'] is False
    assert (tmp_path / 'out' / 'portfolio_historical_prices.csv').is_file()
    assert (tmp_path / 'out' / 'portfolio_positions.csv').is_file()
    assert any(x['ticker'] == 'EUNL.DE' for x in summary['coverage'])


def test_read_only_database_connection_does_not_mutate_database(tmp_path):
    db = tmp_path / 'prices.sqlite'
    _db(db)
    with pytest.raises(sqlite3.OperationalError):
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
            conn.execute('DELETE FROM bars')
    rows, _ = read_prices(db, ['AAA.DE'], pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date())
    assert len(rows) == 2


def test_missing_eunl_position_creates_only_benchmark_marker(tmp_path):
    positions = _positions().loc[lambda frame: frame["Ticker"] != "EUNL.DE"].copy()
    selected, _ = select_relevant_positions(positions, pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date())
    eunl = [x for x in selected if x['ticker'] == 'EUNL.DE']
    assert len(eunl) == 1
    assert eunl[0]['status'] == 'BENCHMARK_ONLY'
    assert eunl[0]['source_row'] == -1


def test_closed_trade_without_exit_date_is_reported_as_issue():
    positions = pd.DataFrame([{"Ticker":"AAA","Status":"Verkauft","Einstiegsdatum":"01.08.2026","Richtung":"Long"}])
    selected, issues = select_relevant_positions(positions, pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date())
    assert selected and selected[0]['ticker'] == 'EUNL.DE'
    assert any('ohne Ausstiegsdatum' in issue['issue'] for issue in issues)


def test_position_closed_after_cutoff_is_open_at_cutoff():
    positions = pd.DataFrame([{"Ticker":"AAA","Status":"Verkauft","Einstiegsdatum":"01.08.2026","Ausstiegsdatum":"30.11.2026","Ausstiegskurs":"15","Richtung":"Long"}])
    selected, issues = select_relevant_positions(positions, pd.Timestamp('2026-08-07').date(), pd.Timestamp('2026-11-26').date())
    aaa = next(x for x in selected if x['ticker'] == 'AAA')
    assert aaa['status_at_cutoff'] == 'OPEN_AT_CUTOFF'
    assert aaa['active_to'] == '2026-11-26'
    assert not issues
