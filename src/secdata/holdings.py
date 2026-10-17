"""13F and N-PORT holdings downloader, parser and comparator.

Production source: ``sec_13f_nport_full_db.py`` (2026-07, canonical),
predecessors ``download_13f_v3.py`` / ``sec_download_13f_nport.py`` for
parser edge-cases, and ``build_nport_ticker_map.py`` for the
issuer-name → ticker mapping rule. MySQL/FastAPI serving code
(``sec_13f_api.py``) informed the row field names and the comparison
GROUP BY semantics; the DB itself is replaced by JSONL files.

Upstream endpoints (all through the shared throttled client):

- ``https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{n}/master.{gz|zip|idx}``
  — quarterly full index, ``CIK|Company|Form|Filed|FileName.txt`` lines
  (the production bulk-discovery approach; try order gz → zip → idx);
- ``https://www.sec.gov/Archives/edgar/data/{cik}/{accessionNoDash}/`` —
  accession directory: ``index.json`` (with ``--full-accession-docs``)
  and the immutable full submission ``{accession}.txt`` (fast default);
- ``https://data.sec.gov/submissions/CIK{cik10}.json`` — targeted mode
  when the caller gives tickers/CIKs (form-filtered via
  ``ticker.filings_of``, amendment-aware);
- ``https://www.sec.gov/files/company_tickers.json`` — issuer-name →
  ticker index (via ``ticker.BRIDGE_TICKERS_URL``, cached 7 days).

Output layout (under ``out/holdings/``):

- ``holdings_13f.jsonl`` / ``holdings_nport.jsonl`` — parsed rows, field
  names identical to the production tables; append + dedupe by
  ``row_hash`` so re-runs are free;
- ``filings_index.jsonl`` — discovered filings (resume ledger);
- ``parse_errors.jsonl`` — per-document parse failures, like production's
  ``parse_errors`` table;
- ``issuer_ticker_map.jsonl`` — ``issuer-map`` output (clean-name exact
  match, production rule);
- ``compare_13f.jsonl`` / ``compare_nport.jsonl`` /
  ``compare_pairwise_{a}_{b}.jsonl`` — period-over-period and
  institution-vs-institution diffs;
- ``checkpoint/done.txt|failed.txt|empty.txt|quarters/done.txt`` and
  ``state.json``; raw text attachments mirrored under ``docs/{cik10}/{acc}/``
  with ``--save-docs``.

All HTTP goes through one ``default_client()`` so ``SEC_EDGAR_SLEEP``
(>= 0.15 s) throttles the whole process; with many ``--workers`` keep the
sleep so the combined rate stays under SEC's 10 req/s fair-access cap.

Actions: ``download`` (bulk or targeted), ``parse`` (local XML/TXT),
``compare`` (QoQ diff or pairwise holders), ``issuer-map``.
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, List
from urllib.parse import quote, urljoin

from . import holdings_parsers as parsers
from . import storage, ticker
from .client import default_client

COMMAND = "holdings"
HELP = "13F & N-PORT holdings: bulk download, parse, compare"

F_13F = "holdings_13f.jsonl"
F_NPORT = "holdings_nport.jsonl"
F_INDEX = "filings_index.jsonl"
F_ERRORS = "parse_errors.jsonl"
F_ISSUER = "issuer_ticker_map.jsonl"

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


# ------------------------------------------------------------------ argparse
def add_arguments(parser):
    sub = parser.add_subparsers(dest="action", metavar="<action>")

    dl = sub.add_parser("download", help="discover + fetch + parse 13F/N-PORT")
    dl.add_argument("symbols", nargs="*",
                    help="tickers to target (resolved via ticker.cik_for); "
                         "omit for bulk full-index discovery")
    dl.add_argument("--cik", action="append", default=[], metavar="CIK",
                    help="target one CIK directly (repeatable)")
    dl.add_argument("--forms", default=parsers.DEFAULT_FORMS,
                    help="comma-separated forms (default: holdings-bearing)")
    dl.add_argument("--all-sec-forms", action="store_true",
                    help="also 13F-NT notices and NPORT exhibits")
    dl.add_argument("--start-year", type=int, default=1994,
                    help="bulk discovery start year (EDGAR indexes begin 1994Q3)")
    dl.add_argument("--end-year", type=int, help="bulk discovery end year (default: now)")
    dl.add_argument("--end-quarter", type=int, choices=(1, 2, 3, 4),
                    help="bulk discovery end quarter (default: current)")
    dl.add_argument("--after", help="targeted mode: filing date >= YYYY-MM-DD")
    dl.add_argument("--before", help="targeted mode: filing date <= YYYY-MM-DD")
    dl.add_argument("--universe", metavar="FILE",
                    help="one ticker symbol per line; filter filings to those CIKs")
    dl.add_argument("--workers", type=int, default=4,
                    help="concurrent filing workers (HTTP still throttled globally)")
    dl.add_argument("--limit", type=int, help="process at most N pending filings")
    dl.add_argument("--discover-only", action="store_true",
                    help="only populate filings_index.jsonl from quarterly indexes")
    dl.add_argument("--rediscover", action="store_true",
                    help="re-fetch quarterly indexes even for quarters already done")
    dl.add_argument("--retry-failed", action="store_true",
                    help="retry accessions in checkpoint/failed.txt")
    dl.add_argument("--no-reparse-empty", action="store_true",
                    help="do not revisit filings that parsed to zero holdings")
    dl.add_argument("--full-accession-docs", action="store_true",
                    help="fetch index.json + every text attachment; default downloads "
                         "only the full submission .txt (faster, same rows)")
    dl.add_argument("--include-binary", action="store_true",
                    help="also download non-text attachments (hashed/kept with --save-docs)")
    dl.add_argument("--max-document-bytes", type=int, default=0,
                    help="fail a filing whose document exceeds this size; 0 = no cap")
    dl.add_argument("--save-docs", action="store_true",
                    help="keep raw text docs under docs/{cik10}/{accession}/ (off by default)")
    dl.add_argument("--progress-every", type=int, default=25)

    pr = sub.add_parser("parse", help="parse local 13F/N-PORT XML or .txt files")
    pr.add_argument("paths", nargs="+", help="files or directories")
    pr.add_argument("--accession", default="", help="accession for row metadata "
                                                    "(default: filename if it looks like one)")
    pr.add_argument("--cik", default="", help="CIK for row metadata")
    pr.add_argument("--form", default="", help="form type (auto-detected by content)")
    pr.add_argument("--filer-name", default="", help="filer/registrant name")
    pr.add_argument("--filing-date", default="", help="filing date YYYY-MM-DD")
    pr.add_argument("--report-date", default="", help="force report_date on rows")

    cp = sub.add_parser("compare", help="quarter-over-quarter or pairwise holdings diff")
    cp.add_argument("--file", help="input rows JSONL "
                                   "(default: out/holdings/holdings_13f.jsonl; "
                                   "N-PORT detected by row fields, or pass holdings_nport.jsonl)")
    cp.add_argument("--between", nargs=2, metavar=("CIK_A", "CIK_B"),
                    help="institution-vs-institution diff at one period "
                         "instead of the period-over-period view")
    cp.add_argument("--date", help="pairwise: exact report_date; QoQ: restrict to this period")
    cp.add_argument("--after", help="QoQ: period_date >=")
    cp.add_argument("--before", help="QoQ: period_date <=")
    cp.add_argument("--cik", action="append", default=[],
                    help="restrict to holder CIK(s) (repeatable)")
    cp.add_argument("--long-only", action="store_true",
                    help="13F: keep plain long positions (put_call blank/share), "
                         "as production's stock-level queries did")

    im = sub.add_parser("issuer-map",
                        help="build name_of_issuer -> ticker map (clean-name exact match)")
    im.add_argument("--file", help="rows to map (default: out/holdings/holdings_13f.jsonl)")
    im.add_argument("--extra", metavar="FILE",
                    help="JSON {\"ISSUER NAME\": \"TICKER\"} or [[name, ticker], ...] "
                         "merged over SEC company_tickers")

    return parser


def run(args) -> int:
    action = getattr(args, "action", None)
    if action == "download":
        return _download(args)
    if action == "parse":
        return _parse(args)
    if action == "compare":
        return _compare(args)
    if action == "issuer-map":
        return _issuer_map(args)
    print("holdings: pick an action: download | parse | compare | issuer-map",
          file=sys.stderr)
    return 2


# -------------------------------------------------------------------- download
def _forms_set(args) -> "set[str]":
    raw = parsers.ALL_FORMS if args.all_sec_forms else args.forms
    return {parsers.normalize_form(f) for f in raw.split(",") if f.strip()}


def _universe_ciks(client, path) -> "set[str]":
    ciks = set()
    text = Path(path).read_text(encoding="utf-8")
    for sym in text.splitlines():
        sym = sym.strip()
        if not sym or sym.startswith("#"):
            continue
        try:
            ciks.add(str(int(ticker.cik_for(sym, client))))
        except KeyError:
            print(f"  universe: no CIK for {sym!r}, skipped", file=sys.stderr)
    return ciks


def _fetch_quarter_index(client, year: int, qtr: int) -> str:
    """master.gz → master.zip → master.idx, as production tried them."""
    base = f"{parsers.SEC_ARCHIVES}edgar/full-index/{year}/QTR{qtr}/"
    live = (year, qtr) == parsers.current_quarter()
    max_age = 24.0 if live else None  # closed quarters are immutable
    last_err = None
    for name in ("master.gz", "master.zip", "master.idx"):
        url = base + name
        try:
            return parsers.decode_index_payload(url, client.get_bytes(url,
                                                                      max_age_hours=max_age))
        except Exception as exc:  # noqa: BLE001 - move to next format
            last_err = exc
    raise RuntimeError(f"quarter index {year}Q{qtr} unreachable: {last_err}")


def _discover(client, forms, args, qchk, idx_writer) -> int:
    """Stream quarterly indexes into filings_index.jsonl. Returns new filings."""
    end_year, end_qtr = args.end_year, args.end_quarter
    if not end_year:
        end_year, end_qtr = parsers.current_quarter()
    if not end_qtr:
        end_qtr = 4
    quarters = parsers.quarter_range(args.start_year, end_year, end_qtr)
    done = set(qchk.done())
    wanted = [q for q in quarters
              if f"{q[0]}Q{q[1]}" not in done or args.rediscover]
    print(f"discovery: {len(wanted)} of {len(quarters)} quarters pending",
          file=sys.stderr)
    found = 0
    for i, (year, qtr) in enumerate(wanted, 1):
        key = f"{year}Q{qtr}"
        try:
            text = _fetch_quarter_index(client, year, qtr)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(wanted)}] {key}: FAILED {exc}", file=sys.stderr)
            qchk.mark_failed(key)
            continue
        entries = parsers.parse_master_index(text, forms, year, qtr)
        qchk.mark_done(key)
        idx_writer.write_many(entries)
        found += len(entries)
        print(f"  [{i}/{len(wanted)}] {key}: {len(entries):,} filings "
              f"(run total {found:,})", file=sys.stderr)
    return found


def _tasks_from_submissions(client, cik: str, forms, after, before) -> "List[dict]":
    """Targeted mode: one CIK's own filing history (v4's submissions-API approach)."""
    c10 = ticker.cik10(cik)
    d = client.get_json(f"https://data.sec.gov/submissions/CIK{c10}.json",
                        max_age_hours=24)
    name = d.get("name", "")
    out = []
    for r in ticker.filings_of(d, forms=sorted(forms), after=after, before=before):
        acc = r["accession"]
        out.append({
            "accession": acc,
            "cik": str(int(cik)),
            "cik10": c10,
            "company_name": name,
            "form_type": parsers.normalize_form(r["form"]),
            "filing_date": r["filing_date"],
            "filename": r["primary_document"],
            "accession_no_dash": parsers.accession_no_dash(acc),
            "accession_dir_url": ticker.archive_url(cik, acc),
            "full_submission_url": ticker.archive_url(cik, acc, f"{acc}.txt"),
            "report_date_hint": r.get("report_date") or None,
        })
    return out


def _load_documents(client, task: "dict[str, Any]", args, root) -> "List[tuple]":
    """(filename, bytes) list. Fast mode = only the full submission .txt."""
    docs: "List[tuple]" = []
    seen: "set[str]" = set()

    def add(name: str, url: str):
        if name in seen or not parsers.valid_attachment(name, args.include_binary):
            return
        content = client.get_bytes(url)
        if args.max_document_bytes > 0 and len(content) > args.max_document_bytes:
            raise RuntimeError(f"{name} exceeds --max-document-bytes")
        seen.add(name)
        docs.append((name, content))
        if args.save_docs:
            storage.save_doc(Path(root) / "docs" / task["cik10"]
                             / task["accession_no_dash"], Path(name).name, content)

    if args.full_accession_docs:
        try:
            idx_url = urljoin(task["accession_dir_url"], "index.json")
            idx = json.loads(parsers.maybe_decode(client.get_bytes(idx_url)))
            for item in idx.get("directory", {}).get("item", []):
                name = item.get("name") or ""
                href = item.get("href") or name
                add(name, urljoin(task["accession_dir_url"], quote(href, safe="/.-_")))
        except Exception:  # noqa: BLE001 - fall back like production did
            docs = []
            seen = set()
    fname = task["full_submission_url"].rsplit("/", 1)[-1]
    add(fname, task["full_submission_url"])
    return docs


def _meta_for(task: "dict[str, Any]") -> "dict[str, Any]":
    return {
        "accession": task["accession"],
        "cik": task["cik"],
        "cik10": task["cik10"],
        "filer_name": task.get("company_name", ""),
        "form_type": task.get("form_type", ""),
        "filing_date": task.get("filing_date", ""),
    }


def _process_filing(client, task, args, root) -> "dict[str, Any]":
    res: "dict[str, Any]" = {"task": task, "ok": True, "rows13": [],
                             "rowsN": [], "errors": [], "error": None}
    try:
        docs = _load_documents(client, task, args, root)
        if not docs:
            res.update(ok=False, error="no documents fetched")
            return res
        pairs = [(fn, parsers.maybe_decode(content)) for fn, content in docs]
        meta = _meta_for(task)
        form = task.get("form_type", "")
        if form.startswith("13F-"):
            rows, rd, errs = parsers.parse_13f_documents(meta, pairs)
            if rd is None and task.get("report_date_hint"):
                rd = task["report_date_hint"]
            res.update(rows13=rows, errors=errs, report_date=rd)
        elif form.startswith("NPORT"):
            rows, rd, _por, errs = parsers.parse_nport_documents(meta, pairs)
            res.update(rowsN=rows, errors=errs, report_date=rd)
        res["notice"] = form.startswith("13F-NT")
    except Exception as exc:  # noqa: BLE001
        res.update(ok=False, error=str(exc))
    return res


def _append_line(path: Path, key: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(key + "\n")


def _read_line_set(path: Path) -> "set[str]":
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()}


def _download(args) -> int:
    forms = _forms_set(args)
    if not forms:
        print("error: --forms cannot be empty", file=sys.stderr)
        return 1
    client = default_client()
    root = storage.out_root(COMMAND)
    chk = storage.Checkpoint(root / "checkpoint")
    qchk = storage.Checkpoint(root / "checkpoint" / "quarters")
    empty_file = root / "checkpoint" / "empty.txt"
    state = storage.State(root / "state.json")

    ciks = [str(c) for c in args.cik]
    for sym in args.symbols:
        try:
            ciks.append(ticker.cik_for(sym, client))
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    universe = _universe_ciks(client, args.universe) if args.universe else None
    idx_writer = storage.JsonlWriter(root / F_INDEX, key="accession")

    tasks: "List[dict]" = []
    if ciks:
        for cik in ciks:
            tasks += _tasks_from_submissions(client, cik, forms,
                                             args.after, args.before)
        idx_writer.write_many(tasks)
    else:
        found = _discover(client, forms, args, qchk, idx_writer)
        print(f"discovery complete: {found:,} filings this run", file=sys.stderr)
        if not args.discover_only:
            tasks = list(storage.read_jsonl(root / F_INDEX))
    if args.discover_only:
        idx_writer.close()
        state.set("last_discover", parsers.utc_now())
        return 0

    done = chk.done()
    failed = chk.failed()
    empty = _read_line_set(empty_file)
    skip = set(done)
    skip |= (empty if args.no_reparse_empty else set())
    if not args.retry_failed:
        skip |= failed
    pending = [t for t in tasks
               if t["accession"] not in skip
               and parsers.normalize_form(t.get("form_type", "")) in forms
               and (universe is None
                    or str(int(t["cik"])) in universe)]
    if args.limit:
        pending = pending[:args.limit]
    print(f"holdings download: {len(pending):,} filings to process "
          f"({len(done):,} done, {len(failed):,} failed, {len(empty):,} empty, "
          f"workers={args.workers}; one global SEC_EDGAR_SLEEP throttle "
          "governs all threads)", file=sys.stderr)
    idx_writer.close()
    if not pending:
        print("nothing pending — re-run is free thanks to checkpoint",
              file=sys.stderr)
        return 0

    w13 = storage.JsonlWriter(root / F_13F, key="row_hash")
    wn = storage.JsonlWriter(root / F_NPORT, key="row_hash")
    we = storage.JsonlWriter(root / F_ERRORS)
    start = time.time()
    ok_n = fail_n = rows13_n = rowsN_n = 0
    pool = futures.ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        it = iter(pending)
        inflight: "set" = set()
        max_inflight = max(args.workers * 4, args.workers)

        def submit_more():
            while len(inflight) < max_inflight:
                try:
                    t = next(it)
                except StopIteration:
                    break
                inflight.add(pool.submit(_process_filing, client, t, args, root))

        submit_more()
        while inflight:
            finished, inflight = futures.wait(
                inflight, timeout=1.0, return_when=futures.FIRST_COMPLETED)
            for fut in finished:
                res = fut.result()
                task = res["task"]
                acc = task["accession"]
                if res["ok"]:
                    n13 = w13.write_many(res["rows13"])
                    nn = wn.write_many(res["rowsN"])
                    for fn, stage, err in res["errors"]:
                        we.write({"accession": acc, "form_type":
                                  task.get("form_type", ""),
                                  "source_filename": fn, "stage": stage,
                                  "error": err, "created_at": parsers.utc_now()})
                    ok_n += 1
                    rows13_n += n13
                    rowsN_n += nn
                    if n13 or nn or res.get("notice"):
                        chk.mark_done(acc)
                    else:
                        _append_line(empty_file, acc)
                else:
                    fail_n += 1
                    chk.mark_failed(acc)
                    print(f"  failed {acc} ({task.get('form_type')}): "
                          f"{res['error']}", file=sys.stderr)
                processed = ok_n + fail_n
                if processed % args.progress_every == 0 \
                        or time.time() - start > 30:
                    elapsed = time.time() - start
                    rate = processed / elapsed if elapsed else 0
                    print(f"  [{processed:,}/{len(pending):,}] ok={ok_n:,} "
                          f"fail={fail_n:,} 13f_rows={rows13_n:,} "
                          f"nport_rows={rowsN_n:,} rate={rate:.2f}/s",
                          file=sys.stderr)
            submit_more()
    except KeyboardInterrupt:
        print("\ninterrupted — checkpoint keeps progress; re-run resumes",
              file=sys.stderr)
        return 130
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        w13.close()
        wn.close()
        we.close()
        state.set("last_run", parsers.utc_now())
    print(f"holdings download complete: ok={ok_n:,} failed={fail_n:,} "
          f"13f_rows={rows13_n:,} nport_rows={rowsN_n:,} "
          f"time={time.time()-start:.0f}s", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------- parse
def _parse(args) -> int:
    root = storage.out_root(COMMAND)
    files: "List[Path]" = []
    for p in args.paths:
        pp = Path(p)
        if pp.is_dir():
            files.extend(f for f in sorted(pp.rglob("*"))
                         if f.is_file()
                         and f.suffix.lower() in parsers.TEXT_EXTENSIONS)
        elif pp.is_file():
            files.append(pp)
        else:
            print(f"holdings parse: no such path {p}", file=sys.stderr)
    if not files:
        print("holdings parse: nothing to do", file=sys.stderr)
        return 1

    def build_meta(path: Path) -> "dict[str, Any]":
        stem = path.stem
        acc = args.accession or (stem if ACCESSION_RE.match(stem) else "")
        cik = str(int(args.cik)) if args.cik else ""
        return {
            "accession": acc, "cik": cik,
            "cik10": parsers.cik10(cik) if cik else "",
            "filer_name": args.filer_name, "form_type": args.form,
            "filing_date": args.filing_date,
        }

    w13 = storage.JsonlWriter(root / F_13F, key="row_hash")
    wn = storage.JsonlWriter(root / F_NPORT, key="row_hash")
    we = storage.JsonlWriter(root / F_ERRORS)
    n13 = nn = 0
    try:
        for path in files:
            text = parsers.maybe_decode(path.read_bytes())
            payloads = " ".join(parsers.iter_xml_payloads(text)).lower()
            meta = build_meta(path)
            docs = [(path.name, text)]
            is_nport = "invstorsec" in payloads
            if not is_nport and "infotable" not in payloads \
                    and "FORM 13F INFORMATION TABLE" not in text:
                print(f"  {path.name}: no 13F/N-PORT content, skipped",
                      file=sys.stderr)
                continue
            if is_nport:
                rows, _rd, _por, errs = parsers.parse_nport_documents(meta, docs)
            else:
                rows, _rd, errs = parsers.parse_13f_documents(meta, docs)
            if args.report_date:
                for r in rows:
                    if not r.get("report_date"):
                        r["report_date"] = args.report_date
            for fn, stage, err in errs:
                we.write({"accession": meta["accession"],
                          "form_type": meta["form_type"],
                          "source_filename": fn, "stage": stage, "error": err,
                          "created_at": parsers.utc_now()})
            if is_nport:
                nn += wn.write_many(rows)
            else:
                n13 += w13.write_many(rows)
            print(f"  {path.name}: {len(rows):,} rows", file=sys.stderr)
    finally:
        w13.close()
        wn.close()
        we.close()
    print(f"holdings parse: {n13:,} new 13F rows, {nn:,} new N-PORT rows "
          f"-> {root}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------- compare
# The pure diff builders live in holdings_parsers (diff_periods /
# diff_pairwise) so tests and other tools can import them without CLI.
def _compare(args) -> int:
    root = storage.out_root(COMMAND)
    src = Path(args.file) if args.file else root / F_13F
    if not src.exists():
        print(f"compare: no such file {src}", file=sys.stderr)
        return 1
    rows = list(storage.read_jsonl(src))
    if not rows:
        print(f"compare: {src} is empty", file=sys.stderr)
        return 1
    kind = parsers.kind_of(rows)
    if args.between:
        diffs = parsers.diff_pairwise(rows, args.between[0], args.between[1],
                              date=args.date, kind=kind)
        out_path = root / (f"compare_pairwise_{args.between[0]}_"
                           f"{args.between[1]}.jsonl")
        if not diffs:
            print("compare: the two holders share no report_date "
                  "(or the --date has no rows for both)", file=sys.stderr)
    else:
        diffs = parsers.diff_periods(rows, kind=kind,
                             after=args.date or args.after,
                             before=args.date or args.before,
                             holders=args.cik or None,
                             long_only=args.long_only)
        out_path = root / f"compare_{kind}.jsonl"
    n = storage.write_jsonl(out_path, diffs)
    print(f"compare: {n:,} rows -> {out_path}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ issuer-map
def _issuer_map(args) -> int:
    client = default_client()
    root = storage.out_root(COMMAND)
    src = Path(args.file) if args.file else root / F_13F
    if not src.exists():
        print(f"issuer-map: no such file {src}", file=sys.stderr)
        return 1

    index: "dict[str, str]" = {}
    raw = client.get_json(ticker.BRIDGE_TICKERS_URL, max_age_hours=24 * 7)
    for e in raw.values():
        c = parsers.clean_issuer_name(e.get("title", ""))
        if c and len(c) > 3:
            index[c] = e.get("ticker", "")
    if args.extra:
        payload = json.loads(Path(args.extra).read_text(encoding="utf-8"))
        pairs = (payload if isinstance(payload, list)
                 else payload.items())
        for name, sym in pairs:
            c = parsers.clean_issuer_name(str(name))
            if c and len(c) > 3:
                index[c] = str(sym).upper()

    w = storage.JsonlWriter(root / F_ISSUER, key="name_of_issuer")
    tested = matched = 0
    try:
        for r in storage.read_jsonl(src):
            name = (r.get("name_of_issuer") or "").strip()
            if not name or name == "N/A":
                continue
            tested += 1
            c = parsers.clean_issuer_name(name)
            sym = index.get(c)
            if sym:
                matched += 1
                w.write({"name_of_issuer": name, "clean_name": c,
                         "ticker": sym})
    finally:
        w.close()
    rate = (100.0 * matched / tested) if tested else 0.0
    print(f"issuer-map: {matched:,}/{tested:,} names matched ({rate:.1f}%) "
          f"-> {root / F_ISSUER}", file=sys.stderr)
    return 0
