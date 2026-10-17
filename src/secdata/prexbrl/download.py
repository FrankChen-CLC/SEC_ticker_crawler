"""Pre-XBRL complete-submission downloader.

Ported from production ``sec_pre_xbrl_download.py`` (the archive that
finished its pre-XBRL bulk run in 2026-08). Upstream endpoints:

    https://data.sec.gov/submissions/CIK##########.json      (recent block)
    https://data.sec.gov/submissions/<name>                  (filings.files[*]
        history JSONs — the critical pre-XBRL step: old filings are NOT in
        ``recent``, they live in these separate history files)
    https://www.sec.gov/Archives/edgar/data/<cik-int>/<accession>.txt
        (the *complete submission* — every SGML DOCUMENT of one filing in a
        single official SEC file; this is what the parser reads)
    https://www.sec.gov/Archives/edgar/data/<cik-int>/<accession-nodash>-index.html
        (recorded in the manifest, not fetched)

Production behaviour preserved:
- legacy form names (``10-K405``/``10-K405/A`` — the pre-2003 annual-report
  variant) in the default form set; exact-match form filtering, amendments
  must be listed explicitly (the defaults list them);
- date bounds applied to the *report period* by default (a FY2006 annual
  report filed in 2007 still qualifies for a pre-2007 archive), with
  ``filingDate`` as fallback when ``reportDate`` is blank; ``--before``
  exclusive, ``--after`` inclusive;
- skip-if-file-exists resume, ``--overwrite``, ``--list-only``;
- one manifest record per accession with sha256 and status
  ``downloaded``/``existing``/``error`` — a failed filing never stops the
  run; the manifest is flushed after every record so an interrupted run
  stays usable.

All HTTP goes through the shared throttled/caching ``EdgarClient``
(complete submissions are cached forever — SEC filings are immutable;
submissions JSONs are re-validated after 24 h).
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Iterable

SEC_SUBMISSIONS = "https://data.sec.gov/submissions"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

# 10-K405 was the pre-2003 annual-report variant used when the filer could not
# check the former Item 405 delinquent-filer disclosure box.
DEFAULT_FORMS = (
    "10-K",
    "10-K/A",
    "10-K405",
    "10-K405/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
)

MANIFEST_FIELDS = (
    "ticker",
    "company_name",
    "cik",
    "form",
    "filing_date",
    "report_date",
    "accession_number",
    "primary_document",
    "complete_submission_url",
    "filing_index_url",
    "local_path",
    "bytes",
    "sha256",
    "status",
    "error",
)


def parse_forms(value: str) -> "set[str] | None":
    if value.strip().upper() == "ALL":
        return None
    forms = {part.strip().upper() for part in value.split(",") if part.strip()}
    if not forms:
        raise ValueError("--forms cannot be empty")
    return forms


def effective_date(row: "dict[str, Any]", date_basis: str) -> str:
    report_date = str(row.get("reportDate", "") or "")
    filing_date = str(row.get("filingDate", "") or "")
    if date_basis == "filing":
        return filing_date
    return report_date or filing_date


def rows_from_columnar(data: "dict[str, list[Any]]") -> "list[dict[str, Any]]":
    accessions = data.get("accessionNumber", [])
    rows: "list[dict[str, Any]]" = []
    for index in range(len(accessions)):
        row: "dict[str, Any]" = {}
        for key, values in data.items():
            row[key] = values[index] if index < len(values) else ""
        rows.append(row)
    return rows


def get_all_submission_rows(client, cik: str) -> "tuple[dict, list[dict]]":
    """Root submissions JSON plus every history-file row (the pre-XBRL step)."""
    from ..ticker import cik10

    root = client.get_json(f"{SEC_SUBMISSIONS}/CIK{cik10(cik)}.json",
                           max_age_hours=24)
    filings = root.get("filings", {})
    rows = rows_from_columnar(filings.get("recent", {}))

    # This is the critical pre-XBRL step: older filings are not in "recent".
    for history in filings.get("files", []):
        name = history.get("name")
        if not name:
            continue
        rows.extend(rows_from_columnar(
            client.get_json(f"{SEC_SUBMISSIONS}/{name}", max_age_hours=24)))

    unique: "dict[str, dict[str, Any]]" = {}
    for row in rows:
        accession = str(row.get("accessionNumber", ""))
        if accession:
            unique[accession] = row
    return root, list(unique.values())


def select_rows(
    rows: "Iterable[dict[str, Any]]",
    *,
    forms: "set[str] | None",
    before: str,
    after: "str | None",
    date_basis: str,
) -> "list[dict[str, Any]]":
    selected = []
    for row in rows:
        form = str(row.get("form", "")).upper()
        if forms is not None and form not in forms:
            continue
        row_date = effective_date(row, date_basis)
        if not row_date or row_date >= before:
            continue
        if after and row_date < after:
            continue
        selected.append(row)
    return sorted(
        selected,
        key=lambda row: (
            effective_date(row, date_basis),
            str(row.get("filingDate", "")),
            str(row.get("accessionNumber", "")),
        ),
    )


def filing_urls(cik: str, accession: str) -> "tuple[str, str]":
    cik_int = str(int(cik))
    no_dash = accession.replace("-", "")
    complete = f"{SEC_ARCHIVES}/{cik_int}/{accession}.txt"
    index = f"{SEC_ARCHIVES}/{cik_int}/{no_dash}/{accession}-index.html"
    return complete, index


def local_doc_path(docs_root, cik: str, accession: str):
    """docs/<cik-int>/<accession-with-dashes>.txt — mirrors the EDGAR URL."""
    return docs_root / str(int(cik)) / f"{accession}.txt"


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_filing(
    client,
    row: "dict[str, Any]",
    *,
    label: str,
    company_name: str,
    cik: str,
    docs_root,
    overwrite: bool,
) -> "dict[str, Any]":
    """Fetch one complete submission; never raises (errors land in the row)."""
    accession = str(row["accessionNumber"])
    form = str(row.get("form", ""))
    report_date = str(row.get("reportDate", "") or "")
    filing_date = str(row.get("filingDate", "") or "")
    local_file = local_doc_path(docs_root, cik, accession)
    complete_url, index_url = filing_urls(cik, accession)

    status = "downloaded"
    error = ""
    from ..storage import save_doc

    try:
        if local_file.exists() and local_file.stat().st_size > 0 and not overwrite:
            status = "existing"
        else:
            data = client.get_bytes(complete_url)  # immutable: cached forever
            if not data:
                raise RuntimeError(f"Empty SEC response for {complete_url}")
            save_doc(local_file.parent, local_file.name, data)

        size = local_file.stat().st_size
        digest = sha256_file(local_file)
    except Exception as exc:  # keep the full run going and record each failure
        status = "error"
        error = str(exc)
        size = local_file.stat().st_size if local_file.exists() else 0
        digest = sha256_file(local_file) if size else ""

    return {
        "ticker": label,
        "company_name": company_name,
        "cik": cik,
        "form": form,
        "filing_date": filing_date,
        "report_date": report_date,
        "accession_number": accession,
        "primary_document": row.get("primaryDocument", ""),
        "complete_submission_url": complete_url,
        "filing_index_url": index_url,
        "local_path": str(local_file),
        "bytes": size,
        "sha256": digest,
        "status": status,
        "error": error,
        # production wrote this into a per-filing metadata.json; the manifest
        # JSONL row carries the full submissions columnar record instead
        "sec_submission_metadata": row,
        "downloaded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
