"""secdata prexbrl — pre-XBRL (SGML-era) filing download + metric extraction.

Pipeline module #4 of the secdata inventory: download the official *complete
submission* SGML text files for filings that predate XBRL, then extract
evidence-linked financial metrics from their HTML/ASCII financial statements.

Upstream EDGAR endpoints (see ``download.py`` / ``parser.py`` docstrings):
    https://data.sec.gov/submissions/CIK##########.json  (+ filings.files
        history JSONs — old rows only exist there)
    https://www.sec.gov/Archives/edgar/data/<cik>/<accession>.txt  (complete
        submission, all DOCUMENTs in one file)
    https://www.sec.gov/files/company_tickers.json  (via ticker.cik_for)

Ported from production ``sec_pre_xbrl_download.py`` (downloader, kept
separate) and ``pre_xbrl_agent`` (parser + deterministic extraction; the
local-LLM consensus stage is out of scope, so extraction here runs the
production *deterministic row geometry* path end to end — every number is
read back from immutable evidence IDs in the source document).

Output layout (all under ``storage.out_root("prexbrl")``):
    docs/<cik>/<accession>.txt            raw complete submissions (EDGAR mirror)
    submissions.jsonl                     manifest, one row per accession
    extractions/<cik>/<accession-nodash>.json  full pre_xbrl_metrics.v1 documents
    metrics.jsonl                         accepted metric rows (append)
    checkpoint/done.txt, failed.txt       resume bookkeeping ("dl|"/"ex|" keys)
    state.json                            last-run counters

Threading: ``--workers`` download/extract threads share the one global
``default_client()`` throttle, so total request rate stays under
``1/SEC_EDGAR_SLEEP`` regardless of worker count.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from .. import storage
from ..client import default_client
from ..ticker import cik10, cik_for
from .download import (
    DEFAULT_FORMS,
    download_filing,
    get_all_submission_rows,
    parse_forms,
    select_rows,
)
from .extract import extract_submission

COMMAND = "prexbrl"
HELP = "download pre-XBRL (SGML-era) complete filings and extract evidence-linked metrics"


def valid_iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}; use YYYY-MM-DD") from exc


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("symbols", nargs="*",
                        help="tickers to archive (resolved via company_tickers.json)")
    parser.add_argument("--cik", action="append", default=None, metavar="CIK",
                        help="CIK (repeatable or comma-separated); label CIK##########")
    parser.add_argument("--universe", metavar="FILE",
                        help="file with one symbol per line ('#' comments allowed)")
    parser.add_argument("--before", type=valid_iso_date, default="2007-01-01",
                        help="exclusive cutoff date (default: 2007-01-01; applies "
                             "to the REPORT period, so a FY2006 annual filed in "
                             "2007 still qualifies)")
    parser.add_argument("--after", type=valid_iso_date,
                        help="optional inclusive lower date bound")
    parser.add_argument("--date-basis", choices=("report", "filing"), default="report",
                        help="apply date bounds to reportDate or filingDate (default: report)")
    parser.add_argument("--forms", default=",".join(DEFAULT_FORMS),
                        help="comma-separated form types, or ALL (default: annual/"
                             "quarterly reports incl. legacy 10-K405 variants)")
    parser.add_argument("--overwrite", action="store_true",
                        help="re-download existing files and re-extract filings")
    parser.add_argument("--list-only", action="store_true",
                        help="print matching filings without downloading")
    parser.add_argument("--no-extract", action="store_true",
                        help="download only; skip SGML parsing/metric extraction")
    parser.add_argument("--scope",
                        choices=("core_statements", "all_entity_core_statements",
                                 "all_financial_tables"),
                        default="core_statements",
                        help="extraction breadth (core statements by default)")
    parser.add_argument("--max-tables", type=int, default=None,
                        help="development aid: process only the first N candidate tables")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent download/extract threads (default 4); all "
                             "share one throttle — scale SEC_EDGAR_SLEEP, not this")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _read_universe(path: str) -> "list[str]":
    out = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def _targets(args, client) -> "tuple[list, int]":
    """[(cik, label)] from symbols + universe file + --cik list; plus error count."""
    symbols = list(args.symbols)
    if args.universe:
        symbols.extend(_read_universe(args.universe))
    ciks: "list[str]" = []
    for value in args.cik or []:
        ciks.extend(part.strip() for part in value.split(",") if part.strip())

    targets: "list[tuple[str, str]]" = []
    seen = set()
    failed = 0
    for symbol in symbols:
        try:
            cik = cik_for(symbol, client)
        except KeyError as exc:
            _err(f"error: {exc}")
            failed += 1
            continue
        label = re.sub(r"[^A-Za-z0-9._-]+", "-", symbol.upper())
        if cik not in seen:
            seen.add(cik)
            targets.append((cik, label))
    for value in ciks:
        digits = re.sub(r"\D", "", value)
        if not digits or len(digits) > 10:
            _err(f"error: invalid CIK {value!r}")
            failed += 1
            continue
        if digits not in seen:
            seen.add(digits)
            targets.append((digits, f"CIK{cik10(digits)}"))
    return targets, failed


def run(args) -> int:
    client = default_client()
    root = storage.out_root("prexbrl")
    docs_root = storage.ensure_dir(root / "docs")
    extractions_root = storage.ensure_dir(root / "extractions")
    checkpoint = storage.Checkpoint(root / "checkpoint")
    done_keys = checkpoint.done()

    targets, resolve_errors = _targets(args, client)
    if not targets:
        _err("error: give at least one ticker, --cik, or --universe FILE")
        return 2

    try:
        forms = parse_forms(args.forms)
    except ValueError as exc:
        _err(f"error: {exc}")
        return 2

    selected_by_target: "list[tuple[str, str, str, list[dict]]]" = []
    for cik, label in targets:
        try:
            sub_root, all_rows = get_all_submission_rows(client, cik)
        except Exception as exc:
            _err(f"error: submissions index failed for {label}: {exc}")
            resolve_errors += 1
            continue
        company_name = str(sub_root.get("name") or label)
        rows = select_rows(all_rows, forms=forms, before=args.before,
                           after=args.after, date_basis=args.date_basis)
        _err(f"Company: {company_name} ({label}, CIK {cik10(cik)})")
        _err(f"SEC submission rows: {len(all_rows):,}")
        _err(f"Matched filings: {len(rows):,} "
             f"[{args.after or 'earliest'}, {args.before}) by {args.date_basis} date")
        if rows:
            from .download import effective_date

            first, last = rows[0], rows[-1]
            _err(f"Range: {effective_date(first, args.date_basis)} "
                 f"{first.get('form')} through "
                 f"{effective_date(last, args.date_basis)} {last.get('form')}")
        if args.list_only:
            for row in rows:
                print(f"{row.get('reportDate') or '-':10}  "
                      f"{row.get('filingDate') or '-':10}  "
                      f"{row.get('form') or '-':10}  {row.get('accessionNumber')}")
        selected_by_target.append((cik, label, company_name, rows))

    if args.list_only:
        return 1 if resolve_errors else 0

    manifest_writer = storage.JsonlWriter(root / "submissions.jsonl")
    metrics_writer = storage.JsonlWriter(root / "metrics.jsonl")
    write_lock = threading.Lock()
    counts = {"downloaded": 0, "existing": 0, "errors": resolve_errors, "bytes": 0}

    # ---------------------------------------------------------- download
    def _do_download(job):
        cik, label, company_name, row = job
        accession = str(row["accessionNumber"])
        key = f"dl|{cik}|{accession}"
        record = download_filing(client, row, label=label,
                                 company_name=company_name, cik=cik,
                                 docs_root=docs_root, overwrite=args.overwrite)
        return key, record

    jobs = [(cik, label, name, row)
            for cik, label, name, rows in selected_by_target for row in rows]
    ready_for_extract: "list[tuple]" = []
    workers = max(1, args.workers)
    if jobs:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, record in pool.map(_do_download, jobs):
                with write_lock:
                    manifest_writer.write(record)
                    if record["status"] == "error":
                        checkpoint.mark_failed(key)
                    else:
                        checkpoint.mark_done(key)
                if record["status"] == "error":
                    _err(f"[download error] {record['ticker']} {record['form']} "
                         f"{record['accession_number']}: {record['error']}")
                    counts["errors"] += 1
                elif record["status"] == "existing":
                    counts["existing"] += 1
                else:
                    counts["downloaded"] += 1
                counts["bytes"] += int(record["bytes"] or 0)
                if record["status"] in ("downloaded", "existing"):
                    ready_for_extract.append(
                        (record["cik"], record["accession_number"], record["form"],
                         record["report_date"], record["filing_date"],
                         record["local_path"]))

    # ----------------------------------------------------------- extract
    extract_counts = {"extracted": 0, "skipped": 0, "errors": 0}
    if not args.no_extract and ready_for_extract:
        def _do_extract(job):
            cik, accession, form, report_date, filing_date, local_path = job
            key = f"ex|{cik}|{accession}"
            if key in done_keys and not args.overwrite:
                return key, "skipped", None
            try:
                with open(local_path, "rb") as handle:
                    body = handle.read()
                result = extract_submission(
                    body,
                    {
                        "cik": cik10(cik),
                        "date": report_date or filing_date,
                        "form": form,
                        "accession": accession,
                    },
                    scope=args.scope,
                    max_tables=args.max_tables,
                )
            except Exception as exc:
                return key, "error", exc
            target = storage.ensure_dir(
                extractions_root / str(int(cik))
            ) / f"{accession.replace('-', '')}.json"
            storage.save_doc(target.parent, target.name,
                             json.dumps(result, ensure_ascii=False, indent=1)
                             .encode("utf-8"))
            return key, "ok", result

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, status, payload in pool.map(_do_extract, ready_for_extract):
                if status == "skipped":
                    extract_counts["skipped"] += 1
                    continue
                if status == "error":
                    _err(f"[extract error] {key}: {payload}")
                    with write_lock:
                        checkpoint.mark_failed(key)
                    extract_counts["errors"] += 1
                    counts["errors"] += 1
                    continue
                with write_lock:
                    checkpoint.mark_done(key)
                    for metric in payload["metrics"]:
                        row = {
                            "cik": payload["filing"]["cik"],
                            "accession": payload["filing"]["accession"],
                            "form": payload["filing"]["form"],
                            "date": payload["filing"]["date"],
                            "registrant_name_raw": payload["filing"]["registrant_name_raw"],
                        }
                        row.update(metric)
                        metrics_writer.write(row)
                extract_counts["extracted"] += 1

    manifest_writer.close()
    metrics_writer.close()

    state = storage.State(root / "state.json")
    state.set("downloaded", counts["downloaded"])
    state.set("existing", counts["existing"])
    state.set("download_errors", counts["errors"])
    state.set("extracted", extract_counts["extracted"])
    state.set("extract_skipped", extract_counts["skipped"])
    state.set("extract_errors", extract_counts["errors"])
    state.set("last_run", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    _err(f"Manifest: {root / 'submissions.jsonl'}")
    _err(f"Files: {counts['downloaded'] + counts['existing']:,} ok, "
         f"{counts['errors']:,} errors, {counts['bytes']:,} bytes")
    if not args.no_extract:
        _err(f"Extractions: {extract_counts['extracted']:,} new, "
             f"{extract_counts['skipped']:,} skipped (checkpointed), "
             f"{extract_counts['errors']:,} errors -> {extractions_root}")
    return 1 if counts["errors"] else 0
