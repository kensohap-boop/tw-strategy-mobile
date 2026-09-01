from pathlib import Path
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
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
            "date": datetime.fromtimestamp(ts[i], tz=timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d") if i < len(ts) else "",
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
        "high": round(rows[-1]["high"], 4),
        "low": round(rows[-1]["low"], 4),
        "date": rows[-1].get("date", ""),
        "prev_date": rows[-2].get("date", "") if len(rows) >= 2 else "",
        "prev_close": round(rows[-2]["close"], 4) if len(rows) >= 2 else None,
        "prev_high": round(rows[-2]["high"], 4) if len(rows) >= 2 else None,
        "prev_low": round(rows[-2]["low"], 4) if len(rows) >= 2 else None,
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


GROUP_DEFS = [
    ("A", "① 6/6 全部符合", lambda c: all(c)),
    ("B", "② 5/6・僅放量未過", lambda c: all(c[:5]) and not c[5]),
    ("C", "③ 5/6・僅成交量未過", lambda c: c[0] and c[1] and c[2] and (not c[3]) and c[4] and c[5]),
    ("D", "④ 5/6・僅多頭未過", lambda c: c[0] and c[1] and (not c[2]) and c[3] and c[4] and c[5]),
    ("E", "⑤ 5/6・僅年線未過", lambda c: c[0] and (not c[1]) and c[2] and c[3] and c[4] and c[5]),
    ("F", "⑥ 5/6・僅均線糾結未過", lambda c: (not c[0]) and c[1] and c[2] and c[3] and c[4] and c[5]),
]

def cond_list(rec):
    c = rec.get("conditions") or {}
    return [
        bool(c.get("compact_ma")),
        bool(c.get("above_ma240")),
        bool(c.get("bullish")),
        bool(c.get("liquid_20d_gt_10000")),
        bool(c.get("breakout")),
        bool(c.get("volume_2x")),
    ]

def group_for(rec):
    cs = cond_list(rec)
    for gid, name, test in GROUP_DEFS:
        if test(cs):
            return gid, name
    return None, None

def load_json_file(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def update_signal_tracking(results, market_filter, today):
    path = Path("signals_history.json")
    data = load_json_file(path, {"signals": []})
    signals = data.get("signals") if isinstance(data, dict) else []
    if not isinstance(signals, list):
        signals = []

    # Bootstrap the latest previous daily snapshot if signals_history did not exist yet.
    history_dir = Path("history")
    if history_dir.exists():
        prev_files = sorted([x for x in history_dir.glob("*.json") if x.stem < today])
        if prev_files:
            prev = load_json_file(prev_files[-1], {})
            prev_date = prev.get("date") or prev_files[-1].stem
            prev_stocks = prev.get("stocks") or {}
            existing_prev = {(x.get("code"), x.get("d0_date")) for x in signals}
            prev_filter = prev.get("market_filter") or {}
            for code, rec in prev_stocks.items():
                gid, gname = group_for(rec)
                if not gid or (code, prev_date) in existing_prev:
                    continue
                cur = results.get(code) or {}
                use_prev = cur.get("prev_date") == prev_date
                d0_close = cur.get("prev_close") if use_prev else rec.get("price")
                d0_high = cur.get("prev_high") if use_prev else rec.get("high", rec.get("price"))
                d0_low = cur.get("prev_low") if use_prev else rec.get("low", rec.get("price"))
                signals.append({
                    "code": code, "name": rec.get("name", ""), "market": rec.get("market", ""),
                    "group": gid, "group_name": gname, "d0_date": prev_date,
                    "d0_close": d0_close, "d0_high": d0_high, "d0_low": d0_low,
                    "conditions": rec.get("conditions", {}),
                    "market_filter": prev_filter, "status": "waiting_d1",
                })

    # First update older signals with today's close/high/low.
    for sig in signals:
        code = sig.get("code")
        rec = results.get(code)
        if not rec or sig.get("d0_date") == today:
            continue

        status = sig.get("status")
        if status == "waiting_d1":
            close = float(rec.get("price", 0) or 0)
            ma5 = float(rec.get("ma5", 0) or 0)
            midpoint = (float(sig.get("d0_high", 0)) + float(sig.get("d0_low", 0))) / 2.0
            d0_close = float(sig.get("d0_close", 0) or 0)
            support = close > 0 and ma5 > 0 and d0_close > 0 and close >= ma5 and close >= midpoint and close <= d0_close * 1.05
            sig["d1_checked_date"] = today
            sig["d1_close"] = round(close, 4)
            sig["d1_ma5"] = round(ma5, 4)
            sig["d0_midpoint"] = round(midpoint, 4)
            sig["support_confirmed"] = support
            if support:
                sig["status"] = "tracking"
                sig["support_date"] = today
                sig["support_close"] = round(close, 4)
                sig["tracking"] = []
            else:
                sig["status"] = "rejected_d1"

        elif status == "tracking":
            tracking = sig.setdefault("tracking", [])
            if len(tracking) >= 10 or any(x.get("date") == today for x in tracking):
                if len(tracking) >= 10:
                    sig["status"] = "complete"
                continue
            close = float(rec.get("price", 0) or 0)
            high = float(rec.get("high", close) or close)
            low = float(rec.get("low", close) or close)
            if close > 0:
                tracking.append({"date": today, "close": round(close,4), "high": round(high,4), "low": round(low,4)})
                if len(tracking) >= 10:
                    sig["status"] = "complete"

    # Then add today's new D0 candidates.
    existing = {(x.get("code"), x.get("d0_date")) for x in signals}
    for code, rec in results.items():
        gid, gname = group_for(rec)
        if not gid or (code, today) in existing:
            continue
        signals.append({
            "code": code,
            "name": rec.get("name", ""),
            "market": rec.get("market", ""),
            "group": gid,
            "group_name": gname,
            "d0_date": today,
            "d0_close": rec.get("price"),
            "d0_high": rec.get("high", rec.get("price")),
            "d0_low": rec.get("low", rec.get("price")),
            "conditions": rec.get("conditions", {}),
            "market_filter": market_filter,
            "status": "waiting_d1",
        })

    data = {"updated_at": today, "signals": signals}
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return signals

def build_stats_summary(signals, generated_at):
    names = {gid: name for gid, name, _ in GROUP_DEFS}
    buckets = {gid: [] for gid, _, _ in GROUP_DEFS}
    for sig in signals:
        if not sig.get("support_confirmed"):
            continue
        base = float(sig.get("support_close", 0) or 0)
        if base <= 0:
            continue
        tr = sig.get("tracking") or []
        if not tr:
            continue
        highs = [float(x.get("high", x.get("close", base))) for x in tr]
        lows = [float(x.get("low", x.get("close", base))) for x in tr]
        closes = [float(x.get("close", base)) for x in tr]
        item = {
            "max_up_pct": (max(highs) / base - 1.0) * 100.0,
            "max_down_pct": (min(lows) / base - 1.0) * 100.0,
            "cumulative_pct": (closes[-1] / base - 1.0) * 100.0,
            "days": len(tr),
            "complete": len(tr) >= 10,
        }
        gid = sig.get("group")
        if gid in buckets:
            buckets[gid].append(item)

    groups = []
    for gid, name, _ in GROUP_DEFS:
        arr = buckets[gid]
        if arr:
            avg = lambda k: sum(x[k] for x in arr) / len(arr)
            groups.append({
                "group": gid,
                "name": name,
                "avg_max_up_pct": round(avg("max_up_pct"), 4),
                "avg_max_down_pct": round(avg("max_down_pct"), 4),
                "avg_cumulative_pct": round(avg("cumulative_pct"), 4),
                "samples": len(arr),
                "complete_samples": sum(1 for x in arr if x["complete"]),
            })
        else:
            groups.append({
                "group": gid, "name": name,
                "avg_max_up_pct": None, "avg_max_down_pct": None,
                "avg_cumulative_pct": None, "samples": 0, "complete_samples": 0,
            })
    groups.sort(key=lambda x: (-9999 if x["avg_max_up_pct"] is None else x["avg_max_up_pct"]), reverse=True)
    summary = {
        "generated_at": generated_at,
        "basis": "support_close",
        "window_trading_days": 10,
        "groups": groups,
    }
    Path("stats_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def calc_market_filter():
    """Calculate TAIEX MA filter from Yahoo ^TWII daily history."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?range=2y&interval=1d&events=div%2Csplits"
    data = fetch_json(url)
    result = data["chart"]["result"][0]
    closes = [x for x in result["indicators"]["quote"][0]["close"] if x is not None]
    if len(closes) < 240:
        raise RuntimeError(f"TAIEX insufficient history: {len(closes)}")
    price = closes[-1]
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    ma20 = mean(closes[-20:])
    ma240 = mean(closes[-240:])
    bullish = ma5 > ma10 > ma20
    above240 = price > ma240
    return {
        "price": round(price, 4),
        "ma5": round(ma5, 4),
        "ma10": round(ma10, 4),
        "ma20": round(ma20, 4),
        "ma240": round(ma240, 4),
        "bullish_ma": bullish,
        "above_ma240": above240,
        "filter_on": bullish and above240,
    }

def main():
    universe = get_universe()
    print(f"Universe: {len(universe)}")

    market_filter = calc_market_filter()
    print("Market filter:", json.dumps(market_filter, ensure_ascii=False))

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
        "market_filter": market_filter,
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

    # Keep one permanent full-market snapshot per trading day.
    history_dir = Path("history")
    history_dir.mkdir(exist_ok=True)
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    snapshot = {
        "date": day,
        "generated_at": meta["generated_at"],
        "market_filter": market_filter,
        "stocks": results,
    }
    (history_dir / f"{day}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    signals = update_signal_tracking(results, market_filter, day)
    stats = build_stats_summary(signals, meta["generated_at"])
    print("Signals:", len(signals), "Stats groups:", len(stats.get("groups", [])))

    print(json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
