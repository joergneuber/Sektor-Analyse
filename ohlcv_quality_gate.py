"""Tolerantes, deterministisches OHLCV-Quality-Gate.

Das Modul verändert niemals die Quelldatenbank. Es maskiert ungültige
Beobachtungen im zurückgegebenen DataFrame und hält erwartete Sitzungen auf
der Zeitachse, damit nachgelagerte Berechnungen keine Lücken komprimieren.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

RULE_VERSION = "1.3"
_MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = Path(os.environ.get(
    "NMM_OHLCV_QUALITY_REGISTRY", str(_MODULE_DIR / "ohlcv_quality_registry.csv")
))
DEFAULT_GAP_REGISTRY = Path(os.environ.get(
    "NMM_OHLCV_GAP_REGISTRY", str(_MODULE_DIR / "ohlcv_quality_gap_registry.csv")
))
DEFAULT_WINDOW_SESSIONS = 252

# Eng begrenzter historischer Sonderfall: NYMEX WTI May-2020 (CLK20),
# abgebildet als Yahoo-Frontmonat CL=F, wurde in den Handelssitzungen
# 20./21.04.2020 real negativ gehandelt. Erlaubte Felder sind je Datum
# festgelegt; Nullpreise, andere Kontrakte und andere Daten bleiben gesperrt.
SPECIAL_CASE_RULES = {
    "CME_SETTLEMENT_CLOSE_OUTSIDE_RANGE_V1": {
        "tickers": {"CL=F", "GC=F", "SI=F", "HG=F", "PL=F", "PA=F"},
        "start": "2000-01-01",
        "end": "2099-12-31",
        "allowed_reason_codes": {
            "HIGH_BELOW_OPEN_OR_CLOSE",
            "LOW_ABOVE_OPEN_OR_CLOSE",
        },
    },
    "WTI_NEGATIVE_SETTLEMENT_2020_V2": {
        "tickers": {"CL=F"},
        "contract_code": "CLK20",
        "start": "2020-04-20",
        "end": "2020-04-21",
        "allowed_reason_codes": {
            "OPEN_NON_POSITIVE", "LOW_NON_POSITIVE", "CLOSE_NON_POSITIVE",
        },
        # Enger Feldvertrag: nur diese negativen OHLC-Felder sind durch die
        # historische Evidenz für die jeweilige Sitzung abgedeckt.
        "allowed_reason_codes_by_date": {
            "2020-04-20": {"LOW_NON_POSITIVE", "CLOSE_NON_POSITIVE"},
            "2020-04-21": {"OPEN_NON_POSITIVE", "LOW_NON_POSITIVE"},
        },
        # Negative fields are locked to the reviewed daily-bar observations.
        # This prevents a permissive date/code rule from accepting arbitrary
        # negative outliers for the same instrument and date.
        "expected_negative_values_by_date": {
            "2020-04-20": {"Low": -40.32, "Close": -37.63},
            "2020-04-21": {"Open": -14.00, "Low": -16.74},
        },
        "negative_value_abs_tolerance": 0.005,
    },
}
PRICE_COLUMNS = ("Open", "High", "Low", "Close")
MASK_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
EXCLUDED_CLASSES = {
    "CONFIRMED_INVALID", "UNRESOLVED", "UNRESOLVED_FX_SESSION",
    "GAP_UNRESOLVED", "GAP_UNRESOLVED_SESSION_OR_PROVIDER",
    "GAP_UNRESOLVED_CALENDAR_OR_PROVIDER", "MISSING",
}


@dataclass(frozen=True)
class QualityGateResult:
    ticker: str
    quality_status: str
    expected_bars: int
    valid_bars: int
    excluded_bars: int
    valid_ratio: float
    max_consecutive_exclusions: int
    latest_bar_valid: bool
    last_3_bars_valid: bool
    reason_codes: tuple[str, ...]
    timeframe: str = "1d"
    rule_version: str = RULE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def _date_index(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    idx = pd.to_datetime(out.index, errors="coerce")
    if not isinstance(idx, pd.DatetimeIndex):
        idx = pd.DatetimeIndex(idx)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    out = out.loc[~out.index.isna()].sort_index()
    return out.loc[~out.index.duplicated(keep="last")]


@lru_cache(maxsize=512)
def _calendar_sessions(ticker: str, start: str, end: str) -> list[str] | None:
    """Ermittelt erwartete Handelssitzungen über die vorhandene Kalenderlogik."""
    try:
        import historical_ohlcv as ho

        calendar = ho._calendar_name_for_ticker(ticker)
        if not calendar:
            return None
        if calendar == "24/5":
            sessions = pd.bdate_range(start=start, end=end)
            closed = set(ho.NON_TRADING_SESSIONS_BY_CALENDAR.get("24/5", ()))
            sessions = [d.date().isoformat() for d in sessions if d.date().isoformat() not in closed]
        elif calendar == "24/7":
            sessions = pd.date_range(start=start, end=end, freq="D").date
            sessions = [d.isoformat() for d in sessions]
        else:
            # exchange_calendars requires start < end. A one-session gate
            # still needs a valid single-date calendar lookup.
            calendar_start, calendar_end = start, end
            if calendar_start >= calendar_end:
                day = pd.Timestamp(start)
                calendar_start = (day - pd.Timedelta(days=1)).date().isoformat()
                calendar_end = (day + pd.Timedelta(days=1)).date().isoformat()
            sessions = ho._calendar_session_dates(calendar, calendar_start, calendar_end)
            if not sessions:
                return None
            sessions = [
                session for session in sessions if start <= str(session)[:10] <= end
            ]
            sessions = ho._expected_daily_bar_dates(calendar, sessions)
        return sorted({str(x)[:10] for x in sessions})
    except Exception:
        return None


def historical_frame_as_of(frame: pd.DataFrame, ticker: str | None = None):
    """Stichtag fuer absichtlich historische SQLite-Abfragen.

    Die erwartete Zeitachse endet an der letzten gelieferten Session statt
    kuenstlich bis zum heutigen Datum verlaengert zu werden. Liegt diese
    Session heute, begrenzt die reale aktuelle Uhrzeit weiterhin laufende
    Tageskerzen. 24/7-Serien werden erst nach dem UTC-Tageswechsel bestaetigt.
    """
    if frame is None or frame.empty:
        return None
    index = pd.to_datetime(frame.index, errors="coerce")
    index = index[~index.isna()]
    if len(index) == 0:
        return None
    latest_date = pd.Timestamp(index.max()).normalize()
    try:
        import historical_ohlcv as ho
        calendar_name = ho._calendar_name_for_ticker(str(ticker or "").upper())
    except Exception:
        calendar_name = None
    if calendar_name == "24/7":
        requested_end = latest_date.tz_localize("UTC") + pd.Timedelta(days=1, minutes=1)
    else:
        requested_end = latest_date.tz_localize("UTC") + pd.Timedelta(hours=23, minutes=59)
    now = pd.Timestamp.now(tz="UTC")
    return min(now, requested_end)


def _last_completed_session_date(ticker: str, as_of=None) -> str | None:
    """Liefert die letzte abgeschlossene Daily-Session (konservativer Cutoff).

    ``as_of`` ist fuer reproduzierbare Tests injizierbar. Naive Zeitstempel
    werden als UTC interpretiert. Exchange-Kalender liefern den tatsaechlichen
    Schlusszeitpunkt inkl. verkürzter Sessions. Fuer die projektspezifischen
    24/5-, 24/7- und IFEU-Kalender gelten explizite konservative Cutoffs.
    """
    try:
        import historical_ohlcv as ho

        now = pd.Timestamp.now(tz="UTC") if as_of is None else pd.Timestamp(as_of)
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        else:
            now = now.tz_convert("UTC")
        calendar_name = ho._calendar_name_for_ticker(str(ticker).upper())
        if not calendar_name:
            return None

        if calendar_name == "24/7":
            local_now = now.tz_convert("UTC")
            cutoff = local_now.normalize() + pd.Timedelta(days=1)
            if local_now < cutoff:
                return (local_now.normalize() - pd.Timedelta(days=1)).date().isoformat()
            return local_now.date().isoformat()

        if calendar_name in {"24/5", "IFEU"}:
            zone = "America/New_York" if calendar_name == "24/5" else "Europe/London"
            # ICE Brent Futures (IFEU) trades until 23:00 London time.
            # This conservative full-session cutoff avoids treating the
            # earlier 19:28 settlement window as the completed daily bar.
            # Official specification: https://www.ice.com/products/219/WTI-Light-Sweet-Crude-Oil-Futures
            close_time = dt.time(17, 0) if calendar_name == "24/5" else dt.time(23, 0)
            local_now = now.tz_convert(zone)
            start = (local_now.normalize() - pd.Timedelta(days=14)).date().isoformat()
            end = local_now.date().isoformat()
            sessions = _calendar_sessions(str(ticker).upper(), start, end) or []
            today = local_now.date().isoformat()
            include_today = local_now.time().replace(tzinfo=None) >= close_time
            eligible = [day for day in sessions if day < today or (day == today and include_today)]
            return eligible[-1] if eligible else None

        import exchange_calendars as exchange_cal
        calendar = exchange_cal.get_calendar(calendar_name)
        # exchange_calendars session labels are timezone-naive dates.
        range_start = pd.Timestamp((now - pd.Timedelta(days=14)).date())
        range_end = pd.Timestamp(now.date())
        all_sessions = calendar.sessions
        sessions = all_sessions[(all_sessions >= range_start) & (all_sessions <= range_end)]
        for session in reversed(sessions):
            close = pd.Timestamp(calendar.session_close(session))
            if close.tzinfo is None:
                close = close.tz_localize("UTC")
            else:
                close = close.tz_convert("UTC")
            if close <= now:
                return pd.Timestamp(session).date().isoformat()
        return None
    except Exception:
        return None


@lru_cache(maxsize=16)
def _load_registry(path: Path) -> dict[tuple[str, str], dict]:
    records: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = str(row.get("ticker", "")).strip().upper()
            date = str(row.get("date", "")).strip()[:10]
            if ticker and date:
                records[(ticker, date)] = row
    return records


@lru_cache(maxsize=16)
def _load_gap_registry(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Liest Gap-Klassifikationen getrennt von fehlerhaften OHLC-Kerzen."""
    records: dict[tuple[str, str], dict[str, str]] = {}
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = str(row.get("ticker", "")).strip().upper()
            date = str(row.get("missing_date", "")).strip()[:10]
            if ticker and date:
                records[(ticker, date)] = {
                    str(key): str(value or "").strip() for key, value in row.items()
                }
    return records


def _special_case_rule_details(version: str) -> dict | None:
    rule = SPECIAL_CASE_RULES.get(str(version).strip())
    if not rule:
        return None
    return {
        key: sorted(value) if isinstance(value, set) else value
        for key, value in rule.items()
    }


def _approved_special_case_codes(
    ticker: str, date_key: str, record: dict | None
) -> set[str]:
    """Nur explizit versionierte, instrument-/datumsgebundene Sonderregeln."""
    if not record:
        return set()
    version = str(record.get("rule_version", "")).strip()
    rule = SPECIAL_CASE_RULES.get(version)
    if not (
        rule
        and str(ticker).upper() in rule["tickers"]
        and rule["start"] <= str(date_key)[:10] <= rule["end"]
        and str(record.get("classification", "")).strip().upper() == "SPECIAL_CASE"
        and str(record.get("usage_decision", "")).strip().upper() in {"ALLOW_WITH_RULE", "ALLOW"}
        and str(record.get("approved_by", "")).strip()
        and str(record.get("evidence_ref", "")).strip()
        and str(record.get("timeframe", "1d") or "1d").strip().lower() == "1d"
    ):
        return set()
    allowed_codes = {
        code.strip().upper()
        for code in str(record.get("allowed_reason_codes", "")).replace(";", ",").split(",")
        if code.strip()
    }
    date_specific = rule.get("allowed_reason_codes_by_date")
    if date_specific is not None:
        required_codes = date_specific.get(str(date_key)[:10], set())
        # Bei WTI muessen die im Register freigegebenen Felder exakt dem
        # datumsbezogenen Evidenzumfang entsprechen; keine Erweiterung per CSV.
        return allowed_codes if required_codes and allowed_codes == required_codes else set()
    return (
        allowed_codes
        if allowed_codes and allowed_codes.issubset(rule["allowed_reason_codes"])
        else set()
    )


def _approved_negative_reason_codes(
    ticker: str, date_key: str, record: dict | None
) -> set[str]:
    """Negativpreise nur unter der eigenen WTI-Instrument-/Datumsregel."""
    if not record or str(record.get("rule_version", "")).strip() != "WTI_NEGATIVE_SETTLEMENT_2020_V2":
        return set()
    return _approved_special_case_codes(ticker, date_key, record)


def _negative_value_is_evidenced(
    ticker: str, date_key: str, column: str, value: float,
    allowed_negative_codes: set[str],
) -> bool:
    """Prueft negative Werte gegen den fest hinterlegten Evidenzwert je Datum/Feld."""
    rule = SPECIAL_CASE_RULES.get("WTI_NEGATIVE_SETTLEMENT_2020_V2", {})
    if str(ticker).upper() not in rule.get("tickers", set()):
        return False
    expected = rule.get("expected_negative_values_by_date", {}).get(str(date_key)[:10], {})
    reason_code = f"{str(column).upper()}_NON_POSITIVE"
    if reason_code not in allowed_negative_codes or column not in expected:
        return False
    try:
        expected_value = float(expected[column])
        tolerance = float(rule.get("negative_value_abs_tolerance", 0.005))
    except (TypeError, ValueError):
        return False
    return expected_value < 0 and math.isclose(
        float(value), expected_value, rel_tol=0.0, abs_tol=tolerance
    )


def _structural_reasons(
    row: pd.Series, has_volume: bool, allowed_negative_codes: set[str] | None = None,
    ticker: str | None = None, date_key: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    values: dict[str, float] = {}
    allowed_negative_codes = {str(code).upper() for code in (allowed_negative_codes or set())}
    for col in PRICE_COLUMNS:
        if col not in row.index:
            reasons.append(f"{col.upper()}_MISSING_COLUMN")
            continue
        try:
            value = float(row[col])
        except (TypeError, ValueError):
            reasons.append(f"{col.upper()}_NON_NUMERIC")
            continue
        if not math.isfinite(value):
            reasons.append(f"{col.upper()}_NON_FINITE")
            continue
        reason_code = f"{col.upper()}_NON_POSITIVE"
        if value <= 0:
            if value < 0 and _negative_value_is_evidenced(
                ticker or "", date_key or "", col, value, allowed_negative_codes
            ):
                values[col] = value
                continue
            if value < 0 and reason_code in allowed_negative_codes:
                reasons.append(f"{col.upper()}_NEGATIVE_VALUE_MISMATCH")
            else:
                reasons.append(reason_code)
            continue
        values[col] = value
    if len(values) == 4:
        if values["High"] < max(values["Open"], values["Close"]):
            reasons.append("HIGH_BELOW_OPEN_OR_CLOSE")
        if values["Low"] > min(values["Open"], values["Close"]):
            reasons.append("LOW_ABOVE_OPEN_OR_CLOSE")
        if values["High"] < values["Low"]:
            reasons.append("HIGH_BELOW_LOW")
    if has_volume:
        try:
            volume = float(row.get("Volume"))
        except (TypeError, ValueError):
            reasons.append("VOLUME_NON_NUMERIC")
        else:
            if not math.isfinite(volume):
                reasons.append("VOLUME_NON_FINITE")
            elif volume < 0:
                reasons.append("VOLUME_NEGATIVE")
    return sorted(set(reasons))


def apply_ohlcv_quality_gate(
    frame: pd.DataFrame,
    ticker: str,
    *,
    registry_path: Path | str = DEFAULT_REGISTRY,
    gap_registry_path: Path | str | None = None,
    window_sessions: int = DEFAULT_WINDOW_SESSIONS,
    require_structure: bool = False,
    preserve_expected_sessions: bool = True,
    full_history: bool = False,
    as_of=None,
    timeframe: str = "1d",
) -> tuple[pd.DataFrame, QualityGateResult]:
    """Prüft OHLCV-Daten gegen Kalender, Registry und konservative Grenzen."""
    ticker_key = str(ticker or "").strip().upper()
    timeframe_key = str(timeframe or "").strip().lower()
    out = _date_index(frame if isinstance(frame, pd.DataFrame) else pd.DataFrame())
    if timeframe_key != "1d":
        result = QualityGateResult(
            ticker_key, "INCOMPLETE", 0, 0, 0, 0.0, 0, False, False,
            ("UNSUPPORTED_TIMEFRAME",), timeframe=timeframe_key,
        )
        out.attrs["ohlcv_quality"] = result.to_dict()
        out.attrs["ohlcv_quality_ticker"] = ticker_key
        out.attrs["ohlcv_quality_timeframe"] = timeframe_key
        return out.iloc[0:0], result
    if out.empty:
        result = QualityGateResult(ticker_key, "INCOMPLETE", 0, 0, 0, 0.0, 0, False, False,
                                   ("NO_DATA",))
        out.attrs["ohlcv_quality"] = result.to_dict()
        return out, result

    registry_file = Path(registry_path)
    gap_registry_file = Path(gap_registry_path) if gap_registry_path is not None else Path(DEFAULT_GAP_REGISTRY)
    missing_files = []
    if not registry_file.is_file():
        missing_files.append("QUALITY_REGISTRY_MISSING")
    if not gap_registry_file.is_file():
        missing_files.append("GAP_REGISTRY_MISSING")

    # Fehlende Register sperren alle Beobachtungen, aber die Kalenderprüfung
    # läuft weiter. So bleibt auch im Fail-Closed-Fall die erwartete Zeitachse
    # erhalten und fehlende Sitzungen werden weiterhin explizit eingefügt.
    registry = _load_registry(registry_file) if registry_file.is_file() else {}
    gap_registry = _load_gap_registry(gap_registry_file) if gap_registry_file.is_file() else {}
    has_volume = "Volume" in out.columns
    reasons_by_date: dict[str, list[str]] = {}
    special_case_details: list[dict[str, object]] = []

    for date, row in out.iterrows():
        date_key = date.date().isoformat()
        record = registry.get((ticker_key, date_key))
        allowed_special_codes = _approved_special_case_codes(
            ticker_key, date_key, record
        )
        allowed_negative_codes = _approved_negative_reason_codes(
            ticker_key, date_key, record
        )
        reasons = _structural_reasons(
            row, has_volume, allowed_negative_codes, ticker_key, date_key
        )
        reasons.extend(missing_files)
        gap_record = gap_registry.get((ticker_key, date_key))
        if gap_record:
            gap_timeframe = str(gap_record.get("timeframe", "1d") or "1d").strip().lower()
            gap_class = str(gap_record.get("quality_class", "")).strip().upper()
            if gap_timeframe != "1d":
                reasons.append("REGISTRY_GAP_TIMEFRAME_MISMATCH")
            elif gap_class == "CLOSED_SESSION":
                reasons.append("UNEXPECTED_BAR_ON_CLOSED_SESSION")
            elif gap_class in {"MISSING", "GAP_UNRESOLVED"} or gap_class.startswith("GAP_UNRESOLVED_"):
                reasons.append("REGISTRY_GAP_UNRESOLVED")
            else:
                reasons.append("REGISTRY_GAP_CLASS_UNRECOGNIZED")
        if record:
            record_timeframe = str(record.get("timeframe", "1d") or "1d").strip().lower()
            if record_timeframe != "1d":
                reasons.append("REGISTRY_TIMEFRAME_MISMATCH")
            quality_class = str(record.get("classification", "")).strip().upper()
            if quality_class in EXCLUDED_CLASSES:
                reasons.append(f"REGISTRY_{quality_class}")
            elif quality_class == "SPECIAL_CASE":
                approval = str(record.get("approved_by", "")).strip()
                evidence = str(record.get("evidence_ref", "")).strip()
                version = str(record.get("rule_version", "")).strip()
                allowed_codes = allowed_special_codes
                if not allowed_codes:
                    reasons.append("SPECIAL_CASE_NOT_APPROVED_OR_EVIDENCED")
                else:
                    special_case_details.append({
                        "date": date_key,
                        "ticker": ticker_key,
                        "allowed_reason_codes": sorted(allowed_codes),
                        "approved_by": approval,
                        "evidence_ref": evidence,
                        "rule_version": version,
                        "instrument_rule": _special_case_rule_details(version),
                    })
                    # Preis-Nichtpositiv-Codes werden bereits feldbezogen in
                    # _structural_reasons behandelt. Sie hier pauschal zu
                    # entfernen waere falsch: insbesondere Null darf nie durch
                    # eine Negativpreis-Sonderregel freigegeben werden.
                    price_non_positive_codes = {
                        "OPEN_NON_POSITIVE", "HIGH_NON_POSITIVE",
                        "LOW_NON_POSITIVE", "CLOSE_NON_POSITIVE",
                    }
                    settlement_allowed_codes = allowed_codes - price_non_positive_codes
                    reasons = [reason for reason in reasons if reason not in settlement_allowed_codes]
        reasons_by_date[date_key] = sorted(set(reasons))
        if reasons:
            for column in MASK_COLUMNS:
                if column in out.columns:
                    out.loc[date, column] = float("nan")

    calendar = None
    try:
        import historical_ohlcv as ho
        calendar = ho._calendar_name_for_ticker(ticker_key)
    except Exception:
        pass
    if not calendar:
        result = QualityGateResult(
            ticker_key, "INCOMPLETE", 0, 0, len(reasons_by_date), 0.0, 0,
            False, False, tuple(sorted(set(missing_files) | {"CALENDAR_UNVERIFIED_OR_UNAVAILABLE"})),
        )
        out.attrs["ohlcv_quality"] = result.to_dict()
        out.attrs["ohlcv_quality_ticker"] = ticker_key
        out.attrs["ohlcv_quality_timeframe"] = "1d"
        out.attrs["ohlcv_quality_window_sessions"] = int(window_sessions)
        out.attrs["ohlcv_quality_exclusions"] = [
            {"date": date, "reason_codes": reasons} for date, reasons in sorted(reasons_by_date.items())
        ]
        return out, result

    # Noch laufende Tages-Sessions duerfen weder als gueltige letzte Kerze
    # noch als fehlende erwartete Session in den Nenner eingehen.
    completed_end = _last_completed_session_date(ticker_key, as_of=as_of)
    if completed_end is None:
        result = QualityGateResult(
            ticker_key, "INCOMPLETE", 0, 0, len(reasons_by_date), 0.0, 0,
            False, False, ("COMPLETED_SESSION_CUTOFF_UNAVAILABLE",),
        )
        out.attrs["ohlcv_quality"] = result.to_dict()
        out.attrs["ohlcv_quality_ticker"] = ticker_key
        out.attrs["ohlcv_quality_timeframe"] = "1d"
        out.attrs["ohlcv_quality_window_sessions"] = int(window_sessions)
        return out.iloc[0:0], result
    out = out.loc[out.index <= pd.Timestamp(completed_end)].copy()
    if out.empty:
        result = QualityGateResult(
            ticker_key, "INCOMPLETE", 0, 0, 0, 0.0, 0,
            False, False, ("NO_COMPLETED_SESSION_DATA",),
        )
        out.attrs["ohlcv_quality"] = result.to_dict()
        return out, result

    calendar_end = completed_end
    # Das Prüf-Fenster wird kalenderbasiert begrenzt. Bei begrenzten
    # Provider-Zeiträumen beginnt es frühestens mit der ersten gelieferten
    # Beobachtung; innerhalb dieser Abdeckung werden sämtliche erwarteten
    # Sitzungen geprüft und fehlende Zwischen-/End-Sitzungen eingefügt.
    calendar_floor = out.index.max() - pd.Timedelta(days=max(30, window_sessions * 3))
    calendar_start = (
        out.index.min().date().isoformat()
        if full_history else max(
            out.index.min().date().isoformat(), calendar_floor.date().isoformat()
        )
    )
    sessions = _calendar_sessions(ticker_key, calendar_start, calendar_end)
    if sessions is not None:
        documented_gaps = {
            date for (symbol, date), record in gap_registry.items()
            if symbol == ticker_key
            and calendar_start <= date <= calendar_end
            and str(record.get("timeframe", "1d") or "1d").strip().lower() == "1d"
            and str(record.get("quality_class", "")).strip().upper() != "CLOSED_SESSION"
        }
        closed_sessions = {
            date for (symbol, date), record in gap_registry.items()
            if symbol == ticker_key
            and str(record.get("timeframe", "1d") or "1d").strip().lower() == "1d"
            and str(record.get("quality_class", "")).strip().upper() == "CLOSED_SESSION"
        }
        sessions = sorted((set(sessions) - closed_sessions) | documented_gaps)
    if not sessions:
        result = QualityGateResult(
            ticker_key, "INCOMPLETE", 0, 0, len(reasons_by_date), 0.0, 0,
            False, False, tuple(sorted(set(missing_files) | {"CALENDAR_UNVERIFIED_OR_UNAVAILABLE"})),
        )
        out.attrs["ohlcv_quality"] = result.to_dict()
        return out, result

    requested_window = max(1, int(window_sessions))
    minimum_window_sessions = max(1, math.ceil(requested_window * 0.95))
    # Auch bei full_history wird die Qualitaet nur im aktuellen
    # Berechnungsfenster bewertet. Die vollstaendige erwartete Zeitachse wird
    # separat erhalten; alte historische Fehler sperren nicht pauschal den
    # gesamten Ticker fuer aktuelle Berechnungen.
    window = sessions[-requested_window:]
    expected_sessions_for_index = sessions if full_history else window
    expected_index = pd.DatetimeIndex(pd.to_datetime(expected_sessions_for_index))
    if preserve_expected_sessions:
        out = out.reindex(out.index.union(expected_index).sort_values())
    if len(sessions) < minimum_window_sessions:
        result = QualityGateResult(
            ticker_key, "INCOMPLETE", len(window), 0, len(window), 0.0,
            len(window), False, False, ("INSUFFICIENT_EXPECTED_SESSIONS",),
        )
        out.attrs["ohlcv_quality"] = result.to_dict()
        out.attrs["ohlcv_quality_ticker"] = ticker_key
        out.attrs["ohlcv_quality_timeframe"] = "1d"
        out.attrs["ohlcv_quality_window_sessions"] = requested_window
        return out, result
    expected_index = pd.DatetimeIndex(pd.to_datetime(window))
    excluded_flags: list[bool] = []
    valid_bars = 0
    reason_codes: set[str] = set()
    exclusion_details: list[dict[str, object]] = []

    for session, date_idx in zip(window, expected_index):
        if date_idx not in out.index:
            excluded_flags.append(True)
            reason_codes.add("MISSING_EXPECTED_SESSION")
            exclusion_details.append({"date": session, "reason_codes": ["MISSING_EXPECTED_SESSION"]})
            continue
        reasons = reasons_by_date.get(session)
        if reasons is None:
            reasons = ["MISSING_EXPECTED_SESSION"]
        if reasons:
            excluded_flags.append(True)
            reason_codes.update(reasons)
            exclusion_details.append({"date": session, "reason_codes": list(reasons)})
            for column in MASK_COLUMNS:
                if column in out.columns:
                    out.loc[date_idx, column] = float("nan")
        else:
            excluded_flags.append(False)
            valid_bars += 1

    expected_bars = len(window)
    excluded_bars = expected_bars - valid_bars
    valid_ratio = valid_bars / expected_bars if expected_bars else 0.0
    max_run = run = 0
    for excluded in excluded_flags:
        run = run + 1 if excluded else 0
        max_run = max(max_run, run)
    latest_valid = bool(excluded_flags and not excluded_flags[-1])
    last3_valid = bool(len(excluded_flags) >= 3 and not any(excluded_flags[-3:]))
    if valid_ratio < 0.95:
        reason_codes.add("VALID_RATIO_BELOW_95_PERCENT")
    if max_run > 1:
        reason_codes.add("CONSECUTIVE_EXCLUSIONS_ABOVE_1")
    if not latest_valid:
        reason_codes.add("LATEST_EXPECTED_BAR_INVALID")
    if require_structure and not last3_valid:
        reason_codes.add("LAST_3_EXPECTED_BARS_INVALID")

    incomplete = (
        valid_ratio < 0.95 or max_run > 1 or not latest_valid
        or (require_structure and not last3_valid)
    )
    status = "INCOMPLETE" if incomplete else ("PASS_WITH_EXCLUSIONS" if excluded_bars else "PASS")
    result = QualityGateResult(
        ticker_key, status, expected_bars, valid_bars, excluded_bars,
        round(valid_ratio, 6), max_run, latest_valid, last3_valid,
        tuple(sorted(reason_codes)),
    )
    out.attrs["ohlcv_quality"] = result.to_dict()
    out.attrs["ohlcv_quality_ticker"] = ticker_key
    out.attrs["ohlcv_quality_timeframe"] = "1d"
    out.attrs["ohlcv_quality_window_sessions"] = int(window_sessions)
    out.attrs["ohlcv_quality_exclusions"] = exclusion_details
    out.attrs["ohlcv_quality_special_cases"] = special_case_details
    return out, result


def require_valid_structure_window(frame: pd.DataFrame, ticker: str, **kwargs):
    return apply_ohlcv_quality_gate(frame, ticker, require_structure=True, **kwargs)


def calculate_valid_atr14(
    frame: pd.DataFrame,
    ticker: str | None = None,
    *,
    registry_path: Path | str = DEFAULT_REGISTRY,
) -> tuple[float | None, str]:
    """ATR14 aus 14 gueltigen TRs; negative Preise nur bei genehmigter Sonderregel."""
    if frame is None or frame.empty or not all(c in frame.columns for c in PRICE_COLUMNS):
        return None, "INCOMPLETE"
    data = _date_index(frame)
    if not ticker:
        # Ohne Instrument kann keine erwartete Session-Achse und damit kein
        # unmittelbar vorheriger Schlusskurs sicher bestimmt werden.
        return None, "INCOMPLETE"
    try:
        expected_sessions = _calendar_sessions(
            str(ticker).upper(),
            data.index.min().date().isoformat(),
            data.index.max().date().isoformat(),
        )
    except Exception:
        expected_sessions = None
    if not expected_sessions:
        return None, "INCOMPLETE"
    expected_index = pd.DatetimeIndex(pd.to_datetime(expected_sessions))
    data = data.reindex(expected_index)
    if len(data) < 15:
        return None, "INCOMPLETE"
    registry = _load_registry(Path(registry_path))
    recent = data.iloc[-21:]
    tr_values: list[float] = []
    for pos in range(1, len(recent)):
        current = recent.iloc[pos]
        previous = recent.iloc[pos - 1]
        current_date = pd.Timestamp(recent.index[pos]).date().isoformat()
        previous_date = pd.Timestamp(recent.index[pos - 1]).date().isoformat()
        try:
            high, low, prev_close = float(current["High"]), float(current["Low"]), float(previous["Close"])
            open_, close = float(current["Open"]), float(current["Close"])
        except (TypeError, ValueError):
            tr_values.append(float("nan"))
            continue
        current_allowed = _approved_negative_reason_codes(
            str(ticker or "").upper(), current_date,
            registry.get((str(ticker or "").upper(), current_date)),
        )
        previous_allowed = _approved_negative_reason_codes(
            str(ticker or "").upper(), previous_date,
            registry.get((str(ticker or "").upper(), previous_date)),
        )
        values = (
            (high, "HIGH_NON_POSITIVE", current_allowed),
            (low, "LOW_NON_POSITIVE", current_allowed),
            (open_, "OPEN_NON_POSITIVE", current_allowed),
            (close, "CLOSE_NON_POSITIVE", current_allowed),
            (prev_close, "CLOSE_NON_POSITIVE", previous_allowed),
        )
        negative_values_valid = all(
            value >= 0 or _negative_value_is_evidenced(
                str(ticker).upper(), value_date, column, value, allowed_codes
            )
            for value, reason_code, allowed_codes, column, value_date in (
                (high, "HIGH_NON_POSITIVE", current_allowed, "High", current_date),
                (low, "LOW_NON_POSITIVE", current_allowed, "Low", current_date),
                (open_, "OPEN_NON_POSITIVE", current_allowed, "Open", current_date),
                (close, "CLOSE_NON_POSITIVE", current_allowed, "Close", current_date),
                (prev_close, "CLOSE_NON_POSITIVE", previous_allowed, "Close", previous_date),
            )
        )
        if not all(math.isfinite(value) for value, _, _ in values) or not negative_values_valid or not all(
            value != 0 and (value > 0 or reason_code in allowed_codes)
            for value, reason_code, allowed_codes in values
        ):
            tr_values.append(float("nan"))
            continue
        if high < max(open_, close) or low > min(open_, close) or high < low:
            tr_values.append(float("nan"))
            continue
        tr_values.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    valid_tr = sum(math.isfinite(value) for value in tr_values)
    if valid_tr < 14 or not tr_values or not math.isfinite(tr_values[-1]):
        return None, "INCOMPLETE"
    atr_series = pd.Series(tr_values, dtype="float64").ewm(
        alpha=1 / 14, adjust=False, min_periods=14, ignore_na=False
    ).mean()
    atr = atr_series.iloc[-1]
    return (float(atr), "PASS") if pd.notna(atr) and atr > 0 else (None, "INCOMPLETE")


def quality_checked_frame(
    frame: pd.DataFrame,
    ticker: str,
    *,
    window_sessions: int = DEFAULT_WINDOW_SESSIONS,
    require_structure: bool = False,
    preserve_expected_sessions: bool = True,
    full_history: bool = False,
    as_of=None,
    timeframe: str = "1d",
) -> pd.DataFrame:
    gated, result = apply_ohlcv_quality_gate(
        frame, ticker, window_sessions=window_sessions,
        require_structure=require_structure,
        preserve_expected_sessions=preserve_expected_sessions,
        full_history=full_history,
        as_of=as_of,
        timeframe=timeframe,
    )
    if result.quality_status == "INCOMPLETE":
        blocked = pd.DataFrame()
        blocked.attrs["ohlcv_quality"] = result.to_dict()
        blocked.attrs["ohlcv_quality_ticker"] = str(ticker or "").strip().upper()
        blocked.attrs["ohlcv_quality_timeframe"] = str(timeframe or "1d").strip().lower()
        blocked.attrs["ohlcv_quality_window_sessions"] = int(window_sessions)
        return blocked
    return gated
