"""secdata reports — SEC 10-K / 10-Q / 20-F archive + incremental refresh.

Ported from the production pipelines (2026):

- ``sec_10k_10q_incremental.py`` — the MAIN script, daily "index-first" cron
  build since the 2026-08-30 rewrite  ->  default mode (``--days N``)
- ``sec_10k_10q_full_download.py`` — historical full archive -> ``--full``
- ``run_incremental.sh`` / ``sec_10k_10q_watchdog.sh`` — context only: the
  ``PROGRESS``/``Done.`` markers emitted below mirror the log lines the
  production watchdog matched on. The watchdog itself, the /proc pid lock
  and the log-file glue are NOT ported (re-runs are already cheap thanks to
  the URL cache + checkpoints).

Upstream EDGAR endpoints (preserved exactly):

    https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/master.{YYYYMMDD}.idx
    https://data.sec.gov/submissions/CIK{10-digit-zero-padded}.json
    https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{doc}
    https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/index.json
    https://www.sec.gov/files/company_tickers.json   (via secdata.ticker)

Index-first strategy (production rationale: probing ~10k issuers' submissions
JSON one by one took 13 h+ and wedged the daily cron; one small index file per
trading day lists the whole market, so a normal run costs a handful of GETs):

1. fetch the daily master index files for the scan window. A 404 is a
   non-trading day and skipped; yesterday is always included (the index for
   day D is only published the evening of D);
2. keep rows whose form is exactly in {10-K, 10-K/A, 10-Q, 10-Q/A, 20-F,
   20-F/A} (amendments included — the production TARGET_FORMS is explicit);
3. map CIK -> ticker; issuers missing from company_tickers.json (funds,
   delisted, freshly listed) are skipped. A filing is "present" when
   ``out/reports/docs/{TICKER}/{FORM}/{ACCN}/primary-document.html`` exists
   with size > 1 KiB (production threshold);
4. only for still-missing filings, fetch the issuer submissions JSON to learn
   the primary document name (authoritative ``primaryDocument``; fallback:
   first .htm/.html in the accession dir's ``index.json``);
5. download the primary HTML into the docs tree plus a per-filing
   ``meta.json`` (iXBRL cover-page fiscal labels), append a row to
   ``out/reports/manifest.jsonl`` and record ``checkpoint/done.txt`` /
   ``failed.txt`` for resume. ``state.json`` keeps the last-scan watermark
   that converges the default window to 3..7 days (production rule).

Amendment path nesting is preserved as in production: joining the form string
``10-K/A`` into the docs path nests it under ``{TICKER}/10-K/A/{ACCN}/`` —
existence checks and the meta backfill walker are aware of that structure.

Watermark semantics preserved: the watermark advances whenever at least one
daily index was fetched — even for ``--dry-run`` (production quirk: debug dry
runs shared the cron's state file). When zero indexes were fetched the state
is NOT advanced and the run exits 1 so schedulers notice.

Pacing: production used 0.2 s between requests with 6 workers (incremental)
and 0.28 s with 3 workers (full). Here the shared EdgarClient throttle
governs every request: keep ``SEC_EDGAR_SLEEP`` >= 0.2 with the default 6
workers and raise it further when passing ``--workers`` larger — the throttle
(not the worker count) is what stays under SEC's 10 req/s.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from . import storage, ticker
from .client import default_client

COMMAND = "reports"
HELP = "SEC 10-K / 10-Q / 20-F archive: index-first incremental refresh, or --full backfill"

# Production TARGET_FORMS (both scripts) — exact membership, amendments listed.
TARGET_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A"})

INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/{qtr}/master.{ymd}.idx"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

MIN_DOC_BYTES = 1024        # production existence / validity floor
META_HEAD_BYTES = 32768     # production backfill read window for meta parsing
DEFAULT_WORKERS = 6         # production incremental MAX_WORKERS
DEFAULT_WORKERS_FULL = 3    # production full MAX_WORKERS

DOC_NAME = "primary-document.html"

# ---------------------------------------------------------------------- logging
def _log(msg: str) -> None:
    """Progress to stderr with the production timestamped look."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", file=sys.stderr,
          flush=True)


# ----------------------------------------------------------------- pure: index
def qtr_of(ymd: str) -> str:
    """YYYYMMDD -> EDGAR quarterly index folder (QTR1..QTR4)."""
    m = int(ymd[4:6])
    return f"QTR{(m - 1) // 3 + 1}"


def parse_master_idx(body: bytes, forms=TARGET_FORMS) -> list:
    """Parse an EDGAR daily ``master.*.idx`` file.

    Line format: ``CIK|Company Name|Form Type|Date Filed|File Name`` where
    File Name is ``edgar/data/{cik}/{acc_nodash}/{acc}.txt``. Header lines
    start with a letter and are skipped. Returns ``[(cik10, form, acc), ...]``
    filtered to ``forms`` (exact membership, production behaviour), deduped by
    accession.
    """
    out, seen = [], set()
    try:
        text = body.decode("latin-1")
    except Exception:
        return out
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line[0].isalpha():
            continue  # header lines
        parts = line.split("|")
        if len(parts) < 5:
            continue
        cik, form, fname = parts[0], parts[2], parts[4]
        if form not in forms:
            continue
        acc = fname.rsplit("/", 1)[-1]
        if acc.endswith(".txt"):
            acc = acc[:-4]
        if not acc or acc in seen:
            continue
        seen.add(acc)
        out.append((cik.zfill(10), form, acc))
    return out


def select_missing(parsed: list, cik_to_ticker: dict, is_have, seen=None) -> list:
    """Pure missing-detector behind the daily scan.

    ``parsed``: rows from :func:`parse_master_idx`; ``is_have(ticker, form,
    acc)`` answers local existence. Rows whose CIK is not in the ticker map
    are skipped (funds / delisted / freshly listed — production behaviour).
    ``seen`` is an optional set of accessions shared across days so one
    filing listed twice is only queued once. Returns ``[(ticker, cik10,
    form, acc), ...]`` preserving input order.
    """
    out = []
    if seen is None:
        seen = set()
    for cik, form, acc in parsed:
        sym = cik_to_ticker.get(cik)
        if sym is None:
            continue
        if acc in seen:
            continue
        if is_have(sym, form, acc):
            continue
        seen.add(acc)
        out.append((sym, cik, form, acc))
    return out


def reverse_cik_map(ticker_to_cik: dict) -> dict:
    """{TICKER: cik10} -> {cik10: TICKER}, symbols sorted so a CIK shared by
    several tickers (GOOG/GOOGL) resolves deterministically to the last
    symbol — the production "latter wins" limitation made reproducible."""
    out = {}
    for sym in sorted(ticker_to_cik):
        out[ticker_to_cik[sym]] = sym
    return out


def default_window_days(last_scan, today=None) -> int:
    """Converge the default scan window from the watermark (production rule):
    ``max(3, min(days_since_last + 1, 7))``; no/broken watermark -> 5."""
    today = today or date.today()
    if last_scan:
        try:
            last = date.fromisoformat(str(last_scan)[:10])
        except ValueError:
            return 5
        return max(3, min((today - last).days + 1, 7))
    return 5


# ------------------------------------------------------------- pure: submissions
def filings_from_submissions(data: dict, forms=TARGET_FORMS, after=None) -> list:
    """Flatten the ``recent`` block of a submissions JSON to rows for ``forms``
    (exact membership, production filter), requiring both accession and
    primaryDocument (production skipped empty-doc rows; the dir-index fallback
    in resolve_primary only exists on the index-first path). ``after`` is an
    optional ISO date cutoff; rows with an empty filingDate pass (production).
    Only the ``recent`` block is consulted — both production scripts ignore
    the historical files arrays (known ceiling, preserved).
    """
    rec = (data or {}).get("filings", {}).get("recent", {}) or {}
    f_list = rec.get("form") or []
    accs = rec.get("accessionNumber") or []
    docs = rec.get("primaryDocument") or []
    dates = rec.get("filingDate") or []
    out = []
    for i, form in enumerate(f_list):
        if form not in forms:
            continue
        acc = accs[i] if i < len(accs) else ""
        doc = docs[i] if i < len(docs) else ""
        dt = dates[i] if i < len(dates) else ""
        if not acc or not doc:
            continue
        if after and dt and dt < after:
            continue
        out.append({"form": form, "accession": acc, "doc": doc, "date": dt})
    return out


def primary_from_submissions(data: dict, accession: str):
    """Authoritative primary-document name for one accession from a
    submissions JSON's ``recent`` block (production resolve_primary)."""
    rec = (data or {}).get("filings", {}).get("recent", {}) or {}
    accs = rec.get("accessionNumber") or []
    docs = rec.get("primaryDocument") or []
    for i, a in enumerate(accs):
        if a == accession and i < len(docs) and docs[i]:
            return docs[i]
    return None


def primary_from_dir_index(data: dict):
    """Fallback: first .htm/.html entry in an accession dir's index.json
    (production resolve_primary fallback)."""
    items = ((data or {}).get("directory", {}) or {}).get("item", []) or []
    for it in items:
        name = str(it.get("name", ""))
        if name.lower().endswith((".htm", ".html")):
            return name
    return None


# ---------------------------------------------------------------- pure: meta
_META_RE_TITLE = re.compile(r"<title>\s*[^<]*?[_\s-](\d{8})", re.IGNORECASE)
_META_RE_PERIOD = re.compile(r"DocumentPeriodEndDate[^>]*>\s*(\d{4}-\d{2}-\d{2})")
_META_RE_FY = re.compile(r"DocumentFiscalYearFocus[^>]*>\s*(\d{4})")
_META_RE_FQ = re.compile(r"DocumentFiscalPeriodFocus[^>]*>\s*(FY|Q[1-4])")


def extract_meta(html: bytes):
    """Fiscal metadata from the filing HTML cover page (production regexes,
    verbatim): period_end (``<title>{TICKER}-{YYYYMMDD}`` first, falling back
    to dei:DocumentPeriodEndDate), fiscal_year/fiscal_quarter from
    dei:DocumentFiscalYearFocus/PeriodFocus, plus the dropdown ``label``.
    10-Ks usually carry FY. Pass bytes; downloads hold the full doc in memory,
    the backfill walker passes a head slice. Returns None when nothing parsed.
    """
    try:
        head = html.decode("utf-8", errors="replace")
    except Exception:
        head = ""
    meta = {}
    t = _META_RE_TITLE.search(head)
    if t:
        p = t.group(1)
        meta["period_end"] = f"{p[:4]}-{p[4:6]}-{p[6:]}"
    if not meta.get("period_end"):
        pe = _META_RE_PERIOD.search(head)
        if pe:
            meta["period_end"] = pe.group(1)
    fy = _META_RE_FY.search(head)
    if fy:
        meta["fiscal_year"] = int(fy.group(1))
    fq = _META_RE_FQ.search(head)
    if fq:
        meta["fiscal_quarter"] = fq.group(1)
    if meta.get("fiscal_year") and meta.get("fiscal_quarter"):
        if meta["fiscal_quarter"] == "FY":
            meta["label"] = f"FY{meta['fiscal_year']} Annual"
        else:
            meta['label'] = f"FY{meta['fiscal_year']} {meta['fiscal_quarter']}"
        if meta.get("period_end"):
            meta["label"] += f" · {meta['period_end']}"
    elif meta.get("period_end"):
        meta["label"] = meta["period_end"]
    return meta or None


# ------------------------------------------------------------------ misc pure
def parse_forms(spec: str) -> frozenset:
    if not spec:
        return TARGET_FORMS
    return frozenset(x.strip().upper() for x in spec.split(",") if x.strip())


def read_universe(path) -> set:
    """--universe FILE: one symbol per line, blank lines and #-comments
    ignored, symbols normalised (BRK.B -> BRK-B, how SEC stores them)."""
    out = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.add(ticker.normalize_symbol(s.upper()))
    if not out:
        raise ValueError(f"universe file {path} contains no symbols")
    return out


def lookup_symbol(t2c: dict, symbol: str):
    sym = str(symbol).upper().strip()
    if sym in t2c:
        return sym
    alt = ticker.normalize_symbol(sym)
    return alt if alt in t2c else None


def _key(ticker_, cik, form, acc) -> str:
    """Checkpoint key; '|' safe (no field contains it)."""
    return "|".join((ticker_, cik, form, acc))


# ---------------------------------------------------------------- filesystem
def local_has(docs_root, sym: str, form: str, acc: str) -> bool:
    """Production existence rule: primary-document.html present and > 1 KiB
    (tiny stubs SEC serves for broken paths are re-downloaded)."""
    f = Path(docs_root) / sym / form / acc / DOC_NAME
    try:
        return f.exists() and f.stat().st_size > MIN_DOC_BYTES
    except OSError:
        return False


def scan_local_ticker(docs_root, sym: str) -> set:
    """{(form, accession)} present for one ticker. os.walk handles the
    amendment nesting ({TICKER}/10-K/A/{ACC}) production paths use. The
    > 1 KiB floor of production local_filings_index is preserved: stubs
    below it do not count as present and get re-downloaded."""
    base = Path(docs_root) / sym
    out = set()
    if not base.exists():
        return out
    for dirpath, _dn, filenames in os.walk(base):
        if DOC_NAME not in filenames:
            continue
        acc_dir = Path(dirpath)
        rel = acc_dir.parent.relative_to(base)
        if not rel.parts:
            continue  # not our layout (file sitting on the ticker dir)
        form = "/".join(rel.parts)   # "/" canonical (Windows-safe nesting)
        try:
            if acc_dir.joinpath(DOC_NAME).stat().st_size <= MIN_DOC_BYTES:
                continue
        except OSError:
            continue
        out.add((form, acc_dir.name))
    return out


# ------------------------------------------------------------------- fetching
def resolve_primary(client, cik: str, accession: str):
    """Primary document filename for one missing filing: submissions JSON
    (authoritative), falling back to the accession dir index.json. Cached:
    submissions are mutable -> 24 h TTL; dir listings are immutable."""
    try:
        data = client.get_json(SUBMISSIONS_URL.format(cik=ticker.cik10(cik)),
                               max_age_hours=24)
        doc = primary_from_submissions(data, accession)
        if doc:
            return doc
    except Exception:
        pass
    try:
        data = client.get_json(ticker.archive_url(cik, accession, "index.json"))
        return primary_from_dir_index(data)
    except Exception:
        return None


def download_one(client, docs_root, item) -> dict:
    """Fetch one primary document. Preserves production guards: exists > 1 KiB
    -> skip; response body < 1 KiB -> treated as failure (retried later)."""
    sym, cik, form, acc, doc = item
    out_dir = Path(docs_root) / sym / form / acc
    out_f = out_dir / DOC_NAME
    url = ticker.archive_url(cik, acc, doc)
    if out_f.exists() and out_f.stat().st_size > MIN_DOC_BYTES:
        return {"status": "skipped", "url": url}
    try:
        body = client.get_bytes(url)
    except Exception as e:
        _log(f"  [{sym}/{form}/{acc}] download failed: {str(e)[:200]}")
        return {"status": "failed", "url": url}
    if not body or len(body) < MIN_DOC_BYTES:
        _log(f"  [{sym}/{form}/{acc}] too small: {len(body) if body else 0} bytes")
        return {"status": "failed", "url": url}
    storage.save_doc(out_dir, DOC_NAME, body)
    meta = extract_meta(body) or {}
    meta.update({"accession": acc, "form": form, "unparsed": not bool(meta)})
    try:
        (out_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _log(f"  [{sym}/{form}/{acc}] meta write failed: {e}")
    return {"status": "ok", "url": url, "bytes": len(body), "meta": meta}


def manifest_row(item, res: dict) -> dict:
    sym, cik, form, acc, doc = item
    row = {
        "accession": acc,
        "ticker": sym,
        "cik": cik,
        "form": form,
        "primary_doc": doc,
        "path": "/".join((sym, form, acc, DOC_NAME)),
        "url": res.get("url", ""),
        "bytes": res.get("bytes", 0),
        "status": "downloaded",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    meta = res.get("meta") or {}
    for k in ("period_end", "fiscal_year", "fiscal_quarter", "label"):
        if k in meta:
            row[k] = meta[k]
    return row


def download_phase(client, docs_root, items, workers, manifest, ckpt,
                   done_keys, log) -> tuple:
    """Parallel download with one PROGRESS line per item (the marker the
    production watchdog watched). Manifest/checkpoint writes stay on the main
    thread (JsonlWriter is not thread-safe); the single client throttle
    governs every worker."""
    total = len(items)
    if not items:
        log("Nothing to download.")
        return 0, 0, 0
    ok = fail = skip = done = 0
    done_keys = done_keys if done_keys is not None else ckpt.done()

    def _do(item):
        key = _key(item[0], item[1], item[2], item[3])
        if key in done_keys:
            return item, {"status": "skipped", "reason": "checkpoint"}
        return item, download_one(client, docs_root, item)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = [ex.submit(_do, it) for it in items]
        for fut in as_completed(futures):
            try:
                item, res = fut.result()
            except Exception as e:
                _log(f"  worker raised: {e}")
                fail += 1
                done += 1
                log(f"PROGRESS downloaded={ok} fail={fail} skip={skip} "
                    f"done={done}/{total}")
                continue
            key = _key(item[0], item[1], item[2], item[3])
            status = res.get("status")
            if status == "ok":
                ok += 1
                ckpt.mark_done(key)
                done_keys.add(key)
                manifest.write(manifest_row(item, res))
            elif status == "skipped":
                skip += 1
                if res.get("reason") != "checkpoint":
                    ckpt.mark_done(key)
                    done_keys.add(key)
            else:
                fail += 1
                ckpt.mark_failed(key)
            done += 1
            log(f"PROGRESS downloaded={ok} fail={fail} skip={skip} "
                f"done={done}/{total}")
    log("=" * 60)
    log(f"Done. ok={ok} fail={fail} skip_dup={skip} total={total}")
    log("=" * 60)
    return ok, fail, skip


# ------------------------------------------------------------------- engines
def scan_days(client, docs_root, days, cik_to_ticker, forms, log):
    """Index-first detection over the last `days` calendar days.
    Returns (missing, fetched_any, failures)."""
    end = date.today() - timedelta(days=1)   # yesterday is always in scope
    start = end - timedelta(days=days - 1)
    missing = []
    fetched_any = False
    failures = 0
    seen = set()
    for i in range(days):
        d = start + timedelta(days=i)
        ymd = d.strftime("%Y%m%d")
        url = INDEX_URL.format(year=d.strftime("%Y"), qtr=qtr_of(ymd), ymd=ymd)
        try:
            body = client.get_bytes(url)  # immutable once published -> forever
            fetched_any = True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log(f"PROGRESS index {ymd} 404 (non-trading day), skip")
                continue
            log(f"  index {ymd} fetch failed status={e.code}")
            failures += 1
            continue
        except Exception as e:
            log(f"  index {ymd} fetch failed: {str(e)[:200]}")
            failures += 1
            continue
        new = select_missing(parse_master_idx(body, forms), cik_to_ticker,
                             lambda t, f, a: local_has(docs_root, t, f, a), seen)
        missing.extend(new)
        log(f"PROGRESS index {ymd} parsed, cumulative missing={len(missing)}")
    return missing, fetched_any, failures


def collect_submissions_items(client, docs_root, syms, forms, after, workers,
                              log, verbose=False) -> list:
    """Submissions engine listing pass: diff each issuer's recent filings
    against the local tree. Two-phase (list everything, then download) so
    --limit and the global queue report work; production --full interleaved
    the two per ticker, which behaves identically thanks to the checkpoint."""
    items = []
    total = len(syms)
    done = 0
    pending = 0

    def _list(sym_cik):
        sym, cik = sym_cik
        try:
            data = client.get_json(SUBMISSIONS_URL.format(cik=cik),
                                   max_age_hours=24)
        except Exception as e:
            log(f"[{sym}] submissions fetch failed: {str(e)[:200]}")
            return None
        rows = filings_from_submissions(data, forms, after)
        have = scan_local_ticker(docs_root, sym)
        miss = [(sym, cik, r["form"], r["accession"], r["doc"]) for r in rows
                if (r["form"], r["accession"]) not in have]
        return sym, rows, miss

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = [ex.submit(_list, s) for s in syms]
        for fut in as_completed(futures):
            done += 1
            try:
                r = fut.result()
            except Exception as e:
                log(f"  worker raised: {e}")
                continue
            if r is not None:
                sym, rows, miss = r
                pending += len(miss)
                if verbose:
                    log(f"[{sym}] recent filings={len(rows)} missing={len(miss)}")
                    for m in miss:
                        log(f"  missing {m[2]} {m[3]} -> {m[4]}")
                items.extend(miss)
            if done % 50 == 0 or done == total:
                log(f"PROGRESS listed tickers={done}/{total} "
                    f"pending_downloads={pending}")
    return items


def run_backfill_meta(docs_root, log) -> int:
    """One-time repair pass (production --backfill-meta): regenerate meta.json
    for downloaded filings lacking complete fiscal labels. Meta that already
    has fiscal_year + fiscal_quarter is skipped; an unreadable meta.json is
    treated as done (production quirk: avoids endless retries)."""
    log("Backfill meta.json for existing filings (missing only)...")
    total = written = unparsed = 0
    root = Path(docs_root)
    if not root.exists():
        log("No docs directory yet - nothing to backfill.")
        return 0
    for ticker_dir in sorted(p for p in root.iterdir()
                             if p.is_dir() and not p.name.startswith(".")):
        for dirpath, _dn, filenames in os.walk(ticker_dir):
            if DOC_NAME not in filenames:
                continue
            acc_dir = Path(dirpath)
            rel = acc_dir.parent.relative_to(ticker_dir)
            if not rel.parts:
                continue
            form = "/".join(rel.parts)   # "/" canonical (Windows-safe nesting)
            acc = acc_dir.name
            meta_f = acc_dir / "meta.json"
            if meta_f.exists():
                try:
                    prev = json.loads(meta_f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if prev.get("fiscal_year") and prev.get("fiscal_quarter"):
                    continue
            total += 1
            try:
                with open(acc_dir / DOC_NAME, "rb") as fh:
                    head = fh.read(META_HEAD_BYTES)
                meta = extract_meta(head) or {}
                meta.update({"accession": acc, "form": form,
                             "unparsed": not bool(meta)})
                meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
                written += 1
                if meta["unparsed"]:
                    unparsed += 1
            except Exception as e:
                log(f"  backfill {acc_dir} failed: {e}")
            if total % 20000 == 0:
                log(f"PROGRESS backfill scanned={total} written={written}")
    log(f"Backfill done: scanned={total} written={written} unparsed={unparsed}")
    return 0


def run_index_engine(args, client, root, docs, t2c, forms, workers, ckpt,
                     manifest, log) -> int:
    state = storage.State(root / "state.json")
    days = args.days if args.days and args.days > 0 \
        else default_window_days(state.get("last_scan_date"))
    log(f"Scan window: {days} calendar day(s) "
        f"(watermark: {state.get('last_scan_date') or 'none'})")
    missing, fetched_any, failures = scan_days(
        client, docs, days, reverse_cik_map(t2c), forms, log)
    log(f"Phase 1 done: {len(missing)} missing filings detected.")

    if fetched_any:
        state.set("last_scan_date", date.today().isoformat())
        log(f"State saved: last_scan_date = {date.today().isoformat()}")
    else:
        log("No daily index fetched - scan failed. State NOT advanced.")

    # resume: re-queue earlier failures that are still missing on disk
    done_keys = ckpt.done()
    seen_acc = {m[3] for m in missing}
    extra = 0
    for line in sorted(ckpt.failed()):
        parts = line.split("|")
        if len(parts) != 4:
            continue
        sym, cik, form, acc = parts
        if acc in seen_acc or line in done_keys:
            continue
        if local_has(docs, sym, form, acc):
            continue
        seen_acc.add(acc)
        missing.append((sym, cik, form, acc))
        extra += 1
    if extra:
        log(f"Resume: re-queued {extra} previously failed filings.")

    if not missing:
        log("Nothing to download.")
        return 0 if fetched_any else 1

    if args.dry_run:
        log(f"DRY-RUN: would download {len(missing)} filings:")
        for sym, cik, form, acc in missing[:50]:
            log(f"  {sym} {form} {acc} (CIK {cik})")
        if len(missing) > 50:
            log(f"  ... and {len(missing) - 50} more")
        return 0 if fetched_any else 1

    items = []
    for sym, cik, form, acc in missing:
        doc = resolve_primary(client, cik, acc)
        if doc:
            items.append((sym, cik, form, acc, doc))
        else:
            log(f"  [{sym}/{form}/{acc}] could not resolve primary doc, "
                "skip this run")
    log(f"Resolved {len(items)}/{len(missing)} primary docs; "
        f"downloading ({workers} workers)...")
    if args.limit and 0 < args.limit < len(items):
        log(f"--limit {args.limit}: cutting queue from {len(items)}")
        items = items[:args.limit]
    download_phase(client, docs, items, workers, manifest, ckpt, done_keys, log)
    return 0 if fetched_any else 1


def finish_submissions(args, client, docs, items, workers, ckpt, manifest,
                       log) -> int:
    log(f"Phase 1 done: {len(items)} missing filings detected.")
    if not items:
        log("Nothing to download.")
        return 0
    if args.dry_run:
        log(f"DRY-RUN: would download {len(items)} filings:")
        for it in items[:50]:
            log(f"  {it[0]} {it[2]} {it[3]} -> {it[4]}")
        if len(items) > 50:
            log(f"  ... and {len(items) - 50} more")
        return 0
    if args.limit and 0 < args.limit < len(items):
        log(f"--limit {args.limit}: cutting queue from {len(items)}")
        items = items[:args.limit]
    log(f"Downloading {len(items)} filings ({workers} workers)...")
    download_phase(client, docs, items, workers, manifest, ckpt,
                   ckpt.done(), log)
    return 0


# ------------------------------------------------------------------ CLI glue
def add_arguments(parser) -> None:
    parser.description = (
        "SEC 10-K / 10-Q / 20-F HTML archive. Default: index-first scan of "
        "EDGAR daily master indexes (minutes). --full: historical backfill "
        "through the Submissions API. Downloads land in out/reports/docs/ "
        "with a manifest.jsonl, meta.json per filing, checkpoints and a "
        "scan watermark.")
    parser.add_argument("--full", action="store_true",
                        help="historical backfill of every ticker via the "
                             "Submissions API instead of the daily-index scan")
    parser.add_argument("--days", type=int, default=0,
                        help="incremental mode: scan back N calendar days of "
                             "the daily index (0 = watermark-converged 3..7)")
    parser.add_argument("--ticker", default="",
                        help="single symbol through the Submissions API "
                             "(production debug mode); skips --days")
    parser.add_argument("--universe", default="",
                        help="file with one symbol per line (# comments ok); "
                             "absent = every ticker in company_tickers.json")
    parser.add_argument("--after", default="",
                        help="YYYY-MM-DD filing-date cutoff (Submissions "
                             "engine: --full / --ticker)")
    parser.add_argument("--forms", default="",
                        help="comma-separated form set (default: 10-K,10-K/A,"
                             "10-Q,10-Q/A,20-F,20-F/A)")
    parser.add_argument("--overrides", default="",
                        help="JSON {TICKER: CIK} file for symbols "
                             "company_tickers.json omits (see also "
                             "SEC_EDGAR_OVERRIDES)")
    parser.add_argument("--workers", type=int, default=0,
                        help="download threads; 0 = production defaults "
                             "(6 incremental / 3 full). Raise SEC_EDGAR_SLEEP "
                             "proportionally when increasing this")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap documents downloaded this run (0 = no cap; "
                             "production's --limit was ignored, this one works)")
    parser.add_argument("--dry-run", action="store_true",
                        help="detect and report missing filings, download nothing")
    parser.add_argument("--backfill-meta", action="store_true",
                        help="one-time: regenerate meta.json for filings "
                             "already present under out/reports/docs")


def run(args) -> int:
    t0 = time.time()
    log = _log
    log("=" * 60)
    log("secdata reports - SEC 10-K / 10-Q / 20-F archive")
    log("=" * 60)

    forms = parse_forms(getattr(args, "forms", "") or "")
    after = (getattr(args, "after", "") or "").strip()
    if after and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", after):
        log(f"error: --after must be YYYY-MM-DD, got {after!r}")
        return 2
    after = after or None
    if getattr(args, "days", 0) < 0 or getattr(args, "workers", 0) < 0 \
            or getattr(args, "limit", 0) < 0:
        log("error: --days/--workers/--limit must be >= 0")
        return 2
    full = bool(getattr(args, "full", False))
    workers = args.workers or (DEFAULT_WORKERS_FULL if full else DEFAULT_WORKERS)

    client = default_client()  # constructed after cli.py applied env knobs
    root = storage.out_root(COMMAND)
    docs = storage.ensure_dir(root / "docs")

    if getattr(args, "backfill_meta", False):
        return run_backfill_meta(docs, log)

    try:
        tm = ticker.ticker_map(client)
    except Exception as e:
        log(f"error: no ticker<->CIK map available: {e}")
        return 1
    ov = ticker.overrides(getattr(args, "overrides", "") or None)
    t2c = {}
    for sym, cik in list(tm.items()) + list(ov.items()):
        try:
            c10 = ticker.cik10(cik)
        except (TypeError, ValueError):
            continue
        t2c[str(sym).upper()] = c10

    uni_path = getattr(args, "universe", "") or ""
    if uni_path:
        try:
            universe_norm = read_universe(uni_path)
        except (OSError, ValueError) as e:
            log(f"error: --universe: {e}")
            return 2
        t2c = {s: c for s, c in t2c.items()
               if s in universe_norm or ticker.normalize_symbol(s) in universe_norm}
        log(f"Universe: {len(universe_norm)} symbols, {len(t2c)} matched "
            "in the ticker map")

    ckpt = storage.Checkpoint(root / "checkpoint")
    manifest = storage.JsonlWriter(root / "manifest.jsonl", key="accession")
    try:
        tspec = (getattr(args, "ticker", "") or "").strip()
        if tspec:
            sym = lookup_symbol(t2c, tspec)
            if sym is None:
                log(f"error: {tspec.upper()} not in the ticker map - typo, "
                    "delisted, or missing from --overrides?")
                return 2
            log(f"Single-ticker mode: {sym} (CIK {t2c[sym]}) via Submissions API")
            items = collect_submissions_items(client, docs, [(sym, t2c[sym])],
                                              forms, after, workers, log,
                                              verbose=True)
            rc = finish_submissions(args, client, docs, items, workers,
                                    ckpt, manifest, log)
        elif full:
            if not t2c:
                log("error: no tickers to process (empty universe?)")
                return 1
            log(f"Full archive mode: {len(t2c)} tickers via Submissions API")
            items = collect_submissions_items(client, docs,
                                              sorted(t2c.items()), forms,
                                              after, workers, log,
                                              verbose=False)
            rc = finish_submissions(args, client, docs, items, workers,
                                    ckpt, manifest, log)
        else:
            rc = run_index_engine(args, client, root, docs, t2c, forms,
                                  workers, ckpt, manifest, log)
    finally:
        manifest.close()
    log(f"Total elapsed: {(time.time() - t0) / 60:.1f} min")
    return rc
