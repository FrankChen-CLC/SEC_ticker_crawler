"""Offline tests for secdata.proxy — DEF 14A executive intelligence.

No network: the pure parse functions (SCT table, PvP inline XBRL, promises
rules, events, capital, Form 4 XML) are fed synthetic fixtures; the flow
test monkeypatches default_client() with a fake serving a canned submissions
JSON + proxy HTML + press release, and asserts the out/proxy/ JSONL layout.
"""
import argparse
import json
import urllib.error

import pytest

from secdata import client as client_mod
from secdata import ticker as ticker_mod
from secdata import proxy
from secdata.proxy import fetch, report, incremental


# --------------------------------------------------------------------- hsoup
def test_hsoup_find_all_and_text():
    from secdata.proxy.hsoup import parse_html
    import re
    soup = parse_html("<div><p>A<b>B</b>C</p><table><tr><td>x</td></tr></table>"
                      "<H2>head</H2></div>")
    assert [e.name for e in soup.find_all("p")] == ["p"]
    assert [e.get_text() for e in soup.find_all("p")] == ["ABC"]
    assert [e.get_text("\n") for e in soup.find_all("p")] == ["A\nB\nC"]
    assert [e.name for e in soup.find_all(re.compile(r"^h[1-6]$"))] == ["h2"]
    # nested tables: inner tr visible from outer (bs4 semantics preserved)
    outer = soup.find_all("table")[0]
    assert len(outer.find_all("tr")) == 1
    # get_text(strip=True) drops empty runs, joins with sep
    assert soup.find_all("p")[0].get_text(" ", strip=True) == "A B C"


def test_hsoup_toleration():
    from secdata.proxy.hsoup import parse_html
    soup = parse_html("<table><td>a</td><td>b</td></table></font></p>")
    trs = soup.find_all("tr")            # missing <tr> invented, lxml-style
    assert len(trs) == 1 and len(trs[0].find_all(["td", "th"])) == 2


# ------------------------------------------------------------------- SCT
SCT_HTML = """
<html><body>
<table>
<tr><th>Name and Principal Position</th><th>Year</th><th>Salary ($)</th>
<th>Bonus ($)</th><th>Stock Awards ($)</th><th>Option Awards ($)</th>
<th>Non-Equity Incentive Plan Compensation ($)</th>
<th>All Other Compensation ($)</th><th>Total ($)</th></tr>
<tr><td rowspan="3">Jane Doe<br/>Chief Executive Officer</td>
<td>2024</td><td>1,500,000</td><td>0</td><td>9,000,000</td><td>0</td>
<td>2,100,000</td><td>140,000</td><td>12,740,000</td></tr>
<tr><td>2023</td><td>1,450,000</td><td>0</td><td>8,000,000</td><td>0</td>
<td>1,900,000</td><td>130,000</td><td>11,480,000</td></tr>
<tr><td>2022</td><td>1,400,000</td><td>$0</td><td>7,000,000</td><td>(500,000)</td>
<td>0</td><td>120,000</td><td>8,520,000</td></tr>
</table></body></html>"""


def test_summary_compensation_table_rowspan():
    rows = report.proxy14a.summary_compensation_table(SCT_HTML)
    assert len(rows) == 3
    assert all(r["name"] == "Jane Doe" for r in rows)   # continuation rows re-attached
    assert rows[0]["title"] == "Chief Executive Officer"
    assert rows[0]["salary"] == 1500000.0
    assert rows[0]["total"] == 12740000.0
    assert rows[2]["option_awards"] == -500000.0        # (500,000) negative
    assert rows[0]["mix"]["stock_awards"] == pytest.approx(70.6, abs=0.1)


def test_summary_compensation_table_hidden_layer():
    # XOM-2026 shape: SCT as a page image, data in one flattened <p>
    def rec(name, year, vals, total):
        cols = " ".join(str(v) for v in vals)
        return f"{name}, Chief Executive Officer {year} {cols} {total}"
    rows = [
        rec("John A. Smith", 2024, [1500000, 0, 9000000, 0, 2100000, 0, 140000],
            12740000),                                   # 7 value cols -> _SCT7
        rec("John A. Smith", 2023, [1450000, 0, 8000000, 0, 1900000, 0, 130000],
            11480000),
        rec("Jane Q. Roe", 2024, [900000, 0, 3000000, 0, 700000, 80000],
            4680000),                                    # 6 value cols -> _SCT6
    ]
    # the filler before the first row must END on a non-alphabetic token: the
    # name walk scans backward through the alpha run, and in a real image layer
    # the previous numeric cell stops it
    doc = ("<html><body><p>"
           "The following Summary Compensation Table sets forth the compensation "
           "earned, including Salary, Bonus and Stock Awards columns for the "
           "named executive officers as required by the SEC rules in this proxy. "
           "Additional filler text pads the paragraph beyond the four hundred "
           "character threshold used by the recovery heuristic for image tables 1 "
           + " ".join(rows) + "</p></body></html>")
    out = report.proxy14a.summary_compensation_table(doc)
    assert len(out) >= 3
    assert {r["name"] for r in out} >= {"John A. Smith", "Jane Q. Roe"}
    sm = next(r for r in out if r["name"] == "John A. Smith" and r["year"] == 2024)
    assert sm["salary"] == 1500000 and sm["total"] == 12740000
    assert sm["non_equity"] == 2100000                  # 7-column mapping
    roe = next(r for r in out if r["name"] == "Jane Q. Roe")
    assert roe["option_awards"] is None and roe["all_other"] == 80000  # 6-col


# ------------------------------------------------------------------- PvP
PVP_DOC = """
<html><body>
<script type="text/javascript">ignore</script>
<xbrl xmlns="http://www.xbrl.org/2003/instance">
<xbrli:context id="FY2024"><xbrli:entity><xbrli:identifier>x</xbrli:identifier>
</xbrli:entity><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>
<xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="FY2023"><xbrli:entity><xbrli:identifier>x</xbrli:identifier>
</xbrli:entity><xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate>
<xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period></xbrli:context>
<div>
<ix:nonNumeric xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2024" name="ecd:PeoName">Jane Doe</ix:nonNumeric>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2024" name="ecd:PeoTotalCompAmt" scale="3" format="ixt:num-dot-decimal"
  decimals="-5">12,740</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2024" name="ecd:PeoActuallyPaidCompAmt" scale="3" decimals="-5"
  >15,000</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2024" name="ecd:TotalShareholderRtnAmt" decimals="3">230.0</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2024" name="ecd:PeerGroupTotalShareholderRtnAmt" decimals="3">180.0</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2024" name="ecd:NonPeoNeoAvgCompActuallyPaidAmt" scale="3"
  decimals="-5">6,000</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2023" name="ecd:PeoTotalCompAmt" scale="3" decimals="-5">11,480</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2023" name="ecd:PeoActuallyPaidCompAmt" scale="3" decimals="-5">9,000</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2023" name="ecd:TotalShareholderRtnAmt" decimals="3">180.0</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2023" name="ecd:PeerGroupTotalShareholderRtnAmt" decimals="3">200.0</ix:nonFraction>
<ix:nonFraction xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2023" name="ecd:NonPeoNeoAvgCompActuallyPaidAmt" scale="3"
  decimals="-5">5,000</ix:nonFraction>
<ix:nonNumeric xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2024" name="ecd:MeasureName">Net income</ix:nonNumeric>
<ix:nonNumeric xmlns:ix="http://www.sec.gov/Archives/edgar/data"
  contextRef="FY2023" name="ecd:MeasureName">TSR</ix:nonNumeric>
</div></xbrl></body></html>"""


def test_pvp_table_years_and_peo():
    pvp = report.proxy14a.pvp_table("123456", doc=PVP_DOC,
                                    filing={"accession": "a-1", "date": "2026-04-01"})
    assert pvp["peo_name"] == "Jane Doe"
    years = [y["fy"] for y in pvp["years"]]
    assert years == [2024, 2023]                      # newest first
    y24 = pvp["years"][0]
    assert y24["peo_sct_total"] == 12740000.0          # scale="3" applied
    assert y24["peo_comp_actually_paid"] == 15000000.0
    assert y24["tsr_index"] == 230.0 and y24["peer_tsr_index"] == 180.0
    assert y24["neo_avg_comp_actually_paid"] == 6000000.0
    assert set(pvp["measures"]) == {"Net income", "TSR"}


# ------------------------------------------------------------------- sections
RP_DOC = """
<html><body>
<h2>Certain Relationships and Related Transactions</h2>
<p>The Audit Committee reviewed and approved the related party policy.
In 2024 the Company paid $12 million to a director-affiliated firm.
In addition $4.5 million of expenses were reimbursed under the arrangement.</p>
<h3>Proposal 2</h3><p>unrelated</p>
</body></html>"""


def test_related_party_section():
    rp = report.proxy14a.related_party_section(RP_DOC)
    assert rp and rp["review_policy_stated"] is True
    assert rp["n_amount_disclosures"] == 2
    assert rp["records"][0]["amount_usd"] == 12000000.0
    assert rp["total_usd"] == 16500000.0


MW_DOC = """
<table><tr><th>Metric</th><th>Weight</th></tr>
<tr><td>Revenue growth</td><td>50%</td></tr>
<tr><td>Adjusted EPS</td><td>30%</td></tr>
<tr><td>Relative TSR</td><td>20%</td></tr></table>"""


def test_metric_weight_tables():
    mw = report.proxy14a.metric_weight_tables(MW_DOC)
    assert len(mw) == 1 and mw[0]["sum"] == 100.0
    assert mw[0]["weights"][0]["weight_pct"] == 50.0


# ------------------------------------------------------------------- promises
def test_extract_promises_quantified():
    rel = {"date": "2026-02-01", "accession": "a", "exhibit": "e",
           "text": ("We had a strong quarter. We expect full year 2026 revenue "
                    "to be approximately $8.5 billion, reflecting continued "
                    "momentum across the business.")}
    ps = report.promises.extract_promises(rel)
    assert len(ps) == 1
    p = ps[0]
    assert p["kind"] == "quantified" and p["fy"] == 2026
    assert p["metric"] == "revenue" and p["direction"] == "~"
    assert p["dollar"] == 8.5e9
    assert "8.5" in p["summary"] and p["status"] == "unparsed"


def test_extract_promises_noise_and_retrospective_dropped():
    rel = {"date": "2026-02-01", "accession": "a", "exhibit": "e",
           "text": ("We expect the new awards to vest over three years under "
                    "the equity plan. Strong margins in the period allowed us "
                    "to exceed our CET1 target of 13 percent in the year. "
                    "Our prior guidance excluded deferred revenue from the "
                    "reported figures, so results may differ from expectations.")}
    assert report.promises.extract_promises(rel) == []


def test_extract_promises_qualitative():
    rel = {"date": "2026-02-01", "accession": "a", "exhibit": "e",
           "text": ("We plan to begin production of the new Megapack 3 next "
                    "year at the expanded factory.")}
    ps = report.promises.extract_promises(rel)
    assert len(ps) == 1
    assert ps[0]["kind"] == "qualitative" and ps[0]["status"] == "tracked"
    assert ps[0]["metric"] == "production"


# ------------------------------------------------------------------- events
def _fake_get_text(monkeypatch, text):
    monkeypatch.setattr(fetch, "get_text", lambda url, **kw: text)


def test_event_stream_and_tenure(monkeypatch):
    filings = [
        {"form": "8-K", "accession": "0001-24", "date": "2024-05-01",
         "doc": "a.htm", "items": "5.02,9.01", "reportDate": ""},
        {"form": "8-K", "accession": "0001-25", "date": "2025-01-10",
         "doc": "b.htm", "items": "2.01", "reportDate": ""},
        {"form": "8-K", "accession": "0001-26", "date": "2025-02-01",
         "doc": "c.htm", "items": "9.01", "reportDate": ""},
    ]
    _fake_get_text(monkeypatch, "<html>Jane Doe has been appointed Chief "
                                "Executive Officer of the Company effective June.</html>")
    evs = report.events.event_stream("1", filings=filings, since="2024-01-01")
    assert [e["item"] for e in evs] == ["2.01", "5.02"]   # date desc; 9.01 not tagged
    ev502 = next(e for e in evs if e["item"] == "5.02")
    assert ev502["detail"][0]["who"] == "Jane Doe"
    assert ev502["detail"][0]["verb"] == "appointed"
    st = report.events.tenure_stats(evs)
    assert st == {"n_502_events": 1, "ceo_change_events": 1, "cfo_change_events": 0}


def test_incremental_relevant_forms():
    assert incremental.relevant("4") and incremental.relevant("4/A")
    assert incremental.relevant("DEF 14A") and incremental.relevant("8-K/A")
    assert incremental.relevant("10-K")
    assert not incremental.relevant("424B2")       # exact "4" only
    assert not incremental.relevant("S-1")


# ------------------------------------------------------------------- insiders
FORM4_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument>
 <periodOfReport>2026-06-01</periodOfReport>
 <documentType>X</documentType>
 <aff10b5One>1</aff10b5One>
 <reportingOwner>
  <reportingOwnerId><rptOwnerCik>0000999888</rptOwnerCik>
   <rptOwnerName>Doe, Jane</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><isDirector>0</isDirector>
   <isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner>
   <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
 </reportingOwner>
 <nonDerivativeTable>
  <nonDerivativeTransaction>
   <securityTitle><value>Common Stock</value></securityTitle>
   <transactionDate><value>2026-06-01</value></transactionDate>
   <transactionCoding><transactionCode>S</transactionCode>
    <equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
   <transactionAmounts><transactionShares><value>5,000</value></transactionShares>
    <transactionPricePerShare><value>210.50</value></transactionPricePerShare>
    <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
   </transactionAmounts>
   <postTransactionAmounts><sharesOwnedFollowingTransaction><value>100,000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
   <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
  </nonDerivativeTransaction>
  <nonDerivativeTransaction>
   <securityTitle><value>Common Stock</value></securityTitle>
   <transactionDate><value>2026-06-15</value></transactionDate>
   <transactionCoding><transactionCode>F</transactionCode>
    <equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
   <transactionAmounts><transactionShares><value>1,000</value></transactionShares>
    <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
   </transactionAmounts>
   <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
  </nonDerivativeTransaction>
 </nonDerivativeTable>
</ownershipDocument>"""


def test_parse_form4_and_classify():
    parsed = report.insiders.parse_form4_xml(FORM4_XML)
    assert parsed["rule_10b5_1"] is True
    assert parsed["insider"]["is_officer"] and parsed["insider"]["title"] == \
        "Chief Executive Officer"
    t0, t1 = parsed["trades"]
    assert t0["bucket"] == "open_market_sell" and t0["price"] == 210.5
    assert t0["shares"] == 5000.0 and t0["shares_after"] == 100000.0
    assert t1["bucket"] == "tax_withholding"
    # classify: wide (clock-independent) trailing window; $ only for open-market P/S
    s = report.insiders.classify_summary(parsed["trades"], days=5000)
    sell = s["open_market_sell_10b5_1"]
    assert sell["count"] == 1 and sell["net_shares"] == -5000.0
    assert sell["net_value"] == pytest.approx(-1052500.0)
    assert s["tax_withholding_10b5_1"]["net_value"] is None  # no fabricated $


# ------------------------------------------------------------------- capital
def test_capital_annual_and_derive():
    d = {"units": {"USD": [
        {"fp": "FY", "form": "10-K", "end": "2024-12-31", "val": 5.0e9,
         "fy": 2024, "filed": "2025-02-01", "accn": "a1"},
        {"fp": "Q1", "form": "10-Q", "end": "2024-03-31", "val": 1.0e9},
        {"fp": "FY", "form": "10-Q", "end": "2023-12-31", "val": 4.0e9},
    ]}}
    ann = report.capital._annual(d)
    assert set(ann) == {"2024-12-31", "2023-12-31"}   # quarterly fact dropped
    series = {
        "buybacks": {"2024-12-31": {"value": 1000.0}},
        "sbc": {"2024-12-31": {"value": 500.0}},
        "operating_cash": {"2024-12-31": {"value": 4000.0}},
        "dividends_paid": {"2024-12-31": {"value": 200.0}},
        "shares_outstanding": {"2024-12-31": {"value": 900.0}},
        "_share_pairs": {"2024-12-31": -1.23},
    }
    der = report.capital.derive(series)
    row = der["2024-12-31"]
    assert row["buyback_over_sbc"] == 2.0
    assert row["buyback_pct_of_ocf"] == 25.0
    assert row["dividend_pct_of_ocf"] == 5.0
    assert row["net_share_count_chg_pct"] == -1.23


# ------------------------------------------------------------------- report agg
def _payload_stub():
    p = {
        "symbol": "ACME", "cik": "123456",
        "built_at": "2026-10-17 09:00:00", "build_seconds": 1.0,
        "proxy": {
            "filing": {"accession": "0001230001-26-000001", "date": "2026-04-01",
                       "doc": "proxy.htm"},
            "pvp": {"peo_name": "Jane Doe", "measures": ["TSR"],
                    "years": [
                        {"fy": 2024, "peo_sct_total": 12740000,
                         "peo_comp_actually_paid": 15000000, "tsr_index": 230,
                         "peer_tsr_index": 180, "neo_avg_comp_actually_paid": None,
                         "neo_avg_sct_total": None, "net_income": None,
                         "company_selected_measure": None,
                         "company_selected_measure_value": None},
                        {"fy": 2023, "peo_sct_total": 11480000,
                         "peo_comp_actually_paid": 9000000, "tsr_index": 180,
                         "peer_tsr_index": 200, "neo_avg_comp_actually_paid": None,
                         "neo_avg_sct_total": None, "net_income": None,
                         "company_selected_measure": None,
                         "company_selected_measure_value": None}],
                    "notes": []},
            "sct": [{"name": "Jane Doe", "title": "CEO", "year": 2024,
                     "salary": 1500000, "bonus": 0, "stock_awards": 9000000,
                     "option_awards": 0, "non_equity": 2100000,
                     "pension": 0, "all_other": 140000, "total": 12740000,
                     "mix": {"salary": 11.8}},
                    {"name": "Jane Doe", "title": "CEO", "year": 2023,
                     "salary": 1450000, "bonus": 0, "stock_awards": 8000000,
                     "option_awards": 0, "non_equity": 1900000,
                     "pension": 0, "all_other": 130000, "total": 11480000,
                     "mix": {"salary": 12.6}},
                    {"name": "Sam Bass", "title": "CFO", "year": 2023,
                     "salary": 900000, "bonus": 0, "stock_awards": 3000000,
                     "option_awards": 0, "non_equity": 700000,
                     "pension": 0, "all_other": 80000, "total": 4680000,
                     "mix": {}}],
            "related_party": {"records": [{"amount_usd": 12000000,
                                           "n_amounts": 1, "year": 2024}],
                              "total_usd": 12000000,
                              "n_amount_disclosures": 1,
                              "review_policy_stated": True},
            "metric_weights": [{"weights": [{"metric": "TSR", "weight_pct": 100.0}],
                                "sum": 100.0}],
        },
        "insiders": {"n_forms": 2, "positions": [], "trades": [],
                     "activity_12m": {}, "parse_errors": []},
        "events": {"events": [], "tenure": {"n_502_events": 0,
                                            "ceo_change_events": 0,
                                            "cfo_change_events": 0},
                   "clawback_ex97": [], "ten_k_quality": None,
                   "plan_10b5_1": None},
        "capital": {"series": {"buybacks": {"2024-12-31": {"value": 1000.0}}},
                    "derived": {"2024-12-31": {"year": "2024-12-31",
                                               "buyback_over_sbc": 2.0}}},
        "promises": {"promises": [{"date": "2026-02-01", "summary": "revenue ~ $8.5B (FY2026)",
                                   "kind": "quantified", "anchor": "FY2026",
                                   "direction": "~", "metric": "revenue",
                                   "dollar": 8.5e9, "pct": None, "fy": 2026,
                                   "status": "pending"}],
                     "n_releases_matched": 3, "n_releases_scanned": 2,
                     "scorecard": {"scored": 0, "met": 0, "missed": 0,
                                   "tracked": 1, "quantified": 1,
                                   "qualitative": 0, "bare": 0,
                                   "hit_rate": None}},
        "analysis": {"pvp_alignment": [], "comp_latest": [],
                     "pvp_measures": ["TSR"]},
        "audit": {}, "module_failures": {},
    }
    # build() fills audit via audit_of; emit() trusts the payload, so mirror it
    p["audit"] = report.audit_of(p["proxy"], p["insiders"], p["events"],
                                 p["capital"], p["promises"])
    return p


def test_report_aggregation_functions():
    p = _payload_stub()
    al = report.pvp_alignment(p["proxy"]["pvp"])
    assert len(al) == 1 and al[0]["fy"] == 2024
    assert al[0]["cap_delta"] == 6000000 and al[0]["tsr_delta"] == 50
    assert al[0]["aligned"] is True          # both deltas >= 0
    mix = report.latest_comp_mix(p["proxy"]["sct"])
    assert [m["name"] for m in mix] == ["Jane Doe", "Sam Bass"]
    assert mix[0]["year"] == 2024            # latest FY per exec
    au = report.audit_of(p["proxy"], p["insiders"], p["events"],
                         p["capital"], p["promises"])
    assert au["sct"]["rows_parsed"] == 3 and au["sct"]["executives"] == 2
    assert au["pvp_xbrl"]["rows_complete_pct"] == 100.0
    assert au["def14a"]["found"] is True
    assert au["promises"]["harvest_rate_pct"] == pytest.approx(66.7)


def test_emit_families():
    p = _payload_stub()
    p["analysis"]["comp_latest"] = report.latest_comp_mix(p["proxy"]["sct"])
    p["analysis"]["pvp_alignment"] = report.pvp_alignment(p["proxy"]["pvp"])
    fam = report.emit(p)
    assert set(fam) == {"executives", "comp", "promises", "events",
                        "capital", "insiders", "builds"}
    assert len(fam["executives"]) == 2
    peo = next(e for e in fam["executives"] if e["name"] == "Jane Doe")
    assert peo["is_peo"] is True and peo["latest_fy"] == 2024
    kinds = {r["kind"] for r in fam["comp"]}
    assert kinds == {"sct", "pvp", "pvp_alignment", "metric_weights",
                     "related_party"}
    assert fam["capital"][0]["end"] == "2024-12-31"
    assert fam["capital"][0]["buybacks"] == 1000.0
    assert fam["capital"][0]["buyback_over_sbc"] == 2.0   # derived merged
    assert fam["promises"][0]["status"] == "pending"
    assert fam["builds"][0]["audit"]["sct"]["rows_parsed"] == 3


def test_merge_upsert(tmp_path):
    rows1 = [{"symbol": "ACME", "v": 1}, {"symbol": "OTHER", "v": 9}]
    proxy._merge(tmp_path / "comp.jsonl", {"ACME"}, rows1)
    rows2 = [{"symbol": "ACME", "v": 2}]
    proxy._merge(tmp_path / "comp.jsonl", {"ACME"}, rows2)
    out = list(__import__("secdata.storage", fromlist=["x"]).read_jsonl(
        tmp_path / "comp.jsonl"))
    assert out == [{"symbol": "OTHER", "v": 9}, {"symbol": "ACME", "v": 2}]


# ------------------------------------------------------------------- flow
CIK = "123456"
C10 = "0000123456"
SUB_URL = f"https://data.sec.gov/submissions/CIK{C10}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PROXY_DOC = ("https://www.sec.gov/Archives/edgar/data/123456/"
             "000123000126000001/proxy.htm")
IDX8K = ("https://www.sec.gov/Archives/edgar/data/123456/"
         "000123000126000002/index.json")
PR99 = ("https://www.sec.gov/Archives/edgar/data/123456/"
        "000123000126000002/ex99-1.htm")
F4 = ("https://www.sec.gov/Archives/edgar/data/123456/"
      "000123000126000003/xy.xml")

SUBS = {
    "filings": {"recent": {
        "accessionNumber": ["0001230001-26-000001", "0001230001-26-000002",
                            "0001230001-26-000003"],
        "form": ["DEF 14A", "8-K", "4"],
        "filingDate": ["2026-04-01", "2026-02-01", "2026-06-20"],
        "reportDate": ["", "", ""],
        "primaryDocument": ["proxy.htm", "a8k.htm", "xy.xml"],
        "items": ["", "2.02", ""],
    }, "files": []},
    "name": "Acme Widgets Inc.", "cik": 123456,
}
# the real inline-XBRL proxy carries the contexts + full PvP facts, so embed
# the PVP_DOC block (hsoup tolerates the nested <html> wrappers, and
# parse_ix_facts/parse_contexts work by regex over the raw doc either way)
PROXY_HTML = ("<html><body><h1>Notice of Meeting</h1>" + PVP_DOC +
              SCT_HTML + "</body></html>")
RELEASE = ("<html>We report record Q4 results. We expect full year 2027 "
           "revenue to be approximately $9.2 billion.</html>")


class FakeClient:
    def __init__(self):
        self.docs = {
            TICKERS_URL: json.dumps({"0": {"cik_str": 123456, "ticker": "ACME",
                                           "title": "Acme Widgets"}}),
            SUB_URL: json.dumps(SUBS),
            PROXY_DOC: PROXY_HTML,
            IDX8K: json.dumps({"directory": {"item": [
                {"name": "a8k.htm"}, {"name": "ex99-1.htm"},
                {"name": "R1.htm"}]}}),
            PR99: RELEASE,
            F4: FORM4_XML,
        }
        self.hits = []

    def _get(self, url, max_age_hours=None):
        self.hits.append(url)
        if url.startswith("https://data.sec.gov/api/xbrl/companyconcept/"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if url not in self.docs:
            raise KeyError(f"unmapped url {url}")
        return self.docs[url]

    def get_text(self, url, max_age_hours=None):
        return self._get(url)

    def get_json(self, url, max_age_hours=None):
        return json.loads(self._get(url))

    def get_bytes(self, url, max_age_hours=None):
        return self._get(url).encode("utf-8")


def _flow_args(tmp_path, monkeypatch, fake, argv):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path))
    monkeypatch.setattr(client_mod, "default_client", lambda: fake)
    monkeypatch.setattr(fetch, "default_client", lambda: fake)
    monkeypatch.setattr(ticker_mod, "default_client", lambda: fake)
    monkeypatch.setattr(ticker_mod, "_MAP", None)          # per-test ticker cache
    monkeypatch.setattr(proxy, "default_client", lambda: fake)
    ap = argparse.ArgumentParser()
    proxy.add_arguments(ap)
    return ap.parse_args(argv)


def test_run_single_symbol_flow(tmp_path, monkeypatch):
    fake = FakeClient()
    args = _flow_args(tmp_path, monkeypatch, fake, ["ACME"])
    rc = proxy.run(args)
    assert rc == 0
    root = tmp_path / "proxy"
    # capital.jsonl absent here: every companyconcept served 404 in the fake
    for fam in ("executives", "comp", "promises", "events",
                "insiders", "builds"):
        assert (root / f"{fam}.jsonl").exists(), fam
    execs = list(_read(root / "executives.jsonl"))
    assert {r["name"] for r in execs} == {"Jane Doe"}
    assert execs[0]["is_peo"] is True
    comp = list(_read(root / "comp.jsonl"))
    assert {r["kind"] for r in comp} >= {"sct", "pvp"}
    prom = [r for r in _read(root / "promises.jsonl") if r["kind"] == "promise"]
    assert prom[0]["fy"] == 2027 and prom[0]["status"] == "pending"
    ins = list(_read(root / "insiders.jsonl"))
    trades = [r for r in ins if r["kind"] == "trade"]   # date desc as coded
    assert [t["bucket"] for t in trades] == ["tax_withholding", "open_market_sell"]
    assert {t["trade_kind"] for t in trades} == {"common"}  # own kind stashed
    builds = list(_read(root / "builds.jsonl"))
    assert builds[0]["symbol"] == "ACME"
    assert builds[0]["module_failures"] == {}
    # checkpoint + state written
    assert (root / "checkpoint" / "state.json").exists()
    st = json.loads((root / "checkpoint" / "state.json").read_text())
    assert st["symbols"]["ACME"]["status"] == "ok"


def test_run_rebuild_upserts(tmp_path, monkeypatch):
    args = _flow_args(tmp_path, monkeypatch, FakeClient(), ["ACME"])
    assert proxy.run(args) == 0
    root = tmp_path / "proxy"
    before = len([r for r in _read(root / "comp.jsonl")
                  if r.get("kind") == "sct"])
    fake2 = FakeClient()
    fake2.docs[PROXY_DOC] = PROXY_HTML.replace("1,500,000", "1,600,000")
    args2 = _flow_args(tmp_path, monkeypatch, fake2, ["ACME"])
    assert proxy.run(args2) == 0
    sct = [r for r in _read(root / "comp.jsonl") if r.get("kind") == "sct"]
    assert len(sct) == before                    # replaced, not duplicated
    assert any(r["salary"] == 1600000.0 for r in sct)


def test_run_unknown_symbol_fails_gracefully(tmp_path, monkeypatch):
    args = _flow_args(tmp_path, monkeypatch, FakeClient(), ["NOPE"])
    assert proxy.run(args) == 1
    assert not (tmp_path / "proxy" / "builds.jsonl").exists()


def test_run_no_symbols_usage(tmp_path, monkeypatch):
    args = _flow_args(tmp_path, monkeypatch, FakeClient(), [])
    assert proxy.run(args) == 2


def test_module_contract():
    import importlib
    mod = importlib.import_module("secdata.proxy")
    assert mod.COMMAND == "proxy" and mod.HELP
    assert callable(mod.add_arguments) and callable(mod.run)


def _read(path):
    from secdata import storage
    return storage.read_jsonl(path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
