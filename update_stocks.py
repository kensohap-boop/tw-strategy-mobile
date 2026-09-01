from pathlib import Path
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import mean

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"}
DEFAULT_CONFIG = {
    "version": "2026-09-02.1",
    "compact_ma_pct": 0.05,
    "min_avg_volume_lots": 10000,
    "volume_multiplier": 2.0,
    "support_max_gap_pct": 0.05,
    "tracking_days": 10,
}

GROUP_NAMES = {
    "A": "① 6/6 全部符合",
    "B": "② 5/6・僅放量未過",
    "C": "③ 5/6・僅成交量未過",
    "D": "④ 5/6・僅多頭未過",
    "E": "⑤ 5/6・僅年線未過",
    "F": "⑥ 5/6・僅均線糾結未過",
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

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    raw = load_json("strategy_config.json", {})
    if isinstance(raw, dict):
        cfg.update(raw)
    cfg["compact_ma_pct"] = float(cfg.get("compact_ma_pct", 0.05))
    cfg["min_avg_volume_lots"] = float(cfg.get("min_avg_volume_lots", 10000))
    cfg["volume_multiplier"] = float(cfg.get("volume_multiplier", 2.0))
    cfg["support_max_gap_pct"] = float(cfg.get("support_max_gap_pct", 0.05))
    cfg["tracking_days"] = int(cfg.get("tracking_days", 10))
    return cfg

def get_universe():
    out, seen = [], set()
    for r in fetch_json(TWSE_URL):
        code = str(r.get("Code", "")).strip()
        name = str(r.get("Name", "")).strip()
        if code.isdigit() and len(code) == 4 and code not in seen:
            seen.add(code)
            out.append((code, name, "TW"))
    for r in fetch_json(TPEX_URL):
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        name = str(r.get("CompanyName", "")).strip()
        if code.isdigit() and len(code) == 4 and code not in seen:
            seen.add(code)
            out.append((code, name, "TWO"))
    return out

def fetch_yahoo_chart(code, market):
    suffix = ".TW" if market == "TW" else ".TWO"
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/" + code + suffix
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
    tz8 = timezone(timedelta(hours=8))
    for i in range(n):
        c, v = close[i], volume[i]
        if c is None or v is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(ts[i], tz=timezone.utc).astimezone(tz8).strftime("%Y-%m-%d"),
            "close": float(c),
            "high": float(high[i]) if i < len(high) and high[i] is not None else float(c),
            "low": float(low[i]) if i < len(low) and low[i] is not None else float(c),
            "volume_lots": float(v) / 1000.0,
        })
    if len(rows) < 240:
        raise RuntimeError(f"insufficient history: {len(rows)}")
    return rows

def evaluate_conditions(rec, cfg):
    try:
        p = float(rec.get("price"))
        m5, m10, m20, m240 = map(float, [rec.get("ma5"), rec.get("ma10"), rec.get("ma20"), rec.get("ma240")])
        v = float(rec.get("volume_lots"))
        v20 = float(rec.get("volume20_avg_lots"))
    except Exception:
        return [False] * 6
    vals = [p, m5, m10, m20]
    compact = min(vals) > 0 and ((max(vals) - min(vals)) / min(vals)) <= cfg["compact_ma_pct"]
    above240 = p > m240
    bullish = m5 > m10 > m20
    liquid = v20 > cfg["min_avg_volume_lots"]
    breakout = p > m5 and p > m10 and p > m20
    surge = v > cfg["volume_multiplier"] * v20
    return [compact, above240, bullish, liquid, breakout, surge]

def group_for(rec, cfg):
    c = evaluate_conditions(rec, cfg)
    if all(c): return "A"
    if all(c[:5]) and not c[5]: return "B"
    if c[0] and c[1] and c[2] and (not c[3]) and c[4] and c[5]: return "C"
    if c[0] and c[1] and (not c[2]) and c[3] and c[4] and c[5]: return "D"
    if c[0] and (not c[1]) and c[2] and c[3] and c[4] and c[5]: return "E"
    if (not c[0]) and c[1] and c[2] and c[3] and c[4] and c[5]: return "F"
    return None

def calc(code, name, market, cfg):
    rows = fetch_yahoo_chart(code, market)
    closes = [r["close"] for r in rows]
    vols = [r["volume_lots"] for r in rows]
    rec = {
        "code": code,
        "name": name,
        "market": "TWSE" if market == "TW" else "TPEx",
        "date": rows[-1]["date"],
        "price": round(closes[-1], 4),
        "high": round(rows[-1]["high"], 4),
        "low": round(rows[-1]["low"], 4),
        "ma5": round(mean(closes[-5:]), 4),
        "ma10": round(mean(closes[-10:]), 4),
        "ma20": round(mean(closes[-20:]), 4),
        "ma240": round(mean(closes[-240:]), 4),
        "volume_lots": round(vols[-1], 2),
        "volume20_avg_lots": round(mean(vols[-20:]), 2),
    }
    cond = evaluate_conditions(rec, cfg)
    rec["conditions"] = {
        "compact_ma": cond[0],
        "above_ma240": cond[1],
        "bullish": cond[2],
        "liquid_20d_gt_min": cond[3],
        "breakout": cond[4],
        "volume_multiplier": cond[5],
    }
    return code, rec

def calc_market_filter():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?range=2y&interval=1d&events=div%2Csplits"
    data = fetch_json(url)
    result = data["chart"]["result"][0]
    closes = [x for x in result["indicators"]["quote"][0]["close"] if x is not None]
    if len(closes) < 240:
        raise RuntimeError(f"TAIEX insufficient history: {len(closes)}")
    price = closes[-1]
    ma5, ma10, ma20, ma240 = mean(closes[-5:]), mean(closes[-10:]), mean(closes[-20:]), mean(closes[-240:])
    bullish, above240 = ma5 > ma10 > ma20, price > ma240
    return {
        "price": round(price, 4), "ma5": round(ma5, 4), "ma10": round(ma10, 4),
        "ma20": round(ma20, 4), "ma240": round(ma240, 4),
        "bullish_ma": bullish, "above_ma240": above240, "filter_on": bullish and above240,
    }

def read_history_snapshots():
    out = []
    h = Path("history")
    if not h.exists():
        return out
    for p in sorted(h.glob("*.json")):
        d = load_json(p, {})
        if isinstance(d, dict) and isinstance(d.get("stocks"), dict):
            out.append(d)
    return out

def rebuild_signals_and_stats(cfg, generated_at):
    """Rebuild all signals from permanent daily snapshots using CURRENT config.
    This makes historical statistics automatically adapt when strategy thresholds change.
    """
    snaps = read_history_snapshots()
    signals = []
    tracking_days = cfg["tracking_days"]
    for i, d0 in enumerate(snaps):
        stocks0 = d0.get("stocks") or {}
        d0_date = d0.get("date") or ""
        for code, rec0 in stocks0.items():
            gid = group_for(rec0, cfg)
            if not gid:
                continue
            sig = {
                "code": code,
                "name": rec0.get("name", ""),
                "market": rec0.get("market", ""),
                "group": gid,
                "group_name": GROUP_NAMES[gid],
                "d0_date": d0_date,
                "d0_close": rec0.get("price"),
                "d0_high": rec0.get("high", rec0.get("price")),
                "d0_low": rec0.get("low", rec0.get("price")),
                "market_filter": d0.get("market_filter") or {},
                "config_version": cfg.get("version", ""),
                "status": "waiting_d1",
            }
            if i + 1 >= len(snaps):
                signals.append(sig)
                continue
            d1 = snaps[i + 1]
            rec1 = (d1.get("stocks") or {}).get(code)
            if not rec1:
                sig["status"] = "missing_d1"
                signals.append(sig)
                continue
            try:
                close1 = float(rec1.get("price", 0) or 0)
                ma5 = float(rec1.get("ma5", 0) or 0)
                d0_close = float(rec0.get("price", 0) or 0)
                d0_high = float(rec0.get("high", d0_close) or d0_close)
                d0_low = float(rec0.get("low", d0_close) or d0_close)
                midpoint = (d0_high + d0_low) / 2.0
                support = close1 > 0 and ma5 > 0 and d0_close > 0 and close1 >= ma5 and close1 >= midpoint and close1 <= d0_close * (1 + cfg["support_max_gap_pct"])
            except Exception:
                support = False
                close1 = ma5 = midpoint = 0
            sig.update({
                "d1_checked_date": d1.get("date", ""),
                "d1_close": round(close1, 4), "d1_ma5": round(ma5, 4), "d0_midpoint": round(midpoint, 4),
                "support_confirmed": support,
            })
            if not support:
                sig["status"] = "rejected_d1"
                signals.append(sig)
                continue
            sig["support_date"] = d1.get("date", "")
            sig["support_close"] = round(close1, 4)
            tracking = []
            for dx in snaps[i + 2:i + 2 + tracking_days]:
                rx = (dx.get("stocks") or {}).get(code)
                if not rx:
                    continue
                try:
                    c = float(rx.get("price", 0) or 0)
                    if c <= 0: continue
                    tracking.append({
                        "date": dx.get("date", ""),
                        "close": round(c, 4),
                        "high": round(float(rx.get("high", c) or c), 4),
                        "low": round(float(rx.get("low", c) or c), 4),
                    })
                except Exception:
                    continue
            sig["tracking"] = tracking
            sig["status"] = "complete" if len(tracking) >= tracking_days else "tracking"
            signals.append(sig)

    Path("signals_history.json").write_text(json.dumps({
        "generated_at": generated_at,
        "config": cfg,
        "signals": signals,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    buckets = {gid: [] for gid in GROUP_NAMES}
    for sig in signals:
        if not sig.get("support_confirmed"):
            continue
        base = float(sig.get("support_close", 0) or 0)
        tr = sig.get("tracking") or []
        if base <= 0 or not tr:
            continue
        highs = [float(x.get("high", x.get("close", base))) for x in tr]
        lows = [float(x.get("low", x.get("close", base))) for x in tr]
        closes = [float(x.get("close", base)) for x in tr]
        buckets[sig["group"]].append({
            "max_up_pct": (max(highs) / base - 1) * 100,
            "max_down_pct": (min(lows) / base - 1) * 100,
            "cumulative_pct": (closes[-1] / base - 1) * 100,
            "days": len(tr),
            "complete": len(tr) >= tracking_days,
        })

    groups = []
    for gid, name in GROUP_NAMES.items():
        arr = buckets[gid]
        if arr:
            av = lambda k: sum(x[k] for x in arr) / len(arr)
            row = {
                "group": gid, "name": name,
                "avg_max_up_pct": round(av("max_up_pct"), 4),
                "avg_max_down_pct": round(av("max_down_pct"), 4),
                "avg_cumulative_pct": round(av("cumulative_pct"), 4),
                "samples": len(arr),
                "complete_samples": sum(1 for x in arr if x["complete"]),
            }
        else:
            row = {"group": gid, "name": name, "avg_max_up_pct": None, "avg_max_down_pct": None, "avg_cumulative_pct": None, "samples": 0, "complete_samples": 0}
        groups.append(row)
    groups.sort(key=lambda x: -9999 if x["avg_max_up_pct"] is None else x["avg_max_up_pct"], reverse=True)
    summary = {
        "generated_at": generated_at,
        "basis": "support_close",
        "window_trading_days": tracking_days,
        "config": cfg,
        "groups": groups,
    }
    Path("stats_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return signals, summary

def main():
    cfg = load_config()
    universe = get_universe()
    print(f"Universe: {len(universe)}")
    print("Strategy config:", json.dumps(cfg, ensure_ascii=False))
    market_filter = calc_market_filter()
    print("Market filter:", json.dumps(market_filter, ensure_ascii=False))

    results, errors, ok = {}, [], 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(calc, *item, cfg): item for item in universe}
        for i, fut in enumerate(as_completed(futs), start=1):
            item = futs[fut]
            try:
                code, rec = fut.result()
                results[code] = rec
                ok += 1
            except Exception as e:
                if len(errors) < 50:
                    errors.append({"code": item[0], "market": item[2], "error": str(e)})
            if i % 100 == 0:
                print(f"Processed {i}/{len(universe)}; ok={ok} err={i-ok}")

    generated_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    meta = {
        "generated_at": generated_at,
        "universe": len(universe), "ok": ok, "errors": len(universe) - ok,
        "sample_errors": errors, "market_filter": market_filter, "strategy_config": cfg,
    }
    Path("stocks_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    min_required = max(100, int(len(universe) * 0.20))
    if ok < min_required:
        raise SystemExit(f"Too few successful stocks: {ok}/{len(universe)}. stocks.json was NOT overwritten.")
    Path("stocks.json").write_text(json.dumps(results, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    h = Path("history"); h.mkdir(exist_ok=True)
    snapshot = {"date": day, "generated_at": generated_at, "market_filter": market_filter, "strategy_config": cfg, "stocks": results}
    (h / f"{day}.json").write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    signals, stats = rebuild_signals_and_stats(cfg, generated_at)
    print("Signals:", len(signals), "Stats groups:", len(stats.get("groups", [])))
    print(json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
