#!/usr/bin/env python3
"""exec_xbrl_symbol.py — 任务E兜底：对无 ticker 的退市 CIK，取末次 10-K 的 XBRL 封面补票
读 out/e_gap_ciks.txt（每行一个 cik）→ 重新拉 submissions 定位最近 form=10-K 的 accession
→ FilingSummary.xml 找含 dei 的 R 文件 → 提取 Trading Symbol。
输出 out/tickers_xbrl_symbol.csv: cik,accession,filing_date,trading_symbol,status
断点续跑：out/.xbrl_symbol_done.txt。SEC_SLEEP≥0.15，workers=3。
"""
import csv
import gzip
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")
UA = os.environ.get("SEC_EDGAR_UA") or os.environ.get("SEC_UA", "")
SLEEP = max(0.15, float(os.environ.get("SEC_SLEEP", "0.15")))
LOCK = threading.Lock()


def fetch(url, tries=3, timeout=60, max_bytes=0):
    for a in range(tries):
        try:
            hdr = {"User-Agent": UA}
            if not max_bytes:
                hdr["Accept-Encoding"] = "gzip"
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(max_bytes) if max_bytes else r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return 200, data
        except urllib.error.HTTPError as e:
            if e.code in (404, 442, 451):
                return e.code, None
            time.sleep(20 if e.code == 429 else 3 * (a + 1))
        except Exception:
            time.sleep(3 * (a + 1))
    return 0, None


def find_cover_r(html):
    # dei 封面：FilingSummary 的 Report 块里 RoleRef/roleType 指向 xbrl.sec.gov/dei
    # 或 LongName 含 Document and Entity / Cover 的，取第一个
    for m in re.finditer(r"<Report\b[^>]*>(.*?)</Report>", html, re.S | re.I):
        blk = m.group(1)
        low = blk.lower()
        if ("xbrl.sec.gov/dei" in low or "cover" in low
                or "document and entity" in low):
            hm = re.search(r"<HtmlFileName>\s*(R\d+\.htm)", blk, re.I)
            if hm:
                return hm.group(1)
    return None


def extract_symbol(htm):
    txt = re.sub(r"<[^>]+>", "\n", htm)
    toks = [t.strip() for t in txt.split("\n") if t.strip()]
    for i, t in enumerate(toks):
        tl = t.lower().replace("&nbsp;", "").strip()
        if tl.startswith("trading symbol") or tl in ("trading symbol 1", "trading symbols"):
            for j in range(i + 1, min(i + 4, len(toks))):
                cand = toks[j].replace("&nbsp;", "").strip()
                if cand and not cand.lower().startswith("trading") and len(cand) <= 12 \
                        and cand.lower().strip(".()?") not in ("none", "n/a", "na", "not applicable", "-"):
                    return cand
            return ""
    return ""


def worker(q, fout, fdone, stats):
    while True:
        try:
            cik = q.get_nowait()
        except queue.Empty:
            return
        acc, fdate, sym, status = "", "", "", ""
        st, data = fetch(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json")
        if st == 200 and data:
            try:
                sub = json.loads(data.decode("utf-8", "replace"))
                recent = sub.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                accs = recent.get("accessionNumber", [])
                dates = recent.get("filingDate", [])
                forms2 = recent.get("primaryDocument", [])
                pick = None
                want = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
                        "N-CSR", "N-CSR/A", "N-CSRS", "10-KSB", "10-KSB-A")
                for i, fm in enumerate(forms):
                    if fm in want and i < len(accs):
                        pick = i
                        break  # recent 按日期倒序，第一个即末次
                if pick is None:
                    status = "NO_COVER_FORM"
                else:
                    acc, fdate = accs[pick], dates[pick]
                    an = acc.replace("-", "")
                    basep = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{an}"
                    st2, fs = fetch(basep + "/FilingSummary.xml", timeout=90)
                    if st2 == 200 and fs:
                        rfile = find_cover_r(fs.decode("utf-8", "replace"))
                        if rfile:
                            st3, htm = fetch(f"{basep}/{rfile}", timeout=90)
                            if st3 == 200 and htm:
                                sym = extract_symbol(htm.decode("utf-8", "replace"))
                                status = "OK" if sym else "EMPTY_SYMBOL"
                            else:
                                status = f"R_{st3}"
                        else:
                            status = "NO_DEI_R"
                    else:
                        status = f"FS_{st2}"
                    if not sym and pick < len(forms2) and forms2[pick]:
                        st4, pd = fetch(f"{basep}/{forms2[pick]}", max_bytes=600000)  # 2009前封面正文兜底
                        if st4 == 200 and pd:
                            sym = extract_symbol(pd.decode("utf-8", "replace"))
                            if sym:
                                status = "OK_HDR"
            except Exception as e:
                status = f"ERR:{type(e).__name__}"
        else:
            status = f"SUB_{st}"
        with LOCK:
            csv.writer(fout).writerow([cik, acc, fdate, sym, status])
            fout.flush()
            fdone.write(cik + "\n")
            fdone.flush()
            stats[0] += 1
            if stats[0] % 50 == 0:
                print(f"[xbrl] {stats[0]} done -> {status}", flush=True)
        time.sleep(SLEEP)


def main():
    os.makedirs(OUT, exist_ok=True)
    inp = os.path.join(OUT, "e_gap_ciks.txt")
    ciks = [ln.strip() for ln in open(inp) if ln.strip().isdigit()]
    done_path = os.path.join(OUT, ".xbrl_symbol_done.txt")
    done = set(open(done_path).read().split()) if os.path.exists(done_path) else set()
    todo = [c for c in ciks if c not in done]
    print(f"[xbrl] gap={len(ciks)} 已完成={len(done)} 待跑={len(todo)}", flush=True)
    out_path = os.path.join(OUT, "tickers_xbrl_symbol.csv")
    new = not os.path.exists(out_path) or os.path.getsize(out_path) == 0
    fout = open(out_path, "a")
    fdone = open(done_path, "a")
    if new:
        fout.write("cik,accession,filing_date,trading_symbol,status\n")
        fout.flush()
    q = queue.Queue()
    for c in todo:
        q.put(c)
    stats = [0]
    ths = [threading.Thread(target=worker, args=(q, fout, fdone, stats)) for _ in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    print(f"[xbrl] 完成 {stats[0]} -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
