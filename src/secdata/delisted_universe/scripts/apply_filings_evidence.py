#!/usr/bin/env python3
"""apply_filings_evidence.py — 用 SEC 申报证据升级退市性质分类（纯 SEC 口径，无 CRSP）

输入 out/：filings_last.csv + filings_events.csv（extract_filings_bulk.py 产出）
          master_universe.csv + delist_companies.csv
输出：覆盖写上述两表，各加 8 列：
  registered_12b        曾按 12(b) 在交易所注册（Form 15-12B 是终止 12(b) 的专用表）
  deregistered          有 Form 15（终止报告义务）
  dereg_type / dereg_date
  merger_ev_form / merger_ev_date      终局窗口内的并购证据
  going_private_ev_date                终局窗口内的 13E-3 私有化证据
  delist_nature         merged / going_private / deregistered / went_dark
并购证据只认 425/DEFM14A/PREM14A/DEFR14A（需股东会表决的并购文件），窗口 = 锚点前 365 天。
  tier A = 上述表决类文件（或 SC 13E-3 私有化）
  tier B = 8-K item 5.01 控制权变更，且该 5.01 晚于末次年报（= 之后没再出年报，收盘型）
刻意排除：8-K item 2.01（破产清算的资产处置也用它，实测假阳性 72%）、
          S-4（预装破产征集也用它，召回增益仅 +0.2pp）。
`old_merged` 从 successor_cik/delist_reason 原始列重算，保证可反复重跑不漂移。
验证明细见 README「退市性质四分」节。幂等：重跑前丢弃同名列。
"""
import csv
import datetime as dt
import os
from collections import defaultdict

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")
WINDOW_DAYS = 365

MERGER_EV = {"425", "425/A", "DEFM14A", "DEFM14A/A", "PREM14A", "PREM14A/A", "DEFR14A"}
PRIV_EV = {"SC 13E-3", "SC 13E3", "SC 13E-3/A", "SC 13E3/A"}
ADDED = ["registered_12b", "deregistered", "dereg_type", "dereg_date",
         "merger_ev_form", "merger_ev_date", "merger_ev_tier",
         "going_private_ev_date", "delist_nature"]


def d(s):
    try:
        return dt.date.fromisoformat(s)
    except Exception:
        return None


def main():
    last = {r["cik"]: r for r in csv.DictReader(open(os.path.join(OUT, "filings_last.csv")))}
    ev = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(OUT, "filings_events.csv"))):
        ev[r["cik"]].append((r["filing_date"], (r["form"] or "").upper()))

    def windowed(cik, base, forms):
        if not base:
            return None
        lo = base - dt.timedelta(days=WINDOW_DAYS)
        best = None
        for fd, form in ev.get(cik, ()):
            dd = d(fd)
            if dd and lo <= dd <= base and form in forms:
                if best is None or dd > best[0]:
                    best = (dd, form)
        return best

    stats = defaultdict(int)
    for fn in ("master_universe.csv", "delist_companies.csv"):
        p = os.path.join(OUT, fn)
        rows = list(csv.DictReader(open(p)))
        if not rows:
            continue
        cols = [c for c in rows[0].keys() if c not in ADDED]
        extra = [c for c in ADDED if c not in cols]
        cols = cols + extra
        has_exch = "was_exchange_listed" in rows[0]
        out = []
        for r in rows:
            c = r["cik"].lstrip("0")
            L = last.get(c, {})
            ftypes = L.get("form15_types", "") or ""
            dereg = L.get("deregistered") == "1"
            r12b = 1 if "15-12B" in ftypes else 0
            # 终局锚点：先 Form 15 注销日，退而末次年报日
            anc = d(L.get("form15_last", "")) or d(L.get("last_10k_date", ""))
            mev = windowed(c, anc, MERGER_EV)
            pev = windowed(c, anc, PRIV_EV)

            old_merged = bool(r.get("successor_cik")) or r.get("delist_reason") == "merger"
            # tier B：8-K 5.01 控制权变更，且晚于末次年报（之后没再出年报 = 收盘型）
            bev = None
            e501 = d(L.get("last_8k_501", ""))
            last10k = d(L.get("last_10k_date", ""))
            if (anc and e501 and anc - dt.timedelta(days=WINDOW_DAYS) <= e501 <= anc
                    and (not last10k or e501 >= last10k)):
                bev = (e501, "8-K 5.01")

            if mev or old_merged:
                nature = "merged"
                tier = "A" if mev else "form25"
                evform = mev[1] if mev else ""
                evdate = mev[0].isoformat() if mev else ""
            elif pev:
                nature, tier, evform, evdate = "going_private", "A", "", ""
            elif bev:
                nature, tier, evform, evdate = "merged", "B", bev[1], bev[0].isoformat()
            elif dereg:
                nature, tier, evform, evdate = "deregistered", "", "", ""
            else:
                nature, tier, evform, evdate = "went_dark", "", "", ""
            r.update(registered_12b=r12b,
                     deregistered=1 if dereg else 0,
                     dereg_type=ftypes,
                     dereg_date=L.get("form15_last", ""),
                     merger_ev_form=evform,
                     merger_ev_date=evdate,
                     merger_ev_tier=tier,
                     going_private_ev_date=pev[0].isoformat() if pev else "",
                     delist_nature=nature)
            r["is_merged"] = "1" if nature in ("merged", "going_private") else "0"
            if has_exch:
                r["was_exchange_listed"] = "1" if (r.get("was_exchange_listed") == "1" or r12b) else "0"
            out.append(r)
            if fn == "delist_companies.csv":
                stats["total"] += 1
                stats["nat_" + nature] += 1
                stats["dereg"] += 1 if dereg else 0
                stats["12b"] += r12b
                if nature in ("merged", "going_private"):
                    stats["is_merged"] += 1
                if tier:
                    stats["tier_" + tier] += 1
                if mev:
                    stats["ev_" + mev[1]] += 1
        with open(p, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(out)

    t = max(stats["total"], 1)
    lines = [f"退市性质四分（分母 {t:,} 家净化后退市公司）", ""]
    for k, lab in (("merged", "并购/被吸收"), ("going_private", "13E-3 私有化"),
                   ("deregistered", "注销登记但非并购"), ("went_dark", "停报失联")):
        n = stats["nat_" + k]
        lines.append(f"  {lab:16s} {n:>6,}  {n/t:5.1%}")
    lines.append(f"  is_merged 合计（并购+私有化） {stats['is_merged']:>6,}  {stats['is_merged']/t:5.1%}")
    lines.append("")
    lines.append(f"  证据层级: tier A(表决类文件) {stats['tier_A']:,} / tier B(8-K 5.01 收盘) {stats['tier_B']:,} / form25(旧口径) {stats['tier_form25']:,}")
    lines.append(f"  Form 15 注销登记 {stats['dereg']:,} ({stats['dereg']/t:.1%})；其中 15-12B（曾交易所注册）{stats['12b']:,}")
    lines.append("  并购证据构成（窗口内最近一份）:")
    for k in sorted(stats):
        if k.startswith("ev_"):
            lines.append(f"    {k[3:]:12s} {stats[k]:>6,}")
    txt = "\n".join(lines)
    open(os.path.join(OUT, "filings_evidence_summary.txt"), "w").write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
