#!/usr/bin/env python3
"""extract_filings_bulk.py — 从 SEC submissions.zip 随机读取，抽退市原因的申报证据

纯 SEC 口径（不依赖 CRSP/交易所数据），只读本地 1.57GB 包，零额外网络。
每个 CIK 的 filings.recent（最近约 1000 条）足够：退市公司的终点事件必然在末尾。

输出（outdir/）：
  filings_last.csv    每 CIK 一行：最后申报、Form 15 注销、并购类申报计数与最近日期
  filings_events.csv  只存命中表格的事件行：cik,form,filing_date,items,accession,primary_doc

分类口径：
  15-*        = 注销证券登记（15-12B 12(b) / 15-12G 12(g) / 15-15D 15(d) 暂停报告义务）
  S-4/425/DEFM14A/PREM14A/SC 13E-3 = 并购/换股/私有化注册与委托书
  8-K items 1.01(重大协议) 2.01(完成收购或处置) 5.01(控制权变更)
用法：python3 -I extract_filings_bulk.py submissions.zip delist_universe.csv out
"""
import csv
import json
import os
import sys
import zipfile
from collections import defaultdict

# 并购/私有化类注册与委托书
MERGER_FORMS = {
    "DEFM14A", "DEFM14A/A", "PREM14A", "PREM14A/A", "DEFR14A",
    "S-4", "S-4/A", "S-4 POS", "S-4 POS AM",
    "425", "425/A",
    "SC 13E-3", "SC 13E3", "SC 13E-3/A", "SC 13E3/A",
}
ITEMS_OF_INTEREST = {"1.01", "2.01", "5.01"}


def is_dereg(form):
    f = form.upper()
    return f.startswith("15-") or f in ("15", "15/A")


def is_8k(form):
    f = form.upper()
    return f == "8-K" or f.startswith("8-K/")


def main():
    zip_path, universe_csv, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    ciks = []
    seen = set()
    for r in csv.DictReader(open(universe_csv)):
        c = r["cik"].strip().lstrip("0")
        if c.isdigit() and c not in seen:
            seen.add(c)
            ciks.append(c)
    print(f"目标 CIK: {len(ciks):,}", flush=True)

    z = zipfile.ZipFile(zip_path)
    names = set(z.namelist())

    last_rows = []
    event_rows = []
    missing = 0
    for i, c in enumerate(ciks):
        entry = "CIK%010d.json" % int(c)
        if entry not in names:
            missing += 1
            continue
        try:
            d = json.loads(z.read(entry))
        except Exception:
            missing += 1
            continue
        rec = (d.get("filings") or {}).get("recent") or {}
        forms = rec.get("form") or []
        dates = rec.get("filingDate") or []
        items = rec.get("items") or [""] * len(forms)
        accs = rec.get("accessionNumber") or [""] * len(forms)
        pdoc = rec.get("primaryDocument") or [""] * len(forms)

        s = defaultdict(int)
        f15 = []
        ev = defaultdict(list)  # key -> [dates]
        last_form, last_date = "", ""
        last_10k_form, last_10k_date = "", ""
        n_recent = len(forms)
        first_date = dates[-1] if dates else ""
        for j in range(n_recent):
            f = forms[j] or ""
            dt = dates[j] or ""
            it = (items[j] or "") if j < len(items) else ""
            if dt > last_date:
                last_form, last_date = f, dt
            fu = f.upper()
            if fu.startswith("10-K") or fu.startswith("20-F") or fu.startswith("40-F"):
                if dt > last_10k_date:
                    last_10k_form, last_10k_date = f, dt
            hit = False
            if is_dereg(f):
                f15.append((dt, f))
                hit = True
            elif fu in MERGER_FORMS:
                s[fu] += 1
                ev[fu].append(dt)
                hit = True
            elif is_8k(f):
                its = {x.strip() for x in it.split(",") if x.strip()}
                want = its & ITEMS_OF_INTEREST
                if want:
                    for w in sorted(want):
                        s["8K_" + w.replace(".", "")] += 1
                        ev["8K_" + w.replace(".", "")].append(dt)
                    hit = True
            if hit:
                event_rows.append([c, f, dt, it, accs[j] if j < len(accs) else "",
                                   pdoc[j] if j < len(pdoc) else ""])

        f15.sort()
        row = {
            "cik": c,
            "name": d.get("name", ""),
            "entity_type": d.get("entityType", ""),
            "sic": d.get("sic", ""),
            "n_recent": n_recent,
            "recent_first_date": first_date,
            "recent_last_date": last_date,
            "last_form": last_form,
            "last_date": last_date,
            "last_10k_form": last_10k_form,
            "last_10k_date": last_10k_date,
            "n_form15": len(f15),
            "form15_first": f15[0][0] if f15 else "",
            "form15_last": f15[-1][0] if f15 else "",
            "form15_types": "|".join(sorted({x[1] for x in f15})),
            "deregistered": 1 if f15 else 0,
            "n_8k_101": s["8K_101"],
            "last_8k_101": max(ev["8K_101"]) if ev["8K_101"] else "",
            "n_8k_201": s["8K_201"],
            "last_8k_201": max(ev["8K_201"]) if ev["8K_201"] else "",
            "n_8k_501": s["8K_501"],
            "last_8k_501": max(ev["8K_501"]) if ev["8K_501"] else "",
            "n_s4": s["S-4"] + s["S-4/A"] + s["S-4 POS"] + s["S-4 POS AM"],
            "last_s4": max([d for k in ("S-4", "S-4/A", "S-4 POS", "S-4 POS AM") for d in ev[k]]) if any(
                ev[k] for k in ("S-4", "S-4/A", "S-4 POS", "S-4 POS AM")) else "",
            "n_425": s["425"] + s["425/A"],
            "last_425": max(ev["425"] + ev["425/A"]) if (ev["425"] or ev["425/A"]) else "",
            "n_defm14a": s["DEFM14A"] + s["DEFM14A/A"] + s["PREM14A"] + s["PREM14A/A"] + s["DEFR14A"],
            "last_defm14a": max(ev["DEFM14A"] + ev["DEFM14A/A"] + ev["PREM14A"] + ev["PREM14A/A"] + ev["DEFR14A"]) if any(
                ev[k] for k in ("DEFM14A", "DEFM14A/A", "PREM14A", "PREM14A/A", "DEFR14A")) else "",
            "n_13e3": s["SC 13E-3"] + s["SC 13E3"] + s["SC 13E-3/A"] + s["SC 13E3/A"],
            "last_13e3": max([d for k in ("SC 13E-3", "SC 13E3", "SC 13E-3/A", "SC 13E3/A") for d in ev[k]]) if any(
                ev[k] for k in ("SC 13E-3", "SC 13E3", "SC 13E-3/A", "SC 13E3/A")) else "",
        }
        last_rows.append(row)
        if (i + 1) % 5000 == 0:
            print(f"  {i+1:,}/{len(ciks):,} ...", flush=True)

    os.makedirs(outdir, exist_ok=True)
    p1 = os.path.join(outdir, "filings_last.csv")
    cols = list(last_rows[0].keys())
    with open(p1, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(last_rows)
    p2 = os.path.join(outdir, "filings_events.csv")
    with open(p2, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "form", "filing_date", "items", "accession", "primary_doc"])
        w.writerows(event_rows)

    dereg = sum(r["deregistered"] for r in last_rows)
    print(f"-> {p1} ({len(last_rows):,} 行, 缺失 {missing:,})")
    print(f"-> {p2} ({len(event_rows):,} 行)")
    print(f"  deregistered(有 Form 15): {dereg:,} ({dereg/max(len(last_rows),1):.1%})")
    for k in ("n_s4", "n_425", "n_defm14a", "n_13e3", "n_8k_101", "n_8k_201", "n_8k_501"):
        print(f"  {k:12s} {sum(1 for r in last_rows if r[k]):,}")


if __name__ == "__main__":
    main()
