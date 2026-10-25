#!/usr/bin/env python3
"""apply_merger_split.py — 把承继主体(B)与 Form25 退市原因(C)并进主表/净化表

输入 out/：merged_into.csv（B，high/medium）、form25_reasons.csv（C，逐事件）
输出：覆盖写 out/master_universe.csv 与 out/delist_companies.csv，各加 5 列：
  successor_cik, successor_confidence, successor_evidence, delist_reason, is_merged
理由优先级：merger > merger_substitution_confirmed > merger_substitution > bankruptcy_liquidation
  > listing_standards > rights_extinguished > redemption_maturity > exchange_action
  > issuer_voluntary > unknown_no_rule > none
is_merged = 有 B 承继主体 或 delist_reason == merger（merger_substitution 不算并购：存托凭证换发/条款替换）
幂等：重跑前丢弃同名列。
"""
import csv
import os
from collections import defaultdict

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")

PRIORITY = ["merger", "merger_substitution_confirmed", "merger_substitution",
            "bankruptcy_liquidation", "listing_standards", "rights_extinguished",
            "redemption_maturity", "exchange_action", "issuer_voluntary",
            "unknown_no_rule", "none"]
ADDED = ["successor_cik", "successor_confidence", "successor_evidence", "delist_reason", "is_merged"]


def main():
    succ = {}
    for r in csv.DictReader(open(os.path.join(OUT, "merged_into.csv"))):
        succ[r["cik"].lstrip("0")] = r
    per = defaultdict(set)
    for r in csv.DictReader(open(os.path.join(OUT, "form25_reasons.csv"))):
        for c in (r["cik"] or "").split("|"):
            c = c.strip().lstrip("0")
            if c.isdigit():
                per[c].add(r["cause_category"])

    def reason(cik):
        cats = per.get(cik, ())
        for p in PRIORITY:
            if p in cats:
                return p
        return ""

    stats = defaultdict(int)
    for fn in ("master_universe.csv", "delist_companies.csv"):
        p = os.path.join(OUT, fn)
        rows = list(csv.DictReader(open(p)))
        if not rows:
            continue
        cols = [c for c in rows[0].keys() if c not in ADDED] + ADDED
        out = []
        for r in rows:
            c = r["cik"].lstrip("0")
            s = succ.get(c)
            rs = reason(c)
            r.update(successor_cik=s["merged_into_cik"] if s else "",
                     successor_confidence=s["confidence"] if s else "",
                     successor_evidence=s["evidence_type"] if s else "",
                     delist_reason=rs,
                     is_merged=1 if (s or rs == "merger") else 0)
            out.append(r)
            if fn == "delist_companies.csv":
                stats["total"] += 1
                if s:
                    stats["B_" + s["confidence"]] += 1
                if rs:
                    stats["C_" + rs] += 1
                if int(r["is_merged"]):
                    stats["merged"] += 1
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(out)

    t = max(stats["total"], 1)
    lines = [f"并购/承继拆分（分母 {t:,} 家净化后退市公司）", ""]
    for k in ("B_high", "B_medium", "merged"):
        lines.append(f"  {k}: {stats[k]:,} ({stats[k]/t:.1%})")
    lines.append("  Form25 原因分布（公司级，priority 取最高）:")
    for k in PRIORITY:
        n = stats["C_" + k]
        if n:
            lines.append(f"    {k:<30} {n:>6,}")
    lines.append(f"  仍未判定（真退市/失联）: {t - stats['merged']:,}")
    txt = "\n".join(lines)
    open(os.path.join(OUT, "merger_split_summary.txt"), "w").write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
