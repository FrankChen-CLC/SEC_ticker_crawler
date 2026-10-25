#!/usr/bin/env python3
"""poc_delisted.py — 美股退市股全历史宇宙（去生存者偏差）EDGAR 原生 POC

三个子命令：
  index   扫 EDGAR full-index（1993Q3→今，全量申报流水）→ 每个 CIK 的上市/退市事件表
  events  轻量：EDGAR 全文搜索拉 25/25-NSE 摘牌公告（2001→今），验证用
  enrich  对 CIK 清单补 ticker 历史（submissions.formerNames/tickers）+ XBRL 财报可得性（2009→今）

Usage (configure SEC_PROXY if SEC /Archives is blocked from your IP):
  python3 -I poc_delisted.py index   # resumable historical full-index scan
  python3 -I poc_delisted.py events --start 2020-01 --end 2020-12
  python3 -I poc_delisted.py enrich --in out/delist_events_fts.csv --limit 200

输出统一在 ./out/。UA 可通过环境变量 SEC_UA 覆盖。
"""
import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import time
import urllib.request

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")
UA = os.environ.get("SEC_EDGAR_UA") or os.environ.get("SEC_UA", "")
SLEEP = float(os.environ.get("SEC_SLEEP", "0.12"))  # EDGAR 上限 10 req/s，留余量
# Some networks receive HTTP 403 for SEC /Archives. An optional proxy can
# be configured by the user without embedding a machine-specific endpoint.
PROXY = os.environ.get("SEC_PROXY", "").strip()
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
) if PROXY else urllib.request.build_opener()

# 2026-10-25 全年代实测的年报类 form 精确字符串（EDGAR 索引用无连字符变体 10KSB 等；
# NT*/NTN* 是迟交通知不算年报；10-KSB/10-KSB-A 是早期猜测名，保留兼容）
LISTED_FORMS = ("10-K", "10-K/A", "10-K405", "10-K405/A", "10-KT", "10-KT/A",
                "10KT405", "10KT405/A", "10KSB", "10KSB/A", "10KSB40", "10KSB40/A",
                "10-KSB", "10-KSB-A", "20-F", "20-F/A",
                "20FR12G", "20FR12G/A", "20FR12B", "20FR12B/A",
                "40-F", "40-F/A", "40FR12B", "40FR12B/A", "40FR12G")
IPO_FORMS = ("S-1", "S-1/A", "S-11", "S-11/A", "SB-1", "SB-1/A", "SB-2", "SB-2/A",
             "F-1", "F-1/A")
# 注意：25/25-NSE/15 常是债券/托凭证单独摘牌（实测 Apple 2019-03-14 Form 25=票据注销），
# 故 delist_form_date 仅供参考列，is_delisted 只由年报静默判定。
DELIST_FORMS = ("25", "25-NSE", "25-NSE/A", "25/A")
# 退市近似：25/25-NSE 由交易所提交；发行人自己撤注册用 S-90/Form 15
DELIST_LIKE = DELIST_FORMS + ("S-90", "S-90C", "S-90D", "15-12B", "15-15D")


def fetch(url, timeout=120, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Encoding": "gzip"})
            with _OPENER.open(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return None


def quarters(frm="1993Q3", to=None):
    """Yield quarters from the first EDGAR full index through today."""
    y, q = int(frm[:4]), int(frm[5])
    today = dt.date.today()
    ey, eq = ((today.year, (today.month - 1) // 3 + 1) if to is None
              else (int(to[:4]), int(to[5])))
    while (y, q) <= (ey, eq):
        yield f"{y}Q{q}"
        q += 1
        if q == 5:
            y, q = y + 1, 1


def index_url(qtr):
    """full-index 路径：2026-10 实测旧 {YYYY}Q{N}/ 与 2024+/quarterly-index/ 均 403，
    官方 sitemap 现行为 {YYYY}/QTR{N}/master.gz 覆盖 1993→今（内容为当季增量流水）。"""
    return f"https://www.sec.gov/Archives/edgar/full-index/{qtr[:4]}/QTR{qtr[5]}/master.gz"


def cmd_index(args):
    """扫全量 master.gz → 每个 CIK 的首次注册/末次年报/摘牌表。断点：out/.done_quarters"""
    done_path = os.path.join(OUT, ".done_quarters")
    done = set(open(done_path).read().split()) if os.path.exists(done_path) else set()
    agg_path = os.path.join(OUT, "cik_events.json")
    if os.path.exists(agg_path):
        agg = json.load(open(agg_path))
    else:
        agg = {}  # cik -> [name, first_s1, first_10k, last_10k, delist_date, seen_quarters_since_last_10k]

    failed = False
    for qtr in quarters(args.frm, args.to):
        if qtr in done:
            continue
        try:
            raw = fetch(index_url(qtr), timeout=300)
        except Exception as e:
            print(f"[index] {qtr} download failed; check SEC access or SEC_PROXY: {e}")
            failed = True
            break
        text = raw.decode("latin-1")
        lines = text.split("\n")
        start, sep = 10, "|"  # 兜底默认（master.gz 实测竖线分隔）
        for i, ln in enumerate(lines[:50]):
            s = ln.strip().upper().replace(" ", "")
            if s.startswith("CIK,") or s.startswith("CIK|"):
                sep = "," if s.startswith("CIK,") else "|"
                start = i + 2  # 跳过 CIK 头后的分隔线
                break
        rows = lines[start:]
        n = 0
        for row in rows:
            if not row.strip():
                continue
            p = row.split(sep)
            if len(p) < 5:
                continue
            cik = p[0].strip().lstrip("0") or "0"
            name, form, fdate = p[1].strip(), p[2].strip(), p[3].strip()[:10]
            is_listed = form in LISTED_FORMS
            is_ipo = form in IPO_FORMS
            is_del = form in DELIST_LIKE
            if not (is_listed or is_ipo or is_del):
                continue  # 个人 Form 4 等无关行不进 agg（省内存：全历史 CIK 数百万）
            a = agg.get(cik)
            if a is None:
                a = [name, "", "", "", "", 0]
                agg[cik] = a
            if is_ipo and not a[1]:
                a[1] = fdate
            if is_listed:
                if not a[2] or fdate < a[2]:
                    a[2] = fdate
                if fdate > a[3]:
                    a[3] = fdate
                a[5] = 0
            elif is_del:
                if not a[4] or fdate < a[4]:
                    a[4] = fdate
            n += 1
        # 该季度内没出现年报类申报的公司，静默计数 +1（用于“不再申报=已退市”近似）
        for a in agg.values():
            if a[3]:
                a[5] += 1
        # Persist aggregation before recording the quarter as done. A crash
        # between checkpoints must never skip a quarter on the next run.
        with open(agg_path + ".tmp", "w") as f:
            json.dump(agg, f)
        os.replace(agg_path + ".tmp", agg_path)
        done.add(qtr)
        with open(done_path + ".tmp", "w") as f:
            f.write(" ".join(sorted(done)))
        os.replace(done_path + ".tmp", done_path)
        print(f"[index] {qtr}: rows={n:,} cum_cik={len(agg):,}")
    json.dump(agg, open(agg_path, "w"))

    with open(os.path.join(OUT, "delist_universe.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "name", "first_s1", "first_10k", "last_10k",
                    "delist_form_date", "quiet_quarters_after_last_10k",
                    "is_delisted", "ipo_year_known"])
        for cik, a in sorted(agg.items(), key=lambda kv: kv[1][3] or "9999"):
            name, s1, f10, l10, dfm, quiet = a
            if not l10:
                continue
            delisted = quiet >= args.quiet  # 仅年报静默判定；dfm 是证券级摘表噪声不可信
            w.writerow([cik, name, s1, f10, l10, dfm, quiet,
                        1 if delisted else 0, 0 if (f10 and f10[:4] <= "1995") else 1])
    print(f"[index] -> out/delist_universe.csv 共 {len(agg):,} CIK")
    if failed:
        raise SystemExit(1)


def cmd_events(args):
    """FTS 拉 25-NSE / 25 摘牌事件（只作快速验证与增量补充，文本项会漏少量）。"""
    terms = ["delisted", "listed", "shares"]
    seen = {}
    failed = False
    y0, m0 = map(int, args.start.split("-"))
    y1, m1 = map(int, args.end.split("-"))
    for form in ("25-NSE", "25"):
        y, m = y0, m0
        while (y, m) <= (y1, m1):
            s = f"{y:04d}-{m:02d}-01"
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            e = f"{ny:04d}-{nm:02d}-01"
            base = f"https://efts.sec.gov/LATEST/search-index?q=%22{terms[0]}%22&forms={form}&dateRange=custom&startdt={s}&enddt={e}"
            for term in terms:
                url0 = base.replace("%22" + terms[0] + "%22", "%22" + term + "%22")
                frm, total = 0, 0
                while True:
                    try:
                        d = json.loads(fetch(url0 + f"&size=10&from={frm}", timeout=30))
                    except Exception as ex:
                        print(f"[events] {form} {s} '{term}' from={frm} 失败: {ex}")
                        failed = True
                        break
                    total = d["hits"]["total"]["value"]
                    hits = d["hits"]["hits"]
                    if not hits:
                        break
                    for h in hits:
                        src = h["_source"]
                        acc = h["_id"].split(":")[0]
                        seen[acc] = {
                            "accession": acc, "ciks": "|".join(src.get("ciks", [])),
                            "display_names": "|".join(src.get("display_names", []))[:120],
                            "form": form, "file_date": src.get("file_date", ""),
                            "period": src.get("period", ""),
                        }
                    frm += 10
                    time.sleep(SLEEP)
                    if frm >= total or frm >= 9990:
                        break
                if total >= 9990:
                    print(f"[events] WARN {form} {s}: '{term}' 命中 {total} 触分页上限，该月不完整（index 模式兜底）")
            print(f"[events] {form} {s[:7]}: cum_uniq={len(seen):,}")
            time.sleep(SLEEP)
            y, m = ny, nm
    path = os.path.join(OUT, "delist_events_fts.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(next(iter(seen.values())).keys()) if seen else
                           ["accession", "ciks", "display_names", "form", "file_date", "period"])
        w.writeheader()
        w.writerows(sorted(seen.values(), key=lambda r: r["file_date"]))
    print(f"[events] -> {path}  {len(seen):,} 条")
    if failed:
        raise SystemExit(1)


def cmd_enrich(args):
    """补 CIK 历史：submissions（formerNames/tickers）+ companyfacts（XBRL 可得性，2009→今）"""
    import urllib.parse as up
    with open(args.inp) as f:
        rows = list(csv.DictReader(f))
    ciks = []
    for r in rows:
        for c in (r.get("cik") or r.get("ciks", "")).split("|"):
            c = c.strip().lstrip("0")
            if c and c not in ciks:
                ciks.append(c)
    ciks = ciks[:args.limit]
    out = os.path.join(OUT, "enriched.csv")
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "entity_name", "tickers_now", "former_ticker_ranges",
                    "xbrl_metrics", "xbrl_first_fy", "xbrl_last_fy"])
        for i, cik in enumerate(ciks):
            pad = cik.zfill(10)
            tick_now, former, sub = "", "", None
            try:
                sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{pad}.json", timeout=60))
                tick_now = "|".join(sub.get("tickers", []))
                former = "|".join(
                    f"{fn.get('ticker','?')}:{fn.get('from','')[:10]}-{fn.get('to','')[:10]}"
                    for fn in sub.get("formerNames", []))
            except Exception as e:
                former = f"SUB_ERR:{e}"
            n_m, fy0, fy1 = 0, "", ""
            try:
                cf = json.loads(fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{pad}.json", timeout=180))
                g = cf.get("facts", {}).get("us-gaap", {})
                n_m = len(g)
                fys = []
                for t in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                          "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "NetIncomeLoss", "Assets"):
                    units = g.get(t, {}).get("units", {})
                    for obs_list in units.values():
                        fys += [x.get("fy") for x in obs_list if x.get("form") == "10-K" and x.get("fy")]
                if fys:
                    fy0, fy1 = min(fys), max(fys)
            except Exception:
                pass  # 退市早/小公司无 XBRL 属正常
            w.writerow([cik, (sub or {}).get("name", ""), tick_now, former, n_m, fy0, fy1])
            if (i + 1) % 20 == 0:
                print(f"[enrich] {i+1}/{len(ciks)} -> {out}")
            time.sleep(SLEEP)
    print(f"[enrich] -> {out}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("index")
    p1.add_argument("--frm", default="1993Q3")
    p1.add_argument("--to", default=None)
    p1.add_argument("--quiet", type=int, default=8, help="末次年报后连续 N 个季度无申报→判退市（默认8=2年）")
    p1.add_argument("--save-every", type=int, default=4, help="每 N 个季度落盘一次断点")
    p1.set_defaults(fn=cmd_index)
    p2 = sub.add_parser("events")
    p2.add_argument("--start", required=True)
    p2.add_argument("--end", required=True)
    p2.set_defaults(fn=cmd_events)
    p3 = sub.add_parser("enrich")
    p3.add_argument("--in", dest="inp", required=True)
    p3.add_argument("--limit", type=int, default=500)
    p3.set_defaults(fn=cmd_enrich)
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    args.fn(args)


if __name__ == "__main__":
    main()
