#!/usr/bin/env python3
"""merge_master.py — 合并三层产出为一张回测主表 out/master_universe.csv

输入（out/，缺谁跳谁）：
  delist_universe.csv        宇宙层（agent1）
  ticker_history_wayback.csv ticker 层主通道（agent2）
  tickers_submissions.csv    ticker 层补漏（agent2，可缺）
  delist_events_fts.csv      25/25-NSE 事件（agent2，可缺）
  facts_coverage.csv         财报层（agent3）
退市日口径：delist_event_25=摘表公告日（参考，证券级噪声）；delist_proxy_date=末次年报日
（went-dark 的真退市在其后 0-15 个月内）；ticker_last_seen=票号最后被观察到（上限证据）。
"""
import csv
import os
import sys
from collections import defaultdict

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")


def load(name, key=None):
    p = os.path.join(OUT, name)
    if not os.path.exists(p):
        return None
    rows = list(csv.DictReader(open(p)))
    return rows


def main():
    universe = load("delist_universe.csv")
    if not universe:
        sys.exit("缺 delist_universe.csv")
    wayback = load("ticker_history_wayback.csv") or []
    subs = {r["cik"].lstrip("0"): r for r in (load("tickers_submissions.csv") or [])}
    events = load("delist_events_fts.csv") or []
    cover = {r["cik"].lstrip("0"): r for r in (load("facts_coverage.csv") or [])}

    # CIK → 最佳 ticker：wayback 里 last_seen 最大的那条
    tk_best = {}
    for r in wayback:
        c = r["cik"].lstrip("0")
        cur = tk_best.get(c)
        if cur is None or r["last_seen"] > cur["last_seen"]:
            tk_best[c] = r

    # E：XBRL 封面页摘到的票（优先级最高=最近申报实证）
    e_cover = {}
    for fn in ("tickers_xbrl_symbol.csv", "tickers_xbrl_symbol_pass1.csv"):
        for r in load(fn) or []:
            if r.get("trading_symbol"):
                e_cover[r["cik"].lstrip("0")] = r

    # 交易所官方符号目录（2008-2017）：票号 + 交易所证据
    nd_best = {}
    exch_map = {}
    exch_any = set()
    for r in load("ticker_history_nasdaqdir.csv") or []:
        if r.get("security_type") != "common":
            continue
        c = r["cik"].lstrip("0")
        exch_map[(c, r["ticker"])] = r.get("exchange", "")
        if r.get("exchange"):
            exch_any.add(c)
        cur = nd_best.get(c)
        if cur is None or r["last_seen"] > cur["last_seen"]:
            nd_best[c] = r

    ev_dates = defaultdict(list)
    for r in events:
        for c in r.get("ciks", "").split("|"):
            c = c.strip().lstrip("0")
            if c.isdigit():
                ev_dates[c].append(r["file_date"])

    path = os.path.join(OUT, "master_universe.csv")
    stats = defaultdict(int)
    year_delist = defaultdict(int)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "name", "ticker", "ticker_first_seen", "ticker_last_seen", "ticker_source",
                    "first_s1", "first_10k", "last_10k",
                    "is_delisted", "delist_event_25", "delist_proxy_date",
                    "in_fundamentals", "fy_min", "fy_max",
                    "ticker_exchange", "was_exchange_listed"])
        for r in universe:
            c = r["cik"].lstrip("0")
            tk = tk_best.get(c)
            ticker, fs, ls, src = (tk["ticker"], tk["first_seen"], tk["last_seen"], "wayback") if tk else ("", "", "", "")
            nd = nd_best.get(c)
            if nd and (not ticker or nd["last_seen"] > ls):
                ticker, fs, ls, src = nd["ticker"], nd["first_seen"], nd["last_seen"], "nasdaqdir"
            if c in e_cover:
                ticker, src = e_cover[c]["trading_symbol"], "xbrl_cover"
            elif not ticker and c in subs and subs[c].get("tickers_now"):
                ticker, src = subs[c]["tickers_now"].split("|")[0], "submissions"
            ev = max(ev_dates.get(c, [])) if ev_dates.get(c) else ""
            exch = exch_map.get((c, ticker), "")
            was_listed = 1 if (c in exch_any or ev) else 0
            cov = cover.get(c)
            w.writerow([c, r["name"], ticker, fs, ls, src,
                        r["first_s1"], r["first_10k"], r["last_10k"],
                        r["is_delisted"], ev, r["last_10k"],
                        1 if cov else 0,
                        cov["fy_min"] if cov else "", cov["fy_max"] if cov else "",
                        exch, was_listed])
            stats["rows"] += 1
            if r["is_delisted"] == "1":
                stats["delisted"] += 1
                year_delist[r["last_10k"][:4]] += 1
            if ticker:
                stats["with_ticker"] += 1
                stats["src_" + src] += 1
            if was_listed:
                stats["was_listed"] += 1
            if cov:
                stats["with_fundamentals"] += 1

    print(f"-> {path}")
    for k in ("rows", "delisted", "with_ticker", "was_listed", "with_fundamentals"):
        print(f"  {k:20s} {stats[k]:,}")
    for k in sorted(stats):
        if k.startswith("src_"):
            print(f"  {k:20s} {stats[k]:,}")
    print(f"  ticker 覆盖率 {stats['with_ticker']/max(stats['rows'],1):.1%} | 财报覆盖 {stats['with_fundamentals']/max(stats['rows'],1):.1%}")
    print("  按末次年报年份的退市分布（Top10）:")
    for y, n in sorted(year_delist.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {y}: {n:,}")


if __name__ == "__main__":
    main()
