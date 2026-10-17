"""secdata enforcement — SEC Enforcement / Litigation Releases scraper
(component 10).

Upstream endpoints (preserved verbatim from the production
``sec_enforcement_scraper.py`` v2 — it is the sole version of this
component):

- listing, paginated: ``https://www.sec.gov/enforcement-litigation/litigation-releases?page=N``
  (page 0 probed first to learn ``total_pages`` from the pagination links;
  when a page carries none the production default of **118** pages is kept);
- detail: ``https://www.sec.gov/enforcement-litigation/litigation-releases/lr-{N}``.

(The ``/news/enforcement-releases`` path sometimes cited for this feed
404s; production scrapes the litigation-releases listing above, so that is
what is ported.)

Production behaviours kept:
- row fields exactly as the SQLite ``enforcement_actions`` table:
  ``source="sec_litigation"``, ``source_id="LR-{n}"``, ``title`` =
  ``respondents`` = listing anchor text, ``action_type=
  "litigation_release"``, ``action_date`` from the row's ``datetime="…"``
  attribute (None when absent), ``url``, ``content_hash`` =
  ``sha256(description + source_id)[:16]`` computed at *insert* time (the
  detail fill never recomputes it — dedup keys stay stable),
  ``has_detail``, ``created_at``.  ``raw_json`` is dropped: the JSONL row
  *is* the JSON.  ``companies`` stays empty exactly as in production.
- **INSERT OR IGNORE dedupe** -> ``JsonlWriter``-style skip of existing
  ``source_id`` (equivalent because the listing-stage ``description`` is
  empty, so the unique content hash is a pure function of ``source_id``).
- **resume cursor** ``scraper_state`` -> ``checkpoint/state.json`` with the
  same columns (last_page / last_lr_num / total_scraped / detail_fetched /
  updated_at).  A completed sweep leaves ``last_page`` past the end; the
  cron-friendly port wraps back to page 0 on the next run (client cache +
  source_id dedupe make the re-scan cheap) instead of idling forever.
- **phase 2 window**: entries without details, ordered by LR number
  *descending* (newest first), capped by ``--max-details`` (production
  ``LIMIT 3000``); pacing sleep of 0.15 s per detail kept on top of the
  shared client throttle; 0.3 s between listing pages as in production.
- **listing regexes**: production's three flat ``re.findall`` patterns
  (``lr-(\\d+)`` / ``datetime="…"`` / anchor text) positional-zip **is kept
  verbatim as the fallback**, but live pages now repeat each LR number
  (respondent anchor + "Release No." span + attached-file anchors), which
  misaligns that zip — so the primary parse walks the table ``<tr>`` rows
  and takes the first ``lr-{N}`` *link* (file anchors like ``lr-N.pdf``
  never match the link pattern) plus the row's datetime per row.
- **detail text chain** verbatim: ``<article>`` -> ``<main>`` -> whole page;
  strip ``<script>``/``<style>``, tags -> space, collapse whitespace, cap at
  10000 chars.  (Pure regex — no bs4 needed, matching production.)

Storage replaces production's ``sec_enforcement.db`` (never shipped):
``out/enforcement/releases.jsonl`` (same row schema, atomic rewrite) and
``out/enforcement/docs/LR-{n}.txt`` full texts, with
``checkpoint/done.txt|failed.txt`` resume — the same incremental semantics:
details already fetched are never re-fetched, failed ones are skipped unless
``--retry-failed``, and a killed run resumes from its cursor.

Dropped per the porting rules: SQLite WAL DB, personal User-Agent (the
foundation client requires ``SEC_EDGAR_UA``), and print-to-stdout logging
(progress goes to stderr).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from .client import default_client

COMMAND = "enforcement"
HELP = "SEC enforcement / litigation releases: scrape listing + full texts."

LISTING_URL = ("https://www.sec.gov/enforcement-litigation/"
               "litigation-releases?page={}")
DETAIL_URL = ("https://www.sec.gov/enforcement-litigation/"
              "litigation-releases/lr-{}")
LISTING_BASE = ("https://www.sec.gov/enforcement-litigation/"
                "litigation-releases")

TOTAL_PAGES_FALLBACK = 118   # production default when a page shows no links
DETAIL_TEXT_CAP = 10000      # production [:10000] clamp on detail text
MAX_DETAILS_DEFAULT = 3000   # production phase-2 "LIMIT 3000"
LISTING_MAX_AGE_HOURS = 6    # listing pages change; details are immutable
LISTING_PAGE_SLEEP = 0.3     # production pacing between listing pages
DETAIL_SLEEP = 0.15          # production pacing between detail fetches

LR_RE = re.compile(r"lr-(\d+)", re.I)
LR_INT_RE = re.compile(r"^LR-(\d+)$")


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _now_utc() -> str:
    # sqlite datetime('now') format, kept so rows look like production's
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def lr_number(source_id) -> int:
    m = LR_INT_RE.match(str(source_id))
    return int(m.group(1)) if m else 0


# ------------------------------------------------------------------ pure parse
def parse_total_pages(html: str) -> int:
    """Production: max page number in the pagination links, else 118.

    Pages scraped are 0..this value (``range(start, total_pages + 1)``),
    exactly as the canonical script counted them.
    """
    pgs = re.findall(r"page=(\d+)", html or "")
    return max(int(p) for p in pgs) if pgs else TOTAL_PAGES_FALLBACK


_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
# live rows quote hrefs with single quotes; file anchors ("lr-N.pdf") can
# never match because a quote must directly follow the digits
_ROW_LR_ANCHOR_RE = re.compile(
    r"<a[^>]*?href=['\"][^'\"]*?/litigation-releases/lr-(\d+)(?:\?[^'\"]*?)?['\"]"
    r"[^>]*>([^<]+)</a>", re.I)
_ROW_DATE_RE = re.compile(r'datetime="([^"]+)"', re.I)


def parse_list_rows(html: str):
    """Row-aligned primary parse: one entry per ``<tr>`` holding an
    ``lr-{N}`` *link* (anchor regexes on live pages also hit "Release No."
    spans and ``lr-N.pdf`` file links, which the canonical flat zip counted
    twice); first datetime attr per row.
    """
    entries = []
    for chunk in _ROW_RE.findall(html or ""):
        m = _ROW_LR_ANCHOR_RE.search(chunk)
        if not m:
            continue
        dm = _ROW_DATE_RE.search(chunk)
        entries.append(_entry(m.group(1), m.group(2).strip(),
                              dm.group(1) if dm else None))
    return entries


def parse_list_page_classic(html: str):
    """Canonical v2 parse kept verbatim for older/newer page shapes:
    three flat findalls, positional zip, fields as production built them."""
    lr_nums = LR_RE.findall(html)
    dates = re.findall(r'datetime="([^"]+)"', html)
    names = re.findall(r'<a[^>]*lr-\d+[^>]*>([^<]+)</a>', html, re.I)
    entries = []
    for i, lr in enumerate(lr_nums):
        entries.append(_entry(lr,
                              names[i].strip() if i < len(names) else "",
                              dates[i] if i < len(dates) else None))
    return entries


def _entry(lr, name, action_date) -> dict:
    return {
        "source": "sec_litigation", "source_id": "LR-" + str(lr),
        "title": name, "respondents": name,
        "action_date": action_date,
        "action_type": "litigation_release",
        "url": LISTING_BASE + "/lr-" + str(lr),
    }


def parse_list_page(html: str):
    """Row parse first; canonical positional-zip fallback when a page has
    no ``<tr>``-shaped rows (older layout)."""
    entries = parse_list_rows(html)
    if entries:
        return entries
    return parse_list_page_classic(html)


_DETAIL_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.I)
_DETAIL_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.I)


def extract_detail(html: str) -> str:
    """Canonical ``scrape_detail`` body extraction, verbatim: article ->
    main -> whole page; drop script/style; tags to spaces; collapse; cap."""
    body = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
    if not body:
        body = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL)
    text = body.group(1) if body else html
    text = _DETAIL_SCRIPT_RE.sub('', text)
    text = _DETAIL_STYLE_RE.sub('', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:DETAIL_TEXT_CAP]


def content_hash(description, source_id) -> str:
    """Production hash: sha256(description + source_id)[:16], computed at
    insert time only (detail fills never recompute it)."""
    payload = (description or "") + (source_id or "")
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_row(entry, created_at=None) -> dict:
    """Insert-stage row, column order of the production table minus
    id/raw_json; ``companies`` empty exactly as production left it."""
    return {
        "source": entry["source"],
        "source_id": entry["source_id"],
        "title": entry.get("title", ""),
        "respondents": entry.get("respondents", ""),
        "companies": entry.get("companies", ""),
        "action_type": entry["action_type"],
        "action_date": entry.get("action_date"),
        "description": entry.get("description", ""),
        "url": entry["url"],
        "content_hash": content_hash(entry.get("description", ""),
                                     entry["source_id"]),
        "has_detail": 0,
        "created_at": created_at or _now_utc(),
    }


# ------------------------------------------------------------------ file store
def load_rows(path: Path) -> "OrderedDict":
    rows = OrderedDict()
    for r in storage.read_jsonl(path):
        sid = r.get("source_id")
        if sid and sid not in rows:
            rows[sid] = r
    return rows


def flush_rows(path: Path, rows) -> None:
    """Atomic full rewrite — the JSONL stand-in for row-level UPDATEs."""
    tmp = path.with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows.values():
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    json_path = Path(tmp)
    json_path.replace(path)


def pending_details(rows, max_details, failed=None, skip_failed=True):
    """Production phase-2 selection: has_detail=0, LR descending, LIMIT."""
    failed = failed or set()
    pend = [sid for sid, r in rows.items()
            if not r.get("has_detail")
            and not (skip_failed and sid in failed)]
    pend.sort(key=lr_number, reverse=True)
    return pend[:max_details]


# ------------------------------------------------------------------------ run
def run(args) -> int:
    client = default_client()
    root = storage.out_root(COMMAND)
    rel_path = root / "releases.jsonl"
    ckpt_dir = root / "checkpoint"
    docs_dir = root / "docs"
    ck = storage.Checkpoint(ckpt_dir)
    state = storage.State(ckpt_dir / "state.json", {
        "source": "sec_litigation", "last_page": 0, "last_lr_num": 0,
        "total_scraped": 0, "detail_fetched": 0, "updated_at": None})
    if args.restart:
        state.data["last_page"] = 0
    rows = load_rows(rel_path)
    dry = args.dry_run

    def flush():
        if not dry:
            flush_rows(rel_path, rows)

    # ══ phase 1: listing sweep (production default, no flags) ═════════════
    if not args.details_only:
        probe = client.get_text(LISTING_URL.format(0),
                                max_age_hours=LISTING_MAX_AGE_HOURS)
        total_pages = parse_total_pages(probe)
        start = int(state.get("last_page", 0) or 0)
        if start > total_pages:
            start = 0          # wrap: re-scan newest pages on every cron tick
        total = int(state.get("total_scraped", 0) or 0)
        _progress("[{}] Phase 1: pages {}..{} ({} row(s) stored)".format(
            _now_utc(), start, total_pages, len(rows)))
        new_ct = 0
        for page in range(start, total_pages + 1):
            html = probe if page == 0 else client.get_text(
                LISTING_URL.format(page),
                max_age_hours=LISTING_MAX_AGE_HOURS)
            entries = parse_list_page(html)
            for e in entries:
                if e["source_id"] not in rows:      # INSERT OR IGNORE
                    rows[e["source_id"]] = make_row(e)
                    new_ct += 1
            total += len(entries)
            last_lr = (lr_number(entries[-1]["source_id"])
                       if entries else 0)
            state.data.update(last_page=page + 1, last_lr_num=last_lr,
                              total_scraped=total, updated_at=_now_utc())
            if not dry:
                state.save()
            if page % 10 == 0 or page == total_pages:
                _progress("  Page {}/{}: {} rows | total={}".format(
                    page, total_pages, len(entries), total))
            if page % 20 == 0:
                flush()
            if page < total_pages:
                time.sleep(LISTING_PAGE_SLEEP)
        flush()
        if not dry and new_ct:
            _progress("  phase 1: {} new releases stored".format(new_ct))

    # ══ phase 2: detail fills (newest first, capped) ═══════════════════════
    if args.details or args.details_only:
        done = ck.done()
        pend = pending_details(rows, args.max_details,
                               failed=ck.failed(),
                               skip_failed=not args.retry_failed)
        if not pend:
            _progress("[{}] Phase 2: No pending details".format(_now_utc()))
            return 0
        _progress("[{}] Phase 2: Fetching details for {} entries".format(
            _now_utc(), len(pend)))
        fetched = 0
        for sid in pend:
            doc = docs_dir / (sid + ".txt")
            if sid in done and doc.exists():
                # resume: text already on disk (crash between save and flush)
                text = doc.read_text(encoding="utf-8", errors="replace")
                rows[sid]["description"] = text
                rows[sid]["has_detail"] = 1
                fetched += 1
                continue
            try:
                html = client.get_text(
                    DETAIL_URL.format(lr_number(sid)),
                    max_age_hours=None)     # releases are immutable
                text = extract_detail(html)
            except Exception as e:
                _progress("  detail {} failed: {}".format(sid, e))
                if not dry:
                    ck.mark_failed(sid)
                time.sleep(DETAIL_SLEEP)
                continue
            if not text:                    # production `if detail:` guard
                if not dry:
                    ck.mark_failed(sid)
                time.sleep(DETAIL_SLEEP)
                continue
            if not dry:
                storage.save_doc(docs_dir, sid + ".txt",
                                 text.encode("utf-8", "replace"))
                ck.mark_done(sid)
            rows[sid]["description"] = text
            rows[sid]["has_detail"] = 1
            fetched += 1
            time.sleep(DETAIL_SLEEP)
            if fetched % 100 == 0:
                flush()
                _progress("  Details: {}/{}".format(fetched, len(pend)))
        if not dry:
            state.set("detail_fetched",
                      int(state.get("detail_fetched", 0) or 0) + fetched)
        flush()
        _progress("[{}] Phase 2 done: {} details".format(_now_utc(), fetched))

    flush()
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--details", action="store_true",
                        help="after the listing sweep, fetch detail texts "
                             "(production --full)")
    parser.add_argument("--details-only", action="store_true",
                        help="skip the listing sweep, only fetch details "
                             "(production --detail)")
    parser.add_argument("--max-details", type=int, default=MAX_DETAILS_DEFAULT,
                        help="detail-fetch cap per run, newest LR first "
                             "(production LIMIT {})".format(
                                 MAX_DETAILS_DEFAULT))
    parser.add_argument("--restart", action="store_true",
                        help="reset the listing page cursor and re-scan from "
                             "page 0 (deduped, cached — cheap)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-attempt detail pages previously recorded as "
                             "failed (default: skip them)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and parse only: write no files")
