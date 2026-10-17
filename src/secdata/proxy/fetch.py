"""EDGAR endpoint helpers for the proxy pipeline, on the shared client.

Upstream endpoints used by this module family (all fetched through
``secdata.client.default_client()`` — throttle, disk cache, retries, proxy and
offline mode live there, replacing the production ``edgar.py``):

  https://data.sec.gov/submissions/CIK##########.json         (+ older "files" pages)
  https://www.sec.gov/Archives/edgar/data/<cik>/<accn>/...    (documents + index.json)
  https://data.sec.gov/api/xbrl/companyconcept/CIK##########/<taxon>/<concept>.json
  https://www.sec.gov/files/company_tickers.json              (via secdata.ticker)

The function names/shapes mirror the production ``edgar`` module so the
ported parsers call the same things with the same arguments
(``recent_filings`` rows carry ``form/accession/date/doc/items/reportDate``).
"""
import re
import urllib.error
from html import unescape

from .. import ticker as _ticker
from ..client import default_client


def get_text(url, max_age_hours=None):
    return default_client().get_text(url, max_age_hours)


def get_json(url, max_age_hours=None):
    return default_client().get_json(url, max_age_hours)


def cik10(cik):
    return _ticker.cik10(cik)


# ---------------------------------------------------------------- endpoints
def submissions(cik, max_age_hours=24):
    return get_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json",
                    max_age_hours=max_age_hours)


def recent_filings(cik, max_age_hours=24):
    """Flat list of dicts: form, accession, date, doc, items, reportDate.

    Same shape production built on; the older ``filings/files`` pages are
    appended when history is needed (each best-effort — a broken history page
    must not void the recent block).
    """
    d = submissions(cik, max_age_hours=max_age_hours)
    out = []

    def flat(r):
        n = len(r["accessionNumber"])
        for i in range(n):
            out.append({
                "form": r["form"][i],
                "accession": r["accessionNumber"][i],
                "date": r["filingDate"][i],
                "doc": (r.get("primaryDocument") or [""] * n)[i],
                "items": (r.get("items") or [""] * n)[i],
                "reportDate": (r.get("reportDate") or [""] * n)[i],
            })

    flat(d["filings"]["recent"])
    for f in d["filings"].get("files", []):
        try:
            flat(get_json("https://data.sec.gov/submissions/" + f["name"],
                          max_age_hours=None))
        except Exception:
            pass
    return out


def filing_index(cik, accession):
    return get_json(_ticker.index_json_url(cik, accession))


def filing_url(cik, accession, doc):
    return _ticker.archive_url(cik, accession, doc)


def companyconcept(cik, taxon, concept, max_age_hours=24):
    url = (f"https://data.sec.gov/api/xbrl/companyconcept/"
           f"CIK{cik10(cik)}/{taxon}/{concept}.json")
    try:
        return get_json(url, max_age_hours=max_age_hours)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ---------------------------------------------------------------- text utils
def strip_tags(html):
    txt = re.sub(r"<[^>]+>", " ", html)
    # full entity decode (missed &#59;/&#36; before — leaked into summaries)
    txt = unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()
