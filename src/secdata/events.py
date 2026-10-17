"""8-K download + event indexing (pipeline #7).

Ported from the production 8-K pipeline (2026):

- ``download_8k_single_stock.py`` — the maintained single-company 8-K crawler:
  discovery by CIK, submission .txt + attachments, per-filing ``_meta.json``
  with the production event-row fields (accession, cik, company_name,
  form_type, filing_date, archive_path, status, files_downloaded, total_bytes,
  error, downloaded_at), accession-level checkpoint resume, downloaded/partial/
  failed statuses, SEC-filing content verification markers, 403/429 cooldown.
- ``download_8k.py`` — the old EFTS bulk discovery (kept here as ``--source
  efts``: monthly chunks, ``from``/``size`` pagination capped at the 10000-hit
  Elasticsearch window).
- ``sec_8k_full_db.py`` — all-market full-index patterns: quarter iteration
  (never future quarters), master.idx gzip handling, forms ``8-K,8-K/A``,
  status vocabulary.
- event indexing per the porting brief: Item numbers resolved from three
  channels, in priority order — (1) the submissions JSON ``items`` column,
  (2) the INDEX-HEAD ``ITEMS:`` line inside the complete submission .txt,
  (3) ``Item X.YY`` headings in the primary 8-K document body (press-release
  exhibits are linked from the accession ``index.json``).

Upstream endpoints (preserved exactly):

    https://data.sec.gov/submissions/CIK{cik10}.json          (recent + filings.files history)
    https://data.sec.gov/submissions/{history-file-name}      (older filings chunks)
    https://www.sec.gov/Archives/edgar/full-index/{Y}/QTR{Q}/master.idx
    https://efts.sec.gov/LATEST/search-index?q=Item&forms=8-K&dateRange=custom&startdt=&enddt=&from=&size=100
    https://www.sec.gov/Archives/edgar/data/{cik}/{acc17}/index.json
    https://www.sec.gov/Archives/edgar/data/{cik}/{acc17}/{accession}.txt

Outputs (under ``out/events/``):

    docs/{cik10}/{acc17}/   complete submission .txt + primary doc + press-release
                            exhibits + raw index.json + _meta.json
    events.jsonl            one row per (accession, item) event
    filings.jsonl           discovered-filing index (also written by --discover-only)
    checkpoint/done.txt|failed.txt

Pacing: all HTTP goes through the shared ``default_client()`` throttle
(``SEC_EDGAR_SLEEP``, default 0.15 s ≈ 7 req/s — production ran 9.8 req/s, the
ceiling being SEC's 10/s; reproduce with ``SEC_EDGAR_SLEEP=0.102`` or
``--rate 9.8``). ``--workers`` parallelism is governed by that one global
throttle, so raising workers without lowering SEC_EDGAR_SLEEP does not raise
the request rate. Idempotent re-runs are cheap: client disk cache + file-exists
skips + checkpoint + JSONL dedupe on event_key.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

from . import ticker
from .client import default_client
from .storage import Checkpoint, JsonlWriter, ensure_dir, out_root, save_doc

COMMAND = "events"
HELP = "8-K download + item-code event indexing (events.jsonl)"

# ---------------------------------------------------------------- production constants
SEC_FULL_INDEX = "https://www.sec.gov/Archives/edgar/full-index"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
DEFAULT_FORMS = "8-K,8-K/A"           # production target_forms
DEFAULT_START_YEAR = 1993             # 8-K era begins; production default
MAX_SEC_RATE = 10.0                   # SEC fair-access ceiling, production clamp
EFTS_PAGE_SIZE = 100                  # production size param
EFTS_MAX_HITS = 10000                 # Elasticsearch window limit, production cap
# Production verified the submission .txt really is an SEC filing before saving:
SEC_MARKERS = (b"<SEC-DOCUMENT>", b"SEC-DOCUMENT", b"<SEC-HEADER>",
               b"SEC-HEADER", b"ACCESSION NUMBER:", b"CONFORMED SUBMISSION TYPE:")
# Production preview heuristic skipped these when picking a readable exhibit:
INDEX_HTML_SUFFIXES = ("-index.html", "-index-headers.html",
                       "-index.htm", "-index-headers.htm")
IMG_SUFFIXES = (".gif", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".xbrl")
NON_DOC_SUFFIXES = (".xml", ".xsd", ".pdf.jpg", ".htm.jpg")
# Name hints that mark an attachment as an earnings/press-release exhibit.
PRESS_HINTS = ("ex-99", "ex99", "press", "release", "earnings", "er99",
               "q1", "q2", "q3", "q4")

STOP = threading.Event()

ITEM_NAMES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety Violation Reporting",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "2.07": "Trading Halt",
    "3.01": "Notice of Delisting or Failure to Satisfy Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "4.11": "Statement by Directors or Officers",  # legacy
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Officers; Elections; Appointments; "
            "Compensatory Arrangements",
    "5.03": "Amendments to Articles/Bylaws; Change in Fiscal Year",
    "5.04": "Removal of Directors",
    "6.01": "Absence of a Director's Report on an Asset-Backed Issuer",
    "6.02": "Material or Adverse Event or Change in Servicing Activity",
    "6.03": "Departure of Principal Manager, Trustee or Securities Provider",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

_ITEM_TOKEN_RE = re.compile(r"\d{1,2}\.\d{2}")
_BODY_ITEM_RE = re.compile(r"\bitem\s+(\d{1,2}\.\d{2})\b", re.IGNORECASE)
_ITEMS_LINE_RE = re.compile(r"^ITEMS:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
_ITEMS_TAG_RE = re.compile(r"<ITEMS>(.*?)</ITEMS>", re.IGNORECASE | re.DOTALL)


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def utc_now():
    # production format: ISO-8601 UTC, second precision
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename(value, max_len=180):
    """Sanitize a string for use as a filename (production helper)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    cleaned = cleaned.strip("._")
    return (cleaned or "unknown")[:max_len]


def current_quarter():
    return ((date.today().month - 1) // 3) + 1


def iter_quarters(start_year, end_year, quarters_filter=None):
    """Yield (year, quarter) pairs that already finished/are current —
    production refused future quarters."""
    today = date.today()
    for year in range(start_year, end_year + 1):
        for quarter in range(1, 5):
            if quarters_filter and quarter not in quarters_filter:
                continue
            if year > today.year or (year == today.year and quarter > current_quarter()):
                continue
            yield year, quarter


# ---------------------------------------------------------------- item parsing
def parse_items_field(value):
    """Channel 1: the submissions JSON ``items`` column, e.g. '2.02,7.01'."""
    return _ITEM_TOKEN_RE.findall(str(value or ""))


def parse_index_head_items(text):
    """Channel 2: INDEX-HEAD of the complete submission .txt — the
    ``ITEMS: 2.02 7.01`` line (or <ITEMS> tag) inside <SEC-HEADER>, before the
    first <DOCUMENT> section."""
    header = re.split(r"<DOCUMENT", str(text or ""), maxsplit=1, flags=re.IGNORECASE)[0]
    found = []
    m = _ITEMS_LINE_RE.search(header)
    if m:
        found.extend(_ITEM_TOKEN_RE.findall(m.group(1)))
    for tag in _ITEMS_TAG_RE.findall(header):
        found.extend(_ITEM_TOKEN_RE.findall(tag))
    return found


def strip_tags(html):
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", str(html or ""))
    html = re.sub(r"(?s)<[^>]*>", " ", html)
    return html_mod.unescape(html)


def parse_body_item_headings(html):
    """Channel 3: 'Item X.YY' headings in the 8-K primary document body."""
    return _BODY_ITEM_RE.findall(strip_tags(html))


def collect_items(items_raw, index_head_items, body_items):
    """Merge the three channels; one entry per item, first channel wins.

    Returns a list of (item, source) tuples. ``source`` records which channel
    produced the item — useful for debugging parser regressions."""
    ordered = (("submissions", parse_items_field(items_raw)),
               ("index_head", list(index_head_items or [])),
               ("body", list(body_items or [])))
    seen = {}
    for source, items in ordered:
        for it in items:
            seen.setdefault(it, source)
    return sorted((item, src) for item, src in seen.items())


# ---------------------------------------------------------------- discovery
def filings_from_submissions(sub_json, forms, after=None, before=None):
    """8-K rows from a submissions JSON — recent block plus the
    ``filings.files`` history chunks' rows (already fetched separately).
    Preserves the production ``items`` column and the 8-K/8-K/A membership
    test (amendments counted, unlike v2's strict '8-K' match)."""
    forms_set = {f.upper() for f in forms}
    blocks = [sub_json.get("filings", {}).get("recent", {})]
    for blk in sub_json.get("_history_blocks", []):
        blocks.append(blk)
    out = []
    seen = set()
    name = sub_json.get("name", "")
    for blk in blocks:
        accs = blk.get("accessionNumber", [])
        for i, acc in enumerate(accs):
            form = str((blk.get("form") or [""] * len(accs))[i])
            if form.upper() not in forms_set:
                continue
            fdate = str((blk.get("filingDate") or [""] * len(accs))[i])
            if after and fdate < after:
                continue
            if before and fdate > before:
                continue
            if acc in seen:
                continue
            seen.add(acc)
            out.append({
                "accession": acc,
                "cik": str(sub_json.get("cik", "")),
                "company_name": name,
                "form_type": form,
                "filing_date": fdate,
                "report_date": str((blk.get("reportDate") or [""] * len(accs))[i]),
                "acceptance_date": str((blk.get("acceptanceDateTime") or [""] * len(accs))[i]),
                "primary_doc": str((blk.get("primaryDocument") or [""] * len(accs))[i]),
                "items_raw": str((blk.get("items") or [""] * len(accs))[i]),
                "archive_path": "",
                "source": "submissions",
            })
    out.sort(key=lambda f: (f["filing_date"], f["accession"]))
    return out


def parse_master_idx_lines(lines, target_forms, cik_padded):
    """Pure core of production ``discover_filings``: pipe-delimited
    CIK|Company|Form|Date|File, latin-1, skip header rows, exact form
    membership, accession = filename minus .txt, dedup."""
    forms_set = {f.upper() for f in target_forms}
    out, seen = [], set()
    for raw_line in lines:
        line = str(raw_line).rstrip("\n")
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) != 5 or parts[0] == "CIK":
            continue
        idx_cik, company_name, form_type, filing_date, archive_path = [p.strip() for p in parts]
        if form_type.upper() not in forms_set:
            continue
        if idx_cik.zfill(10) != cik_padded:
            continue
        accession = archive_path.rsplit("/", 1)[-1].replace(".txt", "")
        if not accession or accession in seen:
            continue
        seen.add(accession)
        out.append({
            "accession": accession,
            "cik": idx_cik,
            "company_name": company_name,
            "form_type": form_type,
            "filing_date": filing_date,
            "report_date": "",
            "acceptance_date": "",
            "primary_doc": "",
            "items_raw": "",
            "archive_path": archive_path,
            "source": "idx",
        })
    out.sort(key=lambda f: (f["filing_date"], f["accession"]))
    return out


def efts_hits_to_filings(hits, forms):
    """Pure core of old ``download_8k.py``: EFTS hit _source → filing dicts.
    Fields preserved: adsh, ciks[0], display_names[0] (strip ' (File ...)'),
    file_date."""
    forms_set = {f.upper() for f in forms}
    out = []
    for hit in hits:
        src = hit.get("_source", {})
        form = str(src.get("root_form") or src.get("form") or "8-K")
        if form.upper() not in forms_set:
            continue
        adsh = src.get("adsh", "")
        if not adsh:
            continue
        cik = str(int(src.get("ciks", ["0"])[0]))
        name = str(src.get("display_names", ["Unknown"])[0]).split(" (")[0]
        out.append({
            "accession": adsh,
            "cik": cik,
            "company_name": name,
            "form_type": form,
            "filing_date": src.get("file_date", ""),
            "report_date": "",
            "acceptance_date": "",
            "primary_doc": "",
            "items_raw": "",
            "archive_path": "",
            "source": "efts",
        })
    return out


def discover(client, cik_padded, forms, source, start_year, end_year,
             after, before, limit):
    """Run discovery for one CIK over the selected channel."""
    if source == "submissions":
        url = SUBMISSIONS_URL.format(cik10=cik_padded)
        sub = client.get_json(url, max_age_hours=24)
        # full history lives in referenced chunks (metadata: refresh daily)
        blocks = []
        for meta in (sub.get("filings", {}) or {}).get("files", []):
            name = meta.get("name")
            if not name:
                continue
            try:
                blocks.append(client.get_json(
                    "https://data.sec.gov/submissions/" + name, max_age_hours=24 * 7))
            except Exception as exc:
                _log("  history chunk %s failed: %s" % (name, exc))
        sub["_history_blocks"] = blocks
        found = filings_from_submissions(sub, forms, after=after, before=before)
        return found, sub.get("name", "")
    if source == "idx":
        found = []
        seen = set()
        for year, qtr in iter_quarters(start_year, end_year):
            if STOP.is_set():
                break
            url = "%s/%d/QTR%d/master.idx" % (SEC_FULL_INDEX, year, qtr)
            try:
                text = client.get_text(url, max_age_hours=24 * 7)
            except Exception as exc:
                _log("  index %d Q%d failed: %s" % (year, qtr, exc))
                continue
            if text.startswith("<?xml") or text.startswith("<Error"):
                _log("  index %d Q%d: access denied, skipping" % (year, qtr))
                continue
            for row in parse_master_idx_lines(text.splitlines(), forms, cik_padded):
                if row["accession"] in seen:
                    continue
                seen.add(row["accession"])
                found.append(row)
        found.sort(key=lambda f: (f["filing_date"], f["accession"]))
        if after or before:
            found = [f for f in found
                     if (not after or f["filing_date"] >= after)
                     and (not before or f["filing_date"] <= before)]
        return found, ""
    if source == "efts":
        cik_int = int(cik_padded)
        found, seen = [], set()
        for year, month in _iter_months(start_year, end_year):
            if STOP.is_set():
                break
            start_dt = "%d-%02d-01" % (year, month)
            end_dt = "%d-%02d-28" % (year, month)  # production used -28 for all months
            page_from = 0
            while page_from < EFTS_MAX_HITS:
                params = {
                    "q": "Item",
                    "forms": "8-K",
                    "dateRange": "custom",
                    "startdt": start_dt,
                    "enddt": end_dt,
                    "ciks": str(cik_int),
                    "from": page_from,
                    "size": EFTS_PAGE_SIZE,
                }
                url = EFTS_URL + "?" + _urlencode(params)
                try:
                    data = client.get_json(url, max_age_hours=24)
                except Exception as exc:
                    _log("  efts %s failed: %s" % (start_dt, exc))
                    break
                hits = (data.get("hits") or {}).get("hits") or []
                rows = [f for f in efts_hits_to_filings(hits, forms)
                        if int(f["cik"]) == cik_int and f["accession"] not in seen]
                for f in rows:
                    seen.add(f["accession"])
                found.extend(rows)
                total = ((data.get("hits") or {}).get("total") or {}).get("value", 0)
                if len(hits) < EFTS_PAGE_SIZE:
                    break
                page_from += EFTS_PAGE_SIZE
                if page_from >= min(total, EFTS_MAX_HITS):
                    break
            if limit and len(found) >= limit:
                break
        found.sort(key=lambda f: (f["filing_date"], f["accession"]))
        if after or before:
            found = [f for f in found
                     if (not after or f["filing_date"] >= after)
                     and (not before or f["filing_date"] <= before)]
        return found, ""
    raise ValueError("unknown source %r" % source)


def _iter_months(start_year, end_year):
    for year in range(start_year, min(end_year, date.today().year) + 1):
        months = range(1, 13) if year < date.today().year else range(1, date.today().month + 1)
        for month in months:
            yield year, month


def _urlencode(params):
    from urllib.parse import urlencode
    return urlencode(params)


# ---------------------------------------------------------------- document selection
def pick_primary_doc(filing, index_items, index_primary, txt_name):
    """8-K primary document name. Priority mirrors production availability:
    submissions primaryDocument -> index.json primaryDoc -> first non-exhibit,
    non-index HTML in the accession listing (largest, like preview scoring)."""
    cand = filing.get("primary_doc") or index_primary or ""
    if cand:
        return cand
    htmls = [it["name"] for it in index_items
             if it["name"].lower().endswith((".htm", ".html"))
             and it["name"] != txt_name
             and not any(it["name"].lower().endswith(s) for s in INDEX_HTML_SUFFIXES)]
    if not htmls:
        return ""
    by_name = {it["name"]: it for it in index_items}
    return sorted(htmls, key=lambda n: (-int(by_name[n].get("size") or 0), n))[0]


def pick_press_release_docs(filing, index_items, primary, txt_name):
    """Exhibit press-release candidates from the accession listing.

    Preserves production's preview rules (PDF first, then HTML; ``*-index*.html``
    listings skipped) and adds the 8-K exhibit-naming hints (EX-99.*,
    press/release/earnings and quarter tokens)."""
    acc_lower = filing["accession"].replace("-", "").lower()
    out = []
    for it in index_items:
        name = it["name"]
        lower = name.lower()
        if lower == txt_name.lower() or lower == "index.json" or lower.startswith("_"):
            continue
        if lower == primary.lower():
            continue
        if any(lower.endswith(s) for s in INDEX_HTML_SUFFIXES):
            continue
        if any(lower.endswith(s) for s in IMG_SUFFIXES + NON_DOC_SUFFIXES):
            continue
        if acc_lower in lower and lower.endswith(".txt"):
            continue
        kind = "pdf" if lower.endswith(".pdf") else "html" if lower.endswith((".htm", ".html")) else "file"
        if kind == "file":
            continue
        hinted = any(h in lower for h in PRESS_HINTS) or re.search(r"ex-?99", lower)
        out.append({"name": name, "kind": kind, "hinted": bool(hinted),
                    "size": int(it.get("size") or 0)})
    def rank(d):
        # production preview ranked PDF before HTML; we additionally rank
        # exhibit-hinted names first so EX-99 press releases lead.
        kind_rank = 0 if d["kind"] == "pdf" else 1
        return (0 if d["hinted"] else 1, kind_rank, -d["size"], d["name"])

    out.sort(key=rank)
    return out


# ---------------------------------------------------------------- download one filing
def download_one_filing(client, root, filing, scope, include_all_docs):
    """Port of production ``download_one_filing``: always the complete
    submission .txt (verified against SEC_MARKERS before saving), then the
    accession ``index.json`` listing, then the scope-selected attachment set.
    Skips files already on disk (resume). Writes ``_meta.json`` with the
    production field names. Returns (filing, meta, docs_text_for_parsing)."""
    accession = filing["accession"]
    cik = filing["cik"]
    acc17 = accession.replace("-", "")
    txt_name = accession + ".txt"
    doc_dir = root / "docs" / ticker.cik10(cik) / acc17

    ok_count = 0
    total_bytes = 0
    errors = []
    txt_text = ""
    primary_text = ""
    index_items = []
    index_primary = ""

    # 1) complete submission .txt (production: always first, immutable doc)
    if filing.get("archive_path"):
        submission_url = "https://www.sec.gov/Archives/" + filing["archive_path"].lstrip("/")
    else:
        submission_url = ticker.archive_url(cik, accession, txt_name)
    txt_path = doc_dir / safe_filename(txt_name)
    if txt_path.exists() and txt_path.stat().st_size > 0:
        ok_count += 1
        total_bytes += txt_path.stat().st_size
        try:
            txt_text = txt_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            txt_text = ""
    elif scope != "none":
        try:
            content = client.get_bytes(submission_url)  # cache-forever: immutable
            first_part = content[:1000]
            if any(marker in first_part for marker in SEC_MARKERS):
                save_doc(doc_dir, txt_name, content)
                txt_text = content.decode("utf-8", "replace")
                ok_count += 1
                total_bytes += len(content)
            else:
                errors.append("submission_txt: response doesn't look like an SEC filing")
        except Exception as exc:
            errors.append("submission_txt: %s" % exc)

    # 2) accession listing + attachment docs
    if scope in ("press", "all") or include_all_docs:
        try:
            listing = client.get_json(ticker.index_json_url(cik, accession), max_age_hours=24)
            for item in (listing.get("directory", {}) or {}).get("item", []) or []:
                name = item.get("name")
                if not name or str(item.get("type", "")).lower() == "dir":
                    continue
                index_items.append({"name": str(name), "size": int(item.get("size") or 0)})
            index_primary = str((listing.get("directory", {}) or {}).get("primaryDoc") or "")
            if not (doc_dir / "index.json").exists():
                save_doc(doc_dir, "index.json",
                         json.dumps(listing, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:
            errors.append("index.json: %s" % exc)

        primary = pick_primary_doc(filing, index_items, index_primary, txt_name)
        by_name = {it["name"]: it for it in index_items}
        wanted = {}
        if primary and primary in by_name:
            wanted[primary] = "primary"
        if scope == "press":
            for doc in pick_press_release_docs(filing, index_items, primary, txt_name):
                wanted.setdefault(doc["name"], "press_release")
        elif scope == "all":
            seen = {txt_name.lower(), "index.json"}
            for it in index_items:
                if it["name"].lower() in seen or it["name"] in wanted:
                    continue
                seen.add(it["name"].lower())
                wanted[it["name"]] = "accession_file"

        base = ticker.archive_url(cik, accession, "")
        for name, role in wanted.items():
            if STOP.is_set():
                break
            path = doc_dir / safe_filename(name)
            if path.exists() and path.stat().st_size > 0:
                ok_count += 1
                total_bytes += path.stat().st_size
                if role == "primary":
                    primary_text = path.read_text(encoding="utf-8", errors="replace")
                continue
            try:
                content = client.get_bytes(base + name)
                if not content:
                    errors.append("%s: empty response" % name)
                    continue
                save_doc(doc_dir, name, content)
                ok_count += 1
                total_bytes += len(content)
                if role == "primary":
                    primary_text = content.decode("utf-8", "replace")
            except Exception as exc:
                errors.append("%s: %s" % (name, exc))
    else:
        # --index-only: no network at all, parse whatever is already on disk
        primary = filing.get("primary_doc") or ""
        if primary:
            p = doc_dir / safe_filename(primary)
            if p.exists():
                try:
                    primary_text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    primary_text = ""

    if ok_count > 0 and not errors:
        status = "downloaded"
    elif ok_count > 0:
        status = "partial"
    elif scope == "none":
        status = "indexed"
    else:
        status = "failed"

    meta = {
        "accession": accession,
        "cik": cik,
        "company_name": filing.get("company_name", ""),
        "form_type": filing.get("form_type", ""),
        "filing_date": filing.get("filing_date", ""),
        "archive_path": filing.get("archive_path") or ("edgar/data/%d/%s/%s"
                                                        % (int(cik), acc17, txt_name)),
        "status": status,
        "files_downloaded": ok_count,
        "total_bytes": total_bytes,
        "error": "; ".join(errors[:5]) if errors else None,
        "downloaded_at": utc_now(),
        "primary_doc": primary,
        "scope": scope,
    }
    try:
        (ensure_dir(doc_dir) / "_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return filing, meta, txt_text, primary_text, index_items, primary


# ---------------------------------------------------------------- event rows
def build_event_rows(filing, meta, txt_text, primary_text, doc_base):
    """One row per (accession, item) with the production meta fields plus the
    indexed item and its linked press-release exhibit."""
    items = collect_items(filing.get("items_raw"),
                          parse_index_head_items(txt_text),
                          parse_body_item_headings(primary_text))
    press_docs = pick_press_release_docs(filing, meta["__index_items__"],
                                         meta.get("primary_doc", ""),
                                         filing["accession"] + ".txt") \
        if "__index_items__" in meta else []
    if not items:
        items = [("", "none")]
    rows = []
    for item, source in items:
        rows.append({
            "event_key": "%s:%s" % (filing["accession"], item),
            "accession": filing["accession"],
            "cik": str(filing.get("cik") or ""),
            "company_name": meta.get("company_name", ""),
            "ticker": filing.get("ticker", ""),
            "form_type": meta.get("form_type", ""),
            "filing_date": meta.get("filing_date", ""),
            "report_date": filing.get("report_date", ""),
            "acceptance_date": filing.get("acceptance_date", ""),
            "item": item,
            "item_name": ITEM_NAMES.get(item, ""),
            "item_source": source,
            "items": filing.get("items_raw", ""),
            "primary_doc": meta.get("primary_doc", ""),
            "press_release_doc": press_docs[0]["name"] if press_docs else "",
            "press_release_docs": [d["name"] for d in press_docs],
            "archive_path": meta.get("archive_path", ""),
            "doc_dir": doc_base,
            "url": "https://www.sec.gov/Archives/edgar/data/%s/%s/" % (
                int(filing["cik"]), filing["accession"].replace("-", "")),
            "status": meta.get("status", ""),
            "files_downloaded": meta.get("files_downloaded", 0),
            "total_bytes": meta.get("total_bytes", 0),
            "error": meta.get("error"),
            "downloaded_at": meta.get("downloaded_at", ""),
            "indexed_at": utc_now(),
        })
    return rows


# ---------------------------------------------------------------- run
def add_arguments(parser):
    parser.add_argument("symbols", nargs="*",
                        help="tickers to fetch 8-K events for")
    parser.add_argument("--cik", help="a single CIK instead of/with tickers")
    parser.add_argument("--universe", metavar="FILE",
                        help="optional file with one symbol per line")
    parser.add_argument("--forms", default=DEFAULT_FORMS,
                        help="comma-separated forms (default 8-K,8-K/A)")
    parser.add_argument("--source", choices=("submissions", "idx", "efts"),
                        default="submissions",
                        help="discovery channel: submissions JSON (default), "
                             "quarterly master.idx scan, or EFTS search")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR,
                        help="first year for idx/efts discovery (default 1993)")
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--after", help="filing-date window start YYYY-MM-DD")
    parser.add_argument("--before", help="filing-date window end YYYY-MM-DD")
    parser.add_argument("--document-scope", choices=("submission", "press", "all"),
                        default="press",
                        help="submission = .txt only (fastest); press = .txt + "
                             "primary doc + exhibit press releases (default); "
                             "all = every accession file (production --document-scope all)")
    parser.add_argument("--workers", type=int, default=4,
                        help="download threads (all share the SEC_EDGAR_SLEEP throttle)")
    parser.add_argument("--rate", type=float, default=None,
                        help="requests/sec (production ran 9.8; ceiling 10); "
                             "sets SEC_EDGAR_SLEEP for this run")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap filings per company (production --limit-filings)")
    parser.add_argument("--discover-only", action="store_true",
                        help="write filings.jsonl only, download nothing")
    parser.add_argument("--index-only", action="store_true",
                        help="no downloads; index events from what is already on disk/cache")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-queue accessions previously marked failed")
    return parser


def _resolve_targets(args, client):
    targets = []
    seen = set()

    def add(sym, cik):
        if cik in seen:
            return
        seen.add(cik)
        targets.append((sym.upper().strip(), ticker.cik10(cik)))

    for sym in args.symbols or []:
        add(sym, ticker.cik_for(sym, client))
    if args.universe:
        with open(args.universe, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    add(line, ticker.cik_for(line, client))
    if args.cik:
        add(args.cik, args.cik)
    return targets


def _signal_stop(signum, _frame):
    STOP.set()
    _log("stop requested by signal %d; finishing active work..." % signum)


def run(args):
    # production pacing knob -> client throttle, before default_client() is built
    if args.rate:
        rate = min(max(args.rate, 0.1), MAX_SEC_RATE)
        os.environ["SEC_EDGAR_SLEEP"] = str(1.0 / rate)
    client = default_client()

    root = out_root(COMMAND)
    events_path = root / "events.jsonl"
    filings_path = root / "filings.jsonl"
    cp = Checkpoint(root / "checkpoint")
    done = cp.done()
    failed = cp.failed()
    if args.retry_failed:
        done -= done & failed
        failed = set()

    forms = {f.strip().upper() for f in args.forms.split(",") if f.strip()}

    targets = _resolve_targets(args, client)
    if not targets:
        _log("error: give at least one ticker, --cik, or --universe FILE")
        return 2

    scope = "none" if args.index_only else args.document_scope
    # --index-only shares the checkpoint dir but must not mark accessions
    # "done" for the downloader; namespaced keys keep the two resumable states.
    prefix = "index:" if scope == "none" else ""

    STOP.clear()
    prev_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev_handlers[sig] = signal.signal(sig, _signal_stop)
        except ValueError:
            pass  # not the main thread (library use / tests)
    try:
        exit_code = _run_body(args, client, root, events_path, filings_path,
                              cp, done, failed, forms, targets, scope, prefix)
    finally:
        for sig, handler in prev_handlers.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass
    return exit_code


def _run_body(args, client, root, events_path, filings_path, cp, done, failed,
              forms, targets, scope, prefix):

    def mark_key(acc):
        return prefix + acc

    total_written = 0
    exit_code = 0

    with JsonlWriter(events_path, key="event_key") as ew, \
            JsonlWriter(filings_path, key="accession") as fw:
        for sym, cik_padded in targets:
            if STOP.is_set():
                _log("stopped before %s" % (sym or cik_padded))
                break
            _log("== %s CIK %s (%s discovery)" % (sym, cik_padded, args.source))
            try:
                found, _name = discover(client, cik_padded, forms, args.source,
                                        args.start_year, args.end_year,
                                        args.after, args.before, args.limit)
            except Exception as exc:
                _log("  discovery failed: %s" % exc)
                exit_code = 1
                continue
            for f in found:
                f["ticker"] = sym
                f["cik"] = f["cik"] or cik_padded
            if args.limit:
                found = found[:args.limit]
            n_skipped = sum(1 for f in found if mark_key(f["accession"]) in done)
            _log("  discovered %d 8-K filing(s); %d already done" %
                 (len(found), n_skipped))
            fw.write_many([{"accession": f["accession"], "ticker": sym,
                            "cik": f["cik"], "company_name": f["company_name"],
                            "form_type": f["form_type"],
                            "filing_date": f["filing_date"],
                            "items": f["items_raw"],
                            "discovered_at": utc_now()} for f in found])

            if args.discover_only:
                continue

            todo = [f for f in found
                    if mark_key(f["accession"]) not in done
                    and (args.retry_failed or mark_key(f["accession"]) not in failed)]
            if not todo:
                # still (re)index events for already-downloaded accessions? cheap
                # checkpoint means no; skip entirely (idempotent re-run is free).
                _log("  nothing new to index")
                continue

            _log("  indexing %d filing(s) with %d worker(s), scope=%s"
                 % (len(todo), max(1, args.workers), scope))
            start = time.time()
            last_log = start
            ok = partial = bad = 0

            def handle(result):
                nonlocal total_written, ok, partial, bad
                filing, meta, txt_text, primary_text, index_items, primary = result
                acc = filing["accession"]
                meta["__index_items__"] = index_items
                meta["primary_doc"] = meta.get("primary_doc") or primary
                doc_base = "docs/%s/%s" % (ticker.cik10(filing["cik"]),
                                           acc.replace("-", ""))
                rows = build_event_rows(filing, meta, txt_text, primary_text, doc_base)
                meta.pop("__index_items__", None)
                ew.write_many(rows)
                if meta["status"] == "downloaded":
                    ok += 1
                elif meta["status"] in ("partial", "indexed"):
                    partial += 1
                else:
                    bad += 1
                if meta["status"] == "failed":
                    cp.mark_failed(mark_key(acc))
                else:
                    cp.mark_done(mark_key(acc))
                return acc

            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
                futs = {ex.submit(download_one_filing, client, root, f, scope, False): f
                        for f in todo}
                for fut in as_completed(futs):
                    if STOP.is_set():
                        break
                    try:
                        acc = handle(fut.result())
                        total_written += 1
                    except Exception as exc:
                        f = futs[fut]
                        bad += 1
                        cp.mark_failed(mark_key(f["accession"]))
                        _log("    %s: %s" % (f["accession"], exc))
                    now = time.time()
                    if now - last_log >= 15:
                        _log("  [%d/%d] ok=%d partial=%d failed=%d (%.1fs)"
                             % (total_written, len(todo), ok, partial, bad,
                                now - start))
                        last_log = now
            _log("  %s: done ok=%d partial=%d failed=%d" %
                 (sym or cik_padded, ok, partial, bad))
            if bad and not ok and not partial:
                exit_code = 1

    _log("events index: %s (%d filing(s) processed)" % (events_path, total_written))
    return exit_code
