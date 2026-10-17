"""Capital allocation + dilution series from SEC XBRL companyconcept (10-K/10-Q
as-filed annual facts). Pure official data, no estimation.
"""
from . import fetch

FLOWS = [
    ("us-gaap", "PaymentsForRepurchaseOfCommonStock", "buybacks", "out"),
    ("us-gaap", "PaymentsOfDividends", "dividends_paid", "out"),
    ("us-gaap", "PaymentsToAcquireBusinessesNetOfCashAcquired", "ma_spend", "out"),
    ("us-gaap", "PaymentsToAcquireProductiveAssets", "capex", "out"),
    ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "capex_alt", "out"),
    ("us-gaap", "ResearchAndDevelopmentExpense", "rnd", "out"),
    ("us-gaap", "RepaymentsOfLongTermDebt", "debt_repay", "out"),
    ("us-gaap", "ProceedsFromIssuanceOfLongTermDebt", "debt_issue", "in"),
    ("us-gaap", "ShareBasedCompensation", "sbc", "info"),
    ("us-gaap", "ProceedsFromStockOptionsExercised", "option_proceeds", "in"),
    ("us-gaap", "ProceedsFromIssuanceOfCommonStock", "stock_issued", "in"),
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "operating_cash", "info"),
    ("us-gaap", "NetIncomeLoss", "net_income", "info"),
    ("us-gaap", "GoodwillImpairmentLoss", "goodwill_impairment", "info"),
    ("us-gaap", "BusinessCombinationConsiderationTransferred1", "ma_consideration", "info"),
]
INSTANTS = [
    ("us-gaap", "CommonStockSharesOutstanding", "shares_outstanding"),
    ("us-gaap", "Goodwill", "goodwill"),
]


def _annual(d, want_instant=False):
    """-> {period_end: value} annual facts, 10-K preferred."""
    if not d:
        return {}
    out = {}
    for unit, facts in d.get("units", {}).items():
        for f in facts:
            if not want_instant and f.get("fp") != "FY":
                continue
            if want_instant and f.get("fp") != "FY":
                continue
            if f.get("form") not in ("10-K", "10-K/A"):
                if f["end"] in out and out[f["end"]].get("form") in ("10-K", "10-K/A"):
                    continue
            out[f["end"]] = {"value": f["val"], "form": f["form"],
                             "fy": f.get("fy"), "filed": f.get("filed"),
                             "accn": f.get("accn")}
    return dict(sorted(out.items()))


def share_pairs(cik):
    """YoY net share-count change computed WITHIN each 10-K (same split basis).

    Cross-filing year-over-year instants mix pre/post-split bases (TSLA FY2021
    looked +200% that way — a pure 3:1-split artifact), so pairs must come from
    the comparative columns of one filing.
    """
    d = fetch.companyconcept(cik, "us-gaap", "CommonStockSharesOutstanding")
    byaccn = {}
    if d:
        for _unit, facts in d.get("units", {}).items():
            for f in facts:
                if f.get("fp") == "FY" and f.get("form") in ("10-K", "10-K/A"):
                    byaccn.setdefault(f.get("accn"), []).append((f["end"], f["val"]))
    out = {}
    for lst in byaccn.values():
        lst.sort()
        for (e0, v0), (e1, v1) in zip(lst, lst[1:]):
            if v0 and int(e1[:4]) - int(e0[:4]) == 1:
                out[e1] = round(100.0 * (v1 - v0) / v0, 2)
    return out


def build(cik):
    series = {}
    for taxon, concept, key, _dir in FLOWS:
        got = _annual(fetch.companyconcept(cik, taxon, concept))
        dest = "capex" if key == "capex_alt" else key  # capex tags differ by issuer
        if dest in series:
            for end, v in got.items():
                series[dest].setdefault(end, v)
        else:
            series[dest] = got
    for taxon, concept, key in INSTANTS:
        series[key] = _annual(fetch.companyconcept(cik, taxon, concept),
                              want_instant=True)
    series["_share_pairs"] = share_pairs(cik)
    derived = derive(series)
    return {"series": series, "derived": derived}


def derive(s):
    """Cross-series ratios that answer 'did buybacks offset SBC dilution'."""
    buy = {k: v["value"] for k, v in s.get("buybacks", {}).items()}
    sbc = {k: v["value"] for k, v in s.get("sbc", {}).items()}
    sh = {k: v["value"] for k, v in s.get("shares_outstanding", {}).items()}
    oc = {k: v["value"] for k, v in s.get("operating_cash", {}).items()}
    div = {k: v["value"] for k, v in s.get("dividends_paid", {}).items()}
    ma = {k: v["value"] for k, v in s.get("ma_spend", {}).items()}
    capex = {k: v["value"] for k, v in s.get("capex", {}).items()}
    rnd = {k: v["value"] for k, v in s.get("rnd", {}).items()}
    pairs = s.get("_share_pairs") or {}
    out = {}
    for yr in sorted(set(buy) | set(sbc) | set(sh)):
        row = {"year": yr}
        if yr in buy and sbc.get(yr):
            row["buyback_over_sbc"] = round(buy[yr] / sbc[yr], 2)
        if yr in buy and oc.get(yr):
            row["buyback_pct_of_ocf"] = round(100.0 * buy[yr] / oc[yr], 1)
        if yr in div and oc.get(yr):
            row["dividend_pct_of_ocf"] = round(100.0 * div[yr] / oc[yr], 1)
        if yr in ma and oc.get(yr):
            row["ma_pct_of_ocf"] = round(100.0 * ma[yr] / oc[yr], 1)
        if yr in capex and oc.get(yr):
            row["capex_pct_of_ocf"] = round(100.0 * capex[yr] / oc[yr], 1)
        if yr in rnd and oc.get(yr):
            row["rnd_pct_of_ocf"] = round(100.0 * rnd[yr] / oc[yr], 1)
        if yr in pairs:
            row["net_share_count_chg_pct"] = pairs[yr]
        out[yr] = row
    return out
