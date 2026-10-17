"""8-K driven event streams + governance flags, all from submissions metadata
where possible (items field), document text only where needed.

- Item 5.02  management tenure history (appoint / depart / interim)
- Item 2.01  acquisition completed            -> M&A scorecard
- Item 4.01  auditor change                    -> financial-reporting quality
- Item 4.02  non-reliance (restatement)        -> financial-reporting quality
- EX-97 exhibit on 10-K                        -> clawback policy on file
- 'material weakness' / 'restatement' scan of latest 10-K body
- Rule 10b5-1 section of 10-Q/10-K             -> plan adoptions/terminations
"""
import re

from . import fetch

ITEM_TAGS = {
    "5.02": "officer_director_change",
    "2.01": "acquisition_completed",
    "4.01": "auditor_change",
    "4.02": "non_reliance_restatement",
    "2.06": "material_impairment",
    "1.01": "material_agreement",
    "2.02": "earnings_release",
}

APPT_RE = re.compile(
    r"([^.,;]{3,60}?)\s+(?:has\s+)?(?:been\s+)?(appointed|elected|named|promoted|"
    r"resigned|retired|stepped\s+down|terminated|removed|succeeds|will\s+serve|"
    r"transition[s]?|departed|left)\b([^.;]{0,160})", re.I)


def event_stream(cik, since="2019-01-01", filings=None):
    filings = filings or fetch.recent_filings(cik)
    events = []
    for f in filings:
        if f["form"] != "8-K" or f["date"] < since:
            continue
        items = [i.strip() for i in (f["items"] or "").split(",") if i.strip()]
        for it in items:
            tag = ITEM_TAGS.get(it)
            if not tag:
                continue
            ev = {"date": f["date"], "item": it, "tag": tag,
                  "accession": f["accession"], "doc": f["doc"]}
            if tag == "officer_director_change":
                ev["detail"] = _502_detail(cik, f)
            events.append(ev)
    events.sort(key=lambda e: e["date"], reverse=True)
    return events


def _502_detail(cik, f):
    """Names + verbs from the 8-K body (best effort)."""
    try:
        doc = fetch.get_text(fetch.filing_url(cik, f["accession"], f["doc"]))
    except Exception:
        return []
    txt = fetch.strip_tags(doc)
    out = []
    for m in APPT_RE.finditer(txt):
        who, verb, rest = m.group(1).strip(), m.group(2).lower(), m.group(3).strip()
        if len(who.split()) > 4 or who.lower().startswith(("the ", "our ", "item")):
            continue
        out.append({"who": who, "verb": verb, "context": rest[:140]})
        if len(out) >= 8:
            break
    return out


def tenure_stats(events):
    """CEO/CFO turnover counts from 5.02 events."""
    ceo_changes = [e for e in events if e["tag"] == "officer_director_change"
                   and any("chief executive" in (d.get("context") or "").lower()
                           or "ceo" in (d.get("context") or "").lower()
                           for d in (e.get("detail") or []))]
    cfo_changes = [e for e in events if e["tag"] == "officer_director_change"
                   and any("chief financial" in (d.get("context") or "").lower()
                           or "cfo" in (d.get("context") or "").lower()
                           for d in (e.get("detail") or []))]
    return {"n_502_events": len([e for e in events
                                 if e["tag"] == "officer_director_change"]),
            "ceo_change_events": len(ceo_changes),
            "cfo_change_events": len(cfo_changes)}


def ex97_on_file(cik, filings=None):
    """True if any 10-K filing index carries an EX-97 exhibit (clawback policy)."""
    filings = filings or fetch.recent_filings(cik)
    hits = []
    for f in filings:
        if f["form"] == "EX-97" or f["form"].startswith("EX-97"):
            hits.append({"date": f["date"], "accession": f["accession"],
                         "doc": f["doc"]})
    if hits:
        return hits
    for f in filings:
        if f["form"] != "10-K":
            continue
        try:
            idx = fetch.filing_index(cik, f["accession"])
        except Exception:
            continue
        for item in idx["directory"]["item"]:
            if re.search(r"ex-?97", item["name"], re.I):
                hits.append({"date": f["date"], "accession": f["accession"],
                             "doc": item["name"]})
    return hits


def ten_k_quality_flags(cik, filings=None):
    """material weakness / restatement mentions in the latest 10-K body."""
    filings = filings or fetch.recent_filings(cik)
    fk = next((f for f in filings if f["form"] == "10-K"), None)
    if not fk:
        return None
    try:
        txt = fetch.strip_tags(
            fetch.get_text(fetch.filing_url(cik, fk["accession"], fk["doc"])))
    except Exception:
        return None
    mw = [m.group(0)[:200] for m in
          re.finditer(r"[^.]{0,120}material weakness[^.]{0,160}\.", txt, re.I)][:6]
    rs = [m.group(0)[:200] for m in
          re.finditer(r"[^.]{0,100}restatement[^.]{0,160}\.", txt, re.I)][:6]
    return {"filing_date": fk["date"], "accession": fk["accession"],
            "material_weakness_mentions": len(mw), "material_weakness_samples": mw,
            "restatement_mentions": len(rs), "restatement_samples": rs}


def tenb51_plan_disclosure(cik, filings=None):
    """Rule 10b5-1 adoption/termination section of the latest 10-Q/10-K."""
    filings = filings or fetch.recent_filings(cik)
    for f in filings:
        if f["form"] not in ("10-Q", "10-K"):
            continue
        try:
            txt = fetch.strip_tags(
                fetch.get_text(fetch.filing_url(cik, f["accession"], f["doc"])))
        except Exception:
            continue
        m = re.search(r"(rule\s+10b5-1[^\n]{0,40}(trading\s+plans?)?)"
                      r"(.{0,2500}?)(item\s+\d|[a-z]{3,}\s+10b5-1\s*$)", txt, re.I | re.S)
        seg = None
        idx = txt.lower().find("10b5-1")
        if idx >= 0:
            seg = txt[max(0, idx - 300): idx + 2500]
        if seg:
            events_out = []
            for sent in re.split(r"(?<=[.;])\s+", seg):
                if not re.search(r"10b5-1|trading arrangement", sent, re.I):
                    continue
                if not re.search(r"adopt|terminat|modif", sent, re.I):
                    continue
                verb = "none" if re.match(r"\s*none of\b", sent, re.I) else \
                    (re.search(r"(adopted|terminated|modified)", sent, re.I) or [None, ""])[1].lower()
                # structured only: the consumer must never see filing prose
                events_out.append({"verb": verb, "form": f["form"],
                                   "filed": f["date"]})
                if len(events_out) >= 8:
                    break
            return {"filing": f["form"], "date": f["date"],
                    "adoption_events": events_out}
    return None


def build(cik, since="2019-01-01", filings=None):
    filings = filings or fetch.recent_filings(cik)
    events = event_stream(cik, since=since, filings=filings)
    return {
        "events": events,
        "tenure": tenure_stats(events),
        "clawback_ex97": ex97_on_file(cik, filings),
        "ten_k_quality": ten_k_quality_flags(cik, filings),
        "plan_10b5_1": tenb51_plan_disclosure(cik, filings),
    }
