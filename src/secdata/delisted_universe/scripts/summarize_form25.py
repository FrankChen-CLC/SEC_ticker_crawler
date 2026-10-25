#!/usr/bin/env python3
"""summarize_form25.py — 汇总 Form 25 原因解析结果，并回贴 merger.log

用法：
  python3 -I summarize_form25.py <outdir>

读 outdir/form25_reasons.csv + delist_companies.csv + merged_into.csv，
输出：分 form × cause 计数、按公司去重的原因分布、与 merged_into 的交叉验证，
追加写入 outdir/merger.log。
"""
import csv
import os
import sys
from collections import Counter, defaultdict


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    P = lambda f: os.path.join(outdir, f)
    rows = list(csv.DictReader(open(P("form25_reasons.csv"))))
    comp = {r["cik"].lstrip("0"): r for r in csv.DictReader(open(P("delist_companies.csv")))}
    merged = {}
    mp = P("merged_into.csv")
    if os.path.exists(mp):
        merged = {r["cik"]: r for r in csv.DictReader(open(mp))}
    lowp = P("merged_into_lowconf.csv")
    low = {r["cik"]: r for r in csv.DictReader(open(lowp))} if os.path.exists(lowp) else {}

    lines = []

    def L(m):
        lines.append(m)
        print(m)

    L("")
    L("[C] ==== Form 25 / 25-NSE 原因解析 ====")
    L(f"[C] 事件行 {len(rows):,}；状态 {dict(Counter(r['status'] for r in rows))}")
    for form in ("25", "25-NSE"):
        sub = [r for r in rows if r["form"] == form]
        c = Counter(r["cause_category"] for r in sub)
        L(f"[C] {form}: {len(sub):,} 条 -> {dict(c.most_common())}")
    L(f"[C] form25 勾选条款分布: "
      f"{dict(Counter(r['rule_checked'] or '(none)' for r in rows if r['form'] == '25').most_common(10))}")
    L(f"[C] 25-NSE 勾选条款分布: "
      f"{dict(Counter(r['rule_checked'] or '(none)' for r in rows if r['form'] == '25-NSE').most_common(10))}")

    # 按发行人 CIK 去重（一条公司可能有多个事件）
    per = defaultdict(Counter)
    for r in rows:
        if r["status"] == "ok" and r["cik"]:
            per[r["cik"]][r["cause_category"]] += 1
    comp_ev = {c: cnt for c, cnt in per.items() if c in comp}
    L(f"[C] 25/25-NSE 覆盖退市公司 {len(comp_ev):,} / {len(comp):,} "
      f"({len(comp_ev)/len(comp)*100:.1f}%)")
    cc = Counter()
    for c, cnt in comp_ev.items():
        if cnt.get("merger") or cnt.get("merger_substitution_confirmed"):
            cc["merger_text"] += 1
        elif cnt.get("merger_substitution"):
            cc["merger_rule_a3a4"] += 1
        elif cnt.get("bankruptcy_liquidation"):
            cc["bankruptcy_liquidation"] += 1
        elif cnt.get("listing_standards"):
            cc["listing_standards"] += 1
        elif cnt.get("issuer_voluntary") or cnt.get("redemption_maturity"):
            cc["voluntary_or_maturity"] += 1
        else:
            cc["no_reason_text"] += 1
    L(f"[C] 按公司去重后原因分布: {dict(cc.most_common())}")
    if comp_ev:
        mg = cc["merger_text"] + cc["merger_rule_a3a4"]
        L(f"[C] 「正文明确写 merger/consolidation/acquisition」= {cc['merger_text']:,} 家"
          f"（占有 Form 25 事件公司 {cc['merger_text']/len(comp_ev)*100:.1f}%，"
          f"占全部 {len(comp):,} 家退市 {cc['merger_text']/len(comp)*100:.1f}%）")
        L(f"[C] 「无原因叙述但勾选 12d2-2(a)(3)/(a)(4)（证券被替换/权利消灭）」= "
          f"{cc['merger_rule_a3a4']:,} 家（含单位/存托凭证换发，不等于公司并购）")
        L(f"[C] 两者合计上限 = {mg:,} 家（占全部退市 {mg/len(comp)*100:.1f}%）")

    # 交叉验证：merged_into 的 high/medium 里有 Form 25 事件的，原因是否支持并购
    for label, book in (("high/medium", merged), ("low", low)):
        hit = [c for c in book if c in per]
        if not hit:
            continue
        mg = sum(1 for c in hit if per[c].get("merger") or per[c].get("merger_substitution")
                 or per[c].get("merger_substitution_confirmed"))
        L(f"[C] 交叉验证 {label}: {len(hit):,} 家有 Form 25 事件，其中 {mg} 家正文/条款支持并购/替换"
          f"({mg/len(hit)*100:.0f}%)")
        others = Counter()
        for c in hit:
            if not (per[c].get("merger") or per[c].get("merger_substitution")
                    or per[c].get("merger_substitution_confirmed")):
                k = [x for x in per[c] if x not in ("none",)]
                others[k[0] if k else "none"] += 1
        L(f"[C]   其余原因分布: {dict(others.most_common(6))}")

    with open(P("merger.log"), "a") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\n-> 追加写入 {P('merger.log')}")


if __name__ == "__main__":
    main()
