#!/usr/bin/env python3
"""
IEA SDMX Dataflow / DSD Registry Discovery v3.1

Purpose:
  Find the actual IEA MESGEN/MESBAL DATAFLOW references from the official
  SDMX structure/registry responses, resolve their DSD references, and only
  then test a bounded set of real data requests.

This is diagnostic only. It never modifies struktur_trends.py or the cache,
and it performs no git operations.

v3.1 fixes:
  - deterministic response-path handling
  - robust XML + JSON dataflow extraction
  - explicit Dataflow/DSD reference correlation
  - strict "confirmed payload" validation
  - bounded, auditable data-request matrix
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

HOSTS = [
    "https://sis-cc-nsi-stable.iea.org",
    "https://sis-cc-api-stable.iea.org",
]
TARGETS = {"MESGEN", "MESBAL"}
AGENCY = "OECD.IEA"
TARGET_VERSION = "1.1"

REGISTRY_PATHS = [
    "/rest/dataflow",
    "/rest/dataflow/all/all/latest",
    "/rest/dataflow/OECD.IEA/all/latest",
    "/rest/v1/dataflow",
    "/rest/v1/dataflow/all/all/latest",
    "/rest/v1/dataflow/OECD.IEA/all/latest",
    "/rest/structure",
    "/rest/v1/structure",
    "/rest/structure/dataflow",
    "/rest/v1/structure/dataflow",
]

DSD_PATHS = [
    "/rest/datastructure/OECD.IEA/MESGEN/1.1",
    "/rest/datastructure/OECD.IEA/MESBAL/1.1",
    "/rest/v1/datastructure/OECD.IEA/MESGEN/1.1",
    "/rest/v1/datastructure/OECD.IEA/MESBAL/1.1",
]

ACCEPTS = [
    ("sdmx_xml", "application/vnd.sdmx.structure+xml;version=2.1"),
    ("xml", "application/xml"),
    ("json", "application/json"),
    ("sdmx_json", "application/vnd.sdmx.structure+json;version=2.0"),
]

DATA_ACCEPTS = [
    ("sdmx_csv", "application/vnd.sdmx.data+csv;version=2.0.0"),
    ("csv", "text/csv"),
    ("sdmx_xml", "application/vnd.sdmx.genericdata+xml;version=2.1"),
    ("xml", "application/xml"),
]

DATAFLOW_ID_RE = re.compile(
    r"(?i)\b(?:MESGEN|MESBAL)\b"
)
OBS_RE = re.compile(r"(?i)\bOBS_VALUE\b")
TIME_RE = re.compile(r"(?i)\bTIME_PERIOD\b")


@dataclass
class Result:
    category: str
    host: str
    url: str
    status: int | None
    content_type: str
    bytes: int
    elapsed_ms: int
    saved: str | None
    target_hits: list[str]
    dataflow_refs: list[dict[str, str]]
    dsd_refs: list[dict[str, str]]
    observation_count: int
    strict_payload: bool
    error_text: str | None


def local_path(root: Path, category: str, accept_name: str,
               status: int | None, url: str, suffix: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return root / f"{category}_{accept_name}_{status or 'ERR'}_{digest}{suffix}"


def extract_refs_from_xml(text: str):
    dataflows: list[dict[str, str]] = []
    dsds: list[dict[str, str]] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return dataflows, dsds

    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        attrs = {k.rsplit("}", 1)[-1].lower(): v for k, v in elem.attrib.items()}

        if tag == "dataflow":
            ident = attrs.get("id") or attrs.get("ref") or attrs.get("resourceid")
            agency = attrs.get("agencyid") or attrs.get("agency")
            version = attrs.get("version")
            if ident:
                dataflows.append({
                    "id": ident,
                    "agency": agency or "",
                    "version": version or "",
                })

        if tag in {"structure", "datastructure"}:
            ident = attrs.get("id") or attrs.get("ref") or attrs.get("resourceid")
            agency = attrs.get("agencyid") or attrs.get("agency")
            version = attrs.get("version")
            if ident:
                dsds.append({
                    "id": ident,
                    "agency": agency or "",
                    "version": version or "",
                })

        # SDMX StructureRef/DataStructureRef often appears as child elements.
        if tag in {"ref", "dataflowref", "datastructureref", "structure"}:
            ident = attrs.get("id") or attrs.get("ref") or attrs.get("idref")
            agency = attrs.get("agencyid") or attrs.get("agency")
            version = attrs.get("version")
            if ident and ("MES" in ident.upper() or "STRUCT" in tag):
                dsds.append({
                    "id": ident,
                    "agency": agency or "",
                    "version": version or "",
                })
    return dataflows, dsds


def walk_json(obj: Any, dataflows: list[dict[str, str]],
              dsds: list[dict[str, str]], context: str = ""):
    if isinstance(obj, dict):
        keys = {str(k).lower(): v for k, v in obj.items()}
        ident = keys.get("id") or keys.get("ref") or keys.get("resourceid")
        agency = keys.get("agencyid") or keys.get("agency")
        version = keys.get("version")
        label = context.lower()

        if ident and isinstance(ident, str) and ident.upper() in TARGETS:
            item = {"id": ident, "agency": str(agency or ""), "version": str(version or "")}
            if "dataflow" in label:
                dataflows.append(item)
            else:
                dsds.append(item)

        for k, v in obj.items():
            walk_json(v, dataflows, dsds, f"{context}/{k}")
    elif isinstance(obj, list):
        for item in obj:
            walk_json(item, dataflows, dsds, context)


def extract_refs(text: str, content_type: str):
    dataflows: list[dict[str, str]] = []
    dsds: list[dict[str, str]] = []

    if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
        try:
            walk_json(json.loads(text), dataflows, dsds)
        except json.JSONDecodeError:
            pass

    if not dataflows and not dsds and text.lstrip().startswith("<"):
        dataflows, dsds = extract_refs_from_xml(text)

    # Conservative fallback: only record exact target IDs that actually occur.
    for target in TARGETS:
        if target.lower() in text.lower():
            if not any(x["id"].upper() == target for x in dataflows):
                # Do not pretend this is a Dataflow; mark it as observed text.
                pass

    return dedupe_dicts(dataflows), dedupe_dicts(dsds)


def dedupe_dicts(items):
    out = []
    seen = set()
    for item in items:
        key = tuple(sorted(item.items()))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def count_observations(text: str, content_type: str) -> int:
    low = text.lower()
    if "csv" in content_type.lower() or "," in text[:500]:
        lines = [x for x in text.splitlines() if x.strip()]
        if not lines:
            return 0
        header = lines[0].lower()
        if "obs_value" in header and "time_period" in header:
            return sum(
                1 for line in lines[1:]
                if line.strip() and not line.lstrip().startswith("#")
            )
    if "xml" in content_type.lower() or text.lstrip().startswith("<"):
        return len(re.findall(r"(?i)<(?:\w+:)?Obs(?:\s|>)", text))
    if "json" in content_type.lower():
        return len(re.findall(r"(?i)[\"']obs_value[\"']", text))
    return 0


def strict_payload(text: str, content_type: str, status: int | None) -> tuple[bool, int]:
    if status != 200:
        return False, 0
    if any(x in text.lower() for x in [
        "could not find", "not found", "unsupportedapiversion",
        "bad request", "object reference not set", "internal server error"
    ]):
        return False, 0
    obs = count_observations(text, content_type)
    has_obs = bool(OBS_RE.search(text))
    has_time = bool(TIME_RE.search(text))
    return bool(obs > 0 and has_obs and has_time), obs


def request(session: requests.Session, root: Path, category: str,
            host: str, path: str, accept_name: str, accept: str,
            timeout: int) -> Result:
    url = host.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        r = session.get(
            url,
            headers={"Accept": accept,
                     "User-Agent": "NEUBER-MACRO-IEA-SDMX-DATAFLOW-DISCOVERY/3.1"},
            timeout=timeout,
            allow_redirects=True,
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        raw = r.content
        ctype = r.headers.get("content-type", "")
        text = raw[:8_000_000].decode("utf-8", errors="replace")
        dataflows, dsds = extract_refs(text, ctype)
        target_hits = sorted({
            t for t in TARGETS if t.lower() in text.lower()
        })
        ok, obs = strict_payload(text, ctype, r.status_code)

        suffix = ".xml" if "xml" in ctype.lower() or text.lstrip().startswith("<") else (
            ".json" if "json" in ctype.lower() or text.lstrip().startswith(("{","[")) else ".txt"
        )
        fp = local_path(root, category, accept_name, r.status_code, url, suffix)
        fp.write_bytes(raw[:8_000_000])

        err = None
        if r.status_code != 200:
            err = text[:1000]
        return Result(category, host, url, r.status_code, ctype, len(raw),
                      elapsed, str(fp.relative_to(root.parent)), target_hits,
                      dataflows, dsds, obs, ok, err)
    except requests.RequestException as exc:
        return Result(category, host, url, None, "", 0,
                      int((time.perf_counter()-t0)*1000), None, [], [], [],
                      0, False, f"{type(exc).__name__}: {exc}")


def main():
    ap = argparse.ArgumentParser(description="IEA MESGEN/MESBAL Dataflow discovery v3.1")
    ap.add_argument("--output", default="iea_sdmx_dataflow_discovery")
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args()

    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    registry: list[Result] = []
    for host in HOSTS:
        for path in REGISTRY_PATHS + DSD_PATHS:
            for name, accept in ACCEPTS:
                registry.append(request(session, root, "registry", host, path, name, accept, args.timeout))

    observed_flows = []
    observed_dsds = []
    for r in registry:
        observed_flows.extend(r.dataflow_refs)
        observed_dsds.extend(r.dsd_refs)
    observed_flows = dedupe_dicts(observed_flows)
    observed_dsds = dedupe_dicts(observed_dsds)

    # Only IDs actually returned in registry/structure content can reach this stage.
    candidate_flows = [
        x for x in observed_flows
        if x["id"].upper() in TARGETS
    ]

    data_results: list[Result] = []
    for flow in candidate_flows:
        flow_id = flow["id"]
        agency = flow["agency"] or AGENCY
        version = flow["version"] or TARGET_VERSION
        flowrefs = [f"{agency},{flow_id},{version}", f"{agency},{flow_id}", flow_id]
        for host in HOSTS:
            for prefix in ("/rest", "/rest/v1"):
                for flowref in flowrefs:
                    for provider in ("all", agency):
                        for key in ("all", "DEU"):
                            path = f"{prefix}/data/{quote(flowref, safe=',')}/{key}/{quote(provider, safe='')}"
                            for name, accept in DATA_ACCEPTS:
                                rr = request(session, root, "data", host, path, name, accept, args.timeout)
                                data_results.append(rr)
                                if rr.strict_payload:
                                    break
                            if data_results[-1].strict_payload:
                                break

    confirmed = [r for r in data_results if r.strict_payload]

    report = {
        "tool": "iea_sdmx_dataflow_discovery",
        "version": "3.1",
        "hosts": HOSTS,
        "targets": sorted(TARGETS),
        "agency_default": AGENCY,
        "target_version": TARGET_VERSION,
        "registry_probe_count": len(registry),
        "observed_dataflows": observed_flows,
        "observed_dsds": observed_dsds,
        "candidate_target_dataflows": candidate_flows,
        "data_probe_count": len(data_results),
        "confirmed_data_payloads": [asdict(x) for x in confirmed],
        "registry_results": [asdict(x) for x in registry],
        "data_results": [asdict(x) for x in data_results],
    }
    rp = root / "iea_sdmx_dataflow_discovery_report.json"
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 78)
    print("IEA SDMX DATAFLOW / DSD REGISTRY DISCOVERY v3.1")
    print("=" * 78)
    print(f"Registry probes:        {len(registry)}")
    print(f"Observed dataflows:     {observed_flows}")
    print(f"Target dataflows:       {candidate_flows}")
    print(f"Data probes:            {len(data_results)}")
    print(f"CONFIRMED data payloads:{len(confirmed)}")
    print(f"Report:                 {rp}")
    for item in confirmed:
        print(f"CONFIRMED {item.url} [{item.content_type}] observations={item.observation_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
