#!/usr/bin/env python3
"""build_tickers_merged.py — 从三条通道重建全量 CIK↔ticker 有效期表 out/tickers_all_merged.csv
通道: wayback 快照差分 / submissions 现票 / XBRL 封面票；(cik,ticker) 去重取最宽窗口，来源用 + 连接。
"""
import csv
from datetime import date
import os
from collections import defaultdict

OUT = os.path.join(os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd())), "out")
agg = {}  # (cik, ticker) -> [first_seen, last_seen, sources, exchanges]

def add(cik, tk, fs, ls, src, exch=""):
    tk = tk.strip()
    if not tk:
        return
    k = (cik, tk)
    a = agg.get(k)
    if a is None:
        agg[k] = [fs, ls, {src}, {exch} if exch else set()]
    else:
        if fs and (not a[0] or fs < a[0]):
            a[0] = fs
        if ls > a[1]:
            a[1] = ls
        a[2].add(src)
        if exch:
            a[3].add(exch)

for r in csv.DictReader(open(os.path.join(OUT, "ticker_history_wayback.csv"))):
    add(r["cik"].lstrip("0"), r["ticker"], r["first_seen"], r["last_seen"], "wayback")
for r in csv.DictReader(open(os.path.join(OUT, "tickers_submissions.csv"))):
    for tk in r.get("tickers_now", "").split("|"):
        add(r["cik"].lstrip("0"), tk, "", date.today().isoformat(), "submissions")
for fn in ("tickers_xbrl_symbol.csv", "tickers_xbrl_symbol_pass1.csv"):
    p = os.path.join(OUT, fn)
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            if r.get("trading_symbol"):
                d = r.get("filing_date", "")
                add(r["cik"].lstrip("0"), r["trading_symbol"], "", d, "xbrl_cover")
# 交易所官方符号目录（2008-2017 回填，common stock 才入表；preferred/other 留在 nasdaqdir 原文件）
p = os.path.join(OUT, "ticker_history_nasdaqdir.csv")
if os.path.exists(p):
    for r in csv.DictReader(open(p)):
        if r.get("security_type") == "common":
            add(r["cik"].lstrip("0"), r["ticker"], r["first_seen"], r["last_seen"],
                "nasdaqdir", r.get("exchange", ""))

path = os.path.join(OUT, "tickers_all_merged.csv")
with open(path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["cik", "ticker", "first_seen", "last_seen", "sources", "exchange"])
    for (cik, tk), (fs, ls, src, exch) in sorted(agg.items()):
        w.writerow([cik, tk, fs, ls, "+".join(sorted(src)), "+".join(sorted(exch))])

ciks = {c for c, _ in agg}
print(f"-> {path}: {len(agg):,} 对 / {len(ciks):,} CIK")
