#!/usr/bin/env python3
"""One-shot forensic discovery of official IEA MES/.Stat/SDMX access paths."""
from __future__ import annotations
import argparse, hashlib, io, json, re, time, zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
PRODUCT_URL='https://www.iea.org/data-and-statistics/data-product/monthly-electricity-statistics'
TOOLS_URL='https://www.iea.org/data-and-statistics/data-tools/monthly-electricity-statistics'
SERVICE_ROOTS=('https://sis-cc-api-stable.iea.org/','https://sis-cc-nsi-stable.iea.org/')
KEYWORDS=('.stat','sdmx','dataflow','datastructure','/data/','/rest/','csv','zip','json','xml','monthly electricity statistics','mesgen','mesbal','generation','balance','access','explorer')
MARKERS=('monthly electricity statistics','mesgen','mesbal','electricity statistics','energy_balance_flow','energy_product')
def relevant(s): return any(k in s.lower() for k in KEYWORDS)
def mes_score(s): return sum(k in s.lower() for k in MARKERS)
def digest(b): return hashlib.sha256(b).hexdigest()
def fname(url,suffix='.bin'):
    base=Path(urlparse(url).path).name or 'payload'; base=re.sub(r'[^A-Za-z0-9._-]+','_',base)
    if '.' not in base: base+=suffix
    return f'{digest(url.encode())[:12]}_{base}'
def html_urls(text,base):
    found=set(re.findall(r'''(?:href|src|action)\s*=\s*["']([^"']+)["']''',text,re.I))
    found={urljoin(base,x) for x in found}
    found.update(x.rstrip('),;') for x in re.findall(r'https?://[^"\'<>\\\s]+',text))
    return sorted(x for x in found if relevant(x))
def payload(data,ctype,url,out):
    r={'url':url,'content_type':ctype,'size':len(data),'sha256':digest(data)}; text=data[:2000000].decode('utf-8','ignore'); r['mes_score']=mes_score(text)
    r['looks_sdmx']=any(x in text.lower() for x in ('sdmx','obs_value','time_period','energy_balance_flow','structure','energy_product'))
    if data[:2]==b'PK' or 'zip' in ctype.lower() or url.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names=z.namelist(); r['zip_members']=names[:200]; r['zip_mes_members']=[n for n in names if mes_score(n)]
                target=out/f"payload_{r['sha256'][:12]}.zip"; target.write_bytes(data); r['saved']=str(target)
                r['text_members']=[{'name':n,'size':len(z.read(n)),'mes_score':mes_score(z.read(n)[:2000000].decode('utf-8','ignore'))} for n in names if n.lower().endswith(('.csv','.xml','.json','.txt'))][:200]
        except zipfile.BadZipFile: r['zip_error']='invalid ZIP'
    return r
def getpage(s,url,out):
    r={'url':url}
    try:
        x=s.get(url,timeout=30,allow_redirects=True); r.update(status=x.status_code,final_url=x.url,content_type=x.headers.get('content-type',''),history=[{'status':h.status_code,'url':h.url} for h in x.history])
        if x.ok:
            r['mes_score']=mes_score(x.text); r['candidate_urls']=html_urls(x.text,x.url); (out/fname(x.url,'.html')).write_text(x.text,encoding='utf-8',errors='replace')
    except Exception as e: r['error']=f'{type(e).__name__}: {e}'
    return r
def probes(s,out):
    result=[]
    for root in SERVICE_ROOTS:
        for path in ('','rest/','rest/dataflow','rest/v1/dataflow','rest/v2/dataflow','SdmxRegistryService'):
            u=urljoin(root,path); item={'url':u}
            try:
                x=s.get(u,timeout=20,allow_redirects=True); item.update(status=x.status_code,final_url=x.url,content_type=x.headers.get('content-type',''))
                if x.ok and x.content: item['payload']=payload(x.content,x.headers.get('content-type',''),x.url,out)
            except Exception as e: item['error']=f'{type(e).__name__}: {e}'
            result.append(item)
    return result
def browser(urls,out,headed,wait):
    try: from playwright.sync_api import sync_playwright
    except ImportError: return {'available':False,'error':'Install Playwright: pip install playwright && playwright install chromium'}
    r={'available':True,'pages':[],'requests':[],'responses':[],'downloads':[]}
    with sync_playwright() as p:
        b=p.chromium.launch(headless=not headed); c=b.new_context(accept_downloads=True); page=c.new_page()
        page.on('request',lambda q:r['requests'].append({'method':q.method,'url':q.url,'resource_type':q.resource_type,'headers':q.headers,'post_data':q.post_data,'relevant':relevant(q.url)}))
        def resp(q):
            item={'status':q.status,'url':q.url,'headers':q.headers,'relevant':relevant(q.url)}
            if item['relevant']:
                try:item['payload']=payload(q.body(),q.headers.get('content-type',''),q.url,out)
                except Exception as e:item['body_error']=str(e)
            r['responses'].append(item)
        page.on('response',resp)
        def dl(d):
            try:
                t=out/fname(d.url,'.download'); d.save_as(t); r['downloads'].append({'url':d.url,'suggested_filename':d.suggested_filename,'path':str(t)})
            except Exception as e:r['downloads'].append({'url':d.url,'error':str(e)})
        page.on('download',dl)
        for start in urls:
            e={'start_url':start}
            try:
                page.goto(start,wait_until='domcontentloaded',timeout=120000); page.wait_for_timeout(wait*1000); e.update(final_url=page.url,title=page.title(),frames=[f.url for f in page.frames])
                links=page.locator('a').evaluate_all("""els=>els.map(a=>({text:(a.innerText||'').trim(),href:a.href||'',target:a.target||''}))""")
                e['relevant_links']=[x for x in links if x.get('href') and (relevant(x['href']) or relevant(x.get('text','')))]
                cand=[x for x in links if re.search(r'\.stat|access|data\s*set|explorer',f"{x.get('text','')} {x.get('href','')}",re.I)]
                e['access_candidates']=cand
                for x in cand[:20]:
                    u=x.get('href','')
                    if u.startswith(('http://','https://')):
                        try: page.goto(u,wait_until='domcontentloaded',timeout=60000); page.wait_for_timeout(wait*1000); e.setdefault('visited_candidates',[]).append({'requested':u,'final_url':page.url,'title':page.title()})
                        except Exception as ex:e.setdefault('visited_candidates',[]).append({'requested':u,'error':str(ex)})
            except Exception as ex:e['error']=f'{type(ex).__name__}: {ex}'
            r['pages'].append(e)
        c.close(); b.close()
    return r
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--headed',action='store_true'); ap.add_argument('--wait',type=int,default=20); ap.add_argument('--output',default='iea_forensic_discovery'); a=ap.parse_args(); out=Path(a.output); out.mkdir(parents=True,exist_ok=True)
    s=requests.Session(); s.headers.update({'User-Agent':'Mozilla/5.0 Chrome/131 Safari/537.36','Accept':'*/*'})
    report={'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'product_page':getpage(s,PRODUCT_URL,out),'data_tools_page':getpage(s,TOOLS_URL,out),'service_probes':probes(s,out)}; report['browser']=browser([PRODUCT_URL,TOOLS_URL],out,a.headed,a.wait)
    cand=set(report['product_page'].get('candidate_urls',[])+report['data_tools_page'].get('candidate_urls',[]))
    for p in report['browser'].get('pages',[]): cand.update(x.get('href','') for x in p.get('relevant_links',[]))
    report['candidate_urls']=sorted(x for x in cand if x.startswith(('http://','https://'))); report['summary']={'candidate_count':len(report['candidate_urls']),'relevant_requests':sum(x.get('relevant',False) for x in report['browser'].get('requests',[])),'relevant_responses':sum(x.get('relevant',False) for x in report['browser'].get('responses',[])),'downloads':len(report['browser'].get('downloads',[]))}
    p=out/'iea_forensic_report.json'; p.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8'); print(json.dumps(report['summary'],indent=2)); print('Report:',p); return 0
if __name__=='__main__': raise SystemExit(main())
