"""secdata preipo — S-1 / F-1 pre-IPO registration monitor (component 9).

Upstream endpoints (preserved from the production ``preipo_monitor.py`` v2
that runs in a daily server cron, plus its older desktop lineage):

- ``https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{n}/company.idx``
  — quarterly whole-market index; the first-run bulk scan seeds the tracker
  (production default: from 2024 Q1 through the current quarter);
- ``https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/{MM}/{DD}/index.json``
  — per-day filing index, scanned over ``--days`` to discover brand-new S-1
  filers between quarterly index uploads (soft-fails on 403/404);
- ``https://efts.sec.gov/LATEST/search-index`` — EFTS full-text search,
  production ``search_efts_s1`` pagination kept
  (``q=<form>&forms=<form>&dateRange=custom&startdt&enddt&from&size=100``);
- ``https://data.sec.gov/submissions/CIK{cik10}.json`` — per-company index:
  S-1 series rows, primary document, IPO/ticker enrichment and the
  company state/industry metadata (``stateOfIncorporation``, ``sic`` /
  ``sicDescription``, ``entityType``) parsed the way the older
  ``sec_ipo_registration_monitor.py`` manifest did;
- ``https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dash}/{doc}``
  — prospectus primary document, only with ``--download-docs`` (archive paths
  use the *unpadded* CIK, via ``ticker.archive_url``);
- ``https://www.sec.gov/files/company_tickers.json`` — the "already listed?"
  universe (replaces the production MySQL ``market_universe`` table read).

Forms tracked, exactly like production: ``S-1, S-1/A, F-1, F-1/A``
(override with ``--forms``).  Confidential draft reports (DRS) never appear
in EDGAR indexes and are deliberately not in the tracked set.

Production behaviours kept on purpose:
- **Phase order** of the cron script: first-run bulk index seed -> discovery
  -> per-tracked-company incremental check -> IPO sweep -> prospectus
  downloads.  Tracker keys are ``{cik}_{form}`` (production shape); an S-1/A
  appearing for a tracked CIK enters as a *new* key, and a changed filing
  date on an existing key is an *amendment* that resets ``downloaded`` — and,
  like the older ``preipo_s1_monitor.py`` (but not v2), also refreshes the
  accession/primary-doc so the re-download fetches the new version.
- **Listed-company exclusion**: v2 filtered candidate names against the
  listed universe and *disabled* the coarse INC/CORP suffix heuristic as too
  aggressive — kept disabled.  New filings from listed-named issuers are
  still inventoried but not announced (v2 gated only the alert list).
- **IPO signals**: v2 confirmed IPOs by "assigned ticker present in the
  listed universe"; the older desktop scripts also treated first public-report
  forms (10-K/10-Q/8-K/20-F/40-F/ARS/DEF 14A/10-B, plus 6-K for FPIs) as
  listing confirmation — both signals are kept.  Withdrawal/rejection forms
  from ``fetch_prospectus.py`` (POS AM, MBW, …) are preserved as a row-level
  ``status`` annotation.
- **Fund/SPAC/ETF classification** (v2 ``categorize`` regex) kept verbatim.
- **Tiny-body guard**: production only accepted a downloaded prospectus with
  >500 chars — SEC serves small generic error pages under HTTP 200.
- **Empty ``primaryDocument``**: resolved via the accession ``index.json``
  (older monitor's ``likely_primary_from_index`` edge case).
- **Dedup by accession** for the archive; resume bookkeeping keeps the
  production ``tracked_filings.json`` v2 shape (with its version migration).

Dropped from production per the porting rules: MySQL ``company_filings``
upserts and its content-hash dedup (JSONL + accession dedupe instead), the
``=== ALERT_DATA ===`` stdout block (now ``out/preipo/new.jsonl``), log
files outside ``--out``, hardcoded personal UA/hosts, and the SEC
comment-letter cross-module import (``scrape_correspondence``) which has no
open-source counterpart.  The canonical cron script has **no watch loop** —
there is no ``--watch`` mode; cron users run it once per tick
(``secdata preipo --days 3``).

Output:
- ``out/preipo/new.jsonl``  — this run's events (new_filing / amendment /
  ipo_detected), the stand-in for production's alert block;
- ``out/preipo/all.jsonl``  — archive of every registration ever seen,
  deduped by accession;
- ``out/preipo/docs/{cik}/{accession_no_dash}/…`` — primary document raw
  bytes plus a ``.txt`` text extract (``--download-docs`` only);
- ``out/preipo/checkpoint/state.json`` — the v2 ``tracked_filings.json``
  shape + ``last_run``; ``done.txt`` / ``failed.txt`` — document resume.

Threads: ``--workers`` (default 4; production used 4–8) fans submissions and
document fetches out, but every request still passes the single shared
``default_client()`` throttle — raise ``SEC_EDGAR_SLEEP`` with worker count
to stay under SEC's 10 req/s.  Quarterly ``company.idx`` files are ~50 MB
each; the client caches them on disk, so re-runs are free.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from . import storage, ticker
from .client import default_client

COMMAND = "preipo"
HELP = "S-1/F-1 pre-IPO registration monitor: discover, track, detect IPOs."

# Production S1_FORMS exactly (v2 cron).
S1_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A"}

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
FULL_INDEX_URL = ("https://www.sec.gov/Archives/edgar/full-index/"
                  "{year}/QTR{q}/company.idx")
DAILY_INDEX_URL = ("https://www.sec.gov/Archives/edgar/daily-index/"
                   "{yyyy}/{mm}/{dd}/index.json")
TICKERS_URL = ticker.BRIDGE_TICKERS_URL

# Index/metadata cache ages; documents are immutable (client caches forever).
FULL_INDEX_HOURS = 24 * 7
DAILY_INDEX_HOURS = 3
EFTS_HOURS = 3
SUBMISSIONS_HOURS = 24

EFTS_PAGE_SIZE = 100       # production paged EFTS in 100s
EFTS_MAX_RESULTS = 5000    # production cap
# v2 accepted a downloaded prospectus only when len(text) > 500 (SEC error
# pages are served under HTTP 200 and are tiny); its DB content guard was 200.
MIN_DOC_TEXT = 500
MIN_TEXT_EXTRACT = 200
DEFAULT_WORKERS = 4
BULK_START_YEAR = 2024     # production "2024Q1 .. today" default seed window

ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Older desktop lineage: first public report == listing confirmation
# (check_ipo_status set + 6-K from sec_ipo_registration_monitor).
PUBLIC_REPORT_FORMS = {"10-K", "10-Q", "8-K", "20-F", "40-F", "6-K",
                       "ARS", "DEF 14A", "10-B"}
# fetch_prospectus.py classify_filing_status reject signals, kept verbatim
# (including its case-insensitive substring fallback).
REJECT_SIGNALS = {"POS AM", "NT CEASED", "WITHDRAWN", "REJECTED",
                  "ABANDONED", "MBW", "NO-AFEE"}

# v2 categorize — SPACs/ETFs/trusts are noise for pre-IPO watchers.
_FUND_PATTERN = re.compile(
    r"\bETF\b|\bTrust\b|\bAcquisition\b|\bCapital Corp\b|\b Fund\b",
    re.IGNORECASE,
)


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def categorize(symbol) -> str:
    """Return 'fund' for SPACs/ETFs/trusts, 'company' otherwise (v2 verbatim)."""
    if symbol and _FUND_PATTERN.search(symbol):
        return "fund"
    return "company"


# ------------------------------------------------------------------ URL helpers
def submissions_url(cik) -> str:
    return "https://data.sec.gov/submissions/CIK{}.json".format(ticker.cik10(cik))


def full_index_url(year, q) -> str:
    return FULL_INDEX_URL.format(year=year, q=q)


def daily_index_url(day) -> str:
    if isinstance(day, str):
        yyyy, mm, dd = day.split("-")
    else:
        yyyy = "{:04d}".format(day.year)
        mm = "{:02d}".format(day.month)
        dd = "{:02d}".format(day.day)
    return DAILY_INDEX_URL.format(yyyy=yyyy, mm=mm, dd=dd)


def efts_url(form, startdt, enddt, offset=0, size=EFTS_PAGE_SIZE) -> str:
    """Production query params in the same order, so cache keys are stable."""
    q = urllib.parse.urlencode([
        ("q", form), ("forms", form), ("dateRange", "custom"),
        ("startdt", startdt), ("enddt", enddt), ("from", offset),
        ("size", size)])
    return EFTS_URL + "?" + q


def quarters_to_date(start_year, end=None):
    """Production quarter list: start_year Q1 .. the current quarter."""
    today = end or date.today()
    quarters = []
    for yr in range(start_year, today.year + 1):
        for q in range(1, 5):
            if yr == today.year and q > ((today.month - 1) // 3 + 1):
                break
            quarters.append((yr, q))
    return quarters


def _cik_int(cik) -> str:
    return str(int(cik))


def is_listed_ticker(name, listed) -> bool:
    """v2 semantics: exact uppercase membership only — the coarse
    INC/CORP/… suffix heuristic was disabled in production as too aggressive."""
    return bool(name) and name.upper() in listed


# ------------------------------------------------------------------ pure parsers
def parse_full_index_line(line: str, forms=S1_FORMS):
    """One ``company.idx`` line -> record or None.

    Production v2 split on whitespace and that is correct for the live
    space-padded layout (``<name…> form cik date path``); the older desktop
    variant sliced fixed columns, kept here only as a fallback for
    differently padded quarters.  Header/metadata lines are skipped.
    """
    line = line.rstrip("\r\n")
    if len(line) < 50:
        return None
    s = line.strip()
    if s.startswith(("Company Name", "Description:", "Last Data Received",
                    "Comments", "Anonymous FTP", "EDGAR")) or \
            s.startswith("-" * 20):
        return None
    parts = line.split()
    form = cik = filing_date = path = company = None
    if len(parts) >= 5 and parts[-3].isdigit() and DATE_RE.match(parts[-2]):
        company = " ".join(parts[:-4])
        form, cik, filing_date, path = parts[-4], parts[-3], parts[-2], parts[-1]
    else:
        # fixed-width fallback (old lineage): name 62 / form / cik / date / path
        try:
            company = line[0:62].strip()
            form = line[62:74].strip()
            cik = line[74:86].strip()
            rest = line[86:]
            m = DATE_RE.search(rest)
            if not (form and cik and cik.isdigit() and m):
                return None
            filing_date = rest[m.start():m.start() + 10]
            path = rest[m.end():].strip()
        except Exception:
            return None
    if form not in forms:
        return None
    am = ACCESSION_RE.search(path or "")
    return {"cik": _cik_int(cik), "company": company, "form": form,
            "date": filing_date, "accession": am.group(1) if am else "",
            "filename": path}


def parse_daily_index(payload: dict, forms=S1_FORMS):
    """Per-date daily-index ``index.json`` -> S-1 rows (defensive on keys —
    the feed has been published in both camelCase and kebab-case shapes)."""
    out = []
    for f in (payload or {}).get("filings", []) or []:
        form = f.get("form") or ""
        if form not in forms:
            continue
        cik = f.get("cik") or f.get("root") or ""
        try:
            cik = _cik_int(cik)
        except (TypeError, ValueError):
            continue
        acc = (f.get("accessionNumber") or f.get("accession-number") or
               f.get("accession") or "")
        when = (f.get("filingDate") or f.get("filing-date") or
                (f.get("acceptanceDateTime") or
                 f.get("acceptance-date-time") or "")[:10])
        name = f.get("name") or f.get("company") or ""
        out.append({"cik": cik, "company": name, "form": form,
                    "date": when, "accession": acc, "filename": ""})
    return out


def company_from_display(display: str) -> str:
    """'ACME Corp (ACME) (CIK 0001234567)' -> 'ACME Corp'.
    v2's cleanup regex plus the older, broader ticker-suffix pattern."""
    company = re.sub(r"\s*\(CIK.*?\)", "", display or "").strip()
    company = re.sub(r"\s*\([A-Z.\-]{1,8}\)\s*$", "", company).strip()
    return company


def parse_efts_hits(payload: dict):
    """EFTS search-index response -> production row dicts
    (CIK without leading zeros, display-name cleanup, adsh kept)."""
    out = []
    for h in (payload or {}).get("hits", {}).get("hits", []) or []:
        src = h.get("_source", {}) or {}
        ciks = src.get("ciks") or []
        if not ciks:
            continue
        try:
            cik = str(ciks[0]).lstrip("0") or str(ciks[0])
        except Exception:
            continue
        display = (src.get("display_names") or [""])[0] or ""
        out.append({"cik": cik, "company": company_from_display(display),
                    "form": src.get("form", ""),
                    "date": src.get("file_date", ""),
                    "accession": src.get("adsh", ""), "filename": ""})
    return out


def parse_submissions(sub: dict, cik, forms=S1_FORMS):
    """Submissions JSON -> production S-1 rows + enrichment metadata
    (ticker/exchanges from v2, sic/state/entityType like the older
    sec_ipo_registration_monitor manifest)."""
    sub = sub or {}
    recent = sub.get("filings", {}).get("recent", {}) or {}
    frms = recent.get("form", []) or []
    n = len(frms)
    s1_rows = []
    for i, form in enumerate(frms):
        if form not in forms:
            continue
        def col(key, idx=i):
            vals = recent.get(key) or []
            return vals[idx] if idx < len(vals) else ""
        s1_rows.append({"cik": _cik_int(cik), "company": sub.get("name", ""),
                        "form": form, "date": col("filingDate"),
                        "accession": col("accessionNumber"),
                        "primary_document": col("primaryDocument")})
    tickers = sub.get("tickers") or []
    ipo_form = next((f for f in frms if f in PUBLIC_REPORT_FORMS), None)
    return {
        "cik": _cik_int(cik),
        "name": sub.get("name", ""),
        "tickers": tickers,
        "ticker": tickers[0] if tickers else None,
        "exchanges": sub.get("exchanges") or [],
        "entity_type": sub.get("entityType", ""),
        "state_of_incorporation": sub.get("stateOfIncorporation", ""),
        "sic": sub.get("sic", ""),
        "sic_description": sub.get("sicDescription", ""),
        "recent_forms": list(frms),
        "s1_rows": s1_rows,
        "status": classify_status(frms),
        "ipo_form": ipo_form,
        "failed": False,
    }


def classify_status(recent_forms) -> str:
    """fetch_prospectus.py status rules kept: listed (a public report is
    present) > rejected (withdrawal/abandonment signals; an empty recent
    block was treated as rejected there too) > preipo."""
    forms = list(recent_forms or [])
    if not forms:
        return "rejected"
    for f in forms:
        if f in PUBLIC_REPORT_FORMS:
            return "listed"
    upper = {f.upper() for f in forms}
    for sig in REJECT_SIGNALS:
        if sig.upper() in upper or any(sig.lower() in f.lower() for f in forms):
            return "rejected"
    return "preipo"


def likely_primary_from_index(items, accession: str) -> str:
    """Older monitor edge case: rows with an empty ``primaryDocument`` —
    pick the first plausible document from the accession ``index.json``.
    The wrapper files (``index.json``, ``<acc>-index.htm``) and the raw
    ``<acc>.txt`` container are never the prospectus."""
    skip = {"index.json", accession + ".txt", ""}
    for item in items or []:
        name = item.get("name") or ""
        if name in skip or name.endswith("-index.htm"):
            continue
        if name.lower().endswith((".htm", ".html", ".xml", ".txt", ".pdf")):
            return name
    return ""


class _TextStripper(HTMLParser):
    """Stdlib stand-in for the old BeautifulSoup text extraction (drop
    script/style/noscript, break on block tags, collapse blank runs)."""

    _SKIP = ("script", "style", "noscript")
    _BREAK = ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "table", "title")

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks).replace("\r", "\n")
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(raw: str) -> str:
    p = _TextStripper()
    p.feed(raw)
    p.close()
    return p.text()


# ------------------------------------------------------------------- IO helpers
def load_listed_tickers(client) -> set:
    """SEC company_tickers.json — the open-source stand-in for production's
    ``market_universe``.  On failure returns an empty set (IPO filtering then
    degrades to public-form signals only, like a production DB outage did)."""
    try:
        raw = client.get_json(TICKERS_URL, max_age_hours=24 * 7)
        return {str(v.get("ticker", "")).upper() for v in raw.values()
                if v.get("ticker")}
    except Exception as e:
        _progress("warning: listed-ticker universe unavailable ({}); "
                  "listed filtering degraded".format(e))
        return set()


def resolve_universe(path, client):
    """DESIGN rule: optional --universe FILE (one symbol per line)."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError("universe file not found: " + str(p))
    syms = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    ciks = set()
    for sym in syms:
        try:
            ciks.add(_cik_int(ticker.cik_for(sym, client=client)))
        except Exception as e:
            _progress("warning: universe symbol {} unresolvable: {}".format(
                sym, e))
    return ciks


def tracked_key(cik, form) -> str:
    return "{}_{}".format(_cik_int(cik), form)


class _SubMemo:
    """One submissions fetch + parse per CIK per run (production re-fetched
    the same URL in every phase; the disk cache made that cheap — the memo
    makes it explicit).  Thread-safe."""

    def __init__(self, client, forms=S1_FORMS):
        self.client = client
        self.forms = forms
        self._memo = {}
        self._lock = threading.Lock()

    def get(self, cik):
        key = _cik_int(cik)
        with self._lock:
            if key in self._memo:
                return self._memo[key]
        parsed = dict({"cik": key, "name": "", "tickers": [], "ticker": None,
                       "exchanges": [], "entity_type": "",
                       "state_of_incorporation": "", "sic": "",
                       "sic_description": "", "recent_forms": [],
                       "s1_rows": [], "status": "unknown", "ipo_form": None},
                      failed=True)
        try:
            sub = self.client.get_json(submissions_url(key),
                                       max_age_hours=SUBMISSIONS_HOURS)
            parsed = parse_submissions(sub, key, self.forms)
        except Exception as e:
            _progress("warning: submissions fetch failed [{}]: {}".format(key, e))
        with self._lock:
            self._memo[key] = parsed
        return parsed


# ------------------------------------------------------------------ archive rows
def row_from_entry(key, entry, source="tracked-check") -> dict:
    """Archive row shape — production company_filings / tracked-entry fields
    flattened for JSONL.  Deduped by accession in out/preipo/all.jsonl."""
    acc = entry.get("accession") or entry.get("adsh") or ""
    if not acc:
        acc = "{}-{}-{}".format(entry.get("cik", ""),
                                entry.get("form", "").replace("/", "-"),
                                entry.get("date", "").replace("-", ""))
    return {
        "accession": acc,
        "key": key,
        "cik": entry.get("cik", ""),
        "company": entry.get("company", ""),
        "form": entry.get("form", ""),
        "filing_date": entry.get("date", entry.get("filing_date", "")),
        "primary_document": entry.get("pdoc", ""),
        "category": categorize(entry.get("company", "")),
        "status": entry.get("status", ""),
        "state_of_incorporation": entry.get("state_of_incorporation", ""),
        "sic": entry.get("sic", ""),
        "sic_description": entry.get("sic_description", ""),
        "entity_type": entry.get("entity_type", ""),
        "tickers": entry.get("tickers", []),
        "exchanges": entry.get("exchanges", []),
        "ipo_detected": bool(entry.get("ipo_detected")),
        "downloaded": bool(entry.get("downloaded")),
        "doc_path": entry.get("doc_path", ""),
        "source_url": entry.get("source_url", ""),
        "first_seen": entry.get("first_seen", ""),
        "source": source,
    }


# --------------------------------------------------------------------- EFTS fetch
def search_efts(client, form, startdt, enddt, max_results=EFTS_MAX_RESULTS):
    """v2 ``search_efts_s1`` pagination kept (size 100, stop on short page).
    Production queried only forms=S-1; the newer lineage queried each tracked
    form separately — we query every form of the tracked set, same params."""
    hits = []
    for offset in range(0, max_results, EFTS_PAGE_SIZE):
        try:
            d = client.get_json(efts_url(form, startdt, enddt, offset),
                                max_age_hours=EFTS_HOURS)
        except Exception as e:
            _progress("warning: EFTS {} failed: {}".format(form, e))
            break
        batch = parse_efts_hits(d)
        hits.extend(batch)
        if len(batch) < EFTS_PAGE_SIZE:
            break
    return hits


# ------------------------------------------------------------------- main phases
def run(args) -> int:
    client = default_client()
    if args.workers < 1:
        _progress("--workers must be >= 1")
        return 2
    forms = set(S1_FORMS)
    if args.forms:
        forms = {f.strip().upper() for f in args.forms.split(",") if f.strip()}

    try:
        universe = resolve_universe(args.universe, client)
    except FileNotFoundError as e:
        _progress(str(e))
        return 2
    if universe is not None:
        _progress("universe loaded: {} CIKs".format(len(universe)))

    root = storage.out_root(COMMAND)
    ckpt_dir = root / "checkpoint"
    state = storage.State(ckpt_dir / "state.json",
                          {"version": 2, "seen": {}, "last_run": None})
    if state.get("version") != 2:
        # production v2 migration: keep seen entries, reset the rest
        _progress("tracked state format old, resetting to v2")
        state.data = {"seen": state.data.get("seen", {}),
                      "last_run": None, "version": 2}
        state.save()
    seen = state.data["seen"]

    listed = load_listed_tickers(client)
    _progress("listed tickers: {} | tracked pre-IPO entries: {}".format(
        len(listed), len(seen)))

    events = []    # -> new.jsonl (this run's alert block)
    rows = []      # -> all.jsonl (archive, deduped by accession)
    dry = args.dry_run
    memo = _SubMemo(client, forms)
    now_iso = datetime.now().isoformat()

    def in_universe(cik):
        return universe is None or _cik_int(cik) in universe

    def add_new(cik, form, info, source, announce=True):
        key = tracked_key(cik, form)
        entry = {"cik": _cik_int(cik), "company": info.get("company", ""),
                 "form": form, "date": info.get("date", ""),
                 "adsh": info.get("accession", info.get("adsh", "")),
                 "pdoc": info.get("primary_document", info.get("pdoc", "")),
                 "filename": info.get("filename", ""),
                 "downloaded": False, "ipo_detected": False,
                 "first_seen": now_iso}
        seen[key] = entry
        if announce:
            events.append({"event": "new_filing", "company": entry["company"],
                           "cik": entry["cik"], "form": form,
                           "file_date": entry["date"],
                           "accession": entry["adsh"], "source": source})
        rows.append(row_from_entry(key, entry, source))
        return "new"

    def seed(cik, form, info, source):
        """Production check_company new/amend semantics for one discovered row."""
        key = tracked_key(cik, form)
        entry = seen.get(key)
        if entry is None:
            return add_new(cik, form, info, source)
        new_date = info.get("date", "")
        if entry.get("date", "") != new_date:
            old = entry.get("date", "")
            entry["date"] = new_date
            entry["downloaded"] = False
            # preipo_s1_monitor.py refreshed adsh/pdoc on amendment (v2 left
            # them stale and re-downloaded the old doc) — kept that fix.
            if info.get("accession"):
                entry["adsh"] = info["accession"]
            if info.get("primary_document"):
                entry["pdoc"] = info["primary_document"]
            events.append({"event": "amendment", "company": entry["company"],
                           "cik": entry["cik"], "form": form,
                           "old_date": old, "new_date": new_date,
                           "accession": entry.get("adsh", "")})
            rows.append(row_from_entry(key, entry, source))
            return "amend"
        return None

    # ══ phase 1: first-run / --bulk quarterly full-index scan ═════════════
    if args.bulk or not seen:
        _progress("bulk scan of full-index from {}Q1 (quarterly company.idx "
                  "files are ~50 MB — cached on disk after the first fetch)"
                  .format(args.bulk_start_year))
        for (yr, q) in quarters_to_date(args.bulk_start_year):
            try:
                text = client.get_text(full_index_url(yr, q),
                                       max_age_hours=FULL_INDEX_HOURS)
            except Exception as e:
                _progress("  {} Q{}: index unavailable ({})".format(yr, q, e))
                continue
            recs = []
            for line in text.splitlines():
                rec = parse_full_index_line(line, forms)
                if rec:
                    recs.append(rec)
            # production dedup: per (cik,form) keep the latest-dated record
            best = {}
            for rec in recs:
                k = (rec["cik"], rec["form"])
                if k not in best or rec["date"] > best[k]["date"]:
                    best[k] = rec
            seeded = 0
            for (cik, form), rec in sorted(best.items()):
                key = tracked_key(cik, form)
                if key in seen:
                    continue
                if is_listed_ticker(rec["company"], listed):
                    continue
                if not in_universe(cik):
                    continue
                # v2 checked each candidate's assigned ticker via submissions
                # before seeding (skip companies that list under our universe)
                sub = memo.get(cik)
                if sub["ticker"] and sub["ticker"].upper() in listed:
                    continue
                add_new(cik, form, rec, "bulk-index")
                _enrich(seen[key], sub)
                seeded += 1
            _progress("  {} Q{}: {} {}-series records kept".format(
                yr, q, seeded, "/".join(sorted(forms))))
        if not dry:
            state.save()

    # ══ phase 2: discovery of new filers (daily-index + EFTS) ═════════════
    if args.days > 0:
        today = date.today()
        if not args.no_daily_index:
            _progress("daily-index scan, {} day(s) back".format(args.days))
            blocked = False
            for back in range(0, args.days + 1):
                day = today - timedelta(days=back)
                if blocked:
                    break
                try:
                    payload = client.get_json(daily_index_url(day),
                                              max_age_hours=DAILY_INDEX_HOURS)
                except urllib.error.HTTPError as e:
                    if e.code == 403:
                        blocked = True
                        _progress("  daily-index blocked (403); relying on "
                                  "EFTS instead")
                        break
                    if e.code != 404:      # 404: non-business day / not yet cut
                        _progress("  {}: {}".format(day, e))
                    continue
                except Exception as e:
                    _progress("  {}: {}".format(day, e))
                    continue
                n = 0
                for rec in parse_daily_index(payload, forms):
                    if seed(rec["cik"], rec["form"], rec, "daily-index"):
                        n += 1
                if n:
                    _progress("  {}: {} S-1-series rows".format(day, n))
        if not args.no_efts:
            start = (today - timedelta(days=args.days)).isoformat()
            end = today.isoformat()
            total = 0
            for form in sorted(forms):
                for rec in search_efts(client, form, start, end):
                    if seed(rec["cik"], rec["form"], rec, "efts"):
                        total += 1
            _progress("EFTS discovery: {} new/amend".format(total))

    # ══ phase 3: incremental check of tracked companies (submissions) ═════
    by_cik = {}
    for key, entry in seen.items():
        if entry.get("ipo_detected"):
            continue
        by_cik.setdefault(_cik_int(entry["cik"]), []).append(key)
    _progress("incremental check: {} tracked companies".format(len(by_cik)))

    def check_cik(cik):
        sub = memo.get(cik)
        for f in sub["s1_rows"]:
            key = tracked_key(cik, f["form"])
            if key not in seen:
                # sibling form (e.g. S-1/A after S-1) never tracked -> new;
                # v2 inventoried listed-named issuers but kept them quiet
                quiet = is_listed_ticker(f["company"], listed)
                add_new(cik, f["form"], f, "submissions", announce=not quiet)
            elif seen[key].get("date", "") != f["date"]:
                seed(cik, f["form"], f, "submissions")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(check_cik, c): c for c in sorted(by_cik)}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                _progress("warning: check failed [{}]: {}".format(
                    futs[fut], e))

    # enrich all tracked entries of every consulted CIK (production stored
    # company name from submissions; state/sic from the older monitor)
    for cik in sorted(by_cik):
        sub = memo.get(cik)
        for key in by_cik[cik]:
            entry = seen.get(key)
            if entry is not None:
                _enrich(entry, sub)

    # ══ phase 4: IPO sweep ════════════════════════════════════════════════
    ipo = []
    if not args.no_ipo_check:
        _progress("IPO check …")

        def check_ipo(cik):
            sub = memo.get(cik)
            tick = sub["ticker"]
            listed_sig = bool(tick) and tick.upper() in listed   # v2 signal
            pub_sig = bool(sub["ipo_form"])                       # desktop signal
            return cik, tick, sub["ipo_form"], (listed_sig or pub_sig)

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(check_ipo, c): c for c in sorted(by_cik)}
            for fut in as_completed(futs):
                try:
                    cik, tick, ipo_form, detected = fut.result()
                except Exception:
                    continue
                if not detected:
                    continue
                for key in by_cik[cik]:
                    entry = seen[key]
                    if entry.get("ipo_detected"):
                        continue
                    entry["ipo_detected"] = True
                    entry["ipo_form"] = ipo_form or "ticker:" + (tick or "")
                    info = {"company": entry["company"], "cik": cik,
                            "ipo_form": entry["ipo_form"], "ticker": tick}
                    ipo.append(info)
                    events.append(dict(event="ipo_detected", **info))
                    rows.append(row_from_entry(key, entry, "ipo-check"))
        for i in ipo:
            _progress("  IPO: {} -> ticker={} ({})".format(
                i["company"], i["ticker"] or "?", i["ipo_form"]))

    # ══ phase 5: optional prospectus document download ════════════════════
    doc_ct = 0
    if args.download_docs:
        ck = storage.Checkpoint(ckpt_dir)
        done_keys = ck.done()
        failed_keys = ck.failed()
        targets = []
        for key, entry in seen.items():
            if entry.get("downloaded") or entry.get("ipo_detected"):
                continue
            if is_listed_ticker(entry.get("company", ""), listed):
                continue
            if not in_universe(entry["cik"]):
                continue
            if key in failed_keys and key not in done_keys:
                continue  # production skipped past failures silently
            targets.append((key, entry))
        _progress("downloading prospectus primary docs for {} entries".format(
            len(targets)))

        def fetch_doc(item):
            key, entry = item
            cik = _cik_int(entry["cik"])
            sub = memo.get(cik)
            row = next((r for r in sub["s1_rows"] if r["form"] == entry["form"]),
                       None)
            adsh = (row or {}).get("accession") or entry.get("adsh", "")
            pdoc = (row or {}).get("primary_document") or entry.get("pdoc", "")
            if not adsh:
                return key, None, "no accession on record"
            if not pdoc:
                # older monitor: resolve primary doc via the accession index
                try:
                    idx = client.get_json(ticker.index_json_url(cik, adsh),
                                          max_age_hours=SUBMISSIONS_HOURS)
                    pdoc = likely_primary_from_index(
                        idx.get("directory", {}).get("item", []), adsh)
                except Exception as e:
                    return key, None, "index.json failed: {}".format(e)
                if not pdoc:
                    return key, None, "no primary document found"
            url = ticker.archive_url(cik, adsh, pdoc)
            try:
                raw = client.get_bytes(url).decode("utf-8", "replace")
            except Exception as e:
                return key, None, "{}: {}".format(url, e)
            if len(raw) < MIN_DOC_TEXT:
                return key, None, "body too small ({} bytes)".format(len(raw))
            return key, (adsh, pdoc, url, raw), None

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(fetch_doc, t): t for t in targets}
            for fut in as_completed(futs):
                try:
                    key, res, err = fut.result()
                except Exception as e:
                    _progress("warning: doc fetch error: {}".format(e))
                    continue
                if err:
                    _progress("  doc skip [{}]: {}".format(key, err))
                    if not dry:
                        ck.mark_failed(key)
                    continue
                adsh, pdoc, url, raw = res
                entry = seen[key]
                entry["adsh"], entry["pdoc"] = adsh, pdoc
                entry["downloaded"] = True
                doc_ct += 1
                _progress("  + {}".format(key))
                if dry:
                    continue
                doc_dir = root / "docs" / entry["cik"] / adsh.replace("-", "")
                saved = storage.save_doc(doc_dir, pdoc,
                                         raw.encode("utf-8", "replace"))
                entry["doc_path"] = str(saved.relative_to(root))
                entry["source_url"] = url
                is_html = pdoc.lower().endswith((".htm", ".html"))
                text = html_to_text(raw) if is_html else raw
                if len(text) >= MIN_TEXT_EXTRACT:
                    storage.save_doc(doc_dir, pdoc + ".txt",
                                     text.encode("utf-8", "replace"))
                ck.mark_done(key + "|" + adsh + "|" + pdoc)
                rows.append(row_from_entry(key, entry, "submissions"))

    # ══ write outputs ═════════════════════════════════════════════════════
    counts = {
        "new": sum(1 for e in events if e["event"] == "new_filing"),
        "amend": sum(1 for e in events if e["event"] == "amendment"),
        "ipo": sum(1 for e in events if e["event"] == "ipo_detected"),
    }
    if not dry:
        with storage.JsonlWriter(root / "new.jsonl") as w:
            w.write_many(events)
        with storage.JsonlWriter(root / "all.jsonl", key="accession") as w:
            w.write_many(rows)
        state.set("last_run", datetime.now().isoformat())

    _progress("=== done | new={} amend={} ipo={} docs={} tracked={} ===".format(
        counts["new"], counts["amend"], counts["ipo"], doc_ct, len(seen)))
    return 0


def _enrich(entry, sub) -> None:
    """Fold submissions metadata into a tracked entry (name / ticker list as
    v2 did; state of incorporation + SIC industry like the older monitor)."""
    if sub.get("failed"):
        return
    if sub.get("name"):
        entry["company"] = sub["name"]
    entry["status"] = sub["status"]
    entry["state_of_incorporation"] = sub.get("state_of_incorporation", "")
    entry["sic"] = str(sub.get("sic", ""))
    entry["sic_description"] = sub.get("sic_description", "")
    entry["entity_type"] = sub.get("entity_type", "")
    entry["tickers"] = list(sub.get("tickers", []))
    entry["exchanges"] = list(sub.get("exchanges", []))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--days", type=int, default=3,
                        help="discovery lookback in days (cron: --days 3; "
                             "0 disables daily-index/EFTS discovery)")
    parser.add_argument("--bulk", action="store_true",
                        help="force the quarterly full-index scan even when "
                             "the tracker already has entries")
    parser.add_argument("--bulk-start-year", type=int, default=BULK_START_YEAR,
                        help="first year of the full-index scan "
                             "(production default {})".format(BULK_START_YEAR))
    parser.add_argument("--forms", default="",
                        help="comma-separated registration forms "
                             "(default: {})".format(",".join(sorted(S1_FORMS))))
    parser.add_argument("--download-docs", action="store_true",
                        help="download new/amended prospectus primary docs "
                             "into docs/ (raw file + .txt text extract)")
    parser.add_argument("--no-efts", action="store_true",
                        help="skip EFTS full-text discovery")
    parser.add_argument("--no-daily-index", action="store_true",
                        help="skip per-date daily-index discovery (e.g. when "
                             "the date-browse paths are IP-blocked)")
    parser.add_argument("--no-ipo-check", action="store_true",
                        help="skip the IPO/listing sweep (phase 4)")
    parser.add_argument("--universe", metavar="FILE",
                        help="restrict tracking to issuers of symbols listed "
                             "in FILE (one per line; absent = no filter)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="concurrent submissions/doc workers (default "
                             "{}); all HTTP still goes through the single "
                             "shared client throttle — raise SEC_EDGAR_SLEEP "
                             "with this".format(DEFAULT_WORKERS))
    parser.add_argument("--dry-run", action="store_true",
                        help="discover/parse only: write no files and update "
                             "no state or checkpoints")
