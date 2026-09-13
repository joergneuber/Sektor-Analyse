#!/usr/bin/env python3
"""
IEA MES SDMX Dataflow/DSD reference forensics v4.0

Diagnostic only. It does not modify production code or the cache.

Core correction versus v3.x:
  MESGEN/MESBAL are treated strictly as DSD IDs, never as Dataflow IDs.

Questions tested, in order:
  Q1  Which real Dataflow objects are exposed by the IEA structure service?
  Q2  Do those Dataflows explicitly reference DSD MESGEN/MESBAL?
  Q3  Do direct DSD requests with references=parents/all expose a parent Dataflow?
  Q4  Does the same relationship appear through REST v1/v2 structure endpoints?
  Q5  Only if a real Dataflow->DSD relationship is proven: can a bounded data
      request return observations for that Dataflow?

A Dataflow becomes a candidate ONLY when the response semantics prove that a
Dataflow object references the target DSD. Seeing the string MESGEN/MESBAL in
an error or unrelated structure is never sufficient.
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
AGENCY = "OECD.IEA"
TARGET_DSD = {"MESGEN", "MESBAL"}
TARGET_VERSION = "1.1"

# v2.1-style resource endpoints and the IEA's advertised REST v2 surface.
DATAFLOW_PATHS = [
    "/rest/dataflow",
    "/rest/dataflow/all/all/latest",
    "/rest/dataflow/OECD.IEA/all/latest",
    "/rest/v1/dataflow",
    "/rest/v1/dataflow/all/all/latest",
    "/rest/v1/dataflow/OECD.IEA/all/latest",
    "/rest/v2/dataflow",
    "/rest/v2/dataflow/all/all/latest",
    "/rest/v2/dataflow/OECD.IEA/all/latest",
    "/rest/structure/dataflow",
    "/rest/v1/structure/dataflow",
    "/rest/v2/structure/dataflow",
]

DSD_PATHS = [
    "/rest/datastructure/OECD.IEA/MESGEN/1.1",
    "/rest/datastructure/OECD.IEA/MESBAL/1.1",
    "/rest/v1/datastructure/OECD.IEA/MESGEN/1.1",
    "/rest/v1/datastructure/OECD.IEA/MESBAL/1.1",
    "/rest/v2/datastructure/OECD.IEA/MESGEN/1.1",
    "/rest/v2/datastructure/OECD.IEA/MESBAL/1.1",
]

ACCEPTS = [
    ("sdmx_xml", "application/vnd.sdmx.structure+xml;version=2.1"),
    ("xml", "application/xml"),
    ("sdmx_json", "application/vnd.sdmx.structure+json;version=2.0"),
    ("json", "application/json"),
]

DATA_ACCEPTS = [
    ("sdmx_csv", "application/vnd.sdmx.data+csv;version=2.0.0"),
    ("csv", "text/csv"),
    ("sdmx_xml", "application/vnd.sdmx.genericdata+xml;version=2.1"),
    ("xml", "application/xml"),
]

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
    semantic_dataflows: list[dict[str, str]]
    dsd_refs: list[dict[str, str]]
    dataflow_dsd_links: list[dict[str, str]]
    target_hits: list[str]
    observation_count: int
    strict_payload: bool
    error_text: str | None


def dedupe(items):
    out, seen = [], set()
    for item in items:
        key = tuple(sorted(item.items())) if isinstance(item, dict) else str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def local_path(root: Path, category: str, name: str, status: int | None,
               url: str, suffix: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:20]
    return root / f"{category}_{name}_{status or 'ERR'}_{digest}{suffix}"


def attrs(elem: ET.Element) -> dict[str, str]:
    return {k.rsplit("}", 1)[-1].lower(): v for k, v in elem.attrib.items()}


def ref_from_elem(elem: ET.Element) -> dict[str, str] | None:
    a = attrs(elem)
    ident = a.get("id") or a.get("idref") or a.get("resourceid") or a.get("ref")
    if not ident:
        # Some SDMX forms put a URN in the text.
        text = (elem.text or "").strip()
        m = re.search(r"=(?:[^=]+:)?([^:(]+)\(([^)]+)\)", text)
        if m:
            ident = m.group(1)
            return {"id": ident, "agency": "", "version": m.group(2)}
        return None
    return {"id": ident, "agency": a.get("agencyid") or a.get("agency") or "",
            "version": a.get("version") or ""}


def xml_semantics(text: str):
    """Return actual Dataflow objects, DSD objects, and Dataflow->DSD links."""
    flows, dsds, links = [], [], []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return flows, dsds, links

    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        if tag == "dataflow":
            fa = attrs(elem)
            flow = {"id": fa.get("id", ""), "agency": fa.get("agencyid", fa.get("agency", "")),
                    "version": fa.get("version", "")}
            if flow["id"]:
                flows.append(flow)
                for child in elem.iter():
                    ctag = child.tag.rsplit("}", 1)[-1].lower()
                    if ctag not in {"ref", "structure", "datastructureref", "structureuse"}:
                        continue
                    r = ref_from_elem(child)
                    if r and r["id"].upper() in TARGET_DSD:
                        links.append({
                            "dataflow_id": flow["id"],
                            "dataflow_agency": flow["agency"],
                            "dataflow_version": flow["version"],
                            "dsd_id": r["id"],
                            "dsd_agency": r["agency"] or flow["agency"],
                            "dsd_version": r["version"] or TARGET_VERSION,
                            "relation": ctag,
                        })
        elif tag == "datastructure":
            a = attrs(elem)
            ident = a.get("id")
            if ident:
                dsds.append({"id": ident, "agency": a.get("agencyid", a.get("agency", "")),
                             "version": a.get("version", "")})
    return dedupe(flows), dedupe(dsds), dedupe(links)


def json_semantics(obj: Any):
    """Conservative JSON parser: classify by explicit Dataflow/DataStructure context."""
    flows, dsds, links = [], [], []

    def walk(x, context="", current_flow=None):
        if isinstance(x, dict):
            lowctx = context.lower()
            ident = x.get("id") or x.get("resourceId") or x.get("resourceid")
            agency = x.get("agencyID") or x.get("agencyId") or x.get("agency") or ""
            version = x.get("version") or ""
            is_flow = "dataflow" in lowctx
            is_dsd = "datastructure" in lowctx or lowctx.endswith("/structure")
            flow = current_flow
            if isinstance(ident, str) and ident:
                item = {"id": ident, "agency": str(agency), "version": str(version)}
                if is_flow:
                    flows.append(item)
                    flow = item
                elif is_dsd:
                    dsds.append(item)
                if flow and ident.upper() in TARGET_DSD and not is_flow:
                    links.append({"dataflow_id": flow["id"], "dataflow_agency": flow["agency"],
                                  "dataflow_version": flow["version"], "dsd_id": ident,
                                  "dsd_agency": str(agency), "dsd_version": str(version),
                                  "relation": context.split("/")[-1]})
            for k, v in x.items():
                walk(v, f"{context}/{k}", flow)
        elif isinstance(x, list):
            for v in x:
                walk(v, context, current_flow)

    walk(obj)
    return dedupe(flows), dedupe(dsds), dedupe(links)


def semantics(text: str, ctype: str):
    if "json" in ctype.lower() or text.lstrip().startswith(("{", "[")):
        try:
            return json_semantics(json.loads(text))
        except json.JSONDecodeError:
            pass
    if text.lstrip().startswith("<"):
        return xml_semantics(text)
    return [], [], []


def count_observations(text: str, ctype: str) -> int:
    if "csv" in ctype.lower():
        lines = [x for x in text.splitlines() if x.strip()]
        if lines and "obs_value" in lines[0].lower() and "time_period" in lines[0].lower():
            return max(0, len(lines) - 1)
    if "xml" in ctype.lower() or text.lstrip().startswith("<"):
        return len(re.findall(r"(?i)<(?:\w+:)?Obs(?:\s|>)", text))
    return len(re.findall(r"(?i)[\"']obs_value[\"']", text))


def strict_payload(text: str, ctype: str, status: int | None):
    if status != 200:
        return False, 0
    low = text.lower()
    if any(x in low for x in ("could not find", "not found", "unsupportedapiversion",
                              "bad request", "internal server error")):
        return False, 0
    obs = count_observations(text, ctype)
    return bool(obs > 0 and OBS_RE.search(text) and TIME_RE.search(text)), obs


def request(session, root, category, host, path, name, accept, timeout):
    url = host.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        r = session.get(url, headers={"Accept": accept,
                                      "User-Agent": "NEUBER-MACRO-IEA-SDMX-DATAFLOW-DISCOVERY/4.0"},
                        timeout=timeout, allow_redirects=True)
        elapsed = int((time.perf_counter() - t0) * 1000)
        raw = r.content
        ctype = r.headers.get("content-type", "")
        text = raw[:8_000_000].decode("utf-8", errors="replace")
        flows, dsds, links = semantics(text, ctype)
        targets = sorted(t for t in TARGET_DSD if t.lower() in text.lower())
        ok, obs = strict_payload(text, ctype, r.status_code)
        suffix = ".xml" if "xml" in ctype.lower() or text.lstrip().startswith("<") else ".json" if "json" in ctype.lower() else ".txt"
        fp = local_path(root, category, name, r.status_code, url, suffix)
        fp.write_bytes(raw[:8_000_000])
        return Result(category, host, url, r.status_code, ctype, len(raw), elapsed,
                      str(fp.relative_to(root.parent)), flows, dsds, links, targets,
                      obs, ok, None if r.status_code == 200 else text[:1000])
    except requests.RequestException as exc:
        return Result(category, host, url, None, "", 0,
                      int((time.perf_counter() - t0) * 1000), None, [], [], [], [],
                      0, False, f"{type(exc).__name__}: {exc}")


def main():
    ap = argparse.ArgumentParser(description="IEA MES SDMX reference forensics v4.0")
    ap.add_argument("--output", default="iea_sdmx_dataflow_discovery")
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    results = []
    # Q1/Q2: discover real Dataflow objects and explicit links to MES DSDs.
    for host in HOSTS:
        for path in DATAFLOW_PATHS:
            for name, accept in ACCEPTS:
                results.append(request(session, root, "dataflow_structure", host, path, name, accept, args.timeout))

    # Q3/Q4: ask the known DSD directly for parents/all references.
    dsd_ref_results = []
    for host in HOSTS:
        for base in DSD_PATHS:
            for refmode in ("parents", "all"):
                path = base + "?references=" + refmode
                for name, accept in ACCEPTS[:2]:
                    dsd_ref_results.append(request(session, root, "dsd_references", host, path,
                                                    name + "_" + refmode, accept, args.timeout))

    all_results = results + dsd_ref_results
    links = dedupe([x for r in all_results for x in r.dataflow_dsd_links])
    observed_flows = dedupe([x for r in all_results for x in r.semantic_dataflows])
    observed_dsds = dedupe([x for r in all_results for x in r.dsd_refs])

    # Only explicit Dataflow -> target DSD links create data candidates.
    candidates = links

    data_results = []
    for link in candidates:
        agency = link["dataflow_agency"] or AGENCY
        flow_id = link["dataflow_id"]
        version = link["dataflow_version"] or TARGET_VERSION
        flowref = f"{agency},{flow_id},{version}"
        for host in HOSTS:
            for prefix in ("/rest", "/rest/v1", "/rest/v2"):
                for key in ("all", "DEU"):
                    path = f"{prefix}/data/{quote(flowref, safe=',')}/{key}/{quote(agency, safe='')}"
                    for name, accept in DATA_ACCEPTS:
                        rr = request(session, root, "data", host, path, name, accept, args.timeout)
                        data_results.append(rr)
                        if rr.strict_payload:
                            break
                    if data_results and data_results[-1].strict_payload:
                        break

    confirmed = [r for r in data_results if r.strict_payload]
    report = {
        "tool": "iea_sdmx_dataflow_discovery",
        "version": "4.0-reference-forensics",
        "purpose": "Discover real Dataflow objects that explicitly reference MESGEN/MESBAL DSDs.",
        "hosts": HOSTS,
        "target_dsd": sorted(TARGET_DSD),
        "registry_probe_count": len(results),
        "dsd_reference_probe_count": len(dsd_ref_results),
        "observed_dataflows": observed_flows,
        "observed_dsds": observed_dsds,
        "explicit_dataflow_to_target_dsd_links": links,
        "candidate_dataflows": candidates,
        "data_probe_count": len(data_results),
        "confirmed_data_payloads": [asdict(x) for x in confirmed],
        "dataflow_structure_results": [asdict(x) for x in results],
        "dsd_reference_results": [asdict(x) for x in dsd_ref_results],
        "data_results": [asdict(x) for x in data_results],
    }
    rp = root / "iea_sdmx_dataflow_discovery_report.json"
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 78)
    print("IEA SDMX DATAFLOW / DSD REFERENCE FORENSICS v4.0")
    print("=" * 78)
    print(f"Dataflow-structure probes: {len(results)}")
    print(f"DSD reference probes:      {len(dsd_ref_results)}")
    print(f"Real Dataflows observed:   {len(observed_flows)}")
    print(f"Target DSDs observed:      {len(observed_dsds)}")
    print(f"EXPLICIT Dataflow->DSD:    {links}")
    print(f"Data probes:               {len(data_results)}")
    print(f"CONFIRMED data payloads:   {len(confirmed)}")
    print(f"Report:                    {rp}")
    for item in confirmed:
        print(f"CONFIRMED {item.url} [{item.content_type}] observations={item.observation_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
