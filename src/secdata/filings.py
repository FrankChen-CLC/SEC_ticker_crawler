"""secdata filings — SEC EDGAR bulk filing downloader & form filters.

Upstream endpoints (preserved exactly as in the production lineage):

- ``https://data.sec.gov/submissions/CIK{cik10}.json`` — per-company filing
  index ("recent" block; older ``-submissions-NNN.json`` history files are
  only pulled with ``--include-historical``);
- ``https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{n}/master.idz``
  (gzip) with fallback ``.../master.idx`` — quarterly whole-market index used
  by ``--scan`` (production insight: 86 quarterly requests instead of ~7,800
  daily ones);
- ``https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dash}/``
  — the archive dir; ``{accession}.txt`` is the full submission archive
  (``--docs full``), ``{primaryDocument}`` the filing's primary document
  (``--docs primary``);
- ``https://www.sec.gov/files/company_tickers.json`` — ticker→CIK resolution
  (via the shared :mod:`secdata.ticker`).

Ported from the production scripts ``sec_download_all.py`` (quarterly index
scan + submissions filter + ``{cik}_{accession}`` done/fail resume) and
``sec_filings_bulk_download.py`` (per-company inventory over a universe,
exact form-list filter, primary-doc download with a minimum-body guard,
4-worker ThreadPoolExecutor).  ``download_regulatory_all.py``'s raw
fetch/backoff loop is superseded by the shared throttled/cached
:class:`secdata.client.EdgarClient`.

Production behaviours kept on purpose:
- **Exact form filtering**: production tested ``form in forms`` literally, so
  ``10-K/A`` is only matched when listed; pass ``--include-amendments`` to get
  the amendment-aware matching of ``ticker.form_matches`` instead.
- **Quarterly index decode**: ``.idz`` first, then ``.idx``; byte content is
  decoded trying utf-8, latin-1, cp1252 (EDGAR emits cp1252 company names).
- **Resume semantics**: keys are ``{cik}_{accession}``; items in ``failed.txt``
  are skipped on re-runs exactly like production — pass ``--retry-failed`` to
  re-attempt them.  Immutable docs and cached indexes make re-runs cheap.
- **Tiny-body guard**: production only accepted a primary document longer
  than 500 bytes because SEC sometimes answers a bad ``primaryDocument`` with
  a small generic HTML error page under HTTP 200.  Kept (``MIN_PRIMARY_BYTES``).
- **CIK path quirk**: archive dirs use the *unpadded* CIK while submissions
  URLs use the 10-digit padded one (both preserved via ``ticker.archive_url``
  / ``ticker.cik10``); the local layout mirrors the unpadded EDGAR path.
- ``primaryDocument`` is empty for some filings — production skipped those;
  skipped here too (the row is still inventoried).

Output: ``out/filings/manifest.jsonl`` (one row per filing, deduped by
accession), ``out/filings/docs/{cik}/{accession_no_dash}/…`` (mirrors EDGAR
archive paths), ``out/filings/checkpoint/done.txt|failed.txt`` and
``out/filings/state.json``.

Threads: ``--workers`` (default 4, production's MAX_WORKERS) fans companies
out, but every request still passes through the single shared client throttle.
With the default ``SEC_EDGAR_SLEEP=0.15`` this stays under SEC's 10 req/s;
raise the sleep if you push more workers.  Inventory-only runs use
``--docs none``; nothing requires a database.
"""
import gzip
import os
import sys
import threading
import time
import urllib.error
from datetime import date
from pathlib import Path

from . import storage, ticker
from .client import default_client

COMMAND = "filings"
HELP = "Download EDGAR filing indexes and documents, filtered by form type and date window."

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
HISTORICAL_BASE = "https://data.sec.gov/submissions/"
FULL_INDEX_URL = ("https://www.sec.gov/Archives/edgar/full-index/"
                  "{year}/QTR{quarter}/master{ext}")
# production sec_filings_bulk_download.py accepted a primary doc only when
# len(text) > 500 — SEC error pages served under HTTP 200 are smaller.
MIN_PRIMARY_BYTES = 500

DEFAULT_WORKERS = 4


# --------------------------------------------------------------------- pure helpers
def _split_csv(values):
    """Flatten repeatable/``--flag a,b,c`` style options into a clean list."""
    out = []
    for v in values or []:
        for piece in str(v).split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def quarters_between(start, end):
    """Quarters covering [start, end].

    Same enumeration as production ``scan_quarterly``: begin at the quarter
    containing ``start`` and advance while the quarter's first month still
    starts on or before ``end``.
    """
    quarters = []
    y, q = start.year, (start.month - 1) // 3 + 1
    while date(y, (q - 1) * 3 + 1, 1) <= end:
        quarters.append((y, q))
        q += 1
        if q > 4:
            q = 1
            y += 1
    return quarters


def current_quarter(when=None):
    when = when or date.today()
    return (when.year, (when.month - 1) // 3 + 1)


def decode_index_bytes(raw):
    """Full-index bytes -> lines, trying the production encoding ladder."""
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc).split("\n")
        except Exception:
            continue
    return None


def parse_index(line):
    """One ``CIK|Company|Form|Date|Filename`` row of a full-index file.

    Rows with fewer than 5 fields (blank lines, footer junk) are rejected,
    exactly like production; the filename column is kept — the accession dir
    is recoverable from it if you need the document path.
    """
    parts = line.split("|")
    if len(parts) < 5:
        return None
    return {
        "cik": parts[0].strip(),
        "company": parts[1].strip(),
        "form": parts[2].strip(),
        "filed_at": parts[3].strip(),
        "filename": parts[4].strip(),
    }


def select_rows(blocks, forms=None, since=None, until=None,
                include_amendments=False):
    """Flatten submissions column-blocks into filtered row dicts.

    Date window is inclusive on both ends (production string compare on
    ``filingDate``).  Form matching defaults to production exact membership;
    ``include_amendments`` switches to ``ticker.form_matches`` so a plain
    ``10-K`` request also captures ``10-K/A``.
    """
    rows = []
    for block in blocks:
        if not block or not block.get("accessionNumber"):
            continue
        rows.extend(ticker.filings_of({"filings": {"recent": block}},
                                      after=since, before=until))
    if forms:
        wanted = {f.upper() for f in forms}
        if include_amendments:
            rows = [r for r in rows if ticker.form_matches(r["form"], wanted)]
        else:
            rows = [r for r in rows if r["form"] in wanted]
    return rows


def plan_docs(cik, row, docs="primary"):
    """(kind, url, filename) downloads for one filing row."""
    acc = row["accession"]
    plans = []
    if docs in ("primary", "both"):
        primary = (row.get("primary_document") or "").strip()
        if primary:  # some filings genuinely have no primaryDocument
            name = os.path.basename(primary)
            plans.append(("primary", ticker.archive_url(cik, acc, name), name))
    if docs in ("full", "both"):
        name = acc + ".txt"
        plans.append(("full", ticker.archive_url(cik, acc, name), name))
    return plans


def is_valid_doc_body(kind, data):
    """Production's minimum-body guard for downloaded documents.

    ``sec_filings_bulk_download.py`` accepted a primary document only when
    ``len(text) > 500``; the full ``.txt`` archive only needed to be non-empty.
    """
    if not data:
        return False
    if kind == "primary":
        return len(data) > MIN_PRIMARY_BYTES
    return len(data) >= 1


# --------------------------------------------------------------------- index scan
def get_quarterly_index(client, year, quarter):
    """Download one quarterly master index; ``.idz`` (gzip) preferred.

    Past quarters are immutable -> cached forever; the quarter we are inside
    is refreshed daily (max_age 24 h), which is what the production daily
    crons relied on.
    """
    max_age = 24 if (year, quarter) == current_quarter() else None
    for ext in (".idz", ".idx"):
        url = FULL_INDEX_URL.format(year=year, quarter=quarter, ext=ext)
        try:
            raw = client.get_bytes(url, max_age_hours=max_age)
        except urllib.error.HTTPError:
            continue  # 404 (very old quarter, missing .idz) -> try next form
        except Exception:
            continue
        if ext == ".idz":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                continue
        lines = decode_index_bytes(raw)
        if lines:
            return lines
    return None


def scan_full_index(client, quarters, forms):
    """Whole-market discovery: CIKs that filed any of ``forms`` in ``quarters``.

    Returns a set of unpadded CIK strings, as they appear in the index files.
    """
    wanted = {f.upper() for f in forms}
    ciks = set()
    for i, (y, q) in enumerate(quarters, 1):
        lines = get_quarterly_index(client, y, q)
        if lines is None:
            print("  scan %d Q%d (%d/%d): index unavailable, skipped"
                  % (y, q, i, len(quarters)), file=sys.stderr)
            continue
        added = 0
        for line in lines:
            row = parse_index(line)
            if row and row["form"].upper() in wanted:
                ciks.add(row["cik"])
                added += 1
        print("  scan %d Q%d (%d/%d): +%d filings, %d distinct CIKs"
              % (y, q, i, len(quarters), added, len(ciks)), file=sys.stderr)
    return ciks


# --------------------------------------------------------------------- targets
def _read_universe(path):
    if not os.path.exists(path):
        raise FileNotFoundError("universe file not found: %s" % path)
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def resolve_targets(symbols=(), ciks=(), universe=None, overrides_path=None,
                    client=None):
    """Turn user input into ``[{symbol, cik}]`` (deduped, CIKs unpadded).

    All-digit tokens are treated as CIKs directly; anything else goes through
    ``ticker.cik_for``.  Unresolvable symbols are reported to stderr and
    counted, never fatal (production kept going over bad tickers).
    """
    tokens = []
    if universe:
        tokens.extend(_read_universe(universe))
    tokens.extend(_split_csv(ciks))
    tokens.extend(_split_csv(symbols))

    targets, seen, unresolved = [], set(), 0
    for tok in tokens:
        if tok.isdigit():
            entry = {"symbol": None, "cik": str(int(tok))}
        else:
            try:
                entry = {"symbol": tok.upper(), "cik": str(int(ticker.cik_for(
                    tok, client=client, overrides_path=overrides_path)))}
            except KeyError as exc:
                print("  unresolved ticker %s: %s" % (tok, exc), file=sys.stderr)
                unresolved += 1
                continue
        num = int(entry["cik"])
        if num in seen:
            continue
        seen.add(num)
        targets.append(entry)
    return targets, unresolved


# --------------------------------------------------------------------- CLI
def add_arguments(parser):
    parser.add_argument("symbols", nargs="*",
                        help="tickers or raw CIKs (e.g. AAPL 1018724)")
    parser.add_argument("--cik", action="append", default=[], metavar="CIK",
                        help="CIK(s), comma-separated or repeated")
    parser.add_argument("--universe", metavar="FILE",
                        help="file with one symbol per line (# comments ok)")
    parser.add_argument("--forms", action="append", default=[], metavar="FORMS",
                        help="form types, comma-separated or repeated "
                             "(e.g. 10-K,10-Q,8-K); exact match by default")
    parser.add_argument("--include-amendments", action="store_true",
                        help="treat 10-K/A as a match for a 10-K request "
                             "(production semantics are exact-listing)")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="filing-date window start (inclusive)")
    parser.add_argument("--until", metavar="YYYY-MM-DD",
                        help="filing-date window end (inclusive, default today)")
    parser.add_argument("--docs", choices=["primary", "full", "both", "none"],
                        default="primary",
                        help="what to download: primary document, full "
                             "submission archive ({accession}.txt), both, or "
                             "none (inventory only)")
    parser.add_argument("--scan", action="store_true",
                        help="discover filers from the quarterly full indexes "
                             "(master.idz/.idx) instead of given tickers; "
                             "requires --forms and --since; given tickers then "
                             "act as a restriction")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="concurrent companies (default %(default)s; all "
                             "HTTP still shares the one client throttle)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-attempt accessions listed in failed.txt "
                             "(production skips them on re-runs)")
    parser.add_argument("--include-historical", action="store_true",
                        help="also pull the submissions history files listed "
                             "under filings.files (production only read the "
                             "'recent' block)")
    parser.add_argument("--overrides", metavar="FILE",
                        help="JSON ticker->CIK overrides (default env "
                             "SEC_EDGAR_OVERRIDES)")
    return parser


# --------------------------------------------------------------------- run
def run(args):
    forms = [f.upper() for f in _split_csv(args.forms)]
    try:
        since = date.fromisoformat(args.since) if args.since else None
        until = date.fromisoformat(args.until) if args.until else date.today()
    except ValueError as exc:
        print("error: bad date (%s) — use YYYY-MM-DD" % exc, file=sys.stderr)
        return 2
    if since and since > until:
        print("error: --since must be on or before --until", file=sys.stderr)
        return 2
    if args.scan:
        if not forms:
            print("error: --scan requires --forms (the full-index scan filters "
                  "by form)", file=sys.stderr)
            return 2
        if not since:
            print("error: --scan requires --since (quarter window start)",
                  file=sys.stderr)
            return 2

    client = default_client()
    try:
        targets, unresolved = resolve_targets(
            symbols=args.symbols, ciks=args.cik, universe=args.universe,
            overrides_path=args.overrides, client=client)
    except FileNotFoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.scan:
        discovered = scan_full_index(client, quarters_between(since, until),
                                     forms)
        known = {t["cik"]: t["symbol"] for t in targets}
        if known:  # tickers given alongside --scan restrict the scan result
            targets = [{"symbol": known.get(c), "cik": str(int(c))}
                       for c in sorted(discovered, key=int) if c in known]
        else:
            targets = [{"symbol": None, "cik": str(int(c))}
                       for c in sorted(discovered, key=int)]
    elif not targets:
        print("error: give tickers/CIKs/--universe, or use --scan",
              file=sys.stderr)
        return 2

    root = storage.out_root(COMMAND)
    docs_root = storage.ensure_dir(root / "docs")
    ckpt = storage.Checkpoint(root / "checkpoint")
    writer = storage.JsonlWriter(root / "manifest.jsonl", key="accession")
    state = storage.State(root / "state.json", default={"runs": 0})
    lock = threading.Lock()
    done = ckpt.done()
    failed = ckpt.failed()
    stats = {"companies": 0, "errors": 0, "filings": 0, "skipped": 0,
             "ok": 0, "failed": 0, "unresolved": unresolved}
    since_s = since.isoformat() if since else None
    until_s = until.isoformat() if until else None
    t0 = time.time()

    def fetch_blocks(cik):
        url = SUBMISSIONS_URL.format(cik10=ticker.cik10(cik))
        d = client.get_json(url, max_age_hours=24)
        blocks = [d.get("filings", {}).get("recent") or {}]
        for entry in (d.get("filings", {}).get("files") or []):
            if not args.include_historical:
                break  # production only ever looked at the "recent" block
            name = entry.get("url") or entry.get("name") or ""
            if not name:
                continue
            if name.startswith("http"):
                hist_url = name
            elif name.startswith("/"):
                hist_url = "https://www.sec.gov" + name
            else:
                hist_url = HISTORICAL_BASE + name
            try:  # history files are immutable once published
                blocks.append(client.get_json(hist_url, max_age_hours=24 * 7))
            except Exception as exc:
                print("  %s: history file failed (%s)" % (cik, exc),
                      file=sys.stderr)
        return d.get("name", ""), blocks

    def process(target):
        cik, symbol = target["cik"], target["symbol"]
        try:
            company, blocks = fetch_blocks(cik)
        except Exception as exc:
            with lock:
                stats["errors"] += 1
                print("  %s: submissions failed: %s" % (cik, exc),
                      file=sys.stderr)
            return
        rows = select_rows(blocks, forms=forms, since=since_s, until=until_s,
                           include_amendments=args.include_amendments)
        with lock:
            stats["companies"] += 1
        for row in rows:
            acc = row["accession"]
            key = "%s_%s" % (cik, acc)  # production checkpoint key
            with lock:
                if key in done or (key in failed and not args.retry_failed):
                    stats["skipped"] += 1
                    continue
                stats["filings"] += 1
                if args.retry_failed:
                    failed.discard(key)
            plans = plan_docs(cik, row, args.docs)
            doc_paths, ok, reason = {}, True, ""
            for kind, url, name in plans:
                local_dir = Path(docs_root) / str(int(cik)) / acc.replace("-", "")
                local = local_dir / name
                if local.exists():  # cheap resume even across checkpoint loss
                    doc_paths[kind] = os.path.relpath(local, root)
                    continue
                try:
                    data = client.get_bytes(url)  # immutable -> cached forever
                    if not is_valid_doc_body(kind, data):
                        raise ValueError("body too small (%d B) — SEC error page?"
                                         % len(data))
                    storage.save_doc(local_dir, name, data)
                    doc_paths[kind] = os.path.relpath(local, root)
                except Exception as exc:
                    ok = False
                    reason = str(exc)[:200]
            mrow = dict(row)
            mrow.update(
                symbol=symbol, cik=str(int(cik)), company=company,
                archive_url=ticker.archive_url(cik, acc),
                docs=doc_paths,
                status=("inventory" if args.docs == "none"
                        else ("ok" if ok else "failed")))
            with lock:
                writer.write(mrow)
                if plans:
                    if ok:
                        done.add(key)
                        ckpt.mark_done(key)
                        stats["ok"] += 1
                    else:
                        failed.add(key)
                        ckpt.mark_failed(key)
                        stats["failed"] += 1
                        print("  %s %s %s: download failed: %s"
                              % (cik, row["form"], acc, reason), file=sys.stderr)
            with lock:
                if stats["filings"] % 50 == 0:
                    el = time.time() - t0
                    print("  %d filings processed (%d ok, %d failed, "
                          "%d skipped) in %.1fs"
                          % (stats["filings"], stats["ok"], stats["failed"],
                             stats["skipped"], el), file=sys.stderr)

    workers = max(1, args.workers)
    if workers == 1 or len(targets) == 1:
        for target in targets:
            process(target)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(process, t) for t in targets]
            for _ in as_completed(futures):
                pass

    writer.close()
    state.set("runs", state.get("runs", 0) + 1)
    state.set("last_run", {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window": [since_s, until_s], "forms": forms, "docs": args.docs,
        "scan": bool(args.scan), "companies": stats["companies"],
        "filings": stats["filings"], "ok": stats["ok"],
        "failed": stats["failed"], "skipped": stats["skipped"],
        "unresolved": stats["unresolved"],
    })
    el = time.time() - t0
    print("filings: %d companies, %d filings (%d ok, %d failed, %d skipped, "
          "%d unresolved tickers) in %.1fs -> %s"
          % (stats["companies"], stats["filings"], stats["ok"],
             stats["failed"], stats["skipped"], stats["unresolved"], el, root),
          file=sys.stderr)
    if stats["failed"] or stats["errors"] or stats["unresolved"]:
        return 1
    return 0
