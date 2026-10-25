#!/usr/bin/env python3
"""extract_form25_reasons.py — 全量解析 Form 25 / 25-NSE 正文的退市原因

用法（在 .39 上）：
  python3 -I extract_form25_reasons.py delist_events_fts.csv outdir [--limit=N]

探测结论（20 条抽样）：
  * issuer Form 25 = 机械表单，正文无原因叙述；唯一结构化信号是勾选的 17 CFR 240.12d2-2 条款
    （勾选格在 HTML 里是 Wingdings &#120; -> 文本 "x"；未勾选是 &#168; -> "¨"）。
    (a)(3)=证券被替换为其他证券仅剩现金对价（并购/换股/兑付单位的典型），(a)(4)=全部权利消灭，
    (a)(1)(2)=赎回/到期/退休，(b)=交易所主动摘牌，(c)=发行人自愿撤回。
    少数 1990s-2000s 的 Form 25 用 .txt 全量提交并在封面写了叙述，故 form 25 优先取全量 .txt。
  * Form 25-NSE = 交易所通知；NYSE（filer 0000876661）的 ruleprovisionnotice.htm 是叙述性
    通知，常直接写明 merger / bankruptcy / 跌破 1 美元等；NYSE Arca / AMEX 的 primary_doc.xml
    只有条款代码。

输出 outdir/form25_reasons.csv:
  accession,cik,form,file_date,rule_checked,cause_category,kw_hits,evidence_snippet,doc,status
支持断点续跑（status=ok 的 accession 跳过）。限速 ~5-6 req/s（8 线程，低于 SEC 10/s 上限）。
"""
import csv
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = os.environ.get("SEC_EDGAR_UA", "")
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".xsd", ".zip")
RULE = re.compile(r"12d2-2\s*\(\s*([abcd])\s*\)\s*(?:\(\s*(\d)\s*\))?", re.I)
CHECKED = re.compile(r"\[\s*[xX]\s*\]|[☒✓✔]|(?<![A-Za-z0-9])[xX](?![A-Za-z0-9])")

KW = {
    "merger": ["merger", "merged with", "consolidation", "acquired by", "acquisition of",
               "business combination", "definitive agreement", "plan of merger", "share exchange",
               "cash-out", "cash out merger"],
    "bankruptcy_liquidation": ["bankruptcy", "chapter 11", "chapter 7", "liquidation",
                               "liquidat", "dissolution", "dissolved", "receivership",
                               "cease operations", "ceased operations"],
    "listing_standards": ["failure to meet", "failed to meet", "minimum listing",
                          "listing standards", "continued listing", "average closing price",
                          "minimum bid price", "timely filing", "delinquent",
                          "market value of", "compliance plan"],
}
RULE_CAT = {"a3": "merger_substitution", "a4": "rights_extinguished", "a2": "redemption_maturity",
            "a1": "redemption_maturity", "b": "exchange_action", "c": "issuer_voluntary"}
FILLER = re.compile(r"\b(?:pursuant|to|17|cfr|240\.12d2-2|item|the|of|under)\b|[\(\)\.\d\s]", re.I)


def get(url, opener, timeout=35, cap=None):
    last = None
    for attempt in range(4):
        try:
            with opener.open(url, timeout=timeout) as r:
                return r.read(cap) if cap else r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429, 500, 502, 503) and attempt < 3:
                time.sleep(8 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last = e
            if attempt < 3:
                time.sleep(4 * (attempt + 1))
                continue
            raise
    raise last


def checked_rules(txt):
    """返回被勾选的条款代码列表，如 ['a3'] 或 ['c']。"""
    out = []
    for m in RULE.finditer(txt):
        i = m.start()
        seg = txt[max(0, i - 60):i]
        seg = seg.replace("¨", " ").replace(" ", " ")
        tail = FILLER.sub(" ", seg)
        if CHECKED.search(tail):
            code = m.group(1).lower() + (m.group(2) or "")
            out.append(code)
    return list(dict.fromkeys(out))


def kw_scan(low):
    hits, snippet, first_i = [], "", -1
    for cat in ("merger", "bankruptcy_liquidation", "listing_standards"):
        for k in KW[cat]:
            i = low.find(k)
            if i >= 0:
                hits.append(f"{cat}:{k}")
                if first_i < 0:
                    first_i = i
    return hits, first_i


def one(ev, opener):
    acc = ev["accession"]
    toks = [t.strip().lstrip("0") for t in re.split(r"[|,]", ev["ciks"] or "") if t.strip()]
    form = ev["form"]
    row = [acc, toks[0] if toks else "", form, ev["file_date"], "", "", "", "", "", ""]
    accn = acc.replace("-", "")
    idx, used_cik = None, ""
    for ck in toks[:2]:
        try:
            idx = json.loads(get(f"https://www.sec.gov/Archives/edgar/data/{ck}/{accn}/index.json",
                                 opener).decode("utf-8", "replace"))
            used_cik = ck
            break
        except Exception:
            continue
    if idx is None:
        row[9] = "index_fail"
        return row
    row[1] = used_cik
    items = idx.get("directory", {}).get("item", [])

    def fetch_doc(name, cap=None):
        return get(f"https://www.sec.gov/Archives/edgar/data/{used_cik}/{accn}/{name}",
                   opener, timeout=45, cap=cap).decode("utf-8", "replace")

    txt = ""
    if form == "25":
        # 优先全量 .txt（含封面叙述 + 勾选条款），否则最大 htm
        full = [it for it in items if it["name"].endswith(".txt")
                and it["name"].startswith(acc)]
        htm = [it for it in items if re.search(r"\.(htm|html)$", it["name"], re.I)
               and "-index" not in it["name"].lower()]
        htm.sort(key=lambda it: (bool(re.match(r"R\d+\.htm$", it["name"])),
                                 -int(it.get("size") or 0)))
        picks = ([full[0]["name"]] if full else []) + ([htm[0]["name"]] if htm else [])
        if not picks:
            row[9] = "no_doc"
            return row
        row[8] = picks[0]
        try:
            txt = WS.sub(" ", html.unescape(TAG.sub(" ", fetch_doc(picks[0], cap=2_500_000)))).strip()
        except Exception:
            row[9] = "doc_fail"
            return row
        if not checked_rules(txt) and len(picks) > 1:  # 全量 .txt 里没勾选 -> 再试 htm 主文档
            try:
                t2 = WS.sub(" ", html.unescape(TAG.sub(" ", fetch_doc(picks[1], cap=1_500_000)))).strip()
                if checked_rules(t2):
                    txt, row[8] = t2, picks[1]
            except Exception:
                pass
    else:
        cands = [it for it in items
                 if not it["name"].lower().endswith(SKIP_EXT)
                 and "-index" not in it["name"].lower()]
        prio = [it for it in cands if re.search(r"\.(htm|html)$", it["name"], re.I)]
        if not prio:
            prio = [it for it in cands if it["name"].lower().endswith(".xml")]
        if not prio:
            row[9] = "no_doc"
            return row
        prio.sort(key=lambda it: (0 if "ruleprovision" in it["name"].lower() else 1,
                                  bool(re.match(r"R\d+\.htm$", it["name"])),
                                  -int(it.get("size") or 0)))
        row[8] = prio[0]["name"]
        try:
            txt = WS.sub(" ", html.unescape(TAG.sub(" ", fetch_doc(prio[0]["name"])))).strip()
        except Exception:
            row[9] = "doc_fail"
            return row

    low = txt.lower()
    codes = checked_rules(txt)
    row[4] = "|".join(codes)
    hits, i = kw_scan(low)
    row[6] = "|".join(hits[:6])
    if form == "25":
        row[5] = RULE_CAT.get(codes[0], "unknown_no_rule") if codes else "unknown_no_rule"
        # 封面叙述里的并购/破产关键词附在 kw_hits，仅当条款是 a3/a4 时升级语义
        if codes and codes[0] in ("a3", "a4") and any(h.startswith("merger") for h in hits):
            row[5] = "merger_substitution_confirmed"
    else:
        cat = "none"
        for c in ("merger", "bankruptcy_liquidation", "listing_standards"):
            if any(h.startswith(c) for h in hits):
                cat = c
                break
        row[5] = cat
    if i >= 0:
        row[7] = WS.sub(" ", txt[max(0, i - 110):i + 190]).strip()
    row[9] = "ok"
    return row


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ev_csv, outdir = args[0], args[1]
    limit = None
    for a in sys.argv:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    os.makedirs(outdir, exist_ok=True)
    outp = os.path.join(outdir, "form25_reasons.csv")
    done = set()
    if os.path.exists(outp):
        for r in csv.DictReader(open(outp)):
            if r.get("status") == "ok":
                done.add(r["accession"])
    rows = [r for r in csv.DictReader(open(ev_csv)) if r["accession"] not in done]
    # 先 form 25（快、信号强），再 25-NSE
    rows.sort(key=lambda r: (r["form"] != "25", r["file_date"]))
    if limit:
        rows = rows[:limit]
    print(f"todo {len(rows):,} (skip done {len(done):,})", flush=True)
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", UA)]
    new = not os.path.exists(outp)
    fh = open(outp, "a", newline="")
    w = csv.writer(fh)
    if new:
        w.writerow(["accession", "cik", "form", "file_date", "rule_checked", "cause_category",
                    "kw_hits", "evidence_snippet", "doc", "status"])
    t0 = time.time()
    n = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = []
        for ev in rows:
            futs.append(ex.submit(one, ev, opener))
            time.sleep(0.06)  # 8 线程，每事件 2 请求，实测 ~5 req/s（SEC 上限 10/s）
            if len(futs) >= 400:
                for fu in futs:
                    try:
                        row = fu.result()
                        w.writerow(row)
                        failed += row[9] in ("index_fail", "doc_fail")
                    except Exception as e:
                        print("row err", e, flush=True)
                        failed += 1
                    n += 1
                fh.flush()
                futs = []
                el = time.time() - t0
                print(f"  {n:,}/{len(rows):,} {el:.0f}s ({n/el if el else 0:.1f}/s)", flush=True)
        for fu in futs:
            try:
                row = fu.result()
                w.writerow(row)
                failed += row[9] in ("index_fail", "doc_fail")
            except Exception as e:
                print("row err", e, flush=True)
                failed += 1
            n += 1
    fh.close()
    print(f"DONE {n:,} in {time.time()-t0:.0f}s -> {outp}", flush=True)
    if failed:
        raise SystemExit(f"{failed} Form 25 documents failed; rerun to retry")


if __name__ == "__main__":
    main()
