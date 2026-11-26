import argparse
import datetime
import importlib.util
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, ".claude", "skills")
sys.path.insert(0, HERE)
import core  # noqa: E402
import sip  # noqa: E402

STEP = 5
N_RANGE = 3
SETTLE = 75
MIN_PRICE = 5.0
A_MAX_PRICE = 10.0
PICK = 100
DUMMY = 10000.0
SPREAD = 0.01
LIVE_KEYS = ("or_bars", "stop", "target", "cutoff", "max_over", "min_volx", "min_risk_pct", "max_risk_pct",
             "min_risk_usd", "spy", "no_gap_over", "be_at", "trail_r", "time_stop", "partial")


def load(folder, name):
    spec = importlib.util.spec_from_file_location("pd_" + name, os.path.join(SKILLS, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = load("trader-scanner", "scan")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)


LOG = []


def say(msg):
    for fh in LOG:
        fh.write(msg + "\n")
        fh.flush()
    if sys.stdout is not None:
        print(msg, flush=True)


def keep_awake(on):
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
    except Exception:
        pass


def take_lock(path, now_utc):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                last = datetime.datetime.strptime(fh.read().strip()[:19], "%Y-%m-%dT%H:%M:%S")
            if (now_utc - last).total_seconds() < 600:
                return False
        except (OSError, ValueError):
            pass
    touch_lock(path, now_utc)
    return True


def touch_lock(path, now_utc):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(now_utc.strftime("%Y-%m-%dT%H:%M:%S"))


def f(arr, i):
    return None if not arr or i >= len(arr) or arr[i] is None else float(arr[i])


def money(x):
    return ("-$%s" % format(-x, ",.2f")) if x < -0.004 else ("$%s" % format(abs(x), ",.2f"))


def pct(x):
    return "%+.2f%%" % x


def tgt(x):
    return "close" if x is None else "%.2f" % x


def result_label(p):
    r = p["result"]
    if r == "stop" and p.get("stop_moved_to") is not None:
        r = "breakeven stop" if abs(p["stop_moved_to"] - p["entry"]) < 0.005 else "trailing stop"
    if p.get("leg1") is not None:
        r = "half at %.2f, rest %s" % (p["leg1"], r)
    return r


class Source(object):
    def __init__(self, a):
        self.data = None
        if a.offline:
            self.data, _ = scan.load_offline(a.offline)
        self.clock = scan.parse_now(a.now) if a.now else None

    def now(self):
        return self.clock if self.clock else utc_now()

    def sleep(self, sec):
        if sec <= 0:
            return
        if self.clock:
            self.clock += datetime.timedelta(seconds=sec)
        else:
            time.sleep(sec)

    def spark(self, syms):
        if self.data is not None:
            return {s: self.data[s] for s in syms if s in self.data}
        data, _ = scan.fetch_spark(list(syms), "1d", "5m", scan.PAUSE)
        return data

    def chart(self, sym):
        if self.data is not None:
            if sym not in self.data:
                raise scan.ScanError("no offline data for %s" % sym)
            return self.data[sym]
        time.sleep(scan.PAUSE)
        return scan.parse_chart(scan.yahoo_get(scan.chart_path(sym, "1d", "5m")), sym)


def clock(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def bars_of(series, rules, cutoff):
    out = {}
    t, c = series.get("t") or [], series.get("c") or []
    o, h, l, v = series.get("o"), series.get("h"), series.get("l"), series.get("v")
    for i, ts in enumerate(t):
        if i >= len(c) or c[i] is None:
            continue
        et = scan.ts_to_eastern(ts)
        if et.date() != rules["open"].date() or et.second or et.minute % STEP:
            continue
        if et < rules["open"] or et >= rules["close"]:
            continue
        if et + datetime.timedelta(minutes=STEP) > cutoff:
            continue
        k = int((et - rules["open"]).total_seconds() // (STEP * 60))
        out[k] = {"o": f(o, i), "h": f(h, i), "l": f(l, i), "c": float(c[i]), "v": (v[i] or 0) if v and i < len(v) else 0}
    return out


def prev_close_of(series):
    m = series.get("meta") or {}
    x = m.get("chartPreviousClose") or m.get("previousClose")
    return float(x) if x else None


def load_rules(day):
    path = os.path.join(HERE, "trader", "learning", "rules.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError:
        return {}
    if doc.get("date", "") >= day.isoformat():
        return {}
    out = {}
    for g, c in (doc.get("challengers") or {}).items():
        v = c.get("variant") or {}
        if v.get("or_bars", 3) != N_RANGE or any(k not in LIVE_KEYS for k in v):
            continue
        out[g] = {"variant": dict(core.BASE, **v), "changes": c.get("changes") or [], "from": doc.get("date")}
    return out


def make_books(cfg, learned):
    a = {"budget": float(cfg["budget"]), "buffer_pct": float(cfg.get("cash_buffer_pct", 10)),
         "pos_pct": float(cfg.get("max_position_pct", 75)), "max_order": float(cfg.get("max_order_usd", 4000)),
         "min_order": float(cfg.get("min_order_usd", 5)), "max_loss": float(cfg["max_loss_per_trade_usd"]),
         "max_daily": float(cfg["max_daily_loss_usd"]), "max_trades": int(cfg.get("max_trades_per_day", 3)),
         "min_rr": float(cfg.get("min_reward_risk", 1.0))}
    k = DUMMY / a["budget"]
    b = dict(a, budget=DUMMY, max_order=a["max_order"] * k, max_loss=a["max_loss"] * k,
             max_daily=a["max_daily"] * k, max_trades=None)
    books = []
    for key, label, base in (("A", "Your rules ($%g)" % a["budget"], a), ("B", "$%s dummy" % format(int(DUMMY), ","), b)):
        for r in (1, 2):
            books.append(dict(base, id="%s-%dR" % (key, r), group=key, label="%s, %dR target" % (label, r),
                              variant=dict(core.BASE, target=float(r))))
        if key in learned:
            books.append(dict(base, id="%s-new" % key, group=key,
                              label="%s, learned rule (%s)" % (label, " + ".join(learned[key]["changes"])),
                              variant=learned[key]["variant"]))
    return books


def make_c_books(cfg):
    b = [x for x in make_books(cfg, {}) if x["id"] == "B-1R"][0]
    return [dict(b, id="C-1", group="C", label="Stocks in play, published rule (5-min range, stop 10% of ATR, hold to close)",
                 variant=dict(sip.C_RULE)),
            dict(b, id="C-2", group="C", label="Stocks in play, today's rule (15-min range, 1R)", variant=dict(sip.C_TODAY))]


def sip_state(path, day, rules, src, local, sip_on):
    if not sip_on:
        return None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            pass
    first_ready = rules["open"] + datetime.timedelta(minutes=STEP, seconds=SETTLE)
    if local < first_ready:
        say("Waiting for the first 5-minute bar to pick today's stocks in play (%s New York)..." % first_ready.strftime("%H:%M:%S"))
        src.sleep((first_ready - local).total_seconds())
    if scan.to_eastern(src.now()) >= rules["after"]:
        return None
    say("Picking today's 20 stocks in play from last night's pool...")
    try:
        picks, note = sip.live_pick(scan, src, rules, say)
    except Exception as e:
        say("Stocks in play skipped today: %s" % e)
        return None
    if picks is None:
        say("Stocks in play skipped today: %s." % note)
        return None
    now = scan.to_eastern(src.now())
    gate = rules["open"] + datetime.timedelta(minutes=STEP * int((now - rules["open"]).total_seconds() // (STEP * 60)))
    doc = {"date": day.isoformat(), "picked_at": now.strftime("%H:%M"), "gate": gate.strftime("%H:%M"),
           "picks": picks, "note": note}
    write_atomic(path, json.dumps(doc, indent=1))
    say("Stocks in play (%s): %s. Signals count from the %s bar on." % (
        note, ", ".join("%s %.1fx" % (p[0], p[1]) for p in picks) or "none today", gate.strftime("%H:%M")))
    return doc


def size(book, entry, risk, cash):
    lim = {"max loss": math.floor(book["max_loss"] / risk + 1e-9),
           "position cap": math.floor(book["budget"] * book["pos_pct"] / 100.0 / entry + 1e-9),
           "max order": math.floor(book["max_order"] / entry + 1e-9),
           "cash": math.floor(max(cash - book["budget"] * book["buffer_pct"] / 100.0, 0.0) / entry + 1e-9)}
    why = min(lim, key=lambda x: lim[x])
    return max(lim[why], 0), why


def account(book, universe, sigs, rank, rules, last_slot):
    cash = book["budget"]
    held, trades, skipped = {}, [], []
    n, risk_used = 0, 0.0
    by_slot = {}
    for sym, t in sigs.items():
        if sym in universe and t and not t.get("pre") and t["slot"] <= last_slot:
            by_slot.setdefault(t["slot"], []).append(sym)
    for k in by_slot:
        by_slot[k].sort(key=lambda s: (-rank.get(s, 0), s))
    if by_slot:
        for s in range(min(by_slot), last_slot + 1):
            for sym in list(held):
                p = held[sym]
                if p["result"] != "open" and p["exit_slot"] == s:
                    cash += p["shares"] * p["exit"]
                    del held[sym]
            for sym in by_slot.get(s, []):
                t = sigs[sym]
                when = clock(core.slot_end(rules, s))
                entry = t.get("entry", 0.0)
                skip = lambda why: skipped.append({"symbol": sym, "time": when, "price": entry, "reason": why})
                if "filtered" in t:
                    skip(t["filtered"])
                    continue
                if book["max_trades"] is not None and n >= book["max_trades"]:
                    skip("already %d trades today" % n)
                    continue
                if t["target"] is not None and (t["target"] - t["entry"]) / t["risk"] + 1e-9 < book["min_rr"]:
                    skip("reward:risk under %.1f" % book["min_rr"])
                    continue
                shares, why = size(book, t["entry"], t["risk"], cash)
                if shares < 1:
                    skip("0 shares fit (the %s limit)" % why)
                    continue
                if shares * t["entry"] < book["min_order"]:
                    skip("under the $%g minimum order" % book["min_order"])
                    continue
                if risk_used + shares * t["risk"] > book["max_daily"] + 1e-9:
                    skip("would pass the $%g daily loss limit" % book["max_daily"])
                    continue
                n += 1
                risk_used += shares * t["risk"]
                cash -= shares * t["entry"]
                p = {"symbol": sym, "entry_slot": s, "entry_time": when, "shares": shares, "entry": t["entry"],
                     "cost": round(shares * t["entry"], 2), "stop": t["stop"], "target": t["target"], "risk": t["risk"],
                     "max_loss": round(shares * t["risk"], 2), "sized_by": why, "result": t["result"],
                     "exit_slot": t["exit_slot"]}
                if t.get("leg1") is not None:
                    p["leg1"] = t["leg1"]
                if t.get("stop_moved_to") is not None:
                    p["stop_moved_to"] = t["stop_moved_to"]
                if t["result"] == "open":
                    p.update(mark=t["exit"], mark_time=clock(core.slot_end(rules, t["exit_slot"])))
                else:
                    p.update(exit=t["exit"], exit_time=clock(core.slot_end(rules, t["exit_slot"])))
                held[sym] = p
                trades.append(p)
    for p in trades:
        px = p["exit"] if "exit" in p else p["mark"]
        p["pnl"] = round(p["shares"] * (px - p["entry"]), 2)
        p["net"] = round(p["pnl"] - 2 * SPREAD * p["shares"], 2)
        p["r_mult"] = round((px - p["entry"]) / p["risk"], 2)
    realized = sum(p["pnl"] for p in trades if p["result"] != "open")
    unreal = sum(p["pnl"] for p in trades if p["result"] == "open")
    closed_cash = book["budget"] + realized - sum(p["cost"] for p in trades if p["result"] == "open")
    return {"id": book["id"], "label": book["label"], "trades": trades, "skipped": skipped,
            "cash": round(closed_cash, 2), "realized": round(realized, 2), "unrealized": round(unreal, 2),
            "pnl": round(realized + unreal, 2), "net": round(sum(p["net"] for p in trades), 2),
            "traded": round(sum(p["cost"] for p in trades), 2), "budget": book["budget"],
            "risk_used": round(risk_used, 2), "variant": book["variant"]}


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def report_md(st, results, rules, local, final):
    L = ["# Paper trading day: %s" % st["date"], ""]
    L.append("%s at %s New York. Started %s; signals counted only on bars that closed after %s." % (
        "Final" if final else "So far", local.strftime("%H:%M"), st["started"], st["gate"]))
    L.append("Rule: buy when a 5-minute bar CLOSES above the 15-minute opening range (entries %s to %s), "
             "stop at the range midpoint, target 1R or 2R, out by %s. One entry per stock per day." % (
                 rules["before"].strftime("%H:%M"), rules["after"].strftime("%H:%M"), rules["flat"].strftime("%H:%M")))
    if st.get("learned"):
        L.append("Learned rules from the %s review run alongside: %s." % (
            ", ".join(sorted({v["from"] for v in st["learned"].values()})),
            "; ".join("%s: %s" % ("your rules" if g == "A" else "dummy", " + ".join(v["changes"]))
                      for g, v in sorted(st["learned"].items()))))
    if st.get("sip"):
        L.append("Stocks in play (C accounts, picked at %s, signals from the %s bar on): %s." % (
            st["sip"]["picked_at"], st["sip"]["gate"], ", ".join("%s %.1fx" % (p[0], p[1]) for p in st["sip"]["picks"])))
    L.append("Watched: %d stocks for your rules ($%g to $%g, most traded first), %d for the dummy (at least $%g, most traded). "
             "Signals: %d." % (len(st["uni_a"]), MIN_PRICE, A_MAX_PRICE, len(st["uni_b"]), MIN_PRICE,
                               len(st.get("triggers", {}))))
    L.append("")
    L.append("| account | trades | up / down | traded ($ in) | P&L | after 1c/share each way | return | ending equity |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        t = r["trades"]
        up = sum(1 for p in t if p["pnl"] > 0)
        L.append("| %s | %d | %d / %d | %s | %s | %s | %s | %s |" % (
            r["label"], len(t), up, len(t) - up, money(r["traded"]), money(r["pnl"]), money(r["net"]),
            pct(100.0 * r["pnl"] / r["budget"]), money(r["budget"] + r["pnl"])))
    for r in results:
        L += ["", "## %s" % r["label"], ""]
        if not r["trades"]:
            L.append("No trades.")
        else:
            L.append("| # | ticker | bought | shares | $ in | entry | stop | target | sold | exit | result | P&L | after 1c/share each way | R |")
            L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for i, p in enumerate(r["trades"], 1):
                px = p.get("exit", p.get("mark"))
                when = p.get("exit_time") or ("open, %s" % p.get("mark_time"))
                L.append("| %d | %s | %s | %d | %s | %.2f | %.2f | %s | %s | %.2f | %s | %s | %s | %+.2f |" % (
                    i, p["symbol"], p["entry_time"], p["shares"], money(p["cost"]), p["entry"], p["stop"], tgt(p["target"]),
                    when, px, result_label(p), money(p["pnl"]), money(p["net"]), p["r_mult"]))
            L.append("| | **total** | | | **%s** | | | | | | | **%s** | **%s** | |" % (
                money(r["traded"]), money(r["pnl"]), money(r["net"])))
        if r["skipped"]:
            why = {}
            for s in r["skipped"]:
                key = s["reason"].split(":")[0]
                why.setdefault(key, []).append(s["symbol"])
            L.append("")
            L.append("Signals not taken: %d. %s." % (len(r["skipped"]), "; ".join(
                "%s (%d): %s" % (k, len(v), ", ".join(v[:8]) + (" ..." if len(v) > 8 else "")) for k, v in why.items())))
    L += ["", "Paper trades only: nothing was placed. Entries fill at the signal bar's close and stops and targets fill exactly "
          "(or at the bar's open on a gap), so real fills would be a little worse; the 'after 1c/share each way' column "
          "shows one cent of slippage on the way in and out. Free Yahoo 5-minute bars, delayed and unofficial. "
          "Not financial advice.", ""]
    return "\n".join(L)


def run(a):
    cfg_path = os.path.join(HERE, "trader", "config.json")
    if not os.path.exists(cfg_path):
        sys.exit("No trader/config.json: set your limits first.")
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    src = Source(a)
    local = scan.to_eastern(src.now())
    day = local.date()
    rules = core.day_rules(day, cfg, scan)
    if not rules:
        sys.exit("The NYSE is closed on %s." % day)
    out_dir = os.path.join(HERE, "trader", "paper-live")
    os.makedirs(out_dir, exist_ok=True)
    st_path = os.path.join(out_dir, "%s.json" % day.isoformat())
    md_path = os.path.join(out_dir, "%s-report.md" % day.isoformat())
    if local >= rules["flat"] and (a.fresh or not os.path.exists(st_path)):
        sys.exit("It is past %s New York and no trial was started today: nothing to finish." % rules["flat"].strftime("%H:%M"))
    if os.path.exists(st_path) and not a.fresh:
        with open(st_path, encoding="utf-8") as fh:
            done = json.load(fh)
        if done.get("final"):
            say("Today's trial is already finished: %s" % os.path.relpath(md_path, HERE))
            if not a.no_learn and not os.path.exists(os.path.join(HERE, "trader", "learning", "%s.md" % day.isoformat())):
                fin = datetime.datetime.combine(day, datetime.datetime.strptime(done.get("updated", "15:56:15"), "%H:%M:%S").time())
                if (local - fin).total_seconds() < 40 * 60:
                    say("The run that finished today is probably still reviewing it; leaving it alone.")
                else:
                    learn_after(a)
            return
    lock = os.path.join(out_dir, "%s.lock" % day.isoformat())
    if src.clock is None and not take_lock(lock, src.now()):
        sys.exit("Today's trial is already running (in the background or another window). Not starting a second copy.")
    keep_awake(True)

    sip_path = os.path.join(out_dir, "%s-sip.json" % day.isoformat())
    sip_doc = sip_state(sip_path, day, rules, src, local, not a.no_sip)
    local = scan.to_eastern(src.now())

    ready = rules["open"] + datetime.timedelta(minutes=STEP * N_RANGE, seconds=SETTLE)
    if local < ready:
        say("Waiting for the opening range to finish (%s New York)..." % ready.strftime("%H:%M:%S"))
        src.sleep((ready - local).total_seconds())
        local = scan.to_eastern(src.now())

    st = None
    if os.path.exists(st_path) and not a.fresh:
        with open(st_path, encoding="utf-8") as fh:
            st = json.load(fh)
        say("Resuming today's trial from %s (started %s)." % (os.path.relpath(st_path, HERE), st["started"]))
    if st is None:
        if a.universe:
            syms = scan.symbols_from_file(a.universe)
        elif src.data is not None:
            syms = sorted(src.data)
        else:
            syms = scan.symbols_from_file(os.path.join(HERE, "trader", "universe.txt"))
        say("Ranking %d stocks by dollar volume so far (about %d requests)..." % (len(syms), -(-len(syms) // 20)))
        sp = src.spark(syms)
        liq = []
        for s, ser in sp.items():
            bars = bars_of(ser, rules, local)
            if len(bars) < N_RANGE or sorted(bars) != list(range(len(bars))):
                continue
            last = bars[max(bars)]["c"]
            vol = sum(b["v"] for b in bars.values()) if ser.get("v") else (ser.get("meta") or {}).get("regularMarketVolume") or 0
            if last >= MIN_PRICE and vol:
                liq.append((s, last, last * vol))
        liq.sort(key=lambda x: -x[2])
        uni_a = [s for s, p, _ in liq if p <= A_MAX_PRICE][:a.pick]
        uni_b = [s for s, p, _ in liq][:a.pick]
        rank = {s: round(dv) for s, _, dv in liq if s in set(uni_a) | set(uni_b)}
        union = sorted(set(uni_a) | set(uni_b), key=lambda s: -rank[s])
        say("Picked %d stocks for your rules and %d for the dummy (%d in all). Reading their opening ranges..." % (
            len(uni_a), len(uni_b), len(union)))
        orr, pre = {}, []
        for i, s in enumerate(union, 1):
            try:
                bars = bars_of(src.chart(s), rules, local)
            except (scan.ScanError, ValueError) as e:
                say("  %s: %s" % (s, e))
                continue
            rg = core.opening_range(bars, N_RANGE)
            if not rg:
                continue
            orr[s] = [rg[0], rg[1]]
            if core.first_trigger(bars, rg[0], N_RANGE, rules) is not None:
                pre.append(s)
            if i % 50 == 0:
                say("  %d of %d" % (i, len(union)))
        local = scan.to_eastern(src.now())
        gate = rules["open"] + datetime.timedelta(
            minutes=STEP * int((local - rules["open"]).total_seconds() // (STEP * 60)))
        st = {"date": day.isoformat(), "started": local.strftime("%H:%M"), "gate": gate.strftime("%H:%M"),
              "uni_a": uni_a, "uni_b": uni_b, "rank": rank, "or": orr, "pre": sorted(pre), "triggers": {},
              "learned": load_rules(day)}
        say("%d already broke out before the trial began and are left out. Watching the other %d from the %s bar on." % (
            len(pre), len(orr) - len(pre), gate.strftime("%H:%M")))
        write_atomic(st_path, json.dumps(st, indent=1))

    learned = st.get("learned") or {}
    books = make_books(cfg, learned)
    for b in books:
        if b["id"].endswith("-new"):
            say("Also running %s." % b["label"])
    cbooks = make_c_books(cfg) if sip_doc and sip_doc.get("picks") else []
    if cbooks:
        say("Also running the stocks-in-play accounts C-1 and C-2 on %d stocks." % len(sip_doc["picks"]))
        st["sip"] = {"picks": sip_doc["picks"], "gate": sip_doc["gate"], "picked_at": sip_doc["picked_at"]}
    cpicks = {p[0]: p for p in (sip_doc or {}).get("picks") or []} if cbooks else {}
    cgate = datetime.datetime.combine(day, scan.parse_hhmm(sip_doc["gate"])) if cbooks else None
    ccache = {}
    need_spy = any(b["variant"].get("spy") for b in books)
    gate = datetime.datetime.combine(day, scan.parse_hhmm(st["gate"]))
    orr, rank, pre = st["or"], st["rank"], set(st["pre"])
    uni = {"A": set(st["uni_a"]), "B": set(st["uni_b"])}
    triggers = {s: int(k) for s, k in st.get("triggers", {}).items()}
    cache, prevc, open_syms, seen = {}, {}, set(), set()
    spy_bars = {}
    say("Paper trading until %s New York. Leave this window open and the computer awake. Ctrl+C stops it; "
        "run it again to resume." % rules["flat"].strftime("%H:%M"))
    while True:
        local = scan.to_eastern(src.now())
        final = local >= rules["flat"] + datetime.timedelta(seconds=SETTLE)
        closed = int((local - rules["open"]).total_seconds() // (STEP * 60)) - 1
        last_slot = min(closed, rules["flat_slot"])
        cutoff = min(local, core.slot_end(rules, last_slot))
        try:
            armed = [s for s in orr if s not in pre and s not in triggers]
            cands = set()
            if armed and core.slot_end(rules, last_slot) > gate:
                for s, ser in src.spark(armed).items():
                    bars = bars_of(ser, rules, cutoff)
                    if core.first_trigger(bars, orr[s][0], N_RANGE, rules) is not None:
                        cands.add(s)
            need = sorted(cands | (open_syms - cands) | {s for s in triggers if s not in cache})
            for s in need:
                try:
                    ser = src.chart(s)
                    cache[s] = bars_of(ser, rules, cutoff)
                    prevc[s] = prev_close_of(ser)
                except (scan.ScanError, ValueError) as e:
                    say("  %s: %s (will retry)" % (s, e))
                    continue
                if s in cands:
                    k = core.first_trigger(cache[s], orr[s][0], N_RANGE, rules)
                    if k is None:
                        continue
                    if core.slot_end(rules, k) <= gate:
                        pre.add(s)
                    else:
                        triggers[s] = k
            if need_spy:
                try:
                    spy_bars = bars_of(src.chart("SPY"), rules, cutoff)
                except (scan.ScanError, ValueError) as e:
                    say("  SPY: %s (will retry)" % e)
        except (scan.ScanError, ValueError, OSError) as e:
            say("%s  data problem: %s. Trying again next bar." % (local.strftime("%H:%M"), e))
        results = []
        for b in books:
            r_day = core.day_rules(day, cfg, scan, b["variant"].get("cutoff"))
            sigs = {}
            for s in triggers:
                if s in cache:
                    sigs[s] = core.trade(cache[s], b["variant"], r_day, gate=gate, spy=spy_bars,
                                         prev_close=prevc.get(s), last_slot=last_slot, final=final)
            results.append(account(b, uni[b["group"]], sigs, rank, rules, last_slot))
        if cbooks:
            for s in cpicks:
                try:
                    ccache[s] = bars_of(src.chart(s), rules, cutoff)
                except (scan.ScanError, ValueError, OSError) as e:
                    say("  %s: %s (will retry)" % (s, e))
            crk = {s: p[1] for s, p in cpicks.items()}
            for b in cbooks:
                sigs = {}
                for s, p in cpicks.items():
                    if s in ccache:
                        tr = core.trade(ccache[s], b["variant"], rules, gate=cgate, last_slot=last_slot, final=final, atr=p[2])
                        if tr and not tr.get("pre"):
                            sigs[s] = tr
                results.append(account(b, set(cpicks), sigs, crk, rules, last_slot))
        open_syms = {p["symbol"] for r in results if not r["id"].startswith("C-") for p in r["trades"] if p["result"] == "open"}
        for r in results:
            for p in r["trades"]:
                key = (r["id"], p["symbol"], "in")
                if key not in seen:
                    seen.add(key)
                    say("  BUY   %-5s %-6s %d sh @ %.2f (%s)  stop %.2f  target %s" % (
                        r["id"], p["symbol"], p["shares"], p["entry"], money(p["cost"]), p["stop"], tgt(p["target"])))
                if p["result"] != "open" and (r["id"], p["symbol"], "out") not in seen:
                    seen.add((r["id"], p["symbol"], "out"))
                    say("  SELL  %-5s %-6s %d sh @ %.2f  %s at %s  P&L %s" % (
                        r["id"], p["symbol"], p["shares"], p["exit"], p["result"], p["exit_time"], money(p["pnl"])))
        say("%s  bar %s closed | signals %d | %s" % (
            local.strftime("%H:%M"), clock(core.slot_end(rules, last_slot)), len(triggers),
            " | ".join("%s %s" % (r["id"], money(r["pnl"])) for r in results)))
        st["triggers"] = triggers
        st["pre"] = sorted(pre)
        st["results"] = results
        st["updated"] = local.strftime("%H:%M:%S")
        st["final"] = final
        write_atomic(st_path, json.dumps(st, indent=1))
        write_atomic(md_path, report_md(st, results, rules, local, final))
        if src.clock is None:
            touch_lock(lock, src.now())
        if final or (a.until and local >= datetime.datetime.combine(day, scan.parse_hhmm(a.until))):
            break
        wake = core.slot_end(rules, closed + 1) + datetime.timedelta(seconds=SETTLE)
        src.sleep((wake - scan.to_eastern(src.now())).total_seconds())
    say("")
    say("Done. Report: %s" % os.path.relpath(md_path, HERE))
    for r in results:
        say("  %-32s %2d trades  traded %10s  P&L %9s  (%s)" % (
            r["label"][:32], len(r["trades"]), money(r["traded"]), money(r["pnl"]), pct(100.0 * r["pnl"] / r["budget"])))
    say("Paper trades only: nothing was placed. Not financial advice.")
    keep_awake(False)
    if src.clock is None:
        try:
            os.remove(lock)
        except OSError:
            pass
    if final and not a.no_learn:
        learn_after(a)


def learn_after(a):
    say("")
    say("Reviewing today and testing rule changes on 60 days of history (a few minutes)...")
    try:
        import learn
        learn.LOG.extend(LOG)
        learn.run(argparse.Namespace(date=None, offline=a.offline, log=None))
    except SystemExit as e:
        say("Learning step stopped: %s" % e)
        return
    except Exception as e:
        say("Learning step failed: %s. Run  python learn.py  to try again." % e)
        return
    say("")
    say("Replaying past days with what the learning knew at the time (walk-forward)...")
    try:
        import walkforward
        walkforward.run(argparse.Namespace(offline=a.offline, no_fetch=True, log=None))
        sc = __import__("scorecard").run(quiet=True)
        if sc:
            say("Updated trader/learning/scorecard.md (%d finished day(s))." % sc.get("days", 0))
    except SystemExit as e:
        say("Walk-forward stopped: %s" % e)
    except Exception as e:
        say("Walk-forward failed: %s. Run  python walkforward.py  to try again." % e)


def main(argv=None):
    p = argparse.ArgumentParser(description="Paper-trade today's opening-range breakouts live, for your rules and a $10,000 dummy.")
    p.add_argument("--pick", type=int, default=PICK, help="most-traded stocks to watch per account (default 100)")
    p.add_argument("--universe", help="symbol list (default trader/universe.txt)")
    p.add_argument("--fresh", action="store_true", help="start over instead of resuming today's trial")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.add_argument("--offline", help=argparse.SUPPRESS)
    p.add_argument("--until", help=argparse.SUPPRESS)
    p.add_argument("--log", help="also write the output to this file")
    p.add_argument("--no-learn", action="store_true", help="skip the learning step after the close")
    p.add_argument("--no-sip", action="store_true", help="skip the stocks-in-play accounts")
    a = p.parse_args(argv)
    if a.log:
        LOG.append(open(a.log, "a", encoding="utf-8"))
    run(a)


if __name__ == "__main__":
    main()
