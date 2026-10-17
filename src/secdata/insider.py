"""secdata insider — Form 4 insider-transaction downloader and parser.

Ported from the production ``form4_daily_v2.py`` cron (runs twice daily) and
the Aug-3 full-history bulk tool ``download_sec_form4_xml_all.py``.

Upstream endpoints (preserved verbatim from production):

  incremental (day-by-day full-text listing, paged by 100):
    https://efts.sec.gov/LATEST/search-index?q=&forms=4
        &dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD&from=N
  submission document selection:
    https://www.sec.gov/Archives/edgar/data/{CIK}/{ADSH_NODASH}/index.json
      → pick ``wk-form4_*`` or ``*.xml`` whose name contains "form4"
    https://www.sec.gov/Archives/edgar/data/{CIK}/{ADSH_NODASH}/{doc}.xml
  bulk history (--bulk): quarterly Archives full-index folders
    https://www.sec.gov/Archives/edgar/full-index/{YYYY}/QTR{N}/master.zip
        (contains ``master.idx``; falls back to the uncompressed
         .../master.idx which always exists — old quarters' zips can lag)
    https://www.sec.gov/Archives/{submission_path}
        (complete-submission SGML .txt; the ownershipDocument XML is
         extracted from its <XML> blocks, form types "4" and "4/A")

What is kept from production:
  * the EFTS listing semantics (ciks[0]=reporter, ciks[1]=issuer, page=100,
    one request per day, break-the-day on error);
  * every field name of the full-fidelity ``filings`` / ``trades`` row
    dicts (the trades rows carry the 8/03 classification columns, computed
    at parse time);
  * the classification table, keyword tuples, confidence ladder, evidence
    strings, plan-adoption-date regex (union of the v2 embedded
    ``classify_trade`` and the Aug-27 ``form4_classify.py`` refinement —
    the logic is identical; KW_TRUST/KW_GIFT are kept as spec constants);
  * the M + same-day S exercise-and-sell grouping with the 0.90 ratio
    threshold;
  * resume semantics: successes are appended to done.txt, failures to
    failed.txt, ``--resume`` skips done.

What was replaced:
  * MySQL ticker-universe (``_valid_tickers``) → optional ``--universe
    FILE`` (one symbol per line, "# comments" ignored; absent = no filter);
  * POST-ingest to a private endpoint → JSONL under out/insider/
    (filings.jsonl + trades.jsonl; raw XML mirrored under
    out/insider/docs/CIK{10}/{adsh_nodash}/);
  * the bulk tool's SQLite queue → storage.Checkpoint + a quarters
    watermark file;
  * raw urllib + per-process proxy + hand-rolled limiter → the shared
    throttled/caching default_client(). All HTTP (including the 4 worker
    threads) goes through one client, so one global SEC_EDGAR_SLEEP governs
    the process — with the default 0.15 s this stays under SEC's 10 req/s;
    if you raise ``--workers``, raise ``SEC_EDGAR_SLEEP`` proportionally
    (e.g. 8 workers → ~0.3 s).

Notes on preserved production quirks:
  * ``_safe_float`` maps a price of 0 to None (many Form 4 rows carry "0"
    as a placeholder);
  * ``is_holding_only``/``deemed_execution_date``/``equity_swap_involved``/
    ``nature_of_ownership`` are parsed as constants (0 / None) exactly as
    production emits them;
  * a filing with zero (filtered) trades counts as ``no_xml`` in the
    stats and is NOT marked done (it is retried on the next run);
  * for an ``S`` row the HIGH-confidence test is ``aff10b5 or plan_date``
    — the raw (possibly "false") aff10b5One string, kept as-is.

Run modes:
    secdata insider --date 2026-08-26            single day
    secdata insider --days-back 1 --resume       incremental (cron pattern)
    secdata insider --bulk                       full history from 2003-01-01
    secdata insider --bulk --start-date ... --end-date ... --limit 50
    secdata insider ... --dry-run                parse only, write nothing
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from . import storage, ticker
from .client import default_client

COMMAND = "insider"
HELP = "Form 4 insider transactions: daily incremental (EFTS) or --bulk history."

EFTS_URL = ("https://efts.sec.gov/LATEST/search-index?q=&forms=4"
            "&dateRange=custom&startdt={day}&enddt={day}&from={frm}")
ARCHIVES_ROOT = "https://www.sec.gov/Archives/"
FULL_INDEX_ROOT = ARCHIVES_ROOT + "edgar/full-index"
PAGE_SIZE = 100          # EFTS paging step (production: size = 100)
FORM_TYPES = {"4", "4/A"}
DEFAULT_BULK_START = date(2003, 1, 1)   # electronic Form 4 XML begins here

# --------------------------------------------------------------------- keywords
# 8/03 classification spec — union of form4_daily_v2.py and form4_classify.py.
KW_OPEN_MARKET = ("open market", "open-market", "purchased in the open market",
                  "sold in the open market")
KW_10B5 = ("10b5-1", "10b5 1", "trading plan", "written plan", "adopted",
           "entered into", "pursuant to a rule 10b5")
KW_TAX = ("tax withholding", "tax obligation", "tax liability", "withheld.*tax",
          "cover.*tax", "net share settlement", "minimum statutory withholding")
KW_EXERCISE_PRICE = ("exercise price", "payment of the exercise price",
                     "exercise price payment")
KW_RSU = ("restricted stock unit", "rsu", "performance stock unit", "psu",
          "restricted stock", "stock unit")
KW_VEST = ("vested", "vesting", "settled", "settlement", "converted into common stock")
KW_GRANT = ("award", "grant", "granted")
# Declared by the Aug-27 refinement for spec completeness; the G and Z
# transaction codes already classify these directly, so no rule reads them.
KW_TRUST = ("transferred to trust", "contributed to trust", "distributed from trust",
            "revocable trust", "irrevocable trust", "family trust", "trust transfer")
KW_GIFT = ("gift", "donation", "charitable")

RE_PLAN_DATE = re.compile(r"(?:adopted|entered into|established)\s*(?:a rule 10b5-1\s*)?"
                          r"(?:trading plan\s*)?(?:by the reporting person\s*)?"
                          r"on\s+([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)


def _progress(msg: str) -> None:
    print(f"insider: {msg}", file=sys.stderr)


# ------------------------------------------------------------------ classification
def classify_trade(t: dict, fn_text: str, aff10b5) -> dict:
    """8/03 spec classification of one trade row. Preserved from production."""
    code = (t.get("transaction_code") or "").upper()
    ad = (t.get("acquired_disposed") or "").upper()
    title = (t.get("security_title") or "").lower()
    ev = []
    cls = None
    econ = None
    plan = "UNKNOWN"
    plan_date = None
    conf = "LOW"

    is_10b5 = aff10b5 and str(aff10b5).lower() in ("true", "1", "yes")
    if not is_10b5 and any(k in fn_text.lower() for k in KW_10B5):
        is_10b5 = True
        ev.append("footnote mentions 10b5-1/plan")
    if is_10b5:
        plan = "10B5_1"
        m = RE_PLAN_DATE.search(fn_text)
        if m:
            plan_date = m.group(1)
            ev.append(f"plan adoption date {plan_date}")

    if code == "P":
        cls = "MARKET_OR_PRIVATE_PURCHASE"
        conf = "HIGH" if ad == "A" else "MEDIUM"
        if any(k in fn_text.lower() for k in KW_OPEN_MARKET):
            cls = "OPEN_MARKET_PURCHASE"
            ev.append("footnote open market")
        ev.append("code=P")
    elif code == "S":
        if is_10b5:
            cls = "10B5_1_PLANNED_SALE"
            conf = "HIGH" if (aff10b5 or plan_date) else "MEDIUM"
        else:
            cls = "NON_10B5_1_IDENTIFIED_SALE"
            conf = "MEDIUM"
        if any(k in fn_text.lower() for k in KW_OPEN_MARKET):
            ev.append("footnote open market")
        ev.append("code=S")
    elif code == "A":
        cls = "GRANT_OR_OTHER_ACQUISITION"
        conf = "MEDIUM"
        if any(k in title for k in KW_RSU) or any(k in fn_text.lower() for k in KW_RSU):
            cls = "RSU_OR_STOCK_AWARD"
            ev.append("RSU title/footnote")
        elif any(k in title for k in ("option", "warrant")) or "option" in fn_text.lower():
            cls = "OPTION_GRANT"
            ev.append("option title")
        if any(k in fn_text.lower() for k in KW_GRANT):
            ev.append("award/grant keyword")
        ev.append("code=A")
    elif code == "M":
        cls = "EXERCISE_OR_CONVERSION"
        conf = "MEDIUM"
        if any(k in fn_text.lower() for k in KW_VEST):
            cls = "RSU_VESTING_OR_SETTLEMENT"
            ev.append("vest/settle keyword")
        ev.append("code=M")
    elif code == "F":
        cls = "TAX_OR_EXERCISE_PRICE_WITHHOLDING"
        conf = "MEDIUM"
        low = fn_text.lower()
        if any(re.search(k, low) for k in KW_TAX):
            cls = "TAX_WITHHOLDING"
            ev.append("tax keyword")
        elif any(k in low for k in KW_EXERCISE_PRICE):
            cls = "EXERCISE_PRICE_PAYMENT"
            ev.append("exercise price keyword")
        ev.append("code=F")
    elif code == "G":
        cls = "GIFT"
        conf = "HIGH"
        ev.append("code=G")
    elif code == "C":
        cls = "CONVERSION"
        ev.append("code=C")
    elif code == "D":
        cls = "DISPOSITION_TO_ISSUER"
        ev.append("code=D")
    elif code == "I":
        cls = "EMPLOYEE_PLAN_TRANSACTION"
        ev.append("code=I")
    elif code == "Z":
        cls = "TRUST_TRANSFER"
        conf = "HIGH"
        ev.append("code=Z")
    elif code == "K":
        cls = "EQUITY_SWAP"
        ev.append("code=K")
    elif code == "U":
        cls = "TENDER_DISPOSITION"
        ev.append("code=U")
    elif code == "J":
        cls = "OTHER"
        conf = "LOW"
        ev.append("code=J see footnotes")
    else:
        cls = "UNCLASSIFIED"
        ev.append(f"code={code or 'NULL'}")

    return {
        "primary_classification": cls,
        "economic_reason": econ,
        "plan_status": plan,
        "plan_adoption_date": plan_date,
        "classification_confidence": conf,
        "evidence": "; ".join(ev),
        "signal_relevance": None,
    }


def apply_exercise_and_sell_group(trades: list) -> None:
    """In-memory port of apply_exercise_and_sell: M + same-day S → SELL / PARTIAL_SELL."""
    groups = {}
    for t in trades:
        if t.get("transaction_code") in ("M", "S") and t.get("transaction_date"):
            key = t["transaction_date"]
            g = groups.setdefault(key, {"m": 0.0, "s": 0.0, "rows": []})
            sh = t.get("shares") or 0
            if t["transaction_code"] == "M":
                g["m"] += sh
            else:
                g["s"] += sh
            g["rows"].append(t)
    for g in groups.values():
        if g["m"] <= 0 or g["s"] <= 0:
            continue
        ratio = g["s"] / g["m"]
        if ratio >= 0.90:
            econ = "EXERCISE_AND_SELL"
        elif ratio > 0:
            econ = "EXERCISE_AND_PARTIAL_SELL"
        else:
            econ = "EXERCISE_AND_HOLD"
        cls = "EXERCISE_AND_SELL" if econ == "EXERCISE_AND_SELL" else econ
        for t in g["rows"]:
            t["primary_classification"] = cls
            t["economic_reason"] = econ


# ---------------------------------------------------------------- XML helpers
def _get_text(node, path: str):
    el = node.find(path)
    if el is None:
        return None
    val = el.find("value")
    if val is not None and val.text:
        return val.text.strip()
    return (el.text or "").strip() or None


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None


def parse_xml(adsh: str, issuer_cik, file_date, xml_bytes, xml_path: str):
    """Pure core: ownershipDocument XML bytes → (filing dict, trades list).

    Returns None when the issuer has no trading symbol or no transaction rows
    (holdings-only filings) — production treats those the same way.
    """
    root = ET.fromstring(xml_bytes)

    issuer_ticker = _get_text(root, ".//issuer/issuerTradingSymbol")
    if not issuer_ticker:
        return None
    issuer_name = _get_text(root, ".//issuer/issuerName") or ""
    reporter_name = _get_text(root, ".//reportingOwnerId/rptOwnerName") or ""
    rpt_owner_cik = _get_text(root, ".//reportingOwnerId/rptOwnerCik") or ""
    period_of_report = _get_text(root, ".//periodOfReport")
    aff10b5_one = _get_text(root, ".//aff10b5One")
    schema_version = root.attrib.get("schemaVersion")

    # footnotes: id → text
    fn_map = {}
    for fn in root.findall(".//footnotes/footnote"):
        fid = fn.attrib.get("id")
        txt = _get_text(fn, "text")
        if fid and txt:
            fn_map[fid] = txt

    filing = {
        "accession": adsh,
        "rpt_owner_cik": rpt_owner_cik,
        "issuer_cik": str(int(issuer_cik)),
        "issuer_ticker": issuer_ticker,
        "issuer_name": issuer_name,
        # adsh is CIK-prefixed (e.g. 0002104636-26-000102) — filing date comes from
        # EftsSearch file_date, falling back to the XML periodOfReport
        "filing_date": file_date or _get_text(root, ".//filingDate") or period_of_report,
        "period_of_report": period_of_report,
        "document_type": "4",
        "schema_version": schema_version,
        "aff10b5_one": aff10b5_one,
        "xml_path": xml_path,
        "download_status": "downloaded",
    }

    trades = []
    for table_type, path in (("nonDerivative", ".//nonDerivativeTransaction"),
                             ("derivative", ".//derivativeTransaction")):
        for tx in root.findall(path):
            d = _parse_tx(tx, table_type, filing, reporter_name, fn_map, aff10b5_one)
            if d:
                trades.append(d)

    if not trades:
        return None

    # 8/03 spec: M + same-day S grouping (in-memory port of apply_exercise_and_sell)
    apply_exercise_and_sell_group(trades)
    return filing, trades


def _parse_tx(tx, table_type, filing, reporter_name, fn_map, aff10b5_one):
    tx_date = _get_text(tx, "transactionDate/value")
    tx_code = _get_text(tx, "transactionCoding/transactionCode") or ""
    shares_v = _get_text(tx, "transactionAmounts/transactionShares/value")
    price_v = _get_text(tx, "transactionAmounts/transactionPricePerShare/value")
    ad_v = _get_text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
    shares_owned = _get_text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
    direct_indirect = _get_text(tx, "ownershipNature/directOrIndirectOwnership/value")
    security_title = _get_text(tx, "securityTitle/value")

    if not tx_date or not shares_v:
        return None
    shares = _safe_int(shares_v)
    if shares is None:
        return None

    # linked footnotes
    fn_ids = [f.text for f in tx.findall(".//footnoteId") if f.text]
    linked = ",".join(fn_ids)[:100] if fn_ids else None
    fn_text = " ".join(fn_map.get(fid, "") for fid in fn_ids if fid in fn_map)

    d = {
        "accession": filing["accession"],
        "rpt_owner_cik": filing["rpt_owner_cik"],
        "table_type": table_type,
        "is_holding_only": 0,
        "security_title": security_title,
        "transaction_date": tx_date,
        "deemed_execution_date": None,
        "transaction_code": tx_code,
        "equity_swap_involved": None,
        "shares": shares,
        "price_per_share": _safe_float(price_v),
        "acquired_disposed": ad_v,
        "shares_owned_after": _safe_int(shares_owned),
        "direct_indirect": direct_indirect,
        "nature_of_ownership": None,
        "conversion_exercise_price": _safe_float(_get_text(tx, ".//conversionOrExercisePrice/value")),
        "exercise_date": _get_text(tx, ".//exerciseDate/value"),
        "expiration_date": _get_text(tx, ".//expirationDate/value"),
        "underlying_security_title": _get_text(tx, ".//underlyingSecurityTitle/value"),
        "underlying_security_shares": _safe_int(_get_text(tx, ".//underlyingSecurityShares/value")),
        "linked_footnote_ids": linked,
        "issuer_cik": filing["issuer_cik"],
        "issuer_ticker": filing["issuer_ticker"],
        "issuer_name": filing["issuer_name"],
        "filing_date": filing["filing_date"],
    }
    # classification (8/03 spec) — computed here, at parse time
    cls = classify_trade(d, fn_text, aff10b5_one)
    d.update(cls)
    return d


# ------------------------------------------------------------------ incremental
def fetch_form4_list(client, start_date: str, end_date: str) -> list:
    """EFTS day-by-day Form 4 listing, paged by 100. Preserved from v2.

    Listing rows are keyed by filing date and settle shortly after acceptance,
    so days before today are cached forever by the client; today is kept
    fresh with a 1 h max_age. A failed day is skipped (break), like cron.
    """
    out = []
    today = storage.today()
    cur = datetime.strptime(start_date, "%Y-%m-%d")
    e = datetime.strptime(end_date, "%Y-%m-%d")
    while cur <= e:
        day_str = cur.strftime("%Y-%m-%d")
        max_age = 1.0 if day_str >= today else None
        frm = 0
        while True:
            url = EFTS_URL.format(day=day_str, frm=frm)
            try:
                data = client.get_json(url, max_age_hours=max_age)
            except Exception as exc:
                _progress(f"efts {day_str} from={frm} fail: {exc}")
                break
            hits = data.get("hits", {}).get("hits", [])
            total = data.get("hits", {}).get("total", {}).get("value", 0)
            if not hits:
                break
            for h in hits:
                s_ = h.get("_source", {})
                ciks = s_.get("ciks", [])
                if len(ciks) < 2:
                    continue
                out.append({
                    "adsh": s_.get("adsh"),
                    "issuer_cik": ciks[1],
                    "reporter_cik": ciks[0],
                    "file_date": s_.get("file_date"),
                    "display_names": s_.get("display_names", []),
                })
            frm += PAGE_SIZE
            if frm >= total:
                break
        cur += timedelta(days=1)
    return out


def parse_filing(client, adsh: str, issuer_cik, file_date=None, docs_root=None):
    """index.json → Form 4 XML → (filing, trades). Returns None on any miss.

    Xml-doc selection: first item whose name starts with ``wk-form4_`` or
    ends with ``.xml`` and contains ``form4`` (production fast path), then —
    deliberate improvement over production — any remaining ``.xml`` is
    content-sniffed for ``<ownershipDocument``. New-schema filers name the
    document e.g. ``section16.xml``, which the name-only production rule
    silently skips (~60% of daily filings, measured 2026-10-17).
    """
    idx_url = ticker.archive_url(issuer_cik, adsh, "index.json")
    try:
        idx = client.get_json(idx_url)
    except Exception as e:
        _progress(f"{adsh}: index.json failed: {e}")
        return None
    items = idx.get("directory", {}).get("item", [])
    xml_name = None
    fallbacks = []
    for it in items:
        n = it.get("name", "")
        if n.startswith("wk-form4_") or (n.endswith(".xml") and "form4" in n.lower()):
            xml_name = n
            break
        if n.lower().endswith(".xml") and not n.lower().endswith("-index.xml"):
            fallbacks.append(n)
    xml_bytes = None
    if xml_name:
        xml_url = ticker.archive_url(issuer_cik, adsh, xml_name)
        try:
            xml_bytes = client.get_bytes(xml_url)
        except Exception as e:
            _progress(f"{adsh}: xml download failed: {e}")
            return None
    else:
        # sniff other .xml items for the ownershipDocument root (section16.xml…)
        for n in fallbacks:
            url = ticker.archive_url(issuer_cik, adsh, n)
            try:
                data = client.get_bytes(url)
            except Exception as e:
                _progress(f"{adsh}: xml probe {n} failed: {e}")
                continue
            if b"ownershipDocument" in data[:1024]:
                xml_name, xml_bytes, xml_url = n, data, url
                break
        if xml_name is None:
            return None
    try:
        parsed = parse_xml(adsh, issuer_cik, file_date, xml_bytes, xml_url)
    except ET.ParseError as e:
        _progress(f"{adsh}: xml parse error: {e}")
        return None
    if parsed is None:
        return None
    if docs_root is not None:
        _save_doc(client, docs_root, issuer_cik, adsh, xml_name, xml_bytes)
    return parsed


def _save_doc(client, docs_root, issuer_cik, adsh, name, xml_bytes) -> Path:
    """Mirror the EDGAR path under out/insider/docs/CIK{10}/{adsh_nodash}/."""
    d = Path(docs_root) / f"CIK{ticker.cik10(issuer_cik)}" / adsh.replace("-", "")
    return storage.save_doc(d, name, xml_bytes)


# ------------------------------------------------------------------------ bulk
def quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1


def iter_quarters(start: date, end: date):
    year, quarter = start.year, quarter_of(start)
    final = (end.year, quarter_of(end))
    while (year, quarter) <= final:
        yield year, quarter
        if quarter == 4:
            year, quarter = year + 1, 1
        else:
            quarter += 1


def parse_master_index(raw: bytes, start: date, end: date) -> list:
    """quarterly master.idx → Form 4/4-A row dicts inside [start, end].

    Preserved from the bulk tool: latin-1 decode, pipe header detection,
    ``line.split("|", 4)``, FORM_TYPES filter, accession = Path(path).stem.
    """
    text = raw.decode("latin-1", errors="replace")
    out = []
    in_data = False
    for line in text.splitlines():
        if not in_data:
            if line.startswith("CIK|Company Name|Form Type|Date Filed|Filename"):
                in_data = True
            continue
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        cik, company_name, form_type, filed, submission_path = parts
        form_type = form_type.strip().upper()
        if form_type not in FORM_TYPES:
            continue
        try:
            filed_date = datetime.strptime(filed.strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        if not start <= filed_date <= end:
            continue
        submission_path = submission_path.strip().lstrip("/")
        out.append({
            "accession": Path(submission_path).stem,
            "issuer_cik": cik.strip(),
            "company_name": company_name.strip(),
            "form_type": form_type,
            "filing_date": filed_date.isoformat(),
            "submission_path": submission_path,
        })
    return out


def load_quarter_index(client, year: int, quarter: int, refresh: bool) -> bytes:
    """master.zip → master.idx, falling back to the uncompressed index.

    Past quarters are immutable (cached forever); the current quarter is
    re-fetched when ``refresh`` is set (its zip lags the raw idx).
    """
    base = f"{FULL_INDEX_ROOT}/{year}/QTR{quarter}"
    age = 24.0 if refresh else None
    try:
        zip_body = client.get_bytes(base + "/master.zip", max_age_hours=age)
    except Exception:
        zip_body = None
    if zip_body:
        try:
            with zipfile.ZipFile(io.BytesIO(zip_body)) as archive:
                names = {name.lower(): name for name in archive.namelist()}
                name = names.get("master.idx")
                if name is None:
                    raise ValueError("no master.idx inside master.zip")
                return archive.read(name)
        except (zipfile.BadZipFile, ValueError):
            pass
    return client.get_bytes(base + "/master.idx", max_age_hours=age)


DOCUMENT_RE = re.compile(br"<DOCUMENT>(.*?)</DOCUMENT>", re.IGNORECASE | re.DOTALL)
TYPE_RE = re.compile(br"(?im)^<TYPE>\s*([^\r\n<]+)")
FILENAME_RE = re.compile(br"(?im)^<FILENAME>\s*([^\r\n<]+)")
XML_BLOCK_RE = re.compile(br"<XML>\s*(.*?)\s*</XML>", re.IGNORECASE | re.DOTALL)
OWNERSHIP_RE = re.compile(
    br"<(?:[A-Za-z_][\w.-]*:)?ownershipDocument\b", re.IGNORECASE
)


def extract_form4_xml(submission: bytes):
    """From an EDGAR complete-submission SGML: (ownershipDocument XML bytes,
    source doc name) or (None, None). Regexes preserved from the bulk tool;
    the FILENAME capture is an addition so raw docs keep an EDGAR doc name.
    """
    candidates = []
    for document_match in DOCUMENT_RE.finditer(submission):
        document = document_match.group(1)
        type_match = TYPE_RE.search(document)
        if not type_match:
            continue
        document_type = type_match.group(1).strip().decode("ascii", errors="ignore").upper()
        if document_type not in FORM_TYPES:
            continue
        name_match = FILENAME_RE.search(document)
        name = (name_match.group(1).strip().decode("ascii", errors="ignore")
                if name_match else None)
        for match in XML_BLOCK_RE.finditer(document):
            candidates.append((match.group(1), name))

    # conservative fallback for early files with non-standard SGML boundaries
    if not candidates:
        candidates = [(m.group(1), None) for m in XML_BLOCK_RE.finditer(submission)]

    for candidate, name in candidates:
        if OWNERSHIP_RE.search(candidate):
            # keep the raw bytes and the XML encoding declaration as returned
            return candidate.strip().replace(b"\x00", b"") + b"\n", name
    return None, None


def process_bulk_filing(client, f: dict, universe, docs_root):
    """Download complete-submission → extract XML → parse → classify."""
    url = ARCHIVES_ROOT + f["submission_path"]
    try:
        body = client.get_bytes(url)
    except Exception as e:
        _progress(f"{f['accession']}: submission failed: {e}")
        return {"adsh": f["accession"], "kind": "fail"}
    xml, name = extract_form4_xml(body)
    if xml is None:
        # pre-2003-era HTML Form 4s and oddballs: terminal, like production's
        # 'no_xml' status row (not retried on resume)
        return {"adsh": f["accession"], "kind": "no_xml"}
    try:
        parsed = parse_xml(f["accession"], f["issuer_cik"], f["filing_date"], xml, url)
    except Exception as e:
        _progress(f"{f['accession']}: xml parse error: {e}")
        return {"adsh": f["accession"], "kind": "fail"}
    if parsed is None:
        return {"adsh": f["accession"], "kind": "empty"}
    filing, trades = parsed
    trades = [t for t in trades
              if not universe or (t["issuer_ticker"] or "").upper() in universe]
    if not trades:
        return {"adsh": f["accession"], "kind": "empty"}
    if docs_root is not None:
        _save_doc(client, docs_root, f["issuer_cik"], f["accession"],
                  name or f["accession"] + ".xml", xml)
    return {"adsh": f["accession"], "kind": "ok", "filing": filing, "trades": trades}


def _run_bulk(args, client, root, ckpt, universe) -> int:
    try:
        start_d = (datetime.strptime(args.start_date, "%Y-%m-%d").date()
                   if args.start_date else DEFAULT_BULK_START)
        end_d = (datetime.strptime(args.end_date, "%Y-%m-%d").date()
                 if args.end_date else date.today())
    except ValueError as e:
        _progress(f"bad date: {e}")
        return 2
    if start_d > end_d:
        _progress("--start-date must not be after --end-date")
        return 2

    quarters_state = storage.State(str(root / "checkpoint" / "bulk_quarters.json"),
                                   {"quarters_done": []})
    done = ckpt.done() if args.resume else set()
    now = date.today()
    current = (now.year, quarter_of(now))

    stats = {"listed": 0, "ok": 0, "fail": 0, "no_xml": 0, "skipped_done": 0,
             "trades": 0, "filings": 0, "rows_written": 0}
    limit = args.limit or 0
    started = 0

    writers = {}                      # opened lazily on the first ok filing
    docs_root = None if args.dry_run else root / "docs"

    try:
        for year, quarter in iter_quarters(start_d, end_d):
            key = f"{year}Q{quarter}"
            if key in quarters_state.get("quarters_done", []) and (year, quarter) != current:
                _progress(f"index {year} Q{quarter} already done, skipping")
                continue
            refresh = (year, quarter) == current
            try:
                body = load_quarter_index(client, year, quarter, refresh)
            except Exception as e:
                _progress(f"index {year} Q{quarter} failed: {e}")
                continue
            rows = parse_master_index(body, start_d, end_d)
            stats["listed"] += len(rows)
            _progress(f"index {year} Q{quarter}: Form 4/4-A {len(rows)} filings")
            if (year, quarter) != current:
                qd = quarters_state.get("quarters_done", [])
                if key not in qd:
                    quarters_state.set("quarters_done", qd + [key])

            for i in range(0, len(rows), 2000):
                batch = rows[i:i + 2000]
                if limit:
                    room = limit - started
                    if room <= 0:
                        break
                    batch = batch[:room]
                started += len(batch)
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    futures = []
                    for f in batch:
                        if f["accession"] in done:
                            stats["skipped_done"] += 1
                            continue
                        futures.append(ex.submit(process_bulk_filing,
                                                 client, f, universe, docs_root))
                    for fut in as_completed(futures):
                        _ingest_result(fut.result(), root, writers, ckpt, stats,
                                       args.dry_run)
                if limit and started >= limit:
                    break
            if limit and started >= limit:
                _progress(f"limit {limit} reached, stopping bulk run")
                break
    finally:
        for w in writers.values():
            w.close()

    _progress(f"parsed: ok={stats['ok']} fail={stats['fail']} no_xml={stats['no_xml']} "
              f"skipped_done={stats['skipped_done']} trades={stats['trades']}")
    if args.dry_run:
        _progress("DRY RUN — nothing written")
    else:
        _progress(f"=== done: rows_written={stats['rows_written']} "
                  f"filings={stats['filings']} trades={stats['trades']} ===")
    return 0


def _ingest_result(res, root, writers, ckpt, stats, dry_run) -> None:
    """Shared completion path for both modes (streaming writes + checkpoint).

    Kinds mirror the production outcome mapping:
      fail   → failed.txt, retried next run
      no_xml → terminal (no ownershipDocument at all) → done.txt
      empty  → parsed but zero (filtered) trades: counted as no_xml like the
               cron did, but NOT marked done so a later --universe run retries
      ok     → JSONL rows + done.txt, written as they arrive
    """
    if res is None:
        return
    adsh = res["adsh"]
    kind = res["kind"]
    if kind == "fail":
        stats["fail"] += 1
        if not dry_run:
            ckpt.mark_failed(adsh)
        return
    if kind in ("no_xml", "empty"):
        stats["no_xml"] += 1
        if kind == "no_xml" and not dry_run:
            ckpt.mark_done(adsh)
        return
    filing, trades = res["filing"], res["trades"]
    stats["ok"] += 1
    stats["filings"] += 1
    stats["trades"] += len(trades)
    if dry_run:
        return
    if "fw" not in writers:
        writers["fw"] = storage.JsonlWriter(root / "filings.jsonl", key="accession")
        writers["tw"] = storage.JsonlWriter(root / "trades.jsonl")
    if writers["fw"].write(filing):
        stats["rows_written"] += 1
    stats["rows_written"] += writers["tw"].write_many(trades)
    ckpt.mark_done(adsh)


# -------------------------------------------------------------------- runner
def _run_incremental(args, client, root, ckpt, universe) -> int:
    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError as e:
            _progress(f"bad --date: {e}")
            return 2
        start_date = end_date = args.date
    else:
        end_date = storage.today()
        start_date = (datetime.now() - timedelta(days=args.days_back)).strftime("%Y-%m-%d")

    _progress(f"=== insider: range={start_date} -> {end_date} ===")

    done = ckpt.done() if args.resume else set()
    if args.resume:
        _progress(f"resume: skipping {len(done)} already-done")

    listings = fetch_form4_list(client, start_date, end_date)
    _progress(f"efts search returned {len(listings)} Form4 filings")
    if args.limit:
        listings = listings[:args.limit]
    _progress(f"will process {len(listings)} listings")

    stats = {"listed": len(listings), "ok": 0, "fail": 0, "no_xml": 0,
             "skipped_done": 0, "trades": 0, "filings": 0, "rows_written": 0}
    writers = {}                      # opened lazily on the first ok filing
    docs_root = None if args.dry_run else root / "docs"

    stats_lock = threading.Lock()

    def process_one(item):
        adsh = item["adsh"]
        if adsh in done:
            with stats_lock:
                stats["skipped_done"] += 1
            return None
        try:
            r = parse_filing(client, adsh, item["issuer_cik"],
                             item.get("file_date"), docs_root)
            if r is None:
                return (adsh, None)
            filing, trades = r
            trades = [t for t in trades
                      if not universe or (t["issuer_ticker"] or "").upper() in universe]
            if not trades:
                return (adsh, "empty")
            return (adsh, (filing, trades))
        except Exception as e:
            _progress(f"{adsh} exception: {e}")
            return (adsh, None)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(process_one, it) for it in listings]
            for fut in as_completed(futures):
                r = fut.result()
                if r is None:
                    continue
                adsh, payload = r
                if payload is None:
                    res = {"adsh": adsh, "kind": "fail"}
                elif payload == "empty":
                    res = {"adsh": adsh, "kind": "empty"}
                else:
                    filing, trades = payload
                    res = {"adsh": adsh, "kind": "ok",
                           "filing": filing, "trades": trades}
                _ingest_result(res, root, writers, ckpt, stats, args.dry_run)
    finally:
        for w in writers.values():
            w.close()

    _progress(f"parsed: ok={stats['ok']} fail={stats['fail']} no_xml={stats['no_xml']} "
              f"skipped_done={stats['skipped_done']} trades={stats['trades']}")
    if args.dry_run:
        _progress("DRY RUN — skipping writes")
    else:
        _progress(f"=== done: rows_written={stats['rows_written']} "
                  f"filings={stats['filings']} trades={stats['trades']} ===")
    return 0


def load_universe(path):
    """--universe FILE: one symbol per line ('#'/blank lines ignored). None = no filter."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"universe file not found: {path}")
    return {ln.strip().upper() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", help="single filing date YYYY-MM-DD (incremental)")
    parser.add_argument("--days-back", type=int, default=1,
                        help="incremental: pull the last N days (default 1)")
    parser.add_argument("--bulk", action="store_true",
                        help="full-history mode via quarterly Archives full-index")
    parser.add_argument("--start-date", help="bulk: first filing date (default 2003-01-01)")
    parser.add_argument("--end-date", help="bulk: last filing date (default today)")
    parser.add_argument("--universe", metavar="FILE",
                        help="keep only trades whose issuer ticker appears in FILE "
                             "(one symbol per line; absent = no filter)")
    parser.add_argument("--resume", action="store_true",
                        help="skip accessions already in checkpoint/done.txt")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse only: write no files and update no checkpoints")
    parser.add_argument("--limit", type=int, default=0,
                        help="process at most N filings this run (0 = all)")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent parse workers (default 4); all HTTP still "
                             "goes through the single shared client throttle — raise "
                             "SEC_EDGAR_SLEEP with this")


def run(args) -> int:
    client = default_client()
    if args.workers < 1:
        _progress("--workers must be >= 1")
        return 2
    try:
        universe = load_universe(args.universe)
    except FileNotFoundError as e:
        _progress(str(e))
        return 2
    if universe is not None:
        _progress(f"universe loaded: {len(universe)} tickers")
    root = storage.out_root(COMMAND)
    ckpt = storage.Checkpoint(str(root / "checkpoint"))
    if args.bulk:
        return _run_bulk(args, client, root, ckpt, universe)
    return _run_incremental(args, client, root, ckpt, universe)
