"""Offline tests for secdata.holdings / secdata.holdings_parsers.

All fixtures are synthetic (small) — real-shaped SEC documents pasted
inline. No network: flows monkeypatch ``holdings.default_client``.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from secdata import holdings, holdings_parsers as parsers, storage  # noqa: E402

# ----------------------------------------------------------------- fixtures
NS13F = "http://www.sec.gov/edgar/document/thirteenf/informationtable"

SECDOC_13F = """<SEC-DOCUMENT>0001000134-24-000001-index.htm
CONFORMED PERIOD OF REPORT: 20240331
FORM TYPE: 13F-HR
<TEXT>
<DOCUMENT>
<TYPE>XML
<XML>
<filingSummary xmlns="urn:loc"><reportName>R1</reportName></filingSummary>
</XML>
</DOCUMENT>
<DOCUMENT>
<TYPE>XML
<XML>
\x00<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="%s">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>1,234</value>
    <shrsOrPrnAmt><sshPrnamt>890</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall></putCall>
    <investmentDiscretion>DFND</investmentDiscretion>
    <otherManager>1</otherManager>
    <votingAuthority><Sole>800</Sole><Shared>90</Shared><None>0</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>SPDR S&amp;P 500 ETF TRUST</nameOfIssuer>
    <titleOfClass>UNIT</titleOfClass>
    <cusip>78462x109</cusip>
    <value>5000</value>
    <shrsOrPrnAmt><sshPrnamt>15</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Call</putCall>
    <investmentDiscretion>DFND</investmentDiscretion>
    <votingAuthority><Sole>0</Sole><Shared>0</Shared><None>15</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>1,234</value>
    <shrsOrPrnAmt><sshPrnamt>890</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall></putCall>
    <investmentDiscretion>DFND</investmentDiscretion>
    <otherManager>1</otherManager>
    <votingAuthority><Sole>800</Sole><Shared>90</Shared><None>0</None></votingAuthority>
  </infoTable>
</informationTable>
</XML>
</TEXT>
</SEC-DOCUMENT>
""" % NS13F

NPORT_XML = """<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
 <headerData>
  <filedDate>2024-04-30</filedDate>
  <respondent><nameOfRegistrant>SKY TRUST II</nameOfRegistrant></respondent>
 </headerData>
 <primaryDocument><financialData>
  <repPdDate>2024-02-29</repPdDate>
  <fundSummary regId="S1">
   <regName>SKY TRUST II</regName>
   <seriesName>Sky Core 500 ETF</seriesName>
   <seriesId>C000045590</seriesId>
   <classId>C000122867</classId>
   <invstOrSecs>
    <invstOrSec>
      <name>APPLE INC</name><title>COM</title><cusip>037833100</cusip>
      <ticker>aapl</ticker><balance>1000.5</balance>
      <currency>USD</currency>
      <valUSD>100000000</valUSD><pctVal>1.2</pctVal>
      <payoffProfile>Long</payoffProfile><assetCat>Eqty</assetCat>
      <issuerCat>RF</issuerCat><invCountry>US</invCountry>
      <fairValLevel>1</fairValLevel>
    </invstOrSec>
    <invstOrSec>
      <name>SAP SE</name><isin>de0007164600</isin><balance>20000</balance>
      <currencyConditional curCd="EUR"><valUSD>5000000</valUSD><pctVal>0.05</pctVal></currencyConditional>
      <payoffProfile>Long</payoffProfile><assetCat>Eqty</assetCat>
      <issuerCat>RF</issuerCat><invCountry>DE</invCountry>
    </invstOrSec>
   </invstOrSecs>
  </fundSummary>
 </financialData></primaryDocument>
</edgarSubmission>"""

SECDOC_NPORT = ("<SEC-DOCUMENT>0001193125-24-098765.txt\n<TEXT>\n<DOCUMENT>\n"
                "<TYPE>XML\n<XML>\n" + NPORT_XML + "\n</XML>\n")

LEGACY_13F = """<DOCUMENT><TEXT>
FORM 13F INFORMATION TABLE

NAME OF ISSUER     TITLE OF CLASS    CUSIP        VALUE    SHARES  TYPE
-----------------------------------------------------------------------------
MICROSOFT CORP COM 595017109 100000 5000 SH 5000 0 0
IBM COM 459201101 250000 7500 SH 7000 500 0
</TABLE></TEXT></DOCUMENT>
"""

MASTER_IDX = """CIK|Company-Name|Form-Type|Filed-as-of-date|FileName
--------------------------------------------------------------------
  0001000134|BLACKROCK INC|13F-HR|2024-05-15|edgar/data/1000134/000100013424000001/0001000134-24-000001.txt
0001000134|BLACKROCK INC| 13F-HR/A |2024-06-01|edgar/data/1000134/000100013424000007/0001000134-24-000007.txt
0001105513|ISHARES TRUST|10-K|2024-03-01|edgar/data/1105513/000110465924012345/0001104659-24-012345.txt
0001067983|BURKITT INC|NPORT-P|2024-04-10|edgar/data/1067983/000119312524098765/0001193125-24-098765.txt
0001000134|BLACKROCK INC|13F-HR|2024-05-15|edgar/data/1000134/000100013424000001/0001000134-24-000001.txt
"""

META13 = {"accession": "0001000134-24-000001", "cik": "1000134",
          "cik10": "0001000134", "filer_name": "BlackRock, Inc.",
          "form_type": "13F-HR", "filing_date": "2024-05-15"}
METAN = {"accession": "0001193125-24-098765", "cik": "1067983",
         "cik10": "0001067983", "filer_name": "BURKITT INC",
         "form_type": "NPORT-P", "filing_date": "2024-04-10"}


def make_args(argv):
    p = argparse.ArgumentParser()
    holdings.add_arguments(p)
    return p.parse_args(argv)


def r13(cik, cusip, pc, period, val, shares):
    return {"accession": "A", "cik": cik, "cik10": cik.zfill(10),
            "filer_name": "HOLDER", "form_type": "13F-HR",
            "filing_date": period, "report_date": period,
            "name_of_issuer": cusip, "title_of_class": "COM", "cusip": cusip,
            "value_x1000": val, "shares_or_principal": shares,
            "shares_type": "SH", "put_call": pc, "vote_sole": shares,
            "vote_shared": 0, "vote_none": 0}


class FakeClient:
    """Serves the fixtures above for every URL the flows request."""

    def __init__(self):
        self.urls = []

    def get_bytes(self, url, max_age_hours=None):
        self.urls.append(url)
        if url.endswith("master.idx"):
            return MASTER_IDX.encode()
        if url.endswith(("master.gz", "master.zip")):
            raise RuntimeError("404")
        if url.endswith(".txt"):
            if "0001000134" in url:
                return SECDOC_13F.encode()
            return SECDOC_NPORT.encode()
        raise RuntimeError("unhandled " + url)

    def get_json(self, url, max_age_hours=None):
        self.urls.append(url)
        if "/submissions/" in url:
            return {"name": "BlackRock, Inc.", "filings": {"recent": {
                "accessionNumber": ["0001000134-24-000001"],
                "form": ["13F-HR"], "filingDate": ["2024-05-15"],
                "reportDate": ["2024-03-31"],
                "primaryDocument": ["infotable.xml"],
                "acceptanceDateTime": ["2024-05-15T17:00:00-04:00"],
            }}}
        if "company_tickers" in url:
            return {"0": {"cik_str": 32019, "ticker": "AAPL",
                          "title": "Apple Inc."},
                    "1": {"cik_str": 1640198, "ticker": "MSFT",
                          "title": "Microsoft Corporation"}}
        raise RuntimeError("unhandled " + url)

    def get_text(self, url, max_age_hours=None):
        return self.get_bytes(url, max_age_hours).decode()


# ------------------------------------------------------------------- helpers
def test_name_and_form_helpers():
    assert parsers.normalize_form(" 13f-hr /a ") == "13F-HR/A"
    assert parsers.clean_cik("0001000134") == "1000134"
    assert parsers.cik10("1000134") == "0001000134"
    assert parsers.accession_no_dash("0001000134-24-000001") == \
        "000100013424000001"
    assert parsers.extract_13f_report_date(
        "PERIOD OF REPORT: 20150331") == "2015-03-31"


def test_clean_issuer_name():
    assert parsers.clean_issuer_name(" BlackRock,   Inc. ") == "BLACKROCK"
    assert parsers.clean_issuer_name("APPLE INC") == "APPLE"
    assert parsers.clean_issuer_name(
        "SPDR S&P 500 ETF TRUST") == "SPDR S&P 500 ETF TRUST"
    # production strips ONE trailing suffix token (not iterative)
    assert parsers.clean_issuer_name("FOO HOLDINGS LLC") == "FOO HOLDINGS"
    assert parsers.clean_issuer_name("FOO CORP") == "FOO"


def test_parse_master_index():
    forms = {parsers.normalize_form(f) for f in parsers.DEFAULT_FORMS.split(",")}
    entries = parsers.parse_master_index(MASTER_IDX, forms, 2024, 1)
    assert len(entries) == 3  # header/dashes skipped, 10-K filtered, dup gone
    e = entries[0]
    assert e["accession"] == "0001000134-24-000001"
    assert e["cik"] == "1000134" and e["cik10"] == "0001000134"
    assert e["form_type"] == "13F-HR" and e["filing_date"] == "2024-05-15"
    assert e["accession_dir_url"].endswith("/1000134/000100013424000001/")
    assert e["full_submission_url"] == (
        "https://www.sec.gov/Archives/edgar/data/1000134/"
        "000100013424000001/0001000134-24-000001.txt")
    assert entries[1]["form_type"] == "13F-HR/A"
    assert entries[2]["form_type"] == "NPORT-P"


# --------------------------------------------------------------------- 13F
def test_parse_13f_documents():
    rows, rd, errs = parsers.parse_13f_documents(META13,
                                                 [("doc.txt", SECDOC_13F)])
    assert errs == []
    assert rd == "2024-03-31"
    assert len(rows) == 2          # third infoTable is an exact dupe
    a = rows[0]
    assert a["name_of_issuer"] == "APPLE INC"
    assert a["cusip"] == "037833100"
    assert a["value_x1000"] == 1234          # thousands, commas stripped
    assert a["shares_or_principal"] == 890
    assert a["shares_type"] == "SH"
    assert a["put_call"] == ""
    assert a["investment_discretion"] == "DFND"
    assert a["other_manager"] == "1"
    assert (a["vote_sole"], a["vote_shared"], a["vote_none"]) == (800, 90, 0)
    assert a["accession"] == "0001000134-24-000001"
    assert a["report_date"] == "2024-03-31"
    assert a["source_filename"] == "doc.txt"
    assert len(a["row_hash"]) == 64
    c = rows[1]
    assert c["cusip"] == "78462X109"         # upper-cased like production
    assert c["put_call"] == "Call"
    assert c["name_of_issuer"] == "SPDR S&P 500 ETF TRUST"


def test_parse_13f_bare_xml_and_legacy():
    xml = '<informationTable xmlns="%s"><infoTable><nameOfIssuer>IBM</nameOfIssuer>' \
          '<cusip>459201101</cusip><value>10</value><shrsOrPrnAmt>' \
          '<sshPrnamt>5</sshPrnamt></shrsOrPrnAmt><putCall></putCall>' \
          '<votingAuthority><Sole>5</Sole><Shared>0</Shared><None>0</None>' \
          "</votingAuthority></infoTable></informationTable>" % NS13F
    rows, rd, _errs = parsers.parse_13f_documents(META13, [("t.xml", xml)])
    assert len(rows) == 1 and rd is None     # no header -> no report date
    rows, rd, _errs = parsers.parse_13f_documents(META13,
                                                  [("old.txt", LEGACY_13F)])
    assert len(rows) == 2
    m = rows[0]
    assert m["name_of_issuer"] == "MICROSOFT CORP"
    assert m["title_of_class"] == "COM"
    assert m["cusip"] == "595017109"
    assert m["value_x1000"] == 100000 and m["shares_or_principal"] == 5000
    assert m["shares_type"] == "SH"
    assert (m["vote_sole"], m["vote_shared"], m["vote_none"]) == (5000, 0, 0)
    assert rows[1]["cusip"] == "459201101"
    assert (rows[1]["vote_sole"], rows[1]["vote_shared"],
            rows[1]["vote_none"]) == (7000, 500, 0)


def test_holding_hash_dedupe_semantics():
    base = {"accession": "X", "cusip": "1", "value_x1000": 5}
    other = {"accession": "X", "cusip": "1", "value_x1000": 6}
    assert parsers.holding_hash(base) == parsers.holding_hash(
        dict(base, created_at="2020-01-01", source_filename="f.xml"))
    assert parsers.holding_hash(base) != parsers.holding_hash(other)


# -------------------------------------------------------------------- N-PORT
def test_parse_nport_documents():
    rows, rd, por, errs = parsers.parse_nport_documents(
        METAN, [("doc.xml", SECDOC_NPORT)])
    assert errs == [] and rd == "2024-02-29"
    assert len(rows) == 2
    a = rows[0]
    assert a["registrant_name"] == "SKY TRUST II"
    assert a["series_name"] == "Sky Core 500 ETF"
    assert a["series_id"] == "C000045590" and a["class_id"] == "C000122867"
    assert a["report_date"] == "2024-02-29"
    assert a["investment_name"] == "APPLE INC"
    assert a["ticker"] == "AAPL"                     # upper-cased
    assert a["balance"] == 1000.5
    assert a["value_usd"] == 100000000.0
    assert a["pct_nav"] == 1.2
    assert a["currency"] == "USD"
    assert a["fair_value_level"] == "1"
    sap = rows[1]
    # regression: N-PORT carries non-USD currency as an *attribute*
    # (currencyConditional curCd) — production's tag-only lookup missed it
    assert sap["currency"] == "EUR"
    assert sap["value_usd"] == 5000000.0
    assert sap["pct_nav"] == 0.05
    assert sap["isin"] == "DE0007164600"
    assert sap["cusip"] == ""


# ------------------------------------------------------------------ compare
def test_diff_periods_qoq():
    rows = [
        r13("1000134", "037833100", "", "2024-03-31", 100, 10),
        r13("1000134", "594918104", "", "2024-03-31", 200, 20),
        r13("1000134", "594918104", "", "2024-06-30", 250, 20),
        r13("1000134", "67066G105", "", "2024-06-30", 30, 3),
        r13("1000134", "594918104", "Call", "2024-03-31", 5, 1),
        r13("1000134", "594918104", "CALL", "2024-06-30", 8, 2),
    ]
    out = parsers.diff_periods(rows)
    idx = {(r["security_id"], r["period_date"], r["put_call"] or ""): r
           for r in out}
    assert len(out) == 7          # 6 inputs + 1 synthesised exit
    assert idx[("037833100", "2024-03-31", "")]["status"] == "initial"
    exited = idx[("037833100", "2024-06-30", "")]
    assert exited["status"] == "exited"
    assert exited["previous_period_date"] == "2024-03-31"
    assert exited["value_usd_change"] == -100000.0   # -(100 * 1000)
    ch = idx[("594918104", "2024-06-30", "")]
    assert ch["status"] == "changed"
    assert ch["value_usd_change"] == 50000.0
    assert ch["previous_period_date"] == "2024-03-31"
    assert ch["holder_type"] == "institution_13f"
    assert ch["value_usd"] == 250000.0               # x1000 -> USD
    # option legs partition separately (never diff against the underlying)
    call = idx[("594918104", "2024-06-30", "CALL")]
    assert call["status"] == "changed"
    assert call["value_usd_change"] == 3000.0        # 8-5, not vs 250
    assert idx[("67066G105", "2024-06-30", "")]["status"] == "initial"


def test_diff_periods_filters():
    rows = [
        r13("1000134", "037833100", "", "2024-03-31", 100, 10),
        r13("1000134", "594918104", "Call", "2024-03-31", 5, 1),
        r13("1000134", "594918104", "", "2024-03-31", 200, 20),
    ]
    longs = parsers.diff_periods(rows, long_only=True)
    assert all((r["put_call"] or "") == "" for r in longs)
    assert len(longs) == 2
    after = parsers.diff_periods(rows, after="2024-06-01")
    assert after == []                                # nothing in that window
    single = parsers.diff_periods(rows, before="2024-03-31")
    assert len(single) == 3 and all(
        r["status"] == "initial" for r in single)


def test_diff_pairwise_institutions():
    rows = [
        r13("111", "037833100", "", "2024-03-31", 100, 10),
        r13("111", "037833100", "", "2024-03-31", 50, 5),   # 2nd line sums
        r13("111", "594918104", "", "2024-03-31", 200, 20),
        r13("222", "037833100", "", "2024-03-31", 150, 15),
        r13("222", "67066G105", "", "2024-03-31", 77, 7),
    ]
    out = parsers.diff_pairwise(rows, "111", "222")
    idx = {r["security_id"]: r for r in out}
    assert idx["037833100"]["status"] == "both"
    assert idx["037833100"]["value_usd_a"] == 150000.0   # GROUP BY SUM
    assert idx["037833100"]["value_delta"] == 0.0
    assert idx["594918104"]["status"] == "a_only"
    assert idx["594918104"]["value_usd_b"] is None
    assert idx["67066G105"]["status"] == "b_only"
    assert idx["67066G105"]["value_delta"] is None
    assert all(r["report_date"] == "2024-03-31" for r in out)
    assert out[0]["value_usd_a"] == 200000.0             # sorted by value desc
    assert parsers.diff_pairwise(rows, "111", "222",
                                 date="2023-12-31") == []


# ------------------------------------------------------------------ CLI flow
def test_parse_cli_and_rerun(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("SEC_EDGAR_OUT", str(out))
    f = tmp_path / "0001000134-24-000001.txt"
    f.write_text(SECDOC_13F, encoding="utf-8")
    args = make_args(["parse", str(tmp_path)])
    assert holdings.run(args) == 0
    rows = list(storage.read_jsonl(out / "holdings" / holdings.F_13F))
    assert len(rows) == 2
    assert rows[0]["accession"] == "0001000134-24-000001"  # from filename
    assert holdings.run(args) == 0                          # dedupe rerun
    rows = list(storage.read_jsonl(out / "holdings" / holdings.F_13F))
    assert len(rows) == 2


def test_download_targeted_flow(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("SEC_EDGAR_OUT", str(out))
    fake = FakeClient()
    monkeypatch.setattr(holdings, "default_client", lambda: fake)
    args = make_args(["download", "--cik", "1000134", "--workers", "1"])
    assert holdings.run(args) == 0
    root = out / "holdings"
    rows = list(storage.read_jsonl(root / holdings.F_13F))
    assert len(rows) == 2
    assert rows[0]["report_date"] == "2024-03-31"
    idx = list(storage.read_jsonl(root / holdings.F_INDEX))
    assert len(idx) == 1 and idx[0]["form_type"] == "13F-HR"
    chk = storage.Checkpoint(root / "checkpoint")
    assert chk.done() == {"0001000134-24-000001"}
    assert not chk.failed()
    assert any("submissions/CIK0001000134.json" in u for u in fake.urls)
    assert holdings.run(args) == 0        # resume: nothing pending
    assert len(list((root / "checkpoint" / "done.txt").read_text()
                    .splitlines())) == 1


def test_download_bulk_flow(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("SEC_EDGAR_OUT", str(out))
    fake = FakeClient()
    monkeypatch.setattr(holdings, "default_client", lambda: fake)
    args = make_args(["download", "--start-year", "2024", "--end-year", "2024",
                      "--end-quarter", "1", "--workers", "2"])
    assert holdings.run(args) == 0
    root = out / "holdings"
    r13 = list(storage.read_jsonl(root / holdings.F_13F))
    rn = list(storage.read_jsonl(root / holdings.F_NPORT))
    # the fixture serves the same 2-row table for BOTH 13F accessions
    assert len(r13) == 4 and len(rn) == 2
    assert {r["accession"] for r in r13} == {"0001000134-24-000001",
                                             "0001000134-24-000007"}
    assert rn[0]["registrant_name"] == "SKY TRUST II"
    assert rn[1]["currency"] == "EUR"
    idx = list(storage.read_jsonl(root / holdings.F_INDEX))
    assert len(idx) == 3                       # 10-K + dup filtered out
    assert (root / "checkpoint" / "quarters" / "done.txt").read_text() \
        .strip() == "2024Q1"
    chk = storage.Checkpoint(root / "checkpoint")
    assert chk.done() == {"0001000134-24-000001", "0001000134-24-000007",
                          "0001193125-24-098765"}
    # gz/zip were tried before falling back to master.idx
    assert any(u.endswith("master.gz") for u in fake.urls)
    assert any(u.endswith("master.idx") for u in fake.urls)
    assert holdings.run(args) == 0             # quarters done + all filings done
    assert len(list(storage.read_jsonl(root / holdings.F_13F))) == 4


def test_compare_cli(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("SEC_EDGAR_OUT", str(out))
    rows = [r13("111", "037833100", "", "2024-03-31", 100, 10),
            r13("111", "037833100", "", "2024-06-30", 150, 15)]
    src = out / "holdings" / holdings.F_13F
    storage.write_jsonl(src, rows)
    assert holdings.run(make_args(["compare"])) == 0
    diffs = list(storage.read_jsonl(out / "holdings" / "compare_13f.jsonl"))
    assert len(diffs) == 2
    assert diffs[-1]["value_usd_change"] == 50000.0
    assert diffs[-1]["previous_period_date"] == "2024-03-31"


def test_issuer_map(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("SEC_EDGAR_OUT", str(out))
    monkeypatch.setattr(holdings, "default_client", lambda: FakeClient())
    storage.write_jsonl(out / "holdings" / holdings.F_13F,
                        [{"name_of_issuer": "Apple Inc."},
                         {"name_of_issuer": "MICROSOFT CORPORATION"},
                         {"name_of_issuer": "N/A"},
                         {"name_of_issuer": "SOME UNKNOWN FUND LLC"}])
    assert holdings.run(make_args(["issuer-map"])) == 0
    mapped = {r["name_of_issuer"]: r["ticker"] for r in storage.read_jsonl(
        out / "holdings" / holdings.F_ISSUER)}
    assert mapped == {"Apple Inc.": "AAPL",
                      "MICROSOFT CORPORATION": "MSFT"}   # clean-name exact match
