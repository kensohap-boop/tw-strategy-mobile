from pathlib import Path
import argparse
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
    "version": "2026-09-02.7",
    "compact_ma_pct": 0.03,
    "min_avg_volume_lots": 10000,
    "volume_multiplier": 2.0,
    "support_max_gap_pct": 0.05,
    "tracking_days": 12,
    "record_through_d": 13,
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
    cfg["compact_ma_pct"] = 0.03  # 固定：只計算 MA5/MA10/MA20 三條均線的 3% 糾結
    cfg["min_avg_volume_lots"] = float(cfg.get("min_avg_volume_lots", 10000))
    cfg["volume_multiplier"] = float(cfg.get("volume_multiplier", 2.0))
    cfg["support_max_gap_pct"] = float(cfg.get("support_max_gap_pct", 0.05))
    cfg["tracking_days"] = int(cfg.get("tracking_days", 12))
    cfg["record_through_d"] = int(cfg.get("record_through_d", 13))
    return cfg

def get_universe():
    out, seen = [], set()
    for r in fetch_json(TWSE_URL):
        code = str(r.get("Code", "")).strip()
        name = str(r.get("Name", "")).strip()
        # Individual-stock strategy: exclude ETF / fund-style 00xx codes.
        if code.isdigit() and len(code) == 4 and not code.startswith("00") and code not in seen:
            seen.add(code)
            out.append((code, name, "TW"))
    for r in fetch_json(TPEX_URL):
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        name = str(r.get("CompanyName", "")).strip()
        # Individual-stock strategy: exclude ETF / fund-style 00xx codes.
        if code.isdigit() and len(code) == 4 and not code.startswith("00") and code not in seen:
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
    ma_vals = [m5, m10, m20]
    # 均線糾結只看 MA5 / MA10 / MA20 彼此距離；突破後股價可高於糾結區，不受 3% 限制。
    compact = min(ma_vals) > 0 and ((max(ma_vals) - min(ma_vals)) / min(ma_vals)) <= cfg["compact_ma_pct"]
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

def tw_tick(price):
    """Taiwan equity price tick size."""
    p = float(price)
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.10
    if p < 500: return 0.50
    if p < 1000: return 1.00
    return 5.00

def tw_limit_up_price(prev_close):
    """Approximate TWSE/TPEx ordinary-stock +10% daily limit using exchange tick grid."""
    from decimal import Decimal, ROUND_FLOOR
    p = Decimal(str(prev_close))
    raw = p * Decimal("1.10")
    tick = Decimal(str(tw_tick(float(raw))))
    return float((raw / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)

def calc(code, name, market, cfg):
    rows = fetch_yahoo_chart(code, market)
    closes = [r["close"] for r in rows]
    vols = [r["volume_lots"] for r in rows]
    prev_close = float(closes[-2])
    close_now = float(closes[-1])
    high_now = float(rows[-1]["high"])
    limit_up_price = tw_limit_up_price(prev_close)
    eps = 1e-9
    rec = {
        "code": code,
        "name": name,
        "market": "TWSE" if market == "TW" else "TPEx",
        "date": rows[-1]["date"],
        "price": round(close_now, 4),
        "high": round(high_now, 4),
        "low": round(rows[-1]["low"], 4),
        "prev_close": round(prev_close, 4),
        "daily_return_pct": round((close_now / prev_close - 1) * 100, 4) if prev_close > 0 else None,
        "intraday_high_return_pct": round((high_now / prev_close - 1) * 100, 4) if prev_close > 0 else None,
        "limit_up_price": round(limit_up_price, 4),
        "limit_up_close": bool(close_now + eps >= limit_up_price),
        "limit_up_touched": bool(high_now + eps >= limit_up_price),
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
    """Build D0 signals from the fixed mother filter, then analyze 64 boolean
    combinations across four post-breakout entry scenarios."""
    snaps = read_history_snapshots()
    signals = []
    record_through_d = int(cfg.get("record_through_d", 13))

    def mother_filter(rec):
        c = evaluate_conditions(rec, cfg)
        daily_ret = rec.get("daily_return_pct")
        if daily_ret is None:
            try:
                px = float(rec.get("price", 0) or 0)
                prev = float(rec.get("prev_close", 0) or 0)
                daily_ret = (px / prev - 1) * 100 if px > 0 and prev > 0 else None
            except Exception:
                daily_ret = None
        # D0 必須是上漲日；下跌剛好落入均線糾結區不算突破。
        return bool(c[0] and c[4] and daily_ret is not None and float(daily_ret) > 0)

    def combo_bits(rec, market_filter):
        cond = rec.get("conditions") or {}
        return [
            bool((market_filter or {}).get("filter_on", False)),
            bool(cond.get("above_ma240", False)),
            bool(cond.get("bullish", False)),
            bool(cond.get("liquid_20d_gt_min", False)),
            bool(cond.get("volume_multiplier", False)),
            bool(rec.get("limit_up_close", False)),
        ]

    def combo_id(bits):
        return "".join("1" if x else "0" for x in bits)

    for i, d0 in enumerate(snaps):
        stocks0 = d0.get("stocks") or {}
        d0_date = d0.get("date") or ""
        for code, rec0 in stocks0.items():
            if not mother_filter(rec0):
                continue

            bits = combo_bits(rec0, d0.get("market_filter") or {})
            gid = group_for(rec0, cfg)
            sig = {
                "code": code, "name": rec0.get("name", ""), "market": rec0.get("market", ""),
                "group": gid, "group_name": GROUP_NAMES.get(gid, "母篩選"),
                "combo_id": combo_id(bits), "combo_bits": bits,
                "d0_date": d0_date, "d0_close": rec0.get("price"),
                "d0_high": rec0.get("high", rec0.get("price")),
                "d0_low": rec0.get("low", rec0.get("price")),
                "d0_prev_close": rec0.get("prev_close"),
                "d0_daily_return_pct": rec0.get("daily_return_pct"),
                "d0_intraday_high_return_pct": rec0.get("intraday_high_return_pct"),
                "d0_limit_up_price": rec0.get("limit_up_price"),
                "d0_limit_up_close": bool(rec0.get("limit_up_close", False)),
                "d0_limit_up_touched": bool(rec0.get("limit_up_touched", False)),
                "d0_volume_lots": rec0.get("volume_lots"),
                "d0_volume20_avg_lots": rec0.get("volume20_avg_lots"),
                "market_filter": d0.get("market_filter") or {},
                "config_version": cfg.get("version", ""),
                "status": "waiting_d1",
            }

            # Permanent raw D0-D13 path.
            path = []
            d0_vol = float(rec0.get("volume_lots", 0) or 0)
            d0_close = float(rec0.get("price", 0) or 0)
            d0_high = float(rec0.get("high", d0_close) or d0_close)
            d0_low = float(rec0.get("low", d0_close) or d0_close)
            midpoint = (d0_high + d0_low) / 2 if d0_high > 0 and d0_low > 0 else 0
            for day_no, snap in enumerate(snaps[i:i + record_through_d + 1]):
                rx = (snap.get("stocks") or {}).get(code)
                if not rx:
                    continue
                try:
                    cx = float(rx.get("price", 0) or 0)
                    hx = float(rx.get("high", cx) or cx)
                    lx = float(rx.get("low", cx) or cx)
                    vx = float(rx.get("volume_lots", 0) or 0)
                    if cx <= 0: continue
                    path.append({
                        "d": day_no, "date": snap.get("date", ""), "close": round(cx, 4),
                        "high": round(hx, 4), "low": round(lx, 4),
                        "prev_close": rx.get("prev_close"),
                        "daily_return_pct": rx.get("daily_return_pct"),
                        "intraday_high_return_pct": rx.get("intraday_high_return_pct"),
                        "ma5": rx.get("ma5"), "ma10": rx.get("ma10"), "ma20": rx.get("ma20"), "ma240": rx.get("ma240"),
                        "volume_lots": rx.get("volume_lots"), "volume20_avg_lots": rx.get("volume20_avg_lots"),
                        "volume_ratio_vs_d0": round(vx / d0_vol, 4) if d0_vol > 0 else None,
                        "volume_contract_vs_d0": bool(d0_vol > 0 and vx < d0_vol),
                        "close_vs_d0_pct": round((cx / d0_close - 1) * 100, 4) if d0_close > 0 else None,
                        "low_above_d0_low": bool(d0_low > 0 and lx >= d0_low),
                        "close_above_d0_midpoint": bool(midpoint > 0 and cx >= midpoint),
                        "limit_up_price": rx.get("limit_up_price"),
                        "limit_up_close": bool(rx.get("limit_up_close", False)),
                        "limit_up_touched": bool(rx.get("limit_up_touched", False)),
                        "conditions": rx.get("conditions") or {},
                    })
                except Exception:
                    continue
            sig["daily_path"] = path
            sig["record_through_d"] = record_through_d

            if i + 1 < len(snaps):
                rec1 = (snaps[i + 1].get("stocks") or {}).get(code)
                if rec1:
                    close1 = float(rec1.get("price", 0) or 0)
                    ma5 = float(rec1.get("ma5", 0) or 0)
                    support = close1 > 0 and ma5 > 0 and close1 >= ma5 and close1 >= midpoint and close1 <= d0_close * (1 + cfg["support_max_gap_pct"])
                    sig.update({
                        "d1_checked_date": snaps[i + 1].get("date", ""),
                        "d1_close": round(close1, 4), "d1_ma5": round(ma5, 4),
                        "d0_midpoint": round(midpoint, 4), "support_confirmed": support,
                        "d1_change_vs_d0_pct": round((close1 / d0_close - 1) * 100, 4) if d0_close > 0 else None,
                        "d1_too_high": bool(d0_close > 0 and close1 > d0_close * (1 + cfg["support_max_gap_pct"])),
                        "d1_below_ma5": bool(close1 > 0 and ma5 > 0 and close1 < ma5),
                        "d1_below_d0_midpoint": bool(close1 > 0 and midpoint > 0 and close1 < midpoint),
                    })
                    sig["status"] = "tracking"
            signals.append(sig)

    # Four entry scenarios. Entry day is excluded from the following-10-day outcome window.
    scenario_names = {
        "S1": "隔日直接漲停",
        "S2": "隔日跌破支撐",
        "S3": "隔日站穩支撐",
        "S4": "D1-D2量縮不破支撐",
        "S5": "D1漲幅>5%但未漲停",
    }
    combo_rows = []

    def outcome(path, entry_d, entry_price):
        future = [x for x in path if entry_d < int(x.get("d", -1)) <= entry_d + 10]
        if entry_price <= 0 or not future:
            return None
        highs = [float(x.get("high", x.get("close", entry_price))) for x in future]
        lows = [float(x.get("low", x.get("close", entry_price))) for x in future]
        closes = [float(x.get("close", entry_price)) for x in future]
        return {
            "max_up_pct": (max(highs) / entry_price - 1) * 100,
            "max_down_pct": (min(lows) / entry_price - 1) * 100,
            "final_pct": (closes[-1] / entry_price - 1) * 100,
            "days": len(future),
            "complete": len(future) >= 10,
        }

    samples = {(sid, cid): [] for sid in scenario_names for cid in [f"{n:06b}" for n in range(64)]}
    for sig in signals:
        path = sig.get("daily_path") or []
        by_d = {int(x.get("d", -1)): x for x in path}
        d1 = by_d.get(1)
        if not d1:
            continue
        d0_close = float(sig.get("d0_close", 0) or 0)
        midpoint = float(sig.get("d0_midpoint", 0) or 0)
        if midpoint <= 0:
            midpoint = (float(sig.get("d0_high", 0) or 0) + float(sig.get("d0_low", 0) or 0)) / 2
        c1 = float(d1.get("close", 0) or 0)
        m51 = float(d1.get("ma5", 0) or 0)
        limit1 = bool(d1.get("limit_up_close", False))
        broken1 = c1 > 0 and ((m51 > 0 and c1 < m51) or (midpoint > 0 and c1 < midpoint))
        stable1 = c1 > 0 and m51 > 0 and c1 >= m51 and c1 >= midpoint and c1 <= d0_close * (1 + cfg["support_max_gap_pct"])

        entries = []
        if limit1: entries.append(("S1", 1, c1))
        if broken1: entries.append(("S2", 1, c1))
        if stable1: entries.append(("S3", 1, c1))
        # Separate the former "too high" rejection into its own research scenario.
        # It is > D0 +5% but has NOT closed at the D1 limit-up price.
        if d0_close > 0 and c1 > d0_close * (1 + cfg["support_max_gap_pct"]) and not limit1:
            entries.append(("S5", 1, c1))

        # D0 must be >2x its 20d average; first D1/D2 day with volume below its
        # own 20d average AND support intact becomes the entry day.
        if bool((sig.get("combo_bits") or [False]*6)[4]):
            for dd in (1, 2):
                x = by_d.get(dd)
                if not x: continue
                cx = float(x.get("close", 0) or 0)
                mx = float(x.get("ma5", 0) or 0)
                vx = float(x.get("volume_lots", 0) or 0)
                v20x = float(x.get("volume20_avg_lots", 0) or 0)
                support_x = cx > 0 and mx > 0 and cx >= mx and cx >= midpoint
                volume_shrunk = vx > 0 and v20x > 0 and vx < v20x
                if support_x and volume_shrunk:
                    entries.append(("S4", dd, cx))
                    break

        for sid, entry_d, entry_price in entries:
            o = outcome(path, entry_d, entry_price)
            if o:
                o.update({"code": sig.get("code"), "entry_d": entry_d, "entry_price": entry_price})
                samples[(sid, sig["combo_id"])].append(o)

    labels = ["大盤濾網", "年線上", "多頭排列", "20日均量>1萬張", "D0量>20日均量2倍", "D0收盤漲停"]
    for sid, sname in scenario_names.items():
        for n in range(64):
            cid = f"{n:06b}"
            arr = samples[(sid, cid)]
            bits = [c == "1" for c in cid]
            if arr:
                av = lambda k: sum(x[k] for x in arr) / len(arr)
                finals = sorted(x["final_pct"] for x in arr)
                med = finals[len(finals)//2] if len(finals)%2 else (finals[len(finals)//2-1]+finals[len(finals)//2])/2
                row = {
                    "scenario": sid, "scenario_name": sname, "combo_id": cid,
                    "combo": [{"name": labels[j], "value": bits[j]} for j in range(6)],
                    "avg_max_up_pct": round(av("max_up_pct"), 4),
                    "avg_max_down_pct": round(av("max_down_pct"), 4),
                    "avg_final_pct": round(av("final_pct"), 4),
                    "cumulative_pct": round(sum(x["final_pct"] for x in arr), 4),
                    "median_final_pct": round(med, 4),
                    "win_rate_pct": round(sum(1 for x in arr if x["max_up_pct"] >= 20) / len(arr) * 100, 2),
                    "win_definition": "買入後10個交易日內最高漲幅曾達20%",
                    "samples": len(arr), "complete_samples": sum(1 for x in arr if x["complete"]),
                }
            else:
                row = {
                    "scenario": sid, "scenario_name": sname, "combo_id": cid,
                    "combo": [{"name": labels[j], "value": bits[j]} for j in range(6)],
                    "avg_max_up_pct": None, "avg_max_down_pct": None, "avg_final_pct": None,
                    "cumulative_pct": None, "median_final_pct": None, "win_rate_pct": None,
                    "win_definition": "買入後10個交易日內最高漲幅曾達20%",
                    "samples": 0, "complete_samples": 0,
                }
            combo_rows.append(row)

    # Global ranking across all 64 combinations x 5 scenarios = 320 possible results.
    # Only the best five are surfaced to the website/reminder.
    ranked_all = [x for x in combo_rows if x["samples"] > 0]
    ranked_all.sort(key=lambda x: (-(x["avg_max_up_pct"] if x["avg_max_up_pct"] is not None else -999999), -x["win_rate_pct"], -x["samples"], x["scenario"], x["combo_id"]))
    rank_lookup = {}
    for rank, row in enumerate(ranked_all, 1):
        rank_lookup[(row["scenario"], row["combo_id"])] = rank
        row["rank"] = rank
    global_top5 = ranked_all[:5]
    # A recommendation is valid only after the new statistics have at least one real sample.
    # Zero-sample / stale legacy data can never become a buy recommendation.
    top5_keys = {f"{x['scenario']}:{x['combo_id']}" for x in global_top5 if int(x.get("samples", 0) or 0) > 0}

    summary = {
        "generated_at": generated_at,
        "basis": "scenario_entry_close",
        "outcome_window_trading_days": 10,
        "win_definition": "買入後10個交易日內最高漲幅曾達20%",
        "exit_rules": {
            "profit_take": "持股報酬達+20%賣出一半",
            "ma5": "收盤跌破MA5賣出一半",
            "ma10": "收盤跌破MA10全部賣出",
            "time_stop": "買入後滿10個交易日仍未曾達+20%，全部出清"
        },
        "raw_signal_path": f"D0-D{record_through_d}",
        "mother_filter": "MA5/MA10/MA20三條均線彼此糾結<=3%；D0股價突破站上三條均線且當日必須上漲；突破後股價不受3%區間限制",
        "scenario_names": scenario_names,
        "config": cfg,
        "combination_stats": combo_rows,
        "global_top5": global_top5,
        "top5_keys": sorted(top5_keys),
        "rank_lookup": {f"{k[0]}:{k[1]}": v for k, v in rank_lookup.items()},
    }
    Path("signals_history.json").write_text(json.dumps({
        "generated_at": generated_at, "config": cfg, "signals": signals
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    Path("stats_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return signals, summary

def build_reminder_json(cfg, generated_at, results, market_filter, signals, stats):
    """Create one compact public file for the 13:00 / 20:00 reminders."""
    portfolio = load_json("portfolio_state.json", {})
    market_data = load_json("market.json", {})

    rank_lookup = stats.get("rank_lookup") or {}
    stats_rows = []
    for row in (stats.get("global_top5") or []):
        stats_rows.append({
            "scenario": row.get("scenario"),
            "scenario_name": row.get("scenario_name"),
            "rank": row.get("rank"),
            "combo_id": row.get("combo_id"),
            "combo": row.get("combo"),
            "avg_max_up_pct": row.get("avg_max_up_pct"),
            "avg_max_down_pct": row.get("avg_max_down_pct"),
            "avg_final_pct": row.get("avg_final_pct"),
            "cumulative_pct": row.get("cumulative_pct"),
            "win_rate_pct": row.get("win_rate_pct"),
            "samples": row.get("samples", 0),
        })
    top5_keys = set(stats.get("top5_keys") or [])

    current_filter_on = bool((market_filter or {}).get("filter_on", False))

    candidates = []
    for code, rec in results.items():
        conds = rec.get("conditions") or {}
        daily_ret = rec.get("daily_return_pct")
        if daily_ret is None:
            try:
                px = float(rec.get("price", 0) or 0)
                prev = float(rec.get("prev_close", 0) or 0)
                daily_ret = (px / prev - 1) * 100 if px > 0 and prev > 0 else None
            except Exception:
                daily_ret = None
        if not (bool(conds.get("compact_ma")) and bool(conds.get("breakout")) and daily_ret is not None and float(daily_ret) > 0):
            continue
        gid = group_for(rec, cfg)
        bits = [
            current_filter_on,
            bool(conds.get("above_ma240", False)),
            bool(conds.get("bullish", False)),
            bool(conds.get("liquid_20d_gt_min", False)),
            bool(conds.get("volume_multiplier", False)),
            bool(rec.get("limit_up_close", False)),
        ]
        cid = "".join("1" if x else "0" for x in bits)
        candidates.append({
            "code": code,
            "name": rec.get("name", ""),
            "market": rec.get("market", ""),
            "group": gid,
            "group_name": GROUP_NAMES.get(gid, "母篩選"),
            "combo_id": cid,
            "market_filter_on": current_filter_on,
            "stats_rank": None,
            "price": rec.get("price"),
            "high": rec.get("high"),
            "low": rec.get("low"),
            "ma5": rec.get("ma5"),
            "ma10": rec.get("ma10"),
            "ma20": rec.get("ma20"),
            "ma240": rec.get("ma240"),
            "volume_lots": rec.get("volume_lots"),
            "volume20_avg_lots": rec.get("volume20_avg_lots"),
            "conditions": rec.get("conditions") or {},
        })
    candidates.sort(key=lambda x: (x.get("stats_rank") or 999, x.get("group") or "Z", x.get("code") or ""))

    support_candidates = []
    for sig in signals:
        status = sig.get("status")
        if status not in ("waiting_d1", "tracking"):
            continue
        gid = sig.get("group")
        filter_on = bool((sig.get("market_filter") or {}).get("filter_on", False))
        d0_close = float(sig.get("d0_close", 0) or 0)
        d0_high = float(sig.get("d0_high", d0_close) or d0_close)
        d0_low = float(sig.get("d0_low", d0_close) or d0_close)
        midpoint = float(sig.get("d0_midpoint", 0) or 0)
        if midpoint <= 0 and d0_high > 0 and d0_low > 0:
            midpoint = (d0_high + d0_low) / 2.0
        live = results.get(sig.get("code")) or {}
        live_price = float(live.get("price", 0) or 0)
        live_ma5 = float(live.get("ma5", 0) or 0)
        live_limit = bool(live.get("limit_up_close", False))
        live_broken = live_price > 0 and ((live_ma5 > 0 and live_price < live_ma5) or (midpoint > 0 and live_price < midpoint))
        live_stable = live_price > 0 and live_ma5 > 0 and live_price >= live_ma5 and live_price >= midpoint and live_price <= d0_close * (1 + cfg["support_max_gap_pct"])
        live_too_high = bool(d0_close > 0 and live_price > d0_close * (1 + cfg["support_max_gap_pct"]) and not live_limit)
        scenario = "S1" if live_limit else ("S2" if live_broken else ("S3" if live_stable else ("S5" if live_too_high else None)))
        cid = sig.get("combo_id")
        scenario_key = f"{scenario}:{cid}" if scenario and cid else None
        scenario_rank = rank_lookup.get(scenario_key) if scenario_key in top5_keys else None
        if scenario_key not in top5_keys:
            continue
        matched_stat = next((r for r in stats_rows if r.get("scenario") == scenario and r.get("combo_id") == cid), {})
        if int(matched_stat.get("samples", 0) or 0) <= 0:
            continue
        support_candidates.append({
            "code": sig.get("code"),
            "name": sig.get("name", ""),
            "market": sig.get("market", ""),
            "group": gid,
            "group_name": sig.get("group_name", GROUP_NAMES.get(gid, gid)),
            "market_filter_on": filter_on,
            "combo_id": cid,
            "scenario": scenario,
            "scenario_name": (stats.get("scenario_names") or {}).get(scenario) if scenario else None,
            "stats_rank": scenario_rank,
            "avg_max_up_pct": matched_stat.get("avg_max_up_pct"),
            "avg_max_down_pct": matched_stat.get("avg_max_down_pct"),
            "avg_final_pct": matched_stat.get("avg_final_pct"),
            "win_rate_pct": matched_stat.get("win_rate_pct"),
            "samples": matched_stat.get("samples", 0),
            "status": status,
            "d0_date": sig.get("d0_date"),
            "d0_close": sig.get("d0_close"),
            "d0_midpoint": round(midpoint, 4) if midpoint else None,
            "d1_checked_date": sig.get("d1_checked_date"),
            "d1_close": sig.get("d1_close"),
            "support_date": sig.get("support_date"),
            "support_close": sig.get("support_close"),
            "latest_price": live.get("price"),
            "latest_ma5": live.get("ma5"),
            "latest_ma10": live.get("ma10"),
        })
    support_candidates.sort(key=lambda x: (x.get("stats_rank") or 999, x.get("d0_date") or "", x.get("code") or ""))
    support_candidates = support_candidates[:10]

    holdings = []
    raw_holdings = portfolio.get("holdings") if isinstance(portfolio, dict) else []
    if not isinstance(raw_holdings, list):
        raw_holdings = []
    for h in raw_holdings:
        if not isinstance(h, dict):
            continue
        code = str(h.get("code", "")).strip()
        live = results.get(code) or {}
        holdings.append({
            "code": code,
            "name": h.get("name") or live.get("name", ""),
            "lots": h.get("lots"),
            "avg_cost": h.get("avg_cost"),
            "latest_price": live.get("price", h.get("latest_price")),
            "ma5": live.get("ma5", h.get("ma5")),
            "ma10": live.get("ma10", h.get("ma10")),
        })

    leveraged = portfolio.get("leveraged") if isinstance(portfolio, dict) else {}
    if not isinstance(leveraged, dict):
        leveraged = {}
    etf_market = (market_data.get("etf00631L") or {}) if isinstance(market_data, dict) else {}
    taiex_market = (market_data.get("taiex") or {}) if isinstance(market_data, dict) else {}
    leveraged_out = {
        "code": leveraged.get("code", "00631L"),
        "lots": leveraged.get("lots"),
        "avg_cost": leveraged.get("avg_cost"),
        "latest_price": etf_market.get("price", leveraged.get("latest_price")),
        "peak_index": leveraged.get("peak_index"),
        "taiex": taiex_market.get("price", leveraged.get("taiex")),
    }

    out = {
        "schema_version": "2026-09-02.7",
        "generated_at": generated_at,
        "strategy_config": cfg,
        "market_filter": market_filter,
        "portfolio_updated_at": portfolio.get("updated_at") if isinstance(portfolio, dict) else None,
        "capital": portfolio.get("capital") if isinstance(portfolio, dict) else None,
        "leveraged": leveraged_out,
        "holdings": holdings,
        "stats_ranking": stats_rows,
        "support_candidates": support_candidates,
        "scanner_candidates": candidates,
    }
    Path("reminder.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return out


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

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["intraday", "final"], default="final")
    args, _ = parser.parse_known_args()

    if args.mode == "final":
        day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        h = Path("history"); h.mkdir(exist_ok=True)
        snapshot = {"date": day, "generated_at": generated_at, "market_filter": market_filter, "strategy_config": cfg, "stocks": results}
        (h / f"{day}.json").write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        signals, stats = rebuild_signals_and_stats(cfg, generated_at)
    else:
        sig_data = load_json("signals_history.json", {})
        stat_data = load_json("stats_summary.json", {})
        signals = sig_data.get("signals", []) if isinstance(sig_data, dict) else []
        stats = stat_data if isinstance(stat_data, dict) else {"groups": []}

    reminder = build_reminder_json(cfg, generated_at, results, market_filter, signals, stats)
    reminder["run_mode"] = args.mode
    Path("reminder.json").write_text(json.dumps(reminder, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("Run mode:", args.mode)
    print("Signals:", len(signals), "Combination stats:", len(stats.get("combination_stats", [])))
    print("Reminder candidates:", len(reminder.get("scanner_candidates", [])),
          "Support candidates:", len(reminder.get("support_candidates", [])),
          "Holdings:", len(reminder.get("holdings", [])))
    print(json.dumps(meta, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
