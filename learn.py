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
import importlib  # noqa: E402
import core  # noqa: E402
core = importlib.reload(core)
STEP = 5
COST = 0.01
MIN_CHECK = 100
MARGIN = 0.03
CONF = 0.95
BOOT = 300
TARGETS = (1.0, 1.5, 2.0, 3.0, None)


def load(folder, name):
    spec = importlib.util.spec_from_file_location("ln_" + name, os.path.join(SKILLS, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = load("trader-scanner", "scan")


LOG = []


def say(msg):
    for fh in LOG:
        fh.write(msg + "\n")
        fh.flush()
    if sys.stdout is not None:
        print(msg, flush=True)


def money(x):
    return ("-$%s" % format(-x, ",.2f")) if x < -0.004 else ("$%s" % format(abs(x), ",.2f"))


def fnum(arr, i):
    return None if not arr or i >= len(arr) or arr[i] is None else float(arr[i])


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def sessions_of(series):
    out = {}
    t = series.get("t") or []
    o, h, l, c, v = (series.get(k) for k in ("o", "h", "l", "c", "v"))
    for i, ts in enumerate(t):
        cc = fnum(c, i)
        if cc is None:
            continue
        et = scan.ts_to_eastern(ts)
        if et.second or et.minute % STEP:
            continue
        s = scan.session(et.date())
        if not s:
            continue
        open_dt = datetime.datetime.combine(et.date(), s[0])
        close_dt = datetime.datetime.combine(et.date(), s[1])
        if et < open_dt or et >= close_dt:
            continue
        k = int((et - open_dt).total_seconds() // (STEP * 60))
        out.setdefault(et.date(), {})[k] = {"o": fnum(o, i), "h": fnum(h, i), "l": fnum(l, i), "c": cc,
                                            "v": (v[i] or 0) if v and i < len(v) else 0}
    return out


def full_days(sess):
    keep = {}
    for d, bars in sess.items():
        s = scan.session(d)
        if s[1] != scan.CLOSE:
            continue
        if len(bars) == 78 and all(b["h"] is not None and b["l"] is not None for b in bars.values()):
            keep[d] = bars
    return keep


CFG = {}


_RULES = {}


def simulate(bars, v, spy=None, prev_close=None, day=None, atr=None):
    key = (day, v.get("cutoff"))
    if key not in _RULES:
        _RULES[key] = core.day_rules(day, CFG, scan, v.get("cutoff"))
    rules = _RULES[key]
    if not rules or not bars:
        return None
    tr = core.trade(bars, v, rules, spy=spy, prev_close=prev_close, atr=atr)
    if not tr or "filtered" in tr or tr.get("pre"):
        return tr
    tr["r_net"] = tr["r"] - 2 * COST / tr["risk"]
    tr["exit_time"] = core.slot_end(rules, tr["exit_slot"]).strftime("%H:%M")
    return tr


def stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0, "win": 0.0, "avg": 0.0, "avg_gross": 0.0, "total": 0.0, "pf": 0.0, "dd": 0.0}
    rs = [x["r_net"] for x in rows]
    pos = sum(x for x in rs if x > 0)
    neg = -sum(x for x in rs if x < 0)
    eq = peak = dd = 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "win": 100.0 * sum(1 for x in rs if x > 0) / n, "avg": sum(rs) / n,
            "avg_gross": sum(x["r"] for x in rows) / n, "total": sum(rs),
            "pf": (pos / neg) if neg else float("inf"), "dd": dd}


def by_day(rows):
    out = {}
    for x in rows:
        out.setdefault(x["date"], []).append(x["r_net"])
    return out


def boot_better(cand, ref, seed=7):
    """Share of day-level resamples in which cand's average R beats ref's on the same days."""
    import random
    rnd = random.Random(seed)
    days = sorted(set(cand) | set(ref))
    if not days:
        return 0.0
    wins = 0
    for _ in range(BOOT):
        pick = [days[rnd.randrange(len(days))] for _ in days]
        cs = [x for d in pick for x in cand.get(d, [])]
        rs = [x for d in pick for x in ref.get(d, [])]
        if cs and rs and sum(cs) / len(cs) > sum(rs) / len(rs):
            wins += 1
    return wins / float(BOOT)


def boot_ci(rows_by_day, lo=0.05, hi=0.95, seed=11):
    import random
    rnd = random.Random(seed)
    days = sorted(rows_by_day)
    if not days:
        return None
    avgs = []
    for _ in range(BOOT):
        pick = [days[rnd.randrange(len(days))] for _ in days]
        xs = [x for d in pick for x in rows_by_day[d]]
        if xs:
            avgs.append(sum(xs) / len(xs))
    avgs.sort()
    if not avgs:
        return None
    return avgs[int(lo * (len(avgs) - 1))], avgs[int(hi * (len(avgs) - 1))]


BASE = core.BASE


def tlabel(t):
    return "hold to the close" if t is None else "%gR" % t


def variants():
    out = []
    for t in TARGETS:
        base = dict(BASE, target=t)
        out.append(("%s, today's rule" % tlabel(t), base, "target"))
        for name, change, fam in (
                ("stop at the range low", {"stop": "or_low"}, "stop"),
                ("30-minute opening range", {"or_bars": 6}, "range"),
                ("no entries after 12:00", {"cutoff": "12:00"}, "time"),
                ("no entries after 11:00", {"cutoff": "11:00"}, "time"),
                ("skip if over 0.5% above the range", {"max_over": 0.5}, "late"),
                ("skip if over 0.25% above the range", {"max_over": 0.25}, "late"),
                ("breakout bar volume at least 1x the range", {"min_volx": 1.0}, "volume"),
                ("breakout bar volume at least 1.5x the range", {"min_volx": 1.5}, "volume"),
                ("skip stops closer than 0.5% of price", {"min_risk_pct": 0.5}, "tight"),
                ("skip stops closer than 15 cents a share", {"min_risk_usd": 0.15}, "tight"),
                ("skip stops closer than 25 cents a share", {"min_risk_usd": 0.25}, "tight"),
                ("skip stops further than 3% of price", {"max_risk_pct": 3.0}, "wide"),
                ("only when SPY is above its own range", {"spy": "above_or"}, "market"),
                ("only when SPY is up on the day", {"spy": "up_day"}, "market"),
                ("skip stocks that gapped over 3%", {"no_gap_over": 3.0}, "gap"),
                ("stop to breakeven at +0.5R", {"be_at": 0.5}, "exit"),
                ("stop to breakeven at +1R", {"be_at": 1.0}, "exit"),
                ("trailing stop 0.5R behind the high", {"trail_r": 0.5}, "exit"),
                ("trailing stop 1R behind the high", {"trail_r": 1.0}, "exit"),
                ("exit after 1 hour if not +0.5R", {"time_stop": [12, 0.5]}, "exit"),
                ("sell half at +1R, stop to breakeven", {"partial": 1.0}, "partial")):
            if fam in ("exit", "partial") and t is not None and t <= change.get("be_at", change.get("partial", 0)):
                continue
            out.append(("%s, %s" % (tlabel(t), name), dict(base, **change), fam))
    return out


def allowed(g, row):
    v = row["variant"]
    if v.get("or_bars", 3) != 3:
        return False
    if g == "A" and row["family"] == "partial":
        return False
    return True


def backtest(data, spy, groups, var_list):
    dates = sorted({d for s in data.values() for d in s})
    if len(dates) < 6:
        return None, dates, None
    cut = dates[int(len(dates) * 2 / 3)]
    prev = {}
    for s, sess in data.items():
        ds = sorted(sess)
        for i, d in enumerate(ds):
            prev[(s, d)] = sess[ds[i - 1]][77]["c"] if i else None
    table = {}
    for g, syms in groups.items():
        for name, v, fam in var_list:
            learn, check = [], []
            for s in syms:
                for d, bars in data.get(s, {}).items():
                    tr = simulate(bars, v, spy.get(d), prev.get((s, d)), d)
                    if not tr or "filtered" in tr or tr.get("pre"):
                        continue
                    row = {"date": d, "slot": tr["slot"], "r": tr["r"], "r_net": tr["r_net"]}
                    (learn if d < cut else check).append(row)
            for rows in (learn, check):
                rows.sort(key=lambda x: (x["date"], x["slot"]))
            table[(g, name)] = {"variant": v, "family": fam, "learn": stats(learn), "check": stats(check),
                                "all": stats(learn + check), "learn_days": by_day(learn), "check_days": by_day(check)}
    return table, dates, cut


def ref_row(table, g, half="check"):
    b1, b2 = table[(g, "1R, today's rule")], table[(g, "2R, today's rule")]
    return b1 if b1[half]["avg"] >= b2[half]["avg"] else b2


def ref_avgs(table, g):
    return ref_row(table, g, "learn")["learn"]["avg"], ref_row(table, g, "check")["check"]["avg"]


def choose(table, g):
    ref_l, ref_c = ref_avgs(table, g)
    refc = ref_row(table, g, "check")["check_days"]
    ups = []
    for (gg, name), row in table.items():
        if gg != g or name in ("1R, today's rule", "2R, today's rule") or not allowed(g, row):
            continue
        dl, dc = row["learn"]["avg"] - ref_l, row["check"]["avg"] - ref_c
        if row["check"]["n"] >= MIN_CHECK and dl >= MARGIN and dc >= MARGIN:
            conf = boot_better(row["check_days"], refc)
            if conf >= CONF:
                row["conf"] = conf
                ups.append((min(dl, dc), name, row))
    ups.sort(key=lambda x: -x[0])
    return ups


def combine(v1, v2):
    out = dict(v1)
    for k, val in v2.items():
        if k in BASE and val == BASE[k] and k != "target":
            continue
        if k not in out or out[k] == BASE.get(k):
            out[k] = val
    return out


def pick_rule(table, g, ups, prev_variant, rows_for):
    """The rule to run next. Keeps yesterday's rule unless a new one is clearly better.
    rows_for(variant) -> (learn rows, check rows) for a rule that is not in the table."""
    if not ups:
        return None
    _, name, row = ups[0]
    champ = {"variant": row["variant"], "changes": [name], "learn": row["learn"], "check": row["check"],
             "check_days": row["check_days"], "conf": row.get("conf")}
    for _, name2, row2 in ups[1:]:
        if row2["family"] in ("target", row["family"]):
            continue
        combo = combine(row["variant"], row2["variant"])
        lr, cr = rows_for(combo)
        cl, cc = stats(lr), stats(cr)
        cd = by_day(cr)
        if (cc["n"] >= MIN_CHECK and cl["avg"] >= row["learn"]["avg"] + MARGIN and cc["avg"] >= row["check"]["avg"] + MARGIN
                and boot_better(cd, row["check_days"]) >= CONF):
            champ = {"variant": combo, "changes": [name, name2.split(", ", 1)[1]], "learn": cl, "check": cc,
                     "check_days": cd, "conf": boot_better(cd, ref_row(table, g)["check_days"])}
        break
    if prev_variant:
        key = json.dumps(dict(BASE, **prev_variant), sort_keys=True)
        if key == json.dumps(dict(BASE, **champ["variant"]), sort_keys=True):
            champ["kept"] = True
        else:
            lr, cr = rows_for(prev_variant)
            pl, pc = stats(lr), stats(cr)
            pd = by_day(cr)
            ref_l, ref_c = ref_avgs(table, g)
            prev_ok = (pc["n"] >= MIN_CHECK and pl["avg"] - ref_l >= MARGIN and pc["avg"] - ref_c >= MARGIN
                       and boot_better(pd, ref_row(table, g)["check_days"]) >= CONF)
            clearly = champ["check"]["avg"] >= pc["avg"] + MARGIN and boot_better(champ["check_days"], pd) >= CONF
            if prev_ok and not clearly:
                return {"variant": prev_variant, "changes": None, "learn": pl, "check": pc, "check_days": pd,
                        "kept": True}
    return champ


def review_today(st, data, spy, groups):
    today = datetime.date.fromisoformat(st["date"])
    out = {"trades": [], "skipped": []}
    seen = {}
    for r in st.get("results", []):
        v = r.get("variant") or dict(BASE, target=float(r["id"].split("-")[1].rstrip("R")))
        for p in r["trades"]:
            bars = data.get(p["symbol"], {}).get(today)
            tr = simulate(bars, v, spy.get(today), None, today) if bars and len(bars) >= 77 else None
            row = {"book": r["id"], "symbol": p["symbol"], "time": p["entry_time"], "shares": p["shares"],
                   "entry": p["entry"], "stop": p["stop"], "target": p["target"], "result": p["result"],
                   "exit": p.get("exit", p.get("mark")), "pnl": p["pnl"], "r": p["r_mult"]}
            if tr and "filtered" not in tr:
                row.update(mfe_r=round(tr["mfe_r"], 2), mae_r=round(tr["mae_r"], 2),
                           after_best_r=round(tr["after_exit_best_r"], 2), feat=tr["feat"])
                row["lesson"] = lesson(row)
            out["trades"].append(row)
        for s in r["skipped"]:
            key = (r["id"], s["symbol"])
            if key in seen:
                continue
            seen[key] = 1
            bars = data.get(s["symbol"], {}).get(today)
            tr = simulate(bars, v, spy.get(today), None, today) if bars and len(bars) >= 77 else None
            out["skipped"].append({"book": r["id"], "symbol": s["symbol"], "time": s["time"], "reason": s["reason"],
                                   "would_r": None if not tr or "filtered" in tr else round(tr["r"], 2),
                                   "would_result": None if not tr or "filtered" in tr else tr["result"]})
    return out


def lesson(row):
    if row["result"] == "stop":
        if row.get("after_best_r", 0) >= 1.0:
            return "stopped out, then the price went on to +%.1fR: the stop was too tight for this stock's swings" % row["after_best_r"]
        if row.get("mfe_r", 0) < 0.3:
            return "false breakout: it never got more than %.1fR in its favour before the stop" % row["mfe_r"]
        return "went +%.1fR first, then reversed to the stop" % row["mfe_r"]
    if row["result"] == "target":
        if row.get("after_best_r", 0) >= 2.0:
            return "target hit, but the price kept going to +%.1fR: the target was close" % row["after_best_r"]
        return "clean win at the target"
    if row["result"] == "time":
        return "time stop: it had not made +0.5R after an hour, sold at %+.1fR" % row["r"]
    if row["result"] in ("flat", "open"):
        if row.get("mfe_r", 0) >= 0.8 and row["r"] < 0.3:
            return "was up +%.1fR, gave most of it back by the close" % row["mfe_r"]
        if row.get("mfe_r", 0) < 0.3:
            return "went nowhere all day"
        return "drifted, closed at %+.1fR" % row["r"]
    return ""


def fetch(syms, a, label="stocks"):
    """Bars for each symbol, merged with trader/history. Only 5 days are downloaded when the saved
    history is recent, so after the first night this is quick."""
    if a.offline:
        d, _ = scan.load_offline(a.offline)
        return {s: d[s] for s in syms if s in d}
    old = load_archive(syms)
    out, fresh = {}, {}
    now = time.time()
    for i, s in enumerate(syms, 1):
        o = old.get(s)
        rng = "5d" if o and o.get("t") and now - o["t"][-1] < 6 * 86400 else "60d"
        try:
            fresh[s] = scan.parse_chart(scan.yahoo_get(scan.chart_path(s, rng, "5m")), s)
        except (scan.ScanError, ValueError) as e:
            say("  %s: %s" % (s, e))
        time.sleep(scan.PAUSE)
        if i % 100 == 0:
            say("  %d of %d %s" % (i, len(syms), label))
    try:
        out = archive(fresh, syms, old=old)
    except OSError as e:
        say("History not saved: %s" % e)
        out = {s: merge(old.get(s), fresh.get(s)) for s in syms if old.get(s) or fresh.get(s)}
    return out


def load_archive(syms):
    import gzip
    hdir = os.path.join(HERE, "trader", "history")
    out = {}
    for s in syms:
        path = os.path.join(hdir, "%s.json.gz" % s.replace(".", "-"))
        if os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    out[s] = json.load(fh)
            except (OSError, ValueError):
                pass
    return out


def merge(old, new):
    rows = {}
    for s in (old, new):
        if not s:
            continue
        t = s.get("t") or []
        cols = [s.get(k) for k in ("o", "h", "l", "c", "v")]
        for i, ts in enumerate(t):
            rows[ts] = tuple(c[i] if c and i < len(c) else None for c in cols)
    ts = sorted(rows)
    out = {"t": ts, "meta": (new or old or {}).get("meta") or {}}
    for j, k in enumerate(("o", "h", "l", "c", "v")):
        out[k] = [rows[x][j] for x in ts]
    return out


def archive(fresh, syms, save=True, old=None):
    import gzip
    hdir = os.path.join(HERE, "trader", "history")
    os.makedirs(hdir, exist_ok=True)
    old = old if old is not None else load_archive(syms)
    out = {}
    for s in syms:
        ser = merge(old.get(s), fresh.get(s))
        if not ser["t"]:
            continue
        out[s] = ser
        if save and s in fresh:
            path = os.path.join(hdir, "%s.json.gz" % s.replace(".", "-"))
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(ser, fh, separators=(",", ":"))
            os.replace(tmp, path)
    return out


def ci_text(ci):
    return "n/a" if not ci else "%+.3f to %+.3fR" % ci


def fmt_row(label, s):
    return "| %s | %d | %.0f%% | %+.3fR | %+.3fR | %+.1fR | %s | %.1fR |" % (
        label, s["n"], s["win"], s["avg_gross"], s["avg"], s["total"],
        "inf" if s["pf"] == float("inf") else "%.2f" % s["pf"], s["dd"])


def run(a):
    with open(os.path.join(HERE, "trader", "config.json"), encoding="utf-8") as fh:
        CFG.update(json.load(fh))
    live = os.path.join(HERE, "trader", "paper-live")
    day = a.date or max((f[:10] for f in os.listdir(live) if f.endswith(".json") and len(f) == 15), default=None)
    if not day:
        sys.exit("No paper trading day in trader/paper-live yet.")
    with open(os.path.join(live, "%s.json" % day), encoding="utf-8") as fh:
        st = json.load(fh)
    if not st.get("final"):
        say("Note: %s's paper run is not final yet (last update %s); open trades are marked, not closed." % (
            day, st.get("updated")))
    groups = {"A": list(st["uni_a"]), "B": list(st["uni_b"])}
    syms = sorted(set(groups["A"]) | set(groups["B"]))
    say("Getting 5-minute bars for %d stocks and SPY (60 days the first time, then 5 days a night)..." % len(syms))
    raw = fetch(syms + ["SPY"], a)
    data = {s: full_days(sessions_of(raw[s])) for s in raw}
    spy = data.pop("SPY", {})
    alld = sorted({d for s in data.values() for d in s})
    nd = getattr(a, "days", 60) or 60
    if len(alld) > nd:
        keep = set(alld[-nd:])
        data = {s: {d: b for d, b in sess.items() if d in keep} for s, sess in data.items()}
        spy = {d: b for d, b in spy.items() if d in keep}
    say("Got %d stocks, %d full sessions." % (len(data), len({d for s in data.values() for d in s})))

    every = {s: sessions_of(raw[s]) for s in raw}
    rev = review_today(st, every, every.get("SPY", {}), groups)
    var_list = variants()
    say("Testing %d rules..." % len(var_list))
    table, dates, cut = backtest(data, spy, groups, var_list)
    if table is None:
        sys.exit("Not enough history to test rules (%d sessions)." % len(dates))
    picks = {g: choose(table, g) for g in groups}
    rules = {"date": day, "made": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
             "learn_days": "%s to %s" % (dates[0], [d for d in dates if d < cut][-1]),
             "check_days": "%s to %s" % (cut, dates[-1]), "challengers": {},
             "standard": "at least %d check trades, +%.2fR a trade on both halves, better in %d%% of day resamples, "
                         "yesterday's rule kept unless the new one is clearly better" % (MIN_CHECK, MARGIN, CONF * 100)}
    prev_ = {}
    for s, sess in data.items():
        ds = sorted(sess)
        for i, d in enumerate(ds):
            prev_[(s, d)] = sess[ds[i - 1]][77]["c"] if i else None
    old_rules = {}
    rpath = os.path.join(HERE, "trader", "learning", "rules.json")
    if os.path.exists(rpath):
        try:
            with open(rpath, encoding="utf-8") as fh:
                doc = json.load(fh)
            if doc.get("date", "") < day:
                old_rules = doc.get("challengers") or {}
        except ValueError:
            old_rules = {}
    for g in groups:
        def rows_for(v, g=g):
            lr, cr = [], []
            for s in groups[g]:
                for d, bars in data.get(s, {}).items():
                    tr = simulate(bars, v, spy.get(d), prev_.get((s, d)), d)
                    if tr and "filtered" not in tr and not tr.get("pre"):
                        (lr if d < cut else cr).append({"date": d, "slot": tr["slot"], "r": tr["r"], "r_net": tr["r_net"]})
            return sorted(lr, key=lambda x: (x["date"], x["slot"])), sorted(cr, key=lambda x: (x["date"], x["slot"]))
        prev_v = (old_rules.get(g) or {}).get("variant")
        c = pick_rule(table, g, picks[g], prev_v, rows_for)
        if not c:
            continue
        if c.get("kept"):
            c["changes"] = (old_rules.get(g) or {}).get("changes") or ["yesterday's rule"]
        lr, cr = rows_for(c["variant"])
        ci = boot_ci(by_day(lr + cr))
        rules["challengers"][g] = {"variant": c["variant"], "changes": c["changes"], "learn": c["learn"],
                                   "check": c["check"], "kept": bool(c.get("kept")), "ci": ci}

    md = report(st, rev, table, picks, groups, dates, cut, rules)
    ldir = os.path.join(HERE, "trader", "learning")
    os.makedirs(ldir, exist_ok=True)
    write_atomic(os.path.join(ldir, "%s.md" % day), md)
    write_atomic(os.path.join(ldir, "rules.json"), json.dumps(rules, indent=1, default=str))
    write_atomic(os.path.join(ldir, "rules-%s.json" % day), json.dumps(rules, indent=1, default=str))
    jpath = os.path.join(ldir, "journal.jsonl")
    old = []
    if os.path.exists(jpath):
        with open(jpath, encoding="utf-8") as fh:
            old = [ln for ln in fh if ln.strip() and json.loads(ln).get("date") != day]
    new = [json.dumps(dict(t, date=day), default=str) + "\n" for t in rev["trades"]]
    write_atomic(jpath, "".join(old + new))
    say("")
    say("Wrote trader/learning/%s.md, rules.json and journal.jsonl." % day)
    for g in groups:
        b1, b2 = table[(g, "1R, today's rule")]["all"], table[(g, "2R, today's rule")]["all"]
        say("  %s: today's rule %+.3fR a trade after costs at 1R, %+.3fR at 2R (%d past signals)." % (
            "Your rules" if g == "A" else "Dummy", b1["avg"], b2["avg"], b1["n"]))
        c = rules["challengers"].get(g)
        if c:
            say("    %s for tomorrow: %s (check days %+.3fR a trade, %d trades)" % (
                "kept" if c["kept"] else "new rule", " + ".join(c["changes"]), c["check"]["avg"], c["check"]["n"]))
        else:
            say("    no change passed the stricter test: tomorrow runs today's rule only")
    try:
        import scorecard
        sc = scorecard.run(quiet=True)
        if sc:
            say("Updated trader/learning/scorecard.md (%d finished day(s))." % sc.get("days", 0))
    except Exception as e:
        say("Scorecard not updated: %s" % e)


def report(st, rev, table, picks, groups, dates, cut, rules):
    L = ["# What the %s paper day taught us" % st["date"], ""]
    L.append("History used: %d full sessions, %s to %s. Rules were judged on the first two thirds (learn, to %s) and "
             "had to hold up on the last third (check, %s on), after 1 cent a share each way." % (
                 len(dates), dates[0], dates[-1], [d for d in dates if d < cut][-1], cut))
    L.append("A change only counts if it has at least %d check trades, beats the better of today's 1R and 2R rules by "
             "+%.2fR a trade on both halves, and beats it in at least %d%% of %d resamples of the check days. "
             "Yesterday's learned rule is kept unless a new one is clearly better." % (MIN_CHECK, MARGIN, CONF * 100, BOOT))
    L += ["", "## Bottom line", ""]
    for g, label in (("A", "Your $5 to $10 stocks"), ("B", "The most-traded stocks")):
        r1, r2 = table[(g, "1R, today's rule")], table[(g, "2R, today's rule")]
        b1, b2 = r1["all"], r2["all"]
        ci1 = boot_ci({**{d: v for d, v in r1["learn_days"].items()}, **r1["check_days"]})
        line = ("- **%s:** over %d past signals, today's rule averaged %+.3fR a trade after costs at 1R (90%% range %s) "
                "and %+.3fR at 2R (before costs %+.3fR and %+.3fR; the 1 cent a share each way cost %.3fR a trade at 1R)." % (
                    label, b1["n"], b1["avg"], ci_text(ci1), b2["avg"], b1["avg_gross"], b2["avg_gross"],
                    b1["avg_gross"] - b1["avg"]))
        c = rules["challengers"].get(g)
        if c:
            line += " %s: %s, %+.3fR a trade on the check days (90%% range over all days %s)%s." % (
                "Kept from yesterday" if c.get("kept") else "New learned rule", " + ".join(c["changes"]), c["check"]["avg"],
                ci_text(c.get("ci")), ", still a loss after costs, so it is less bad, not good" if c["check"]["avg"] < 0 else "")
        else:
            line += " No change passed the test."
        if max(b1["avg"], b2["avg"]) < 0 and (not c or c["check"]["avg"] < 0):
            line += " On this evidence the setup loses money here; do not trade it with real money."
        L.append(line)
    L += ["", "## Today's trades: what went right and wrong", ""]
    L.append("| account | ticker | bought | result | R | best it got (R) | worst it got (R) | best after exit (R) | over range at entry | stop % of price | volume vs range | lesson |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for t in rev["trades"]:
        f = t.get("feat") or {}
        L.append("| %s | %s | %s | %s | %+.2f | %s | %s | %s | %s | %s | %s | %s |" % (
            t["book"], t["symbol"], t["time"], t["result"], t["r"],
            "%+.2f" % t["mfe_r"] if "mfe_r" in t else "", "%+.2f" % t["mae_r"] if "mae_r" in t else "",
            "%+.2f" % t["after_best_r"] if "after_best_r" in t else "",
            "%.2f%%" % f["pct_over"] if f else "", "%.2f%%" % f["risk_pct"] if f.get("risk_pct") is not None else "",
            "%.1fx" % f["vol_x"] if f.get("vol_x") is not None else "", t.get("lesson", "no full bars for today")))
    losers = [t for t in rev["trades"] if t["pnl"] < 0]
    if losers:
        L += ["", "Losers: %d. %s" % (len(losers), "; ".join("%s %s: %s" % (t["book"], t["symbol"], t.get("lesson", "")) for t in losers))]
    if rev["skipped"]:
        would = [s for s in rev["skipped"] if s["would_r"] is not None]
        if would:
            tot = sum(s["would_r"] for s in would)
            L += ["", "Signals the accounts skipped (limits or cash): %d. Had each been taken alone they would have "
                  "made %+.1fR in total (%d up, %d down)." % (len(would), tot, sum(1 for s in would if s["would_r"] > 0),
                                                            sum(1 for s in would if s["would_r"] <= 0))]
    for g, label in (("A", "your $5 to $10 stocks"), ("B", "the most-traded stocks")):
        L += ["", "## 60-day test on %s (%d stocks), every signal, 1 trade per stock per day" % (label, len(groups[g])), ""]
        L.append("| rule | trades | win rate | avg R before costs | avg R after costs | total R | profit factor | worst drawdown |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for t in TARGETS:
            row = table[(g, "%s, today's rule" % tlabel(t))]
            L.append(fmt_row("%s, today's rule (all days)" % tlabel(t), row["all"]))
        ref_l, ref_c = ref_avgs(table, g)
        L += ["", "Changes that passed (both halves, %d+ check trades, better in %d%%+ of resamples):" % (MIN_CHECK, CONF * 100), ""]
        L.append("| rule | learn: avg R | vs today | check: avg R | vs today | check trades | check win rate | resamples better |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        if picks[g]:
            for _, name, row in picks[g][:8]:
                L.append("| %s | %+.3f | %+.3f | %+.3f | %+.3f | %d | %.0f%% | %.0f%% |" % (
                    name, row["learn"]["avg"], row["learn"]["avg"] - ref_l, row["check"]["avg"],
                    row["check"]["avg"] - ref_c, row["check"]["n"], row["check"]["win"], 100 * row.get("conf", 0)))
        else:
            L.append("| none passed | | | | | | | |")
        near = []
        for (gg, name), row in table.items():
            if gg != g or not allowed(g, row) or "today's rule" in name:
                continue
            dl, dc = row["learn"]["avg"] - ref_l, row["check"]["avg"] - ref_c
            if dl > 0 and dc > 0 and not any(name == x[1] for x in picks[g]):
                near.append((min(dl, dc), name, row))
        near.sort(key=lambda x: -x[0])
        if near:
            L += ["", "Better on both halves but not proven (too few trades or not consistent enough): " + "; ".join(
                "%s (%+.3fR, %d check trades)" % (n, row["check"]["avg"] - ref_c, row["check"]["n"]) for _, n, row in near[:6]) + "."]
        hurt = []
        for (gg, name), row in table.items():
            if gg != g or row["family"] == "target":
                continue
            base = table[(g, "%s, today's rule" % tlabel(row["variant"]["target"]))]
            dl = row["learn"]["avg"] - base["learn"]["avg"]
            dc = row["check"]["avg"] - base["check"]["avg"]
            name = name.split(", ", 1)[1] if row["variant"]["target"] == 1.0 else name
            if dl <= -MARGIN and dc <= -MARGIN and row["variant"]["target"] == 1.0:
                hurt.append("%s (%+.3fR learn, %+.3fR check)" % (name, dl, dc))
        if hurt:
            L += ["", "Changes that made it worse on both halves: " + "; ".join(hurt[:8]) + "."]
    L += ["", "## Tomorrow's paper run", ""]
    if rules["challengers"]:
        L.append("These run side by side with today's rule tomorrow, on the same signals. Your real rules in "
                 "trader/config.json are not changed; that is your call after a few days of results.")
        L.append("")
        for k, c in rules["challengers"].items():
            L.append("- **%s**: %s%s. On the check days it averaged %+.3fR a trade after costs over %d trades (win rate %.0f%%)." % (
                "Your rules ($5 to $10 stocks)" if k == "A" else "The $10,000 dummy", " + ".join(c["changes"]),
                " (kept from yesterday)" if c.get("kept") else "", c["check"]["avg"], c["check"]["n"], c["check"]["win"]))
    else:
        L.append("No change held up on both halves, so tomorrow runs today's rule again. That is a result too: the "
                 "losses were not something a simple filter fixes.")
    L += ["", "## Read this with care", "",
          "- One day is noise. The 60-day test is the evidence; today's trades only point at what to test.",
          "- The stock lists are today's most-traded names, so the history favours stocks that are busy now.",
          "- Fills are idealized (signal-bar close, exact stops and targets, 1 cent a share of slippage each way).",
          "- A rule that looks best on past bars can still stop working. That is why it is paper-tested first.",
          "- Not financial advice.", ""]
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description="Review a paper trading day and test rule changes on 60 days of 5-minute bars.")
    p.add_argument("--date", help="the paper day to review (default: the latest)")
    p.add_argument("--offline", help=argparse.SUPPRESS)
    p.add_argument("--days", type=int, default=60, help="most recent full sessions to test on (default 60)")
    p.add_argument("--log", help="also write the output to this file")
    a = p.parse_args(argv)
    if a.log:
        LOG.append(open(a.log, "a", encoding="utf-8"))
    run(a)


if __name__ == "__main__":
    main()
