#!/usr/bin/env python3
"""probe_form25.py — 抽查 Form 25 / 25-NSE 正文，判断能否可靠解析退市原因

用法（在 .39 上）：
  python3 -I probe_form25.py delist_events_fts.csv <outdir> [n_per_form] [--dump]

对每种 form 各抽 N 条（按 file_date 均匀取），抓 index.json 选主文档，
剥标签后统计原因关键词命中，并把首段正文 dump 出来供人工判断。
"""
import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = os.environ.get("SEC_EDGAR_UA", "")
KEYWORDS = [
    "merger", "merged", "consolidation", "acquisition", "acquired", "tender offer",
    "definitive agreement", "liquidation", "liquidat", "dissolution", "dissolved",
    "bankruptcy", "chapter 11", "chapter 7", "receivership", "cease operations",
    "failure to meet", "minimum listing", "listing standards", "delist",
    "reverse split", "reverse stock split", "going private", "business combination",
    "reorganization", "spin-off", "spun off", "redemption", "matured", "called for redemption",
]
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".xsd", ".zip")


def get(url, opener, timeout=30):
    for attempt in range(3):
        try:
            r = opener.open(url, timeout=timeout)
            return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dump = "--dump" in sys.argv
    ev_csv, outdir = args[0], args[1]
    n_per = int(args[2]) if len(args) > 2 else 10
    os.makedirs(outdir, exist_ok=True)

    rows = list(csv.DictReader(open(ev_csv)))
    by_form = {}
    for r in rows:
        by_form.setdefault(r["form"], []).append(r)
    sample = []
    for form, lst in sorted(by_form.items()):
        lst.sort(key=lambda r: r["file_date"])
        step = max(1, len(lst) // n_per)
        sample += [lst[i * step] for i in range(min(n_per, len(lst)))]

    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", UA)]  # 不加 Accept-Encoding：SEC 会回 gzip，urllib 不自动解压
    out = open(os.path.join(outdir, "form25_probe.txt"), "w")
    stat = {"fetched": 0, "no_doc": 0, "fail": 0, "kw_hit": 0, "empty": 0}
    for i, r in enumerate(sample, 1):
        acc = r["accession"]
        cik = re.split(r"[|,]", r["ciks"] or "")[0].strip().lstrip("0") or "0"
        url_dir = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
        line = [f"=== {i}/{len(sample)} form={r['form']} acc={acc} cik={cik} date={r['file_date']} name={r['display_names'][:60]}"]
        try:
            idx = json.loads(get(url_dir + "index.json", opener).decode("utf-8", "replace"))
            items = idx.get("directory", {}).get("item", [])
        except Exception as e:
            stat["fail"] += 1
            line.append(f"  index FAIL {e}")
            print("\n".join(line)); out.write("\n".join(line) + "\n")
            continue
        cands = [it["name"] for it in items
                 if not it["name"].lower().endswith(SKIP_EXT) and "-index" not in it["name"].lower()]
        prio = [c for c in cands if re.search(r"\.(htm|html|xml)$", c, re.I)]
        if not prio:
            stat["no_doc"] += 1
            line.append(f"  no html/xml doc; items={[it['name'] for it in items][:8]}")
            print("\n".join(line)); out.write("\n".join(line) + "\n")
            continue
        # 主文档优先：名字里含 form25/exhibit 少的、非 R\d+.htm 的、体积最大的 htm
        sz = {it["name"]: (it.get("size") or "0") for it in items}
        prio.sort(key=lambda c: (bool(re.match(r"R\d+\.htm$", c)), -int(sz.get(c) or 0)))
        doc = prio[0]
        try:
            raw = get(url_dir + doc, opener, timeout=40).decode("utf-8", "replace")
        except Exception as e:
            stat["fail"] += 1
            line.append(f"  doc FAIL {e} ({doc})")
            print("\n".join(line)); out.write("\n".join(line) + "\n")
            continue
        stat["fetched"] += 1
        txt = html.unescape(TAG.sub(" ", raw))
        txt = WS.sub(" ", txt).strip()
        low = txt.lower()
        hits = sorted({k for k in KEYWORDS if k in low})
        if hits:
            stat["kw_hit"] += 1
        if len(txt) < 100:
            stat["empty"] += 1
        line.append(f"  doc={doc} len={len(txt)} kw={hits}")
        # 关键词上下文
        for k in hits[:6]:
            m = low.find(k)
            line.append(f"    [{k}] ...{txt[max(0, m-90):m+130].strip()}...")
        if dump:
            line.append("  TEXT>>> " + txt[:1800])
        print("\n".join(line))
        out.write("\n".join(line) + "\n")
        time.sleep(0.45)
    out.write(f"\nSUMMARY {stat}\n")
    out.close()
    print(f"\nSUMMARY {stat} -> {os.path.join(outdir, 'form25_probe.txt')}")


if __name__ == "__main__":
    main()
