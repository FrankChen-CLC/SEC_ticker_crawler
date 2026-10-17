"""13F / N-PORT document parsers — pure functions, no network, no database.

Ported from ``sec_13f_nport_full_db.py`` (the newest production build,
2026-07), which superseded ``download_13f_v3.py`` and
``sec_download_13f_nport.py``. Field names match the production tables
``holdings_13f`` / ``holdings_nport`` exactly, so downstream joins keep
working. Behaviours deliberately preserved:

- A full submission ``.txt`` carries one or more ``<XML>`` segments; the
  13F information table often sits in the *second* segment (the header
  form XML comes first).
- EDGAR serves these XMLs under several namespaces, and some elements
  carry their value in a ``value`` attribute — every lookup is a
  namespace-agnostic local-name search (direct children first, then a
  deep scan).
- Real filings contain stray control characters and text before the
  first ``<`` — stripped before ``ET.fromstring``.
- Pre-2013 13Fs have no XML at all: a fixed-width "FORM 13F INFORMATION
  TABLE" text table is parsed instead (fallback as of v3/newest build).
- Duplicate rows inside one accession are removed via ``holding_hash``
  (SHA-256 over the meaningful fields) — production's
  ``UNIQUE(accession, row_hash)``.

``nport_currency()`` folds in a fix the production build missed: N-PORT
encodes a non-USD security as ``<currencyConditional curCd="EUR">`` —
currency is an *attribute* there — while USD rows carry a plain
``<currency>`` tag; the old candidate list covered neither reliably.

Quarterly full-index discovery helpers live here too (``master.idx``
pipe format, ``.gz``/``.zip`` decoding) since they are pure text work.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import io
import json
import math
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, List, Tuple
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

SEC_ARCHIVES = "https://www.sec.gov/Archives/"

DEFAULT_FORMS = "13F-HR,13F-HR/A,NPORT-P,NPORT-P/A"
ALL_FORMS = (
    "13F-HR,13F-HR/A,13F-NT,13F-NT/A,"
    "NPORT-P,NPORT-P/A,NPORT-NP,NPORT-NP/A,NPORT-EX,NPORT-EX/A"
)
TEXT_EXTENSIONS = {
    ".txt", ".htm", ".html", ".xml", ".xsd", ".json", ".idx", ".csv",
    ".tsv", ".css", ".js",
}


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def normalize_form(form: str) -> str:
    return re.sub(r"\s+", "", (form or "").upper())


def clean_cik(cik: str) -> str:
    s = re.sub(r"\D", "", str(cik or ""))
    return s.lstrip("0") or "0"


def cik10(cik) -> str:
    return clean_cik(str(cik)).zfill(10)


def accession_no_dash(accession: str) -> str:
    return (accession or "").replace("-", "")


def maybe_decode(data: bytes) -> str:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def valid_attachment(name: str, include_binary: bool) -> bool:
    if not name or name.endswith("/"):
        return False
    if include_binary:
        return True
    return Path(name).suffix.lower() in TEXT_EXTENSIONS


# --------------------------------------------------------------- XML helpers
def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def element_text(el) -> str:
    if el is None:
        return ""
    parts = [p.strip() for p in el.itertext() if p and p.strip()]
    return " ".join(parts).strip()


def direct_or_deep_text(el, *names: str) -> str:
    """First direct child whose local name matches, else first anywhere below.

    Falls back to a ``value`` attribute (some N-PORT elements are
    ``<tag value="..."/>``). Matching is case-insensitive on the local
    name — namespaces vary across EDGAR vintages.
    """
    wanted = {n.lower() for n in names}
    for child in el:
        if local_name(child.tag).lower() in wanted:
            txt = element_text(child)
            if txt:
                return txt
            if "value" in child.attrib:
                return str(child.attrib.get("value") or "").strip()
    for child in el.iter():
        if child is el:
            continue
        if local_name(child.tag).lower() in wanted:
            txt = element_text(child)
            if txt:
                return txt
            if "value" in child.attrib:
                return str(child.attrib.get("value") or "").strip()
    return ""


def first_text(root, *names: str) -> str:
    wanted = {n.lower() for n in names}
    for el in root.iter():
        if local_name(el.tag).lower() in wanted:
            txt = element_text(el)
            if txt:
                return txt
            if "value" in el.attrib:
                return str(el.attrib.get("value") or "").strip()
    return ""


def safe_int(value: Any) -> "int | None":
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if not s:
        return None
    try:
        n = float(s)
        if not math.isfinite(n):
            return None
        return int(n)
    except ValueError:
        return None


def safe_float(value: Any) -> "float | None":
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if s.lower() in ("n/a", "na", "nan"):
        return None
    try:
        n = float(s)
        if not math.isfinite(n):
            return None
        return n
    except ValueError:
        return None


def flatten_element(el, prefix: str = "") -> "dict[str, Any]":
    """Whole subtree -> dotted dict; repeated keys become lists.

    Stored as ``extra_json`` per row (production kept it for audit).
    """
    out: "dict[str, Any]" = {}
    name = local_name(el.tag)
    key = f"{prefix}.{name}" if prefix else name
    txt = element_text(el)
    if txt and not list(el):
        out[key] = txt
    for attr, value in el.attrib.items():
        out[f"{key}@{attr}"] = value
    for child in el:
        child_map = flatten_element(child, key)
        for k, v in child_map.items():
            if k in out:
                if not isinstance(out[k], list):
                    out[k] = [out[k]]
                out[k].append(v)
            else:
                out[k] = v
    return out


def parse_xml(xml_text: str):
    """Parse leniently: drop junk before the first ``<``, strip control chars."""
    s = xml_text.strip()
    if not s:
        return None
    first = s.find("<")
    if first > 0:
        s = s[first:]
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    try:
        return ET.fromstring(s.encode("utf-8"))
    except ET.ParseError:
        return None


def iter_xml_payloads(text: str) -> "Iterable[str]":
    """Yield each ``<XML>…</XML>`` body; bare-XML docs yield their whole text."""
    pos = 0
    found = False
    while True:
        start = text.find("<XML>", pos)
        if start < 0:
            break
        end = text.find("</XML>", start)
        if end < 0:
            break
        found = True
        yield text[start + 5:end].strip()
        pos = end + 6
    stripped = text.strip()
    if not found and stripped.startswith("<"):
        yield stripped


# ------------------------------------------------------------ index discovery
def decode_index_payload(url: str, payload: bytes) -> str:
    if url.endswith(".gz"):
        return gzip.decompress(payload).decode("utf-8", errors="replace")
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            name = next((n for n in zf.namelist() if n.endswith("master.idx")),
                        zf.namelist()[0])
            return zf.read(name).decode("utf-8", errors="replace")
    return payload.decode("utf-8", errors="replace")


def parse_master_index(text: str, forms: "set[str]", year: int,
                       qtr: int) -> "List[dict[str, str]]":
    """Filter a quarterly ``master.idx`` to the wanted forms.

    Line format: ``CIK|Company|Form|Filed as of date|FileName.txt``.
    """
    entries: "List[dict[str, str]]" = []
    seen: "set[str]" = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "|" not in line or line.startswith("-") \
                or line.lower().startswith("cik|"):
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        cik, company, form, filed_at, filename = [p.strip() for p in parts[:5]]
        form_norm = normalize_form(form)
        if form_norm not in forms:
            continue
        accession = filename.rsplit("/", 1)[-1].replace(".txt", "")
        if accession in seen:
            continue
        seen.add(accession)
        acc_no_dash = accession_no_dash(accession)
        entries.append({
            "accession": accession,
            "cik": clean_cik(cik),
            "cik10": cik10(cik),
            "company_name": company,
            "form_type": form_norm,
            "filing_date": filed_at,
            "filename": filename,
            "accession_no_dash": acc_no_dash,
            "accession_dir_url":
                f"{SEC_ARCHIVES}edgar/data/{clean_cik(cik)}/{acc_no_dash}/",
            "full_submission_url": urljoin(SEC_ARCHIVES, filename),
            "index_year": str(year),
            "index_quarter": str(qtr),
        })
    return entries


def quarter_for_month(month: int) -> int:
    return (month - 1) // 3 + 1


def current_quarter() -> "Tuple[int, int]":
    today = _dt.date.today()
    return today.year, quarter_for_month(today.month)


def quarter_range(start_year: int, end_year: int,
                  end_quarter: int) -> "List[Tuple[int, int]]":
    out = []
    for year in range(start_year, end_year + 1):
        for qtr in range(1, 5):
            if year == 1994 and qtr < 3:      # EDGAR full-index starts 1994Q3
                continue
            if year == end_year and qtr > end_quarter:
                break
            out.append((year, qtr))
    return out


# ------------------------------------------------------------------ 13F rows
def extract_13f_report_date(text: str) -> "str | None":
    for pat in (
        r"CONFORMED PERIOD OF REPORT:\s*([0-9]{8})",
        r"PERIOD OF REPORT:\s*([0-9]{8})",
        r"<periodOfReport>\s*([^<]+)\s*</periodOfReport>",
    ):
        m = re.search(pat, text, flags=re.I)
        if m:
            raw = m.group(1).strip()
            if re.fullmatch(r"\d{8}", raw):
                return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
            return raw
    return None


def extract_13f_row(meta: "dict[str, Any]", info, source_filename: str,
                    report_date: "str | None") -> "dict[str, Any]":
    return {
        "accession": meta.get("accession", ""),
        "cik": meta.get("cik", ""),
        "cik10": meta.get("cik10", ""),
        "filer_name": meta.get("filer_name", ""),
        "form_type": meta.get("form_type", ""),
        "filing_date": meta.get("filing_date", ""),
        "report_date": report_date,
        "name_of_issuer": direct_or_deep_text(info, "nameOfIssuer"),
        "title_of_class": direct_or_deep_text(info, "titleOfClass"),
        "cusip": direct_or_deep_text(info, "cusip").upper(),
        "figi": direct_or_deep_text(info, "figi"),
        "value_x1000": safe_int(direct_or_deep_text(info, "value")),
        "shares_or_principal": safe_int(direct_or_deep_text(info, "sshPrnamt")),
        "shares_type": direct_or_deep_text(info, "sshPrnamtType"),
        "put_call": direct_or_deep_text(info, "putCall"),
        "investment_discretion": direct_or_deep_text(info, "investmentDiscretion"),
        "other_manager": direct_or_deep_text(info, "otherManager"),
        "vote_sole": safe_int(direct_or_deep_text(info, "Sole")) or 0,
        "vote_shared": safe_int(direct_or_deep_text(info, "Shared")) or 0,
        "vote_none": safe_int(direct_or_deep_text(info, "None")) or 0,
        "source_filename": source_filename,
        "extra_json": json.dumps(flatten_element(info),
                                 ensure_ascii=False, sort_keys=True),
        "created_at": utc_now(),
    }


def parse_old_13f_table(meta: "dict[str, Any]", text: str, source_filename: str,
                        report_date: "str | None") -> "List[dict[str, Any]]":
    """Pre-2013 fixed-width "FORM 13F INFORMATION TABLE" text fallback."""
    rows = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        upper = line.upper()
        if "FORM 13F INFORMATION TABLE" in upper:
            in_table = True
            continue
        if in_table and ("</TABLE>" in upper or "</TEXT>" in upper):
            in_table = False
        if not in_table:
            continue
        if not line or any(x in upper for x in (
                "NAME OF ISSUER", "VOTING AUTHORITY", "----", "<PAGE>",
                "VALUE SHRS")):
            continue
        # First 9-char alnum token that contains a DIGIT. Production used a
        # bare \b[A-Z0-9]{9}\b, which also swallows 9-letter issuer words
        # like "MICROSOFT" when they precede the real CUSIP; every CUSIP
        # carries digits, so the extra condition is strictly safer.
        match = next((m for m in re.finditer(r"\b[A-Z0-9]{9}\b", line)
                      if any(ch.isdigit() for ch in m.group(0))), None)
        if match is None:
            continue
        cusip = match.group(0)
        before = line[:match.start()].strip()
        after = line[match.end():].strip()
        before_parts = before.rsplit(None, 1)
        name = before_parts[0] if len(before_parts) > 1 else before
        title = before_parts[1] if len(before_parts) > 1 else ""
        tokens = after.split()
        if len(tokens) < 2:
            continue
        value = safe_int(tokens[0])
        shares = safe_int(tokens[1])
        share_type = next((t for t in tokens[2:]
                           if t.upper() in {"SH", "SHS", "PRN"}), "")
        nums = [safe_int(t) for t in tokens if safe_int(t) is not None]
        row = dict(meta)
        row.update({
            "report_date": report_date,
            "name_of_issuer": name,
            "title_of_class": title,
            "cusip": cusip,
            "figi": "",
            "value_x1000": value,
            "shares_or_principal": shares,
            "shares_type": share_type,
            "put_call": "",
            "investment_discretion": "",
            "other_manager": "",
            "vote_sole": nums[-3] if len(nums) >= 3 else 0,
            "vote_shared": nums[-2] if len(nums) >= 2 else 0,
            "vote_none": nums[-1] if len(nums) >= 1 else 0,
            "source_filename": source_filename,
            "extra_json": json.dumps({"legacy_line": line}, ensure_ascii=False),
            "created_at": utc_now(),
        })
        rows.append(row)
    return rows


def parse_13f_documents(meta: "dict[str, Any]",
                        docs: "Iterable[Tuple[str, str]]"):
    """(filename, text) docs -> (rows, report_date, [(file, stage, error)])."""
    holdings: "List[dict[str, Any]]" = []
    errors: "List[Tuple[str, str, str]]" = []
    report_date: "str | None" = None
    seen: "set[str]" = set()
    for filename, text in docs:
        if Path(filename).suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if report_date is None:
            report_date = extract_13f_report_date(text)
        parsed_any = False
        for xml in iter_xml_payloads(text):
            if "infotable" not in xml.lower() \
                    and "informationtable" not in xml.lower():
                continue
            root = parse_xml(xml)
            if root is None:
                errors.append((filename, "13f_xml_parse", "XML parse failed"))
                continue
            for info in root.iter():
                if local_name(info.tag).lower() != "infotable":
                    continue
                row = extract_13f_row(meta, info, filename, report_date)
                if not row.get("name_of_issuer") and not row.get("cusip"):
                    continue
                row_hash = holding_hash(row)
                if row_hash in seen:
                    continue
                row["row_hash"] = row_hash
                seen.add(row_hash)
                holdings.append(row)
                parsed_any = True
        if not parsed_any and "FORM 13F INFORMATION TABLE" in text:
            for row in parse_old_13f_table(meta, text, filename, report_date):
                row_hash = holding_hash(row)
                if row_hash in seen:
                    continue
                row["row_hash"] = row_hash
                seen.add(row_hash)
                holdings.append(row)
    return holdings, report_date, errors


# ----------------------------------------------------------------- N-PORT rows
def nport_currency(inv) -> str:
    v = direct_or_deep_text(inv, "curCd", "currencyCode", "currencyISO",
                            "baseCurCd", "currency")
    if v:
        return v
    for el in inv.iter():
        if local_name(el.tag).lower() == "currencyconditional":
            for k, val in el.attrib.items():
                if k.lower() == "curcd":
                    return str(val).strip()
    return ""


def extract_nport_fund_meta(root) -> "dict[str, str]":
    return {
        "registrant_name": first_text(root, "regName", "registrantName",
                                      "nameOfRegistrant"),
        "series_name": first_text(root, "seriesName"),
        "series_id": first_text(root, "seriesId"),
        "class_id": first_text(root, "classId"),
    }


def extract_nport_row(meta: "dict[str, Any]", inv, fund_meta: "dict[str, str]",
                      source_filename: str, report_date: "str | None",
                      period_of_report: "str | None") -> "dict[str, Any]":
    row = {
        "accession": meta.get("accession", ""),
        "cik": meta.get("cik", ""),
        "cik10": meta.get("cik10", ""),
        "registrant_name": fund_meta.get("registrant_name") \
            or meta.get("filer_name") or "",
        "series_name": fund_meta.get("series_name"),
        "series_id": fund_meta.get("series_id"),
        "class_id": fund_meta.get("class_id"),
        "form_type": meta.get("form_type", ""),
        "filing_date": meta.get("filing_date", ""),
        "report_date": report_date,
        "period_of_report": period_of_report,
        "investment_name": direct_or_deep_text(inv, "name"),
        "lei": direct_or_deep_text(inv, "lei"),
        "title": direct_or_deep_text(inv, "title"),
        "cusip": direct_or_deep_text(inv, "cusip").upper(),
        "isin": direct_or_deep_text(inv, "isin", "isIn").upper(),
        "ticker": direct_or_deep_text(inv, "ticker", "tickerSymbol").upper(),
        "balance": safe_float(direct_or_deep_text(inv, "balance")),
        "units": direct_or_deep_text(inv, "units", "unit", "descOthUnits"),
        "value_usd": safe_float(direct_or_deep_text(inv, "valUSD")),
        "pct_nav": safe_float(direct_or_deep_text(inv, "pctVal")),
        "currency": nport_currency(inv),
        "asset_category": direct_or_deep_text(inv, "assetCat"),
        "issuer_category": direct_or_deep_text(inv, "issuerCat"),
        "investment_country": direct_or_deep_text(inv, "invCountry", "country"),
        "payoff_profile": direct_or_deep_text(inv, "payoffProfile"),
        "fair_value_level": direct_or_deep_text(inv, "fairValLevel"),
        "restricted_security": direct_or_deep_text(inv, "isRestrictedSec"),
        "source_filename": source_filename,
        "extra_json": json.dumps(flatten_element(inv),
                                 ensure_ascii=False, sort_keys=True),
        "created_at": utc_now(),
    }
    return row


def parse_nport_documents(meta: "dict[str, Any]",
                          docs: "Iterable[Tuple[str, str]]"):
    """(filename, text) docs -> (rows, report_date, period_of_report, errors)."""
    rows: "List[dict[str, Any]]" = []
    errors: "List[Tuple[str, str, str]]" = []
    seen: "set[str]" = set()
    report_date: "str | None" = None
    period_of_report: "str | None" = None
    for filename, text in docs:
        if Path(filename).suffix.lower() not in TEXT_EXTENSIONS:
            continue
        for xml in iter_xml_payloads(text):
            if "invstorsec" not in xml.lower() \
                    and "invstorsecs" not in xml.lower():
                continue
            root = parse_xml(xml)
            if root is None:
                errors.append((filename, "nport_xml_parse", "XML parse failed"))
                continue
            report_date = report_date or first_text(
                root, "repPdDate", "reportDate", "periodOfReport")
            period_of_report = period_of_report or first_text(
                root, "periodOfReport", "repPdDate")
            fund_meta = extract_nport_fund_meta(root)
            for inv in root.iter():
                if local_name(inv.tag).lower() != "invstorsec":
                    continue
                row = extract_nport_row(meta, inv, fund_meta, filename,
                                        report_date, period_of_report)
                if not any(row.get(k) for k in
                           ("investment_name", "cusip", "isin", "ticker")):
                    continue
                row_hash = holding_hash(row)
                if row_hash in seen:
                    continue
                row["row_hash"] = row_hash
                seen.add(row_hash)
                rows.append(row)
    return rows, report_date, period_of_report, errors


def holding_hash(row: "dict[str, Any]") -> str:
    """SHA-256 over the meaningful fields (production dedupe key)."""
    meaningful = {
        k: v for k, v in row.items()
        if k not in {"id", "created_at", "row_hash", "source_filename"}
    }
    payload = json.dumps(meaningful, ensure_ascii=False, sort_keys=True,
                         default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------------- issuer -> ticker
_CORP_SUFFIX = re.compile(
    r",?\s*(INC\.?|CORP\.?|CORPORATION|COMPANY|CO\.?|LTD\.?|LLC|LP|PLC"
    r"|LIMITED|HOLDINGS?|SPONSORED\s+ADR|GROUP|S\.?A\.?|AG|NV|SE|ASA|AB)$",
    re.I,
)


def clean_issuer_name(name: str) -> str:
    """Production ``build_nport_ticker_map.py`` normalisation, verbatim.

    Upper-cased, whitespace collapsed, 80 chars, trailing corporate
    suffix removed. Exact match only — no fuzzy matching (production
    learned that fuzzy joins misattribute holdings).
    """
    c = re.sub(r"\s+", " ", (name or "").upper().strip())[:80]
    c = _CORP_SUFFIX.sub("", c)
    return c.strip()

# ----------------------------------------------------- comparison helpers (pure)
# Ported from production's SQLite views v_security_holders_history /
# v_security_holder_changes (LAG per holder+security ordered by period) and
# from sec_13f_api.py's GROUP BY (cik, report_date, cusip, put_call) SUM.
def kind_of(rows) -> str:
    for r in rows[:50]:
        if "registrant_name" in r:
            return "nport"
    return "13f"


def period_date(r, kind: str):
    if kind == "nport":
        return r.get("report_date") or r.get("period_of_report") \
            or r.get("filing_date")
    return r.get("report_date") or r.get("filing_date")


def security_id(r, kind: str):
    if kind == "nport":
        for k in ("ticker", "cusip", "isin"):     # production's COALESCE(NULLIF…)
            v = (r.get(k) or "").strip()
            if v:
                return v.upper()
        return None
    v = (r.get("cusip") or "").strip()
    return v.upper() or None


def value_usd(r, kind: str):
    if kind == "nport":
        return r.get("value_usd")
    v = r.get("value_x1000")
    return None if v is None else v * 1000.0


def quantity(r, kind: str):
    return r.get("balance") if kind == "nport" else r.get("shares_or_principal")


def long_position(r) -> bool:
    pc = (r.get("put_call") or "").strip().lower()
    return pc in ("", "share")


def diff_periods(rows, kind=None, after=None, before=None,
                 holders=None, long_only=False) -> "List[dict]":
    """Period-over-period changes, porting the ``v_security_holder_changes``
    LAG view: partition by (holder, security) — plus ``put_call`` for 13F so
    option legs never diff against the underlying — order by period, and
    emit ``value_usd_change`` / ``quantity_change`` / ``previous_period_date``.
    Also synthesises ``exited`` rows: a security that disappeared while the
    holder kept filing (the production API showed this only implicitly).
    """
    kind = kind or kind_of(rows)
    groups: "dict[tuple, List[dict]]" = {}
    holder_last: "dict[tuple, str]" = {}
    for r in rows:
        sec = security_id(r, kind)
        pd = period_date(r, kind)
        if not sec or not pd:
            continue
        if after and pd < after or before and pd > before:
            continue
        if kind == "13f" and long_only and not long_position(r):
            continue
        if holders:
            hk = {str(int(h)) for h in holders}
            if str(int(r.get("cik") or 0)) not in hk:
                continue
        if kind == "nport":
            key = (str(r.get("cik", "")), str(r.get("series_id") or ""),
                   str(r.get("class_id") or ""), sec)
        else:
            key = (str(r.get("cik", "")), sec,
                   (r.get("put_call") or "").strip().upper())
        groups.setdefault(key, []).append((pd, r))
        holder_key = key[: (3 if kind == "nport" else 1)]
        prev = holder_last.get(holder_key)
        if prev is None or pd > prev:
            holder_last[holder_key] = pd

    out: "List[dict]" = []
    for key, seq in groups.items():
        seq.sort(key=lambda t: t[0])
        prev_row = None
        prev_period = None
        sec_last = seq[-1][0]
        holder_key = key[: (3 if kind == "nport" else 1)]
        for pd, r in seq:
            v = value_usd(r, kind)
            q = quantity(r, kind)
            pv = value_usd(prev_row, kind) if prev_row else None
            pq = quantity(prev_row, kind) if prev_row else None
            change_v = None if (pv is None or v is None) else v - pv
            change_q = None if (pq is None or q is None) else \
                (float(q) - float(pq)) if (isinstance(q, (int, float))
                                           and isinstance(pq, (int, float))) else None
            status = ("initial" if prev_row is None else
                      "changed" if (change_v not in (None, 0.0)
                                    or change_q not in (None, 0.0))
                      else "unchanged")
            out.append(_change_row(kind, r, pd, v, q, prev_period,
                                   change_v, change_q, status))
            prev_row, prev_period = r, pd
        hlast = holder_last.get(holder_key)
        if hlast and sec_last < hlast:
            pd, r = seq[-1]
            v = value_usd(r, kind)
            q = quantity(r, kind)
            out.append(_change_row(
                kind, r, hlast, None, None, pd,
                None if v is None else -v,
                None if q is None else -float(q), "exited"))
    out.sort(key=lambda x: (x["holder_cik"], str(x["security_id"]),
                            x["period_date"]))
    return out


def _change_row(kind, r, period, v, q, prev_period, dv, dq, status) -> "dict[str, Any]":
    if kind == "nport":
        return {
            "holder_type": "fund_nport",
            "holder_cik": r.get("cik"), "holder_cik10": r.get("cik10"),
            "holder_name": r.get("series_name") or r.get("registrant_name"),
            "fund_series_id": r.get("series_id"),
            "fund_class_id": r.get("class_id"),
            "security_id": security_id(r, kind),
            "cusip": r.get("cusip"), "isin": r.get("isin"),
            "ticker": r.get("ticker"),
            "security_name": r.get("investment_name"),
            "security_title": r.get("title"),
            "asset_category": r.get("asset_category"),
            "period_date": period,
            "value_usd": v, "quantity": q,
            "quantity_units": r.get("units"),
            "put_call": None, "pct_nav": r.get("pct_nav"),
            "previous_period_date": prev_period,
            "value_usd_change": dv, "quantity_change": dq,
            "status": status,
        }
    return {
        "holder_type": "institution_13f",
        "holder_cik": r.get("cik"), "holder_cik10": r.get("cik10"),
        "holder_name": r.get("filer_name"),
        "fund_series_id": None, "fund_class_id": None,
        "security_id": security_id(r, kind),
        "cusip": r.get("cusip"), "isin": None, "ticker": None,
        "security_name": r.get("name_of_issuer"),
        "security_title": r.get("title_of_class"),
        "asset_category": "EQUITY_OR_OPTION",
        "period_date": period,
        "value_usd": v, "quantity": q,
        "quantity_units": r.get("shares_type"),
        "put_call": r.get("put_call"), "pct_nav": None,
        "previous_period_date": prev_period,
        "value_usd_change": dv, "quantity_change": dq,
        "status": status,
    }


def aggregate_at(rows, cik: str, kind: str, date: str) -> "dict[tuple, dict]":
    """Production API's GROUP BY (cik, report_date, cusip, put_call) SUM."""
    agg: "dict[tuple, dict]" = {}
    for r in rows:
        if str(r.get("cik", "")) != str(cik):
            continue
        if period_date(r, kind) != date:
            continue
        sec = security_id(r, kind)
        if not sec:
            continue
        key = (sec, (r.get("put_call") or "").strip().upper())
        a = agg.setdefault(key, {
            "security_id": sec,
            "cusip": r.get("cusip"), "isin": r.get("isin"),
            "ticker": r.get("ticker"),
            "security_name": r.get("name_of_issuer") or r.get("investment_name"),
            "put_call": r.get("put_call"),
            "value": 0.0, "quantity": 0.0, "lines": 0})
        v = value_usd(r, kind)
        q = quantity(r, kind)
        a["value"] += v or 0
        a["quantity"] += (q or 0) if isinstance(q, (int, float)) else 0
        a["lines"] += 1
    return agg


def diff_pairwise(rows, cik_a: str, cik_b: str, date=None, kind=None) -> "List[dict]":
    """Institution-vs-institution holdings diff at one common period."""
    kind = kind or kind_of(rows)
    dates_a = {period_date(r, kind) for r in rows if str(r.get("cik", "")) == str(cik_a)}
    dates_b = {period_date(r, kind) for r in rows if str(r.get("cik", "")) == str(cik_b)}
    common = sorted(d for d in (dates_a & dates_b) if d)
    if date:
        chosen = date if date in common else None
    else:
        chosen = common[-1] if common else None
    if chosen is None:
        return []
    a = aggregate_at(rows, cik_a, kind, chosen)
    b = aggregate_at(rows, cik_b, kind, chosen)
    out = []
    for key in sorted(set(a) | set(b)):
        ra, rb = a.get(key), b.get(key)
        base = ra or rb
        va = ra["value"] if ra else None
        vb = rb["value"] if rb else None
        out.append({
            "report_date": chosen, "holder_a_cik": str(cik_a),
            "holder_b_cik": str(cik_b),
            "security_id": base["security_id"], "cusip": base["cusip"],
            "isin": base["isin"], "ticker": base["ticker"],
            "security_name": base["security_name"], "put_call": base["put_call"],
            "in_a": ra is not None, "in_b": rb is not None,
            "value_usd_a": va, "value_usd_b": vb,
            "quantity_a": ra["quantity"] if ra else None,
            "quantity_b": rb["quantity"] if rb else None,
            "value_delta": None if (va is None or vb is None) else va - vb,
            "status": "both" if (ra and rb) else ("a_only" if ra else "b_only"),
        })
    out.sort(key=lambda x: -(max(x["value_usd_a"] or 0, x["value_usd_b"] or 0)))
    return out


