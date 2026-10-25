#!/usr/bin/env python3
"""workflow.py — 退市股去生存者偏差数据库的单一入口（分阶段、可断点、跨机）

设计原则
  1. 每个阶段的输入输出都是 out/ 里的普通文件，没有隐藏状态；除 out/.state/*.json 记录完成时间。
  2. 网络获取（A 类）与本地推导（B 类）彻底分开：A 类慢且会失败，B 类纯离线秒级重算。
  3. 下载所需的大型 SEC bulk archives 放在数据目录，不写进安装包。
  4. 一律 python3 -I（-I 隔离 sys.path，避免数据目录里的同名模块被 import 走）。

用法
  python3 -I scripts/workflow.py list                  # 阶段清单
  python3 -I scripts/workflow.py status                # 完成情况（读 out/.state）
  python3 -I scripts/workflow.py run                   # 跑所有未完成阶段
  python3 -I scripts/workflow.py run --from B1         # 从某阶段起
  python3 -I scripts/workflow.py run --only B7,B8      # 只跑指定阶段
  python3 -I scripts/workflow.py run --force           # 全部强制重跑
  python3 -I scripts/workflow.py run --stale           # 只重跑「依赖比它新」的阶段
  python3 -I scripts/workflow.py run --dry-run         # 只打印命令
  python3 -I scripts/workflow.py check                 # 校验关键产物行数与列数
"""
import argparse
import csv
import datetime as dt
import json
import os
import shlex
import subprocess
import sys

SOURCE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.abspath(os.environ.get("SEC_DELISTED_ROOT", os.getcwd()))
OUT = os.path.join(BASE, "out")
STATE = os.path.join(OUT, ".state")
LOG = os.path.join(OUT, "workflow.log")
PYI = [sys.executable, "-I"]


class Stage:
    def __init__(self, sid, title, host, cmd, outs, deps=(), note="", opts=""):
        self.sid = sid
        self.title = title
        self.host = host
        # cmd 可以是单条命令（list[str]）或多条顺序执行（list[list[str]]）
        self.cmds = cmd if cmd and isinstance(cmd[0], list) else [cmd]
        self.outs = outs          # 相对 BASE 的路径
        self.deps = list(deps)
        self.note = note
        self.opts = opts          # 可选阶段：默认跳过，需 --only 显式点名

    def expand(self, cmd):
        ym = dt.date.today().strftime("%Y-%m")
        ym_prev = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
        return [c.replace("{YYYY-MM}", ym_prev) for c in cmd]


STAGES = [
    # ---------- A 类：网络获取 ----------
    Stage("A1", "宇宙：EDGAR full-index 1993Q3→今", "any",
          PYI + ["poc_delisted.py", "index"],
          ["out/delist_universe.csv"],
          note="Full index may require a SEC_PROXY; resumes via out/.done_quarters",
          ),
    Stage("A2", "退市事件：EDGAR 全文搜索 forms=25/25-NSE", "any",
          PYI + ["poc_delisted.py", "events", "--start", "2001-01", "--end", "{YYYY-MM}"],
          ["out/delist_events_fts.csv"],
          note="Searches the monthly FTS windows sequentially"),
    Stage("A3", "票号历史：Wayback company_tickers 快照差分", "any",
          PYI + ["wayback_tickers.py"],
          ["out/ticker_history_wayback.csv"],
          note="唯一能给 2017 前退市票有效期的免费通道"),
    Stage("A4", "票号补漏：逐 CIK submissions API", "any",
          PYI + ["fetch_submissions.py", "--workers", "8", "--in", "out/delist_events_fts.csv"],
          ["out/tickers_submissions.csv"], deps=["A2"],
          note="8 线程、EDGAR 限 10 req/s，别调高"),
    Stage("A6", "财报：companyfacts 树 → 年度指标", "any",
          PYI + ["scripts/extract_facts.py", "facts", "out/fundamentals_fy.csv",
                 "out/facts_coverage.csv", "out/facts_failed.txt"],
          ["out/fundamentals_fy.csv", "out/facts_coverage.csv"], deps=["A1"],
          ),
    Stage("A7", "实体分类：submissions.zip → SIC/entityType", "any",
          PYI + ["scripts/fetch_sic_bulk.py", "subs/submissions.zip",
                 "out/delist_universe.csv", "out/sics.csv"],
          ["out/sics.csv"], deps=["A1"], note="随机读 zip 的 CIK 条目，不整包解压"),
    Stage("A8", "formerNames / 地址：submissions.zip 全量抽取", "any",
          PYI + ["scripts/extract_former_addr.py", "subs/submissions.zip",
                 "out/delist_universe.csv", "out"],
          ["out/former_names.csv", "out/addresses.csv"], deps=["A1"],
          ),
    Stage("A10", "Form 25 原因解析（NYSE narrative + Nasdaq 规则码）", "any",
          PYI + ["scripts/extract_form25_reasons.py", "out/delist_events_fts.csv", "out"],
          ["out/form25_reasons.csv"], deps=["A2"],
          ),
    # ---------- B 类：本地推导（纯离线） ----------
    Stage("B1", "主表 v1：宇宙 + 票号 + 财报覆盖", "any",
          PYI + ["scripts/merge_master.py"],
          ["out/master_universe.csv"], deps=["A1", "A3"],
          ),
    Stage("A9", "申报证据：Form 15 / 并购文件 / 8-K items", "any",
          PYI + ["scripts/extract_filings_bulk.py", "subs/submissions.zip",
                 "out/master_universe.csv", "out"],
          ["out/filings_last.csv", "out/filings_events.csv"], deps=["B1"],
          note="Uses master_universe.csv for the full CIK list"),
    Stage("B2", "实体净化：剔除 ABS/信托/基金等非公司载体", "any",
          PYI + ["scripts/apply_entity_filter.py"],
          ["out/delist_companies.csv", "out/entity_class.csv",
           "out/excluded_non_company.csv", "out/master_universe.csv"], deps=["B1", "A7"],
          ),
    Stage("A5", "票号兜底：末次 10-K 封面的 XBRL symbol", "any",
          PYI + ["exec_xbrl_symbol.py"],
          ["out/tickers_xbrl_symbol.csv"], deps=["B2"], opts="只补 e_gap_ciks.txt 里的空号 CIK"),
    Stage("A11", "交易所符号目录回填（2008–2017）", "any",
          [PYI + ["scripts/nasdaqdir_fetch.py"],
           PYI + ["scripts/nasdaqdir_parse.py"],
           PYI + ["scripts/nasdaqdir_match.py"]],
          ["nasdaqdir/spells.csv", "out/ticker_history_nasdaqdir.csv"], deps=["B2"],
          note="fetch→parse→match→verify 四步顺序执行；fetch 可增量（已抓的 CDX 跳过）"),
    Stage("B3", "承继主体识别（formerNames/同址）", "any",
          PYI + ["scripts/detect_merged_into.py", "out"],
          ["out/merged_into.csv"], deps=["B2", "A8"],
          ),
    Stage("B4", "票号总表：多通道合并 + 有效期", "any",
          PYI + ["scripts/build_tickers_merged.py"],
          ["out/tickers_all_merged.csv"], deps=["A3", "A4", "A5", "A11"],
          ),
    Stage("A12", "Validate Nasdaq directory coverage", "any",
          PYI + ["scripts/nasdaqdir_verify.py"],
          ["out/ticker_history_nasdaqdir.csv"], deps=["A11", "B4"], opts="optional diagnostics"),
    Stage("B5", "主表 v2：并入交易所证据与票号", "any",
          PYI + ["scripts/merge_master.py"],
          ["out/master_universe.csv"], deps=["B4", "A11"],
          ),
    Stage("B6", "实体净化（对 v2 重跑，勿省）", "any",
          PYI + ["scripts/apply_entity_filter.py"],
          ["out/delist_companies.csv", "out/entity_class.csv"], deps=["B5"],
          note="merge_master 会整表重写，实体列必须重新贴回"),
    Stage("B7", "并购/承继拆分（Form 25 原因 + 承继主体）", "any",
          PYI + ["scripts/apply_merger_split.py"],
          ["out/master_universe.csv", "out/delist_companies.csv"], deps=["B6", "B3", "A10"],
          ),
    Stage("B8", "退市性质四分 + Form 15 注销登记", "any",
          PYI + ["scripts/apply_filings_evidence.py"],
          ["out/master_universe.csv", "out/delist_companies.csv"], deps=["B7", "A9"],
          ),
]


def state_path(sid):
    return os.path.join(STATE, sid + ".json")


def load_state(sid):
    p = state_path(sid)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def save_state(st, md5s):
    os.makedirs(STATE, exist_ok=True)
    json.dump({"stage": st.sid, "ts": dt.datetime.now().isoformat(timespec="seconds"),
               "outs": {o: md5s.get(o, "") for o in st.outs}},
              open(state_path(st.sid), "w"), ensure_ascii=False, indent=1)


def md5_of(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def outs_present(st):
    return all(os.path.exists(os.path.join(BASE, o)) for o in st.outs)


def is_done(st):
    s = load_state(st.sid)
    return bool(s) and outs_present(st)


def seed_state():
    """产物已存在但没有状态记录时，按文件 mtime 补记，避免 run 误把历史产物全部重跑"""
    n = 0
    os.makedirs(STATE, exist_ok=True)
    for st in STAGES:
        if load_state(st.sid) or not outs_present(st):
            continue
        ts = max(dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(BASE, o))).isoformat(
            timespec="seconds") for o in st.outs)
        json.dump({"stage": st.sid, "ts": ts, "seeded": True,
                   "outs": {o: "" for o in st.outs}},
                  open(state_path(st.sid), "w"), ensure_ascii=False, indent=1)
        n += 1
    if n:
        print(f"[seed] 为 {n} 个已有产物补记完成时间（取文件 mtime）")


def is_stale(st):
    """依赖阶段完成时间晚于本阶段 → 需要重跑

    只比较本机真实跑出来的记录：seed 记录取的是文件 mtime，而 .39 与 Mac 有时差
    （CST vs IST，约 7 小时），跨机比较会造出一堆假的「旧」。
    """
    s = load_state(st.sid)
    if not s or s.get("seeded"):
        return False
    mine = s.get("ts", "")
    for d in st.deps:
        ds = load_state(d)
        if ds and not ds.get("seeded") and ds.get("ts", "") > mine:
            return True
    return False

def run_cmd(st, dry=False):
    def command(c):
        return [os.path.join(SOURCE, x) if x.endswith(".py") and
                os.path.isfile(os.path.join(SOURCE, x)) else x
                for x in st.expand(c)]

    if dry:
        for c in st.cmds:
            print("    " + " ".join(shlex.quote(x) for x in command(c)))
        return 0
    os.makedirs(OUT, exist_ok=True)
    t0 = dt.datetime.now()
    rc = 0
    with open(LOG, "a") as lg:
        lg.write(f"\n=== {t0:%Y-%m-%d %H:%M:%S} {st.sid} {st.title} ===\n")
        for cmd in st.cmds:
            cmd = command(cmd)
            lg.write("$ " + " ".join(cmd) + "\n")
            lg.flush()
            p = subprocess.Popen(cmd, cwd=BASE, env={**os.environ,
                                 "SEC_DELISTED_ROOT": BASE}, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p.stdout:
                sys.stdout.write("    " + line)
                lg.write(line)
            rc = p.wait()
            if rc != 0:
                break
        secs = (dt.datetime.now() - t0).total_seconds()
        lg.write(f"--- exit {rc} ({secs:.0f}s)\n")
    return rc


def cmd_list():
    print(f"{'ID':5s} {'主机':4s} {'阶段':38s} 输出")
    for st in STAGES:
        mark = "○" if st.opts else " "
        print(f"{st.sid:5s} {st.host:4s} {mark}{st.title:36.36s} {', '.join(st.outs)}")


def cmd_status():
    print(f"{'ID':5s} {'状态':8s} {'完成时间':20s} {'阶段':34s} 输出")
    for st in STAGES:
        s = load_state(st.sid)
        if not outs_present(st):
            stt = "缺失"
        elif s:
            stt = "✓" + ("(旧)" if is_stale(st) else "")
        else:
            stt = "无记录"
        ts = s.get("ts", "") if s else ""
        print(f"{st.sid:5s} {stt:8s} {ts:20s} {st.title:34.34s} {len(st.outs)} 个文件")
    tot = len([s for s in STAGES if is_done(s)])
    print(f"\n完成 {tot}/{len(STAGES)} 个阶段")


def cmd_run(args):
    only = set(x.strip() for x in args.only.split(",")) if args.only else None
    if not args.dry_run:
        seed_state()
    ran = skipped = failed = 0
    sids = [s.sid for s in STAGES]
    if args.frm and args.frm not in sids:
        sys.exit(f"未知阶段 {args.frm}；用 list 查看")
    start_i = sids.index(args.frm) if args.frm else 0
    for st in STAGES[start_i:]:
        if only and st.sid not in only:
            continue
        if st.opts and not only and not args.force:
            continue
        if not args.force and not only:
            done = is_done(st) or (args.dry_run and outs_present(st))
            if done and not (args.stale and is_stale(st)):
                skipped += 1
                continue
        print(f"\n[{st.sid}] {st.title}  ({st.host})")
        if st.note:
            print(f"    备注: {st.note}")
        rc = run_cmd(st, dry=args.dry_run)
        if rc != 0:
            failed += 1
            print(f"    ✗ 失败 exit={rc}（日志 out/workflow.log）")
            if not args.keep_going:
                print("    中止。修好后用 --from", st.sid, "续跑")
                return 1
            continue
        ran += 1
        if not args.dry_run:
            m = {o: md5_of(os.path.join(BASE, o)) for o in st.outs
                 if os.path.exists(os.path.join(BASE, o))}
            save_state(st, m)
            print(f"    ✓ 完成，记录 md5（{len(m)} 个文件）")
    print(f"\n汇总: 执行 {ran} / 跳过 {skipped} / 失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_check():
    """Validate shape and cross-file CIK invariants without frozen row counts."""
    checks = [
        ("out/delist_universe.csv", ("cik", "name", "is_delisted")),
        ("out/delist_companies.csv", ("cik", "entity_class", "delist_nature", "merger_ev_tier")),
        ("out/master_universe.csv", ("cik", "was_exchange_listed", "delist_nature")),
        ("out/filings_last.csv", ("cik", "deregistered", "form15_types")),
        ("out/tickers_all_merged.csv", ("cik", "ticker", "first_seen", "last_seen")),
    ]
    bad = 0
    ids = {}
    for rel, cols in checks:
        p = os.path.join(BASE, rel)
        if not os.path.exists(p):
            print(f"✗ {rel} 不存在"); bad += 1; continue
        with open(p, newline="") as f:
            reader = csv.DictReader(f)
            hdr = reader.fieldnames or []
            rows = list(reader)
        miss = [c for c in cols if c not in hdr]
        ciks = [r.get("cik", "") for r in rows]
        unique = rel == "out/tickers_all_merged.csv" or len(set(ciks)) == len(ciks)
        ok = bool(rows) and not miss and unique and all(ciks)
        ids[rel] = set(ciks)
        print(f"{'✓' if ok else '✗'} {rel}: {len(rows):,} rows, {len(hdr)} columns"
              + (f", 缺列 {miss}" if miss else ""))
        if not unique:
            print("  duplicate CIK values")
        bad += 0 if ok else 1
    universe = ids.get("out/delist_universe.csv")
    master = ids.get("out/master_universe.csv")
    filings = ids.get("out/filings_last.csv")
    companies = ids.get("out/delist_companies.csv")
    if universe is not None and master is not None and universe != master:
        print("✗ master CIK set differs from universe"); bad += 1
    if universe is not None and filings is not None and universe != filings:
        print("✗ filings CIK set differs from universe"); bad += 1
    if master is not None and companies is not None and not companies <= master:
        print("✗ company CIK is absent from master"); bad += 1
    print("\n体检通过" if bad == 0 else f"\n{bad} 项异常")
    return 0 if bad == 0 else 1


def cmd_manifest():
    """把数据与代码的 md5 写成 MANIFEST.md5，用于跨机部署校验"""
    import glob
    pats = ["out/*.csv", "out/*.txt", "scripts/*.py", "*.py", "*.md"]
    skip = ("out/.state", "__pycache__")
    files = []
    for p in pats:
        for f in glob.glob(os.path.join(BASE, p)):
            rel = os.path.relpath(f, BASE)
            if any(s in rel for s in skip) or os.path.isdir(f):
                continue
            files.append(rel)
    files.sort()
    lines = [f"{md5_of(os.path.join(BASE, r))}  {r}" for r in files]
    p = os.path.join(BASE, "MANIFEST.md5")
    open(p, "w").write("\n".join(lines) + "\n")
    print(f"-> {p}（{len(files)} 个文件）")
    print("校验: cd 项目根 && md5sum -c MANIFEST.md5 | grep -v ': OK' || echo 全部一致")


def main():
    ap = argparse.ArgumentParser(description="退市股数据库工作流")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("check")
    sub.add_parser("manifest")
    r = sub.add_parser("run")
    r.add_argument("--from", dest="frm")
    r.add_argument("--only")
    r.add_argument("--force", action="store_true")
    r.add_argument("--stale", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--keep-going", action="store_true")
    a = ap.parse_args()
    if a.action == "list":
        cmd_list()
    elif a.action == "status":
        cmd_status()
    elif a.action == "check":
        sys.exit(cmd_check())
    elif a.action == "manifest":
        cmd_manifest()
    else:
        sys.exit(cmd_run(a))


if __name__ == "__main__":
    main()
