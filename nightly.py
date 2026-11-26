import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import learn  # noqa: E402
import sip  # noqa: E402
import month  # noqa: E402

scan = learn.scan
say = month.say
CHUNK = 25


def rank(universe):
    say("Ranking %d stocks by today's dollar volume (about %d requests)..." % (len(universe), -(-len(universe) // 20)))
    for interval in ("1d", "5m"):
        data, _ = scan.fetch_spark(list(universe), "1d", interval, scan.PAUSE)
        rows, times = [], []
        for s, ser in data.items():
            m = ser.get("meta") or {}
            p, v = m.get("regularMarketPrice"), m.get("regularMarketVolume")
            if p and v and p >= sip.MIN_PRICE:
                rows.append((s, p * v, p))
            if m.get("regularMarketTime"):
                times.append(m["regularMarketTime"])
        if len(rows) >= 2 * sip.POOL_N:
            break
        say("  only %d stocks came back with volume; trying again with 5-minute bars" % len(rows))
    rows.sort(key=lambda x: -x[1])
    asof = scan.ts_to_eastern(sorted(times)[len(times) // 2]).date() if times else None
    return rows, asof


def refresh(pool, extra, nxt):
    """Tops up the saved 5-minute history a few stocks at a time and keeps only the stats the morning pick needs."""
    stats = {}
    syms = list(dict.fromkeys(list(pool) + list(extra) + ["SPY"]))
    say("Updating 5-minute history for %d stocks (60 days the first time, then 5 days a night)..." % len(syms))
    for i in range(0, len(syms), CHUNK):
        part = syms[i:i + CHUNK]
        got = learn.fetch(part, argparse.Namespace(offline=None), label="stocks")
        for s in part:
            if s in got and s in pool:
                st = sip.stats_before(learn.full_days(learn.sessions_of(got[s])), nxt)
                if st:
                    stats[s] = st
        del got
        if (i // CHUNK + 1) % 8 == 0:
            say("  %d of %d" % (min(i + CHUNK, len(syms)), len(syms)))
    return stats


def summary(mdir, cfg):
    led = month.load_json(os.path.join(mdir, "ledger.json"), {"days": []})
    days = led.get("days", [])
    L = ["# $100,000 paper month: summary", ""]
    if not days:
        return "\n".join(L + ["No finished days yet.", ""])
    last = days[-1]
    spy_end = (last.get("spy") or {}).get("equity", cfg["start_equity"])
    trades = [p for d in days for p in d["closed"]]
    wins = [p for p in trades if p["pnl"] - p["costs"] > 0]
    longs = [p for p in trades if p["side"] > 0]
    shorts = [p for p in trades if p["side"] < 0]
    charges = sum(d["charges"]["margin_interest"] + d["charges"]["borrow_fee"] for d in days)
    costs = sum(p["costs"] for p in trades)
    peak, dd = cfg["start_equity"], 0.0
    for d in days:
        peak = max(peak, d["equity_end"])
        dd = min(dd, d["equity_end"] / peak - 1)
    L += ["%d trading day(s), %s to %s. Paper money only." % (len(days), days[0]["date"], last["date"]), "",
          "| | account | SPY buy-and-hold |", "| --- | --- | --- |",
          "| started with | %s | %s |" % (month.money(cfg["start_equity"]), month.money(cfg["start_equity"])),
          "| now | %s | %s |" % (month.money(last["equity_end"]), month.money(spy_end)),
          "| return | %+.2f%% | %+.2f%% |" % (100.0 * (last["equity_end"] / cfg["start_equity"] - 1),
                                            100.0 * (spy_end / cfg["start_equity"] - 1)),
          "| worst drop from a high | %.2f%% | |" % (100 * dd), "",
          "| trades closed | win rate | long trades (P&L after costs) | short trades (P&L after costs) | share costs | overnight charges |",
          "| --- | --- | --- | --- | --- | --- |",
          "| %d | %.0f%% | %d (%s) | %d (%s) | %s | %s |" % (
              len(trades), 100.0 * len(wins) / len(trades) if trades else 0, len(longs),
              month.money(sum(p["pnl"] - p["costs"] for p in longs)), len(shorts),
              month.money(sum(p["pnl"] - p["costs"] for p in shorts)), month.money(costs), month.money(charges)), "",
          "## Every day", "",
          "| day | start | end | P&L | return | opened | held overnight | SPY | ahead of SPY |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for d in days:
        se = (d.get("spy") or {}).get("equity", 0.0)
        L.append("| %s | %s | %s | %s | %+.2f%% | %d | %d | %s | %s |" % (
            d["date"], month.money(d["equity_start"]), month.money(d["equity_end"]), month.money(d["pnl"]), d["ret_pct"],
            d["n_opened"], len(d["positions"]), month.money(se), month.money(d["equity_end"] - se)))
    by = {}
    for p in trades:
        b = by.setdefault(p["symbol"], [0, 0.0])
        b[0] += 1
        b[1] += p["pnl"] - p["costs"]
    if by:
        L += ["", "## By stock (after costs)", "", "| ticker | trades | P&L |", "| --- | --- | --- |"]
        for s, (n, x) in sorted(by.items(), key=lambda kv: kv[1][1]):
            L.append("| %s | %d | %s |" % (s, n, month.money(x)))
    L += ["", "Paper trades only. Not financial advice.", ""]
    return "\n".join(L)


def run(a):
    now = scan.to_eastern(scan.parse_now(None))
    a.dry = a.dry or now.date().isoformat() < month.settings(month.month_dir(False))["start_date"]
    mdir = month.month_dir(a.dry)
    cfg = month.settings(mdir)
    if not a.force and not scan.session(now.date()):
        say("Market closed today; nothing to update.")
        return
    universe = scan.symbols_from_file(os.path.join(HERE, "trader", "universe.txt"))
    rows, asof = rank(universe)
    if not rows:
        say("No ranking came back; keeping yesterday's pool.")
        return
    if asof:
        sip.save_ranking(rows, asof)
    pool = [r[0] for r in rows[:sip.POOL_N]]
    nxt = month.next_session(asof or now.date())
    led = month.load_json(os.path.join(mdir, "ledger.json"), {"days": []})
    held = [p["symbol"] for p in (led["days"][-1]["positions"] if led.get("days") else [])]
    stats = refresh(pool, held, nxt)
    doc = {"built": str(asof), "pool": pool, "stats": stats}
    path = os.path.join(sip.SDIR, "stats.json")
    month.write_atomic(path, json.dumps(doc))
    say("Saved the stocks-in-play pool for %s: %d stocks with stats." % (nxt, len(stats)))
    month.write_atomic(os.path.join(mdir, "summary.md"), summary(mdir, cfg))
    say("Wrote trader/%s/summary.md." % os.path.basename(mdir))


def main(argv=None):
    p = argparse.ArgumentParser(description="After the close: rebuild tomorrow's stocks-in-play pool and the month summary.")
    p.add_argument("--dry", action="store_true", help="summarise the rehearsal folder")
    p.add_argument("--force", action="store_true", help="run even on a day the market was closed")
    p.add_argument("--log", help="also write the output to this file")
    a = p.parse_args(argv)
    if a.log:
        month.LOG.append(open(a.log, "a", encoding="utf-8"))
    t0 = time.time()
    run(a)
    say("Done in %.0f minutes." % ((time.time() - t0) / 60))


if __name__ == "__main__":
    main()
