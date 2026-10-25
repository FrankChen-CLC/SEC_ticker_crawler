#!/usr/bin/env python3
"""nasdaqdir_verify.py — 校验 SymDir 回填结果
- 2008 危机标志票 (LEH/CFC/WB/WM/BSC) 在快照中的出现/消失
- 覆盖率 (delist_companies, 分退市年段, 相对 tickers_all_merged 的新增)
- 与现有票号表的重叠/同一 CIK 不同 ticker/同一 ticker 不同 CIK 冲突
- 随机+定向抽样 8 条命中供人工核对
输出打印到 stdout 并追加 out/nasdaqdir.log
用法: python3 -I scripts/nasdaqdir_verify.py
"""
import csv
import json
import os
import random
import time
from collections import Counter, defaultdict

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
DL = os.path.join(BASE, "nasdaqdir")
OUT = os.path.join(BASE, "out")
LOG = os.path.join(OUT, "nasdaqdir.log")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def main():
    log("=== nasdaqdir_verify start ===")
    parsed = list(csv.DictReader(open(os.path.join(DL, "parsed_rows.csv"))))
    hist = list(csv.DictReader(open(os.path.join(OUT, "ticker_history_nasdaqdir.csv"))))
    comps = list(csv.DictReader(open(os.path.join(OUT, "delist_companies.csv"))))
    merged = list(csv.DictReader(open(os.path.join(OUT, "tickers_all_merged.csv"))))
    comp_by_cik = {r["cik"]: r for r in comps}
    merged_by_cik = defaultdict(list)
    for r in merged:
        merged_by_cik[r["cik"].lstrip("0")].append(r)

    # ---- A. 规模 ----
    snaps = Counter((r["source"], r["snapshot_date"]) for r in parsed)
    per_src = Counter(r["source"] for r in parsed)
    log(f"[A] parsed rows={len(parsed)} 快照文件={len(snaps)} by source={dict(per_src)}")
    # 按 source 统计最早/最晚
    for src in per_src:
        ds = sorted(d for (s, d) in snaps if s == src)
        log(f"[A] {src}: {len(ds)} snaps, {ds[0]} .. {ds[-1]}")
    for name in ("nasdaqlisted", "otherlisted"):
        cdxp = os.path.join(DL, f"cdx_{name}.json")
        if not os.path.exists(cdxp):
            continue
        expected = {r[0] for r in json.load(open(cdxp))[1:] if len(r) > 1 and r[1] == "200"}
        rawd = os.path.join(DL, "raw", name)
        have = {f[:-4] for f in os.listdir(rawd) if f.endswith(".txt")}
        missing = sorted(expected - have)
        log(f"[A] {name} CDX200={len(expected)} 已下载={len(have)} 缺失={len(missing)} {missing}")
    parsed_ne = [r for r in parsed if r["etf_flag"] != "Y"]
    common_rows = [r for r in parsed_ne if r["security_type"] == "common"]
    log(f"[A] parsed 去ETF行={len(parsed_ne)} 总票号(去ETF)={len({r['symbol'] for r in parsed_ne})} "
        f"common票号={len({r['symbol'] for r in common_rows})} 命中行={len(hist)} "
        f"命中CIK={len({r['cik'] for r in hist})}")
    log(f"[A] 命中 security_type: " + str(Counter(r["security_type"] for r in hist)))
    log(f"[A] 命中 match_type: " + str(Counter(r["match_type"] for r in hist)))
    log(f"[A] 命中 exchange: " + str(Counter(r["exchange"] for r in hist)))

    # ---- B. 2008 危机标志票 (按公司名而非票号, 因为票号会被复用) ----
    marks = {"LEH": ("Lehman", "LEHMAN"), "CFC": ("Countrywide", "COUNTRYWIDE"),
             "WB": ("Wachovia", "WACHOVIA"), "WM": ("Washington Mutual", "WASHINGTON MUTUAL"),
             "BSC": ("Bear Stearns", "BEAR STEARNS")}
    all_dates = sorted({r["snapshot_date"] for r in parsed})
    for sym, (label, kw) in marks.items():
        rows = [r for r in parsed if r["symbol"] == sym]
        own = [r for r in rows if kw in r["raw_name"].upper()]
        if own:
            dates = sorted({r["snapshot_date"] for r in own})
            exch = Counter(r["exchange"] for r in own)
            names = sorted({r["raw_name"] for r in own})[:3]
            after = [d for d in all_dates if d > dates[-1]]
            nextd = after[0] if after else None
            still = nextd and any(r["symbol"] == sym and r["snapshot_date"] == nextd for r in own)
            log(f"[B] {sym} ({label}): 公司名记录 {len(dates)} 快照 {dates[0]}..{dates[-1]} "
                f"exchange={dict(exch)} names={names}; 之后最近快照={nextd} 仍出现={'是' if still else '否'}")
        else:
            log(f"[B] {sym} ({label}): 名称含 '{kw}' 的记录 0 条 "
                f"(该票号在快照中共 {len(rows)} 行, 若存在均为复用: "
                f"{sorted({r['raw_name'] for r in rows})[:3]})")
        # 票号整体出现区间（含复用）
        if rows:
            ds = sorted({r["snapshot_date"] for r in rows})
            log(f"[B] {sym} 票号总区间 {ds[0]}..{ds[-1]} 快照数={len(ds)}")

    # ---- C. 覆盖率 ----
    have_before = {c for c in comp_by_cik if c in merged_by_cik}
    have_new = {r["cik"] for r in hist}
    have_new_clean = {c.lstrip("0") for c in have_new}
    added = have_new_clean - have_before
    total_after = have_before | have_new_clean
    log(f"[C] delist_companies={len(comps)} 原有票号覆盖={len(have_before)} ({len(have_before)/len(comps):.1%})")
    log(f"[C] SymDir 命中 CIK={len(have_new_clean)}; 其中新增={len(added)}; "
        f"合并后总覆盖={len(total_after)} ({len(total_after)/len(comps):.1%})")

    def cohort_year(r):
        d = r.get("delist_form_date") or ""
        if len(d) >= 4:
            return int(d[:4])
        d = r.get("last_10k") or ""
        return int(d[:4]) if len(d) >= 4 else None

    buckets = [(2008, 2016), (2017, 2025), (1993, 2007)]
    for lo, hi in buckets:
        rows = [r for r in comps if cohort_year(r) and lo <= cohort_year(r) <= hi]
        b = sum(1 for r in rows if r["cik"] in have_before)
        a = sum(1 for r in rows if r["cik"] in added)
        t = sum(1 for r in rows if r["cik"] in total_after)
        log(f"[C] 退市年 {lo}-{hi}: {len(rows)} 家, 原有={b} ({b/max(1,len(rows)):.1%}), "
            f"新增={a}, 合并后={t} ({t/max(1,len(rows)):.1%})")
    # 按 last_10k 年段（与任务口径对照）
    for lo, hi in [(2008, 2016), (2017, 2025)]:
        rows = [r for r in comps if r.get("last_10k") and lo <= int(r["last_10k"][:4]) <= hi]
        b = sum(1 for r in rows if r["cik"] in have_before)
        t = sum(1 for r in rows if r["cik"] in total_after)
        log(f"[C] last_10k {lo}-{hi}: {len(rows)} 家, 原有={b} ({b/max(1,len(rows)):.1%}), "
            f"合并后={t} ({t/max(1,len(rows)):.1%})")

    # ---- D. 与 tickers_all_merged 重叠/冲突 ----
    merged_pairs = {(r["cik"].lstrip("0"), r["ticker"]) for r in merged}
    new_pairs = {(r["cik"], r["ticker"]) for r in hist}
    log(f"[D] 与现有票号表: 相同(cik,ticker)对={len(new_pairs & merged_pairs)}; "
        f"仅 SymDir 新增对={len(new_pairs - merged_pairs)}")

    # 时间回填: 相同 (cik,ticker) 对, SymDir 的 first_seen 是否更早
    merged_first = {}
    for r in merged:
        merged_first.setdefault((r["cik"].lstrip("0"), r["ticker"]), r["first_seen"])
    earlier = back2008 = 0
    gains = []
    for r in hist:
        k = (r["cik"], r["ticker"])
        mf = merged_first.get(k)
        if not mf:
            continue
        if r["first_seen"] < mf:
            earlier += 1
            gains.append((int(mf[:4]) - int(r["first_seen"][:4])))
            if mf >= "2017-08-01" and r["first_seen"] <= "2009-12-31":
                back2008 += 1
    gains.sort()
    med = gains[len(gains) // 2] if gains else 0
    log(f"[D] 时间回填: 重叠对中 SymDir first_seen 更早={earlier}; "
        f"其中旧表 first_seen>=2017-08 且 SymDir<=2009-12 的对={back2008}; "
        f"提前年数中位数={med}, P90={gains[int(len(gains)*0.9)] if gains else 0}")
    log(f"[D] SymDir 命中对中 first_seen<=2008-01-31 的对数="
        f"{sum(1 for r in hist if r['first_seen'] <= '2008-01-31')}")
    # 同一 CIK 新增不同 ticker
    merged_tk = defaultdict(set)
    for cik, tk in merged_pairs:
        merged_tk[cik].add(tk)
    cik_newtk = []
    for r in hist:
        if r["ticker"] not in merged_tk.get(r["cik"], set()):
            cik_newtk.append(r)
    log(f"[D] 同一 CIK 不同 ticker 的 SymDir 新增行={len(cik_newtk)}; 样例:")
    for r in cik_newtk[:8]:
        nm = comp_by_cik.get(r["cik"], {}).get("name", "?")
        old = sorted(merged_tk.get(r["cik"], []))
        log(f"[D]   cik={r['cik']} {nm} | SymDir={r['ticker']} {r['first_seen']}..{r['last_seen']} "
            f"| 旧={old}")
    # 同一 ticker 不同 CIK（潜在复用/冲突）
    tick_cik = defaultdict(set)
    for r in hist:
        tick_cik[r["ticker"]].add(r["cik"])
    conflict = {t: c for t, c in tick_cik.items() if len(c) > 1}
    log(f"[D] 同一 ticker 匹配到多 CIK={len(conflict)}")
    for t in sorted(conflict)[:8]:
        log(f"[D]   {t}: " + "; ".join(
            f"{c}={comp_by_cik.get(c, {}).get('name', '?')}" for c in sorted(conflict[t])))
    # 与 merged 同 ticker 不同 CIK
    merged_tick = defaultdict(set)
    for cik, tk in merged_pairs:
        merged_tick[tk].add(cik)
    cross = []
    for t in tick_cik:
        for cik in tick_cik[t]:
            others = merged_tick.get(t, set()) - {cik}
            if others:
                cross.append((t, cik, sorted(others)))
    log(f"[D] 同一 ticker 在 merged 中属于其他 CIK 的行={len(cross)}; 样例={cross[:8]}")

    # ---- E. 人工核对抽样 ----
    random.seed(42)
    picks = random.sample(hist, min(8, len(hist)))
    log("[E] 命中抽样 (cik | EDGAR 名 | 快照证据名 | ticker | 窗口 | 类型/分数):")
    for r in picks:
        nm = comp_by_cik.get(r["cik"], {}).get("name", "?")
        log(f"[E]   {r['cik']} | {nm} | {r['evidence_name']} | {r['ticker']} {r['exchange']} "
            f"{r['first_seen']}..{r['last_seen']} | {r['security_type']}/{r['match_type']}/{r['score']}")
    # 定向抽样：知名退市公司
    for tk in ("LEH", "CFC", "WB", "WM", "BSC", "ENRN", "WCOM", "GM", "AIG", "BSC"):
        for r in hist:
            if r["ticker"] == tk:
                nm = comp_by_cik.get(r["cik"], {}).get("name", "?")
                log(f"[E] 定向 {tk}: cik={r['cik']} {nm} | {r['evidence_name']} | "
                    f"{r['first_seen']}..{r['last_seen']} | {r['match_type']}/{r['score']}")
                break
    log("=== nasdaqdir_verify done ===")


if __name__ == "__main__":
    main()
