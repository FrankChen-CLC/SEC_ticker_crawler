"""Forms 3/4/5 parsing: insider transactions, classification, holdings series,
Rule 10b5-1 flags.

Transaction-code taxonomy (SEC Rule 16a-4 / Form 4 instructions):
  P/S open-market or private purchase/sale  -> DISCRETIONARY-ish (P/S are the
      only codes that express intent; a P/S inside a 10b5-1 plan is flagged by
      <aff10b5One>)
  M   exercise/conversion of derivative     -> derivative->equity, not a sale
  F   tax withholding (shares withheld)     -> passive
  G   gift                                  -> passive
  A   grant/award                           -> passive (comp)
  C   conversion of derivative              -> passive
  D   sale to issuer / disposition          -> issuer-side
  J   other (inter vivos transfer etc.)     -> other
  e   small acquisition (rule 16a-6)        -> other
"""
import xml.etree.ElementTree as ET
from collections import defaultdict

from . import fetch

CODE_BUCKET = {
    "P": "open_market_buy", "S": "open_market_sell",
    "M": "option_exercise", "C": "conversion",
    "F": "tax_withholding", "G": "gift", "A": "grant_award",
    "D": "sale_to_issuer", "J": "other", "e": "other",
    "I": "other", "X": "option_exercise",  # M+Sale pair codes handled below
}
PASSIVE = {"tax_withholding", "gift", "grant_award", "conversion",
           "option_exercise", "sale_to_issuer", "other"}


def _txt(el, path, default=""):
    n = el.find(path)
    if n is None:
        return default
    v = el.find(path + "/value")
    return (v.text or "").strip() if v is not None else (n.text or "").strip()


def _val(el, path):
    v = el.find(path + "/value")
    if v is None or not (v.text or "").strip():
        return None
    try:
        return float(v.text.strip().replace(",", ""))
    except ValueError:
        return None


def parse_form4_xml(xml_text):
    root = ET.fromstring(xml_text)
    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship")
    insider = {
        "cik": _txt(owner, "reportingOwnerId/rptOwnerCik"),
        "name": _txt(owner, "reportingOwnerId/rptOwnerName"),
        "is_director": _txt(owner, "reportingOwnerRelationship/isDirector") == "1",
        "is_officer": _txt(owner, "reportingOwnerRelationship/isOfficer") == "1",
        "is_10pct": _txt(owner, "reportingOwnerRelationship/isTenPercentOwner") == "1",
        "title": _txt(owner, "reportingOwnerRelationship/officerTitle"),
    }
    tenb5 = _txt(root, "aff10b5One") or _txt(root, "rule10b5One")
    trades = []

    def walk(table_tag, txn_tag, kind):
        tbl = root.find(table_tag)
        if tbl is None:
            return
        for txn in tbl.findall(txn_tag):
            code = _txt(txn, "transactionCoding/transactionCode")
            trades.append({
                "kind": kind,
                "security": _txt(txn, "securityTitle"),
                "date": _txt(txn, "transactionDate"),
                "code": code,
                "bucket": CODE_BUCKET.get(code, "other"),
                "in_10b5_1": tenb5 == "1",
                "shares": _val(txn, "transactionAmounts/transactionShares"),
                "price": _val(txn, "transactionAmounts/transactionPricePerShare"),
                "acq_disp": _txt(txn, "transactionAmounts/transactionAcquiredDisposedCode"),
                "shares_after": _val(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction"),
                "direct": _txt(txn, "ownershipNature/directOrIndirectOwnership") == "D",
                "nature": _txt(txn, "ownershipNature/natureOfOwnership"),
                "exercise_price": _val(txn, "derivativeInfo/derivativeTransactionCoding/derivativeTransactionCode") if kind == "derivative" else None,
            })

    walk("nonDerivativeTable", "nonDerivativeTransaction", "common")
    walk("nonDerivativeTable", "nonDerivativeHolding", "common_holding")
    walk("derivativeTable", "derivativeTransaction", "derivative")
    walk("derivativeTable", "derivativeHolding", "derivative_holding")
    return {
        "period_of_report": _txt(root, "periodOfReport"),
        "document_type": _txt(root, "documentType"),
        "insider": insider,
        "rule_10b5_1": tenb5 == "1",
        "trades": trades,
    }


def collect(cik, since="2021-01-01", filings=None, max_forms=400):
    """All Forms 3/4/5 since `since`. -> {trades:[...], positions:{...}}"""
    filings = filings or fetch.recent_filings(cik)
    forms = [f for f in filings
             if f["form"] in ("4", "3", "5") and f["date"] >= since][:max_forms]
    trades, positions, meta = [], {}, []
    for f in forms:
        # Form 4 primaryDocument carries an "xslF345X06/" prefix: that URL serves
        # the XSL-transformed HTML view, raw XML lives at accession/<basename>.
        doc = f["doc"].split("/")[-1]
        url = fetch.filing_url(cik, f["accession"], doc)
        try:
            parsed = parse_form4_xml(fetch.get_text(url))
        except Exception as e:
            meta.append({"accession": f["accession"], "error": str(e)})
            continue
        ins = parsed["insider"]
        for t in parsed["trades"]:
            row = dict(t)
            row.update({"filer_accession": f["accession"], "filing_date": f["date"],
                        "form": f["form"],
                        "insider_name": ins["name"], "insider_cik": ins["cik"],
                        "insider_title": ins["title"],
                        "is_director": ins["is_director"],
                        "is_officer": ins["is_officer"],
                        "is_10pct": ins["is_10pct"]})
            if t["kind"].endswith("holding"):
                key = (ins["name"], t["security"], t["direct"])
                cur = positions.get(key)
                if cur is None or f["date"] >= cur["as_of"]:
                    positions[key] = {"insider": ins["name"], "title": ins["title"],
                                      "security": t["security"],
                                      "shares": t["shares_after"],
                                      "direct": t["direct"], "as_of": f["date"]}
                continue
            trades.append(row)
            if t["shares_after"] is not None:
                key = (ins["name"], t["security"], t["direct"])
                cur = positions.get(key)
                if cur is None or f["date"] >= cur["as_of"]:
                    positions[key] = {"insider": ins["name"], "title": ins["title"],
                                      "security": t["security"],
                                      "shares": t["shares_after"],
                                      "direct": t["direct"], "as_of": f["date"]}
    trades.sort(key=lambda r: r["date"], reverse=True)
    return {"trades": trades, "positions": list(positions.values()),
            "n_forms": len(forms), "errors": meta}


def classify_summary(trades, days=365):
    """Bucketed net activity over the trailing window."""
    from datetime import date, timedelta
    if not trades:
        return {}
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    win = [t for t in trades if t["date"] >= cutoff and t["kind"] == "common"]
    out = defaultdict(lambda: {"count": 0, "shares": 0.0, "value": 0.0})
    for t in win:
        b = t["bucket"]
        if t["in_10b5_1"]:
            b = b + "_10b5_1"
        shares = t["shares"] or 0
        sign = 1 if t["acq_disp"] == "A" else -1
        out[b]["count"] += 1
        out[b]["shares"] += sign * shares
        # $ only meaningful for open-market P/S; grant/exercise "price" is a
        # strike or award-basis number and would fabricate huge notionals
        if t["price"] and b.split("_10b5_1")[0] in ("open_market_buy",
                                                    "open_market_sell"):
            out[b]["value"] += sign * shares * t["price"]
    return {k: {"count": v["count"], "net_shares": v["shares"],
                "net_value": round(v["value"], 2)
                if k.split("_10b5_1")[0] in ("open_market_buy", "open_market_sell")
                else None}
            for k, v in out.items()}
