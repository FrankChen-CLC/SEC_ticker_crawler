#!/usr/bin/env python3
"""fix_form25_rules.py — 补齐 25-NSE XML 通知里的 <ruleProvision> 条款（勾选框逻辑抓不到它）

背景：NYSE Arca / AMEX / Cboe 的 25-NSE 用结构化 XML（primary_doc.xml），没有勾选框，
只有 <ruleProvision>17 CFR 240.12d2-2(a)(3)</ruleProvision>；主流程靠"勾选标记"解析，
这些行 rule_checked 为空、cause_category=none。

做法：对 form=25-NSE 且 rule_checked 为空的行重取主文档，正则抽 ruleProvision；
并按条款语义给 cause_category 兜底：
  a3/a4 -> merger_substitution（证券被替换/权利消灭，并购现金对价或换股的交易所表述）
  a1/a2 -> redemption_maturity
  b     -> exchange_action
  c     -> issuer_voluntary

用法（.39 上，主任务 DONE 之后跑）：
  python3 -I fix_form25_rules.py out/form25_reasons.csv
原地重写该 CSV（先写 .tmp 再 rename）。
"""
import csv
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = os.environ.get("SEC_EDGAR_UA", "")
RP = re.compile(r"<ruleProvision>\s*([^<]+?)\s*</ruleProvision>", re.I)
CODE = re.compile(r"12d2-2\s*\(\s*([abcd])\s*\)\s*(?:\(\s*(\d)\s*\))?", re.I)
CAT = {"a3": "merger_substitution", "a4": "merger_substitution", "a2": "redemption_maturity",
       "a1": "redemption_maturity", "b": "exchange_action", "c": "issuer_voluntary"}


def fetch(url):
    op = urllib.request.build_opener()
    op.addheaders = [("User-Agent", UA)]
    for a in range(3):
        try:
            with op.open(url, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503) and a < 2:
                time.sleep(6 * (a + 1))
                continue
            raise
        except Exception:
            if a < 2:
                time.sleep(3 * (a + 1))
                continue
            raise
    return ""


def one(row):
    accn = row["accession"].replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{accn}/{row['doc']}"
    try:
        txt = fetch(url)
    except Exception:
        return None
    m = RP.search(txt)
    code = ""
    if m:
        c = CODE.search(m.group(1))
        if c:
            code = c.group(1).lower() + (c.group(2) or "")
    elif row["doc"].lower().endswith(".xml"):
        c = CODE.search(txt)      # 兜底：全文第一次出现的条款
        if c:
            code = c.group(1).lower() + (c.group(2) or "")
    return code


def main():
    path = sys.argv[1]
    rows = list(csv.DictReader(open(path)))
    cols = list(rows[0].keys())
    todo = [r for r in rows if r["form"] == "25-NSE" and not r["rule_checked"]]
    print(f"rows {len(rows):,}; 待补条款 {len(todo):,}")
    got = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = []
        for r in todo:
            futs.append((r, ex.submit(one, r)))
            time.sleep(0.12)
        for i, (r, fu) in enumerate(futs, 1):
            try:
                code = fu.result()
            except Exception:
                code = None
            if code:
                r["rule_checked"] = code
                got += 1
            if i % 200 == 0:
                print(f"  {i:,}/{len(todo):,} got={got:,}", flush=True)
    # 条款语义兜底
    recls = 0
    for r in rows:
        if r["form"] == "25-NSE" and r["cause_category"] == "none" and r["rule_checked"]:
            cat = CAT.get(r["rule_checked"])
            if cat:
                r["cause_category"] = cat
                recls += 1
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    print(f"补齐条款 {got:,} 行；按条款重分类 {recls:,} 行 -> {path}")


if __name__ == "__main__":
    main()
