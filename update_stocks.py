import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import mean

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
}

def fetch_json(url, timeout=20, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last

def get_universe():
    out = []
    seen = set()

    twse = fetch_json(TWSE_URL)
    for r in twse:
        code = str(r.get("Code", "")).strip()
        name = str(r.get("Name", "")).strip()
        if code.isdigit() and len(code) == 4 and code not in seen:
            seen.add(code)
            out.append((code, name, "TW"))

    tpex = fetch_json(TPEX_URL)
    for r in tpex:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        name = str(r.get("CompanyName", "")).strip()
        if code.isdigit() and len(code) == 4 and code not in seen:
            seen.add(code)
            out.append((code, name, "TWO"))

    return out

def fetch_yahoo_chart(code, market):
    suffix = ".TW" if market == "TW" else ".TWO"
    symbol = code + suffix
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + symbol
        + "?range=2y&interval=1d&includePrePost=false&events=div%2Csplits"
    )
    data = fetch_json(url, timeout=20, retries=2)
    result = (((data or {}).get("chart") or {}).get("result") or [])
    if not result:
        raise RuntimeError("Yahoo result empty")

    r = result[0]
    ts = r.get("timestamp") or []
    quote = (((r.get("indicators") or {}).get("quote") or [{}])[0])
    close = quote.get("close") or []
    high = quote.get("high") or []
    low = quote.get("low") or []
    volume = quote.get("volume") or []

    rows = []
    n = min(len(ts), len(close), len(volume))
    for i in range(n):
        c = close[i]
        v = volume[i]
        if c is None or v is None:
            continue
        rows.append({
            "close": float(c),
            "high": float(high[i]) if i < len(high) and high[i] is not None else float(c),
            "low": float(low[i]) if i < len(low) and low[i] is not None else float(c),
            "volume_lots": float(v) / 1000.0,
        })

    if len(rows) < 240:
        raise RuntimeError(f"insufficient history: {len(rows)}")
    return rows

def calc(code, name, market):
    rows = fetch_yahoo_chart(code, market)
    closes = [r["close"] for r in rows]
    vols = [r["volume_lots"] for r in rows]

    price = closes[-1]
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    ma20 = mean(closes[-20:])
    ma240 = mean(closes[-240:])
    v20 = mean(vols[-20:])
    vt = vols[-1]

    vals = [price, ma5, ma10, ma20]
    compact = ((max(vals) - min(vals)) / min(vals)) <= 0.05
    above240 = price > ma240
    bull = ma5 > ma10 > ma20
    liquid = v20 > 10000
    breakout = price > ma5 and price > ma10 and price > ma20
    volume2x = vt > 2 * v20

    return code, {
        "code": code,
        "name": name,
        "market": "TWSE" if market == "TW" else "TPEx",
        "price": round(price, 4),
        "ma5": round(ma5, 4),
        "ma10": round(ma10, 4),
        "ma20": round(ma20, 4),
        "ma240": round(ma240, 4),
        "volume_lots": round(vt, 2),
        "volume20_avg_lots": round(v20, 2),
        "conditions": {
            "compact_ma": compact,
            "above_ma240": above240,
            "bullish": bull,
            "liquid_20d_gt_10000": liquid,
            "breakout": breakout,
            "volume_2x": volume2x,
        },
    }

def main():
    universe = get_universe()
    print(f"Universe: {len(universe)}")

    results = {}
    errors = []
    ok = 0

    # Reduce concurrency to lower the chance of rate-limiting.
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(calc, *item): item for item in universe}
        for i, fut in enumerate(as_completed(futs), start=1):
            item = futs[fut]
            try:
                code, rec = fut.result()
                results[code] = rec
                ok += 1
            except Exception as e:
                if len(errors) < 50:
                    errors.append({
                        "code": item[0],
                        "market": item[2],
                        "error": str(e),
                    })

            if i % 100 == 0:
                print(f"Processed {i}/{len(universe)}; ok={ok} err={i-ok}")

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "universe": len(universe),
        "ok": ok,
        "errors": len(universe) - ok,
        "sample_errors": errors,
    }

    Path("stocks_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Do not allow a false-green workflow with empty data.
    min_required = max(100, int(len(universe) * 0.20))
    if ok < min_required:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        raise SystemExit(
            f"Too few successful stocks: {ok}/{len(universe)}. "
            "stocks.json was NOT overwritten."
        )

    Path("stocks.json").write_text(
        json.dumps(results, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
