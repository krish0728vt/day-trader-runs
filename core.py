import datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

STEP = 5
CENT = Decimal("0.01")
BASE = {"or_bars": 3, "stop": "or_mid", "target": 1.0, "cutoff": "15:30"}


def D(x):
    return Decimal(repr(float(x)))


def floor_cents(x):
    return float(D(x).quantize(CENT, rounding=ROUND_FLOOR))


def day_rules(day, cfg, scan, cutoff=None):
    s = scan.session(day)
    if not s:
        return None
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    shift = datetime.datetime.combine(day, scan.CLOSE) - close_dt
    t = lambda k, d: datetime.datetime.combine(day, scan.parse_hhmm(cfg.get(k) or d, k))
    flat = t("flat_by", "15:55") - shift
    after = t("no_entries_after", "15:30") - shift
    if cutoff:
        after = min(after, datetime.datetime.combine(day, scan.parse_hhmm(cutoff, "cutoff")) - shift)
    return {"open": open_dt, "close": close_dt, "before": t("no_entries_before", "09:45"), "after": after,
            "flat": flat, "flat_slot": int((flat - open_dt).total_seconds() // (STEP * 60)) - 1}


def slot_end(rules, k):
    return rules["open"] + datetime.timedelta(minutes=STEP * (k + 1))


def opening_range(bars, n):
    rng = [bars.get(k) for k in range(n)]
    if any(b is None or b["h"] is None or b["l"] is None for b in rng):
        return None
    return max(b["h"] for b in rng), min(b["l"] for b in rng), sum(b["v"] for b in rng) / float(n)


def first_trigger(bars, orh, n, rules):
    for k in sorted(bars):
        if k < n:
            continue
        end = slot_end(rules, k)
        if end < rules["before"] or end > rules["after"]:
            continue
        if bars[k]["c"] > orh:
            return k
    return None


def plan(entry, orh, orl, v, atr=None):
    e = D(round(entry, 2)).quantize(CENT, rounding=ROUND_HALF_UP)
    if v["stop"] == "or_mid":
        raw = (D(round(orh, 2)).quantize(CENT, rounding=ROUND_HALF_UP) + D(round(orl, 2)).quantize(CENT, rounding=ROUND_HALF_UP)) / 2
    elif v["stop"] == "or_low":
        raw = D(round(orl, 2)).quantize(CENT, rounding=ROUND_HALF_UP)
    elif v["stop"] == "atr":
        if not atr:
            return None
        raw = e - D(v["stop_atr"]) * D(atr)
    else:
        raw = e * (1 - D(v["stop_pct"]) / 100)
    stop = raw.quantize(CENT, rounding=ROUND_FLOOR)
    risk = e - stop
    if stop <= 0 or risk <= 0 or risk < e * Decimal("0.0005"):
        return None
    if v.get("target") is None:
        target = None
    else:
        target = float((e + D(v["target"]) * risk).quantize(CENT, rounding=ROUND_HALF_UP))
    return float(e), float(stop), target, float(risk)


def features(bars, k, orh, orl, avg_v, spy, prev_close, risk, entry):
    b = bars[k]
    f = {"slot": k, "pct_over": round((b["c"] / orh - 1) * 100, 3),
         "or_width_pct": round((orh - orl) / b["c"] * 100, 3),
         "risk_pct": round(risk / entry * 100, 3), "risk_usd": round(risk, 2),
         "vol_x": round(b["v"] / avg_v, 2) if avg_v else None,
         "day_move_pct": round((b["c"] / bars[0]["o"] - 1) * 100, 3) if bars.get(0) and bars[0]["o"] else None,
         "gap_pct": round((bars[0]["o"] / prev_close - 1) * 100, 3) if prev_close and bars.get(0) and bars[0]["o"] else None}
    if spy and k in spy and all(j in spy for j in range(3)):
        f["spy_above_or"] = spy[k]["c"] > max(spy[j]["h"] for j in range(3))
        f["spy_up_day"] = spy[k]["c"] > spy[0]["o"] if spy[0]["o"] else None
    return f


def passes(v, f):
    if v.get("max_over") is not None and f["pct_over"] > v["max_over"]:
        return "more than %.2f%% over the range at entry" % v["max_over"]
    if v.get("min_volx") is not None and (f["vol_x"] is None or f["vol_x"] < v["min_volx"]):
        return "breakout bar under %.1fx the range's volume" % v["min_volx"]
    if v.get("min_risk_pct") is not None and f["risk_pct"] < v["min_risk_pct"]:
        return "stop closer than %.2f%% of the price" % v["min_risk_pct"]
    if v.get("max_risk_pct") is not None and f["risk_pct"] > v["max_risk_pct"]:
        return "stop more than %.2f%% away" % v["max_risk_pct"]
    if v.get("min_risk_usd") is not None and f["risk_usd"] < v["min_risk_usd"]:
        return "stop closer than %d cents a share" % round(v["min_risk_usd"] * 100)
    if v.get("spy") == "above_or" and f.get("spy_above_or") is False:
        return "SPY not above its own opening range"
    if v.get("spy") == "up_day" and f.get("spy_up_day") is False:
        return "SPY below its open"
    if v.get("no_gap_over") is not None and f.get("gap_pct") is not None and abs(f["gap_pct"]) > v["no_gap_over"]:
        return "gapped more than %.1f%% at the open" % v["no_gap_over"]
    return None


def trade(bars, v, rules, gate=None, spy=None, prev_close=None, last_slot=None, final=True, atr=None):
    """One stock, one day, one rule. None = no signal; {"pre": True} = signalled before the gate;
    {"filtered": why} = signal skipped by a filter; otherwise the trade with its result:
    stop, target, time, flat, or open (still held at last_slot)."""
    n = v.get("or_bars", 3)
    rg = opening_range(bars, n)
    if not rg:
        return None
    orh, orl, avg_v = rg
    k = first_trigger(bars, orh, n, rules)
    if k is None:
        return None
    if gate is not None and slot_end(rules, k) <= gate:
        return {"pre": True, "slot": k}
    p = plan(round(bars[k]["c"], 2), orh, orl, v, atr)
    if not p:
        return {"filtered": "stop too tight to survive the spread" if v["stop"] != "atr" or atr else "no ATR for this stock",
                "slot": k, "entry": round(bars[k]["c"], 2)}
    entry, stop0, target, risk = p
    f = features(bars, k, orh, orl, avg_v, spy, prev_close, risk, entry)
    why = passes(v, f)
    if why:
        return {"filtered": why, "slot": k, "entry": entry, "feat": f}
    last = rules["flat_slot"] if last_slot is None else min(last_slot, rules["flat_slot"])
    stop = stop0
    part = v.get("partial")
    part_px = float(D(entry + part * risk).quantize(CENT, rounding=ROUND_HALF_UP)) if part else None
    leg1 = None
    be_at, trail = v.get("be_at"), v.get("trail_r")
    tstop = v.get("time_stop")
    hi = lo = entry
    res = px = xk = None
    for j in range(k + 1, last + 1):
        b = bars.get(j)
        if b is None:
            continue
        if b["l"] is not None and b["l"] <= stop:
            px, res, xk = (min(b["o"], stop) if b["o"] is not None else stop), "stop", j
            lo = min(lo, b["l"])
            break
        if part and leg1 is None and b["h"] is not None and b["h"] >= part_px:
            leg1 = max(b["o"], part_px) if b["o"] is not None else part_px
        if target is not None and b["h"] is not None and b["h"] >= target:
            px, res, xk = (max(b["o"], target) if b["o"] is not None else target), "target", j
            hi = max(hi, b["h"])
            break
        hi, lo = max(hi, b["h"] if b["h"] is not None else b["c"]), min(lo, b["l"] if b["l"] is not None else b["c"])
        if j >= rules["flat_slot"]:
            px, res, xk = b["c"], "flat", j
            break
        if tstop and j - k == tstop[0] and hi < entry + tstop[1] * risk:
            px, res, xk = b["c"], "time", j
            break
        if leg1 is not None:
            stop = max(stop, entry)
        if be_at is not None and hi >= entry + be_at * risk:
            stop = max(stop, entry)
        if trail is not None:
            stop = max(stop, floor_cents(hi - trail * risk))
    if res is None:
        ks = [j for j in bars if k < j <= last]
        xk = max(ks) if ks else k
        px = bars[xk]["c"] if ks else entry
        res = "flat" if final else "open"
    after = [bars[j]["h"] for j in range(xk + 1, rules["flat_slot"] + 1) if j in bars and bars[j]["h"] is not None]
    px = round(px, 2)
    out = {"slot": k, "entry": entry, "stop": stop0, "target": target, "risk": risk, "result": res,
           "exit_slot": xk, "exit": px, "mfe_r": (hi - entry) / risk, "mae_r": (lo - entry) / risk,
           "after_exit_best_r": (max(after) - entry) / risk if after else (px - entry) / risk, "feat": f}
    if stop != stop0:
        out["stop_moved_to"] = stop
    if leg1 is not None:
        out["leg1"] = round(leg1, 2)
        out["exit"] = round((leg1 + px) / 2, 4)
    out["r"] = (out["exit"] - entry) / risk
    return out
