import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core  # noqa: E402

POOL_N = 500
TOP = 20
PRE = 60
MIN_RV = 1.0
MIN_ADV = 1000000
MIN_ATR = 0.50
MIN_PRICE = 5.0
LOOK = 14
C_RULE = {"or_bars": 1, "stop": "atr", "stop_atr": 0.10, "target": None, "cutoff": "15:30"}
C_TODAY = dict(core.BASE, target=1.0)
SDIR = os.path.join(HERE, "trader", "sip")


def last_bar(bars):
    return bars[max(bars)]


def stats_before(sess, day):
    """14-day stats from the full sessions before `day`: average first 5-minute volume, average daily
    volume and the 14-day ATR. None when there is not enough history."""
    prior = sorted(d for d in sess if d < day)[-(LOOK + 1):]
    if len(prior) < LOOK + 1:
        return None
    days = prior[1:]
    v1 = sum(sess[d][0]["v"] for d in days) / float(LOOK)
    adv = sum(sum(b["v"] for b in sess[d].values()) for d in days) / float(LOOK)
    trs = []
    for i in range(1, len(prior)):
        pc = last_bar(sess[prior[i - 1]])["c"]
        bars = sess[prior[i]]
        h = max(b["h"] for b in bars.values())
        lo = min(b["l"] for b in bars.values())
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return {"v1": v1, "adv": adv, "atr": sum(trs) / float(LOOK), "last": last_bar(sess[prior[-1]])["c"],
            "asof": str(prior[-1])}


def choose(first, stats):
    """first: {sym: (open, first 5-minute volume)}. The stocks in play: price over $5, 14-day average
    volume over 1M, ATR over $0.50, opening volume at least its 14-day average, top 20 by that ratio."""
    rows = []
    for s, (o, v) in first.items():
        st = stats.get(s)
        if not st or not st["v1"] or o is None or v is None:
            continue
        if o <= MIN_PRICE or st["adv"] < MIN_ADV or st["atr"] < MIN_ATR:
            continue
        rv = v / st["v1"]
        if rv >= MIN_RV:
            rows.append((s, round(rv, 2), round(st["atr"], 4)))
    rows.sort(key=lambda x: (-x[1], x[0]))
    return rows[:TOP]


def rank_universe(scan, universe, say):
    """Every stock over $5 ranked by today's dollar volume, and the session date the ranking belongs to."""
    say("Ranking %d stocks for tomorrow's stocks-in-play pool (about %d requests)..." % (len(universe), -(-len(universe) // 20)))
    data, _ = scan.fetch_spark(list(universe), "1d", "5m", scan.PAUSE)
    rows, times = [], []
    for s, ser in data.items():
        m = ser.get("meta") or {}
        p, v = m.get("regularMarketPrice"), m.get("regularMarketVolume")
        if p and v and p >= MIN_PRICE:
            rows.append((s, p * v, p))
        if m.get("regularMarketTime"):
            times.append(m["regularMarketTime"])
    rows.sort(key=lambda x: -x[1])
    asof = scan.ts_to_eastern(sorted(times)[len(times) // 2]).date() if times else None
    return rows, asof


def build_pool(scan, universe, say):
    """Tonight's 500 most-traded stocks over $5, by today's dollar volume."""
    rows, asof = rank_universe(scan, universe, say)
    if asof:
        save_ranking(rows, asof)
    return [r[0] for r in rows[:POOL_N]]


PDIR = os.path.join(SDIR, "pools")
DAILY = os.path.join(SDIR, "daily.json.gz")
RANK_KEEP = 1500
BACKFILL_N = 1200
CLEAN = 0.9


def save_ranking(rows, asof):
    os.makedirs(PDIR, exist_ok=True)
    doc = {"asof": str(asof), "rows": [[s, round(dv), round(p, 4)] for s, dv, p in rows[:RANK_KEEP]]}
    path = os.path.join(PDIR, "%s.json" % asof)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(path + ".tmp", path)


def load_rankings():
    out = {}
    if not os.path.isdir(PDIR):
        return out
    for f in os.listdir(PDIR):
        if f.endswith(".json"):
            try:
                with open(os.path.join(PDIR, f), encoding="utf-8") as fh:
                    doc = json.load(fh)
                out[doc["asof"]] = [r[0] for r in doc["rows"]]
            except (ValueError, KeyError, OSError):
                continue
    return out


def load_daily():
    import gzip
    if not os.path.exists(DAILY):
        return None
    try:
        with gzip.open(DAILY, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_daily(doc):
    import gzip
    os.makedirs(SDIR, exist_ok=True)
    with gzip.open(DAILY + ".tmp", "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    os.replace(DAILY + ".tmp", DAILY)


def fetch_daily(scan, syms, say, rng="6mo"):
    """Daily closes and volumes: 20 a request when Yahoo's spark answer has volume, else one chart request each."""
    data, _ = scan.fetch_spark(syms[:20], rng, "1d", scan.PAUSE)
    if any(any(x for x in (ser.get("v") or []) if x) for ser in data.values()):
        data, _ = scan.fetch_spark(syms, rng, "1d", scan.PAUSE)
    else:
        say("Getting daily volume for %d stocks, one request each (once only)..." % len(syms))
        data = {}
        for i, s in enumerate(syms, 1):
            try:
                data[s] = scan.parse_chart(scan.yahoo_get(scan.chart_path(s, rng, "1d")), s)
            except (scan.ScanError, ValueError):
                pass
            time.sleep(scan.PAUSE)
            if i % 200 == 0:
                say("  %d of %d" % (i, len(syms)))
    out = {}
    for s, ser in data.items():
        t, c, v = ser.get("t") or [], ser.get("c") or [], ser.get("v") or []
        rows = {}
        for i, ts in enumerate(t):
            if i < len(c) and i < len(v) and c[i] is not None and v[i] is not None:
                rows[str(scan.ts_to_eastern(ts).date())] = [round(c[i], 4), int(v[i])]
        if rows:
            out[s] = rows
    return out


def daily_rankings(daily):
    """{session date: symbols over $5 ranked by that day's dollar volume} from the daily cache."""
    by_day = {}
    for s, rows in (daily or {}).get("bars", {}).items():
        for d, (c, v) in rows.items():
            if c and v and c >= MIN_PRICE:
                by_day.setdefault(d, []).append((s, c * v))
    return {d: [s for s, _ in sorted(r, key=lambda x: -x[1])] for d, r in by_day.items() if len(r) >= POOL_N}


def pool_for(prev_day, saved, daily_rank):
    """The 500 most-traded stocks as of the session before a day, and where that list came from."""
    k = str(prev_day)
    if k in saved:
        return saved[k][:POOL_N], "saved"
    if k in daily_rank:
        return daily_rank[k][:POOL_N], "daily"
    return None, None


def save_stats(pool, sessions, day):
    """Stats for the next trading day, from the history up to and including `day`."""
    nxt = day + datetime.timedelta(days=1)
    stats = {}
    for s in pool:
        st = stats_before(sessions.get(s, {}), nxt)
        if st:
            stats[s] = st
    os.makedirs(SDIR, exist_ok=True)
    doc = {"built": str(day), "pool": pool, "stats": stats}
    tmp = os.path.join(SDIR, "stats.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, os.path.join(SDIR, "stats.json"))
    return doc


def load_stats():
    path = os.path.join(SDIR, "stats.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError:
        return None


def live_pick(scan, src, rules, say):
    """At or after 9:35, pick today's stocks in play from last night's pool and stats."""
    doc = load_stats()
    if not doc or not doc.get("stats"):
        return None, "no stocks-in-play pool yet (it is built after the close)"
    stats = doc["stats"]
    pool = [s for s in doc["pool"] if s in stats]
    first_end = rules["open"] + datetime.timedelta(minutes=core.STEP)
    sp = src.spark(pool)
    pre = []
    for s, ser in sp.items():
        m = ser.get("meta") or {}
        v = m.get("regularMarketVolume")
        if v and stats[s]["adv"]:
            pre.append((s, v / stats[s]["adv"]))
    pre.sort(key=lambda x: -x[1])
    first = {}
    for s, _ in pre[:PRE]:
        try:
            ser = src.chart(s)
        except Exception:
            continue
        t, o, v = ser.get("t") or [], ser.get("o") or [], ser.get("v") or []
        for i, ts in enumerate(t):
            et = scan.ts_to_eastern(ts)
            if et == rules["open"] and et + datetime.timedelta(minutes=core.STEP) <= first_end:
                if i < len(o) and i < len(v) and o[i] is not None and v[i] is not None:
                    first[s] = (float(o[i]), float(v[i]))
                break
    return choose(first, stats), "%d in the pool, %d checked closely" % (len(pool), len(first))
