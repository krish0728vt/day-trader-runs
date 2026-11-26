import argparse
import datetime
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paper_day  # noqa: E402
import core  # noqa: E402
import sip  # noqa: E402

scan = paper_day.scan
STEP = core.STEP
SETTLE = 75
CFG = {"flat_by": "15:55", "no_entries_after": "15:30", "no_entries_before": "09:45"}
DEFAULTS = {
    "start_date": "2026-11-30",
    "start_equity": 100000.0,
    "or_bars": 3,
    "risk_pct": 0.5,
    "max_position_pct": 25.0,
    "max_gross": 2.0,
    "max_open": 8,
    "min_risk_usd": 0.10,
    "half_at_r": 1.0,
    "overnight_target_r": 3.0,
    "overnight_close_frac": 0.25,
    "max_overnight": 4,
    "max_overnight_gross": 1.0,
    "max_hold_days": 5,
    "day_stop_pct": 2.0,
    "margin_apr": 8.0,
    "borrow_apr": 3.0,
    "spread": 0.01,
}
LOG = []


def say(msg):
    for fh in LOG:
        fh.write(msg + "\n")
        fh.flush()
    if sys.stdout is not None:
        print(msg, flush=True)


def money(x):
    return ("-$%s" % format(-x, ",.2f")) if x < -0.004 else ("$%s" % format(abs(x), ",.2f"))


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def month_dir(dry):
    d = os.path.join(HERE, "trader", "month-dry" if dry else "month")
    os.makedirs(d, exist_ok=True)
    return d


def settings(mdir):
    path = os.path.join(mdir, "config.json")
    cfg = load_json(path)
    if cfg is None:
        cfg = dict(DEFAULTS)
        write_atomic(path, json.dumps(cfg, indent=1))
    return dict(DEFAULTS, **cfg)


def cents(x):
    return round(x + 1e-9, 2)


def next_session(day):
    d = day + datetime.timedelta(days=1)
    while not scan.session(d):
        d += datetime.timedelta(days=1)
    return d


def clock(rules, k):
    return core.slot_end(rules, k).strftime("%I:%M %p").lstrip("0")


def breakout(bars, n, s):
    """The side of the first 5-minute close outside the opening range, if it happened on bar s."""
    rg = core.opening_range(bars, n)
    if not rg:
        return None
    hi, lo, _ = rg
    for k in range(n, s + 1):
        b = bars.get(k)
        if not b:
            continue
        side = 1 if b["c"] > hi else (-1 if b["c"] < lo else 0)
        if side:
            return (side, hi, lo) if k == s else None
    return None


def simulate_day(day, rules, cfg, start, bars, picks, gate, last_slot, final):
    """Replays one session bar by bar from the morning's account. Deterministic, so a restart lands on the same state."""
    eq0 = start["equity"]
    cash = start["cash"]
    spread = cfg["spread"]
    flat = rules["flat_slot"]
    n = int(cfg["or_bars"])
    pos = []
    for p in start["positions"]:
        q = dict(p, legs=[dict(l) for l in p.get("legs_prev", [])], carried=True, mfe=p.get("mfe", 0.0),
                 mae=p.get("mae", 0.0))
        q.pop("legs_prev", None)
        pos.append(q)
    closed, skipped, traded = [], [], set()
    stopped_for_day = False
    marks = {p["symbol"]: p.get("mark", p["entry"]) for p in pos}

    def equity_now():
        return cash + sum(p["side"] * p["shares"] * marks.get(p["symbol"], p["entry"]) for p in pos)

    def gross_now():
        return sum(p["shares"] * marks.get(p["symbol"], p["entry"]) for p in pos)

    def fill(p, shares, px, k, why):
        nonlocal cash
        px = cents(px)
        cash += p["side"] * shares * px - spread * shares
        p["legs"].append({"date": day.isoformat(), "time": clock(rules, k), "slot": k, "shares": shares, "price": px,
                          "why": why})
        p["shares"] -= shares

    def finish(p):
        pnl = sum(p["side"] * l["shares"] * (l["price"] - p["entry"]) for l in p["legs"])
        p["pnl"] = round(pnl, 2)
        p["costs"] = round(spread * (p["orig_shares"] + sum(l["shares"] for l in p["legs"])), 2)
        p["r"] = round(pnl / (p["orig_shares"] * p["risk"]), 2) if p["risk"] else 0.0
        closed.append(p)

    for s in range(0, last_slot + 1):
        for p in list(pos):
            b = bars.get(p["symbol"], {}).get(s)
            if not b or (not p["carried"] and s <= p["entry_slot"]):
                continue
            side, stop, e, risk = p["side"], p["stop"], p["entry"], p["risk"]
            o = b["o"] if b["o"] is not None else b["c"]
            hi = b["h"] if b["h"] is not None else b["c"]
            lo = b["l"] if b["l"] is not None else b["c"]
            fav = (hi - e) / risk if side > 0 else (e - lo) / risk
            adv = (e - lo) / risk if side > 0 else (hi - e) / risk
            p["mfe"], p["mae"] = max(p["mfe"], fav), max(p["mae"], adv)
            hit_stop = lo <= stop if side > 0 else hi >= stop
            if hit_stop:
                gapped = (o <= stop) if side > 0 else (o >= stop)
                px = o if gapped else stop
                why = "gapped through stop at the open" if gapped and s == 0 and p["carried"] else (
                    "breakeven stop" if abs(stop - e) < 0.005 else ("trailing stop" if p["half"] else "stop"))
                fill(p, p["shares"], px, s, why)
                pos.remove(p)
                finish(p)
                marks[p["symbol"]] = b["c"]
                continue
            if not p["half"]:
                t1 = e + side * cfg["half_at_r"] * risk
                if (hi >= t1) if side > 0 else (lo <= t1):
                    half = p["shares"] // 2
                    if half >= 1:
                        fill(p, half, t1, s, "half at %gR" % cfg["half_at_r"])
                    p["half"] = True
                    p["stop"] = e
            if p["carried"]:
                t3 = e + side * cfg["overnight_target_r"] * risk
                if (hi >= t3) if side > 0 else (lo <= t3):
                    fill(p, p["shares"], t3, s, "target %gR" % cfg["overnight_target_r"])
                    pos.remove(p)
                    finish(p)
            marks[p["symbol"]] = b["c"]
        for sym in bars:
            b = bars[sym].get(s)
            if b:
                marks[sym] = b["c"]
        if not stopped_for_day and equity_now() <= eq0 * (1 - cfg["day_stop_pct"] / 100.0):
            stopped_for_day = True
        can_enter = (s >= n and core.slot_end(rules, s) >= rules["before"] and core.slot_end(rules, s) <= rules["after"]
                     and s < flat)
        if can_enter:
            for sym, rv, atr in picks:
                if sym in traded or any(p["symbol"] == sym for p in pos):
                    continue
                sb = bars.get(sym, {})
                if s not in sb:
                    continue
                hit = breakout(sb, n, s)
                if not hit:
                    continue
                traded.add(sym)
                side, hi, lo = hit
                entry = cents(sb[s]["c"])
                mid = (hi + lo) / 2.0
                stop = math.floor(mid * 100) / 100.0 if side > 0 else math.ceil(mid * 100) / 100.0
                risk = round(abs(entry - stop), 4)
                when = clock(rules, s)
                note = lambda why: skipped.append({"symbol": sym, "side": "long" if side > 0 else "short",
                                                   "time": when, "price": entry, "reason": why})
                if s < gate:
                    note("signal came before this run started")
                    continue
                if stopped_for_day:
                    note("down %g%% on the day: no new trades" % cfg["day_stop_pct"])
                    continue
                if risk < cfg["min_risk_usd"]:
                    note("stop only %.2f away (under %.2f)" % (risk, cfg["min_risk_usd"]))
                    continue
                if len(pos) >= cfg["max_open"]:
                    note("already %d positions open" % len(pos))
                    continue
                eq = equity_now()
                lim = {"risk": math.floor(eq0 * cfg["risk_pct"] / 100.0 / risk),
                       "position cap": math.floor(eq0 * cfg["max_position_pct"] / 100.0 / entry),
                       "margin": math.floor(max(cfg["max_gross"] * eq - gross_now(), 0.0) / entry)}
                why = min(lim, key=lambda x: lim[x])
                shares = max(lim[why], 0)
                if shares < 2:
                    note("no room (the %s limit)" % why)
                    continue
                cash -= side * shares * entry + spread * shares
                p = {"symbol": sym, "side": side, "opened": day.isoformat(), "entry_slot": s, "entry_time": when,
                     "shares": shares, "orig_shares": shares, "entry": entry, "stop": stop, "stop0": stop, "risk": risk,
                     "half": False, "days_held": 0, "rv": rv, "atr": atr, "sized_by": why, "legs": [], "carried": False,
                     "mfe": 0.0, "mae": 0.0}
                pos.append(p)
                marks[sym] = entry
    held = []
    if final:
        day_hi, day_lo = {}, {}
        for sym, bb in bars.items():
            hs = [b["h"] for k, b in bb.items() if k <= flat and b["h"] is not None]
            ls = [b["l"] for k, b in bb.items() if k <= flat and b["l"] is not None]
            if hs and ls:
                day_hi[sym], day_lo[sym] = max(hs), min(ls)
        eq = equity_now()
        keep_gross = 0.0
        order = sorted(pos, key=lambda p: (not p["carried"], -(p["side"] * (marks[p["symbol"]] - p["entry"]) / p["risk"])))
        for p in order:
            sym, px = p["symbol"], marks.get(p["symbol"], p["entry"])
            hi, lo = day_hi.get(sym, px), day_lo.get(sym, px)
            frac = (px - lo) / (hi - lo) if hi > lo else 0.5
            strong = frac >= 1 - cfg["overnight_close_frac"] if p["side"] > 0 else frac <= cfg["overnight_close_frac"]
            if p["carried"]:
                ok = p["days_held"] + 1 < cfg["max_hold_days"]
                why = "held %d days: closed at the bell" % (p["days_held"] + 1)
            else:
                ok = p["half"] and strong
                why = "closed at the bell" if not p["half"] else "closed at the bell (finished away from the day's %s)" % (
                    "high" if p["side"] > 0 else "low")
            ok = ok and len(held) < cfg["max_overnight"] and keep_gross + p["shares"] * px <= cfg["max_overnight_gross"] * eq
            if ok:
                keep_gross += p["shares"] * px
                p["days_held"] += 1
                trail = lo if p["side"] > 0 else hi
                p["stop"] = cents(max(p["stop"], trail) if p["side"] > 0 else min(p["stop"], trail))
                held.append(p)
            else:
                fill(p, p["shares"], px, flat, why)
                finish(p)
        pos = held
    eq_end = equity_now()
    long_v = sum(p["shares"] * marks.get(p["symbol"], p["entry"]) for p in pos if p["side"] > 0)
    short_v = sum(p["shares"] * marks.get(p["symbol"], p["entry"]) for p in pos if p["side"] < 0)
    charges = {"margin_interest": 0.0, "borrow_fee": 0.0, "nights": 0}
    if final and pos:
        nights = (next_session(day) - day).days
        charges = {"margin_interest": round(max(long_v - eq_end, 0.0) * cfg["margin_apr"] / 100.0 / 360 * nights, 2),
                   "borrow_fee": round(short_v * cfg["borrow_apr"] / 100.0 / 360 * nights, 2), "nights": nights}
        cash -= charges["margin_interest"] + charges["borrow_fee"]
        eq_end = equity_now()
    opened = [p for p in closed + pos if not p["carried"]]
    out_pos = []
    for p in pos:
        q = {k: v for k, v in p.items() if k not in ("legs", "carried")}
        q["mark"] = marks.get(p["symbol"], p["entry"])
        q["unrealized"] = round(p["side"] * p["shares"] * (q["mark"] - p["entry"]), 2)
        q["legs_prev"] = p["legs"]
        out_pos.append(q)
    return {"date": day.isoformat(), "equity_start": round(eq0, 2), "equity_end": round(eq_end, 2),
            "cash_end": round(cash, 2), "pnl": round(eq_end - eq0, 2), "ret_pct": round(100.0 * (eq_end - eq0) / eq0, 3),
            "closed": [dict(p) for p in closed],
            "positions": out_pos, "skipped": skipped, "n_opened": len(opened), "stopped_for_day": stopped_for_day,
            "long_value": round(long_v, 2), "short_value": round(short_v, 2), "charges": charges,
            "gross_end": round(long_v + short_v, 2), "last_slot": last_slot, "final": final}


def spy_track(ledger, spy_bars, cfg, last_slot):
    sp = ledger.get("spy")
    if not spy_bars:
        return sp, None
    if not sp:
        b0 = spy_bars.get(0) or spy_bars[min(spy_bars)]
        px = b0["o"] if b0["o"] is not None else b0["c"]
        sh = int(cfg["start_equity"] // px)
        sp = {"bought": ledger.get("today"), "price": round(px, 2), "shares": sh,
              "cash": round(cfg["start_equity"] - sh * px - cfg["spread"] * sh, 2)}
    k = max(x for x in spy_bars if x <= last_slot) if any(x <= last_slot for x in spy_bars) else None
    if k is None:
        return sp, None
    close = spy_bars[k]["c"]
    return sp, {"close": close, "equity": round(sp["cash"] + sp["shares"] * close, 2)}


def side_word(side):
    return "long" if side > 0 else "short"


def report_md(day_doc, ledger, cfg, rules):
    d = day_doc
    L = ["# $100,000 month: %s" % datetime.date.fromisoformat(d["date"]).strftime("%A, %B %d, %Y").replace(" 0", " "), ""]
    status = "final at the close" if d["final"] else "as of %s New York" % clock(rules, d["last_slot"])
    L.append("Paper money only; nothing is placed. Each day starts with what the day before ended with. Status: %s." % status)
    days = ledger.get("days", [])
    spy = d.get("spy") or {}
    L += ["", "## Today", "", "| | account | SPY buy-and-hold |", "| --- | --- | --- |",
          "| started the day | %s | %s |" % (money(d["equity_start"]), money(d.get("spy_start") or cfg["start_equity"])),
          "| now | %s | %s |" % (money(d["equity_end"]), money(spy.get("equity", 0.0)) if spy else "n/a"),
          "| today | %s (%+.2f%%) | %s |" % (money(d["pnl"]), d["ret_pct"], (money(spy["equity"] - (d.get("spy_start") or cfg["start_equity"])) if spy else "n/a")),
          "| since the start | %s (%+.2f%%) | %s (%+.2f%%) |" % (
              money(d["equity_end"] - cfg["start_equity"]), 100.0 * (d["equity_end"] / cfg["start_equity"] - 1),
              money(spy.get("equity", cfg["start_equity"]) - cfg["start_equity"]),
              100.0 * (spy.get("equity", cfg["start_equity"]) / cfg["start_equity"] - 1))]
    if d.get("picks"):
        L += ["", "Stocks in play today (opening volume against its 14-day average): " +
              ", ".join("%s %.1fx" % (p[0], p[1]) for p in d["picks"]) + "."]
    trades = d["closed"]
    if trades:
        L += ["", "## Closed today", "",
              "| ticker | side | opened | in | shares | entry | first stop | exits | P&L | costs | R | best R | worst R |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for p in trades:
            ex = "; ".join("%d @ %.2f %s%s (%s)" % (
                l["shares"], l["price"], "" if l["date"] == d["date"] else datetime.date.fromisoformat(l["date"]).strftime("%b %d ").replace(" 0", " "),
                l["time"], l["why"]) for l in p["legs"])
            L.append("| %s | %s | %s | %s | %d | %.2f | %.2f | %s | %s | %s | %+.2f | %+.2f | %+.2f |" % (
                p["symbol"], side_word(p["side"]), p["opened"], p["entry_time"], p["orig_shares"], p["entry"], p["stop0"],
                ex, money(p["pnl"]), money(p["costs"]), p["r"], p["mfe"], -p["mae"]))
    if d["positions"]:
        L += ["", "## Open positions" + (" (held overnight)" if d["final"] else ""), "",
              "| ticker | side | opened | shares | entry | stop now | last | unrealized | days held |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for p in d["positions"]:
            L.append("| %s | %s | %s | %d | %.2f | %.2f | %.2f | %s | %d |" % (
                p["symbol"], side_word(p["side"]), p["opened"], p["shares"], p["entry"], p["stop"], p["mark"],
                money(p["unrealized"]), p["days_held"]))
        L.append("")
        L.append("Gross exposure %s (longs %s, shorts %s) against equity %s." % (
            money(d["gross_end"]), money(d["long_value"]), money(d["short_value"]), money(d["equity_end"])))
    if d["charges"]["nights"]:
        L.append("Overnight charges: margin interest %s, short borrow fee %s (%d night%s)." % (
            money(d["charges"]["margin_interest"]), money(d["charges"]["borrow_fee"]), d["charges"]["nights"],
            "" if d["charges"]["nights"] == 1 else "s"))
    if d["skipped"]:
        L += ["", "## Signals not taken", "", "| ticker | side | time | price | why |", "| --- | --- | --- | --- | --- |"]
        for x in d["skipped"]:
            L.append("| %s | %s | %s | %.2f | %s |" % (x["symbol"], x["side"], x["time"], x["price"], x["reason"]))
    if days:
        L += ["", "## Every day so far", "",
              "| day | start | end | P&L | return | trades opened | held overnight | SPY | account vs SPY since start |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for x in days:
            L.append("| %s | %s | %s | %s | %+.2f%% | %d | %d | %s | %s |" % (
                x["date"], money(x["equity_start"]), money(x["equity_end"]), money(x["pnl"]), x["ret_pct"], x["n_opened"],
                len(x["positions"]), money((x.get("spy") or {}).get("equity", 0.0)),
                money(x["equity_end"] - (x.get("spy") or {}).get("equity", x["equity_end"]))))
    L += ["", "## The rules", "",
          "- Each morning: the 20 stocks with the heaviest opening volume against their 14-day average (price over $5, "
          "average volume over 1M, daily range over $0.50), from the 500 most-traded stocks the night before.",
          "- Trade the first 5-minute close outside the first %d minutes' range: above it buys, below it sells short. Entries "
          "9:45 to 3:30. Stop at the middle of the range." % (cfg["or_bars"] * STEP),
          "- Risk %g%% of the morning's equity a trade, at most %g%% of equity in one stock, total positions up to %gx equity "
          "(margin), at most %d open. No new trades once the day is down %g%%." % (
              cfg["risk_pct"], cfg["max_position_pct"], cfg["max_gross"], cfg["max_open"], cfg["day_stop_pct"]),
          "- Half comes off at %gR and the stop moves to breakeven. The rest closes at 3:55, unless the trade is past %gR and "
          "the stock finished in the top (bottom, for a short) %d%% of its day: then it is held overnight with the stop trailed "
          "to the day's low (high), for up to %d days or a %gR target. At most %d held overnight, worth up to %gx equity." % (
              cfg["half_at_r"], cfg["half_at_r"], cfg["overnight_close_frac"] * 100, cfg["max_hold_days"],
              cfg["overnight_target_r"], cfg["max_overnight"], cfg["max_overnight_gross"]),
          "- Costs: 1 cent a share on every fill, %g%% a year margin interest on borrowed money and %g%% a year to borrow "
          "shorted stock, both charged per night held. Fills are idealized (bar closes and exact stops; a gap through a stop "
          "fills at the open)." % (cfg["margin_apr"], cfg["borrow_apr"]),
          "", "Paper trades only. Not financial advice.", ""]
    return "\n".join(L)


def last_complete_slot(rules, local):
    k = int((local - rules["open"]).total_seconds() // (STEP * 60)) - 1
    if local < core.slot_end(rules, max(k, 0)) + datetime.timedelta(seconds=SETTLE):
        k -= 1
    return min(k, rules["flat_slot"])


def fetch_bars(src, syms, rules, local, cache):
    out = {}
    for s in syms:
        ser = None
        for _ in range(3):
            try:
                ser = src.chart(s)
                break
            except Exception as e:
                say("  %s: %s (will retry)" % (s, e))
                src.sleep(2)
        if ser is not None:
            cache[s] = ser
        ser = cache.get(s)
        if ser is not None:
            out[s] = paper_day.bars_of(ser, rules, local)
    return out


def run(a):
    src = paper_day.Source(a)
    local = scan.to_eastern(src.now())
    day = local.date()
    a.dry = a.dry or day.isoformat() < settings(month_dir(False))["start_date"]
    mdir = month_dir(a.dry)
    cfg = settings(mdir)
    rules = core.day_rules(day, CFG, scan)
    if not rules:
        say("The NYSE is closed on %s." % day)
        return
    led_path = os.path.join(mdir, "ledger.json")
    ledger = load_json(led_path, {"days": []})
    days = [x for x in ledger["days"] if x["date"] < day.isoformat()]
    if any(x["date"] == day.isoformat() and x["final"] for x in ledger["days"]):
        say("Today is already finished: trader/%s/%s.md" % (os.path.basename(mdir), day.isoformat()))
        return
    if local >= rules["close"] + datetime.timedelta(minutes=30) and not os.path.exists(os.path.join(mdir, "%s.json" % day)):
        say("Past the close and today never started: nothing to replay.")
        return
    prev = days[-1] if days else None
    start = {"equity": prev["equity_end"] if prev else cfg["start_equity"],
             "cash": prev["cash_end"] if prev else cfg["start_equity"],
             "positions": [dict(p) for p in (prev["positions"] if prev else [])]}
    for p in start["positions"]:
        p.pop("unrealized", None)
    day_path = os.path.join(mdir, "%s.json" % day.isoformat())
    md_path = os.path.join(mdir, "%s.md" % day.isoformat())
    meta = load_json(day_path, {})
    if "gate" not in meta:
        meta["gate"] = max(last_complete_slot(rules, local) + 1, 0)
        meta["started"] = local.strftime("%H:%M")
    say("$100,000 month%s: %s. Starting equity %s, %d position(s) carried in." % (
        " (rehearsal)" if a.dry else "", day, money(start["equity"]), len(start["positions"])))
    first_ready = rules["open"] + datetime.timedelta(minutes=STEP, seconds=SETTLE)
    if "picks" not in meta:
        if local < first_ready:
            say("Waiting for the first 5-minute bar (%s New York)..." % first_ready.strftime("%H:%M:%S"))
            src.sleep((first_ready - local).total_seconds())
            local = scan.to_eastern(src.now())
        picks = []
        for attempt in range(3):
            try:
                got, note = sip.live_pick(scan, src, rules, say)
                if got is not None:
                    picks = [list(p) for p in got]
                    meta["pick_note"] = note
                    break
                meta["pick_note"] = note
                break
            except Exception as e:
                say("Picking stocks in play failed: %s (try %d of 3)" % (e, attempt + 1))
                src.sleep(20)
        meta["picks"] = picks
        meta["picked_at"] = scan.to_eastern(src.now()).strftime("%H:%M")
        say("Stocks in play: %s" % (", ".join("%s %.1fx" % (p[0], p[1]) for p in picks) or "none"))
    picks = [tuple(p) for p in meta["picks"]]
    syms = sorted({p[0] for p in picks} | {p["symbol"] for p in start["positions"]} | {"SPY"})
    cache = {}
    while True:
        local = scan.to_eastern(src.now())
        k = last_complete_slot(rules, local)
        if k < 0:
            src.sleep((core.slot_end(rules, 0) + datetime.timedelta(seconds=SETTLE) - local).total_seconds())
            continue
        bars = fetch_bars(src, syms, rules, local, cache)
        spy_bars = bars.pop("SPY", {})
        final = k >= rules["flat_slot"]
        doc = simulate_day(day, rules, cfg, start, bars, picks, meta["gate"], k, final)
        ledger["today"] = day.isoformat()
        sp, mark = spy_track(ledger, spy_bars, cfg, k)
        doc.update(picks=meta["picks"], gate=meta["gate"], started=meta["started"], picked_at=meta.get("picked_at"),
                   pick_note=meta.get("pick_note"), spy=mark,
                   spy_start=(days[-1].get("spy") or {}).get("equity") if days else cfg["start_equity"])
        write_atomic(day_path, json.dumps(doc, indent=1))
        if final:
            ledger["spy"] = sp
            ledger["days"] = days + [doc]
            ledger.pop("today", None)
            write_atomic(led_path, json.dumps(ledger, indent=1))
        write_atomic(md_path, report_md(doc, {"days": days + ([doc] if final else [])}, cfg, rules))
        say("%s  equity %s (%+.2f%% today) | open %d | closed %d | SPY %s" % (
            clock(rules, k), money(doc["equity_end"]), doc["ret_pct"], len(doc["positions"]), len(doc["closed"]),
            money(mark["equity"]) if mark else "n/a"))
        if final:
            say("Final for %s: %s. Report: trader/%s/%s.md" % (day, money(doc["equity_end"]), os.path.basename(mdir), day))
            return
        if a.until and local.strftime("%H:%M") >= a.until:
            return
        nxt = core.slot_end(rules, k + 1) + datetime.timedelta(seconds=SETTLE)
        src.sleep(max((nxt - scan.to_eastern(src.now())).total_seconds(), 5))


def main(argv=None):
    p = argparse.ArgumentParser(description="The $100,000 paper month: stocks in play, long and short, carried day to day.")
    p.add_argument("--dry", action="store_true", help="rehearsal: write to trader/month-dry (automatic before the start date)")
    p.add_argument("--log", help="also write the output to this file")
    p.add_argument("--offline", help=argparse.SUPPRESS)
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.add_argument("--until", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if a.log:
        LOG.append(open(a.log, "a", encoding="utf-8"))
    run(a)


if __name__ == "__main__":
    main()
