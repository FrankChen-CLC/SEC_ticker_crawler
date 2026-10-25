#!/usr/bin/env python3
"""nasdaqdir_parse.py — 解析 nasdaqdir/raw 下的 Nasdaq Trader SymDir 快照
输入: nasdaqdir/raw/{nasdaqlisted,otherlisted}/<timestamp>.txt (不可信下载物, 只读字节)
输出:
  nasdaqdir/parsed_rows.csv  每快照每票一行
  nasdaqdir/spells.csv       (symbol, clean_name) 聚合的有效期
日志: out/nasdaqdir.log
用法: python3 -I scripts/nasdaqdir_parse.py
"""
import csv
import gzip
import os
import re
import time
from collections import Counter, defaultdict

BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
DL = os.path.join(BASE, "nasdaqdir")
OUT = os.path.join(BASE, "out")
LOG = os.path.join(OUT, "nasdaqdir.log")
NAMES = ["nasdaqlisted", "otherlisted"]
EX_MAP = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca",
          "Z": "Cboe", "V": "IEX"}

# 证券后缀 (从末尾反复剥离)
SEC_SUFFIX = {
    "COMMON", "STOCK", "SHARES", "SHARE", "SHS", "ORDINARY", "ORD", "COM",
    "CL", "CLASS", "SERIES", "PREFERRED", "PFD",
    "PREF", "NEW", "ETF", "WARRANT", "WARRANTS", "WT", "RIGHT", "RIGHTS",
    "UNIT", "UNITS", "NOTE", "NOTES", "BOND", "BONDS", "DEBENTURE",
    "SUBORDINATED", "DEPOSITARY", "DEPOSITORY",
}
# 公司后缀 (位于证券后缀之后, 循环剥)
CORP_SUFFIX = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COMPANIES",
    "LLC", "LTD", "LIMITED", "LP", "PLC", "NV", "SA", "AG", "HOLDINGS",
    "HOLDING", "GROUP", "BV", "AB", "OYJ", "SE", "DEL",
}
# 法人后缀出现在名称中间也要去掉 ("Dynasil Corporation of America" vs "Dynasil Corp of America")
ANYWHERE_SUFFIX = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "COMPANY", "COMPANIES",
    "LLC", "LTD", "LIMITED", "PLC", "HOLDINGS", "HOLDING", "GROUP",
}
STATE2 = {"DE", "MD", "PA", "NV", "CA", "NY", "TX", "FL", "VA", "WA", "MA",
          "MN", "OH", "NC", "GA", "IL", "NJ", "WI", "MO", "CT", "MI", "IN",
          "TN", "AZ", "CO", "KS", "KY", "LA", "ME", "MS", "NE", "OK", "OR",
          "SC", "UT", "WY", "DC", "SD", "ND", "AL", "AR", "IA", "ID", "MT",
          "NH", "NM", "RI", "VT", "WV"}
PREF_RE = re.compile(
    r"\b(PREFERRED|PFD|PREF|PF|DEPOSITARY PREF|DEPOSITORY PREF)\b|\bSER(?:IES)?\s+[A-Z]{1,3}\b")
# 2008-2010 NYSE 目录用缩写名 (AGILENT TECH / AMER INTL GROUP / WACHOVIA CP)
ABBREV = {
    "TECH": "TECHNOLOGY", "TECHS": "TECHNOLOGIES", "HLDGS": "HOLDINGS",
    "HLDG": "HOLDING", "CP": "CORP", "FINL": "FINANCIAL",
    "INTL": "INTERNATIONAL", "AMER": "AMERICAN", "NATL": "NATIONAL",
    "INDS": "INDUSTRIES", "SVC": "SERVICES", "SVCS": "SERVICES",
    "SYS": "SYSTEMS", "MTG": "MORTGAGE", "MFG": "MANUFACTURING",
    "GENL": "GENERAL", "GEN": "GENERAL", "PETE": "PETROLEUM",
    "PETRO": "PETROLEUM", "PHARM": "PHARMACEUTICAL", "PWR": "POWER",
    "ELEC": "ELECTRIC", "PRODS": "PRODUCTS", "PROD": "PRODUCTS",
    "BANC": "BANCORP", "CMN": "COMMON", "STK": "STOCK",
    "SER": "SERIES", "MTL": "METALS", "RES": "RESOURCES",
    "COMM": "COMMUNICATIONS", "INS": "INSURANCE",
    "BROS": "BROTHERS", "COS": "COMPANIES", "LABS": "LABORATORIES",
}
OTHER_RE = re.compile(r"\b(WARRANTS?|RIGHTS?|UNITS?|NOTES?|BONDS?|DEBENTURES?|SUBORDINATED)\b")
ETF_RE = re.compile(r"\b(ETF|ETFS|EXCHANGE[ -]TRADED)\b")
# 名称里不含 "ETF" 字样的基金族 (仅用于统计口径, 不影响匹配)
ETF_FAMILY_RE = re.compile(
    r"\b(ISHARES|POWERSHARES|PROSHARES|SPDR|WISDOMTREE|VANGUARD|"
    r"FIRST TRUST|GUGGENHEIM|DIREXION|VANECK|GLOBAL X)\b")
COMMON_UNIT_RE = re.compile(
    r"\b(COMMON UNITS?|LIMITED PARTNER|BENEFICIAL UNIT|PARTNERSHIP INTERESTS?)\b")
ADS_CUT = re.compile(
    r"\s+(?:AMERICAN\s+DEPOSIT[OA]RY\s+(?:SHARES?|RECEIPTS?)|"
    r"DEPOSIT[OA]RY\s+(?:SHARES?|RECEIPTS?)|ADS)\b", re.I)
STATE_CUT = re.compile(r"/\s*(" + "|".join(sorted(STATE2)) + r")\s*$", re.I)


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def classify(symbol, raw_name):
    u = raw_name.upper()
    if "$" in symbol or PREF_RE.search(u):
        return "preferred"
    if COMMON_UNIT_RE.search(u) and not re.search(r"\b(WARRANTS?|RIGHTS?)\b", u):
        return "common"
    if OTHER_RE.search(u):
        return "other"
    return "common"


def clean_name(raw):
    s = raw.strip()
    # Nasdaq 风格 "Company - Common Stock" 直接取前段
    s = re.split(r"\s+-\s+", s, maxsplit=1)[0]
    # ADS/ADR 描述从句截断 ("American Depositary Shares, each representing ...")
    s = ADS_CUT.split(" " + s, maxsplit=1)[0].strip()
    s = re.split(r",?\s+each\s+representing\b", s, maxsplit=1, flags=re.I)[0]
    return re.sub(r"\s+", " ", s).strip()


def norm_name(raw):
    s = clean_name(raw).upper()
    s = STATE_CUT.sub("", s)
    s = s.replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    toks = s.split()
    # 合并连续单字母 ("I D SYSTEMS" -> "ID SYSTEMS", "A H BELO" -> "AH BELO")
    merged, run = [], []
    for t in toks:
        if len(t) == 1 and t.isalpha():
            run.append(t)
        else:
            if run:
                merged.append("".join(run) if len(run) > 1 else run[0])
                run = []
            merged.append(t)
    if run:
        merged.append("".join(run) if len(run) > 1 else run[0])
    toks = [ABBREV.get(t, t) for t in merged]
    # 剥离中间法人后缀
    toks = [t for t in toks if t not in ANYWHERE_SUFFIX]
    # 反复剥离尾部证券后缀（含两词组合）
    changed = True
    while toks and changed:
        changed = False
        if toks[-1] in SEC_SUFFIX:
            toks.pop()
            changed = True
            continue
        if len(toks) >= 2 and toks[-1] in ("STOCK", "SHARES", "SHARE", "WARRANT",
                                           "WARRANTS", "RIGHT", "RIGHTS", "UNIT",
                                           "UNITS", "NOTE", "NOTES", "BOND", "BONDS"):
            toks.pop()
            changed = True
            continue
        if len(toks) >= 2 and toks[-2] in ("CLASS", "CL", "SERIES") \
                and len(toks[-1]) <= 2 and toks[-1].isalnum():
            del toks[-2:]
            changed = True
    # 拆散的法人后缀: P L C / L L C / L P / N V / S A
    while toks:
        if len(toks) >= 3 and tuple(toks[-3:]) in (("P", "L", "C"), ("L", "L", "C")):
            del toks[-3:]
        elif len(toks) >= 2 and tuple(toks[-2:]) in (("L", "P"), ("N", "V"), ("S", "A")):
            del toks[-2:]
        elif toks[-1] in CORP_SUFFIX or (len(toks[-1]) == 2 and toks[-1] in STATE2):
            toks.pop()
        else:
            break
    if toks and toks[0] == "THE":
        toks.pop(0)
    return " ".join(toks)


def parse_file(path, name):
    raw = open(path, "rb").read()
    if raw[:2] == b"\x1f\x8b":  # Wayback 对部分快照存的是 gzip 原文
        try:
            raw = gzip.decompress(raw)
        except Exception as e:
            return None, f"gzip_bad:{e}"
    if not raw or b"\x00" in raw[:2000]:
        return None, "empty_or_binary"
    txt = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in txt.split("\n") if ln.strip()]
    if not lines or "|" not in lines[0] or "Symbol" not in lines[0]:
        return None, "not_piped_header"
    header = [h.strip().lower() for h in lines[0].split("|")]
    def idx(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None
    i_sym = idx("symbol", "act symbol")
    i_name = idx("security name")
    if i_sym is None or i_name is None:
        return None, "header_missing_symbol_or_name"
    i_test = idx("test issue")
    i_exch = idx("exchange")
    i_etf = idx("etf")
    i_mkt = idx("market category")
    rows, bad = [], 0
    for ln in lines[1:]:
        if ln.startswith("File Creation Time"):
            continue
        parts = ln.split("|")
        if len(parts) <= max(i_sym, i_name):
            bad += 1
            continue
        sym = parts[i_sym].strip()
        nm = parts[i_name].strip()
        if not sym or not nm:
            bad += 1
            continue
        if i_test is not None and len(parts) > i_test and parts[i_test].strip().upper() == "Y":
            continue  # 测试票
        etf = "Y" if (i_etf is not None and len(parts) > i_etf
                      and parts[i_etf].strip().upper() == "Y") else ""
        if not etf and (ETF_RE.search(nm.upper()) or ETF_FAMILY_RE.search(nm.upper())):
            etf = "Y"
        if name == "nasdaqlisted":
            exch = "NASDAQ"
        else:
            code = parts[i_exch].strip().upper() if i_exch is not None and len(parts) > i_exch else ""
            exch = EX_MAP.get(code, code or "UNKNOWN")
        rows.append({"symbol": sym, "raw_name": nm, "exchange": exch,
                     "etf": etf, "security_type": classify(sym, nm),
                     "market": parts[i_mkt].strip() if i_mkt is not None and len(parts) > i_mkt else ""})
    return rows, f"ok bad={bad}"


def main():
    log("=== nasdaqdir_parse start ===")
    parsed = []
    stats = Counter()
    fails = []
    for name in NAMES:
        d = os.path.join(DL, "raw", name)
        if not os.path.isdir(d):
            log(f"[parse] 缺目录 {d}")
            continue
        files = sorted(f for f in os.listdir(d) if f.endswith(".txt"))
        log(f"[parse] {name}: {len(files)} 个文件")
        for fn in files:
            ts = fn[:-4]
            if not re.fullmatch(r"\d{14}", ts):
                stats["bad_filename"] += 1
                continue
            rows, msg = parse_file(os.path.join(d, fn), name)
            if rows is None:
                stats["file_reject"] += 1
                fails.append((name, ts, msg))
                log(f"[parse] 拒收 {name}/{fn}: {msg}")
                continue
            stats["files_ok"] += 1
            stats["rows_raw"] += len(rows)
            date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            for r in rows:
                r["snapshot"] = date
                r["ts"] = ts
                r["source"] = name
                r["clean_name"] = clean_name(r["raw_name"])
                r["norm_name"] = norm_name(r["raw_name"])
            parsed.extend(rows)
    log(f"[parse] 文件ok={stats['files_ok']} 拒收={stats['file_reject']} 原始行={stats['rows_raw']}")

    # 写 parsed_rows
    pp = os.path.join(DL, "parsed_rows.csv")
    with open(pp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["snapshot_date", "snapshot_ts", "symbol", "raw_name", "clean_name",
                    "norm_name", "exchange", "security_type", "etf_flag", "source"])
        for r in parsed:
            w.writerow([r["snapshot"], r["ts"], r["symbol"], r["raw_name"], r["clean_name"],
                        r["norm_name"], r["exchange"], r["security_type"], r["etf"], r["source"]])
    log(f"[parse] -> {pp} rows={len(parsed)}")

    # 聚合 spell: (symbol, norm_name) -> 第一次/最后一次出现
    spells = {}
    etf_rows = [r for r in parsed if r["etf"] == "Y"]
    for r in parsed:
        if r["etf"] == "Y":
            continue
        key = (r["symbol"], r["norm_name"])
        sp = spells.get(key)
        if sp is None:
            spells[key] = {"symbol": r["symbol"], "norm_name": r["norm_name"],
                           "clean_name": r["clean_name"], "raw_name": r["raw_name"],
                           "exchange": r["exchange"], "security_type": r["security_type"],
                           "first": r["ts"], "last": r["ts"], "n": 1,
                           "exch": Counter({r["exchange"]: 1}),
                           "dates": {r["snapshot"]}}
        else:
            sp["n"] += 1
            sp["first"] = min(sp["first"], r["ts"])
            sp["last"] = max(sp["last"], r["ts"])
            sp["exch"][r["exchange"]] += 1
            sp["dates"].add(r["snapshot"])
            if len(r["raw_name"]) > len(sp["raw_name"]):
                sp["raw_name"] = r["raw_name"]
    sp_path = os.path.join(DL, "spells.csv")
    with open(sp_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "norm_name", "clean_name", "evidence_name", "exchange",
                    "security_type", "first_seen", "last_seen", "n_snapshots", "sources",
                    "snapshot_dates"])
        for sp in sorted(spells.values(), key=lambda x: (x["symbol"], x["first"])):
            w.writerow([sp["symbol"], sp["norm_name"], sp["clean_name"], sp["raw_name"],
                        sp["exch"].most_common(1)[0][0],
                        sp["security_type"], sp["first"][:4] + "-" + sp["first"][4:6] + "-" + sp["first"][6:8],
                        sp["last"][:4] + "-" + sp["last"][4:6] + "-" + sp["last"][6:8],
                        sp["n"], sp["first"], " ".join(sorted(sp["dates"]))])
    log(f"[parse] -> {sp_path} spells={len(spells)} etf_rows_dropped={len(etf_rows)}")

    # 统计 (ETF 行保留在 parsed_rows 供审计, 但票号口径剔除)
    ne = [r for r in parsed if r["etf"] != "Y"]
    syms = {r["symbol"] for r in ne}
    common = {r["symbol"] for r in ne if r["security_type"] == "common"}
    log(f"[stats] 总行={len(parsed)} 去ETF后={len(ne)} 唯一symbol(去ETF)={len(syms)} "
        f"common唯一symbol={len(common)} spells={len(spells)}")
    log(f"[stats] security_type: " + str(Counter(r["security_type"] for r in parsed if r["etf"] != "Y")))
    log(f"[stats] exchange: " + str(Counter(r["exchange"] for r in parsed if r["etf"] != "Y")))
    if fails:
        log(f"[stats] 拒收文件 {len(fails)}: {fails[:10]}")


if __name__ == "__main__":
    main()
