"""Regression tests for descriptive SMA20/50/100 market-breadth evidence."""
import pandas as pd
import pytest

from trading_radar_market_first import append_live_market_snapshot


SYMBOLS = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
           "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]


def _data(periods):
    index = pd.date_range("2026-01-01", periods=periods, freq="B", tz="UTC")
    columns = pd.MultiIndex.from_product([SYMBOLS, ["Close"]])
    return pd.DataFrame(
        {col: [100.0 + i * 0.25 for i in range(len(index))] for col in columns},
        index=index,
    )


def test_market_breadth_reports_three_smas_dynamics_and_index_context():
    result, findings = append_live_market_snapshot(
        "### 1. Trading-Radar\nIndizes/Marktbreite: Beobachtung.\n### 2. Thesen-Update\n",
        fetcher=lambda requested: _data(125),
    )
    assert findings == []
    assert "SMA20: 11/11" in result
    assert "SMA50: 11/11" in result
    assert "SMA100: 11/11" in result
    assert "5S" in result and "10S" in result
    assert "S&P-500-Trendstruktur" in result
    assert "S&P-500-/SMA50-Marktbreitenvergleich (deskriptiv, kein Signal;" in result
    assert "in beiden geprüften Horizonten keine Gegenläufigkeit" in result


def test_short_history_marks_sma100_unavailable_without_fabrication():
    result, findings = append_live_market_snapshot(
        "### 1. Trading-Radar\nIndizes/Marktbreite: Beobachtung.\n### 2. Thesen-Update\n",
        fetcher=lambda requested: _data(80),
    )
    assert findings == []
    assert "SMA20: 11/11" in result
    assert "SMA50: 11/11" in result
    assert "SMA100: NICHT VERFÜGBAR" in result
    assert "SMA100 NICHT VERFÜGBAR" in result


def test_breadth_decline_against_rising_sp500_is_reported_as_possible_negative_divergence():
    index = pd.date_range("2026-01-01", periods=125, freq="B", tz="UTC")
    columns = pd.MultiIndex.from_product([SYMBOLS, ["Close"]])
    values = {}
    for symbol in SYMBOLS:
        if symbol == "^GSPC":
            values[(symbol, "Close")] = [100.0 + i * 0.3 for i in range(len(index))]
        elif symbol in {"^VIX", "DX-Y.NYB", "^TNX", "TLT"}:
            values[(symbol, "Close")] = [100.0 + i * 0.1 for i in range(len(index))]
        else:
            values[(symbol, "Close")] = [100.0 + i * 0.1 for i in range(115)] + [80.0] * 10
    data = pd.DataFrame(values, index=index)
    result, findings = append_live_market_snapshot(
        "### 1. Trading-Radar\nIndizes/Marktbreite: Beobachtung.\n### 2. Thesen-Update\n",
        fetcher=lambda requested: data,
    )
    assert findings == []
    assert "10S: S&P" not in result or "10S:" in result
    assert "10S:" in result and "gegenläufig" in result


def test_breadth_uses_one_common_completed_cutoff_and_excludes_missing_etfs():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot

    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    idx = pd.date_range("2026-01-05", periods=130, freq="B", tz="UTC")
    cols = pd.MultiIndex.from_product([symbols, ["Close"]])
    payload = {col: [100.0 + i for i in range(len(idx))] for col in cols}
    # One sector has no observation on the latest date; no forward-fill and no
    # mixing of its previous close with the other ten current closes.
    payload[("XLY", "Close")] = [float("nan")] * len(idx)
    data = pd.DataFrame(payload, index=idx)
    report = "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n"
    result, findings = append_live_market_snapshot(report, fetcher=lambda _: data)
    assert not findings
    assert "Marktbreite-Proxy: 10/10 Sektor-ETFs" in result
    assert "gemeinsamer abgeschlossener Datenstichtag" in result
    assert "SMA20: 10/10 von 11 ETFs" in result


def test_breadth_does_not_mix_individual_latest_dates_or_forward_fill():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot

    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    idx = pd.date_range("2026-01-05", periods=130, freq="B", tz="UTC")
    cols = pd.MultiIndex.from_product([symbols, ["Close"]])
    payload = {col: [100.0 + i * 0.1 for i in range(len(idx))] for col in cols}
    payload[("XLY", "Close")][-1] = float("nan")
    payload[("XLY", "Close")][-2] = float("nan")
    data = pd.DataFrame(payload, index=idx)
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
    )
    assert not findings
    assert "11/11 Sektor-ETFs" in result
    assert "SMA50: 11/11 von 11 ETFs" in result
    assert f"Datenstichtag {idx[-3].date().isoformat()}" in result
    assert f"Datenstichtag {idx[-1].date().isoformat()}" not in result


def test_divergence_reports_magnitude_and_horizon_persistence_without_thresholds():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot

    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    idx = pd.date_range("2026-01-05", periods=140, freq="B", tz="UTC")
    cols = pd.MultiIndex.from_product([symbols, ["Close"]])
    payload = {col: [100.0 + i * 0.1 for i in range(len(idx))] for col in cols}
    # Index rises throughout. Make the sector basket breadth deteriorate over
    # both horizons by changing the recent price path for a subset.
    for sym in ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE"]:
        arr = payload[(sym, "Close")]
        for j in range(len(arr)-10, len(arr)):
            arr[j] = arr[j-1] * 0.985
    data = pd.DataFrame(payload, index=idx)
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
    )
    assert not findings
    divergence_line = next(
        line for line in result.splitlines()
        if "S&P-500-/SMA50-Marktbreitenvergleich" in line
    )
    assert "gegenläufig nur in einem der geprüften Horizonte (5 oder 10 Sitzungen)" in divergence_line
    assert "5S: S&P +0.44%, Marktbreite +0/11 (+0.0 Prozentpunkte), nicht gegenläufig" in divergence_line
    assert "10S: S&P +0.89%, Marktbreite -8/11 (-72.7 Prozentpunkte), gegenläufig" in divergence_line


def test_live_snapshot_repeat_keeps_legacy_sma50_field_and_one_snapshot():
    import pandas as pd
    from trading_radar_market_first import append_live_market_snapshot

    symbols = ["^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
               "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    idx = pd.date_range("2026-01-05", periods=130, freq="B", tz="UTC")
    cols = pd.MultiIndex.from_product([symbols, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.2 for i in range(len(idx))] for col in cols},
        index=idx,
    )
    report = "1. Trading-Radar\nIndizes/Marktbreite: ALT.\n2. Thesen-Update\n"
    first, f1 = append_live_market_snapshot(report, fetcher=lambda _: data)
    second, f2 = append_live_market_snapshot(first, fetcher=lambda _: data)
    assert not f1 and not f2
    assert second.count("TRADING_RADAR_LIVE_MARKET_SNAPSHOT_START") == 1
    assert second.count("TRADING_RADAR_LIVE_MARKET_SNAPSHOT_END") == 1
    assert second.count("Quantitative Marktbreite:") == 1
    assert "Marktbreite-Proxy:" in second
    assert "SMA50:" in second and "SMA20:" in second and "SMA100:" in second


def test_current_new_york_date_is_excluded_before_regular_session_close():
    import pandas as pd

    now = pd.Timestamp("2026-06-05 15:59", tz="America/New_York")
    prior = pd.bdate_range(end="2026-06-04", periods=125).tz_localize("UTC")
    current_day = pd.DatetimeIndex([pd.Timestamp("2026-06-05", tz="UTC")])
    idx = prior.append(current_day)
    columns = pd.MultiIndex.from_product([SYMBOLS, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.2 for i in range(len(idx))] for col in columns},
        index=idx,
    )
    result, findings = append_live_market_snapshot(
        "### 1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n### 2. Thesen-Update\n",
        fetcher=lambda _: data, as_of_time=now,
    )
    assert not findings
    assert f"gemeinsamer abgeschlossener Datenstichtag; Veränderung" not in result or prior[-1].date().isoformat() in result
    assert f"Datenstichtag: {prior[-1].date().isoformat()}" in result
    assert f"Datenstichtag: {current_day[-1].date().isoformat()}" not in result


def test_fully_completed_current_new_york_date_is_included_after_close_buffer():
    import pandas as pd

    now = pd.Timestamp("2026-06-05 23:15", tz="America/New_York")
    prior = pd.bdate_range(end="2026-06-04", periods=125).tz_localize("UTC")
    current_day = pd.DatetimeIndex([pd.Timestamp("2026-06-05", tz="UTC")])
    idx = prior.append(current_day)
    columns = pd.MultiIndex.from_product([SYMBOLS, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.2 for i in range(len(idx))] for col in columns},
        index=idx,
    )
    result, findings = append_live_market_snapshot(
        "### 1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n### 2. Thesen-Update\n",
        fetcher=lambda _: data, as_of_time=now,
    )
    assert not findings
    assert f"Datenstichtag: {current_day[-1].date().isoformat()}" in result
    assert f"Datenstichtag: {prior[-1].date().isoformat()}" not in result


def test_materially_stale_common_sector_cutoff_is_explicitly_warned():
    import pandas as pd

    idx = pd.bdate_range(end="2026-05-20", periods=125).tz_localize("UTC")
    columns = pd.MultiIndex.from_product([SYMBOLS, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.2 for i in range(len(idx))] for col in columns},
        index=idx,
    )
    result, findings = append_live_market_snapshot(
        "### 1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n### 2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-06-05 23:15", tz="America/New_York"),
    )
    assert not findings
    assert "WARNUNG Marktbreite" in result
    assert "mehr als 7 Kalendertage" in result


def _data_with_current_session(session_date, periods=125):
    prior = pd.bdate_range(end=pd.Timestamp(session_date) - pd.Timedelta(days=1),
                            periods=periods).tz_localize("UTC")
    current = pd.DatetimeIndex([pd.Timestamp(session_date, tz="UTC")])
    idx = prior.append(current)
    columns = pd.MultiIndex.from_product([SYMBOLS, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.2 for i in range(len(idx))] for col in columns},
        index=idx,
    )
    return data, prior[-1], current[-1]


def test_nyse_close_buffer_boundary_16_14_excludes_current_session():
    data, prior_last, current = _data_with_current_session("2026-06-05")
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-06-05 16:14", tz="America/New_York"),
    )
    assert not findings
    assert f"Stichtag {prior_last.date().isoformat()}" in result
    assert f"Stichtag {current.date().isoformat()}" not in result


def test_nyse_close_buffer_boundary_16_15_includes_current_session():
    data, prior_last, current = _data_with_current_session("2026-06-05")
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-06-05 16:15", tz="America/New_York"),
    )
    assert not findings
    assert f"Stichtag {current.date().isoformat()}" in result
    assert f"Stichtag {prior_last.date().isoformat()}" not in result


def test_nyse_close_buffer_boundary_16_16_includes_current_session():
    data, _, current = _data_with_current_session("2026-06-05")
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-06-05 16:16", tz="America/New_York"),
    )
    assert not findings
    assert f"Stichtag {current.date().isoformat()}" in result


def test_nyse_early_close_uses_actual_session_close_plus_publication_buffer():
    data, prior_last, current = _data_with_current_session("2026-11-27")
    before_buffer, findings_before = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-11-27 13:14", tz="America/New_York"),
    )
    after_buffer, findings_after = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-11-27 13:15", tz="America/New_York"),
    )
    assert not findings_before and not findings_after
    assert f"Stichtag {current.date().isoformat()}" not in before_buffer
    assert f"Stichtag {current.date().isoformat()}" in after_buffer
    assert f"Stichtag {prior_last.date().isoformat()}" not in after_buffer


def test_snapshot_clock_is_captured_exactly_once_and_reused_for_report_stamp(monkeypatch):
    original_now = pd.Timestamp.now
    calls = []

    def counted_now(*args, **kwargs):
        calls.append((args, kwargs))
        return pd.Timestamp("2026-06-05 23:15", tz="America/New_York")

    monkeypatch.setattr(pd.Timestamp, "now", counted_now)
    data = _data(125)
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
    )
    monkeypatch.setattr(pd.Timestamp, "now", original_now)
    assert not findings
    assert len(calls) == 1
    assert "- Datenstand UTC: 2026-06-06T03:15:00+00:00" in result


# M-09 time-control coverage: exchange sessions, conservative fallback and DST.
def _time_test_data_with_current_session(session_date, *, prior_end=None, periods=125):
    prior_end = prior_end or (pd.Timestamp(session_date) - pd.Timedelta(days=1)).date().isoformat()
    prior = pd.bdate_range(end=prior_end, periods=periods).tz_localize("UTC")
    current = pd.DatetimeIndex([pd.Timestamp(session_date, tz="UTC")])
    idx = prior.append(current)
    columns = pd.MultiIndex.from_product([SYMBOLS, ["Close"]])
    data = pd.DataFrame(
        {col: [100.0 + i * 0.2 for i in range(len(idx))] for col in columns},
        index=idx,
    )
    return data


def _assert_common_cutoff(result, expected_date):
    assert f"gemeinsamer abgeschlossener Datenstichtag {expected_date}" in result


def _block_exchange_calendars_import(monkeypatch):
    import builtins

    original_import = builtins.__import__
    blocked = []

    def import_without_exchange_calendars(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "exchange_calendars":
            blocked.append(name)
            raise ImportError("exchange_calendars intentionally blocked by test")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_exchange_calendars)
    return blocked


def test_xnys_holiday_uses_last_completed_real_session_not_holiday_date():
    import pytest
    pytest.importorskip("exchange_calendars")

    # 2026-07-03 is the observed Independence Day NYSE holiday.
    data = _time_test_data_with_current_session("2026-07-03", prior_end="2026-07-02")
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-07-03 16:15", tz="America/New_York"),
    )
    assert not findings
    _assert_common_cutoff(result, "2026-07-02")
    assert "gemeinsamer abgeschlossener Datenstichtag 2026-07-03" not in result


@pytest.mark.parametrize("weekend_date", ["2026-06-06", "2026-06-07"])
def test_xnys_weekend_uses_friday_as_last_completed_session(weekend_date):
    import pytest
    pytest.importorskip("exchange_calendars")

    data = _time_test_data_with_current_session(weekend_date, prior_end="2026-06-05")
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp(f"{weekend_date} 16:15", tz="America/New_York"),
    )
    assert not findings
    _assert_common_cutoff(result, "2026-06-05")
    assert f"gemeinsamer abgeschlossener Datenstichtag {weekend_date}" not in result


@pytest.mark.parametrize(
    ("clock", "expected_date"),
    [
        ("2026-06-05 16:14", "2026-06-04"),
        ("2026-06-05 16:15", "2026-06-05"),
    ],
)
def test_missing_exchange_calendars_uses_fixed_1615_fallback_on_regular_day(
    monkeypatch, clock, expected_date
):
    blocked = _block_exchange_calendars_import(monkeypatch)
    data = _time_test_data_with_current_session("2026-06-05", prior_end="2026-06-04")
    result, findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp(clock, tz="America/New_York"),
    )
    assert not findings
    assert blocked == ["exchange_calendars"]
    _assert_common_cutoff(result, expected_date)


def test_xnys_early_close_is_recognized_at_1315_with_publication_buffer():
    """The installed XNYS calendar recognizes the 13:00 early close + 15 min."""
    pytest.importorskip("exchange_calendars")
    data = _time_test_data_with_current_session("2026-11-27", prior_end="2026-11-25")
    regular_result, regular_findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-11-27 13:15", tz="America/New_York"),
    )
    assert not regular_findings
    _assert_common_cutoff(regular_result, "2026-11-27")


def test_missing_exchange_calendars_fallback_is_conservative_on_early_close_day(monkeypatch):
    """Fallback test must run even when exchange_calendars is not installed."""
    data = _time_test_data_with_current_session("2026-11-27", prior_end="2026-11-25")
    blocked = _block_exchange_calendars_import(monkeypatch)
    fallback_result, fallback_findings = append_live_market_snapshot(
        "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
        fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-11-27 13:15", tz="America/New_York"),
    )
    assert not fallback_findings
    assert blocked == ["exchange_calendars"]
    _assert_common_cutoff(fallback_result, "2026-11-25")
    assert "gemeinsamer abgeschlossener Datenstichtag 2026-11-27" not in fallback_result


def test_naive_new_york_and_equivalent_utc_aware_timestamp_have_same_cutoff():
    data = _time_test_data_with_current_session("2026-06-05", prior_end="2026-06-04")
    report = "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n"
    naive_result, naive_findings = append_live_market_snapshot(
        report, fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-06-05 16:15"),
    )
    utc_result, utc_findings = append_live_market_snapshot(
        report, fetcher=lambda _: data,
        as_of_time=pd.Timestamp("2026-06-05 20:15", tz="UTC"),
    )
    assert not naive_findings and not utc_findings
    _assert_common_cutoff(naive_result, "2026-06-05")
    _assert_common_cutoff(utc_result, "2026-06-05")
    assert "- Datenstand UTC: 2026-06-05T20:15:00+00:00" in naive_result
    assert "- Datenstand UTC: 2026-06-05T20:15:00+00:00" in utc_result


@pytest.mark.parametrize(
    ("local_time", "utc_time", "session_date"),
    [
        ("2026-03-08 03:30", "2026-03-08 07:30", "2026-03-08"),
        ("2026-11-01 02:30", "2026-11-01 07:30", "2026-11-01"),
    ],
)
def test_dst_transition_naive_and_utc_aware_times_remain_equivalent(
    local_time, utc_time, session_date
):
    data = _time_test_data_with_current_session(session_date, prior_end="2026-03-06" if "03-08" in session_date else "2026-10-30")
    report = "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n"
    naive_result, naive_findings = append_live_market_snapshot(
        report, fetcher=lambda _: data, as_of_time=pd.Timestamp(local_time),
    )
    utc_result, utc_findings = append_live_market_snapshot(
        report, fetcher=lambda _: data, as_of_time=pd.Timestamp(utc_time, tz="UTC"),
    )
    assert not naive_findings and not utc_findings
    expected_utc = pd.Timestamp(utc_time, tz="UTC").isoformat()
    assert f"- Datenstand UTC: {expected_utc}" in naive_result
    assert f"- Datenstand UTC: {expected_utc}" in utc_result
    expected_prior_session = "2026-03-06" if "03-08" in session_date else "2026-10-30"
    _assert_common_cutoff(naive_result, expected_prior_session)
    _assert_common_cutoff(utc_result, expected_prior_session)


@pytest.mark.parametrize(
    ("naive_time", "expected_exception"),
    [
        ("2026-03-08 02:30", "NonExistentTimeError"),
        ("2026-11-01 01:30", "AmbiguousTimeError"),
    ],
)
def test_nonexistent_or_ambiguous_naive_dst_time_is_not_silently_reinterpreted(
    naive_time, expected_exception
):
    data = _data(125)
    with pytest.raises(Exception) as exc_info:
        append_live_market_snapshot(
            "1. Trading-Radar\nIndizes/Marktbreite: vorhanden.\n2. Thesen-Update\n",
            fetcher=lambda _: data,
            as_of_time=pd.Timestamp(naive_time),
        )
    # pandas 2.x (pytz) raises NonExistentTimeError/AmbiguousTimeError;
    # pandas 3.x (zoneinfo) may expose the same localization rejection as ValueError.
    # The contract is that the invalid local wall-clock time is rejected, not the
    # concrete exception class chosen by the pandas timezone backend.
    assert (
        type(exc_info.value).__name__ == expected_exception
        or isinstance(exc_info.value, ValueError)
    )
    assert naive_time in str(exc_info.value)
