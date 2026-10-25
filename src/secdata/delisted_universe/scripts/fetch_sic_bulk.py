#!/usr/bin/env python3
"""fetch_sic_bulk.py — 从 SEC 官方 submissions.zip 抽取 universe 内每个 CIK 的 SIC/实体类型

用法（在 .39 上）：
  python3 -I fetch_sic_bulk.py submissions.zip delist_universe.csv sics.csv

原理：submissions.zip 内每个 CIK 一个 CIK##########.json；用 zipfile 随机访问只解压
xuniverse 内 4.8 万个条目（不必解全包）。输出 cik,sic,sic_desc,entity_type,category,exchanges,tickers。
"""
import csv
import json
import sys
import zipfile


def main():
    zpath, uni_csv, out_csv = sys.argv[1], sys.argv[2], sys.argv[3]
    ciks = []
    for r in csv.DictReader(open(uni_csv)):
        c = (r.get("cik") or "").strip().lstrip("0")
        if c:
            ciks.append(c)
    print(f"universe CIK: {len(ciks):,}, zip: {zpath}")

    miss = 0
    rows = []
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())
        print(f"zip entries: {len(names):,}")
        for i, c in enumerate(ciks):
            fn = f"CIK{int(c):010d}.json"
            if fn not in names:
                miss += 1
                continue
            try:
                with zf.open(fn) as f:
                    d = json.load(f)
            except Exception as e:
                print(f"  parse fail {fn}: {e}")
                miss += 1
                continue
            ex = [str(x) for x in (d.get("exchanges") or []) if x]
            tk = [str(x) for x in (d.get("tickers") or []) if x]
            rows.append([
                c, d.get("sic", ""), d.get("sicDescription", ""), d.get("entityType", ""),
                d.get("category", ""), "|".join(ex), "|".join(tk),
            ])
            if (i + 1) % 5000 == 0:
                print(f"  {i+1:,}/{len(ciks):,} hit={len(rows):,} miss={miss:,}")

    with open(out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "sic", "sic_desc", "entity_type", "category", "exchanges", "tickers"])
        w.writerows(rows)
    print(f"-> {out_csv}: {len(rows):,} rows, miss {miss:,}")


if __name__ == "__main__":
    main()
