#!/usr/bin/env python3
"""Stage 1: discover MA-consolidation -> >5% breakout events without defining an entry rule.

Reads research_db/daily_YYYY.csv.gz created by build_research_db.py.
Outputs compact JSON only.

Definitions:
- MA convergence = spread among MA5/MA10/MA20 <= threshold (2%,3%,4%,5% tested).
- Breakout D0 = daily close return > 5% AND close > MA5/MA10/MA20.
- For each D0 and each threshold, measure consecutive convergence days ending at D-1.
- Hypothetical entries at D-1..D-10 close; measure first future day whose HIGH reaches +20%.
No D0 information is used as an entry feature; D0 is only the outcome/event label.
"""
from __future__ import annotations
import argparse,csv,gzip,json,math
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path

THRESHOLDS=(0.02,0.03,0.04,0.05)
ENTRY_OFFSETS=tuple(range(1,11))
FORWARD=20

def f(x):
    try:return float(x)
    except:return None

def load(outdir):
    stocks=defaultdict(list); index=[]
    for p in sorted(Path(outdir).glob("daily_*.csv.gz")):
        with gzip.open(p,"rt",encoding="utf-8",newline="") as g:
            for r in csv.DictReader(g):
                row={"date":r["date"],"o":f(r["open"]),"h":f(r["high"]),"l":f(r["low"]),
                     "c":f(r["close"]),"v":f(r["volume_shares"])}
                if None in (row["o"],row["h"],row["l"],row["c"]): continue
                if r["code"]=="^TWII": index.append(row)
                else: stocks[r["code"]].append(row)
    for rows in stocks.values(): rows.sort(key=lambda x:x["date"])
    index.sort(key=lambda x:x["date"])
    return stocks,index

def enrich(rows):
    closes=[]
    for i,r in enumerate(rows):
        closes.append(r["c"])
        for n in (5,10,20):
            r[f"ma{n}"]=sum(closes[-n:])/n if len(closes)>=n else None
        r["ret"]=(r["c"]/rows[i-1]["c"]-1) if i else None
        mas=[r["ma5"],r["ma10"],r["ma20"]]
        r["spread"]=(max(mas)-min(mas))/min(mas) if all(mas) and min(mas)>0 else None

def first_plus20(rows,entry_i):
    entry=rows[entry_i]["c"]; target=entry*1.20
    for j in range(entry_i+1,min(len(rows),entry_i+FORWARD+1)):
        if rows[j]["h"]>=target:return j-entry_i
    return None

def bucket_days(n):
    if n<=0:return "0"
    if n<=2:return str(n)
    if n<=5:return str(n)
    if n<=10:return "6-10"
    if n<=20:return "11-20"
    return "21+"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default="research_db")
    ap.add_argument("--out",default="breakout_stage1.json")
    a=ap.parse_args()
    stocks,index=load(a.db)

    yearly=defaultdict(lambda:defaultdict(lambda:{"breakouts":0,"durations":defaultdict(int)}))
    totals=defaultdict(lambda:{"breakouts":0,"durations":defaultdict(int)})
    speed=defaultdict(lambda:defaultdict(lambda:{"eligible":0,"hit20":0,"days_sum":0,"day_hist":defaultdict(int)}))
    examples=[]
    breakout_dates=set()

    for k,(code,rows) in enumerate(stocks.items(),1):
        enrich(rows)
        for i,r in enumerate(rows):
            if i<21 or r["ret"] is None or r["ret"]<=0.05: continue
            if not all(r["c"]>r[f"ma{n}"] for n in (5,10,20)): continue
            breakout_dates.add((code,r["date"]))
            yr=r["date"][:4]
            # Each threshold is an independent research lens.
            for th in THRESHOLDS:
                n=0; j=i-1
                while j>=0 and rows[j].get("spread") is not None and rows[j]["spread"]<=th:
                    n+=1;j-=1
                # "consolidation breakout" requires D-1 to be converged at this threshold.
                if n==0: continue
                key=f"{int(th*100)}pct"
                totals[key]["breakouts"]+=1
                totals[key]["durations"][bucket_days(n)]+=1
                yearly[yr][key]["breakouts"]+=1
                yearly[yr][key]["durations"][bucket_days(n)]+=1

                for off in ENTRY_OFFSETS:
                    ei=i-off
                    if ei<0: continue
                    sk=speed[key][f"D-{off}"]
                    sk["eligible"]+=1
                    d=first_plus20(rows,ei)
                    if d is not None:
                        sk["hit20"]+=1; sk["days_sum"]+=d; sk["day_hist"][str(d)]+=1
                if len(examples)<1000:
                    examples.append({"code":code,"d0":r["date"],"threshold_pct":int(th*100),
                                     "consecutive_days_to_d1":n,"d0_return_pct":round(r["ret"]*100,4)})
        if k%100==0: print("analyzed",k,"/",len(stocks))

    def norm(x):
        if isinstance(x,defaultdict): x=dict(x)
        if isinstance(x,dict): return {k:norm(v) for k,v in x.items()}
        return x

    s=norm(speed)
    for th,dct in s.items():
        for off,z in dct.items():
            e=z["eligible"]; h=z["hit20"]
            z["hit20_rate_pct"]=round(h/e*100,4) if e else None
            z["avg_days_to_20_among_hits"]=round(z["days_sum"]/h,4) if h else None

    result={
      "schema_version":"2026-09-03.breakout-stage1.1",
      "generated_at":datetime.now(timezone.utc).isoformat(),
      "definitions":{
        "convergence":"MA5/MA10/MA20 spread; thresholds 2%,3%,4%,5%",
        "breakout_d0":"D0 close return >5% and D0 close above MA5/MA10/MA20",
        "consecutive_days":"consecutive convergence days ending D-1",
        "plus20":"from hypothetical entry-day close, first later daily HIGH >= entry*1.20",
        "forward_days":FORWARD,
        "entry_offsets":"D-1 through D-10 are measured; these are benchmarks, not entry rules"
      },
      "stock_count":len(stocks),
      "unique_breakout_events":len(breakout_dates),
      "threshold_summary":norm(totals),
      "yearly_summary":norm(yearly),
      "entry_offset_plus20":s,
      "sample_events_first_1000":examples
    }
    Path(a.out).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print("unique breakout events",len(breakout_dates))
    print("wrote",a.out,round(Path(a.out).stat().st_size/1024/1024,2),"MB")

if __name__=="__main__":
    main()
