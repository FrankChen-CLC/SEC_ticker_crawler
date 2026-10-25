#!/usr/bin/env python3
"""nasdaqdir_fetch.py — 从 Wayback 拉 Nasdaq Trader SymDir 月度符号目录快照
来源:
  nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
  nasdaqtrader.com/dynamic/SymDir/otherlisted.txt
输出: nasdaqdir/raw/<name>/<timestamp>.txt  (原始字节, 不做任何解析)
CDX 列表: nasdaqdir/cdx_<name>.json
日志: out/nasdaqdir.log
断点: 已存在且非空的快照直接跳过。串行 1 req/s, retry 3。
下载物不可信: 本脚本只写字节, 绝不执行/导入下载目录内任何内容。
用法: python3 -I scripts/nasdaqdir_fetch.py
"""
import json
import os
import time
import urllib.request
import urllib.error

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
DL = os.path.join(BASE, "nasdaqdir")
OUT = os.path.join(BASE, "out")
LOG = os.path.join(OUT, "nasdaqdir.log")
UA = {"User-Agent": os.environ.get("SEC_EDGAR_UA", "secdata historical ticker research")}
SLEEP = 1.0
NAMES = ["nasdaqlisted", "otherlisted"]
CDX_URL = ("http://web.archive.org/cdx/search/cdx?url=nasdaqtrader.com/dynamic/SymDir/{n}.txt"
           "&output=json&fl=timestamp,statuscode&filter=statuscode:200&collapse=timestamp:8")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def http_get(url, timeout=120, tries=3, backoff=3):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.status
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            time.sleep(backoff * (a + 1) if e.code != 429 else 20 * (a + 1))
        except Exception as e:
            last = repr(e)
            time.sleep(backoff * (a + 1))
    log(f"[fail] {url} :: {last}")
    return None, None


def main():
    os.makedirs(DL, exist_ok=True)
    log("=== nasdaqdir_fetch start ===")
    total_ok = total_fail = 0
    for name in NAMES:
        cdx_path = os.path.join(DL, f"cdx_{name}.json")
        if os.path.exists(cdx_path):
            rows = json.load(open(cdx_path))
        else:
            data, _ = http_get(CDX_URL.format(n=name), timeout=120, tries=4)
            if data is None:
                log(f"[cdx] {name} 彻底失败, 跳过")
                total_fail += 1
                continue
            rows = json.loads(data.decode("utf-8"))
            with open(cdx_path + ".tmp", "w") as f:
                json.dump(rows, f)
            os.replace(cdx_path + ".tmp", cdx_path)
        ts_list = [r[0] for r in rows[1:] if len(r) > 1 and r[1] == "200"]
        # 去重同一秒重复
        ts_list = sorted(set(ts_list))
        log(f"[cdx] {name}: {len(ts_list)} 个快照, 首 {ts_list[0] if ts_list else '-'} "
            f"末 {ts_list[-1] if ts_list else '-'}")
        d = os.path.join(DL, "raw", name)
        os.makedirs(d, exist_ok=True)
        ok = fail = skip = 0
        for i, ts in enumerate(ts_list):
            p = os.path.join(d, ts + ".txt")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                skip += 1
                ok += 1
                continue
            url = f"https://web.archive.org/web/{ts}id_/http://nasdaqtrader.com/dynamic/SymDir/{name}.txt"
            data, status = http_get(url)
            if data is None:
                fail += 1
                total_fail += 1
                continue
            with open(p + ".tmp", "wb") as f:
                f.write(data)
            os.replace(p + ".tmp", p)
            ok += 1
            total_ok += 1
            if (i + 1) % 10 == 0:
                log(f"[snap] {name}: {i+1}/{len(ts_list)} ok={ok} fail={fail} skip={skip} bytes_last={len(data)}")
            time.sleep(SLEEP)
        log(f"[snap] {name} 完成: ok={ok} fail={fail} skip={skip}")
    log(f"=== nasdaqdir_fetch done: downloaded={total_ok} failed={total_fail} ===")
    if total_fail:
        raise SystemExit(f"{total_fail} archive requests failed; rerun to retry")


if __name__ == "__main__":
    main()
