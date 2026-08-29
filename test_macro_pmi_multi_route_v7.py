#!/usr/bin/env python3
import csv,json,re,html,sys
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urljoin,urlparse
import requests
try:
 from bs4 import BeautifulSoup
except Exception: BeautifulSoup=None
try:
 import pandas as pd
except Exception: pd=None

OUT=Path("macro_pmi_multi_route_v7"); OUT.mkdir(exist_ok=True)
EVID=OUT/"evidence.jsonl"; SUMMARY=OUT/"summary.json"; MATRIX=OUT/"source_matrix.csv"; REPORT=OUT/"report.txt"
S=requests.Session(); S.headers.update({"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36","Accept-Language":"en-US,en;q=0.9,de;q=0.8"})
records=[]; errors=[]; seen=set()
TARGETS={
"ISM_SERVICES":["PMI","Business Activity","New Orders","New Export Orders","Employment","Prices","Supplier Deliveries","Backlogs","Inventories","Inventory Sentiment","Imports","Exports"],
"ISM_MANUFACTURING":["PMI","New Orders","Production","Employment","Prices","Supplier Deliveries","Backlog of Orders","Inventories","Customers' Inventories","Imports","Exports","New Export Orders"],
"SPG_SERVICES":["Business Activity","New Business","New Export Business","Employment","Backlogs","Input Prices","Prices Charged","Future Activity"],
"TE_SERVICES":["Services PMI"],"TE_COMPOSITE":["Composite PMI"]}
PAGES=[
("TE_SERVICES_US","https://tradingeconomics.com/united-states/services-pmi"),
("TE_SERVICES_US_DE","https://de.tradingeconomics.com/united-states/services-pmi"),
("TE_NON_MANUFACTURING_US","https://tradingeconomics.com/united-states/non-manufacturing-pmi"),
("TE_SERVICES_WORLD","https://tradingeconomics.com/country-list/services-pmi"),
("TE_SERVICES_G20","https://tradingeconomics.com/g20/services-pmi"),
("TE_SERVICES_EUROPE","https://tradingeconomics.com/europe/services-pmi"),
("TE_COMPOSITE_US","https://tradingeconomics.com/united-states/composite-pmi"),
("TE_COMPOSITE_WORLD","https://tradingeconomics.com/country-list/composite-pmi"),
("TE_SERVICES_FORECAST","https://tradingeconomics.com/united-states/services-pmi/forecast"),
("TE_COMPOSITE_FORECAST","https://tradingeconomics.com/united-states/composite-pmi/forecast"),
("SPG_PUBLIC_DE","https://www.pmi.spglobal.com/Public?language=de"),
("SPG_PRESS","https://www.pmi.spglobal.com/Public/Home/PressRelease"),
("ISM_SERVICES","https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/services/"),
("ISM_MANUFACTURING","https://www.ismworld.org/supply-management-news-and-reports/reports/ism-report-on-business/pmi/")]
def rec(**x):
 x["ts_utc"]=datetime.now(timezone.utc).isoformat(); records.append(x)
 with EVID.open("a",encoding="utf-8") as f:f.write(json.dumps(x,ensure_ascii=False,default=str)+"\n")
def safe(label,fn):
 try:return fn()
 except Exception as e:
  errors.append({"label":label,"error":repr(e)}); print("WARNUNG:",label,e); return None
def norm(x): return re.sub(r"\s+"," ",html.unescape(str(x or ""))).strip()
def txt(raw):
 if BeautifulSoup:
  s=BeautifulSoup(raw,"html.parser")
  for z in s(["script","style","noscript"]):z.decompose()
  return norm(s.get_text(" ",strip=True))
 return norm(re.sub("<[^>]+>"," ",raw))
def hits(t):
 low=t.lower(); terms=["actual","previous","forecast","consensus","reference","release","release date","last","value","unit","business activity","new business","new orders","new export","employment","prices","input prices","prices charged","backlogs","future activity","supplier deliveries","inventories","inventory sentiment","imports","exports","production"]
 return {q:len(re.findall(re.escape(q),low)) for q in terms if q in low}
def targethits(t):
 low=t.lower(); return {g:{n:len(re.findall(re.escape(n.lower()),low)) for n in ns} for g,ns in TARGETS.items()}
def get(url,label):
 try:
  r=S.get(url,timeout=(12,30),allow_redirects=True); rec(route="HTTP",label=label,url=url,final_url=r.url,status=r.status_code,content_type=r.headers.get("content-type",""),bytes=len(r.content),redirect=r.url!=url); return r
 except Exception as e: rec(route="HTTP",label=label,url=url,status="ERROR",error=repr(e)); return None
def inspect(label,url):
 if url in seen:return
 seen.add(url); r=get(url,label)
 if not r:return
 raw=r.text[:3000000]; t=txt(raw)
 title=None
 if BeautifulSoup:
  s=BeautifulSoup(raw,"html.parser"); title=s.title.get_text(" ",strip=True) if s.title else None
 rec(route="PAGE_EVIDENCE",label=label,url=url,final_url=r.url,status=r.status_code,title=title,text_preview=t[:12000],dates=re.findall(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b",raw,re.I)[:200],field_hits=hits(raw),target_hits=targethits(t))
 if BeautifulSoup:
  s=BeautifulSoup(raw,"html.parser"); tabs=s.find_all("table")
  rec(route="HTML_TABLES",label=label,url=url,table_count=len(tabs),table_previews=[norm(x.get_text(" ",strip=True))[:3000] for x in tabs[:30]])
  links=[]
  for a in s.find_all("a",href=True):
   u=urljoin(r.url,a["href"])
   if urlparse(u).netloc==urlparse(r.url).netloc:links.append((norm(a.get_text(" ",strip=True)),u))
  rec(route="LINK_INVENTORY",label=label,url=url,internal_links=len(links),links=links[:250])
  attrs=[]
  for z in s.find_all(True):
   for k,v in z.attrs.items():
    if str(k).lower().startswith("data-") or str(k).lower() in {"content","value","datetime","itemprop","name"}:
     v=norm(v if isinstance(v,str) else " ".join(map(str,v)))
     if v and re.search(r"pmi|services|actual|previous|forecast|reference|release|employment|prices|orders|business",f"{k} {v}",re.I):attrs.append({"tag":z.name,"attr":k,"value":v[:1000]})
  rec(route="ATTRIBUTES",label=label,url=url,matches=attrs[:500])
 if pd:
  try:
   dfs=pd.read_html(raw); rec(route="PANDAS_READ_HTML",label=label,url=url,table_count=len(dfs),tables=[{"shape":list(d.shape),"columns":[str(x) for x in d.columns],"rows":d.head(15).to_dict("records")} for d in dfs[:30]])
  except Exception as e:rec(route="PANDAS_READ_HTML",label=label,url=url,status="ERROR",error=repr(e))
 blobs=[]
 if BeautifulSoup:
  for z in BeautifulSoup(raw,"html.parser").find_all("script"):
   q=(z.string or z.get_text()).strip()
   if q.startswith("{") or q.startswith("["):
    try:blobs.append(json.loads(q))
    except Exception:pass
 rec(route="EMBEDDED_JSON",label=label,url=url,blob_count=len(blobs),blobs=blobs[:30])
 eps=sorted(set(re.findall(r'https?://[^"\'<>\s]+|(?:/api|/ajax|/data|/historical|/calendar|/chart|/forecast)[^"\'<>\s]*',raw,re.I)))
 rec(route="ENDPOINT_DISCOVERY",label=label,url=url,candidates=[html.unescape(x).rstrip("),.;") for x in eps[:250]])
 windows=[]
 for term in sorted(set(sum(TARGETS.values(),[])),key=len,reverse=True):
  for m in list(re.finditer(re.escape(term),t,re.I))[:20]:windows.append({"term":term,"window":t[max(0,m.start()-350):m.end()+700]})
 rec(route="TEXT_WINDOWS",label=label,url=url,windows=windows[:500])
 # bounded relevant crawl
 if BeautifulSoup:
  c=[]; s=BeautifulSoup(raw,"html.parser")
  for a in s.find_all("a",href=True):
   u=urljoin(r.url,a["href"]); blob=norm(a.get_text(" ",strip=True))+" "+u
   if urlparse(u).netloc==urlparse(r.url).netloc and re.search(r"pmi|services|service|composite|non-manufacturing|manufacturing|historical|forecast|release|press",blob,re.I):c.append(u)
  for i,u in enumerate(dict.fromkeys(c)):
   if i>=20:break
   safe("CRAWL:"+u,lambda u=u:inspect(label+"_LINK",u))
def finish():
 matrix=[]; alltext=" ".join(str(r.get("text_preview",""))+" "+str(r.get("table_previews",""))+" "+str(r.get("tables",""))+" "+str(r.get("windows","")) for r in records).lower()
 for g,ns in TARGETS.items():
  for n in ns:
   h=len(re.findall(re.escape(n.lower()),alltext)); status="NOT_FOUND" if h==0 else ("PARTIAL" if h<3 else "FOUND")
   matrix.append({"source_group":g,"indicator":n,"evidence_mentions":h,"status":status})
 with MATRIX.open("w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=matrix[0].keys());w.writeheader();w.writerows(matrix)
 summary={"version":"V7","records":len(records),"pages_attempted":len(seen),"errors":len(errors),"actual_inference":"FORBIDDEN","production_file_modified":False,"exit_policy":"0 / non-aborting","collection_finished_unconditionally":True,"matrix":matrix,"errors_detail":errors}
 SUMMARY.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
 lines=["=== V7 MACRO PMI / ISM MULTI-ROUTE EXTRACTION ===",f"RECORDS={len(records)}",f"PAGES_ATTEMPTED={len(seen)}",f"ERRORS={len(errors)}","ACTUAL_INFERENCE=FORBIDDEN","PRODUCTION_FILE_MODIFIED=False","EXIT_POLICY=0","COLLECTION_FINISHED_UNCONDITIONALLY=True","", "MATRIX:"]
 lines += [f'{x["source_group"]} | {x["indicator"]} | {x["status"]} | mentions={x["evidence_mentions"]}' for x in matrix]
 REPORT.write_text("\n".join(lines)+"\n",encoding="utf-8")
def main():
 print("=== V7 MACRO PMI / ISM MULTI-ROUTE EXTRACTION ===")
 print("MODE=READ_ONLY|NON_ABORTING|NO_TE_API")
 for a,b in PAGES:safe("PAGE:"+a,lambda a=a,b=b:inspect(a,b))
 for a,b in [("TE_SERVICES_US","https://tradingeconomics.com/united-states/services-pmi"),("TE_US_COMPOSITE","https://tradingeconomics.com/united-states/composite-pmi"),("SPG_PUBLIC","https://www.pmi.spglobal.com/Public?language=de"),("ISM_HOME","https://www.ismworld.org/")]:
  safe("EXTRA:"+a,lambda a=a,b=b:inspect(a,b))
 finish(); print(f"V7_RECORDS={len(records)}");print(f"V7_ERRORS={len(errors)}");print("V7_RESULT=COLLECTION_COMPLETE");print("V7_PRODUCTION_FILE_MODIFIED=False");print("V7_EXIT_POLICY=0");return 0
if __name__=="__main__":raise SystemExit(main())
