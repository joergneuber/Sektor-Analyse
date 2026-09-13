#!/usr/bin/env python3
"""
IEA MES SDMX provisioning/data-source forensics v5.0

Diagnostic only. No production/cache changes.

Lauf 32 questions, deliberately narrow:
  Q1  Provision Agreements: does the official IEA SDMX REST service expose a
      ProvisionAgreement for MESGEN/MESBAL or for their Dataflows?
  Q2  DataProvider / DataSource: does the ProvisionAgreement identify a provider,
      registration, data source URL, or actionable annotation?
  Q3  Official IEA download references: what do the MESGEN/MESBAL product-download
      links actually return (redirect, ZIP, CSV, SDMX, HTML)?
  Q4  NonProductionDataflow: are there explicit annotations/references indicating
      a production/superseding flow?
  Q5  Only after Q1-Q4 establish an evidence-backed route: perform at most ONE
      data request per proven route, with no blind matrix.

Important safeguards:
  - MESGEN/MESBAL are accepted as Dataflow IDs only when the IEA response contains
    an actual Dataflow object.
  - A string occurrence in an error, URL, or unrelated structure is not a flow.
  - ProvisionAgreement IDs are never invented. They are extracted from returned
    SDMX semantics first; only then are detail requests made.
  - Suggested agency 'IEA' is tested only as a bounded hypothesis; the official
    IEA agency observed in Lauf 31 is OECD.IEA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import zipfile
import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

HOSTS = [
    "https://sis-cc-nsi-stable.iea.org",
    "https://sis-cc-api-stable.iea.org",
]
AGENCIES = ["OECD.IEA", "IEA"]
TARGETS = {
    "MESGEN": {"dataflow": "MESGEN", "download": "https://www.iea.org/product/download/023477-000289-023189"},
    "MESBAL": {"dataflow": "MESBAL", "download": "https://www.iea.org/product/download/023477-000289-023188"},
}

# Standard SDMX REST resource forms, plus the IEA advertised REST v2 surface.
PA_LIST_PATHS = [
    "/rest/provisionagreement/OECD.IEA/all/latest",
    "/rest/provisionagreement/IEA/all/latest",
    "/rest/provisionagreement",
    "/rest/v1/provisionagreement/OECD.IEA/all/latest",
    "/rest/v1/provisionagreement/IEA/all/latest",
    "/rest/v2/provisionagreement/OECD.IEA/all/latest",
    "/rest/v2/provisionagreement/IEA/all/latest",
]
FLOW_REF_PATHS = [
    "/rest/dataflow/OECD.IEA/{flow}/1.1?references=provisionagreement",
    "/rest/v1/dataflow/OECD.IEA/{flow}/1.1?references=provisionagreement",
    "/rest/v2/dataflow/OECD.IEA/{flow}/1.1?references=provisionagreement",
]

STRUCTURE_ACCEPTS = [
    ("sdmx_xml", "application/vnd.sdmx.structure+xml;version=2.1"),
    ("xml", "application/xml"),
    ("sdmx_json", "application/vnd.sdmx.structure+json;version=2.0"),
    ("json", "application/json"),
]

DOWNLOAD_URLS = {k: v["download"] for k, v in TARGETS.items()}
OBS_RE = re.compile(r"(?i)\bOBS_VALUE\b")
TIME_RE = re.compile(r"(?i)\bTIME_PERIOD\b")
ANNOTATION_TYPES = {
    "ENDPOINT", "REST_ENDPOINT", "DOWNLOAD_URL", "PRIMARY_MEASURE",
    "SUPERSEDED_BY", "PRODUCTION_FLOW_REF", "PRODUCTION_DATAFLOW",
    "DATA_SOURCE", "DATASOURCE", "URL", "SOURCE", "REGISTRATION",
}

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
    dataflows: list[dict[str, str]]
    dsds: list[dict[str, str]]
    provision_agreements: list[dict[str, str]]
    data_providers: list[dict[str, str]]
    data_sources: list[str]
    annotations: list[dict[str, str]]
    links: list[dict[str, str]]
    target_hits: list[str]
    observation_count: int
    strict_payload: bool
    error_text: str | None


def dedupe(items):
    out, seen = [], set()
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, dict) else str(item)
        if key not in seen:
            seen.add(key); out.append(item)
    return out


def attrs(elem):
    return {k.rsplit("}", 1)[-1].lower(): v for k, v in elem.attrib.items()}


def ref(elem):
    a = attrs(elem)
    ident = a.get("id") or a.get("idref") or a.get("resourceid")
    if ident:
        return {"id": ident, "agency": a.get("agencyid", a.get("agency", "")), "version": a.get("version", "")}
    txt = (elem.text or "").strip()
    m = re.search(r"(?:Dataflow|ProvisionAgreement|DataProvider|DataStructure)[^=]*=([^:(]+):?([^:(]*)\(([^)]+)\)", txt, re.I)
    if m:
        return {"id": m.group(2) or m.group(1), "agency": m.group(1), "version": m.group(3)}
    return None


def parse_xml(text: str):
    flows=[]; dsds=[]; pas=[]; providers=[]; sources=[]; annotations=[]; links=[]
    try: root=ET.fromstring(text)
    except ET.ParseError: return flows,dsds,pas,providers,sources,annotations,links
    for e in root.iter():
        tag=e.tag.rsplit("}",1)[-1].lower(); a=attrs(e)
        if tag == "dataflow":
            x={"id":a.get("id",""),"agency":a.get("agencyid",a.get("agency","")),"version":a.get("version","")}
            if x["id"]: flows.append(x)
            if a.get("isfinal"): x["is_final"]=a["isfinal"]
            if a.get("nonproductiondataflow"): x["nonproductiondataflow"]=a["nonproductiondataflow"]
        elif tag == "datastructure":
            x={"id":a.get("id",""),"agency":a.get("agencyid",a.get("agency","")),"version":a.get("version","")}
            if x["id"]: dsds.append(x)
        elif tag == "provisionagreement":
            x={"id":a.get("id",""),"agency":a.get("agencyid",a.get("agency","")),"version":a.get("version","")}
            if x["id"]: pas.append(x)
        elif tag in {"dataprovider","dataproviderref"}:
            x=ref(e)
            if x and x["id"]: providers.append(x)
        elif tag in {"datasource","datasourceref","dataregistration","registration"}:
            txt=(e.text or "").strip()
            if txt.startswith(("http://","https://")): sources.append(txt)
            href=a.get("href") or a.get("uri") or a.get("url")
            if href: sources.append(href)
        elif tag == "annotation":
            at=a.get("annotationtype","")
            title=a.get("annotationtitle","")
            textval=" ".join("".join(e.itertext()).split())
            if at or title or textval:
                rec={"type":at,"title":title,"text":textval[:1000]}
                annotations.append(rec)
                low=(at+" "+title+" "+textval).lower()
                if any(k.lower() in low for k in ANNOTATION_TYPES):
                    for u in re.findall(r"https?://[^\s<>\"']+", textval): sources.append(u.rstrip(".,;"))
        elif tag in {"dataflowref","datastructureref","provisionagreementref","registrationref"}:
            x=ref(e)
            if x: links.append({"kind":tag, **x})
        # Some SDMX structures use Ref with class/package attributes.
        elif tag == "ref" and (a.get("class") or a.get("package")):
            x=ref(e)
            if x: links.append({"kind":f"{a.get('package','')}.{a.get('class','')}", **x})
    return [*dedupe(flows)],[*dedupe(dsds)],[*dedupe(pas)],[*dedupe(providers)],dedupe(sources),dedupe(annotations),dedupe(links)


def save_body(root:Path, category:str, url:str, status:int|None, body:bytes, suffix:str):
    d=hashlib.sha256(url.encode()).hexdigest()[:20]
    p=root/f"{category}_{status or 'ERR'}_{d}{suffix}"; p.write_bytes(body); return str(p)


def fetch(session, root, category, host, url, accept, timeout):
    headers={"User-Agent":"NEUBER-MACRO-MES-forensics/5.0","Accept":accept,"Accept-Encoding":"gzip, deflate"}
    t=time.perf_counter()
    try:
        r=session.get(url,headers=headers,timeout=timeout,allow_redirects=True)
        ms=int((time.perf_counter()-t)*1000); body=r.content
        suffix=".json" if "json" in r.headers.get("Content-Type","").lower() else ".xml" if "xml" in r.headers.get("Content-Type","").lower() else ".bin"
        saved=save_body(root,category,url,r.status_code,body,suffix)
        text=body.decode("utf-8","replace")
        f,d,p,pr,s,a,l=parse_xml(text)
        err=None if r.status_code==200 else text[:500]
        return Result(category,host,url,r.status_code,r.headers.get("Content-Type",""),len(body),ms,saved,f,d,p,pr,s,a,l,[],len(OBS_RE.findall(text)),bool(r.status_code==200 and OBS_RE.search(text) and TIME_RE.search(text)),err),r
    except Exception as e:
        return Result(category,host,url,None,"",0,int((time.perf_counter()-t)*1000),None,[],[],[],[],[],[],[],[],0,False,str(e)),None


def download_probe(session, root, key, url, timeout):
    t=time.perf_counter()
    try:
        r=session.get(url,headers={"User-Agent":"NEUBER-MACRO-MES-forensics/5.0","Accept":"*/*"},timeout=timeout,allow_redirects=True)
        body=r.content; ms=int((time.perf_counter()-t)*1000)
        ct=r.headers.get("Content-Type",""); final=r.url
        suffix=".zip" if body[:2]==b"PK" else ".bin"
        if "text/html" in ct.lower(): suffix=".html"
        elif "csv" in ct.lower(): suffix=".csv"
        saved=save_body(root,"download_"+key,final,r.status_code,body,suffix)
        zip_members=[]
        if body[:2]==b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as z: zip_members=z.namelist()[:100]
            except zipfile.BadZipFile: pass
        return {"category":"official_download","target":key,"url":url,"final_url":final,"status":r.status_code,"content_type":ct,"bytes":len(body),"elapsed_ms":ms,"saved":saved,"zip_members":zip_members,"looks_like_sdmx":bool(b"OBS_VALUE" in body or b"TIME_PERIOD" in body),"error":None if r.status_code==200 else body[:500].decode("utf-8","replace")}
    except Exception as e:
        return {"category":"official_download","target":key,"url":url,"final_url":None,"status":None,"content_type":"","bytes":0,"elapsed_ms":int((time.perf_counter()-t)*1000),"saved":None,"zip_members":[],"looks_like_sdmx":False,"error":str(e)}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--output",default="iea_sdmx_dataflow_discovery"); ap.add_argument("--timeout",type=int,default=25); args=ap.parse_args()
    root=Path(args.output); root.mkdir(parents=True,exist_ok=True)
    s=requests.Session(); results=[]; downloads=[]

    # Q1/Q2: PA lists and exact Dataflow references. Bounded: 7 paths x 2 hosts + 6 exact flow-ref probes x 2 hosts.
    for host in HOSTS:
        for path in PA_LIST_PATHS:
            for label,accept in STRUCTURE_ACCEPTS[:2]:
                res,_=fetch(s,root,"pa_list",host,host+path,accept,args.timeout); results.append(res)
        for flow in TARGETS:
            for path in FLOW_REF_PATHS:
                for label,accept in STRUCTURE_ACCEPTS[:2]:
                    res,_=fetch(s,root,"flow_pa_ref",host,host+path.format(flow=flow),accept,args.timeout); results.append(res)

    # Q3: official IEA product download surfaces, exactly once each.
    for key,url in DOWNLOAD_URLS.items(): downloads.append(download_probe(s,root,key,url,args.timeout))

    # Extract PA identities only from real PA XML responses, then query exact PA detail once per unique identity.
    pa_ids=[]
    for r in results:
        if r.category in {"pa_list","flow_pa_ref"}:
            for p in r.provision_agreements:
                if p["id"]: pa_ids.append(p)
    pa_ids=dedupe(pa_ids)
    pa_details=[]
    for p in pa_ids:
        agency=p.get("agency") or "OECD.IEA"; version=p.get("version") or "latest"
        for host in HOSTS:
            path=f"/rest/provisionagreement/{agency}/{p['id']}/{version}"
            for label,accept in STRUCTURE_ACCEPTS[:2]:
                res,_=fetch(s,root,"pa_detail",host,host+path,accept,args.timeout); pa_details.append(res)

    allres=results+pa_details
    flows=dedupe([x for r in allres for x in r.dataflows])
    dsds=dedupe([x for r in allres for x in r.dsds])
    pas=dedupe([x for r in allres for x in r.provision_agreements])
    providers=dedupe([x for r in allres for x in r.data_providers])
    sources=dedupe([x for r in allres for x in r.data_sources])
    annotations=dedupe([x for r in allres for x in r.annotations if any(k.lower() in json.dumps(x).lower() for k in ANNOTATION_TYPES)])
    links=dedupe([x for r in allres for x in r.links])

    # Only evidence-backed endpoint candidates may reach Q5. No blind data probes in v5.
    endpoint_candidates=[]
    for u in sources:
        if u.startswith(("http://","https://")): endpoint_candidates.append({"url":u,"source":"sdmx_metadata"})
    for d in downloads:
        if d.get("status")==200 and d.get("final_url") and d.get("final_url")!=d.get("url"):
            endpoint_candidates.append({"url":d["final_url"],"source":"official_download_redirect","target":d["target"]})
    endpoint_candidates=dedupe(endpoint_candidates)

    report={
      "version":"5.0-targeted-provision-download",
      "questions":[
        "ProvisionAgreement exposure and references",
        "DataProvider/DataSource/annotation discovery",
        "official MESGEN/MESBAL product-download behavior",
        "NonProductionDataflow/production-flow evidence",
        "only evidence-backed data endpoint; no blind matrix"
      ],
      "hosts":HOSTS,"agencies_tested":AGENCIES,
      "targets":TARGETS,
      "counts":{"structure_probes":len(allres),"pa_identities":len(pa_ids),"pa_details":len(pa_details),"official_download_probes":len(downloads),"evidence_backed_endpoint_candidates":len(endpoint_candidates)},
      "observed_dataflows":flows,"observed_dsds":dsds,"observed_provision_agreements":pas,
      "observed_data_providers":providers,"observed_data_sources":sources,
      "relevant_annotations":annotations,"observed_reference_links":links,
      "official_downloads":downloads,
      "evidence_backed_endpoint_candidates":endpoint_candidates,
      "data_probes":[],"confirmed_data_payloads":[],
      "status_counts":{},
      "results":[asdict(r) for r in allres]
    }
    for r in allres:
        key=f"{r.status}|{r.content_type}"; report["status_counts"][key]=report["status_counts"].get(key,0)+1
    (root/"iea_sdmx_dataflow_discovery_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("IEA MES SDMX PROVISION / DOWNLOAD FORENSICS – targeted v5.0")
    print(f"Structure probes:                 {len(allres)}")
    print(f"Observed Dataflows:               {flows}")
    print(f"Observed DSDs:                    {dsds}")
    print(f"Provision Agreements:             {pas}")
    print(f"Data Providers:                   {providers}")
    print(f"Data sources / endpoint refs:     {sources}")
    print(f"Relevant annotations:              {len(annotations)}")
    print(f"Official download probes:         {len(downloads)}")
    print(f"Evidence-backed endpoint candidates:{len(endpoint_candidates)}")
    print("Blind data probes:                0")
    print(f"Report:                            {root/'iea_sdmx_dataflow_discovery_report.json'}")

if __name__ == "__main__": main()
