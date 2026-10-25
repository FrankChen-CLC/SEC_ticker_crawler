#!/usr/bin/env python3
"""detect_merged_into.py — 从退市公司里识别"被并购/重组吸收"的承继主体

用法（Mac，stdlib）：
  python3 -I detect_merged_into.py <outdir>

输入（outdir 内）：
  delist_companies.csv   29,839 家退市公司（主表）
  master_universe.csv    退市事件日期补充
  former_names.csv       universe 内 CIK 的 formerNames（退市方自己的旧名）
  addresses.csv          universe 内 CIK 的地址
  all_ciks.csv           全量 99 万 CIK: name/entity_type/last_10k/business addr
  all_former_names.csv   全量 formerNames（B1 反查底座）
  all_addr.csv           全量地址（B2 同址底座）

B1 formerNames 反查：退市 CIK 的归一化名（含其自身旧名）出现在**另一个 CIK** 的 formerNames 里，
   且该 formerName 的 from/to 与退市 CIK 末次年报后 0-3 年窗口重叠 -> successor 候选。
   置信：high = 时间重叠 + 承继方有更晚申报 + 名字有辨识度（>=2 词且>=8 字符，或单词>=12 字符）
                    + 该名在全库命中 <=4 个 CIK；
        medium = 时间重叠 + 名字有辨识度，但承继方无更晚申报；
        其余（无时间重叠 / 名太短太通用）一律 low，不硬配。
B2 同址+名似：street1+zip 与另一 CIK 完全相同、归一化名相似度>=0.7、对方年报更晚。
   medium 仅当归一化名完全相同、或一方是另一方 >=3 词的词级前缀（且不是兄弟基金/序号主体）；
   其余 low。地址下 CIK >30 个的（注册代理/共享地址）跳过。

归一化：大写、去标点、& -> AND、去尾部法人后缀 INC/CORP/CO/LLC/LTD/LP/HOLDINGS/GROUP/THE/DE 等；
TRUST/FUND/PARTNERS 等不剥，避免把兄弟基金判成同一主体。

输出：
  merged_into.csv           high/medium
  merged_into_lowconf.csv   low
  merger.log                明细日志 + decade 统计
"""
import csv
import difflib
import re
import os
import sys
from collections import Counter, defaultdict

SUFFIX = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COS", "LLC", "L L C",
    "LTD", "LIMITED", "LP", "L P", "LLP", "PLC", "PC", "NV", "SA", "AG", "BV", "SE",
    "HOLDINGS", "HOLDING", "GROUP", "GRP", "THE", "COM", "DEL", "DE", "MD", "PA",
    "CLASS", "SERIES",
}
# 注意：TRUST / FUND / PARTNERS / INDUSTRIES 等不剥——它们是兄弟基金/子实体的区分词，
# 剥掉会把 "... REALTY FUND" 和 "... REALTY GROWTH FUND" 误判成同一主体。
MASS_ADDR = 30  # 同一 street1+zip 下 CIK 数超过此值视为注册代理/共享地址，跳过
GENERIC = 4     # 同一归一化名命中超过此数量候选 CIK 视为过于通用


def norm(s):
    s = (s or "").upper()
    s = s.replace("&", " AND ").replace(".", " ").replace(",", " ")
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch.isspace() or ch == "/") else " ")
    s = " ".join("".join(out).split())
    toks = s.split()
    if toks and toks[0] == "THE":
        toks = toks[1:]
    while toks and toks[-1] in SUFFIX:
        toks = toks[:-1]
    return " ".join(toks)


def nstreet(s):
    s = (s or "").upper()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    s = " ".join(s.split())
    for a, b in ((" STREET", " ST"), (" AVENUE", " AVE"), (" ROAD", " RD"),
                 (" DRIVE", " DR"), (" BOULEVARD", " BLVD"), (" SUITE", " STE"),
                 (" FLOOR", " FL"), (" NORTH", " N"), (" SOUTH", " S"),
                 (" EAST", " E"), (" WEST", " W")):
        s = s.replace(a, b)
    return s


def zip5(z):
    z = (z or "").strip().replace(" ", "")
    digits = "".join(c for c in z if c.isdigit())
    return digits[:5] if len(digits) >= 5 else ""


def yr(d):
    return int(d[:4]) if d and len(d) >= 4 and d[:4].isdigit() else None


def add_years(d, n):
    return f"{int(d[:4]) + n}{d[4:]}" if d and len(d) >= 4 and d[:4].isdigit() else ""


def dist_tokens(n):
    return n.replace("/", " ").split()


class Log:
    def __init__(self, p):
        self.fh = open(p, "w")

    def __call__(self, *a):
        m = " ".join(str(x) for x in a)
        print(m)
        self.fh.write(m + "\n")

    def close(self):
        self.fh.close()


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "out"
    log = Log(os.path.join(outdir, "merger.log"))
    P = lambda f: os.path.join(outdir, f)

    # ---------- 主表 ----------
    comp = []
    for r in csv.DictReader(open(P("delist_companies.csv"))):
        cik = (r["cik"] or "").strip().lstrip("0")
        if not cik:
            continue
        end = (r.get("last_10k") or "").strip() or (r.get("delist_form_date") or "").strip()
        comp.append({"cik": cik, "name": r["name"].strip(), "norm": norm(r["name"]),
                     "last_10k": (r.get("last_10k") or "").strip(),
                     "delist_form_date": (r.get("delist_form_date") or "").strip(),
                     "end": end})
    by_cik = {c["cik"]: c for c in comp}
    # 事件日期补充（master_universe 的 delist_event_25 / delist_proxy_date）
    for r in csv.DictReader(open(P("master_universe.csv"))):
        c = by_cik.get((r["cik"] or "").strip().lstrip("0"))
        if not c:
            continue
        ev = (r.get("delist_event_25") or "").strip() or (r.get("delist_proxy_date") or "").strip()
        c["event_date"] = ev
        if not c["end"]:
            c["end"] = ev
    for c in comp:
        c.setdefault("event_date", "")
        c["delist_year"] = yr(c["end"]) or yr(c["event_date"]) or yr(c["delist_form_date"])
    log(f"[base] 退市公司 {len(comp):,}，有 last_10k {sum(1 for c in comp if c['last_10k']):,}，"
        f"有 delist_year {sum(1 for c in comp if c['delist_year']):,}")

    # ---------- 全量 CIK 名录 ----------
    info = {}
    for r in csv.DictReader(open(P("all_ciks.csv"))):
        info[r["cik"]] = r
    log(f"[base] all_ciks {len(info):,}")

    univ_ciks = set(by_cik)
    # 退市宇宙内 CIK 的退市年（判断"对方其实更早退市"）
    uni_end = {}
    for r in csv.DictReader(open(P("delist_universe.csv"))):
        k = (r["cik"] or "").strip().lstrip("0")
        uni_end[k] = (r.get("last_10k") or "").strip() or (r.get("delist_form_date") or "").strip()

    def alive_after(cik, end):
        """对方是否有晚于 end 的年报/仍在活跃。返回 (bool, 依据)"""
        if not end:
            return (True, "no_end_date")
        oi = info.get(cik)
        if not oi:
            return (False, "not_in_submissions")
        l10 = (oi.get("last_10k") or "").strip()
        if l10 and l10 > end:
            return (True, f"last_10k={l10}")
        if cik in uni_end:
            return (False, f"also_delisted(last_10k={uni_end[cik]})")
        if oi.get("entity_type") in ("operating", "investment") and not l10:
            return (True, "active_no_10k")
        return (False, "no_later_filing")

    # ---------- 退市方自己的旧名 ----------
    own_former = defaultdict(set)
    for r in csv.DictReader(open(P("former_names.csv"))):
        n = norm(r["former_name"])
        if n:
            own_former[r["cik"].lstrip("0")].add(n)
    log(f"[base] universe formerNames 覆盖 CIK {len(own_former):,}（共 "
        f"{sum(len(v) for v in own_former.values()):,} 个归一化旧名）")

    # ---------- B1 索引：全量 formerNames ----------
    f_idx = defaultdict(list)
    for r in csv.DictReader(open(P("all_former_names.csv"))):
        n = norm(r["former_name"])
        if n:
            f_idx[n].append((r["cik"].lstrip("0"), r["from_date"].strip(), r["to_date"].strip()))
    log(f"[B1] formerNames 索引 {len(f_idx):,} 个归一化名 / "
        f"{sum(len(v) for v in f_idx.values()):,} 条记录")

    def overlaps(fr, to, lo, hi):
        if not lo:
            return False
        fr = fr or "0000-00-00"
        to = to or "9999-12-31"
        return fr <= hi and to >= lo

    b1_rows = []
    b1_stat = Counter()
    b1_diag_current = 0
    for c in comp:
        end = c["end"]
        lo = end
        hi = add_years(end, 3) if end else ""
        names = {c["norm"]} | own_former.get(c["cik"], set())
        names.discard("")
        cands = defaultdict(lambda: {"matched": set(), "evidence": [], "time_ok": False})
        for n in names:
            for (ocik, fr, to) in f_idx.get(n, ()):
                if ocik == c["cik"]:
                    continue
                d = cands[ocik]
                d["matched"].add(n)
                tok = overlaps(fr, to, lo, hi) if (lo and hi) else False
                if tok:
                    d["time_ok"] = True
                d["evidence"].append((n, fr, to, tok))
        if not cands:
            b1_stat["no_name_hit"] += 1
            continue
        for ocik, d in cands.items():
            alive, why = alive_after(ocik, end)
            nmatch = len(d["matched"])
            popularity = max(len({x[0] for x in f_idx.get(n, ())}) for n in d["matched"])
            def _dist(n):
                t = dist_tokens(n)
                return (len(t) >= 2 and len(n) >= 8) or (len(t) == 1 and len(n) >= 12)
            distinctive = nmatch >= 2 or any(_dist(n) for n in d["matched"])
            if not d["time_ok"]:
                conf = "low"          # 无时间重叠：只是同名/改名，不硬配
            elif not distinctive or popularity > GENERIC:
                conf = "low"          # 名字过于通用/太短：证据不足，留 unknown
            elif alive:
                conf = "high"         # 旧名反查 + 时间窗重叠 + 承继方仍在申报
            else:
                conf = "medium"       # 名字有辨识度、时间对，但承继方已无更晚申报
            d["evidence"].sort(key=lambda x: (not x[3], x[0]))
            ev = "; ".join(f"formerName='{n}' on CIK {ocik} [{fr}..{to}]"
                           + ("(overlap)" if t else "") for n, fr, to, t in d["evidence"][:3])
            b1_rows.append({
                "cik": c["cik"], "name": c["name"], "merged_into_cik": ocik,
                "confidence": conf, "evidence_type": "former_name",
                "detail": f"{ev}; window={lo}..{hi}; successor {why}",
                "delist_year": c["delist_year"] or "",
            })
            b1_stat[conf] += 1
    log(f"[B1] 候选行 {len(b1_rows):,} 分布 {dict(b1_stat)}")

    # B1 诊断（不计入交付）：退市名 == 另一 CIK 的当前名
    c_idx = defaultdict(list)
    for cik, r in info.items():
        n = norm(r["name"])
        if n:
            c_idx[n].append(cik)
    for c in comp:
        for ocik in c_idx.get(c["norm"], ()):
            if ocik != c["cik"] and alive_after(ocik, c["end"])[0]:
                b1_diag_current += 1
                break
    log(f"[B1diag] 退市名与另一活跃 CIK 当前名相同的公司数 {b1_diag_current:,}（未计入交付）")

    # ---------- B2 同址 + 名似 ----------
    addr_idx = defaultdict(list)
    for r in csv.DictReader(open(P("all_addr.csv"))):
        z = zip5(r["zip"])
        st = nstreet(r["street1"])
        if not z or len(st) < 6:
            continue
        addr_idx[(st, z)].append(r["cik"].lstrip("0"))
    log(f"[B2] 地址索引 {len(addr_idx):,} 个 street1+zip")

    my_addr = defaultdict(set)
    for r in csv.DictReader(open(P("addresses.csv"))):
        z = zip5(r["zip"])
        st = nstreet(r["street1"])
        if z and len(st) >= 6:
            my_addr[r["cik"].lstrip("0")].add((st, z))
    log(f"[B2] 退市方地址覆盖 {len(my_addr):,} / {len(comp):,}")

    b2_rows = []
    b2_stat = Counter()
    for c in comp:
        end = c["end"]
        cands = defaultdict(list)
        for key in my_addr.get(c["cik"], ()):
            occ = addr_idx.get(key, ())
            if len(set(occ)) > MASS_ADDR:
                b2_stat["mass_addr_skip"] += 1
                continue
            for ocik in occ:
                if ocik != c["cik"]:
                    cands[ocik].append(key)
        for ocik, keys in cands.items():
            oi = info.get(ocik)
            if not oi:
                continue
            o_norm = norm(oi["name"])
            sim = difflib.SequenceMatcher(None, c["norm"], o_norm).ratio()
            if sim < 0.7:
                b2_stat["sim_below"] += 1
                continue
            alive, why = alive_after(ocik, end)
            if not alive:
                b2_stat["not_later"] += 1
                continue
            # 强形态：归一化名完全相同，或一方是另一方的词级前缀（>=3 词）。
            # 仅差尾号/序列字母的兄弟基金（如 ...TRUST 1990 A vs 1990 D）不算。
            at, bt = dist_tokens(c["norm"]), dist_tokens(o_norm)
            k = min(len(at), len(bt))
            same = c["norm"] == o_norm and (len(c["norm"]) >= 6 or k >= 2)
            prefix = k >= 3 and at[:k] == bt[:k]
            # 兄弟主体护栏：差异只在尾部罗马数字/序号/单字母（... L P II vs ... L P）
            tail = at[k:] if len(at) > len(bt) else bt[k:]
            sibling = bool(tail) and len(tail) == 1 and re.fullmatch(
                r"(?:I{1,3}|IV|VI{0,3}|IX|X|[A-Z]|[0-9]{1,4})", tail[0])
            strong = (same or prefix) and not sibling
            if strong:
                b2_stat["strong"] += 1
            conf = "medium" if strong else "low"
            b2_rows.append({
                "cik": c["cik"], "name": c["name"], "merged_into_cik": ocik,
                "confidence": conf, "evidence_type": "address_name",
                "detail": (f"same street1+zip={keys[0][0]} {keys[0][1]}; "
                           f"name_sim={sim:.2f} ('{oi['name']}'); successor {why}; "
                           f"window_after={end}"),
                "delist_year": c["delist_year"] or "",
            })
            b2_stat[conf] += 1
    log(f"[B2] 候选行 {len(b2_rows):,} 分布 {dict(b2_stat)}")

    # ---------- 合并 + 去重（每家退市公司一行，取最优） ----------
    rank = {"high": 0, "medium": 1, "low": 2}
    etype_rank = {"former_name": 0, "address_name": 1}
    best = {}
    for r in b1_rows + b2_rows:
        k = r["cik"]
        cur = best.get(k)
        rk = (rank[r["confidence"]], etype_rank[r["evidence_type"]])
        if cur is None or rk < (rank[cur["confidence"]], etype_rank[cur["evidence_type"]]):
            best[k] = r

    merged = [r for r in best.values() if r["confidence"] in ("high", "medium")]
    low = [r for r in best.values() if r["confidence"] == "low"]
    cols = ["cik", "merged_into_cik", "confidence", "evidence_type", "evidence_detail",
            "delist_year"]
    for fn, rows in (("merged_into.csv", merged), ("merged_into_lowconf.csv", low)):
        with open(P(fn), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in sorted(rows, key=lambda x: (x["delist_year"] or "", x["cik"])):
                w.writerow([r["cik"], r["merged_into_cik"], r["confidence"],
                            r["evidence_type"], r["detail"], r["delist_year"]])
    log(f"[out] merged_into.csv {len(merged):,} 行；merged_into_lowconf.csv {len(low):,} 行")

    log("[detail] 逐条证据（含 low；格式: 退市CIK 退市名 -> 承接CIK 承接名 置信 路径 证据）")
    for r in sorted(best.values(), key=lambda x: (x["cik"])):
        tgt = (info.get(r["merged_into_cik"]) or {}).get("name", "?")
        log(f"[detail] {r['cik']} {r['name'][:40]} -> {r['merged_into_cik']} {tgt[:40]} "
            f"{r['confidence']} {r['evidence_type']} | {r['detail'][:220]}")

    # ---------- 统计 E ----------
    tot = len(comp)
    n_hm = len(merged)
    log(f"[stat] 退市公司总数 {tot:,}；high/medium 判定被并购/承继 {n_hm:,} "
        f"({n_hm / tot * 100:.2f}%)；仍未判定(真退市/失联) {tot - n_hm:,}")
    log(f"[stat] 分证据路径 high/medium: " + str(Counter(r["evidence_type"] for r in merged)))
    dec = Counter()
    for r in merged:
        y = r["delist_year"]
        dec[f"{(int(y) // 10) * 10}s" if y else "unknown"] += 1
    tot_dec = Counter()
    for c in comp:
        y = c["delist_year"]
        tot_dec[f"{(int(y) // 10) * 10}s" if y else "unknown"] += 1
    log("[stat] decade  判定并购  退市总数  比例")
    for k in sorted(dec, key=lambda x: (x == "unknown", x)):
        log(f"[stat]   {k:<9} {dec[k]:>8,} {tot_dec[k]:>9,}  {dec[k]/max(1, tot_dec[k])*100:5.1f}%")
    log(f"[stat] low 置信单独文件 {len(low):,} 家；未收录(无候选) {tot - len(best):,} 家")
    log.close()


if __name__ == "__main__":
    main()
