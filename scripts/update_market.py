import json, os, sys, time
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import quote

TZ = timezone(timedelta(hours=8))
now = datetime.now(TZ)

# 排程在非交易日/非盤中仍可手動執行；會保留 API 回傳的最新交易資料。
symbols = "tse_t00.tw|tse_00631L.tw"
url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=" + quote(symbols, safe="|_.") + "&json=1&delay=0&_=" + str(int(time.time()*1000))
req = Request(url, headers={
    "User-Agent":"Mozilla/5.0",
    "Accept":"application/json,text/plain,*/*",
    "Referer":"https://mis.twse.com.tw/stock/index.jsp"
})
with urlopen(req, timeout=20) as r:
    data = json.loads(r.read().decode("utf-8"))

arr = data.get("msgArray") or []
if not arr:
    raise RuntimeError("TWSE MIS returned no msgArray")

def value(row):
    for k in ("z","y","o"):
        v = str(row.get(k,"")).replace(",","").strip()
        if v not in ("","-"):
            try: return float(v)
            except: pass
    return None

def stamp(row):
    d=str(row.get("d",""))
    t=str(row.get("t",""))
    if len(d)==8 and t:
        return f"{d[:4]}-{d[4:6]}-{d[6:]} {t}"
    return now.strftime("%Y-%m-%d %H:%M:%S")

idx = next((x for x in arr if str(x.get("c"))=="t00"), None)
etf = next((x for x in arr if str(x.get("c"))=="00631L"), None)
if not idx or not etf:
    raise RuntimeError(f"missing symbol: idx={bool(idx)} etf={bool(etf)}")

out = {
  "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
  "source": "TWSE MIS",
  "taiex": {"price": value(idx), "market_time": stamp(idx)},
  "etf00631L": {"price": value(etf), "market_time": stamp(etf)}
}
if out["taiex"]["price"] is None or out["etf00631L"]["price"] is None:
    raise RuntimeError("price parse failed")

with open("market.json","w",encoding="utf-8") as f:
    json.dump(out,f,ensure_ascii=False,indent=2)
print(json.dumps(out,ensure_ascii=False))
