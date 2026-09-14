#!/usr/bin/env python3
"""
IEA MESGEN / MESBAL SDMX Discovery Test
---------------------------------------

Purpose:
  Exhaustively test the already-proven official IEA SDMX service for the
  Monthly Electricity Statistics dataflows MESGEN and MESBAL.

This is a DISCOVERY/DIAGNOSTIC script. It does not modify production code,
does not infer market direction, and does not write to the project cache.

The script deliberately tests many documented/observed SDMX REST URL shapes,
flowRef forms, providerRef forms, key wildcards, API generations and
content-negotiation headers. Responses are capped/streamed where possible.

Output:
  iea_mes_sdmx_discovery_report.json
  iea_mes_sdmx_responses/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests


HOSTS = [
    "https://sis-cc-nsi-stable.iea.org",
    "https://sis-cc-api-stable.iea.org",
]

FLOWS = {
    "MESGEN": ("OECD.IEA", "MESGEN", "1.1", 7),
    "MESBAL": ("OECD.IEA", "MESBAL", "1.1", 6),
}

# The IEA service advertises /rest/ and /rest/v1/... data endpoints.
API_PREFIXES = ["/rest", "/rest/v1"]

# Different SDMX implementations accept different flowRef spellings.
FLOWREF_FORMS = [
    lambda a, i, v: f"{a},{i},{v}",
    lambda a, i, v: f"{a},{i}",
    lambda a, i, v: f"{i}",
    lambda a, i, v: f"{i},{v}",
]

# providerRef is required by the documented endpoint shape. "all" is a
# standard discovery candidate; OECD.IEA is also tested because it is the
# agency proven by the structure response.
PROVIDER_FORMS = ["all", "OECD.IEA"]

# Key candidates. For MESGEN the DSD order is:
# COUNTRY.ENERGY_PRODUCT.PLANT.ENERGY_BALANCE_FLOW.FREQUENCY.CORRECTION.TIME_PERIOD
# For MESBAL it is:
# COUNTRY.ENERGY_PRODUCT.ENERGY_BALANCE_FLOW.FREQUENCY.CORRECTION.TIME_PERIOD
KEYS = {
    7: [
        "all",
        ".......",
        "......",
        ".......",
        "*.*.*.*.*.*.*",
        "........",
    ],
    6: [
        "all",
        "......",
        "*.*.*.*.*.*",
        ".......",
    ],
}

# Additional bounded keys. These are intentionally broad: a country wildcard
# plus wildcards for the remaining dimensions, and Germany as a known valid
# country candidate. The test only uses a short time window.
BOUNDED_KEYS = {
    7: [
        "DEU......",
        "DEU.*.*.*.*.*.*",
    ],
    6: [
        "DEU.....",
        "DEU.*.*.*.*.*",
    ],
}

ACCEPT_HEADERS = [
    ("csv", "text/csv"),
    ("sdmx_csv", "application/vnd.sdmx.data+csv;version=2.0.0"),
    ("sdmx_xml", "application/vnd.sdmx.genericdata+xml;version=2.1"),
    ("xml", "application/xml"),
    ("json", "application/json"),
]

TIME_PARAMS = [
    {},
    {"startPeriod": "2026-01", "endPeriod": "2026-05"},
    {"startPeriod": "2025-01", "endPeriod": "2026-05"},
]

DATA_ENDPOINTS = [
    "data",
    "data/1.0",
]

STRUCTURE_CHECKS = [
    "/rest/datastructure/OECD.IEA/MESGEN/1.1",
    "/rest/datastructure/OECD.IEA/MESBAL/1.1",
    "/rest/v1/datastructure/OECD.IEA/MESGEN/1.1",
    "/rest/v1/datastructure/OECD.IEA/MESBAL/1.1",
    "/rest/datastructure/OECD.IEA/MESGEN/latest",
    "/rest/datastructure/OECD.IEA/MESBAL/latest",
]


@dataclass
class Probe:
    category: str
    host: str
    method: str
    url: str
    status: int | None
    content_type: str
    elapsed_ms: int
    bytes: int
    success: bool
    data_signal: bool
    error_signal: bool
    notes: list[str]
    saved_body: str | None = None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(value: str, limit: int = 150) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value[:limit].strip("_") or "response"


def signal_text(text: str) -> tuple[bool, bool, list[str]]:
    low = text.lower()
    positive_terms = [
        "obs_value",
        "time_period",
        "mesgen",
        "mesbal",
        "energy_product",
        "energy_balance_flow",
        "country",
        "sdmx",
        "data",
    ]
    error_terms = [
        "unsupportedapiversion",
        "object reference not set",
        "could not find requested structures",
        "not found",
        "bad request",
        "internal server error",
    ]
    hits = [x for x in positive_terms if x in low]
    errors = [x for x in error_terms if x in low]
    return bool(hits), bool(errors), hits + [f"ERROR:{x}" for x in errors]


def request_probe(
    session: requests.Session,
    out_dir: Path,
    category: str,
    host: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 25,
    save_limit: int = 2_000_000,
) -> Probe:
    url = host.rstrip("/") + "/" + path.lstrip("/")
    if params:
        url += "?" + urlencode(params)

    t0 = time.perf_counter()
    try:
        r = session.get(
            url,
            headers=headers or {},
            timeout=timeout,
            allow_redirects=True,
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        raw = r.content
        content_type = r.headers.get("content-type", "")
        text = raw[:save_limit].decode("utf-8", errors="replace")
        data_signal, error_signal, notes = signal_text(text)

        saved = None
        if raw and (data_signal or len(raw) < save_limit):
            name = safe_name(
                f"{category}_{r.status_code}_{hashlib.sha1(url.encode()).hexdigest()[:12]}"
            )
            suffix = ".bin"
            if "json" in content_type or text.lstrip().startswith("{"):
                suffix = ".json"
            elif "csv" in content_type or "obs_value" in text.lower():
                suffix = ".csv"
            elif "xml" in content_type or text.lstrip().startswith("<"):
                suffix = ".xml"
            p = out_dir / f"{name}{suffix}"
            p.write_bytes(raw[:save_limit])
            saved = str(p.relative_to(out_dir.parent))

        success = r.status_code == 200 and data_signal and not error_signal
        return Probe(
            category=category,
            host=host,
            method="GET",
            url=url,
            status=r.status_code,
            content_type=content_type,
            elapsed_ms=elapsed,
            bytes=len(raw),
            success=success,
            data_signal=data_signal,
            error_signal=error_signal,
            notes=notes,
            saved_body=saved,
        )
    except requests.RequestException as exc:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return Probe(
            category=category,
            host=host,
            method="GET",
            url=url,
            status=None,
            content_type="",
            elapsed_ms=elapsed,
            bytes=0,
            success=False,
            data_signal=False,
            error_signal=True,
            notes=[f"REQUEST_ERROR:{type(exc).__name__}:{exc}"],
        )


def build_data_paths(flow_id: str, agency: str, version: str, dim_count: int):
    flowrefs = [f(agency, flow_id, version) for f in FLOWREF_FORMS]
    keys = KEYS[dim_count] + BOUNDED_KEYS[dim_count]
    for prefix in API_PREFIXES:
        for endpoint in DATA_ENDPOINTS:
            for flowref in flowrefs:
                for key in keys:
                    for provider in PROVIDER_FORMS:
                        yield (
                            f"{prefix}/{endpoint}/"
                            f"{quote(flowref, safe=',')}/"
                            f"{quote(key, safe='.*')}/"
                            f"{quote(provider, safe='')}"
                        )


def dedupe(seq):
    seen = set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            yield x


def main() -> int:
    parser = argparse.ArgumentParser(description="Broad IEA MESGEN/MESBAL SDMX discovery test")
    parser.add_argument("--output", default="iea_mes_sdmx_discovery", help="Output directory")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument(
        "--max-data-probes",
        type=int,
        default=160,
        help="Maximum HTTP data probes per flow across all hosts/variants (default: 160)",
    )
    args = parser.parse_args()

    root = Path(args.output)
    body_dir = root / "responses"
    body_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "NEUBER-MACRO-MES-SDMX-DISCOVERY/1.0",
            "Accept-Encoding": "gzip, deflate",
        }
    )

    probes: list[Probe] = []

    # 1) Re-prove the exact structure endpoints on both official hosts.
    for host in HOSTS:
        for path in STRUCTURE_CHECKS:
            probes.append(
                request_probe(
                    session,
                    body_dir,
                    "structure",
                    host,
                    path,
                    headers={"Accept": "application/xml"},
                    timeout=args.timeout,
                )
            )

    # 2) Probe data endpoints with a deliberately broad matrix.
    for flow_id, (agency, fid, version, dim_count) in FLOWS.items():
        candidates = list(dedupe(build_data_paths(fid, agency, version, dim_count)))
        # Bound the TOTAL number of HTTP data probes, not merely the number of
        # URL candidates. This keeps the matrix broad without accidentally
        # turning 160 candidates x 2 hosts x 4 variants into 1,280 requests.
        probe_budget = max(4, args.max_data_probes)
        probe_count = 0
        found = False

        for host in HOSTS:
            if probe_count >= probe_budget:
                break
            for path in candidates:
                if probe_count >= probe_budget:
                    break
                for params in TIME_PARAMS[:2]:
                    if probe_count >= probe_budget:
                        break
                    for label, accept in ACCEPT_HEADERS[:2]:
                        if probe_count >= probe_budget:
                            break
                        category = f"{flow_id}_data"
                        p = request_probe(
                            session,
                            body_dir,
                            category,
                            host,
                            path,
                            headers={"Accept": accept},
                            params=params,
                            timeout=args.timeout,
                        )
                        p.notes.insert(0, f"accept={label}")
                        p.notes.insert(1, f"params={params}")
                        probes.append(p)
                        probe_count += 1

                        # Stop immediately on a confirmed MES payload.
                        if p.success:
                            found = True
                            break
                    if found:
                        break
                if found:
                    break
            if found:
                break

    successes = [p for p in probes if p.success]
    data_hits = [p for p in probes if p.data_signal and not p.error_signal]
    errors = [p for p in probes if p.error_signal]

    report = {
        "tool": "iea_mes_sdmx_discovery",
        "purpose": "MESGEN/MESBAL official SDMX data-path discovery",
        "hosts": HOSTS,
        "flows": {
            k: {
                "agency": v[0],
                "id": v[1],
                "version": v[2],
                "dimension_count": v[3],
            }
            for k, v in FLOWS.items()
        },
        "probe_count": len(probes),
        "success_count": len(successes),
        "data_signal_count": len(data_hits),
        "error_signal_count": len(errors),
        "successes": [asdict(p) for p in successes],
        "probes": [asdict(p) for p in probes],
    }

    report_path = root / "iea_mes_sdmx_discovery_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 78)
    print("IEA MESGEN / MESBAL SDMX DISCOVERY")
    print("=" * 78)
    print(f"Probes:             {len(probes)}")
    print(f"Successful payloads:{len(successes)}")
    print(f"Data signals:       {len(data_hits)}")
    print(f"Error signals:      {len(errors)}")
    print(f"Report:             {report_path}")
    print()

    if successes:
        print("SUCCESSFUL DATA PATH(S):")
        for p in successes:
            print(f"  {p.status} {p.url}")
            print(f"     {p.content_type} | {p.bytes} bytes | {p.notes}")
    else:
        print("No confirmed MESGEN/MESBAL payload path found.")
        print("This is a discovery result; production code was not changed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
