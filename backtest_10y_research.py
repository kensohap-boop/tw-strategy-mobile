#!/usr/bin/env python3
"""TW Strategy 10-year exploratory backtest.

Purpose: fast historical research baseline. Fetches 10y daily bars from Yahoo Finance for
current TWSE/TPEx universe, adjusts OHLC by Yahoo adjclose factor, applies the user's fixed
mother filter, 64 binary combinations, S1-S5 entries, and exact exit rules.

Important: this is exploratory because the universe is today's universe (survivorship bias).
Forward/live validation remains authoritative. Output makes this limitation explicit.
"""
from __future__ import annotations
import argparse, json, math, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEADERS={"User-Agent":"Mozilla/5.0","Accept":"application/json,text/plain,*/*"}
TWSE_UNIVERSE="https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_UNIVERSE="https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
SCENARIOS={"S1":"D1直接漲停","S2":"D1跌破支撐","S3":"D1站穩支撐","S4":"D1-D2量縮不破支撐","S5":"D1>+5%但未漲停"}
LABELS=["大盤濾網","年線上","多頭排列","20日均量>1萬張","D0量>20日均量2倍","D0收盤漲停"]


def get_json(url, timeout=30, retries=3):
    err=None
    for k in range(retries):
        try:
            req=urllib.request.Request(url,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r)
        except Exception as e:
            err=e; time.sleep(1.2*(k+1))
    raise err


def universe():
    out={}
    try:
        for r in get_json(TWSE_UNIVERSE):
            c=str(r.get("Code") or "").strip(); n=str(r.get("Name") or "").strip()
            if len(c)==4 and c.isdigit(): out[c]=(n,"TWSE")
    except Exception as e: print("TWSE universe warning",e)
    try:
        for r in get_json(TPEX_UNIVERSE):
            c=str(r.get("SecuritiesCompanyCode") or r.get("Code") or "").strip()
            n=str(r.get("CompanyName") or r.get("Name") or "").strip()
            if len(c)==4 and c.isdigit(): out[c]=(n,"TPEx")
    except Exception as e: print("TPEx universe warning",e)
    return out


def yf_symbol(code, market): return code + (".TW" if market=="TWSE" else ".TWO")

def fetch_bars(code, market, years=10):
    end=int(datetime.now(timezone.utc).timestamp())
    start=int((datetime.now(timezone.utc)-timedelta(days=365.25*years+420)).timestamp())
    u=f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol(code,market)}?period1={start}&period2={end}&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
    j=get_json(u); res=(j.get("chart",{}).get("result") or [None])[0]
    if not res:return []
    ts=res.get("timestamp") or []; q=((res.get("indicators",{}).get("quote") or [{}])[0]); adj=((res.get("indicators",{}).get("adjclose") or [{}])[0].get("adjclose") or [])
    rows=[]
    for i,t in enumerate(ts):
        try:
            o,h,l,c,v=[q.get(k,[None]*len(ts))[i] for k in ("open","high","low","close","volume")]
            if not all(x is not None for x in (o,h,l,c,v)) or c<=0: continue
            a=adj[i] if i<len(adj) and adj[i] else c; f=a/c if c else 1
            rows.append({"date":datetime.fromtimestamp(t,timezone.utc).date().isoformat(),"open":o*f,"high":h*f,"low":l*f,"close":c*f,"volume_lots":v/1000.0})
        except Exception: pass
    return rows


def sma(rows,i,n,key="close"):
    if i+1<n:return None
    vals=[rows[j][key] for j in range(i-n+1,i+1)]
    return sum(vals)/n if len(vals)==n else None

def enrich(rows):
    for i,r in enumerate(rows):
        for n in (5,10,20,240):r[f"ma{n}"]=sma(rows,i,n)
        r["vol20"]=sma(rows,i,20,"volume_lots")
        r["prev_close"]=rows[i-1]["close"] if i else None
        r["ret_pct"]=(r["close"]/r["prev_close"]-1)*100 if r["prev_close"] else None
    return rows

def compact(r):
    m=[r.get("ma5"),r.get("ma10"),r.get("ma20")]
    return all(x and x>0 for x in m) and (max(m)-min(m))/min(m)<=.05


COMPACT_THRESHOLDS=(0.02,0.03,0.04,0.05)

def ma_spread_ratio(r):
    m=[r.get("ma5"),r.get("ma10"),r.get("ma20")]
    if not all(x and x>0 for x in m): return None
    return (max(m)-min(m))/min(m)

def compact_days_before_d0(rows,i,threshold,max_days=120):
    days=0
    for j in range(i-1,max(-1,i-max_days-1),-1):
        s=ma_spread_ratio(rows[j])
        if s is None or s>threshold: break
        days+=1
    return days

def compact_volume_features(rows,i,days):
    if days<=0: return {"avg_volume_lots":None,"volume_ratio_vs_prior20":None,"volume_trend_ratio":None}
    w=rows[max(0,i-days):i]
    vols=[x.get("volume_lots") for x in w if x.get("volume_lots") is not None]
    if not vols: return {"avg_volume_lots":None,"volume_ratio_vs_prior20":None,"volume_trend_ratio":None}
    avg=sum(vols)/len(vols)
    prior=rows[max(0,i-days-20):max(0,i-days)]
    pvol=[x.get("volume_lots") for x in prior if x.get("volume_lots") is not None]
    prior_avg=sum(pvol)/len(pvol) if pvol else None
    half=max(1,len(vols)//2)
    first=sum(vols[:half])/len(vols[:half]); second=sum(vols[-half:])/len(vols[-half:])
    return {"avg_volume_lots":avg,
            "volume_ratio_vs_prior20":(avg/prior_avg if prior_avg else None),
            "volume_trend_ratio":(second/first if first else None)}

def compact_research(rows,i):
    out={}
    for t in COMPACT_THRESHOLDS:
        d=compact_days_before_d0(rows,i,t)
        out[str(int(t*100))]={"days":d,**compact_volume_features(rows,i,d)}
    return out

def day_bucket(d):
    if d<=0:return "0天"
    if d<=3:return "1-3天"
    if d<=5:return "4-5天"
    if d<=10:return "6-10天"
    if d<=20:return "11-20天"
    return "21天以上"

def mother(r):
    return compact(r) and (r.get("ret_pct") or 0)>0 and all(r["close"]>=r[k] for k in ("ma5","ma10","ma20") if r.get(k))

def limit_up_close(r):
    p=r.get("prev_close"); c=r.get("close")
    return bool(p and c and c/p>=1.095)  # adjusted-data approximation; flagged in methodology

def market_filter_map(index_rows):
    enrich(index_rows); return {r["date"]: bool(r.get("ma5") and r["close"]>r["ma240"] and r["ma5"]>r["ma10"]>r["ma20"]) for r in index_rows if r.get("ma240")}

def combo(r,mkt):
    bits=[mkt, bool(r.get("ma240") and r["close"]>r["ma240"]), bool(r.get("ma5")>r.get("ma10")>r.get("ma20")), bool(r.get("vol20") and r["vol20"]>10000), bool(r.get("vol20") and r["volume_lots"]>2*r["vol20"]), limit_up_close(r)]
    return ''.join('1' if x else '0' for x in bits),bits

def outcome(rows, entry_i, entry_price):
    future=rows[entry_i+1:entry_i+11]
    if not future:return None
    remain=1.0; realized=0.0; hit20=False; ma5done=False; exits=[]
    for d,x in enumerate(future,1):
        c=x["close"]; h=x["high"]; m5=x.get("ma5") or 0; m10=x.get("ma10") or 0
        if m10 and c<m10 and remain>0:
            q=remain; realized+=q*(c/entry_price-1); exits.append([d,"MA10",q,c]); remain=0; break
        if not hit20 and h>=entry_price*1.20 and remain>0:
            q=remain*.5; realized+=q*.20; remain-=q; hit20=True; exits.append([d,"+20%",q,entry_price*1.2])
        if not ma5done and m5 and c<m5 and remain>0:
            q=remain*.5; realized+=q*(c/entry_price-1); remain-=q; ma5done=True; exits.append([d,"MA5",q,c])
        if d==10 and remain>0:
            q=remain; realized+=q*(c/entry_price-1); exits.append([d,"D10",q,c]); remain=0
    return {"strategy_return_pct":realized*100 if remain==0 else None,"hit20":hit20,"complete":remain==0,"exits":exits}

def process_stock(code,name,market,rows,mktmap,start_date):
    enrich(rows); samples=[]; d0s=[]
    for i,r in enumerate(rows):
        if r["date"]<start_date or i+2>=len(rows) or not mother(r):continue
        cid,bits=combo(r,mktmap.get(r["date"],False)); mid=(r["high"]+r["low"])/2
        research=compact_research(rows,i)
        d0s.append({"code":code,"name":name,"date":r["date"],"close":round(r["close"],4),"combo_id":cid,"compact_research":research})
        d1=rows[i+1]; c1=d1["close"]; entries=[]
        if limit_up_close(d1):entries.append(("S1",i+1,c1))
        if c1<(d1.get("ma5") or -math.inf) or c1<mid:entries.append(("S2",i+1,c1))
        if d1.get("ma5") and c1>=d1["ma5"] and c1>=mid and c1<=r["close"]*1.05:entries.append(("S3",i+1,c1))
        if c1>r["close"]*1.05 and not limit_up_close(d1):entries.append(("S5",i+1,c1))
        if bits[4]:
            for j in (i+1,i+2):
                x=rows[j]
                if x.get("vol20") and x["volume_lots"]<x["vol20"] and x.get("ma5") and x["close"]>=x["ma5"] and x["close"]>=mid:
                    entries.append(("S4",j,x["close"])); break
        for sid,ei,ep in entries:
            o=outcome(rows,ei,ep)
            if o and o["strategy_return_pct"] is not None:
                samples.append({"scenario":sid,"combo_id":cid,"code":code,"name":name,"d0_date":r["date"],"entry_date":rows[ei]["date"],"entry_price":ep,"compact_research":research,**o})
    return samples,d0s

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--years',type=int,default=10); ap.add_argument('--workers',type=int,default=12); ap.add_argument('--limit',type=int,default=0); ap.add_argument('--out',default='backtest_10y.json'); a=ap.parse_args()
    now=datetime.now(timezone.utc); start=(now-timedelta(days=365.25*a.years)).date().isoformat()
    uni=universe(); items=list(uni.items())[:a.limit or None]
    print('universe',len(items),'start',start)
    # TAIEX history via Yahoo for historical market filter.
    idx=fetch_bars('^TWII','INDEX',a.years) if False else []
    try:
        end=int(now.timestamp()); st=int((now-timedelta(days=365.25*a.years+420)).timestamp())
        j=get_json(f'https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?period1={st}&period2={end}&interval=1d&includeAdjustedClose=true')
        res=j['chart']['result'][0]; q=res['indicators']['quote'][0]; ts=res['timestamp']; idx=[]
        for i,t in enumerate(ts):
            c=q['close'][i]; h=q['high'][i]; l=q['low'][i]; o=q['open'][i]; v=q['volume'][i] or 0
            if c is not None: idx.append({'date':datetime.fromtimestamp(t,timezone.utc).date().isoformat(),'open':o or c,'high':h or c,'low':l or c,'close':c,'volume_lots':v/1000})
    except Exception as e: print('index warning',e); idx=[]
    mktmap=market_filter_map(idx) if idx else {}
    all_samples=[]; all_d0=[]; failures=[]
    def job(it):
        code,(name,market)=it; return code,name,market,process_stock(code,name,market,fetch_bars(code,market,a.years),mktmap,start)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        fs={ex.submit(job,it):it[0] for it in items}
        for k,f in enumerate(as_completed(fs),1):
            try:
                code,name,market,(s,d)=f.result(); all_samples+=s; all_d0+=d
            except Exception as e: failures.append({'code':fs[f],'error':str(e)})
            if k%100==0: print('done',k,'samples',len(all_samples),'fail',len(failures))
    buckets={}
    for s in all_samples:buckets.setdefault((s['scenario'],s['combo_id']),[]).append(s)
    rows=[]
    for sid in SCENARIOS:
        for n in range(64):
            cid=f'{n:06b}'; arr=buckets.get((sid,cid),[]); rets=[x['strategy_return_pct'] for x in arr]
            rows.append({'scenario':sid,'scenario_name':SCENARIOS[sid],'combo_id':cid,'combo':[{'name':LABELS[j],'value':cid[j]=='1'} for j in range(6)],'samples':len(rets),'strategy_avg_return_pct':round(sum(rets)/len(rets),4) if rets else None,'strategy_median_return_pct':round(sorted(rets)[len(rets)//2],4) if rets else None,'strategy_profit_rate_pct':round(sum(x>0 for x in rets)/len(rets)*100,2) if rets else None,'hit20_rate_pct':round(sum(x['hit20'] for x in arr)/len(arr)*100,2) if arr else None,'best_trade_pct':round(max(rets),4) if rets else None,'worst_trade_pct':round(min(rets),4) if rets else None})
    ranked=[x for x in rows if x['samples']>=20 and x['strategy_avg_return_pct'] is not None]; ranked.sort(key=lambda x:(-x['strategy_avg_return_pct'],-x['hit20_rate_pct'],-x['samples']))
    yearly={}
    for s in all_samples: yearly.setdefault(s['entry_date'][:4],[]).append(s['strategy_return_pct'])
    compact_stats=[]
    for sid in SCENARIOS:
        sarr=[x for x in all_samples if x["scenario"]==sid]
        for pct in ("2","3","4","5"):
            for bucket in ("0天","1-3天","4-5天","6-10天","11-20天","21天以上"):
                arr=[x for x in sarr if day_bucket(x["compact_research"][pct]["days"])==bucket]
                rets=[x["strategy_return_pct"] for x in arr]
                compact_stats.append({"scenario":sid,"scenario_name":SCENARIOS[sid],
                    "ma_spread_threshold_pct":int(pct),"compact_days_bucket":bucket,"samples":len(arr),
                    "strategy_avg_return_pct":round(sum(rets)/len(rets),4) if rets else None,
                    "strategy_median_return_pct":round(sorted(rets)[len(rets)//2],4) if rets else None,
                    "strategy_profit_rate_pct":round(sum(x>0 for x in rets)/len(rets)*100,2) if rets else None,
                    "hit20_rate_pct":round(sum(x["hit20"] for x in arr)/len(arr)*100,2) if arr else None})
    s1_trades=[{"code":x["code"],"name":x["name"],"d0_date":x["d0_date"],
        "entry_date":x["entry_date"],"entry_price":round(x["entry_price"],4),"combo_id":x["combo_id"],
        "strategy_return_pct":round(x["strategy_return_pct"],4),"hit20":x["hit20"],
        "exits":x["exits"],"compact_research":x["compact_research"]}
        for x in all_samples if x["scenario"]=="S1"]

    result={'schema_version':'2026-09-02.backtest10y.3','generated_at':now.isoformat(),'period':{'start':start,'end':now.date().isoformat(),'years':a.years},'methodology':{'purpose':'exploratory historical baseline; forward daily data remains out-of-sample validation','universe':'current TWSE/TPEx universe; survivorship bias possible','price_source':'Yahoo Finance daily chart, OHLC adjusted by adjclose/close factor','limit_up_note':'historical limit-up close approximated as adjusted close return >=9.5%; validate important rows against official data','mother_filter':'MA5/10/20 spread <=5%; D0 return >0; D0 close above MA5/10/20','compact_research_note':'research only; mother filter is now 5%. Pre-D0 consecutive MA5/10/20 spread <=2/3/4/5%, with volume descriptors.','exit_rules':['intraday high reaches +20%: sell half of current remaining at +20%','close below MA5: sell half of current remaining','close below MA10: sell all remaining','D10: sell all remaining','same-day priority MA10 > +20% > MA5']},'universe_count':len(items),'failed_symbols':failures,'d0_count':len(all_d0),'completed_strategy_samples':len(all_samples),'yearly':[{'year':y,'samples':len(v),'avg_return_pct':round(sum(v)/len(v),4),'profit_rate_pct':round(sum(x>0 for x in v)/len(v)*100,2)} for y,v in sorted(yearly.items())],'combination_stats':rows,'top20':ranked[:20],'compact_duration_stats':compact_stats,'s1_trade_details':s1_trades}
    Path(a.out).write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print('wrote',a.out,'D0',len(all_d0),'samples',len(all_samples),'eligible',len(ranked))
if __name__=='__main__': main()
