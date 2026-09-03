#!/usr/bin/env python3
"""
Build one reusable Taiwan equity MASTER research database.

Design goal:
- Download once, analyze repeatedly without re-downloading.
- Store raw + adjusted OHLC, Adj Close, volume, symbol metadata.
- Include warm-up history before the 10-year research window.
- Include market index (^TWII) for ex-ante market-state research.
- Manifest freezes data snapshot and coverage.
- Technical indicators are deliberately NOT stored: they are reproducible from OHLCV.

Output: master_db/*.csv.gz + symbols.csv + manifest.json
"""
from __future__ import annotations
import argparse,csv,gzip,json,math,time,urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone,timedelta
from pathlib import Path

UA={"User-Agent":"Mozilla/5.0"}

def get_json(url,tries=5):
    err=None
    for k in range(tries):
        try:
            req=urllib.request.Request(url,headers=UA)
            with urllib.request.urlopen(req,timeout=30) as f:return json.load(f)
        except Exception as e:
            err=e;time.sleep(1.5*(k+1))
    raise err

def current_symbols():
    # Current listed + OTC universe. Keep instrument type/name fields when supplied.
    urls=[
      ("TWSE","https://openapi.twse.com.tw/v1/opendata/t187ap03_L"),
      ("TPEx","https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"),
    ]
    out={}
    for market,url in urls:
        try:
            data=get_json(url)
        except Exception as e:
            print("symbol source failed",market,e);continue
        for r in data:
            code=str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
            if not (len(code)==4 and code.isdigit()): continue
            name=str(r.get("公司簡稱") or r.get("CompanyName") or r.get("公司名稱") or "").strip()
            out[code]={"code":code,"name":name,"market":market,
                       "yahoo":code+(".TW" if market=="TWSE" else ".TWO")}
    return list(out.values())

def yahoo_chart(sym,start,end):
    p1=int(start.replace(tzinfo=timezone.utc).timestamp());p2=int(end.replace(tzinfo=timezone.utc).timestamp())
    u=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
    j=get_json(u);res=j["chart"]["result"]
    if not res:return []
    z=res[0];ts=z.get("timestamp") or [];q=z["indicators"]["quote"][0]
    adj=(z["indicators"].get("adjclose") or [{}])[0].get("adjclose") or [None]*len(ts)
    rows=[]
    for i,t in enumerate(ts):
        vals=[q.get(k,[None]*len(ts))[i] for k in ("open","high","low","close","volume")]
        if any(v is None for v in vals):continue
        o,h,l,c,v=map(float,vals);ac=adj[i]
        if ac is None or c<=0:factor=1.0;ac=c
        else:factor=float(ac)/c
        rows.append({"date":datetime.fromtimestamp(t,timezone.utc).date().isoformat(),
          "raw_open":o,"raw_high":h,"raw_low":l,"raw_close":c,"adj_close":float(ac),
          "adj_open":o*factor,"adj_high":h*factor,"adj_low":l*factor,"adj_close_ohlc":c*factor,
          "volume_shares":int(v),"adjust_factor":factor})
    return rows

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--research-years",type=int,default=10)
    ap.add_argument("--warmup-years",type=int,default=2)
    ap.add_argument("--workers",type=int,default=12)
    ap.add_argument("--outdir",default="master_db")
    a=ap.parse_args();out=Path(a.outdir);out.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc)
    research_start=now-timedelta(days=365.25*a.research_years)
    download_start=research_start-timedelta(days=365.25*a.warmup_years)
    download_end=now+timedelta(days=2)

    syms=current_symbols()
    # Index is stored in the same snapshot for future market-regime analysis.
    syms_all=syms+[{"code":"^TWII","name":"TAIEX","market":"INDEX","yahoo":"^TWII"}]
    failures=[];allrows=[]

    def one(s):
        try:return s,yahoo_chart(s["yahoo"],download_start,download_end),None
        except Exception as e:return s,[],repr(e)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        fut=[ex.submit(one,s) for s in syms_all]
        for n,f in enumerate(as_completed(fut),1):
            s,rows,err=f.result()
            if err or not rows:failures.append({"symbol":s["yahoo"],"error":err or "no rows"})
            else:
                for r in rows:r.update({"code":s["code"],"name":s["name"],"market":s["market"]})
                allrows.extend(rows)
            if n%100==0 or n==len(fut):print(n,"/",len(fut),"failed",len(failures))

    fields=["date","code","name","market","raw_open","raw_high","raw_low","raw_close","adj_close",
            "adj_open","adj_high","adj_low","adj_close_ohlc","volume_shares","adjust_factor"]
    years=sorted(set(r["date"][:4] for r in allrows))
    files=[]
    for y in years:
        rr=[r for r in allrows if r["date"].startswith(y)]
        p=out/f"master_daily_{y}.csv.gz"
        with gzip.open(p,"wt",encoding="utf-8",newline="") as g:
            w=csv.DictWriter(g,fieldnames=fields);w.writeheader();w.writerows(rr)
        files.append({"file":p.name,"rows":len(rr),"bytes":p.stat().st_size})
        print(y,len(rr),round(p.stat().st_size/1024/1024,2),"MB")
    with open(out/"symbols.csv","w",encoding="utf-8",newline="") as g:
        w=csv.DictWriter(g,fieldnames=["code","name","market","yahoo"]);w.writeheader();w.writerows(syms_all)

    manifest={"schema_version":"2026-09-03.master-db.1",
      "created_at_utc":now.isoformat(),"snapshot_end":now.date().isoformat(),
      "research_start":research_start.date().isoformat(),"download_start_for_warmup":download_start.date().isoformat(),
      "research_years":a.research_years,"warmup_years":a.warmup_years,
      "symbol_count":len(syms),"includes_taiex":True,
      "price_fields":{"raw":"raw_open/high/low/close","adjusted":"adj_open/high/low/adj_close_ohlc",
                      "yahoo_adjclose":"adj_close","adjust_factor":"adj_close/raw_close"},
      "technical_indicators":"not stored; derive reproducibly from master OHLCV",
      "known_limitations":[
        "Universe is built from currently listed TWSE/TPEx 4-digit companies; delisted historical securities are not guaranteed by these current-universe APIs.",
        "Yahoo history availability/adjustments are frozen only as of this build snapshot."
      ],
      "files":files,"failed_symbols":failures}
    (out/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print("MASTER DB COMPLETE",len(allrows),"rows; failures",len(failures))

if __name__=="__main__":main()
