#!/usr/bin/env python3
"""Stage 2: discover pre-breakout signals from D-10..D-1.

This is feature discovery, NOT an entry rule.
Breakout D0: daily close return >5% and close > MA5/MA10/MA20.
Research universe: D-1 MA5/10/20 spread <= 5%.

For every breakout event, compute daily features on D-10..D-1 and label each
pre-breakout day by:
- days_to_breakout
- whether buying that day's close reaches +20% within 3/5/10/20 trading days
- whether D0 itself reaches +20% from that entry

Outputs compact aggregate distributions by relative day and outcome class,
plus bounded row-level samples for later inspection. No entry condition is
hard-coded.
"""
from __future__ import annotations
import argparse,csv,gzip,json,math
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path

LOOKBACK=10
FWD=20

def num(x):
    try:return float(x)
    except:return None

def load(db):
    s=defaultdict(list)
    for p in sorted(Path(db).glob("daily_*.csv.gz")):
        with gzip.open(p,"rt",encoding="utf-8",newline="") as g:
            for r in csv.DictReader(g):
                if r["code"]=="^TWII": continue
                x={"date":r["date"],"o":num(r["open"]),"h":num(r["high"]),"l":num(r["low"]),
                   "c":num(r["close"]),"v":num(r["volume_shares"])}
                if None not in (x["o"],x["h"],x["l"],x["c"],x["v"]): s[r["code"]].append(x)
    for rows in s.values(): rows.sort(key=lambda z:z["date"])
    return s

def mean(a): return sum(a)/len(a) if a else None
def ema(vals,n):
    out=[]; a=2/(n+1); e=None
    for v in vals:
        e=v if e is None else a*v+(1-a)*e
        out.append(e)
    return out

def enrich(r):
    cs=[x["c"] for x in r]; vs=[x["v"] for x in r]
    e12=ema(cs,12); e26=ema(cs,26); dif=[a-b for a,b in zip(e12,e26)]; dea=ema(dif,9)
    # RSI14 Wilder
    ag=al=None; K=D=50.0
    for i,x in enumerate(r):
        for n in (5,10,20,60,240):
            x[f"ma{n}"]=mean(cs[i-n+1:i+1]) if i+1>=n else None
        x["vol20"]=mean(vs[i-19:i+1]) if i>=19 else None
        x["ret"]=(x["c"]/r[i-1]["c"]-1) if i else None
        mas=[x["ma5"],x["ma10"],x["ma20"]]
        x["spread"]=(max(mas)-min(mas))/min(mas) if all(mas) and min(mas)>0 else None
        prev=r[i-1]["c"] if i else x["c"]
        tr=max(x["h"]-x["l"],abs(x["h"]-prev),abs(x["l"]-prev))
        x["tr"]=tr
        x["atr14"]=mean([z["tr"] for z in r[max(0,i-13):i+1]]) if i>=13 else None
        x["atr_pct"]=x["atr14"]/x["c"]*100 if x["atr14"] else None
        if i:
            ch=x["c"]-r[i-1]["c"]; g=max(ch,0); lo=max(-ch,0)
            if i<14:
                gains=[max(r[q]["c"]-r[q-1]["c"],0) for q in range(1,i+1)]
                loss=[max(r[q-1]["c"]-r[q]["c"],0) for q in range(1,i+1)]
                ag=mean(gains); al=mean(loss)
            else:
                ag=(ag*13+g)/14; al=(al*13+lo)/14
            x["rsi14"]=100 if al==0 else 100-100/(1+ag/al)
        else:x["rsi14"]=None
        if i>=8:
            w=r[i-8:i+1]; hh=max(z["h"] for z in w); ll=min(z["l"] for z in w)
            rsv=(x["c"]-ll)/(hh-ll)*100 if hh>ll else 50
            K=K*2/3+rsv/3; D=D*2/3+K/3
            x["k9"]=K;x["d9"]=D
        else:x["k9"]=x["d9"]=None
        x["macd_dif"]=dif[i];x["macd_hist"]=(dif[i]-dea[i])*2
        if i>=19:
            m=mean(cs[i-19:i+1]); sd=(sum((q-m)**2 for q in cs[i-19:i+1])/20)**.5
            x["bb_width_pct"]=(4*sd/m*100) if m else None
        else:x["bb_width_pct"]=None

def feats(r,i):
    x=r[i]; prev=r[i-1] if i else None
    rng=x["h"]-x["l"]
    return {
      "return_pct": x["ret"]*100 if x["ret"] is not None else None,
      "range_pct": (x["h"]-x["l"])/prev["c"]*100 if prev else None,
      "body_pct": (x["c"]-x["o"])/x["o"]*100 if x["o"] else None,
      "close_location_pct": (x["c"]-x["l"])/rng*100 if rng>0 else 50,
      "volume_vs_20d": x["v"]/x["vol20"] if x["vol20"] else None,
      "volume_vs_prev": x["v"]/prev["v"] if prev and prev["v"] else None,
      "ma_spread_pct": x["spread"]*100 if x["spread"] is not None else None,
      "spread_change_pctpt": (x["spread"]-prev["spread"])*100 if prev and x["spread"] is not None and prev["spread"] is not None else None,
      "close_vs_ma5_pct": (x["c"]/x["ma5"]-1)*100 if x["ma5"] else None,
      "atr_pct":x["atr_pct"], "bb_width_pct":x["bb_width_pct"],
      "rsi14":x["rsi14"], "rsi_change":x["rsi14"]-prev["rsi14"] if prev and x["rsi14"] is not None and prev["rsi14"] is not None else None,
      "k9":x["k9"],"d9":x["d9"],
      "kd_gap":x["k9"]-x["d9"] if x["k9"] is not None else None,
      "macd_hist":x["macd_hist"],
      "macd_hist_change":x["macd_hist"]-prev["macd_hist"] if prev else None,
    }

FEATURES=["return_pct","range_pct","body_pct","close_location_pct","volume_vs_20d","volume_vs_prev",
          "ma_spread_pct","spread_change_pctpt","close_vs_ma5_pct","atr_pct","bb_width_pct",
          "rsi14","rsi_change","k9","d9","kd_gap","macd_hist","macd_hist_change"]

def label_hit(r,ei,horizon):
    target=r[ei]["c"]*1.20
    end=min(len(r)-1,ei+horizon)
    for j in range(ei+1,end+1):
        if r[j]["h"]>=target:return j-ei
    return None

def quantiles(a):
    a=sorted(v for v in a if v is not None and math.isfinite(v))
    if not a:return {}
    def q(p):
        k=(len(a)-1)*p; lo=int(k); hi=min(lo+1,len(a)-1); t=k-lo
        return a[lo]*(1-t)+a[hi]*t
    return {"n":len(a),"p10":round(q(.1),4),"p25":round(q(.25),4),"median":round(q(.5),4),
            "p75":round(q(.75),4),"p90":round(q(.9),4),"mean":round(sum(a)/len(a),4)}

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--db",default="research_db");ap.add_argument("--out",default="prebreak_stage2.json")
    a=ap.parse_args(); stocks=load(a.db)
    # relative day -> outcome class -> feature -> values
    vals=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    counts=defaultdict(lambda:defaultdict(int))
    yearly=defaultdict(lambda:defaultdict(int))
    events=0
    for z,(code,r) in enumerate(stocks.items(),1):
        enrich(r)
        for i,x in enumerate(r):
            if i<250 or i+20>=len(r) or x["ret"] is None or x["ret"]<=.05:continue
            if not all(x["c"]>x[f"ma{n}"] for n in (5,10,20)):continue
            if r[i-1]["spread"] is None or r[i-1]["spread"]>.05:continue
            events+=1; yearly[x["date"][:4]]["breakouts"]+=1
            for off in range(LOOKBACK,0,-1):
                ei=i-off; F=feats(r,ei)
                h3=label_hit(r,ei,3);h5=label_hit(r,ei,5);h10=label_hit(r,ei,10);h20=label_hit(r,ei,20)
                # Outcome groups intentionally overlap; useful for discovery.
                groups=["all"]
                if h3 is not None:groups.append("hit20_within_3d")
                if h5 is not None:groups.append("hit20_within_5d")
                if h10 is not None:groups.append("hit20_within_10d")
                if h20 is None:groups.append("no20_within_20d")
                rel=f"D-{off}"
                for g in groups:
                    counts[rel][g]+=1
                    for fn,v in F.items():
                        if v is not None and math.isfinite(v):vals[rel][g][fn].append(v)
        if z%100==0:print("analyzed",z,"/",len(stocks))
    summary={}
    for rel,gd in vals.items():
        summary[rel]={}
        for g,fd in gd.items():
            summary[rel][g]={"count":counts[rel][g],"features":{fn:quantiles(v) for fn,v in fd.items()}}
    out={"schema_version":"2026-09-03.prebreak-stage2.1",
         "generated_at":datetime.now(timezone.utc).isoformat(),
         "purpose":"Discover common D-10..D-1 pre-breakout feature changes; no entry signal hard-coded.",
         "breakout_definition":"D0 return >5%, close > MA5/10/20, D-1 MA5/10/20 spread <=5%",
         "breakout_events":events,
         "features":FEATURES,
         "relative_day_outcome_feature_distributions":summary,
         "yearly_breakout_counts":{y:dict(v) for y,v in yearly.items()}}
    Path(a.out).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
    print("breakout events",events,"wrote",a.out,round(Path(a.out).stat().st_size/1024/1024,2),"MB")
if __name__=="__main__":main()
