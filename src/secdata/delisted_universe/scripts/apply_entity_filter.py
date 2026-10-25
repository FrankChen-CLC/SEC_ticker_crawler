#!/usr/bin/env python3
"""apply_entity_filter.py — 用 submissions.zip 抽出的 SIC 给宇宙打实体类型，删确定非公司载体后重算

输入（out/）：sics.csv, delist_universe.csv, master_universe.csv
输出：
  out/entity_class.csv          全宇宙实体分类（含缺失标 unknown）
  out/excluded_non_company.csv  被删除的确定非公司载体（带 reason）
  out/delist_companies.csv      净化后的退市公司表（is_delisted=1 且是公司）
  out/master_universe.csv       原主表 + sic/sic_desc/entity_class/is_company/is_shell 列
  out/entity_class_summary.txt  重算统计
确定非公司 = SIC 法定分类直接判定的载体（ABS 信托/开放式基金/UIT/信托），不含名称推断。
空壳(6770 SPAC) 是法人公司：保留但打 is_shell，单列统计。
"""
import csv
import os
import re
from collections import Counter

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")

NON_COMPANY_SIC = {
    "6189": "ABS/证券化信托",
}
SHELL_SIC = {"6770": "空白支票/SPAC"}
# 无歧义载体名称模式（2026-10-25 逐条人工核样后定稿；不含裸 TRUST/SECURITIES，避免误杀银行/REIT/券商）
VEHICLE_PAT = re.compile(
    r"GRANTOR TRUST|OWNER TRUST|PASS[ -]THROUGH|TRUST SERIES|FUNDING TRUST|"
    r"MORTGAGE (LOAN )?TRUST|LOAN TRUST|ASSET[ -]BACKED|FUTURES FUND|FUND L\.?P\b|"
    r"UNIT INVESTMENT TRUST|RECEIVABLES|SECURITIZATION", re.I)


def main():
    sics = {}
    p = os.path.join(OUT, "sics.csv")
    if not os.path.exists(p):
        raise SystemExit("缺 out/sics.csv（先在 .39 跑 fetch_sic_bulk.py）")
    for r in csv.DictReader(open(p)):
        sics[r["cik"].lstrip("0")] = r

    uni = list(csv.DictReader(open(os.path.join(OUT, "delist_universe.csv"))))
    mst = {r["cik"]: r for r in csv.DictReader(open(os.path.join(OUT, "master_universe.csv")))}

    def classify(cik, name):
        s = sics.get(cik)
        if not s:
            return "", "", "unknown", 1, 0, "missing_sic"
        sic, desc = s["sic"], s["sic_desc"]
        if sic in NON_COMPANY_SIC:
            return sic, desc, "non_company", 0, 0, NON_COMPANY_SIC[sic]
        if VEHICLE_PAT.search(name):
            return sic, desc, "non_company", 0, 0, "名称判定载体"
        if sic in SHELL_SIC:
            return sic, desc, "shell", 1, 1, ""
        return sic, desc, "company", 1, 0, ""

    excluded = []
    cleaned = []
    cls_all = []
    stats = Counter()
    year_after = Counter()
    for r in uni:
        c = r["cik"].lstrip("0")
        sic, desc, ecls, is_co, is_shell, reason = classify(c, r["name"])
        cls_all.append([c, r["name"], sic, desc, ecls, is_co, is_shell, reason])
        if r["is_delisted"] != "1":
            continue
        stats["delisted_before"] += 1
        if ecls == "non_company":
            excluded.append([c, r["name"], sic, desc, reason, r["last_10k"]])
            stats["excluded_" + reason] += 1
            continue
        cleaned.append(dict(r, sic=sic, sic_desc=desc, entity_class=ecls, is_shell=is_shell))
        stats["delisted_after"] += 1
        year_after[r["last_10k"][:4]] += 1
        if is_shell:
            stats["shell_kept"] += 1
        if ecls == "probable_vehicle":
            stats["probable_vehicle_kept"] += 1
        if ecls == "unknown":
            stats["unknown_kept"] += 1
        m = mst.get(c)
        if m and m.get("ticker"):
            stats["with_ticker"] += 1
        if m and m.get("in_fundamentals") == "1":
            stats["with_fundamentals"] += 1

    def w(path, header, rows):
        with open(path, "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(header)
            wr.writerows(rows)

    cls_map = {x[0]: x for x in cls_all}
    w(os.path.join(OUT, "entity_class.csv"),
      ["cik", "name", "sic", "sic_desc", "entity_class", "is_company", "is_shell", "exclude_reason"], cls_all)
    w(os.path.join(OUT, "excluded_non_company.csv"),
      ["cik", "name", "sic", "sic_desc", "reason", "last_10k"], sorted(excluded, key=lambda x: x[5]))
    w(os.path.join(OUT, "delist_companies.csv"),
      ["cik", "name", "first_s1", "first_10k", "last_10k", "delist_form_date",
       "quiet_quarters_after_last_10k", "is_delisted", "ipo_year_known", "sic", "sic_desc",
       "entity_class", "is_shell"],
      [[r["cik"], r["name"], r["first_s1"], r["first_10k"], r["last_10k"], r["delist_form_date"],
        r["quiet_quarters_after_last_10k"], r["is_delisted"], r["ipo_year_known"], r["sic"],
        r["sic_desc"], r["entity_class"], r["is_shell"]] for r in cleaned])

    # 主表加列（保持原行序，全宇宙）
    with open(os.path.join(OUT, "master_universe.csv"), "w", newline="") as f:
        ADDED = ["sic", "sic_desc", "entity_class", "is_company", "is_shell", "is_delisted_company"]
        cols = [c for c in next(iter(mst.values())).keys() if c not in ADDED] + ADDED
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for c, row in mst.items():
            extra = cls_map.get(c)
            if extra:
                row.update(sic=extra[2], sic_desc=extra[3], entity_class=extra[4],
                           is_company=extra[5], is_shell=extra[6])
            else:
                row.update(sic="", sic_desc="", entity_class="unknown", is_company=1, is_shell=0)
            row["is_delisted_company"] = 1 if (row.get("is_delisted") == "1" and str(row.get("is_company")) == "1") else 0
            wr.writerow(row)

    lines = ["实体过滤重算（确定非公司 = SIC 6189 ABS信托 + 无歧义载体名称模式）", ""]
    lines.append(f"退市标的主体 前: {stats['delisted_before']:,}")
    lines.append(f"删除非公司载体: {sum(v for k, v in stats.items() if k.startswith('excluded_')):,}"
                 f"（SIC6189 ABS/证券化信托 {stats['excluded_ABS/证券化信托']:,}"
                 f" + 名称判定载体 {stats['excluded_名称判定载体']:,}）")
    lines.append(f"退市公司 后: {stats['delisted_after']:,}")
    lines.append(f"  其中空壳SPAC保留(is_shell=1): {stats['shell_kept']:,}")
    lines.append(f"  疑似载体保留(probable_vehicle): {stats['probable_vehicle_kept']:,}")
    lines.append(f"  无SIC(unknown,保留): {stats['unknown_kept']:,}")
    lines.append(f"票号覆盖: {stats['with_ticker']:,} ({stats['with_ticker']/max(stats['delisted_after'],1):.1%})")
    lines.append(f"财报覆盖: {stats['with_fundamentals']:,} ({stats['with_fundamentals']/max(stats['delisted_after'],1):.1%})")
    lines.append("")
    lines.append("净化后按末次年报年:")
    for y, n in sorted(year_after.items()):
        if y:
            lines.append(f"  {y}: {n:,}")
    open(os.path.join(OUT, "entity_class_summary.txt"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
