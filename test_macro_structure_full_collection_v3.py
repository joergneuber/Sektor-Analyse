#!/usr/bin/env python3
from __future__ import annotations
import ast,csv,json,os,re,sys,traceback
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from io import StringIO
from pathlib import Path
from urllib.parse import quote
import pandas as pd
import requests

TARGET=Path("makro_szenario.py")
OUT=Path("macro_structure_test_v3")
OUT.mkdir(parents=True,exist_ok=True)
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 (compatible; NeuberMacroDiscovery/3.0)","Accept-Language":"en-US,en;q=0.9,de;q=0.8"})

TE_URLS=[
 "https://tradingeconomics.com/united-states/non-manufacturing-pmi",
 "https://tradingeconomics.com/united-states/services-pmi",
]
SP_URLS=[
 "https://www.pmi.spglobal.com/Public?language=de",
 "https://www.pmi.spglobal.com/Public?language=en",
 "https://www.pmi.spglobal.com/Public/Release/PressReleases?language=de",
]
TE_FIELDS={
 "ISM Services PMI":["Services PMI","Non Manufacturing PMI","ISM Services PMI"],
 "ISM Services Business Activity":["Business Activity"],
 "ISM Services New Orders":["New Orders"],
 "ISM Services Employment":["Employment"],
 "ISM Services Prices":["Prices"],
 "ISM Services Supplier Deliveries":["Supplier Deliveries"],
 "ISM Services Backlog of Orders":["Backlog of Orders","Backlog"],
 "ISM Services Inventories":["Inventories"],
 "ISM Services Inventory Sentiment":["Inventory Sentiment"],
 "ISM Services Imports":["Imports"],
 "ISM Services Exports":["Exports"],
 "ISM Services New Export Orders":["New Export Orders"],
}
SP_FIELDS={
 "S&P Global Services PMI":["Services PMI"],
 "S&P Global Services Business Activity":["Business Activity"],
 "S&P Global Services New Business":["New Business"],
 "S&P Global Services New Export Business":["New Export Business"],
 "S&P Global Services Employment":["Employment"],
 "S&P Global Services Outstanding Business":["Outstanding Business","Backlog"],
 "S&P Global Services Input Prices":["Input Prices"],
 "S&P Global Services Prices Charged":["Prices Charged","Prices"],
 "S&P Global Services Future Activity":["Future Activity","Business Expectations"],
}

def add(rows,source,field,status,value=None,reference=None,release=None,url=None,method=None,note="",evidence=""):
 rows.append({"source":source,"field":field,"status":status,"value":value,"reference":reference,"release":release,"method":method,"url":url,"evidence":str(evidence)[:2000],"note":note})

def safe(label,fn,rows):
 try: fn()
 except Exception as exc:
  print(f"SAFE_ERROR={label}|{type(exc).__name__}|{exc}")
  add(rows,"TEST",label,"ERROR",note=f"{type(exc).__name__}: {exc}",evidence=traceback.format_exc(limit=4))

def fetch(url,timeout=30):
 try: return S.get(url,timeout=timeout,allow_redirects=True),None
 except Exception as exc: return None,exc

def clean_html(html):
 x=re.sub(r"<script.*?</script>"," ",html,flags=re.I|re.S)
 x=re.sub(r"<style.*?</style>"," ",x,flags=re.I|re.S)
 return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",x)).strip()

def norm(s): return re.sub(r"[^a-z0-9]+"," ",str(s).lower()).strip()

def contexts(text,labels,radius=350):
 out=[]; low=text.casefold()
 for label in labels:
  start=0
  while True:
   pos=low.find(label.casefold(),start)
   if pos<0: break
   out.append(text[max(0,pos-radius):pos+len(label)+radius])
   start=pos+max(1,len(label))
   if len(out)>=10: break
 return out

def table_inventory(html):
 try: tables=pd.read_html(StringIO(html))
 except Exception as exc: return [{"index":-1,"error":f"{type(exc).__name__}: {exc}"}]
 return [{"index":i,"rows":int(df.shape[0]),"columns":int(df.shape[1]),"headers":[str(c) for c in df.columns],"preview":df.head(15).to_string(index=False)[:4000]} for i,df in enumerate(tables)]

def discover_site(rows,source,urls,fields,prefix):
 print(f"=== {source} DEEP DISCOVERY ===")
 for i,url in enumerate(urls,1):
  r,err=fetch(url)
  if err:
   add(rows,source,f"PAGE_{i}","ERROR",url=url,method="GET",note=str(err)); continue
  text=clean_html(r.text)
  (OUT/f"{prefix}_page_{i}.html").write_text(r.text,encoding="utf-8")
  (OUT/f"{prefix}_tables_{i}.json").write_text(json.dumps(table_inventory(r.text),ensure_ascii=False,indent=2),encoding="utf-8")
  print(f"{prefix.upper()}_PAGE={i}|STATUS={r.status_code}|FINAL={r.url}|BYTES={len(r.content)}")
  add(rows,source,f"PAGE_{i}","HTTP_OK" if r.ok else "HTTP_ERROR",url=url,method="GET",note=f"final_url={r.url}|bytes={len(r.content)}",evidence=text[:2000])
  for field,labels in fields.items():
   ctx=contexts(text,labels)
   add(rows,source,field,"LABEL_FOUND" if ctx else "NOT_FOUND",url=url,method="HTML_LABEL_SCAN",evidence="\n---\n".join(ctx[:5]),note="Discovery only; value never inferred.")
   try:
    tables=pd.read_html(StringIO(r.text))
   except Exception: tables=[]
   for ti,df in enumerate(tables):
    d=df.astype(str)
    found=False
    for ri,row in d.iterrows():
     row_text=" | ".join(row.tolist())
     if any(norm(x) in norm(row_text) for x in labels):
      add(rows,source,field,"ROW_FOUND",url=url,method="PANDAS_TABLE_ROW",evidence=row_text,note=f"table_index={ti}|row={int(ri)}; value not guessed")
      found=True; break
    if found: break
  if source=="S&P Global":
   links=re.findall(r'href=["\']([^"\']+)["\']',r.text,re.I)
   scripts=re.findall(r'<script[^>]+src=["\']([^"\']+)["\']',r.text,re.I)
   json_blocks=re.findall(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',r.text,re.I|re.S)
   (OUT/f"{prefix}_assets_{i}.json").write_text(json.dumps({"url":url,"links":links[:500],"scripts":scripts[:500],"application_json_blocks":[x[:12000] for x in json_blocks[:30]]},ensure_ascii=False,indent=2),encoding="utf-8")
   print(f"SP_ASSETS={i}|LINKS={len(links)}|SCRIPTS={len(scripts)}|JSON={len(json_blocks)}")

def load_defs(rows):
 if not TARGET.exists():
  add(rows,"TEST","makro_szenario.py","MISSING"); return {},{}
 try: tree=ast.parse(TARGET.read_text(encoding="utf-8"))
 except Exception as exc:
  add(rows,"TEST","makro_szenario.py","SYNTAX_ERROR",note=str(exc)); return {},{}
 fred={}; market={}
 for node in tree.body:
  if isinstance(node,ast.Assign):
   for target in node.targets:
    if isinstance(target,ast.Name) and target.id in {"FRED_SERIES","MARKET_DATA"}:
     try:
      value=ast.literal_eval(node.value)
      if target.id=="FRED_SERIES" and isinstance(value,dict): fred=value
      if target.id=="MARKET_DATA" and isinstance(value,dict): market=value
     except Exception as exc: add(rows,"TEST",target.id,"DEFINITION_PARSE_ERROR",note=str(exc))
 print(f"FRED_DEFINITIONS={len(fred)}|MARKET_DEFINITIONS={len(market)}")
 return fred,market

def collect_fred(rows,defs):
 print(f"=== FRED DEEP TEST: {len(defs)} SERIES ===")
 key=os.environ.get("FRED_API_KEY"); print(f"FRED_API_KEY_PRESENT={'YES' if key else 'NO'}")
 if not key:
  for n,sid in defs.items(): add(rows,"FRED",n,"CONFIG_MISSING",note=f"series={sid}")
  return
 def one(name,sid):
  try:
   p={"api_key":key,"file_type":"json","series_id":sid,"sort_order":"desc","limit":5}
   r=S.get("https://api.stlouisfed.org/fred/series/observations",params=p,timeout=20)
   if r.status_code!=200: return name,sid,f"HTTP_{r.status_code}",None,None,r.text[:500]
   obs=r.json().get("observations",[]); o=obs[0] if obs else {}
   return name,sid,"REAL_API" if o else "NO_DATA",o.get("value"),o.get("date"),f"observations={len(obs)}"
  except Exception as exc: return name,sid,"ERROR",None,None,f"{type(exc).__name__}: {exc}"
 with ThreadPoolExecutor(max_workers=min(12,max(1,len(defs)))) as pool:
  fs=[pool.submit(one,n,s) for n,s in defs.items()]
  for f in as_completed(fs):
   n,s,status,val,ref,note=f.result()
   print(f"FRED_RESULT={n}|SERIES={s}|STATUS={status}|VALUE={val}|DATE={ref}")
   add(rows,"FRED",n,status,val,ref,method="FRED_API",note=f"series={s}|{note}")

def probe_fred_metadata(rows,defs):
 key=os.environ.get("FRED_API_KEY")
 if not key: return
 print("=== FRED METADATA ===")
 for n,sid in defs.items():
  try:
   r=S.get("https://api.stlouisfed.org/fred/series",params={"api_key":key,"file_type":"json","series_id":sid},timeout=20)
   if r.ok:
    m=(r.json().get("seriess") or [{}])[0]
    meta={k:m.get(k) for k in ("title","frequency","units","seasonal_adjustment","observation_start","observation_end","last_updated")}
    add(rows,"FRED",n+" [metadata]","REAL_API",method="FRED_SERIES_METADATA",note=json.dumps(meta,ensure_ascii=False))
   else: add(rows,"FRED",n+" [metadata]",f"HTTP_{r.status_code}",method="FRED_SERIES_METADATA",note=r.text[:500])
  except Exception as exc: add(rows,"FRED",n+" [metadata]","ERROR",note=str(exc))

def inspect_market(rows,defs):
 print(f"=== MARKET DEFINITIONS: {len(defs)} ===")
 for n,v in defs.items(): add(rows,"MARKET_DEFINITION",n,"DEFINED",value=str(v),method="AST_READ_ONLY")

def write_reports(rows):
 json.dump(rows,(OUT/"results.json").open("w",encoding="utf-8"),ensure_ascii=False,indent=2)
 with (OUT/"results.csv").open("w",newline="",encoding="utf-8") as f:
  fields=list(rows[0]) if rows else ["source","field","status"]
  w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
 counts={}
 for r in rows: counts[r["status"]]=counts.get(r["status"],0)+1
 lines=["=== MACRO STRUCTURE DISCOVERY V3 ===",f"GENERATED_UTC={datetime.now(timezone.utc).isoformat()}",f"RECORDS={len(rows)}","NON_ABORTING=True","PRODUCTION_FILE_MODIFIED=False","PREVIOUS_AS_ACTUAL=FORBIDDEN","FORECAST_AS_ACTUAL=FORBIDDEN","","STATUS_COUNTS:"]+[f"{k}={v}" for k,v in sorted(counts.items())]
 (OUT/"summary.txt").write_text("\n".join(lines),encoding="utf-8"); print("\n".join(lines))

def main():
 print("=== MACRO STRUCTURE FULL DISCOVERY V3 ===")
 print("PURPOSE=MAXIMUM_INFORMATION_PER_RUN")
 print("POLICY=NON_ABORTING|READ_ONLY|NO_GUESSED_ACTUALS")
 rows=[]
 if TARGET.exists():
  try: ast.parse(TARGET.read_text(encoding="utf-8")); print("TARGET_SYNTAX=GREEN")
  except Exception as exc: add(rows,"TEST","makro_szenario.py","SYNTAX_ERROR",note=str(exc))
 fred,market=load_defs(rows)
 safe("TE_DEEP_DISCOVERY",lambda:discover_site(rows,"TradingEconomics",TE_URLS,TE_FIELDS,"te"),rows)
 safe("SP_DEEP_DISCOVERY",lambda:discover_site(rows,"S&P Global",SP_URLS,SP_FIELDS,"sp"),rows)
 safe("FRED_API_COLLECTION",lambda:collect_fred(rows,fred),rows)
 safe("FRED_METADATA_COLLECTION",lambda:probe_fred_metadata(rows,fred),rows)
 safe("MARKET_DEFINITION_COLLECTION",lambda:inspect_market(rows,market),rows)
 safe("REPORT_GENERATION",lambda:write_reports(rows),rows)
 if not (OUT/"results.json").exists():
  try: write_reports(rows)
  except Exception as exc: print(f"REPORT_FALLBACK_ERROR={type(exc).__name__}|{exc}")
 print("RESULT=COLLECTION_COMPLETE"); print("EXIT_POLICY=0"); return 0

if __name__=="__main__": sys.exit(main())
