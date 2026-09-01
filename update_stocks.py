#!/usr/bin/env python3
import concurrent.futures
import datetime as dt
import json
import math
import time
import urllib.parse
import urllib.request

TWSE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d&events=div%2Csplits"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"

def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def clean_num(x):
    if x is None:
        return None
    s = str(x).replace(",", "").replace("--", "").strip()
    try:
        return float(s)
    except Exception:
        return None

def universe():
    out = {}
    # TWSE listed
    for r in get_json(TWSE):
        code = str(r.get("Code", "")).strip()
        name = str(r.get("Name", "")).strip()
        if code and code[0].isdigit():
            out[code] = {"name": name, "suffix": ".TW", "market": "TWSE"}

    # TPEx OTC
    for r in get_json(TPEX):
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        name = str(r.get("CompanyName", "")).strip()
        if code and code[0].isdigit():
            out[code] = {"name": name, "suffix": ".TWO", "market": "TPEx"}

    return out

def avg(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and math.isfinite(x)]
    return sum(xs)/len(xs) if xs else None

def fetch_one(item):
    code, meta = item
    symbol = code + meta["suffix"]
    url = YAHOO.format(symbol=urllib.parse.quote(symbol))

    last_err = None
    for attempt in range(3):
        try:
            body = get_json(url, timeout=25)
            result = (body.get("chart", {}).get("result") or [None])[0]
            if not result:
                raise RuntimeError("no chart result")

            timestamps = result.get("timestamp") or []
            q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            closes = q.get("close") or []
            vols = q.get("volume") or []

            rows = []
            for i, ts in enumerate(timestamps):
                c = clean_num(closes[i] if i < len(closes) else None)
                v = clean_num(vols[i] if i < len(vols) else None)
                if c is None:
                    continue
                rows.append((ts, c, v if v is not None else 0.0))

            if len(rows) < 240:
                raise RuntimeError(f"history too short: {len(rows)}")

            rows = rows[-300:]
            close = [x[1] for x in rows]
            vol_shares = [x[2] for x in rows]

            price = close[-1]
            ma5 = avg(close[-5:])
            ma10 = avg(close[-10:])
            ma20 = avg(close[-20:])
            ma240 = avg(close[-240:])
            volume_lots = vol_shares[-1] / 1000.0
            volume20_avg_lots = avg(vol_shares[-20:]) / 1000.0

            vals = [price, ma5, ma10, ma20]
            ma_converged = ((max(vals) - min(vals)) / min(vals)) <= 0.05
            above_ma240 = price > ma240
            bullish = ma5 > ma10 > ma20
            volume20_gt_10000 = volume20_avg_lots > 10000
            above_all_short = price > ma5 and price > ma10 and price > ma20
            volume_surge_2x = volume_lots > volume20_avg_lots * 2

            trade_date = dt.datetime.fromtimestamp(rows[-1][0], tz=dt.timezone.utc).astimezone(
                dt.timezone(dt.timedelta(hours=8))
            ).strftime("%Y-%m-%d")

            return code, {
                "name": meta["name"],
                "market": meta["market"],
                "price": round(price, 4),
                "ma5": round(ma5, 4),
                "ma10": round(ma10, 4),
                "ma20": round(ma20, 4),
                "ma240": round(ma240, 4),
                "volume_lots": round(volume_lots, 2),
                "volume20_avg_lots": round(volume20_avg_lots, 2),
                "ma_converged": ma_converged,
                "above_ma240": above_ma240,
                "bullish": bullish,
                "volume20_gt_10000": volume20_gt_10000,
                "above_all_short_ma": above_all_short,
                "volume_surge_2x": volume_surge_2x,
                # Backward-compatible aliases used by older UI builds
                "ma240_up": None,
                "volume_gt_1000": None,
                "kd_up": None,
                "revenue_strong": None,
                "updated_at": trade_date,
            }
        except Exception as e:
            last_err = e
            time.sleep(1.2 * (attempt + 1))
    return code, {"_error": str(last_err), "name": meta["name"], "market": meta["market"]}

def main():
    uni = universe()
    print("Universe:", len(uni))

    results = {}
    errors = {}
    # Conservative concurrency to reduce Yahoo throttling.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_one, item) for item in uni.items()]
        for idx, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            code, data = fut.result()
            if "_error" in data:
                errors[code] = data["_error"]
            else:
                results[code] = data
            if idx % 100 == 0:
                print(f"Processed {idx}/{len(futs)}; ok={len(results)} err={len(errors)}")

    payload = results
    with open("stocks.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    meta = {
        "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "universe": len(uni),
        "success": len(results),
        "failed": len(errors),
        "source_universe": "TWSE OpenAPI + TPEx OpenAPI",
        "source_history": "Yahoo Finance chart endpoint (unofficial)",
        "failed_sample": dict(list(errors.items())[:20]),
    }
    with open("stocks_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
