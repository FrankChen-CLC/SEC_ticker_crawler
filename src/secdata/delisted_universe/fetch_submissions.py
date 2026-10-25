#!/usr/bin/env python3
"""fetch_submissions.py — 对退市事件 unique CIK 逐个拉 EDGAR submissions
读 out/delist_events_fts.csv 的 ciks 列（pipe 分隔）→ unique CIK →
https://data.sec.gov/submissions/CIK{10位补零}.json
提取 entity_name / tickers 现值 / formerNames 全量 JSON（保留其 ticker 字段）。
输出 out/tickers_submissions.csv: cik,entity_name,tickers_now,former_names_json,status
断点续跑：out/.submissions_done.txt；404/442/451 记 status 后跳过。
用法: SEC_SLEEP=0.15 python3 -I fetch_submissions.py [--workers 3]
"""
import argparse
import csv
import gzip
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")
UA = os.environ.get("SEC_EDGAR_UA") or os.environ.get("SEC_UA", "")
SLEEP = max(0.15, float(os.environ.get("SEC_SLEEP", "0.15")))

LOCK = threading.Lock()
STATS = {"ok": 0, "err": 0, "retry": 0, "done": 0}


def fetch(url, tries=4):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return 200, data
        except urllib.error.HTTPError as e:
            if e.code in (404, 442, 451):
                return e.code, None
            time.sleep(30 + 30 * a if e.code == 429 else 3 * (a + 1))
        except Exception:
            time.sleep(3 * (a + 1))
    return 0, None


def worker(q, w, fout, fdone):
    while True:
        try:
            cik = q.get_nowait()
        except queue.Empty:
            return
        pad = cik.zfill(10)
        status, data = fetch(f"https://data.sec.gov/submissions/CIK{pad}.json")
        name = tick_now = former = ""
        if status == 200 and data:
            try:
                sub = json.loads(data.decode("utf-8", "replace"))
                name = (sub.get("name") or "").replace("\n", " ")
                tick_now = "|".join(sub.get("tickers") or [])
                former = json.dumps(sub.get("formerNames") or [], ensure_ascii=False, separators=(",", ":"))
            except Exception:
                status = "BADJSON"
        with LOCK:
            if status in (200, 404, 442, 451):
                csv.writer(fout).writerow([cik, name, tick_now, former, status])
                fout.flush()
                fdone.write(cik + "\n")
                fdone.flush()
            STATS["done"] += 1
            if status == 200:
                STATS["ok"] += 1
            else:
                STATS["err"] += 1
                if status not in (404, 442, 451):
                    STATS["retry"] += 1
            if STATS["done"] % 500 == 0:
                print(f"[submissions] {STATS['done']}/{q.qsize() + STATS['done']} ok={STATS['ok']} "
                      f"err={STATS['err']} t={time.strftime('%H:%M:%S')}", flush=True)
        time.sleep(SLEEP)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--in", dest="inp", default=os.path.join(OUT, "delist_events_fts.csv"))
    args = ap.parse_args()

    ciks = set()
    with open(args.inp) as f:
        for r in csv.DictReader(f):
            for c in (r.get("ciks") or "").split("|"):
                c = c.strip().lstrip("0")
                if c:
                    ciks.add(c)
    done_path = os.path.join(OUT, ".submissions_done.txt")
    out_path = os.path.join(OUT, "tickers_submissions.csv")
    done = set(open(done_path).read().split()) if os.path.exists(done_path) else set()
    todo = sorted(ciks - done, key=lambda c: (len(c), c))
    print(f"[submissions] unique CIK={len(ciks):,} 已完成={len(done):,} 待拉={len(todo):,} "
          f"workers={args.workers} sleep={SLEEP}", flush=True)
    if not todo:
        return

    q = queue.Queue()
    for c in todo:
        q.put(c)
    new = not os.path.exists(out_path) or os.path.getsize(out_path) == 0
    fout = open(out_path, "a")
    fdone = open(done_path, "a")
    if new:
        fout.write("cik,entity_name,tickers_now,former_names_json,status\n")
        fout.flush()
    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(q, i, fout, fdone)) for i in range(args.workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = time.time() - t0
    print(f"[submissions] 完成 {STATS['done']} 条 用={dt/60:.1f}分钟 ok={STATS['ok']} err={STATS['err']} -> {out_path}",
          flush=True)
    if STATS["retry"]:
        raise SystemExit(f"{STATS['retry']} submissions failed; rerun to retry")


if __name__ == "__main__":
    main()
