from __future__ import annotations

"""Deterministic daily Trade-Story candidate universe.

This module only aggregates already-produced rule-based outputs. It does not
score, rank, invent candidates, or apply new technical thresholds.
"""

import csv
import json
import os
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

    # Hebeltrader A/B comes from the current observation state. C/KEIN KANDIDAT
    # are deliberately excluded from the Trade-Story candidate universe.
    if observation_path and os.path.isfile(observation_path):
        try:
            data = json.load(open(observation_path, encoding="utf-8"))
            if isinstance(data, dict):
                for tk, row in data.items():
                    if not isinstance(row, dict):
                        continue
                    status = str(row.get("status", "")).strip().upper()
                    quelle = str(row.get("quelle", "")).strip().upper()
                    if status not in {"KAUFKANDIDAT A", "KAUFKANDIDAT B"}:
                        continue
                    # The Trade-Story source is strictly the HEbeltrader
                    # Einzel-Check. Manual/other observation-list entries
                    # (e.g. Quelle='-') are not eligible candidates here.
                    if not quelle or "HEBELTRADER" not in quelle:
                        continue
                    item = {
                        "ticker": _ticker(tk),
                        "name": str(row.get("name") or row.get("firmenname") or "").strip() or None,
                        "direction": "Long",
                        "trade_story_status": VALID_STATUS if status.endswith("A") else PREPARED_STATUS,
                        "sources": ["Hebeltrader-Einzel-Check"],
                        "hebeltrader_status": status,
                    }
                    for field in ("quelle", "momentum", "gruende", "risiken", "technischer_zustand"):
                        if row.get(field) not in (None, "", []):
                            item[field] = row[field]
                    _merge(candidates, item)
        except Exception:
            pass

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
