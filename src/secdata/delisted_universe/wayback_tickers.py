#!/usr/bin/env python3
"""wayback_tickers.py — Wayback 历史快照重建 CIK→ticker 有效期
覆盖 EDGAR company_tickers.json / company_tickers_exchange.json 的全部 200 状态快照。
CDX 相邻同 digest 去重后 >300 个（估 >3GB）则降级为每季度取首个快照（保留最新一个）。
输出 out/ticker_history_wayback.csv: cik,ticker,first_seen,last_seen（快照日期）
断点：out/.wayback_done.txt（fname ts）+ out/.wayback_hist.json 缓存（可随时中断重跑）。
archive.org 串行限速 1 req/s，429 退避。
"""
import csv
import gzip
import json
import os
import time
import urllib.request

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")
UA = {"User-Agent": os.environ.get("SEC_EDGAR_UA", "secdata historical ticker research")}
SLEEP = 1.0
FILES = ["company_tickers.json", "company_tickers_exchange.json"]
CDX_KEEP_MAX = 300


def http_get(url, timeout=180, tries=4, backoff=30):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            req.add_header("Accept-Encoding", "gzip")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    data = gzip.decompress(data)
                return data
        except Exception as e:
            code = getattr(e, "code", None)
            print(f"[get] {url[:90]} attempt{a} {e}", flush=True)
            time.sleep(backoff if code == 429 else 5 * (a + 1))
    return None


def cdx(fname):
    url = (f"https://web.archive.org/cdx/search/cdx?url=sec.gov/files/{fname}"
           f"&fl=timestamp,statuscode,digest&output=text&filter=statuscode:200")
    data = http_get(url, timeout=300, tries=6, backoff=60)
    if data is None:
        return None
    return [ln.split() for ln in data.decode().splitlines() if ln.strip()]


def sample(rows):
    uniq, last_dg = [], None
    for ts, sc, dg in rows:
        if dg == last_dg:
            continue
        uniq.append(ts)
        last_dg = dg
    if len(uniq) <= CDX_KEEP_MAX:
        return uniq, "full"
    kept, seen_q = [], set()
    for ts in uniq:
        key = (ts[:4], (int(ts[4:6]) - 1) // 3)
        if key not in seen_q:
            seen_q.add(key)
            kept.append(ts)
    if kept and kept[-1] != uniq[-1]:
        kept.append(uniq[-1])
    return kept, "quarterly"


def main():
    os.makedirs(OUT, exist_ok=True)
    failures = 0
    done_path = os.path.join(OUT, ".wayback_done.txt")
    hist_path = os.path.join(OUT, ".wayback_hist.json")
    hist = json.load(open(hist_path)) if os.path.exists(hist_path) else {}
    done = set(tuple(l.split()) for l in open(done_path).read().splitlines() if l.strip()) \
        if os.path.exists(done_path) else set()

    for fname in FILES:
        if ("cdx", fname) not in done:
            rows = cdx(fname)
            if rows is None:
                print(f"[cdx] {fname} 彻底失败，跳过", flush=True)
                failures += 1
                continue
            kept, mode = sample(rows)
            with open(os.path.join(OUT, f".wayback_cdx_{fname}.txt"), "w") as f:
                f.write(f"raw200={len(rows)} kept={len(kept)} mode={mode}\n")
                for ts in kept:
                    f.write(ts + "\n")
            print(f"[cdx] {fname}: statuscode200行={len(rows)} 去重后保留={len(kept)} 模式={mode} "
                  f"首个={kept[0] if kept else '-'} 末个={kept[-1] if kept else '-'}", flush=True)
            done.add(("cdx", fname))
            _save_state(done_path, hist_path, done, hist)

        cdx_file = os.path.join(OUT, f".wayback_cdx_{fname}.txt")
        if not os.path.exists(cdx_file):
            continue
        ts_list = [ln.strip() for ln in open(cdx_file) if ln.strip() and not ln.startswith("raw200")]
        n_ok = n_fail = n_nocik = 0
        for i, ts in enumerate(ts_list):
            if (fname, ts) in done:
                n_ok += 1
                continue
            data = http_get(f"https://web.archive.org/web/{ts}id_/https://www.sec.gov/files/{fname}", timeout=300)
            if data is None:
                n_fail += 1
                failures += 1
                print(f"[snap] {fname} {ts} 下载失败（已重试用尽）", flush=True)
            else:
                try:
                    obj = json.loads(data.decode("utf-8", "replace"))
                except Exception as e:
                    n_fail += 1
                    failures += 1
                    print(f"[snap] {fname} {ts} JSON解析失败: {e} head={data[:60]!r}", flush=True)
                    obj = None
                if obj is not None:
                    d = ts[:4] + "-" + ts[4:6] + "-" + ts[6:8]
                    for v in obj.values():
                        if not isinstance(v, dict):
                            continue
                        cik, tick = (v.get("cik_str") or v.get("cik")), v.get("ticker")
                        if cik in (None, 0, "", "?") or not tick:
                            n_nocik += 1
                            continue
                        key = f"{cik}|{tick}"
                        r = hist.get(key)
                        if r is None:
                            hist[key] = [d, d]
                        else:
                            if d < r[0]:
                                r[0] = d
                            if d > r[1]:
                                r[1] = d
                    n_ok += 1
            if data is not None and obj is not None:
                done.add((fname, ts))
            if (i + 1) % 5 == 0:
                _save_state(done_path, hist_path, done, hist)
                print(f"[snap] {fname}: {i+1}/{len(ts_list)} ok={n_ok} fail={n_fail} nocik={n_nocik} "
                      f"pairs={len(hist)}", flush=True)
            time.sleep(SLEEP)
        print(f"[snap] {fname} 完成: ok={n_ok} fail={n_fail} 无cik条目={n_nocik}", flush=True)
        _save_state(done_path, hist_path, done, hist)

    path = os.path.join(OUT, "ticker_history_wayback.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "ticker", "first_seen", "last_seen"])
        for key in sorted(hist, key=lambda k: (int(k.split("|")[0]), k.split("|")[1])):
            cik, tick = key.split("|", 1)
            w.writerow([cik, tick, hist[key][0], hist[key][1]])
    print(f"-> {path} 行数={len(hist)}", flush=True)
    if failures:
        raise SystemExit(f"{failures} archived snapshots failed; rerun to retry")


def _save_state(done_path, hist_path, done, hist):
    with open(done_path + ".tmp", "w") as f:
        f.write("\n".join(" ".join(x) for x in sorted(done)))
    os.replace(done_path + ".tmp", done_path)
    with open(hist_path + ".tmp", "w") as f:
        json.dump(hist, f)
    os.replace(hist_path + ".tmp", hist_path)


if __name__ == "__main__":
    main()
