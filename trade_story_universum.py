from __future__ import annotations

"""Deterministic daily Trade-Story candidate universe.

This module only aggregates already-produced rule-based outputs. It does not
score, rank, invent candidates, or apply new technical thresholds.
"""

import csv
import json
import os
import re
from datetime import date
from typing import Any


VALID_STATUS = "VALIDE SETUP"
PREPARED_STATUS = "VORBEREITET"
EXCLUDED_HEBEL = {"KAUFKANDIDAT C", "KEIN KANDIDAT", "NICHT AUSGELESEN"}
NAME_FIELDS = ("Name", "Firmenname", "name", "firmenname")
TICKER_FIELDS = ("Ticker", "ticker", "Yahoo-Ticker", "Yahoo_Ticker", "yahoo-ticker")


def _value(row: dict[str, Any], *names: str) -> str:
    lowered = {str(k).strip().casefold(): v for k, v in row.items()}
    for name in names:
        v = lowered.get(name.casefold())
        if v is not None and str(v).strip() and str(v).strip().lower() != "nan":
            return str(v).strip()
    return ""


def _ticker(value: str) -> str:
    return str(value or "").strip().upper()


def _read_csv(path: str) -> list[dict[str, str]]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        return [dict(r) for r in csv.DictReader(f, dialect=dialect)]


def _candidate_from_row(row: dict[str, Any], source: str, status: str,
                        direction: str = "Long", extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    tk = _ticker(_value(row, *TICKER_FIELDS))
    name = _value(row, *NAME_FIELDS)
    if not tk and not name:
        return None
    item = {
        "ticker": tk or None,
        "name": name or None,
        "direction": direction,
        "trade_story_status": status,
        "sources": [source],
    }
    for key in (
        "Sektor", "Markt", "Waehrung", "Setup_Typ", "Pattern", "Status_Grund",
        "CRV1", "CRV2", "Kurs", "Einstieg", "Stop", "TP1", "TP2",
        "Risk_Perc", "RS_vs_Benchmark%", "Vol_Ratio", "Trend",
    ):
        v = _value(row, key)
        if v:
            item[key] = v
    if extra:
        item.update(extra)
    return item


def _merge(target: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
    key = item.get("ticker") or str(item.get("name") or "").casefold()
    if not key:
        return
    if key not in target:
        target[key] = item
        return
    old = target[key]
    old_direction = str(old.get("direction") or "").strip()
    new_direction = str(item.get("direction") or "").strip()
    if old_direction and new_direction and old_direction != new_direction:
        old["direction"] = "CONFLICT"
        old["trade_story_status"] = "STATUSKONFLIKT"
        old["status_conflict"] = True
        old.setdefault("conflicting_directions", [])
        for direction in (old_direction, new_direction):
            if direction not in old["conflicting_directions"]:
                old["conflicting_directions"].append(direction)
    for field in ("name", "ticker"):
        if not old.get(field) and item.get(field):
            old[field] = item[field]
    old.setdefault("sources", [])
    for source in item.get("sources", []):
        if source not in old["sources"]:
            old["sources"].append(source)
    # Confirmed always dominates prepared. A later source may also add details.
    if (old.get("trade_story_status") != "STATUSKONFLIKT" and
            old.get("trade_story_status") != VALID_STATUS and
            item.get("trade_story_status") == VALID_STATUS):
        old["trade_story_status"] = VALID_STATUS
    for k, v in item.items():
        if k in {"sources", "trade_story_status"}:
            continue
        if v not in (None, "") and k not in old:
            old[k] = v


def _normal_setup_rows(paths: dict[str, str]) -> list[dict[str, Any]]:
    raw = _read_csv(paths.get("Trade_Story_Setup_Rohuniversum(...).csv", ""))
    final = _read_csv(paths.get("Setups(...).csv", ""))
    us_perf = _read_csv(paths.get("Performance(...).csv", ""))
    eu_perf = _read_csv(paths.get("Performance_EU(...).csv", ""))
    def top_sectors(rows, limit):
        ranked = []
        for r in rows:
            sector = _value(r, "Sektor")
            try:
                score = float(_value(r, "Rotation-Score").replace(",", "."))
            except (TypeError, ValueError):
                continue
            if sector:
                ranked.append((score, sector))
        return {sector for _, sector in sorted(ranked, reverse=True)[:limit]}
    top_us = top_sectors(us_perf, 8)
    top_eu = top_sectors(eu_perf, 5)
    # Standalone Gemini runs may not have the new pre-filter export yet.
    # In that case use the final setup file as a conservative compatibility
    # fallback; the full no-top-sector-loss behavior requires the raw export.
    if not raw:
        raw = final
    final_by_ticker = {
        _ticker(_value(r, *TICKER_FIELDS)): r for r in final if _ticker(_value(r, *TICKER_FIELDS))
    }
    out = []
    for row in raw:
        status = _value(row, "Status2").upper()
        if status not in {"VALIDE", "ACHTUNG"}:
            continue
        tk = _ticker(_value(row, *TICKER_FIELDS))
        effective = final_by_ticker.get(tk, {})
        final_status = _value(effective, "Status2").upper()
        if final_status in {"VALIDE", "ACHTUNG"}:
            status = final_status
        item = _candidate_from_row(
            row, "Normales Setup",
            VALID_STATUS if status == "VALIDE" else PREPARED_STATUS,
            "Long",
            {"normal_setup_status": status},
        )
        if item:
            if effective:
                item["final_setup_status"] = final_status
                reason = _value(effective, "Status_Grund")
                if reason:
                    item["Status_Grund"] = reason
            markt = str(item.get("Markt") or "").strip().upper()
            sektor = str(item.get("Sektor") or "").strip()
            item["top_sector"] = (
                sektor in (top_us if markt == "US" else top_eu)
                if sektor else False
            )
            out.append(item)
    return out


def build_trade_story_universe(paths: dict[str, str], observation_path: str | None = None) -> dict[str, Any]:
    candidates: dict[str, dict[str, Any]] = {}

    # Normal setup: raw pre-presentation-filter universe, with final Setups.csv
    # status used when that ticker survived the existing presentation pipeline.
    for item in _normal_setup_rows(paths):
        _merge(candidates, item)

    # Trendwende: every emitted data row is a scanner-confirmed setup.
    for row in _read_csv(paths.get("Trendwende_Setups(...).csv", "")):
        if any(str(v or "").strip() for v in row.values()):
            item = _candidate_from_row(row, "Trendwende", VALID_STATUS, _value(row, "Richtung") or "Long")
            if item:
                _merge(candidates, item)

    # Short: only explicitly valid rows.
    for row in _read_csv(paths.get("Short_Setups(...).csv", "")):
        if _value(row, "Status2").upper() != "VALIDE":
            continue
        item = _candidate_from_row(row, "Short-Setup", VALID_STATUS, "Short")
        if item:
            _merge(candidates, item)

    # Edelmetals: VALID is confirmed; ACHTUNG is prepared.
    for row in _read_csv(paths.get("Edelmetalle_Setups(...).csv", "")):
        status = _value(row, "Status2").upper()
        if status not in {"VALIDE", "ACHTUNG"}:
            continue
        item = _candidate_from_row(
            row, "Edelmetalle-Setup",
            VALID_STATUS if status == "VALIDE" else PREPARED_STATUS,
            _value(row, "Richtung") or "Long",
            {"edelmetalle_status": status},
        )
        if item:
            _merge(candidates, item)

    # Name-Master fuer zeitverschobene HEBELTRADER-Status:
    # Die Beobachtungsliste enthaelt bewusst nur den Status. Die technische
    # Historie besitzt dagegen den zuletzt berechneten Namen je Ticker. Ohne
    # diesen Fallback wuerden B-Kandidaten wie BP.L/RVTY/TMO im Universum mit
    # name=null landen und spaeter als unvollstaendige Trade-Story erscheinen.
    hebel_name_by_ticker: dict[str, str] = {}
    history_path = paths.get("Einzel-Check-Technikhistorie", "")
    if history_path and os.path.isfile(history_path):
        try:
            with open(history_path, encoding="utf-8-sig") as f:
                for raw_line in f:
                    try:
                        row = json.loads(raw_line)
                    except (TypeError, ValueError):
                        continue
                    tk = _ticker(row.get("Ticker"))
                    name = str(row.get("Name") or "").strip()
                    if tk and name:
                        hebel_name_by_ticker[tk] = name
        except Exception as exc:
            print(f"WARNUNG: HEBELTRADER-Technikhistorie fuer Namensauflösung unlesbar: {exc}")

    # HEBELTRADER status is time-shifted by design:
    # the Einzel-Check/Beobachtungsliste is updated around 10:00 MESZ and is
    # therefore consumed by the next main run. Do NOT require letzter_check
    # == today; use the latest available per-ticker observation with a date
    # <= today. This is the authoritative A/B/C/KEIN-KANDIDAT snapshot for
    # the current main run.
    current_hebel: dict[str, tuple[str, str, dict[str, Any]]] = {}
    priority = {"structured": 1, "a_message": 2, "observation": 3}

    def _record_hebel(ticker, status, source, name="", issue="", check_date=""):
        tk = _ticker(ticker)
        st = str(status or "").strip().upper()
        if not tk or st not in {"KAUFKANDIDAT A", "KAUFKANDIDAT B", "KAUFKANDIDAT C", "KEIN KANDIDAT", "NICHT AUSGELESEN"}:
            return
        resolved_name = str(name or "").strip() or hebel_name_by_ticker.get(tk, "")
        old = current_hebel.get(tk)
        if old is None or priority[source] >= priority[old[1]]:
            # Bei einer hoeher priorisierten Quelle niemals einen bereits
            # bekannten Namen durch null/leer ueberschreiben.
            previous_name = old[2].get("name", "") if old else ""
            current_hebel[tk] = (
                st,
                source,
                {
                    "name": resolved_name or previous_name,
                    "issue": str(issue or "").strip(),
                    "check_date": str(check_date or "").strip(),
                },
            )

    def _collect_rows(node):
        found = []
        if isinstance(node, dict):
            check = node.get("einzel_check") if isinstance(node.get("einzel_check"), dict) else {}
            status = str(check.get("status") or node.get("status") or "").strip().upper()
            tk = _ticker(str(node.get("ticker") or check.get("ticker") or ""))
            if tk and status in {"KAUFKANDIDAT A", "KAUFKANDIDAT B", "KAUFKANDIDAT C", "KEIN KANDIDAT", "NICHT AUSGELESEN"}:
                found.append((node, check, tk, status))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    found.extend(_collect_rows(value))
        elif isinstance(node, list):
            for value in node:
                found.extend(_collect_rows(value))
        return found

    def _parse_current_stdout(text):
        rx = re.compile(r"^\s*([A-Za-z0-9.\-^]+)\s+(KAUFKANDIDAT\s+[ABC]|KEIN KANDIDAT|NICHT AUSGELESEN)\b", re.IGNORECASE)
        for line in str(text or "").splitlines():
            match = rx.match(line)
            if match:
                _record_hebel(match.group(1), match.group(2), "structured")

    if hebel_path := paths.get("HEBELTRADER-Einzelcheck", ""):
        if os.path.isfile(hebel_path):
            try:
                with open(hebel_path, encoding="utf-8") as f:
                    data = json.load(f)
                for row, check, tk, status in _collect_rows(data):
                    _record_hebel(tk, status, "structured", row.get("name") or check.get("name"), row.get("quelle") or check.get("quelle"), row.get("letzter_check") or check.get("letzter_check"))
                if isinstance(data, dict):
                    _parse_current_stdout(data.get("einzel_check_stdout", ""))
            except Exception as exc:
                print(f"WARNUNG: HEBELTRADER-Einzelcheck fuer Trade-Story-Universum unlesbar: {exc}")

    # A-messages are dated artifacts from the 10:00 run. On the next main
    # run (and only then) they are valid input. Accept the newest artifact
    # whose embedded date is not in the future.
    a_path = paths.get("Einzel_Check_A_Meldungen(...).txt", "")
    if a_path and os.path.isfile(a_path):
        try:
            filename = os.path.basename(a_path)
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
            artifact_date = dm.group(1) if dm else ""
            run_date = date.today().isoformat()
            if artifact_date and artifact_date <= run_date:
                rx = re.compile(r"^\s*(?:Name:.*?\|\s*)?Ticker:\s*([A-Za-z0-9.\-^]+).*?KAUFKANDIDAT\s+A\b", re.IGNORECASE)
                with open(a_path, encoding="utf-8-sig") as f:
                    for line in f:
                        match = rx.search(line)
                        if match:
                            _record_hebel(match.group(1), "KAUFKANDIDAT A", "a_message", check_date=artifact_date)
        except Exception as exc:
            print(f"WARNUNG: HEBELTRADER-A-Meldungen unlesbar: {exc}")

    # The observation list is the authoritative time-shifted daily status.
    # Resolve the newest non-future status independently for every ticker.
    # This deliberately permits 16.09. data to feed the 17.09. 05:17 main run.
    if observation_path and os.path.isfile(observation_path):
        try:
            with open(observation_path, encoding="utf-8") as f:
                observation = json.load(f)
            run_date = date.today().isoformat()
            if isinstance(observation, dict):
                for tk_raw, row in observation.items():
                    if not isinstance(row, dict):
                        continue
                    check_date = str(row.get("letzter_check") or "").strip()
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", check_date) or check_date > run_date:
                        continue
                    status = str(row.get("status") or "").strip().upper()
                    last_candidate_date = str(row.get("last_candidate_date") or "").strip()
                    # Positive statuses need an actual candidate date no later
                    # than the check date. Negative statuses remain authoritative
                    # even when their last positive candidate is older.
                    if status in {"KAUFKANDIDAT A", "KAUFKANDIDAT B"}:
                        if not last_candidate_date or last_candidate_date > check_date:
                            continue
                    _record_hebel(
                        tk_raw, status, "observation",
                        row.get("name"), row.get("quelle"), check_date,
                    )
        except Exception as exc:
            print(f"WARNUNG: HEBELTRADER-Beobachtungsliste fuer Trade-Story-Universum unlesbar: {exc}")

    # Latest technical confirmation per ticker. HEBELTRADER-A alone is not
    # sufficient to label a story "VALIDE SETUP": the Einzel-Check must have
    # recorded a confirmed Trendfolge- oder Trendwende-Setup on the same
    # candidate date. This prevents A-status and technische ACHTUNG/absence
    # from being semantically mixed.
    hebel_valid_by_ticker_date: set[tuple[str, str]] = set()
    if history_path and os.path.isfile(history_path):
        try:
            with open(history_path, encoding="utf-8-sig") as f:
                for raw_line in f:
                    try:
                        row = json.loads(raw_line)
                    except (TypeError, ValueError):
                        continue
                    tk = _ticker(row.get("Ticker"))
                    status_date = str(row.get("Datum") or "").strip()
                    if not tk or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", status_date):
                        continue
                    if str(row.get("Status") or "").strip().upper() != "KAUFKANDIDAT A":
                        continue
                    tf = row.get("Trendfolge") if isinstance(row.get("Trendfolge"), dict) else {}
                    tw = row.get("Trendwende") if isinstance(row.get("Trendwende"), dict) else {}
                    if (str(tf.get("Status2") or "").upper() == "VALIDE" or
                            str(tw.get("Status2") or "").upper() == "VALIDE" or
                            str(row.get("Trendfolge_Status") or "").upper() == "VALIDE" or
                            str(row.get("Trendwende_Status") or "").upper() == "VALIDE"):
                        hebel_valid_by_ticker_date.add((tk, status_date))
        except Exception as exc:
            print(f"WARNUNG: HEBELTRADER-Technikhistorie fuer Setup-Bestaetigung unlesbar: {exc}")

    # A = candidate; A is VALID only when the same day's technical history
    # confirms a setup. B is prepared. C/KEIN KANDIDAT/NICHT AUSGELESEN = excluded.
    # No status is invented from older history.
    for tk, (status, source, meta) in current_hebel.items():
        if status in EXCLUDED_HEBEL:
            candidates.pop(tk, None)
            continue
        item = {
            "ticker": tk,
            "name": meta.get("name") or None,
            "direction": "Long",
            "trade_story_status": (
                VALID_STATUS
                if status == "KAUFKANDIDAT A" and (tk, str(meta.get("check_date") or "").strip()) in hebel_valid_by_ticker_date
                else PREPARED_STATUS
            ),
            "sources": ["Hebeltrader-Einzel-Check"],
            "hebeltrader_status": status,
        }
        if meta.get("issue"):
            item["hebeltrader_issue"] = meta["issue"]
        _merge(candidates, item)

    # Bitcoin is a distinct source, not a stock setup and not a normal CRV
    # setup. Only confirmed Long events enter VALID; prealerts remain prepared.
    btc_path = paths.get("Trade_Story_Bitcoin(...).json", "")
    if btc_path and os.path.isfile(btc_path):
        try:
            btc = json.load(open(btc_path, encoding="utf-8"))
            pi = btc.get("pi_cycle_bottom") or {}
            sma = btc.get("sma50w") or {}
            for source, result in (("Bitcoin Pi-Cycle Bottom", pi), ("Bitcoin 50W-SMA", sma)):
                signal_type = str(result.get("signal_type", "")).upper()
                if signal_type in {"BOTTOM_LONG", "CROSS_UP"}:
                    item = {
                        "ticker": "BTC-USD",
                        "name": "Bitcoin",
                        "direction": "Long",
                        "trade_story_status": VALID_STATUS,
                        "sources": [source],
                        "bitcoin_signal_type": signal_type,
                        "bitcoin_trade_action": result.get("trade_action") or ("LONG" if signal_type in {"BOTTOM_LONG", "CROSS_UP"} else ""),
                        "bitcoin_signal_date": str(result.get("date") or result.get("cross_date") or ""),
                    }
                    _merge(candidates, item)
                elif signal_type == "PREALERT":
                    item = {
                        "ticker": "BTC-USD",
                        "name": "Bitcoin",
                        "direction": "Long",
                        "trade_story_status": PREPARED_STATUS,
                        "sources": [source],
                        "bitcoin_signal_type": signal_type,
                    }
                    _merge(candidates, item)
        except Exception:
            pass

    # Portfolio is context only and never removes a candidate.
    portfolio_tickers = set()
    for row in _read_csv(paths.get("Offene Positionen+Check.csv", "")):
        status = _value(row, "Status").casefold()
        if status == "offen":
            tk = _ticker(_value(row, *TICKER_FIELDS))
            if tk:
                portfolio_tickers.add(tk)
    for item in candidates.values():
        if item.get("ticker") in portfolio_tickers:
            item["portfolio_status"] = "OFFENE POSITION"
            item["portfolio_context_only"] = True

    candidates_list = sorted(
        candidates.values(),
        key=lambda x: (0 if x.get("trade_story_status") == VALID_STATUS else 1,
                       str(x.get("ticker") or x.get("name") or ""))
    )
    return {
        "schema_version": 1,
        "generated_at": date.today().isoformat(),
        "principles": {
            "valid": VALID_STATUS,
            "prepared": PREPARED_STATUS,
            "hebeltrader_excluded": sorted(EXCLUDED_HEBEL),
            "portfolio_is_context": True,
            "top_sector_is_not_candidate_filter": True,
        },
        "candidates": candidates_list,
    }


def write_trade_story_universe(paths: dict[str, str], output_path: str,
                               observation_path: str | None = None) -> str:
    universe = build_trade_story_universe(paths, observation_path)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(universe, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, output_path)
    return output_path
