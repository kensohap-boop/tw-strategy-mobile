#!/usr/bin/env python3
"""Build a compact, reusable Taiwan-stock daily OHLCV research database.

- Current TWSE/TPEx universe (survivorship bias remains).
- Yahoo Finance daily chart data, adjusted OHLC using adjclose/close factor.
- Stores RAW daily OHLCV only; indicators are intentionally calculated later.
- Output is split by calendar year and gzip-compressed to keep files small.
- Includes ~420 warm-up days before requested research start in downloads, but
  writes only requested calendar years.
"""
from __future__ import annotations
import argparse, csv, gzip, json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEADERS={"User-Agent":"Mozilla/5.0","Accept":"application/json,text/plain,*/*"}
TWSE_UNIVERSE="https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_UNIVERSE="https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"

def get_json(url, timeout=30, retries=4):
    err=None
    for k in range(retries):
        try:
            req=urllib.request.Request(url,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            err=e; time.sleep(1.2*(k+1))
    raise err

def universe():
    out={}
    try:
        for r in get_json(TWSE_UNIVERSE):
            c=str(r.get("Code") or "").strip()
            n=str(r.get("Name") or "").strip()
            if len(c)==4 and c.isdigit(): out[c]=(n,"TWSE")
    except Exception as e: print("TWSE universe warning:",e)
    try:
        for r in get_json(TPEX_UNIVERSE):
            c=str(r.get("SecuritiesCompanyCode") or r.get("Code") or "").strip()
            n=str(r.get("CompanyName") or r.get("Name") or "").strip()
            if len(c)==4 and c.isdigit(): out[c]=(n,"TPEx")
    except Exception as e: print("TPEx universe warning:",e)
    return out

def yf_symbol(code,market):
    return code + (".TW" if market=="TWSE" else ".TWO")

def fetch_symbol(code,name,market,start_ts,end_ts):
    u=(f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol(code,market)}"
       f"?period1={start_ts}&period2={end_ts}&interval=1d&events=div%2Csplits"
       f"&includeAdjustedClose=true")
    j=get_json(u)
    res=(j.get("chart",{}).get("result") or [None])[0]
    if not res: return code,[]
    ts=res.get("timestamp") or []
    q=((res.get("indicators",{}).get("quote") or [{}])[0])
    adj=((res.get("indicators",{}).get("adjclose") or [{}])[0].get("adjclose") or [])
    rows=[]
    for i,t in enumerate(ts):
        try:
            o=q.get("open",[None]*len(ts))[i]; h=q.get("high",[None]*len(ts))[i]
            l=q.get("low",[None]*len(ts))[i]; c=q.get("close",[None]*len(ts))[i]
            v=q.get("volume",[None]*len(ts))[i]
            if any(x is None for x in (o,h,l,c,v)) or c<=0: continue
            a=adj[i] if i<len(adj) and adj[i] else c
            f=a/c
            rows.append((datetime.fromtimestamp(t,timezone.utc).date().isoformat(),
                         code,name,market,o*f,h*f,l*f,c*f,int(v)))
        except Exception:
            pass
    return code,rows

def fetch_index(start_ts,end_ts):
    u=(f"https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
       f"?period1={start_ts}&period2={end_ts}&interval=1d&includeAdjustedClose=true")
    j=get_json(u); res=(j.get("chart",{}).get("result") or [None])[0]
    if not res: return []
    ts=res.get("timestamp") or []; q=(res.get("indicators",{}).get("quote") or [{}])[0]
    rows=[]
    for i,t in enumerate(ts):
        try:
            o=q["open"][i]; h=q["high"][i]; l=q["low"][i]; c=q["close"][i]; v=q["volume"][i]
            if any(x is None for x in (o,h,l,c)): continue
            rows.append((datetime.fromtimestamp(t,timezone.utc).date().isoformat(),
                         "^TWII","TAIEX","INDEX",o,h,l,c,int(v or 0)))
        except Exception: pass
    return rows

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--years",type=int,default=10)
    ap.add_argument("--workers",type=int,default=12)
    ap.add_argument("--limit",type=int,default=0)
    ap.add_argument("--outdir",default="research_db")
    a=ap.parse_args()

    now=datetime.now(timezone.utc)
    research_start=(now-timedelta(days=365.25*a.years)).date()
    first_year=research_start.year
    # Warm-up is downloaded so later indicators (e.g. MA240) can be computed correctly.
    download_start=datetime(first_year-2,1,1,tzinfo=timezone.utc)
    end=now+timedelta(days=1)
    start_ts=int(download_start.timestamp()); end_ts=int(end.timestamp())

    uni=universe()
    items=list(uni.items())[:a.limit or None]
    print("universe",len(items),"research years",first_year,"-",now.year)

    by_year={y:[] for y in range(first_year,now.year+1)}
    failures=[]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs={ex.submit(fetch_symbol,c,nm,m,start_ts,end_ts):c for c,(nm,m) in items}
        for k,f in enumerate(as_completed(futs),1):
            c=futs[f]
            try:
                _,rows=f.result()
                if not rows: failures.append(c)
                for row in rows:
                    y=int(row[0][:4])
                    if y in by_year and row[0] >= research_start.isoformat():
                        by_year[y].append(row)
            except Exception as e:
                failures.append(c); print("FAIL",c,e)
            if k%100==0: print("done",k,"/",len(items))

    try:
        for row in fetch_index(start_ts,end_ts):
            y=int(row[0][:4])
            if y in by_year and row[0] >= research_start.isoformat():
                by_year[y].append(row)
    except Exception as e:
        print("INDEX FAIL",e)

    outdir=Path(a.outdir); outdir.mkdir(parents=True,exist_ok=True)
    header=["date","code","name","market","open","high","low","close","volume_shares"]
    manifest={"schema_version":"2026-09-03.daily-ohlcv.1",
              "generated_at":now.isoformat(),
              "research_start":research_start.isoformat(),
              "research_end":now.date().isoformat(),
              "years":a.years,
              "universe_count":len(items),
              "failed_symbols":sorted(set(failures)),
              "source":"Yahoo Finance daily chart; current TWSE/TPEx universe",
              "adjustment":"OHLC multiplied by adjclose/close factor; volume is raw shares",
              "limitations":["current-universe survivorship bias","Yahoo data should be validated against official data for important findings"],
              "files":[]}
    for y,rows in sorted(by_year.items()):
        rows.sort(key=lambda x:(x[0],x[1]))
        p=outdir/f"daily_{y}.csv.gz"
        with gzip.open(p,"wt",encoding="utf-8",newline="") as g:
            w=csv.writer(g); w.writerow(header); w.writerows(rows)
        manifest["files"].append({"year":y,"file":p.name,"rows":len(rows),"bytes":p.stat().st_size})
        print(y,len(rows),round(p.stat().st_size/1024/1024,2),"MB")

    mp=outdir/"manifest.json"
    mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print("wrote",mp)

if __name__=="__main__":
    main()
