"""Deterministic market-data and event normalization helpers for Trading Radar.

These helpers do not infer financial facts. They normalize source rows into one
canonical record and aggregate explicit source statuses without conflating them.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from datetime import datetime, date, timezone
from dataclasses import dataclass
import hashlib
from pathlib import Path
import json
from typing import Any
import re
from urllib.parse import urlsplit

MISSING_TOKENS = {"", "-", "—", "n/a", "na", "nicht verfügbar", "nicht verfuegbar",
                  "unbekannt", "fehlt", "unklar", "none", "null"}
OHLCV_STATUSES = {"VALID", "MISSING", "STALE", "INVALID"}


@dataclass(frozen=True, eq=False)
class _EventVerificationEvidence:
    """Opaque in-process evidence created only by a completed live verifier."""
    kind: str
    event_id: str
    event_name: str
    event_date: str
    event_time: str | None
    timezone: str | None
    requested_url: str
    final_url: str
    http_status: int
    retrieved_at: str
    content_match: bool
    adapter: str
    response_sha256: str
    verified: bool
    detail: str
    press_conference_verified: bool = False

    def __iter__(self):
        # Backward-compatible diagnostic unpacking; production validation uses
        # the opaque evidence object itself, never this textual projection.
        yield self.verified
        yield self.detail


# Identity registration prevents report text or caller-constructed dictionaries
# from being mistaken for a result returned by this module's live verifier.
_VERIFIED_EVIDENCE: dict[int, _EventVerificationEvidence] = {}


def _register_verified_evidence(evidence: _EventVerificationEvidence):
    if evidence.verified and evidence.content_match and evidence.http_status == 200:
        _VERIFIED_EVIDENCE[id(evidence)] = evidence
    return evidence


def _normalized_event_identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _is_forexfactory_source(value: Any) -> bool:
    source = str(value or "")
    if re.search(r"(?i)\bForexFactory\b", source):
        return True
    match = re.search(r"https://[^\s)<>]+", source, flags=re.I)
    if not match:
        return False
    try:
        host = (urlsplit(match.group(0).rstrip(".,;]")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "forexfactory.com" or host.endswith(".forexfactory.com")


def _event_identity_matches(report_name: Any, source_name: Any) -> bool:
    report_identity = _normalized_event_identity(report_name)
    source_identity = _normalized_event_identity(source_name)
    if report_identity == source_identity and report_identity:
        return True
    # The macro snapshot prefixes some event titles with its deterministic
    # canonical category (for example "ADP: ADP Weekly Employment Change").
    if ":" in str(report_name or ""):
        prefix, suffix = str(report_name).split(":", 1)
        if _normalized_event_identity(prefix) in {
            "adp", "nfp", "cpi", "core cpi", "ppi", "pce", "gdp",
            "jolts", "ism", "jobless claims", "fomc",
        } and _normalized_event_identity(suffix) == source_identity:
            return True
    return False


def _is_fomc_press_conference_event(value: Any) -> bool:
    text = _normalized_event_identity(value)
    return bool("fomc" in text and ("press conference" in text or "pressekonferenz" in text))


def _is_fomc_minutes_event(value: Any) -> bool:
    text = _normalized_event_identity(value)
    return bool("fomc" in text and re.search(r"\b(?:minutes|protokoll|sitzungsprotokoll)\b", text))


def _is_fomc_decision_event(value: Any) -> bool:
    text = _normalized_event_identity(value)
    if "fomc" not in text or _is_fomc_minutes_event(value) or _is_fomc_press_conference_event(value):
        return False
    return bool(re.search(r"\b(?:zinsentscheid(?:ung)?|entscheidung|decision|rate decision|statement|fed funds rate)\b", text))


def _fomc_event_kind(value: Any) -> str | None:
    text = _normalized_event_identity(value)
    is_minutes = _is_fomc_minutes_event(value)
    is_press = _is_fomc_press_conference_event(value)
    is_decision_label = bool(
        "fomc" in text
        and re.search(r"\b(?:zinsentscheid(?:ung)?|entscheidung|decision|rate decision|statement|fed funds rate)\b", text)
    )
    if is_minutes:
        return None if (is_press or is_decision_label) else "MINUTES"
    if is_press and is_decision_label:
        return None
    if is_press:
        return "PRESS_CONFERENCE"
    if is_decision_label:
        return "DECISION"
    return None


def _is_trusted_event_evidence(
    evidence: Any, *, kind: str, event_name: Any, event_date: Any
) -> bool:
    if not isinstance(evidence, _EventVerificationEvidence):
        return False
    if _VERIFIED_EVIDENCE.get(id(evidence)) is not evidence:
        return False
    if (not evidence.verified or not evidence.content_match or evidence.http_status != 200
            or evidence.kind != kind or not evidence.requested_url or not evidence.final_url
            or not evidence.retrieved_at or not evidence.response_sha256):
        return False
    try:
        expected_date = date.fromisoformat(str(event_date or "")[:10]).isoformat()
        evidence_date = date.fromisoformat(evidence.event_date[:10]).isoformat()
    except (TypeError, ValueError):
        return False
    if expected_date != evidence_date:
        return False
    if kind == "FOREXFACTORY":
        return bool(
            _normalized_event_identity(event_name)
            and _event_identity_matches(event_name, evidence.event_name)
            and evidence.adapter == "forexfactory_weekly_json_adapter"
        )
    if kind == "FOMC":
        event_kind = _fomc_event_kind(event_name)
        expected = {
            "DECISION": ("FOMC_DECISION", "14:00"),
            "PRESS_CONFERENCE": ("FOMC_PRESS_CONFERENCE", "14:30"),
        }.get(event_kind)
        if expected is None:
            return False
        expected_id, expected_time = expected
        return bool(
            evidence.event_id == expected_id
            and evidence.event_time == expected_time
            and evidence.adapter == "federal_reserve_month_calendar_verifier"
            and _normalized_event_identity(evidence.event_name)
            == ("fomc zinsentscheid" if event_kind == "DECISION" else "fomc press conference")
        )
    return False


def _verify_forexfactory_event(event_name: str, event_date: str, *, requester=None):
    """Perform a fresh ForexFactory weekly JSON adapter request and bind one event."""
    requested_url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    if requester is None:
        import requests
        requester = requests.get
    try:
        response = requester(
            requested_url, timeout=20,
            headers={"User-Agent": "NeuberMacro/1.0 TradingRadar-event-verification"},
        )
        status = int(getattr(response, "status_code", 0) or 0)
        final_url = str(getattr(response, "url", requested_url) or requested_url)
        if status != 200:
            return None, f"ForexFactory-Adapter HTTP {status}; Event nicht verifiziert"
        final = urlsplit(final_url)
        if (final.scheme.lower() != "https" or final.hostname != "nfs.faireconomy.media"
                or final.path.rstrip("/") != "/ff_calendar_thisweek.json"):
            return None, "ForexFactory-Adapter: unzulässige finale URL; Event nicht verifiziert"
        payload = response.json()
        if not isinstance(payload, list):
            return None, "ForexFactory-Adapter: JSON-Antwort ist keine Eventliste"
        retrieved_at = datetime.now(timezone.utc).isoformat()
        raw_text = getattr(response, "text", None)
        if not isinstance(raw_text, str) or not raw_text:
            raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        response_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        expected_date = date.fromisoformat(str(event_date)[:10]).isoformat()
        matches = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            country = str(item.get("country") or "").strip().casefold()
            if country not in {"usd", "us", "usa", "united states", "united states of america"}:
                continue
            title = _clean(item.get("title") or item.get("event") or item.get("name"))
            raw_date = str(item.get("date") or "").strip()
            try:
                actual_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date().isoformat()
            except ValueError:
                try:
                    actual_date = date.fromisoformat(raw_date[:10]).isoformat()
                except ValueError:
                    continue
            if title and actual_date == expected_date and _event_identity_matches(event_name, title):
                matches.append((item, title, actual_date))
        if len(matches) != 1:
            return None, (
                f"ForexFactory-Adapter: {len(matches)} exakt passende Events für "
                f"{event_name!r} am {expected_date}; Event nicht verifiziert"
            )
        item, title, actual_date = matches[0]
        event_id = str(item.get("id") or _normalized_event_identity(title))
        event_time = _clean(item.get("time"))
        event_timezone = _clean(item.get("timezone") or item.get("tz"))
        detail = (
            f"ForexFactory LIVE-Adapter: HTTP 200; Event={title}; Datum={actual_date}; "
            f"Abruf={requested_url}; Final={final_url}"
        )
        evidence = _EventVerificationEvidence(
            kind="FOREXFACTORY", event_id=event_id, event_name=title, event_date=actual_date,
            event_time=event_time, timezone=event_timezone, requested_url=requested_url,
            final_url=final_url, http_status=status, retrieved_at=retrieved_at,
            content_match=True, adapter="forexfactory_weekly_json_adapter",
            response_sha256=response_hash, verified=True, detail=detail,
        )
        return _register_verified_evidence(evidence), detail
    except Exception as exc:
        return None, f"ForexFactory-Adapter fehlgeschlagen: {type(exc).__name__}: {exc}"

SECTOR_ETF_LABELS = {
    "XLB": "Rohstoff-ETF",
    "XLC": "Kommunikations-ETF",
    "XLE": "Energie-ETF",
    "XLF": "Finanz-ETF",
    "XLI": "Industrie-ETF",
    "XLK": "Technologie-ETF",
    "XLP": "Basiskonsum-ETF",
    "XLRE": "Immobilien-ETF",
    "XLU": "Versorger-ETF",
    "XLV": "Gesundheit-ETF",
    "XLY": "Zyklischer-Konsum-ETF",
}


def _is_valid_calendar_event_date(value: str) -> bool:
    """Accept supported ISO/German date forms only when the calendar date exists."""
    candidate = str(value or "").strip()
    if not candidate:
        return False
    iso_candidate = re.sub(r"\s+(?:UTC|GMT)$", "+00:00", candidate, flags=re.I)
    try:
        datetime.fromisoformat(iso_candidate.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", candidate):
        try:
            date.fromisoformat(f"{candidate[6:10]}-{candidate[3:5]}-{candidate[0:2]}")
            return True
        except ValueError:
            return False
    return False


def _explicit_date_from_period(value: Any) -> str | None:
    """Return one unambiguous valid date explicitly stated in a period field.

    Relative wording is never interpreted. Multiple dates/ranges and invalid
    date tokens are rejected rather than guessed.
    """
    if value is None:
        return None
    text = str(value)
    candidates = re.findall(
        r"(?<!\d)(?:20\d{2}-\d{2}-\d{2}|\d{2}\.\d{2}\.20\d{2})(?!\d)",
        text,
    )
    if len(candidates) == 0:
        return None
    normalized = []
    for candidate in candidates:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
                parsed = date.fromisoformat(candidate)
            else:
                parsed = date.fromisoformat(
                    f"{candidate[6:10]}-{candidate[3:5]}-{candidate[0:2]}"
                )
        except ValueError:
            return None
        normalized.append(parsed.isoformat())
    unique = list(dict.fromkeys(normalized))
    return unique[0] if len(unique) == 1 else None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).strip().split())
    return None if value.casefold() in MISSING_TOKENS else value


def _legacy_event_source_evidence(value: Any) -> bool:
    """Keep the pre-existing provider/source recognition for non-FF/FOMC events."""
    source = _clean(value) or ""
    if not source or re.search(r"(?i)\b(?:CACHE|CACHED|STALE|VERALTET)\b", source):
        return False
    if re.search(r"(?i)https://api\.stlouisfed\.org/fred/series/observations", source):
        return False
    if re.search(r"https?://", source, re.I):
        return _is_verified_event_source(source)
    return bool(re.search(
        r"(?i)(?:forexfactory|\bfred\b|federal reserve|official|"
        r"\bbls\b|bureau of labor|\bbea\b|\becb\b|\beia\b|"
        r"\bism\b|\badp\b|\bcme\b|\bsec\b|treasury|"
        r"opec|world bank|imf|yahoo finance|trading economics)",
        source,
    ))


def _has_live_verification(
    value: Any,
    source_value: Any = None,
    *,
    event_name: Any = None,
    event_date: Any = None,
    evidence: Any = None,
) -> bool:
    """Validate structured evidence for FF/FOMC; retain legacy text checks elsewhere."""
    source = _clean(source_value) or ""
    if _is_forexfactory_source(source):
        return _is_verified_event_source(source) and _is_trusted_event_evidence(
            evidence, kind="FOREXFACTORY", event_name=event_name, event_date=event_date
        )
    if re.search(r"(?i)\bFOMC\b", str(event_name or "")):
        return _is_trusted_event_evidence(
            evidence, kind="FOMC", event_name=event_name, event_date=event_date
        )

    text = _clean(value)
    if not text:
        return _is_verified_event_source(source)
    if re.search(r"(?i)\bNICHT\s+(?:LIVE[_ ]?)?VERIFIZIERT\b", text):
        return False
    if not re.match(r"(?i)^\s*(?:\(?\s*)LIVE_VERIFIZIERT\b", text):
        return False
    if not re.search(r"(?i)\bHTTP\s*200\b", text):
        return False
    fetched = re.search(r"(?i)\bAbruf\s*:\s*(https://[^\s)<>]+)", text)
    declared = re.search(r"https://[^\s)<>]+", source, flags=re.I)
    if not fetched or not declared:
        return False
    try:
        fetched_url = urlsplit(fetched.group(1).rstrip(".,;]"))
        declared_url = urlsplit(declared.group(0).rstrip(".,;]"))
    except ValueError:
        return False
    if (
        (fetched_url.hostname or "").lower().rstrip(".")
        != (declared_url.hostname or "").lower().rstrip(".")
        or (fetched_url.path or "/").rstrip("/").casefold()
        != (declared_url.path or "/").rstrip("/").casefold()
        or not _is_verified_event_source(fetched_url.geturl())
    ):
        return False
    expected_name = _normalized_event_identity(event_name)
    expected_date = str(event_date or "")[:10]
    if not expected_name or not re.search(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)", text):
        return False
    date_match = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", text)
    if not date_match or date_match.group(1) != expected_date:
        return False
    event_match = re.search(
        r"(?i)(?:event|ereignis|bestätigt|bestaetigt)\s*[:=]\s*([^;|]+)", text
    )
    if not event_match:
        return False
    evidence_name = re.sub(r"\s+am\s+20\d{2}-\d{2}-\d{2}.*$", "", event_match.group(1)).strip()
    return _event_identity_matches(event_name, evidence_name)


def _is_verified_event_source(value: Any) -> bool:
    """Accept only explicit HTTPS calendar/release URLs on trusted domains.

    Provider names, search snippets, observation APIs and arbitrary URLs are
    not evidence that an event's publication date was verified.
    """
    source = _clean(value)
    if not source:
        return False
    if re.search(r"(?i)\b(?:CACHE|CACHED|STALE|VERALTET)\b", source):
        return False
    # The project's ForexFactory adapter stamps LIVE only after a successful
    # calendar HTTP fetch; CACHE/UNAVAILABLE values are rejected above.
    if re.search(r"(?i)\bForexFactory\s*\(\s*LIVE\s*\)", source):
        return True
    url_match = re.search(r"https://[^\s)<>]+", source, flags=re.I)
    if not url_match:
        return False
    try:
        parsed = urlsplit(url_match.group(0).rstrip(".,;]"))
    except ValueError:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return False
    host = parsed.hostname.lower().rstrip(".")
    path = (parsed.path or "/").lower()
    if host == "fred.stlouisfed.org" or host.endswith(".fred.stlouisfed.org") or host == "api.stlouisfed.org":
        return False

    trusted_hosts = {
        "forexfactory.com", "www.forexfactory.com",
        "bls.gov", "www.bls.gov",
        "dol.gov", "www.dol.gov",
        "adp.com", "www.adp.com",
        "federalreserve.gov", "www.federalreserve.gov",
        "bea.gov", "www.bea.gov",
        "ecb.europa.eu",
        "eia.gov", "www.eia.gov",
        "ismworld.org", "www.ismworld.org",
        "cmegroup.com", "www.cmegroup.com",
        "sec.gov", "www.sec.gov",
        "treasury.gov", "home.treasury.gov",
        "opec.org", "www.opec.org",
        "worldbank.org", "www.worldbank.org",
        "imf.org", "www.imf.org",
    }
    if host not in trusted_hosts:
        return False

    # A trusted domain is not sufficient: the URL must identify a calendar,
    # schedule, release/newsroom page or a provider's calendar endpoint.
    calendar_path = bool(re.search(
        r"(?:calendar|schedule|release|releases|newsroom|press-release|press_release)",
        path,
    ))
    if host.endswith("forexfactory.com"):
        return path.rstrip("/") == "/calendar"
    if host.endswith("bls.gov"):
        return path.startswith("/schedule/news_release/")
    if host.endswith("dol.gov"):
        return calendar_path and ("release" in path or "newsroom" in path)
    if host.endswith("adp.com"):
        return calendar_path
    if host.endswith("federalreserve.gov"):
        return calendar_path or path.startswith("/newsevents/")
    return calendar_path


def normalize_event(event: Mapping[str, Any], *, verification_evidence=None) -> dict[str, Any]:
    """Normalize one event. Missing fields are represented once as null/status."""
    out = {
        "event_id": _clean(event.get("event_id") or event.get("id")),
        "name": _clean(event.get("name") or event.get("event") or event.get("kennzahl")),
        "date": (
            _clean(event.get("date") or event.get("datum"))
            or _explicit_date_from_period(event.get("period") or event.get("berichtszeitraum"))
        ),
        "time": _clean(event.get("time") or event.get("uhrzeit")),
        "timezone": _clean(event.get("timezone") or event.get("zeitzone")),
        "period": _clean(event.get("period") or event.get("berichtszeitraum")),
        "previous": _clean(event.get("previous") or event.get("last_value") or event.get("letzter_wert")),
        "consensus": _clean(event.get("consensus") or event.get("konsens")),
        "actual": _clean(event.get("actual") or event.get("istwert")),
        "source": _clean(event.get("source") or event.get("quelle")),
        "source_verification": _clean(
            event.get("source_verification") or event.get("verification")
            or event.get("quellenpruefung") or event.get("quellenprüfung")
        ),
    }
    live_verified = _has_live_verification(
        out["source_verification"], out["source"],
        event_name=out["name"], event_date=out["date"], evidence=verification_evidence,
    )
    out["source_verified"] = _is_verified_event_source(out["source"]) and live_verified
    out["date_valid"] = bool(out["date"] and _is_valid_calendar_event_date(out["date"]))
    missing = [key for key in ("name",) if out[key] is None]
    if not out["date_valid"]:
        missing.append("date")
    if not out["source_verified"]:
        missing.append("source")
    out["status"] = "INCOMPLETE" if missing else "READY"
    out["missing_required"] = missing
    out["time_verified"] = bool(out["time"] and out["timezone"])
    return out


def deduplicate_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate events by stable ID, else by normalized name/date/source."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in events:
        item = normalize_event(raw)
        key = (item["event_id"] or "", item["name"] or "", item["date"] or "", item["source"] or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def classify_ohlcv_row(row: Mapping[str, Any], *, cutoff: datetime) -> str:
    """Classify a symbol row as VALID/MISSING/STALE/INVALID using explicit inputs."""
    values = [row.get(k) for k in ("open", "high", "low", "close", "volume", "timestamp")]
    if any(v is None or (isinstance(v, str) and v.strip().casefold() in MISSING_TOKENS) for v in values):
        return "MISSING"
    try:
        numbers = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
        stamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError):
        return "INVALID"
    if stamp.tzinfo is None:
        return "INVALID"
    op, hi, lo, close, vol = numbers
    if min(op, hi, lo, close) <= 0 or vol < 0 or hi < max(op, lo, close) or lo > min(op, hi, close):
        return "INVALID"
    if stamp > cutoff:
        return "INVALID"  # future-dated data is invalid for this cutoff
    if (cutoff - stamp).total_seconds() > 7 * 24 * 3600:
        return "STALE"
    return "VALID"


def summarize_ohlcv_status(statuses: Iterable[str]) -> dict[str, int]:
    """Count each explicit OHLCV status exactly once; unknown values fail closed."""
    counts = {status: 0 for status in sorted(OHLCV_STATUSES)}
    for status in statuses:
        if status not in OHLCV_STATUSES:
            raise ValueError(f"Unbekannter OHLCV-Status: {status!r}")
        counts[status] += 1
    counts["TOTAL"] = sum(counts.values())
    return counts




def append_live_market_snapshot(report_text: str, *, fetcher=None) -> tuple[str, list[str]]:
    """Append source-stamped live market data and a common-cutoff 1M/3M matrix."""
    import pandas as pd
    if fetcher is None:
        try:
            import yfinance as yf
        except ImportError:
            return report_text, ["Markt-Snapshot nicht erstellt: yfinance nicht installiert"]
        def fetcher(symbols):
            return yf.download(tickers=symbols, period="6mo", interval="1d",
                               auto_adjust=True, group_by="ticker", progress=False, threads=True)
    symbols = [
        "^GSPC", "^IXIC", "^RUT", "^VIX", "DX-Y.NYB", "^TNX", "TLT",
        "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        "SPY", "VGK", "EEM", "EWJ", "GLD", "SLV", "USO", "UUP", "BTC-USD",
    ]
    try:
        data = fetcher(symbols)
    except Exception as exc:
        return report_text, [f"Markt-Snapshot fehlgeschlagen: {type(exc).__name__}: {exc}"]
    if data is None or getattr(data, "empty", True):
        return report_text, ["Markt-Snapshot fehlgeschlagen: keine Kursdaten geliefert"]

    def series_for(symbol):
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if (symbol, "Close") in data.columns:
                    values = data[(symbol, "Close")].dropna()
                elif ("Close", symbol) in data.columns:
                    values = data[("Close", symbol)].dropna()
                else:
                    return None
            else:
                values = data["Close"].dropna() if "Close" in data else None
            if values is None or len(values) < 2:
                return None
            values = values.copy()
            values.index = pd.to_datetime(values.index, utc=True, errors="coerce")
            values = values[~values.index.isna()].sort_index()
            return values[~values.index.duplicated(keep="last")]
        except (KeyError, TypeError, AttributeError):
            return None

    def latest(symbol):
        values = series_for(symbol)
        try:
            return float(values.iloc[-1]) if values is not None else None
        except (ValueError, TypeError, IndexError):
            return None

    def pct(symbol, periods):
        values = series_for(symbol)
        if values is None or len(values) <= periods:
            return None
        try:
            base = float(values.iloc[-1-periods])
            return (float(values.iloc[-1]) / base - 1) * 100 if base else None
        except (ValueError, TypeError, ZeroDivisionError):
            return None

    def fmt(value, suffix=""):
        return f"{value:+.2f}{suffix}" if value is not None else "NICHT VERFÜGBAR"

    now = datetime.now().astimezone().astimezone(__import__("datetime").timezone.utc).replace(microsecond=0).isoformat()
    lines = ["", "**Live-Marktbelege (Yahoo Finance; Schlusskurse, keine Prognose):**",
             f"- Datenstand UTC: {now}"]
    for symbol, label in (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq Composite"), ("^RUT", "Russell 2000")):
        close, r1, r3 = latest(symbol), pct(symbol, 21), pct(symbol, 63)
        lines.append(
            f"- Index {label} ({symbol}): letzter Schluss {close:.2f}; 1M {fmt(r1, '%')}; 3M {fmt(r3, '%')}."
            if close is not None else f"- Index {label} ({symbol}): NICHT VERFÜGBAR."
        )
    vix, dxy, tnx_raw, tlt = latest("^VIX"), latest("DX-Y.NYB"), latest("^TNX"), pct("TLT", 21)
    tnx = (tnx_raw / 10.0 if abs(tnx_raw) >= 10.0 else tnx_raw) if tnx_raw is not None else None
    tnx_series = series_for("^TNX")
    tnx_observation_date = (
        tnx_series.index[-1].isoformat() if tnx_series is not None and len(tnx_series)
        else "NICHT VERFÜGBAR"
    )
    lines.extend([
        f"- Volatilität VIX: {vix:.2f}" if vix is not None else "- Volatilität VIX: NICHT VERFÜGBAR.",
        f"- Dollar-Proxy DXY (DX-Y.NYB): {dxy:.2f}" if dxy is not None else "- Dollar-Proxy DXY: NICHT VERFÜGBAR.",
        (
            f"- US-10J-Rendite (^TNX): {tnx:.2f}% | Quelle: Yahoo Finance (^TNX); "
            f"Abrufzeit: {now}; Datenstichtag: {tnx_observation_date}; Einheit: %; "
            "Verzögerung: Tages-Schlusskurs; keine Intraday-Aktualität im gelieferten Snapshot."
        ) if tnx is not None else "- US-10J-Rendite: NICHT VERFÜGBAR.",
        f"- Anleihe-ETF TLT 1M: {fmt(tlt, '%')}.",
    ])

    # Keep the quantitative breadth observation inside the canonical Market-First
    # field as well as in the live-evidence appendix; the validator intentionally
    # does not borrow numbers from unrelated sections.
    sector_etfs = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
    above = total = 0
    for symbol in sector_etfs:
        values = series_for(symbol)
        if values is not None and len(values) >= 50:
            total += 1
            above += int(float(values.iloc[-1]) > float(values.iloc[-50:].mean()))
    lines.append(f"- Marktbreite-Proxy: {above}/{total} Sektor-ETFs über ihrem 50-Tage-Durchschnitt."
                 if total else "- Marktbreite-Proxy: NICHT VERFÜGBAR.")
    if total:
        breadth_fact = f" Quantitative Marktbreite: {above}/{total} Sektor-ETFs über ihrem 50-Tage-Durchschnitt."
        report_text = re.sub(
            r"(?im)^(\s*(?:[-*•]\s*)?(?:\*\*)?Indizes/Marktbreite(?:\*\*)?\s*:\s*)([^\r\n]*)$",
            lambda m: m.group(1) + m.group(2).rstrip(". ") + ";" + breadth_fact,
            report_text,
            count=1,
        )
    lines.append("- Quelle: Yahoo Finance (Ticker in den Zeilen; Tages-Schlusskurse).")

    # M-07: only the canonical Zinsen/Renditen observation is a market source.
    # Thesis triggers, invalidation levels and other narrative percentages are
    # not observations and must never create a Treasury source conflict.
    def _balanced_local_parenthetical(value: str) -> str:
        value = value.lstrip()
        if not value.startswith("("):
            return ""
        depth = 0
        for index, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return value[1:index]
        return ""

    def _macro_provenance(source_line: str, value_match) -> dict[str, str]:
        annotation = _balanced_local_parenthetical(source_line[value_match.end():])
        patterns = {
            "source": r"(?i)(?:^|[;|])\s*(?:Quelle|Source)\s*[:=]\s*",
            "retrieved": r"(?i)(?:^|[;|])\s*(?:Abruf|Abrufzeit|Abrufzeitpunkt|Retrieved(?:_At)?)\s*[:=]\s*",
            "observed": r"(?i)(?:^|[;|])\s*(?:Datenstand|Datenstichtag|Beobachtungsdatum|Observation_Date|DATA_DATE)\s*[:=]\s*",
            "unit": r"(?i)(?:^|[;|])\s*(?:Einheit|Unit)\s*[:=]\s*",
            "delay": r"(?i)(?:^|[;|])\s*(?:Verzögerung|Verzoegerung|Delay(?:_Days)?)\s*[:=]\s*",
        }

        def field_value(pattern):
            match = re.search(pattern, annotation)
            if not match:
                return "NICHT VERFÜGBAR"
            start = match.end()
            depth = 0
            for index in range(start, len(annotation)):
                char = annotation[index]
                if char == "(":
                    depth += 1
                elif char == ")" and depth:
                    depth -= 1
                elif char in ";|" and depth == 0:
                    return annotation[start:index].strip()
            return annotation[start:].strip().rstrip(")").strip()

        return {key: field_value(pattern) for key, pattern in patterns.items()}

    macro_observations = []
    treasury_value_pattern = re.compile(
        r"(?i)(?:US\s*[- ]?\s*10Y(?:\s*[- ]?Treasury)?"
        r"|10Y\s*[- ]?US\s*[- ]?Treasury"
        r"|10\s*[- ]?year\s+US\s+Treasury"
        r"|10\s*[- ]?year\s+US\s+yield"
        r"|10\s*[- ]?jährig(?:er|e|en)?\s+US\s*[- ]?(?:Treasury|Rendite))"
        r"[^\r\n]{0,100}?(\d+(?:[,.]\d+)?)\s*%"
    )
    for source_line in report_text.splitlines():
        canonical_rate_line = bool(re.match(
            r"(?i)^\s*(?:[-*•]\s*)?(?:\*\*)?Zinsen/Renditen(?:\*\*)?\s*:",
            source_line,
        ))
        # Also retain an explicitly labelled standalone Treasury observation
        # used by older report layouts; generic prose and thesis triggers remain
        # excluded because they do not begin with an observation label.
        legacy_explicit_observation = bool(re.match(
            r"(?i)^\s*(?:[-*•]\s*)?(?:10[- ]?jährige[rn]?\s+US[- ]?Treasury(?:[- ]?Rendite)?"
            r"|US[- ]?10Y(?:[- ]?Treasury)?(?:[- ]?Rendite)?"
            r"|10Y[- ]?US[- ]?Treasury)\s*[:=]",
            source_line,
        ))
        if not (canonical_rate_line or legacy_explicit_observation):
            continue
        for match in treasury_value_pattern.finditer(source_line):
            try:
                observed_value = float(match.group(1).replace(",", "."))
            except ValueError:
                continue
            macro_observations.append((observed_value, _macro_provenance(source_line, match)))

    if tnx is not None:
        # Keep every distinct value/provenance pair; conflicting metadata must
        # remain visible so M-07 can reject ambiguity instead of hiding it.
        distinct_records = {}
        for value, provenance in macro_observations:
            key = (round(value, 6), tuple(provenance.items()))
            distinct_records[key] = (value, provenance)
        for macro_value, provenance in sorted(distinct_records.values(), key=lambda item: item[0]):
            if abs(macro_value - tnx) < 0.001:
                continue
            macro_fields = (
                f"Quelle: {provenance['source']}; "
                f"Abrufzeit: {provenance['retrieved']}; "
                f"Datenstichtag: {provenance['observed']}; "
                f"Einheit: {provenance['unit']}; "
                f"Verzögerung: {provenance['delay']}"
            )
            live_fields = (
                f"Quelle: Yahoo Finance (^TNX); Abrufzeit: {now}; "
                f"Datenstichtag: {tnx_observation_date}; Einheit: %; "
                "Verzögerung: Tages-Schlusskurs"
            )
            macro_date_match = re.search(r"\d{4}-\d{2}-\d{2}", provenance["observed"])
            live_date_match = re.search(r"\d{4}-\d{2}-\d{2}", tnx_observation_date)
            dates_differ = bool(
                macro_date_match and live_date_match
                and macro_date_match.group(0) != live_date_match.group(0)
            )
            observation_label = (
                "ZEITLICH UNTERSCHIEDLICHE BEOBACHTUNGEN"
                if dates_differ else "GETRENNT DOKUMENTIERTE US-10Y-BEOBACHTUNGEN"
            )
            lines.append(
                f"- {observation_label} | Instrument: US-10Y Treasury | "
                f"Makro-Wert: {macro_value:.4f}% "
                f"(Makro-Provenienz: {macro_fields}) | "
                f"Live-Wert: {tnx:.4f}% ({live_fields}) | "
                "Beide Werte bleiben mit eigenem Datenstichtag und eigener Provenienz erhalten; "
                "sie werden nicht als gleichzeitige Messung verglichen und nicht überschrieben."
            )

    groups = {
        "Indizes": [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^RUT", "Russell 2000")],
        "Regionen": [("SPY", "USA"), ("VGK", "Europa"), ("EEM", "Emerging Markets"), ("EWJ", "Japan")],
        "Sektoren": [(x, SECTOR_ETF_LABELS.get(x, "Sektor-ETF")) for x in sector_etfs],
        "Anlageklassen": [("GLD", "Gold"), ("SLV", "Silber"), ("USO", "Öl"),
                          ("TLT", "US-Anleihen"), ("UUP", "US-Dollar"), ("BTC-USD", "Bitcoin")],
    }
    all_series = {sym: series_for(sym) for rows in groups.values() for sym, _ in rows}
    # Volatility and dollar observations are part of the same regime snapshot,
    # so they participate in (and must match) the common observation cutoff.
    all_series["^VIX"] = series_for("^VIX")
    all_series["DX-Y.NYB"] = series_for("DX-Y.NYB")
    all_series = {sym: ser for sym, ser in all_series.items() if ser is not None and not ser.empty}
    common_cutoff = min((ser.index.max() for ser in all_series.values()), default=None)
    if common_cutoff is not None:
        def close_at_common_cutoff(symbol):
            series = all_series.get(symbol)
            if series is None:
                return None
            eligible = series.loc[series.index <= common_cutoff]
            if eligible.empty or eligible.index[-1] != common_cutoff:
                return None
            try:
                return float(eligible.iloc[-1])
            except (TypeError, ValueError):
                return None
        vix = close_at_common_cutoff("^VIX")
        dxy = close_at_common_cutoff("DX-Y.NYB")
        for index, line in enumerate(lines):
            if line.startswith("- Volatilität VIX:"):
                lines[index] = f"- Volatilität VIX: {vix:.2f}" if vix is not None else "- Volatilität VIX: NICHT VERFÜGBAR."
            elif line.startswith("- Dollar-Proxy DXY"):
                lines[index] = f"- Dollar-Proxy DXY (DX-Y.NYB): {dxy:.2f}" if dxy is not None else "- Dollar-Proxy DXY: NICHT VERFÜGBAR."
    # M-02 consumes the same real market-data cutoff as the relative-strength
    # matrix. Retrieval time is not substituted for an observation date.
    if common_cutoff is not None:
        cutoff_date = common_cutoff.date().isoformat()
        report_text = re.sub(
            r"(?im)^\s*Marktregime-Datenstichtag\s*:[^\r\n]*\n?", "", report_text
        )
        regime_heading = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?Marktregime(?:\*\*)?\s*:",
            report_text,
        )
        if regime_heading:
            line_end = report_text.find("\n", regime_heading.start())
            if line_end < 0:
                line_end = len(report_text)
            report_text = (
                report_text[:line_end] + "\nMarktregime-Datenstichtag: " + cutoff_date
                + report_text[line_end:]
            )
    # The report gate deliberately validates the canonical risk field, not a
    # detached appendix. Copy only the measured VIX/DXY values into that field.
    if vix is not None or dxy is not None:
        def enrich_risk_line(match):
            original = match.group(0)
            suffix = []
            if vix is not None:
                suffix.append(f"VIX: {vix:.2f}")
            else:
                suffix.append("VIX: NICHT VERFÜGBAR")
            if dxy is not None:
                suffix.append(f"DXY: {dxy:.2f}")
            else:
                suffix.append("DXY: NICHT VERFÜGBAR")
            # Keep the original M2/liquidity narrative while replacing only
            # the broken entity-resolver placeholder for VIX.
            original = re.sub(r"(?i)Name nicht aufgelöst\s*\(VIX\)", "VIX", original)
            original = re.sub(r"(?i)Name nicht aufgeloest\s*\(VIX\)", "VIX", original)
            return original.rstrip(". ") + "; " + "; ".join(suffix)
        report_text = re.sub(
            r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?Dollar/Liquidität/Volatilität(?:\*\*)?\s*:[^\r\n]*$",
            enrich_risk_line, report_text, count=1,
        )
    matrix_results = {}
    matrix_lines = ["Relative-Stärke-Matrix (adjusted close; 1M=21 Handelstage, 3M=63 Handelstage):"]
    matrix_lines.append(f"Gemeinsamer Datenstichtag: {common_cutoff.isoformat()}" if common_cutoff is not None
                        else "Gemeinsamer Datenstichtag: NICHT VERFÜGBAR")
    for group, rows in groups.items():
        row_text = []
        for sym, label in rows:
            ser = all_series.get(sym)
            eligible = ser.loc[ser.index <= common_cutoff] if ser is not None and common_cutoff is not None else None
            # A common cutoff is only comparable when every included series has
            # an observation exactly at that cutoff. Do not silently substitute
            # an older last close for a missing market session.
            if (eligible is None or eligible.empty or common_cutoff is None
                    or eligible.index[-1] != common_cutoff or len(eligible) <= 63):
                row_text.append(
                    f"{label} ({sym}) 1M NICHT VERFÜGBAR (kein Kurs exakt am gemeinsamen Stichtag); "
                    "3M NICHT VERFÜGBAR (kein Kurs exakt am gemeinsamen Stichtag)"
                )
                continue
            last = float(eligible.iloc[-1])
            base1, base3 = float(eligible.iloc[-22]), float(eligible.iloc[-64])
            if last <= 0 or base1 <= 0 or base3 <= 0:
                row_text.append(f"{label} ({sym}) 1M NICHT VERFÜGBAR (Datenlücke); 3M NICHT VERFÜGBAR (Datenlücke)")
                continue
            r1, r3 = (last/base1-1)*100, (last/base3-1)*100
            matrix_results[sym] = (r1, r3)
            row_text.append(f"{label} ({sym}) 1M {r1:+.2f}%; 3M {r3:+.2f}%")
        matrix_lines.append(f"{group}:")
        matrix_lines.extend(f"- {row}" for row in row_text)
    if matrix_results:
        ranked = sorted(matrix_results.items(), key=lambda item: item[1][1])
        def display_matrix_symbol(symbol):
            label = SECTOR_ETF_LABELS.get(symbol)
            return f"{label} ({symbol})" if label else symbol
        matrix_lines.extend([
            "Gewinner: " + ", ".join(f"{display_matrix_symbol(sym)} ({v[1]:+.2f}% 3M)" for sym, v in ranked[-3:]),
            "Verlierer: " + ", ".join(f"{display_matrix_symbol(sym)} ({v[1]:+.2f}% 3M)" for sym, v in ranked[:3]),
            f"Divergenz: {max(v[1] for v in matrix_results.values())-min(v[1] for v in matrix_results.values()):.2f} Prozentpunkte (3M-Spannweite)",
        ])
    else:
        matrix_lines.extend([
            "Gewinner: NICHT VERFÜGBAR (Datenlücke)",
            "Verlierer: NICHT VERFÜGBAR (Datenlücke)",
            "Divergenz: NICHT VERFÜGBAR (Datenlücke)",
        ])
    block = "\n".join(lines) + "\n"
    matrix_block = "\n".join(matrix_lines) + "\n"

    # Idempotent: remove prior live block and prior matrix before insertion.
    report_text = re.sub(
        r"(?ms)\n\*\*Live-Marktbelege \(Yahoo Finance; Schlusskurse, keine Prognose\):\*\*"
        r".*?(?=\n[ \t]*(?:#{1,6}[ \t]*)?2[ \t]*[.·][ \t]*Thesen-Update\b[^\r\n]*$)",
        "", report_text,
    )
    report_text = re.sub(
        r"(?ms)\nRelative-Stärke-Matrix \(adjusted close;.*?(?=\n[ \t]*(?:#{1,6}[ \t]*)?5[ \t]*[.·][ \t]*Technische Kandidaten\b)",
        "", report_text,
    )
    section_two_heading = r"^[ \t]*(?:#{1,6}[ \t]*)?2[ \t]*[.·][ \t]*Thesen-Update\b[^\r\n]*$"
    insertion = re.search(section_two_heading, report_text, re.I | re.M)
    if not insertion:
        return report_text, ["Markt-Snapshot vorhanden, aber Abschnitt 2 für Einfügung nicht auffindbar"]
    report_text = report_text[:insertion.start()] + block + "\n" + report_text[insertion.start():]
    section4 = re.search(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?4[ \t]*[.·][ \t]*Assetklassenvergleich[^\r\n]*$", report_text)
    section5 = re.search(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?5[ \t]*[.·][ \t]*Technische Kandidaten[^\r\n]*$", report_text)
    if section4 and section5 and section4.start() < section5.start():
        report_text = report_text[:section5.start()] + "\n" + matrix_block + "\n" + report_text[section5.start():]
    return report_text, []



def _contains_calendar_day_number(text: str, day: int) -> bool:
    """Match a standalone calendar day without mistaking clock digits for a date."""
    without_clock_times = re.sub(
        r"\b\d{1,2}(?::\d{2})?\s*(?:a|p)\s*\.?\s*m\.?\b",
        " ",
        str(text or ""),
        flags=re.I,
    )
    return bool(re.search(rf"(?<!\d){int(day)}(?!\d)", without_clock_times))


def _fomc_calendar_date_match(
    text: str, event_date: date, *, page_context: str = ""
) -> bool:
    """Match a single day or final day of an explicit range, using page month/year only as context."""
    month = event_date.strftime("%B")
    year = str(event_date.year)
    patterns = [
        re.compile(
            rf"\b{re.escape(month)}\s+(\d{{1,2}})\s*(?:[-–]\s*(\d{{1,2}}))?\s*,?\s+{year}\b",
            re.I,
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(text):
            start_day = int(match.group(1))
            end_day = int(match.group(2) or match.group(1))
            if event_date.day == end_day and start_day <= end_day:
                return True
    # Some Fed table rows omit the year because it is present in the page title.
    page_has_month_year = bool(
        re.search(rf"\b{re.escape(month)}\s+{year}\b", page_context, re.I)
    )
    if page_has_month_year:
        no_year = re.compile(
            rf"\b{re.escape(month)}\s+(\d{{1,2}})\s*(?:[-–]\s*(\d{{1,2}}))?\b", re.I
        )
        for match in no_year.finditer(text):
            start_day = int(match.group(1))
            end_day = int(match.group(2) or match.group(1))
            if event_date.day == end_day and start_day <= end_day:
                return True
    return False


def _verify_fomc_official_source(
    event_date: date, *, requester=None, event_kind: str = "DECISION"
) -> _EventVerificationEvidence:
    """Return opaque evidence for one specific FOMC event from a real Fed request."""
    event_kind = str(event_kind or "DECISION").strip().upper()
    if event_kind not in {"DECISION", "PRESS_CONFERENCE"}:
        raise ValueError("event_kind must be DECISION or PRESS_CONFERENCE")
    expected_id, expected_name, expected_time = (
        ("FOMC_DECISION", "FOMC Zinsentscheid", "14:00")
        if event_kind == "DECISION"
        else ("FOMC_PRESS_CONFERENCE", "FOMC Press Conference", "14:30")
    )
    month_slug = f"{event_date:%Y}-{event_date.strftime('%B').lower()}"
    urls = [
        f"https://www.federalreserve.gov/newsevents/{month_slug}.htm?view=calendar",
        f"https://www.federalreserve.gov/newsevents/{month_slug}.htm",
    ]
    if requester is None:
        import requests
        requester = requests.get

    errors = []
    responses = []
    for requested_url in urls:
        try:
            response = requester(
                requested_url, timeout=12,
                headers={"User-Agent": "TradingRadar/1.1 (official-event-verification)"},
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            final_url = str(getattr(response, "url", requested_url) or requested_url)
            if status_code != 200:
                errors.append(f"{requested_url} -> HTTP {status_code}")
                continue
            parsed_final = urlsplit(final_url)
            expected_final_path = f"/newsevents/{month_slug}.htm"
            if (parsed_final.scheme.lower() != "https"
                    or parsed_final.hostname != "www.federalreserve.gov"
                    or parsed_final.path.lower() != expected_final_path.lower()):
                errors.append(f"{requested_url} -> unzulässige finale URL: {final_url}")
                continue
            html = str(getattr(response, "text", "") or "")
            if not html.strip():
                errors.append(f"{requested_url} -> HTTP 200, aber leerer Antworttext")
                continue
            retrieved_at = datetime.now(timezone.utc).isoformat()
            responses.append((requested_url, final_url, html, status_code, retrieved_at))
        except Exception as exc:
            errors.append(f"{requested_url} -> {type(exc).__name__}: {exc}")

    def plain_text(fragment: str) -> str:
        fragment = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>", " ", fragment)
        fragment = re.sub(r"(?is)<[^>]+>", " ", fragment)
        return re.sub(r"\s+", " ", fragment).casefold()

    for requested_url, final_url, html, status_code, retrieved_at in responses:
        page_text = plain_text(html)
        blocks = re.findall(r"(?is)<tr\b[^>]*>.*?</tr\s*>", html)
        group_text = ""
        if not blocks:
            group_match = re.search(
                r"(?is)FOMC Meetings(.*?)(?=Beige Book|Statistical Releases|Speeches|Testimony|Other)",
                html,
            )
            if group_match:
                group_text = plain_text(group_match.group(1))
                blocks = [group_match.group(1)]
        meeting_date_context = any(
            re.search(r"\bfomc\s+meeting\b", plain_text(block))
            and re.search(r"\b2\s*:\s*00\s*p\.?m\.?\b", plain_text(block))
            and _fomc_calendar_date_match(plain_text(block), event_date, page_context=page_text)
            for block in blocks
        )
        page_month_year_ok = bool(
            re.search(rf"\b{re.escape(event_date.strftime('%B'))}\s+{event_date.year}\b", page_text, re.I)
        )
        matching = []
        for block in blocks:
            block_text = plain_text(block)
            has_fomc_meeting = bool(re.search(r"\bfomc\s+meeting\b", block_text))
            has_press_conference = bool(re.search(r"\bfomc\s+press\s+conference\b|\bpress\s+conference\b", block_text))
            block_date_matches = _fomc_calendar_date_match(
                block_text, event_date, page_context=page_text
            )
            if not block_date_matches:
                # A separate press row may contain only the day number. It is
                # accepted only when the same page has a matching FOMC meeting
                # row whose explicit range ends on this date and the page title
                # binds that day to the requested month/year.
                separate_press_day_matches = bool(
                    event_kind == "PRESS_CONFERENCE" and has_press_conference
                    and meeting_date_context and page_month_year_ok
                    and _contains_calendar_day_number(block_text, event_date.day)
                )
                group_day_matches = bool(
                    group_text
                    and _fomc_calendar_date_match(group_text, event_date, page_context=page_text)
                    and (event_kind != "PRESS_CONFERENCE"
                         or _contains_calendar_day_number(block_text, event_date.day))
                )
                if not (separate_press_day_matches or group_day_matches):
                    continue
            if event_kind == "DECISION":
                if has_fomc_meeting and re.search(r"\b2\s*:\s*00\s*p\.?m\.?\b", block_text):
                    matching.append(block_text)
                elif group_text and _fomc_calendar_date_match(group_text, event_date, page_context=page_text) and re.search(
                    r"\bfomc\s+meeting\b", group_text
                ) and re.search(r"\b2\s*:\s*00\s*p\.?m\.?\b", group_text):
                    matching.append(group_text)
            else:
                if has_press_conference and re.search(r"\b2\s*:\s*30\s*p\.?m\.?\b", block_text):
                    matching.append(block_text)
                elif group_text and _fomc_calendar_date_match(group_text, event_date, page_context=page_text):
                    press_segment = re.search(
                        r"(?is)(?:press\s+conference|fomc\s+press\s+conference)(.{0,180})",
                        group_text,
                    )
                    if press_segment and re.search(r"\b2\s*:\s*30\s*p\.?m\.?\b", press_segment.group(0)):
                        if _contains_calendar_day_number(press_segment.group(0), event_date.day):
                            matching.append(press_segment.group(0))
        # Deduplicate the same logical row if both table and group parsing saw it.
        matching = list(dict.fromkeys(matching))
        if len(matching) == 1:
            response_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
            detail = (
                f"HTTP 200; {expected_name}; Datum={event_date.isoformat()}; Zeit={expected_time} "
                f"America/New_York; offizieller Fed-Kalenderbereich; Final={final_url}; Abruf={requested_url}"
            )
            if errors:
                detail += "; vorherige Abrufversuche fehlgeschlagen: " + " | ".join(errors)
            evidence = _EventVerificationEvidence(
                kind="FOMC", event_id=expected_id, event_name=expected_name,
                event_date=event_date.isoformat(), event_time=expected_time,
                timezone="America/New_York", requested_url=requested_url, final_url=final_url,
                http_status=status_code, retrieved_at=retrieved_at, content_match=True,
                adapter="federal_reserve_month_calendar_verifier", response_sha256=response_hash,
                verified=True, detail=detail,
            )
            return _register_verified_evidence(evidence)
        errors.append(
            f"{requested_url} -> {len(matching)} passende {expected_name}-Einträge für {event_date.isoformat()}"
        )

    diagnostic = " | ".join(errors) if errors else "keine offizielle Kalenderantwort erhalten"
    return _EventVerificationEvidence(
        kind="FOMC", event_id=expected_id, event_name=expected_name,
        event_date=event_date.isoformat(), event_time=expected_time, timezone="America/New_York",
        requested_url=urls[-1], final_url="", http_status=0,
        retrieved_at=datetime.now(timezone.utc).isoformat(), content_match=False,
        adapter="federal_reserve_month_calendar_verifier", response_sha256="",
        verified=False, detail=f"Fed-Kalenderprüfung fehlgeschlagen; {diagnostic}",
    )


def _enrich_fomc_event(
    body: str,
    report_text: str,
    calendar_path: str | Path = "fomc_termine.json",
    *,
    source_requester=None,
    verification_results: dict | None = None,
) -> str:
    """Enrich FOMC record, marking it verified only after a live official-source check."""
    event_kind = _fomc_event_kind(body)
    if event_kind not in {"DECISION", "PRESS_CONFERENCE"}:
        return body
    try:
        calendar_file = Path(calendar_path)
        if not calendar_file.is_absolute():
            calendar_file = Path(__file__).resolve().parent / calendar_file
        payload = json.loads(calendar_file.read_text(encoding="utf-8-sig"))
        dates = sorted(date.fromisoformat(str(value)) for value in payload.get("termine", []))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return body + "\nQuellenprüfung: NICHT VERIFIZIERT (Projektkalender fehlt oder ist ungültig)\n"
    cutoff_match = re.search(
        r"(?i)(?:Datenstand|Cutoff)[^\n]*?(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?)",
        report_text,
    )
    try:
        cutoff_value = cutoff_match.group(1) if cutoff_match else ""
        if "T" in cutoff_value:
            from datetime import time as datetime_time, timezone
            from zoneinfo import ZoneInfo
            cutoff_dt = datetime.fromisoformat(cutoff_value.replace("Z", "+00:00"))
            if cutoff_dt.tzinfo is None:
                cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
            cutoff_local = cutoff_dt.astimezone(ZoneInfo("America/New_York"))
            cutoff = cutoff_local.date()
            # Each event has its own scheduled release time. The decision
            # is at 14:00 ET; the press conference is at 14:30 ET.
            scheduled_time = (
                datetime_time(14, 30)
                if event_kind == "PRESS_CONFERENCE"
                else datetime_time(14, 0)
            )
            if cutoff_local.time().replace(tzinfo=None) >= scheduled_time:
                cutoff = date.fromordinal(cutoff.toordinal() + 1)
        else:
            cutoff = date.fromisoformat(cutoff_value) if cutoff_value else date.today()
    except (ValueError, OverflowError):
        cutoff = date.today()
    event_date = next((value for value in dates if value >= cutoff), None)
    if event_date is None:
        return body + "\nQuellenprüfung: NICHT VERIFIZIERT (kein passender zukünftiger Termin im Projektkalender)\n"
    verification = _verify_fomc_official_source(
        event_date, requester=source_requester, event_kind=event_kind
    )
    verified, verification_detail = verification.verified, verification.detail
    press_verification = None
    if event_kind == "DECISION":
        press_verification = _verify_fomc_official_source(
            event_date, requester=source_requester, event_kind="PRESS_CONFERENCE"
        )
    if verification_results is not None:
        verification_results[(event_date.isoformat(), event_kind)] = verification
        if press_verification is not None:
            verification_results[(event_date.isoformat(), "PRESS_CONFERENCE")] = press_verification
    month_url = f"https://www.federalreserve.gov/newsevents/{event_date:%Y-%B}.htm?view=calendar".replace(
        event_date.strftime("%B"), event_date.strftime("%B").lower()
    )
    clean = []
    for line in body.splitlines():
        if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:Ereignis-Uhrzeit|Uhrzeit|Zeit|Zeitzone|Datum|Quelle|Kennzahl|Quellenprüfung)\s*[:=]", re.sub(r"\*\*", "", line)):
            continue
        if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:Ereignis(?:\s*\d*)?)\s*[:=]", re.sub(r"\*\*", "", line)):
            continue
        clean.append(line)
    values = "\n".join(clean).strip()
    if event_kind == "PRESS_CONFERENCE":
        title = "FOMC Pressekonferenz"
        event_time = "14:30"
        identity = "FOMC_PRESS_CONFERENCE"
        metric = "FOMC Pressekonferenz / Erläuterung des Zinsentscheids"
    else:
        title = "FOMC-Zinsentscheid"
        event_time = "14:00"
        identity = "FOMC_DECISION"
        metric = "FOMC-Zinsentscheid / Zielkorridor Fed Funds Rate"
    fields = [
        f"Event-ID: {identity}",
        f"Datum: {event_date.isoformat()}",
        f"Uhrzeit: {event_time}",
        "Zeitzone: America/New_York (EDT/EST gemäß Ereignisdatum)",
        f"Kennzahl: {metric}",
        f"Quelle: Federal Reserve, offizieller Kalender ({month_url})",
        f"Quellenprüfung: {'LIVE_VERIFIZIERT' if verified else 'NICHT VERIFIZIERT'} — {verification_detail}",
    ]
    decision_or_press_record = f"- Ereignis: {title}\n" + "\n".join(fields) + "\n" + values + "\n"
    if event_kind == "DECISION" and press_verification is not None and press_verification.verified:
        press_fields = [
            "Event-ID: FOMC_PRESS_CONFERENCE",
            f"Datum: {event_date.isoformat()}",
            "Uhrzeit: 14:30",
            "Zeitzone: America/New_York (EDT/EST gemäß Ereignisdatum)",
            "Kennzahl: FOMC Pressekonferenz / Erläuterung des Zinsentscheids",
            f"Quelle: Federal Reserve, offizieller Kalender ({month_url})",
            f"Quellenprüfung: LIVE_VERIFIZIERT — {press_verification.detail}",
        ]
        decision_or_press_record += "\n- Ereignis: FOMC Pressekonferenz\n" + "\n".join(press_fields) + "\n"
    return decision_or_press_record



def normalize_event_section(report_text: str, *, forexfactory_requester=None, fomc_requester=None) -> tuple[str, list[str]]:
    """Canonicalize event records in report section 3 and remove duplicates.

    This is intentionally conservative: it does not invent values or infer dates.
    If the section is not parseable, it is left intact and a finding is returned.
    """
    import re
    headings = list(re.finditer(r"(?im)^\s*(?:#{1,6}\s*)?3\s*[.·]\s*Ereignisradar\s*$", report_text))
    if len(headings) != 1:
        return report_text, [f"Ereignisradar-Abschnitt nicht eindeutig auffindbar (Anzahl={len(headings)})"]
    start = headings[0].end()
    next_heading = re.search(r"(?im)^\s*(?:#{1,6}\s*)?4\s*[.·]\s*Assetklassenvergleich\s*$", report_text[start:])
    if not next_heading:
        return report_text, ["Ereignisradar-Ende nicht eindeutig auffindbar"]
    end = start + next_heading.start()
    body = report_text[start:end]
    no_events = bool(re.search(
        r"(?im)^\s*[-*•]?\s*keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?ereignisse[^\r\n]*$",
        body,
    ))
    if no_events:
        # A no-events statement must not be followed by orphaned event-field
        # placeholders; they otherwise get misread as a malformed event record.
        nonempty = [line.strip() for line in body.splitlines() if line.strip()]
        allowed = re.compile(
            r"(?i)^(?:[-*•]\s*)?(?:Uhrzeit/Zeitzone|Uhrzeit|Zeit|Zeitzone|Kennzahl|Indikator|"
            r"Berichtszeitraum|Zeitraum|Vorwert|Letzter Wert|Konsens|Istwert|Quelle)\s*[:=]\s*"
            r"(?:NICHT VERFÜGBAR(?:\s*\([^)]*\))?|[-–—]|TBD|TBA|N/?A|NA|UNBEKANNT|FEHLT|UNKLAR).*$"
        )
        if all(re.search(
            r"(?i)^[-*•]?\s*keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?ereignisse",
            line,
        ) or allowed.fullmatch(line) or line in {"---"} for line in nonempty):
            clean_body = "\n\n* Keine relevanten Ereignisse im geprüften Zeitraum.\n\n"
            return report_text[:start] + clean_body + report_text[end:], []
    if re.fullmatch(r"(?is)\s*(?:keine\s+(?:relevanten\s+)?(?:anstehenden\s+)?ereignisse[^\n]*\s*)?", body):
        return report_text, []
    # Enrich only an undated FOMC mention. If the report already contains an
    # explicit event date, preserve that record and its verification state:
    # enrichment must not silently replace a dated, unverified event with the
    # next calendar entry and thereby erase the original unknowns.
    fomc_has_explicit_date = bool(
        re.search(r"(?is)\bFOMC\b.{0,1200}?^\s*(?:[-*•]\s*)?(?:Datum|Date)\s*[:=]\s*\d{4}-\d{2}-\d{2}", body, re.MULTILINE)
    )
    fomc_verification_results = {}
    if not fomc_has_explicit_date:
        body = _enrich_fomc_event(
            body, report_text, source_requester=fomc_requester,
            verification_results=fomc_verification_results,
        )
    # A record starts at a bullet or numbered line that is not a field label.
    lines = body.splitlines()
    # Markdown labels (including "- **Quelle:**") are continuation fields,
    # not new event records. Strip formatting before classifying each line.
    field_labels = (
        "Ereignis-Uhrzeit|Ereignis Uhrzeit|Datum|Date|Uhrzeit/Zeitzone|Uhrzeit|Zeit|Zeitzone|"
        "Kennzahl|Indikator|Berichtszeitraum|Zeitraum|Vorwert|Letzter Wert|"
        "Konsens|Istwert|Quelle|Source|Betroffene These/Assetklasse/konkrete offene Positionen|"
        "Szenarien(?:\\s+(?:unter/entsprechend/über|unter|entsprechend|über|ueber)\\s+Konsens)?|"
        "Betroffene Thesen/Assetklassen/Positionen|"
        "Reaktionskanäle|Wirkung|Bestätigung/Widerlegung|Bestaetigung/Widerlegung|"
        "Was die These bestätigen/widerlegen würde|Quellenprüfung|Quellenpruefung|Status"
    )
    field = re.compile(rf"(?i)^\s*(?:{field_labels})\s*[:=]")
    def is_field_line(line: str) -> bool:
        normalized = re.sub(r"^\s*[-*•]\s*", "", line).strip()
        normalized = normalized.replace("**", "").replace("__", "")
        return bool(field.match(normalized))
    def fomc_event_kind(value: str) -> str | None:
        """Use the canonical identity classifier; ambiguous labels never merge."""
        return _fomc_event_kind(value)

    starts = []
    has_named_event_bullets = any(
        re.match(r"(?i)^\s*(?:[-*•]\s+|\d+[.)]\s+)(?:Ereignis|Termin|Event)\s*[:=]", line)
        for line in lines
    )
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.+)$", line)
        is_event_header = bool(m and re.match(r"(?i)^Ereignis[- ]Uhrzeit\s*[:=]", m.group(1).strip()))
        # Legacy report layout: if no explicit Ereignis-/Termin-/Event header
        # exists, each dated bullet begins one record; following labels remain
        # fields of that record (including the combined Uhrzeit/Zeitzone field).
        is_date_record = bool(
            m and not has_named_event_bullets
            and re.match(r"(?i)^Datum\s*[:=]", m.group(1).strip())
        )
        is_same_fomc_event = False
        if m and starts:
            current_text = "\n".join(lines[starts[-1]:i])
            current_date = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", current_text)
            summary_date = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", m.group(1))
            current_kind = fomc_event_kind(current_text)
            summary_kind = fomc_event_kind(m.group(1))
            is_same_fomc_event = bool(
                current_date and summary_date
                and current_date.group(0) == summary_date.group(0)
                and current_kind is not None
                and current_kind == summary_kind
            )
        if m and (is_event_header or is_date_record or not is_field_line(line)) and not is_same_fomc_event:
            starts.append(i)
    if not starts:
        # Single non-empty paragraph is treated as one event only if explicitly named.
        if not re.search(r"(?im)^\s*(?:Ereignis|Termin|Event)\s*[:=]", body):
            return report_text, ["Ereignisradar enthält keine eindeutig abgrenzbaren Ereignisdatensätze"]
        starts = [i for i, line in enumerate(lines) if re.match(r"(?i)^\s*(?:Ereignis|Termin|Event)\s*[:=]", line)]
    records = []
    if starts:
        if starts[0] > 0 and any(x.strip() for x in lines[:starts[0]]):
            intro = lines[:starts[0]]
        else:
            intro = []
        for n, idx in enumerate(starts):
            stop = starts[n+1] if n+1 < len(starts) else len(lines)
            records.append(lines[idx:stop])
    else:
        intro, records = [], []
    seen = set(); unique = []; findings = []
    for rec_lines in records:
        rec = "\n".join(rec_lines).strip()
        name_m = re.search(r"(?im)^\s*(?:[-*•]\s*)?(?:Ereignis|Termin|Event|Kennzahl|Indikator)?\s*[:=]?\s*(.+?)\s*$", rec_lines[0])
        name = (name_m.group(1) if name_m else rec_lines[0]).strip().casefold()
        # Strip markdown emphasis before extracting labels. Both **Quelle:** and
        # **Quelle**: are common generator outputs and mean the same field.
        plain_rec = re.sub(r"\*\*([^*]+)\*\*", r"\1", rec)
        if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:Datum|Date)\s*[:=]", rec_lines[0]):
            metric_identity = re.search(
                r"(?im)^\s*(?:[-*•]\s*)?(?:Kennzahl|Indikator)\s*[:=]\s*([^\r\n|;]+)",
                plain_rec,
            )
            if metric_identity:
                name = metric_identity.group(1).strip().casefold()
        date_m = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?(?:Datum|Date)\s*[:=]\s*([^\r\n|;]+)",
            plain_rec,
        )
        if not date_m:
            # A date inside the structured event-time field is explicit event
            # data. Dates embedded only in a narrative event title are not.
            event_time_line = re.search(
                r"(?im)^\s*(?:[-*•]\s*)?Ereignis-Uhrzeit\s*[:=]\s*([^\r\n]+)",
                plain_rec,
            )
            event_time_date = (
                re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", event_time_line.group(1))
                if event_time_line else None
            )
            if event_time_date:
                date_m = event_time_date
        period_m = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?(?:Zeitraum|Berichtszeitraum)\s*[:=]\s*([^\r\n|;]+)",
            plain_rec,
        )
        period_date = _explicit_date_from_period(period_m.group(1)) if period_m else None
        date_placeholder = bool(date_m and re.match(
            r"(?i)^\s*(?:NICHT VERFÜGBAR|NICHT VERFUEGBAR|NICHT BEKANNT|UNBEKANNT|FEHLT|UNKLAR|TBD|TBA|N/?A|NA)(?:\b|\s*\()",
            date_m.group(1).strip(),
        ))
        date_inferred_from_period = False
        if period_date and (date_m is None or date_placeholder):
            date_inferred_from_period = True
            datum_line = re.compile(
                r"(?im)^(\s*(?:[-*•]\s*)?(?:\*\*)?(?:Datum|Date)(?:\*\*)?\s*[:=]\s*)"
                r"(?:NICHT VERFÜGBAR(?:\s*\([^\r\n]*?\))?|NICHT VERFUEGBAR(?:\s*\([^\r\n]*?\))?|"
                r"NICHT BEKANNT|UNBEKANNT|FEHLT|UNKLAR|TBD|TBA|N/?A|NA)\s*$"
            )
            if datum_line.search(rec):
                rec = datum_line.sub(lambda match: match.group(1) + period_date, rec, count=1)
            else:
                rec = rec.rstrip() + f"\n- Datum: {period_date}"
            plain_rec = re.sub(r"\*\*([^*]+)\*\*", r"\1", rec)
            date_m = re.search(
                r"(?im)^\s*(?:[-*•]\s*)?(?:Datum|Date)\s*[:=]\s*([^\r\n|;]+)",
                plain_rec,
            )
        source_m = re.search(
            r"(?im)(?:^|[|;\n])\s*(?:[-*•]\s*)?(?:Quelle|Source)\s*[:=]\s*([^\r\n|;]+)",
            plain_rec,
        )
        key = (name, (date_m.group(1).strip().casefold() if date_m else ""), (source_m.group(1).strip().casefold() if source_m else ""))
        if key in seen:
            continue
        seen.add(key)
        # Remove only duplicate unavailable placeholders. Never splice from the
        # first placeholder to the last: valid values/scenarios between them are
        # meaningful event evidence and must survive normalization.
        rec_lines = rec.splitlines()
        for label in ("Uhrzeit", "Zeitzone", "Kennzahl", "Berichtszeitraum", "Zeitraum", "Letzter Wert", "Konsens"):
            label_pattern = re.compile(
                rf"(?i)^\s*(?:[-*•]\s*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*[:=]\s*(.*)$"
            )
            indexed = [(i, label_pattern.match(line)) for i, line in enumerate(rec_lines)]
            indexed = [(i, m) for i, m in indexed if m]
            if not indexed:
                continue
            placeholder_indices = []
            valid_indices = []
            for i, match in indexed:
                value = match.group(1).strip().strip("* ")
                if re.fullmatch(
                    r"(?i)(?:NICHT VERFÜGBAR(?:\s*\([^)]*\))?|[-–—]|TBD|TBA|N/?A|NA|UNBEKANNT|FEHLT|UNKLAR)",
                    value,
                ):
                    placeholder_indices.append(i)
                else:
                    valid_indices.append(i)
            remove = set(placeholder_indices if valid_indices else placeholder_indices[1:])
            if remove:
                rec_lines = [line for i, line in enumerate(rec_lines) if i not in remove]
        rec = "\n".join(rec_lines)
        source_value = source_m.group(1).strip() if source_m else ""
        verification_m = re.search(
            r"(?im)^\s*(?:[-*•]\s*)?(?:Quellenprüfung|Quellenpruefung)\s*[:=]\s*([^\r\n]+)",
            plain_rec,
        )
        verification_value = verification_m.group(1).strip() if verification_m else ""
        if not source_value and len(records) == 1:
            # In a single-event section, tolerate a source line retained in
            # the section body even if malformed markdown confused record slicing.
            fallback_source = re.search(
                r"(?im)^\s*(?:[-*•]\s*)?(?:Quelle|Source)\s*[:=]\s*([^\r\n|;]+)",
                re.sub(r"\*\*([^*]+)\*\*", r"\1", body),
            )
            source_value = fallback_source.group(1).strip() if fallback_source else ""
        source_is_placeholder = bool(re.fullmatch(
            r"(?i)(?:NICHT VERFÜGBAR(?:\s*\([^)]*\))?|[-–—]|TBD|TBA|N/?A|NA|UNBEKANNT|FEHLT|UNKLAR)",
            source_value,
        ))
        date_value = date_m.group(1).strip() if date_m else ""
        evidence = None
        event_fomc_kind = _fomc_event_kind(plain_rec)
        if event_fomc_kind and date_value and _is_valid_calendar_event_date(date_value):
            evidence = fomc_verification_results.get((date_value[:10], event_fomc_kind))
            if evidence is None:
                try:
                    evidence = _verify_fomc_official_source(
                        date.fromisoformat(date_value[:10]), requester=fomc_requester,
                        event_kind=event_fomc_kind,
                    )
                except ValueError:
                    evidence = None
            source_live_verified = _has_live_verification(
                verification_value, source_value, event_name=plain_rec,
                event_date=date_value, evidence=evidence,
            )
        elif re.search(r"(?i)\bFOMC\b", plain_rec):
            source_live_verified = False
        elif _is_forexfactory_source(source_value):
            evidence, adapter_detail = _verify_forexfactory_event(
                name, date_value, requester=forexfactory_requester
            ) if date_value and _is_valid_calendar_event_date(date_value) else (None, "Eventdatum fehlt")
            source_live_verified = _has_live_verification(
                verification_value, source_value, event_name=name,
                event_date=date_value, evidence=evidence,
            )
            if evidence is None:
                verification_value = "NICHT VERIFIZIERT (" + adapter_detail + ")"
        else:
            source_live_verified = _has_live_verification(
                verification_value, source_value, event_name=name, event_date=date_value
            )
        structured_fomc_verified = bool(
            evidence is not None and _is_trusted_event_evidence(
                evidence, kind="FOMC", event_name=plain_rec, event_date=date_value
            )
        )
        structured_forexfactory_verified = bool(
            evidence is not None and _is_trusted_event_evidence(
                evidence, kind="FOREXFACTORY", event_name=name, event_date=date_value
            )
        )
        if event_fomc_kind or re.search(r"(?i)\bFOMC\b", plain_rec):
            source_present = structured_fomc_verified
        elif _is_forexfactory_source(source_value):
            source_present = structured_forexfactory_verified
        else:
            legacy_fred_source = bool(
                re.search(r"(?i)\bfred\b", source_value)
                and not re.search(r"(?i)https://api\.stlouisfed\.org/fred/series/observations", source_value)
            )
            source_present = bool(
                not source_is_placeholder
                and not re.search(r"(?i)\b(?:CACHE|CACHED|STALE|VERALTET)\b", source_value)
                and (legacy_fred_source or (
                    source_live_verified if verification_value else _legacy_event_source_evidence(source_value)
                ))
            )
        if evidence is not None and evidence.verified:
            canonical_source = (
                f"Federal Reserve official calendar ({evidence.final_url})"
                if evidence.kind == "FOMC"
                else f"ForexFactory LIVE adapter ({evidence.final_url})"
            )
            source_line_pattern = re.compile(r"(?im)^\s*(?:[-*•]\s*)?(?:Quelle|Source)\s*[:=].*$")
            if source_line_pattern.search(rec):
                rec = source_line_pattern.sub("- Quelle: " + canonical_source, rec, count=1)
            else:
                rec = rec.rstrip() + "\n- Quelle: " + canonical_source
            verification_line_pattern = re.compile(r"(?im)^\s*(?:[-*•]\s*)?(?:Quellenprüfung|Quellenpruefung)\s*[:=].*$")
            verified_detail = "LIVE_VERIFIZIERT — " + evidence.detail
            if verification_line_pattern.search(rec):
                rec = verification_line_pattern.sub("- Quellenprüfung: " + verified_detail, rec)
            else:
                rec = rec.rstrip() + "\n- Quellenprüfung: " + verified_detail
        if date_inferred_from_period and date_m and _is_valid_calendar_event_date(date_m.group(1).strip()):
            rec = re.sub(
                r"(?im)^(\s*(?:[-*•]\s*)?(?:\*\*)?(?:Quellenprüfung|Quellenpruefung)(?:\*\*)?\s*[:=]\s*)"
                r"NICHT VERIFIZIERT\s*\(\s*Datum nicht belegt\s*\)\s*$",
                lambda match: match.group(1) + "DATUM AUS ZEITRAUM ÜBERNOMMEN (Quelle nicht separat live verifiziert)",
                rec,
            )
        date_is_placeholder = bool(re.match(
            r"(?i)^(?:NICHT VERFÜGBAR|NICHT VERFUEGBAR|NICHT BEKANNT|UNBEKANNT|FEHLT|UNKLAR|TBD|TBA|N/?A|NA)(?:\b|\s*\()",
            date_value,
        ))
        date_present = bool(date_value) and not date_is_placeholder and _is_valid_calendar_event_date(date_value)
        if not source_present:
            findings.append("Ereignis ohne verifizierte Quelle")
        if not date_present:
            findings.append("Ereignis ohne Datum")
        if not date_present and not date_m:
            # Make the missing date explicit in the report rather than leaving
            # the reader to infer it from a finding or from the period text.
            rec = rec.rstrip() + "\n- Datum: NICHT VERFÜGBAR"
        if not source_present or not date_present:
            reason = []
            if not date_present:
                reason.append("Datum nicht belegt")
            if not source_present:
                reason.append("Quelle fehlt, ist nicht vertrauenswürdig oder nicht live verifiziert")
            status_pattern = re.compile(
                r"(?im)^(\s*(?:[-*•]\s*)?(?:\*\*(?:Quellenprüfung|Quellenpruefung):\*\*|\*\*(?:Quellenprüfung|Quellenpruefung)\*\*|(?:Quellenprüfung|Quellenpruefung))\s*[:=]?\s*)[^\r\n]*$"
            )
            downgraded_status = "NICHT VERIFIZIERT (" + "; ".join(reason) + ")"
            if status_pattern.search(rec):
                # Normalize every duplicate status field. A later LIVE_VERIFIZIERT
                # marker must not contradict the authoritative downgrade.
                rec = status_pattern.sub(
                    lambda match: match.group(1) + downgraded_status,
                    rec,
                )
            else:
                rec = rec.rstrip() + "\n- Quellenprüfung: " + downgraded_status
        # Re-evaluate the normalized record, not the pre-normalization copy;
        # an earlier LIVE_VERIFIZIERT marker may have been downgraded above.
        plain_rec = re.sub(r"\*\*([^*]+)\*\*", r"\1", rec)
        if _fomc_event_kind(plain_rec) and not re.search(
            r"(?im)^\s*(?:[-*•]\s*)?Quellenprüfung\s*[:=]\s*[^\r\n]*\bLIVE_VERIFIZIERT\b",
            plain_rec,
        ):
            findings.append("FOMC-Ereignis nicht live gegen den offiziellen Fed-Kalender verifiziert")
        unique.append(rec)
    rebuilt = "\n".join(intro + ([""] if intro and unique else []) + [r for r in unique])
    if not unique and body.strip():
        findings.append("Ereignisradar konnte nicht sicher normalisiert werden")
        return report_text, findings
    return report_text[:start] + "\n" + rebuilt.rstrip() + "\n\n" + report_text[end:], sorted(set(findings))



def downgrade_ohlcv_affected_candidates(report_text: str, audit: Mapping[str, Any]) -> str:
    """Mark only affected section-5 candidates non-tradable; keep unrelated report usable."""
    affected = {
        str(t).strip().upper()
        for key in ("missing_tickers", "stale_tickers", "recent_invalid_ohlcv_tickers")
        for t in (audit.get(key) or [])
    }
    if not affected:
        return report_text
    headings = list(re.finditer(r"(?im)^\s*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten\s*$", report_text))
    if len(headings) != 1:
        return report_text
    start = headings[0].end()
    next_heading = re.search(r"(?im)^\s*(?:#{1,6}\s*)?6\s*[.·]\s*Portfolio\s*&\s*Risiko\s*$", report_text[start:])
    if not next_heading:
        return report_text
    end = start + next_heading.start()
    section = report_text[start:end]
    # Candidate blocks begin at a bullet containing Asset/Ticker; field lines remain in the block.
    starts = list(re.finditer(r"(?im)^\s*[-*•]\s+.*Asset/Ticker\s*[:：]", section))
    if not starts:
        starts = list(re.finditer(r"(?im)^\s*[-*•]\s+.*(?:Asset/Ticker|Ticker|Asset)\s*[:：=]", section))
    if not starts:
        return report_text
    chunks = []
    cursor = 0
    for i, match in enumerate(starts):
        chunks.append(section[cursor:match.start()])
        block_end = starts[i+1].start() if i+1 < len(starts) else len(section)
        block = section[match.start():block_end]
        ticker_values = set(re.findall(r"\(([A-Z0-9.^=!-]+(?:\.[A-Z0-9]+)?)\)", block, re.I))
        ticker_values.update(re.findall(r"(?i)(?:Ticker|Asset/Ticker)\s*[:：=]\s*([A-Z0-9.^=!-]+)", block))
        matched_affected = sorted(t.upper() for t in ticker_values if t.upper() in affected)
        plain_block = re.sub(r"\*\*([^*]+):\*\*", r"\1:", block)
        existing_nontradable = re.search(
            r"(?i)(?:Regel[-/ ]*Datenstatus|Regelstatus|Datenstatus)\s*[:：=]\s*NICHT HANDELBAR",
            plain_block,
        )
        if matched_affected and not existing_nontradable:
            status_pattern = re.compile(
                r"(?im)^\s*(?:[-*•]\s*)?(?:Regel[-/ ]*Datenstatus|Regelstatus|Datenstatus)\s*[:：=].*$"
            )
            note = "OHLCV für " + ", ".join(matched_affected) + " nicht aktuell/vollständig; Setup bis zur Datenprüfung nicht handelbar"
            if status_pattern.search(plain_block):
                lines = block.splitlines()
                for line_index, line in enumerate(lines):
                    plain_line = re.sub(r"\*\*([^*]+):\*\*", r"\1:", line)
                    if re.match(r"(?i)^\s*(?:[-*•]\s*)?(?:Regel[-/ ]*Datenstatus|Regelstatus|Datenstatus)\s*[:：=]", plain_line):
                        prefix = re.split(r"[:：=]", plain_line, maxsplit=1)[0].strip().lstrip("-*• ").strip()
                        lines[line_index] = "  - " + prefix + ": NICHT HANDELBAR"
                        break
                block = "\n".join(lines)
            else:
                block = block.rstrip() + "\n  - Regel-/Datenstatus: NICHT HANDELBAR\n"
            if "OHLCV-Datenhinweis:" not in block:
                block = block.rstrip() + "\n  - OHLCV-Datenhinweis: " + note + "\n"
        chunks.append(block)
        cursor = block_end
    chunks.append(section[cursor:])
    new_section = "".join(chunks)
    return report_text[:start] + new_section + report_text[end:]



def validate_ohlcv_audit(audit: Mapping[str, Any], *, explicit_no_candidates: bool = False) -> list[str]:
    """Validate audit integrity; universe-wide gaps are disclosed, not an automatic report veto.

    Candidate-specific consequences belong to the candidate's own tradeability status.
    """
    findings = []
    universe, covered = audit.get("universe_count"), audit.get("covered_count")
    if not isinstance(universe, int) or isinstance(universe, bool) or not isinstance(covered, int) or isinstance(covered, bool):
        return ["OHLCV-Prüfung ohne gültige Universums-/Abdeckungszahlen"]
    if universe < 0 or covered < 0 or covered > universe:
        return ["Widersprüchliche OHLCV-Abdeckungszahlen"]
    status = str(audit.get("status", "unavailable")).casefold()
    if status == "unavailable":
        return [str(audit.get("error") or "OHLCV-Datenbank oder Audit nicht verfügbar")]
    if status == "complete" and covered != universe:
        findings.append("OHLCV-Status complete widerspricht den aktuellen Abdeckungszahlen")
        findings.append(f"OHLCV-Abdeckung nicht vollständig: {covered}/{universe} Symbole abgedeckt")
        return findings
    if covered < universe or status in {"gaps", "partial", "incomplete"}:
        # Universe-Lücken dürfen nur dann rein dokumentarisch bleiben, wenn
        # das Audit eine tatsächlich nichtleere, verwertbare Kandidatenbewertung
        # mitliefert. Ein vorhandener Schlüssel mit []/None/falschem Typ ist
        # kein Nachweis, dass Kandidatenfolgen geprüft wurden.
        affected_candidates = audit.get("affected_candidates")
        has_affected_rows = (
            isinstance(affected_candidates, (list, tuple, set))
            and any(
                (isinstance(item, str) and item.strip())
                or (
                    isinstance(item, Mapping)
                    and any(
                        isinstance(item.get(key), str) and item.get(key).strip()
                        for key in ("ticker", "symbol", "asset", "candidate")
                    )
                )
                for item in affected_candidates
            )
        )
        # A complete candidate mapping with an explicitly empty affected list is
        # a valid assessment: the named candidates were checked and none overlaps
        # current missing/stale/recent-invalid OHLCV symbols. Universe-wide gaps
        # remain disclosed; they must not block unrelated, fully mapped candidates.
        checked = audit.get("candidate_tickers_checked")
        unmapped = audit.get("unmapped_candidate_tickers")
        has_complete_zero_impact_assessment = (
            audit.get("candidate_mapping_complete") is True
            and isinstance(checked, (list, tuple, set))
            and any(isinstance(item, str) and item.strip() for item in checked)
            and isinstance(unmapped, (list, tuple, set))
            and not any(isinstance(item, str) and item.strip() for item in unmapped)
            and isinstance(affected_candidates, (list, tuple, set))
            and len(affected_candidates) == 0
        )
        has_candidate_assessment = has_affected_rows or has_complete_zero_impact_assessment
        if not has_candidate_assessment and not explicit_no_candidates:
            if covered < universe:
                findings.append(
                    f"OHLCV-Symbolabdeckung nicht vollständig: {covered}/{universe} Symbole abgedeckt"
                )
            else:
                # Symbol coverage and candle quality are separate dimensions:
                # all symbols may exist while stale/invalid/gapped history still
                # requires disclosure. Never call 862/862 "not fully covered".
                findings.append(
                    f"OHLCV-Datenqualitätsstatus '{status}' trotz Symbolabdeckung "
                    f"{covered}/{universe}; Datenlücken/Veraltung/ungültige Kerzen separat prüfen"
                )
            findings.append(
                "OHLCV-Kandidatenfolgen fehlen: affected_candidates ist leer/ungültig "
                "und es liegt kein vollständiger Kandidaten-Mapping-Nachweis vor"
            )
    # Kandidatenbezogene Auswirkungen werden zusätzlich einzeln markiert; eine
    # globale Datenlücke darf nicht dazu führen, dass betroffene Kandidaten
    # stillschweigend als handelbar erscheinen.
    return findings


def report_explicitly_declares_no_technical_candidates(report_text: str) -> bool:
    """Detect a plain-language zero-candidate declaration without guessing from an empty parse."""
    if not isinstance(report_text, str):
        return False
    section = re.search(
        r"(?ims)^[ \t]*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten[^\r\n]*\r?\n"
        r"(.*?)(?=^[ \t]*(?:#{1,6}\s*)?6(?:\s*[.·](?!\d)|\s+)|\Z)", report_text
    )
    if not section:
        return False
    body = section.group(1)
    explicit = re.search(
        r"(?i)keine belastbaren technischen kandidaten|keine technischen kandidaten|"
        r"keine kandidaten|kein kandidat|keine handelsfreigabe", body
    )
    structured_candidate = re.search(
        r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?(?:Asset/Ticker|Ticker|Asset)\s*[:=]", body
    )
    return bool(explicit and not structured_candidate)


def report_has_explicit_empty_candidate_groups(report_text: str) -> bool:
    """Return True only when all four required candidate groups explicitly declare zero records.

    This is deliberately stricter than a plain-language no-candidate sentence:
    the report must contain section 5, all four required groups exactly once,
    each group must explicitly be empty, and no candidate record may appear.
    """
    if not isinstance(report_text, str):
        return False
    section_match = re.search(
        r"(?ims)^[ \t]*(?:#{1,6}\s*)?5\s*[.·]\s*Technische Kandidaten[^\r\n]*\r?\n"
        r"(.*?)(?=^[ \t]*(?:#{1,6}\s*)?6(?:\s*[.·](?!\d)|\s+)|\Z)",
        report_text,
    )
    if not section_match:
        return False
    body = section_match.group(1)
    required_groups = (
        "JETZT TECHNISCH PRÜFEN",
        "AUF TRIGGER WARTEN",
        "EREIGNIS ABWARTEN",
        "NICHT HANDELBAR",
    )
    group_pattern = re.compile(
        r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:[-*•][ \t]*)?(?:\*\*)?("
        + "|".join(re.escape(group) for group in sorted(required_groups, key=len, reverse=True))
        + r")(?:\*\*)?[ \t]*[:：][ \t]*([^\r\n]*)$"
    )
    matches = list(group_pattern.finditer(body))
    if len(matches) != len(required_groups):
        return False
    by_group = {}
    for match in matches:
        group = re.sub(r"\s+", " ", match.group(1)).strip().upper()
        by_group.setdefault(group, []).append(match)
    if set(by_group) != set(required_groups) or any(len(v) != 1 for v in by_group.values()):
        return False
    if re.search(
        r"(?im)^[ \t]*(?:[-*•][ \t]*)?(?:\*\*)?(?:Asset/Ticker|Ticker|Asset)(?:\*\*)?[ \t]*[:=]",
        body,
    ):
        return False
    empty_markers = re.compile(
        r"(?i)^(?:keine kandidaten(?: in dieser gruppe)?|0 kandidaten|explizit leer|leer)[ \t.!—-]*$"
    )
    for match in matches:
        group_body_start = match.end()
        later = [m.start() for m in matches if m.start() > match.start()]
        group_body_end = min(later) if later else len(body)
        group_content = body[group_body_start:group_body_end].strip()
        # The inline heading marker is the preferred canonical form. For a
        # multiline section, accept a single explicit empty marker and nothing else.
        if not empty_markers.fullmatch(match.group(2).strip()):
            if not empty_markers.fullmatch(group_content):
                return False
    return True
