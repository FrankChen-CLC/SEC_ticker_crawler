"""Ticker <-> CIK resolution.

Primary source is SEC's own ``company_tickers.json`` (cached 7 days). For
symbols it omits (renamed/delisted issuers) users can keep a small JSON of
curated overrides — same idea as the production ``cik_overrides.json``.
``--overrides`` / ``SEC_EDGAR_OVERRIDES`` points at that file; missing = no-op.
"""
import json
import os
import re
import threading

from .client import default_client

_LOCK = threading.Lock()
_MAP = None

BRIDGE_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def ticker_map(client=None) -> dict:
    """{TICKER: cik-str}, loaded once per process and cached on disk."""
    global _MAP
    with _LOCK:
        if _MAP is None:
            client = client or default_client()
            raw = client.get_json(BRIDGE_TICKERS_URL, max_age_hours=24 * 7)
            _MAP = {str(v["ticker"]).upper(): str(v["cik_str"])
                    for v in raw.values()}
        return _MAP


def overrides(path=None) -> dict:
    p = path or os.environ.get("SEC_EDGAR_OVERRIDES", "")
    if not p or not os.path.exists(p):
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return {str(k).upper(): str(v) for k, v in json.load(f).items()}


def normalize_symbol(symbol: str) -> str:
    sym = symbol.upper().strip()
    # BRK.B style tickers appear as BRK-B in SEC data
    return sym.replace(".", "-") if "." in sym and sym.rsplit(".", 1)[-1] \
        in ("A", "B", "C") else sym


def cik_for(symbol: str, client=None, overrides_path=None) -> str:
    """Resolve a ticker to a CIK string (no padding). Raises KeyError if unknown."""
    sym = symbol.upper().strip()
    alt = normalize_symbol(sym)
    o = overrides(overrides_path)
    for key in (sym, alt):
        if key in o:
            return o[key]
    m = ticker_map(client)
    for key in (sym, alt):
        if key in m:
            return m[key]
    raise KeyError(f"no CIK found for {symbol!r} — check the ticker, or add "
                   "it to your overrides file (see ticker.py docstring)")


def name_for(cik: str, client=None) -> str:
    """Company title straight from the (cached) submissions JSON."""
    c = client or default_client()
    d = c.get_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json",
                   max_age_hours=24)
    return d.get("name", "")


def cik10(cik) -> str:
    return str(int(cik)).zfill(10)


# ---------------------------------------------------------------- filings helpers
def form_matches(form: str, forms) -> bool:
    """True if form is in the wanted list, or is an amendment of it
    (``10-K/A`` matches a ``10-K`` request)."""
    if not forms:
        return True
    base = form.split("/")[0]
    return form in forms or base in forms


def filings_of(submissions_json: dict, forms=None, after=None, before=None):
    """Flatten a submissions JSON column block into row dicts, filtered by
    form list / date window (ISO YYYY-MM-DD strings).

    Accepts either the wrapper (``{"filings": {"recent": …}}``) or a flat
    column block straight from an older ``files/…`` history file.

    Kept intentionally small: pipeline modules extend it for their own fields.
    """
    if "filings" in submissions_json:
        r = submissions_json["filings"]["recent"]
    else:
        r = submissions_json
    out = []
    n = len(r["accessionNumber"])
    forms_set = {f.upper() for f in forms} if forms else None
    for i in range(n):
        form = r["form"][i]
        if forms_set is not None and not form_matches(form, forms_set):
            continue
        date = r["filingDate"][i]
        if after and date < after:
            continue
        if before and date > before:
            continue
        out.append({
            "form": form,
            "accession": r["accessionNumber"][i],
            "filing_date": date,
            "report_date": (r.get("reportDate") or [""] * n)[i],
            "acceptance_date": (r.get("acceptanceDateTime") or [""] * n)[i],
            "primary_document": (r.get("primaryDocument") or [""] * n)[i],
            "items": (r.get("items") or [""] * n)[i],
        })
    return out


def archive_url(cik, accession, doc=None) -> str:
    accn = re.sub(r"-", "", accession)
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}"
    return f"{base}/{doc}" if doc else base + "/"


def index_json_url(cik, accession) -> str:
    return archive_url(cik, accession, "index.json")
