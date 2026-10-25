#!/usr/bin/env python3
"""nasdaqdir_match.py — 把 SymDir 符号目录 spell 按名称+时间窗匹配到 CIK
输入: nasdaqdir/spells.csv, out/delist_companies.csv
输出:
  out/ticker_history_nasdaqdir.csv   命中 (cik,ticker,exchange,first_seen,last_seen,...)
  out/nasdaqdir_unmatched.csv        未命中/歧义/时间冲突, 供抽查
日志: out/nasdaqdir.log
匹配分三级: 归一化全等 / token 集合全等 (0.95) / difflib 模糊 >=0.86 且共享 len>=5 token (0.6-0.9)
时间约束: 快照年窗 ∩ [first_10k, last_10k+2] 非空。
用法: python3 -I scripts/nasdaqdir_match.py
"""
import csv
import os
import sys
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nasdaqdir_parse import norm_name  # noqa: E402  (共用归一化规则, 保证两端一致)

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
DL = os.path.join(BASE, "nasdaqdir")
OUT = os.path.join(BASE, "out")
LOG = os.path.join(OUT, "nasdaqdir.log")
FUZZY_MIN = 0.86
MIN_SHARED_TOKEN = 5
MAX_CAND = 4000
# 2008-2010 NYSE 8 字缩写名的人工别名 (仅少数知名标的)
NAME_ALIAS = {
    "ML": "MERRILL LYNCH",
    "FANNIE MAE": "FEDERAL NATIONAL MORTGAGE ASSOCIATION",
    "FREDDIE MAC": "FEDERAL HOME LOAN MORTGAGE",
}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def fy(d):
    return int(d[:4]) if d else None


def load_companies():
    comps = []
    for r in csv.DictReader(open(os.path.join(OUT, "delist_companies.csv"))):
        st = fy(r.get("first_10k") or r.get("first_s1"))
        en = fy(r.get("last_10k"))
        if st is None:
            continue
        norm = norm_name(r["name"])
        comps.append({"cik": r["cik"], "name": r["name"],
                      "norm": NAME_ALIAS.get(norm, norm),
                      "start": st, "end": (en + 2) if en else st + 10})
    return comps


def year_overlap(a0, a1, b0, b1):
    return max(0, min(a1, b1) - max(a0, b0))


def tok_ratio(a, b):
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    # 只对长词前缀放宽 (BANCORP/BANCORPORATION), 避免 RANGE/RANGEFORD 误配
    if min(len(a), len(b)) >= 7 and abs(len(a) - len(b)) >= 4 \
            and (a.startswith(b) or b.startswith(a)):
        r = max(r, 0.9)
    return r


def tokens_compatible(A, B):
    """模糊匹配护栏: 两边每个 token 都能在对方找到 >=0.90 的对应词
    (避免 FERO INDUSTRIES vs VELCRO INDUSTRIES 这类只靠行业词相似命中)"""
    for t in A:
        if max(tok_ratio(t, u) for u in B) < 0.90:
            return False
    for u in B:
        if max(tok_ratio(t, u) for t in A) < 0.90:
            return False
    return True


def main():
    log("=== nasdaqdir_match start ===")
    comps = load_companies()
    log(f"[load] delist_companies 可用={len(comps)}")

    exact_idx = defaultdict(list)          # norm -> [cix]
    tokset_idx = defaultdict(list)         # frozenset(tokens) -> [cix]
    tok_bucket = defaultdict(list)         # token(len>=5) -> [(cix,year)]
    for i, c in enumerate(comps):
        norm = c["norm"]
        exact_idx[norm].append(i)
        toks = frozenset(norm.split())
        tokset_idx[toks].append(i)
        for t in toks:
            if len(t) >= MIN_SHARED_TOKEN:
                tok_bucket[t].append(i)

    spells = []
    with open(os.path.join(DL, "spells.csv")) as f:
        for r in csv.DictReader(f):
            r["_y0"] = fy(r["first_seen"])
            r["_y1"] = fy(r["last_seen"])
            spells.append(r)
    log(f"[load] spells={len(spells)}")

    results = []       # matched rows
    unmatched = []
    stat = Counter()
    discard_extra = 0

    for sp in spells:
        norm = NAME_ALIAS.get(sp["norm_name"], sp["norm_name"])
        y0, y1 = sp["_y0"], sp["_y1"]
        sp_dates = sp.get("snapshot_dates", "").split()
        sp_years = [int(d[:4]) for d in sp_dates]
        if not norm:
            unmatched.append((sp, "empty_name", 0, "", ""))
            stat["unmatched_empty"] += 1
            continue
        tokens = frozenset(norm.split())

        cand = set()
        name_hit = False
        for i in exact_idx.get(norm, ()):
            cand.add(i)
            name_hit = True
        for i in tokset_idx.get(tokens, ()):
            cand.add(i)
            name_hit = True
        n_name_cand = len(cand)

        # 模糊候选：共享 >=5 字符 token
        fuzz_pool = set()
        for t in tokens:
            if len(t) >= MIN_SHARED_TOKEN:
                fuzz_pool.update(tok_bucket.get(t, ()))
        if len(fuzz_pool) > MAX_CAND:
            # 只用最稀有 token 的桶
            rare = sorted((t for t in tokens if len(t) >= MIN_SHARED_TOKEN),
                          key=lambda t: len(tok_bucket.get(t, ())))
            fuzz_pool = set()
            for t in rare[:2]:
                fuzz_pool.update(tok_bucket.get(t, ()))
            fuzz_pool = set(sorted(fuzz_pool)[:MAX_CAND])
        if fuzz_pool:
            name_hit = True
        cand |= fuzz_pool

        scored = []
        name_level_hit = False
        for i in cand:
            c = comps[i]
            cnorm = c["norm"]
            if cnorm == norm:
                mtype, score = "exact_token", 0.95
            elif frozenset(cnorm.split()) == tokens:
                mtype, score = "exact_token", 0.95
            else:
                ctoks = set(cnorm.split())
                shared = tokens & ctoks
                if not shared or max(len(t) for t in shared) < MIN_SHARED_TOKEN:
                    continue
                ratio = SequenceMatcher(None, norm, cnorm).ratio()
                if ratio < FUZZY_MIN:
                    continue
                if not tokens_compatible(tokens, ctoks):
                    continue
                mtype = "fuzzy"
                score = 0.6 + (ratio - FUZZY_MIN) / (1 - FUZZY_MIN) * 0.3
            name_level_hit = True
            ov = sum(1 for yr in sp_years if c["start"] <= yr <= c["end"])
            if ov <= 0:
                continue  # 名称对上但窗口内无共现快照: 记为 time_conflict
            scored.append((score, ov, mtype, i))

        if not scored:
            reason = "time_conflict" if name_level_hit else "no_name_candidate"
            unmatched.append((sp, reason, len(cand), "", ""))
            stat["unmatched_time" if reason == "time_conflict" else "unmatched_name"] += 1
            continue

        scored.sort(key=lambda x: (-x[0], -x[1]))
        top = scored[0]
        tied = [s for s in scored if abs(s[0] - top[0]) < 1e-9 and s[1] == top[1]]
        if len(tied) > 1:
            # 同名同时间窗多 CIK: 不硬配
            ciks = ",".join(comps[s[3]]["cik"] for s in tied[:5])
            unmatched.append((sp, "ambiguous", len(scored), ciks, f"{top[0]:.3f}"))
            stat["unmatched_ambiguous"] += 1
            continue
        if len(scored) > 1:
            discard_extra += len(scored) - 1
        score, ov, mtype, ci = top
        c = comps[ci]
        win = [d for d, yr in zip(sp_dates, sp_years) if c["start"] <= yr <= c["end"]]
        results.append({"cik": c["cik"], "ticker": sp["symbol"], "exchange": sp["exchange"],
                        "first_seen": win[0], "last_seen": win[-1],
                        "security_type": sp["security_type"], "match_type": mtype,
                        "score": f"{score:.3f}", "evidence_name": sp["evidence_name"],
                        "norm": norm, "_y0": y0, "_y1": y1, "ci": ci})
        stat["matched_" + mtype] += 1

    log(f"[match] matched={len(results)} unmatched={len(unmatched)} 额外候选丢弃={discard_extra}")
    log(f"[match] " + str(dict(sorted(stat.items()))))

    # 合并同一 (cik,ticker) 的多个 spell
    agg = {}
    for r in results:
        k = (r["cik"], r["ticker"])
        a = agg.get(k)
        if a is None:
            agg[k] = dict(r)
        else:
            a["first_seen"] = min(a["first_seen"], r["first_seen"])
            a["last_seen"] = max(a["last_seen"], r["last_seen"])
            if r["score"] > a["score"]:
                a["score"], a["match_type"] = r["score"], r["match_type"]
            if a["security_type"] == "common" and r["security_type"] != "common":
                a["security_type"] = r["security_type"]
            if len(r["evidence_name"]) > len(a["evidence_name"]):
                a["evidence_name"] = r["evidence_name"]

    path = os.path.join(OUT, "ticker_history_nasdaqdir.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "ticker", "exchange", "first_seen", "last_seen",
                    "security_type", "match_type", "score", "evidence_name"])
        for (cik, tk), a in sorted(agg.items(), key=lambda kv: (int(kv[0][0]), kv[0][1])):
            w.writerow([cik, tk, a["exchange"], a["first_seen"], a["last_seen"],
                        a["security_type"], a["match_type"], a["score"], a["evidence_name"]])
    ciks = {k[0] for k in agg}
    log(f"[out] -> {path} rows={len(agg)} ciks={len(ciks)}")

    up = os.path.join(OUT, "nasdaqdir_unmatched.csv")
    with open(up, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "clean_name", "exchange", "first_seen", "last_seen",
                    "security_type", "reason", "n_candidates", "candidate_ciks", "best_score", "norm_name"])
        for sp, reason, n, ciks_s, bs in sorted(unmatched, key=lambda x: (x[1], x[0]["symbol"], x[0]["first_seen"])):
            w.writerow([sp["symbol"], sp["clean_name"], sp["exchange"], sp["first_seen"],
                        sp["last_seen"], sp["security_type"], reason, n, ciks_s, bs, sp["norm_name"]])
    log(f"[out] -> {up} rows={len(unmatched)}")
    log(f"[stats] unmatched reason: " + str(Counter(u[1] for u in unmatched)))

    # 票号复用: 同一 symbol 匹配到多个 CIK
    by_sym = defaultdict(set)
    for cik, tk in agg:
        by_sym[tk].add(cik)
    reuse = {t: c for t, c in by_sym.items() if len(c) > 1}
    log(f"[stats] ticker 复用(同 symbol 多 CIK)={len(reuse)} 例; 样例=" +
        str([(t, sorted(v)[:3]) for t, v in list(reuse.items())[:10]]))

    # 时间冲突明细计数: symbol 出现在多个 spell 但只匹配到部分
    sym_spells = Counter(s["symbol"] for s in spells)
    sym_matched = Counter(r["ticker"] for r in results)
    partial = sum(1 for s, n in sym_spells.items() if n > 1 and 0 < sym_matched.get(s, 0) < n)
    log(f"[stats] 多 spell symbol={sum(1 for n in sym_spells.values() if n>1)}, 部分匹配={partial}")


if __name__ == "__main__":
    main()
