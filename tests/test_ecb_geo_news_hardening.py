from pathlib import Path
import ast
import datetime as dt
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "makro_szenario.py"


def _load_module():
    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = lambda *args, **kwargs: None
    sys.modules.setdefault("yfinance", fake_yf)
    sys.path.insert(0, str(ROOT))
    import makro_szenario as m
    return m


def test_ecb_calendar_distinguishes_non_monetary_meeting():
    source = SOURCE.read_text(encoding="utf-8")
    assert "non-monetary policy meeting" in source
    assert "EZB-Rat: Nicht-geldpolitische Sitzung" in source
    assert "ECB official calendar" in source
    assert "((?!\\d{2}/\\d{2}/\\d{4}).){0,500}?" in source


def test_ecb_deposit_facility_has_targeted_official_refresh():
    source = SOURCE.read_text(encoding="utf-8")
    assert 'ECB_KEY_RATES_URL = "https://www.ecb.europa.eu/press/press_conference/html/index.en.html"' in source
    assert 'def _official_ecb_deposit_facility_series():' in source
    assert 'if series_id == "ECBDFR":' in source
    assert 'official, official_source = _official_ecb_deposit_facility_series()' in source
    assert 'ECBDFR": 7' in source


def test_ecb_official_parser_extracts_current_rate_and_date():
    m = _load_module()

    class FakeResponse:
        text = "<html>With effect from: 16 September 2026 Deposit facility 2.50 % Main refinancing operations 2.65 %</html>"
        status_code = 200

        def raise_for_status(self):
            return None

    original = m.requests.get
    try:
        m.requests.get = lambda *args, **kwargs: FakeResponse()
        df, source = m._official_ecb_deposit_facility_series()
    finally:
        m.requests.get = original

    assert source == m.ECB_KEY_RATES_URL
    assert float(df.iloc[-1]["ECBDFR"]) == 2.50
    assert df.iloc[-1]["DATE"].date() == dt.date(2026, 9, 16)


def test_geo_provider_is_free_public_dw_rss_with_24h_cache():
    source = SOURCE.read_text(encoding="utf-8")
    assert 'GEO_NEWS_RSS_URL = "https://rss.dw.com/syndication/feeds/VAS_DE_NeuseelandNews.32453-copypaste.html"' in source
    assert 'GEO_NEWS_CACHE_FILE = MACRO_CACHE_DIR / "geo_news_cache.json"' in source
    assert 'GEO_NEWS_CACHE_MAX_AGE_HOURS = 24' in source
    assert 'Deutsche Welle RSS' in source
    assert 'def _geo_news_fetch_rss(today):' in source


def test_geo_rss_parser_extracts_concrete_article_without_market_preinterpretation():
    m = _load_module()

    class FakeResponse:
        status_code = 200
        content = b'''<?xml version="1.0"?><rss><channel>
        <item><title>Iran war spreads through region as Trump seeks way out</title>
        <link>https://www.dw.com/test</link>
        <pubDate>Wed, 23 Sep 2026 12:00:00 +0000</pubDate>
        <teaser>Regional tensions are affecting shipping and energy markets.</teaser>
        <full_text><![CDATA[The full article explains the regional escalation and its consequences for shipping routes and energy supply.]]></full_text>
        </item>
        <item><title>Local culture story</title><link>https://www.dw.com/other</link>
        <pubDate>Wed, 23 Sep 2026 12:00:00 +0000</pubDate></item>
        </channel></rss>'''

        def raise_for_status(self):
            return None

    original = m.requests.get
    try:
        m.requests.get = lambda *args, **kwargs: FakeResponse()
        items = m._geo_news_fetch_rss(dt.date(2026, 9, 23))
    finally:
        m.requests.get = original

    assert len(items) == 1
    assert items[0]["cluster"] == "Nahost"
    assert items[0]["title"].startswith("Iran war spreads")
    assert items[0]["url"] == "https://www.dw.com/test"
    assert items[0]["content_status"] == "FULL_TEXT"
    assert items[0]["article_text"].startswith("The full article explains")


def test_gdelt_failure_activates_independent_article_fallback():
    source = SOURCE.read_text(encoding="utf-8")
    assert "if failures or doc_missing_articles:" in source
    assert "_geo_news_fallback(today, doc_clusters)" in source
    assert "GDELT-DOC-ARTIKELFALLBACK: Deutsche Welle RSS | STATUS=AKTIV" in source
    assert 'source = "Deutsche Welle RSS | GDELT-DOC-FALLBACK"' in source
    assert 'status = item.get("status", "REAL_PUBLIC_SECONDARY")' in source


def test_gdelt_429_remains_global_breaker_and_no_broad_retry():
    source = SOURCE.read_text(encoding="utf-8")
    assert "GDELT-DOC: HTTP 429 erkannt - DOC-Circuit-Breaker aktiviert" in source
    assert "GDELT_DOC_MAX_WORKERS = 1" in source
    assert "Nach HTTP 429 keine weitere DOC-Anfrage im selben Lauf." in source
    assert "Kein redundanter Broad-DOC-Call mehr." in source


def test_no_score_or_direction_logic_added_to_geo_layer():
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("def _geo_news_fetch_rss")
    end = source.index("def geopolitics_snapshot", start)
    geo_block = source[start:end]
    assert "score" not in geo_block.lower()
    assert "buy" not in geo_block.lower()
    assert "sell" not in geo_block.lower()


def test_geo_rss_parser_marks_teaser_only_instead_of_claiming_full_text():
    m = _load_module()

    class FakeResponse:
        status_code = 200
        content = b'''<?xml version="1.0"?><rss><channel>
        <item><title>Iran tensions</title><link>https://www.dw.com/test2</link>
        <pubDate>Wed, 23 Sep 2026 12:00:00 +0000</pubDate>
        <teaser>Short context only.</teaser></item>
        </channel></rss>'''

        def raise_for_status(self):
            return None

    original = m.requests.get
    try:
        m.requests.get = lambda *args, **kwargs: FakeResponse()
        items = m._geo_news_fetch_rss(dt.date(2026, 9, 23))
    finally:
        m.requests.get = original

    assert len(items) == 1
    assert items[0]["content_status"] == "TEASER_ONLY"
    assert items[0]["article_text"] == "Short context only."
