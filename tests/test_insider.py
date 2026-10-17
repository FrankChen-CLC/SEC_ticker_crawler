"""Offline tests for secdata.insider — synthetic Form 4 XML only.

No network: pure functions are fed pasted fixtures; flow tests monkeypatch
the module-level default_client() with a fake that serves canned EFTS /
index.json / submission bytes and records exact URLs.
"""
import argparse
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from secdata import insider, storage, ticker

ADSH = "0001234567-26-000123"
ISSUER_CIK = "0000222233"
XML_DOC = "wk-form4_1756178799.xml"
ARCHIVE_DIR = "https://www.sec.gov/Archives/edgar/data/222233/000123456726000123"

FORM4_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ownershipDocument schemaVersion="1.01">
  <documentType>4</documentType>
  <periodOfReport>2024-06-12</periodOfReport>
  <filingDate>2024-06-15</filingDate>
  <issuer>
    <issuerCik>0000222233</issuerCik>
    <issuerName>Test Issuer Inc.</issuerName>
    <issuerTradingSymbol>TEST</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>33445566</rptOwnerCik>
      <rptOwnerName>Doe Jane</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>CFO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One></aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-01</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
        <footnoteId>1</footnoteId>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>10.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-02</value></transactionDate>
      <transactionCoding>
        <transactionCode>F</transactionCode>
        <footnoteId>2</footnoteId>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>50</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>4950</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Restricted Stock Unit</value></securityTitle>
      <transactionDate><value>2024-06-03</value></transactionDate>
      <transactionCoding>
        <transactionCode>A</transactionCode>
        <footnoteId>3</footnoteId>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5150</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-04</value></transactionDate>
      <transactionCoding><transactionCode>G</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5050</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-10</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>20.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>4050</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-11</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>850</value></transactionShares>
        <transactionPricePerShare><value>21.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>3200</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-05</value></transactionDate>
      <transactionCoding><transactionCode>X</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-06</value></transactionDate>
      <transactionCoding><transactionCode>J</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10</value></transactionShares>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Option to Purchase Common Stock</value></securityTitle>
      <conversionOrExercisePrice><value>1.00</value></conversionOrExercisePrice>
      <exerciseDate><value>2024-07-01</value></exerciseDate>
      <expirationDate><value>2031-06-01</value></expirationDate>
      <underlyingSecurity>
        <underlyingSecurityTitle><value>Common Stock</value></underlyingSecurityTitle>
        <underlyingSecurityShares><value>1000</value></underlyingSecurityShares>
      </underlyingSecurity>
      <transactionDate><value>2024-06-10</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>6000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </derivativeTransaction>
    <derivativeTransaction>
      <securityTitle><value>Option to Purchase Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-11</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </derivativeTransaction>
    <derivativeTransaction>
      <securityTitle><value>Option to Purchase Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-12</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </derivativeTransaction>
  </derivativeTable>
  <footnotes>
    <footnote id="1"><text>Purchased in the open market pursuant to a Rule 10b5-1 trading plan adopted on March 5, 2024.</text></footnote>
    <footnote id="2"><text>Shares withheld to satisfy tax withholding.</text></footnote>
    <footnote id="3"><text>Grant of annual equity award.</text></footnote>
  </footnotes>
</ownershipDocument>
"""

HOLDINGS_ONLY_XML = """<?xml version="1.0"?>
<ownershipDocument schemaVersion="1.01">
  <issuer><issuerName>T</issuerName><issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
  <nonDerivativeTable>
    <nonDerivativeHolding>
      <securityTitle><value>Common Stock</value></securityTitle>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>10</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeHolding>
  </nonDerivativeTable>
</ownershipDocument>
"""

NO_TICKER_XML = """<?xml version="1.0"?>
<ownershipDocument schemaVersion="1.01">
  <issuer><issuerName>T</issuerName></issuer>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-06-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>1</value></transactionShares></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def _trade_key(t):
    return (t["transaction_date"], t["table_type"], t["transaction_code"])


def _parsed():
    filing, trades = insider.parse_xml(ADSH, ISSUER_CIK, None, FORM4_XML.encode("utf-8"),
                                       ARCHIVE_DIR + "/" + XML_DOC)
    return filing, { _trade_key(t): t for t in trades }, trades


# ------------------------------------------------------------------- parser
def test_parse_xml_filing_fields():
    filing, _, trades = _parsed()
    assert filing == {
        "accession": ADSH,
        "rpt_owner_cik": "33445566",
        "issuer_cik": "222233",
        "issuer_ticker": "TEST",
        "issuer_name": "Test Issuer Inc.",
        "filing_date": "2024-06-15",     # no EFTS file_date → XML filingDate
        "period_of_report": "2024-06-12",
        "document_type": "4",
        "schema_version": "1.01",
        "aff10b5_one": None,             # empty element → None (not "")
        "xml_path": ARCHIVE_DIR + "/" + XML_DOC,
        "download_status": "downloaded",
    }
    assert len(trades) == 11


def test_parse_xml_file_date_priority_and_trade_fields():
    filing, by, _ = _parsed()
    f2, _ = insider.parse_xml(ADSH, ISSUER_CIK, "2026-08-26",
                              FORM4_XML.encode("utf-8"), "x")
    assert f2["filing_date"] == "2026-08-26"  # EFTS file_date wins
    p = by[("2024-06-01", "nonDerivative", "P")]
    assert p["accession"] == ADSH and p["rpt_owner_cik"] == "33445566"
    assert p["table_type"] == "nonDerivative"
    assert p["is_holding_only"] == 0
    assert p["security_title"] == "Common Stock"
    assert p["shares"] == 1000
    assert p["price_per_share"] == 10.5
    assert p["acquired_disposed"] == "A"
    assert p["shares_owned_after"] == 5000
    assert p["direct_indirect"] == "D"
    assert p["deemed_execution_date"] is None
    assert p["equity_swap_involved"] is None
    assert p["nature_of_ownership"] is None
    assert p["linked_footnote_ids"] == "1"
    assert p["issuer_ticker"] == "TEST" and p["issuer_cik"] == "222233"
    assert p["filing_date"] == "2024-06-15"
    m = by[("2024-06-10", "derivative", "M")]
    assert m["conversion_exercise_price"] == 1.0
    assert m["exercise_date"] == "2024-07-01"
    assert m["expiration_date"] == "2031-06-01"
    assert m["underlying_security_title"] == "Common Stock"
    assert m["underlying_security_shares"] == 1000
    assert m["price_per_share"] is None          # production quirk: "0" → None
    assert m["shares_owned_after"] == 6000


def test_parse_xml_classification_and_evidence_strings():
    _, by, _ = _parsed()
    p = by[("2024-06-01", "nonDerivative", "P")]
    assert p["primary_classification"] == "OPEN_MARKET_PURCHASE"
    assert p["classification_confidence"] == "HIGH"
    assert p["plan_status"] == "10B5_1"
    assert p["plan_adoption_date"] == "March 5, 2024"
    assert p["economic_reason"] is None
    assert p["signal_relevance"] is None
    assert p["evidence"] == ("footnote mentions 10b5-1/plan; plan adoption date "
                             "March 5, 2024; footnote open market; code=P")
    f = by[("2024-06-02", "nonDerivative", "F")]
    assert f["primary_classification"] == "TAX_WITHHOLDING"
    assert f["classification_confidence"] == "MEDIUM"
    assert f["evidence"] == "tax keyword; code=F"
    a = by[("2024-06-03", "nonDerivative", "A")]
    assert a["primary_classification"] == "RSU_OR_STOCK_AWARD"
    assert a["evidence"] == "RSU title/footnote; award/grant keyword; code=A"
    g = by[("2024-06-04", "nonDerivative", "G")]
    assert g["primary_classification"] == "GIFT"
    assert g["classification_confidence"] == "HIGH"
    assert g["evidence"] == "code=G"
    x = by[("2024-06-05", "nonDerivative", "X")]
    assert x["primary_classification"] == "UNCLASSIFIED"
    assert x["evidence"] == "code=X"
    j = by[("2024-06-06", "nonDerivative", "J")]
    assert j["primary_classification"] == "OTHER"
    assert j["classification_confidence"] == "LOW"
    assert j["evidence"] == "code=J see footnotes"


def test_footnoteid_attribute_form_does_not_link():
    """Real legacy EDGAR files serialize ``<footnoteId id="F1"/>`` (attribute).
    Production reads only the element *text* (preserved verbatim): with that
    serialization the footnote never links, so the row classifies by code alone."""
    xml = (
        '<?xml version="1.0"?><ownershipDocument schemaVersion="1.01">'
        '<issuer><issuerName>T</issuerName><issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>'
        '<nonDerivativeTable><nonDerivativeTransaction>'
        '<securityTitle><value>Common Stock</value></securityTitle>'
        '<transactionDate><value>2024-06-01</value></transactionDate>'
        '<transactionCoding><transactionCode>F</transactionCode>'
        '<footnoteId id="1"/></transactionCoding>'
        '<transactionAmounts><transactionShares><value>10</value></transactionShares>'
        '<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>'
        '</transactionAmounts></nonDerivativeTransaction></nonDerivativeTable>'
        '<footnotes><footnote id="1">'
        '<text>Shares withheld to satisfy tax withholding.</text></footnote></footnotes>'
        '</ownershipDocument>'
    )
    filing, trades = insider.parse_xml(ADSH, ISSUER_CIK, None, xml.encode(), "x")
    t = trades[0]
    assert t["linked_footnote_ids"] is None
    assert t["primary_classification"] == "TAX_OR_EXERCISE_PRICE_WITHHOLDING"
    assert t["evidence"] == "code=F"


def test_parse_xml_exercise_and_sell_grouping():
    _, by, _ = _parsed()
    s_full = by[("2024-06-10", "nonDerivative", "S")]
    m_full = by[("2024-06-10", "derivative", "M")]
    assert s_full["primary_classification"] == "EXERCISE_AND_SELL"
    assert s_full["economic_reason"] == "EXERCISE_AND_SELL"
    assert m_full["primary_classification"] == "EXERCISE_AND_SELL"
    assert m_full["economic_reason"] == "EXERCISE_AND_SELL"
    # ratio 0.85 → PARTIAL on both the S and the M row
    s_p = by[("2024-06-11", "nonDerivative", "S")]
    m_p = by[("2024-06-11", "derivative", "M")]
    assert s_p["primary_classification"] == "EXERCISE_AND_PARTIAL_SELL"
    assert s_p["economic_reason"] == "EXERCISE_AND_PARTIAL_SELL"
    assert m_p["economic_reason"] == "EXERCISE_AND_PARTIAL_SELL"
    # lone M (same accession, no same-day S) keeps its own class
    m_alone = by[("2024-06-12", "derivative", "M")]
    assert m_alone["primary_classification"] == "EXERCISE_OR_CONVERSION"
    assert m_alone["economic_reason"] is None


def test_parse_xml_holdings_only_and_no_ticker_return_none():
    assert insider.parse_xml(ADSH, ISSUER_CIK, None, HOLDINGS_ONLY_XML.encode(), "x") is None
    assert insider.parse_xml(ADSH, ISSUER_CIK, None, NO_TICKER_XML.encode(), "x") is None


# ---------------------------------------------------------------- classifier
def test_classify_trade_branches():
    base = {"transaction_code": "P", "acquired_disposed": "D", "security_title": ""}
    r = insider.classify_trade(base, "", None)
    assert r["primary_classification"] == "MARKET_OR_PRIVATE_PURCHASE"
    assert r["classification_confidence"] == "MEDIUM"   # P but ad != A
    s10b5 = insider.classify_trade(
        {"transaction_code": "S", "acquired_disposed": "D", "security_title": ""}, "", "true")
    assert s10b5["primary_classification"] == "10B5_1_PLANNED_SALE"
    assert s10b5["classification_confidence"] == "HIGH"
    assert s10b5["plan_status"] == "10B5_1"
    f_ex = insider.classify_trade(
        {"transaction_code": "F", "acquired_disposed": "D", "security_title": ""},
        "Payment of the exercise price in connection with option exercise.", None)
    assert f_ex["primary_classification"] == "EXERCISE_PRICE_PAYMENT"
    assert f_ex["evidence"] == "exercise price keyword; code=F"
    opt = insider.classify_trade(
        {"transaction_code": "A", "acquired_disposed": "A", "security_title": "Stock Option"},
        "", None)
    assert opt["primary_classification"] == "OPTION_GRANT"
    assert opt["evidence"] == "option title; code=A"
    vest = insider.classify_trade(
        {"transaction_code": "M", "acquired_disposed": "A", "security_title": ""},
        "Vested pursuant to award.", None)
    assert vest["primary_classification"] == "RSU_VESTING_OR_SETTLEMENT"
    assert vest["evidence"] == "vest/settle keyword; code=M"
    z = insider.classify_trade(
        {"transaction_code": "Z", "acquired_disposed": "D", "security_title": ""}, "", None)
    assert (z["primary_classification"], z["classification_confidence"], z["evidence"]) == \
        ("TRUST_TRANSFER", "HIGH", "code=Z")
    for code, cls in (("C", "CONVERSION"), ("D", "DISPOSITION_TO_ISSUER"),
                      ("I", "EMPLOYEE_PLAN_TRANSACTION"), ("K", "EQUITY_SWAP"),
                      ("U", "TENDER_DISPOSITION")):
        r = insider.classify_trade(
            {"transaction_code": code, "acquired_disposed": "D", "security_title": ""},
            "", None)
        assert r["primary_classification"] == cls
        assert r["evidence"] == f"code={code}"
    nul = insider.classify_trade({"transaction_code": None}, "", None)
    assert nul["primary_classification"] == "UNCLASSIFIED"
    assert nul["evidence"] == "code=NULL"


def test_apply_exercise_and_sell_group_thresholds():
    mk = lambda d, c, sh: {"transaction_date": d, "transaction_code": c, "shares": sh,
                           "primary_classification": "X0", "economic_reason": None}
    rows = [mk("2024-01-01", "M", 1000), mk("2024-01-01", "S", 900),
            mk("2024-01-02", "M", 1000), mk("2024-01-02", "S", 890),
            mk("2024-01-03", "S", 500), mk("2024-01-04", "M", 500),
            mk(None, "M", 500)]
    insider.apply_exercise_and_sell_group(rows)
    assert rows[0]["primary_classification"] == "EXERCISE_AND_SELL"       # ratio == 0.90
    assert rows[1]["economic_reason"] == "EXERCISE_AND_SELL"
    assert rows[2]["primary_classification"] == "EXERCISE_AND_PARTIAL_SELL"
    assert rows[4]["primary_classification"] == "X0"   # S without M: untouched
    assert rows[5]["primary_classification"] == "X0"   # M without S: untouched
    assert rows[6]["primary_classification"] == "X0"   # no date: ignored


# ---------------------------------------------------- bulk index + SGML
MASTER_IDX = """CIK|Company Name|Form Type|Date Filed|Filename
222233|Test Issuer Inc.|4|2026-07-15|edgar/data/222233/000123456726000123/0001234567-26-000123.txt
222233|Test Issuer Inc.|4/A|2026-07-16|edgar/data/222233/000123456726000124/0001234567-26-000124.txt
222233|Test Issuer Inc.|10-K|2026-07-15|edgar/data/222233/x/x.txt
222233|Test Issuer Inc.|4|2025-07-15|edgar/data/222233/y/0001234567-25-000001.txt
BADLINE without pipes
"""


def test_parse_master_index_filters():
    rows = insider.parse_master_index(MASTER_IDX.encode("latin-1"),
                                      date(2026, 7, 1), date(2026, 7, 31))
    assert [r["accession"] for r in rows] == [ADSH, "0001234567-26-000124"]
    assert rows[0]["form_type"] == "4"
    assert rows[1]["form_type"] == "4/A"
    assert rows[0]["filing_date"] == "2026-07-15"
    assert rows[0]["company_name"] == "Test Issuer Inc."


def _sgml(inner=FORM4_XML, with_filename=True):
    fn = "<FILENAME>form4.xml\n" if with_filename else ""
    return (
        "<SEC-DOCUMENT>\n"
        "<DOCUMENT>\n<TYPE>10-K\n<SEQUENCE>1\n<FILENAME>big.htm\n"
        "<XML>\n<html>not form4</html>\n</XML>\n</DOCUMENT>\n"
        "<DOCUMENT>\n<TYPE>4\n<SEQUENCE>2\n" + fn +
        "<XML>\n" + inner + "\n</XML>\n</DOCUMENT>\n</SEC-DOCUMENT>\n"
    ).encode()


def test_extract_form4_xml():
    xml, name = insider.extract_form4_xml(_sgml())
    assert name == "form4.xml"
    assert b"ownershipDocument" in xml
    assert b"<html>" not in xml
    assert xml.endswith(b"\n")
    # fallback: no DOCUMENT wrappers, ownershipDocument still found
    xml2, name2 = insider.extract_form4_xml(b"<XML>\n<ownershipDocument/>x\x00\n</XML>")
    assert name2 is None and b"\x00" not in xml2
    # HTML-only legacy filing → no XML at all
    xml3, name3 = insider.extract_form4_xml(b"<DOCUMENT><TYPE>4\n<HTML>nope</HTML></DOCUMENT>")
    assert xml3 is None and name3 is None


class _ZipClient:
    def __init__(self, zip_body, idx_body):
        self.zip_body, self.idx_body = zip_body, idx_body
        self.calls = []

    def get_bytes(self, url, max_age_hours=None):
        self.calls.append((url, max_age_hours))
        if url.endswith("master.zip"):
            return self.zip_body
        return self.idx_body


def test_load_quarter_index_zip_and_fallback():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("MASTER.IDX", MASTER_IDX)
    got = insider.load_quarter_index(_ZipClient(buf.getvalue(), b"fallback"), 2026, 3, False)
    assert got.decode() == MASTER_IDX
    # bad zip → fall back to the uncompressed master.idx
    c2 = _ZipClient(b"not a zip at all", MASTER_IDX.encode())
    got2 = insider.load_quarter_index(c2, 2026, 4, True)
    assert got2.decode() == MASTER_IDX
    assert c2.calls == [("https://www.sec.gov/Archives/edgar/full-index/2026/QTR4/master.zip", 24.0),
                        ("https://www.sec.gov/Archives/edgar/full-index/2026/QTR4/master.idx", 24.0)]
    # past quarters cached forever (None)
    c3 = _ZipClient(buf.getvalue(), b"")
    insider.load_quarter_index(c3, 2010, 1, False)
    assert c3.calls[0][1] is None


# ------------------------------------------------------------------ flow
class FakeClient:
    def __init__(self):
        self.json_calls = []
        self.bytes_calls = []

    def get_json(self, url, max_age_hours=None):
        self.json_calls.append((url, max_age_hours))
        if "search-index" in url:
            frm = int(url.split("from=")[1])
            if frm:
                return {"hits": {"hits": [], "total": {"value": 0}}}
            return {"hits": {"total": {"value": 1}, "hits": [{"_source": {
                "adsh": ADSH, "ciks": ["111222333", "222233"],
                "file_date": "2026-08-26", "display_names": ["Doe Jane"]}}]}}
        if url.endswith("index.json"):
            return {"directory": {"item": [
                {"name": "xslF345X05/wk-form4_1.json"},   # first item is a decoy
                {"name": "form4misc.txt"},
                {"name": XML_DOC},
            ]}}
        raise AssertionError(url)

    def get_bytes(self, url, max_age_hours=None):
        self.bytes_calls.append((url, max_age_hours))
        if url.endswith(XML_DOC):
            return FORM4_XML.encode("utf-8")
        raise AssertionError(url)


EXPECTED_EFTS = ("https://efts.sec.gov/LATEST/search-index?q=&forms=4"
                 "&dateRange=custom&startdt=2026-08-26&enddt=2026-08-26&from=0")
EXPECTED_IDX_URL = ARCHIVE_DIR + "/index.json"
EXPECTED_XML_URL = ARCHIVE_DIR + "/" + XML_DOC


def _args(tmp_path, monkeypatch, fake, argv):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path))
    monkeypatch.setattr(insider, "default_client", lambda: fake)
    ap = argparse.ArgumentParser()
    insider.add_arguments(ap)
    return ap.parse_args(argv)


def test_incremental_flow(tmp_path, monkeypatch):
    fake = FakeClient()
    rc = insider.run(_args(tmp_path, monkeypatch, fake, ["--date", "2026-08-26"]))
    assert rc == 0
    assert fake.json_calls[0] == (EXPECTED_EFTS, None)   # past day: cached forever
    assert fake.json_calls[1] == (EXPECTED_IDX_URL, None)
    assert fake.bytes_calls[0][0] == EXPECTED_XML_URL
    root = tmp_path / "insider"
    filings = list(storage.read_jsonl(root / "filings.jsonl"))
    trades = list(storage.read_jsonl(root / "trades.jsonl"))
    assert len(filings) == 1 and filings[0]["accession"] == ADSH
    assert filings[0]["filing_date"] == "2026-08-26"     # EFTS file_date wins
    assert len(trades) == 11
    assert (root / "checkpoint" / "done.txt").read_text().split() == [ADSH]
    doc = root / "docs" / f"CIK{ISSUER_CIK}" / ADSH.replace("-", "") / XML_DOC
    assert doc.exists() and b"ownershipDocument" in doc.read_bytes()

    # --resume: no re-download, no duplicate rows
    fake2 = FakeClient()
    rc = insider.run(_args(tmp_path, monkeypatch, fake2, ["--date", "2026-08-26", "--resume"]))
    assert rc == 0
    assert fake2.bytes_calls == []
    assert len(list(storage.read_jsonl(root / "filings.jsonl"))) == 1
    assert len(list(storage.read_jsonl(root / "trades.jsonl"))) == 11


def test_incremental_dry_run_writes_nothing(tmp_path, monkeypatch):
    fake = FakeClient()
    rc = insider.run(_args(tmp_path, monkeypatch, fake, ["--date", "2026-08-26", "--dry-run"]))
    assert rc == 0
    assert len(fake.bytes_calls) == 1        # it still parsed
    root = tmp_path / "insider"
    assert not (root / "filings.jsonl").exists()
    assert not (root / "trades.jsonl").exists()
    assert not (root / "checkpoint" / "done.txt").exists()


def test_universe_filter(tmp_path, monkeypatch):
    root = tmp_path / "insider"
    (tmp_path / "uni.txt").write_text("# comment\nNOPE\n")
    fake = FakeClient()
    rc = insider.run(_args(tmp_path, monkeypatch, fake,
                           ["--date", "2026-08-26", "--universe", str(tmp_path / "uni.txt")]))
    assert rc == 0
    assert not (root / "filings.jsonl").exists()          # "empty": nothing written
    assert not (root / "checkpoint" / "done.txt").exists()  # and NOT marked done
    # matching universe writes rows
    (tmp_path / "uni.txt").write_text("test\n")           # case-insensitive
    rc = insider.run(_args(tmp_path, monkeypatch, FakeClient(),
                           ["--date", "2026-08-26", "--universe", str(tmp_path / "uni.txt")]))
    assert rc == 0
    assert len(list(storage.read_jsonl(root / "trades.jsonl"))) == 11
    # missing universe file → error code, not a crash
    rc = insider.run(_args(tmp_path, monkeypatch, FakeClient(),
                           ["--date", "2026-08-26", "--universe", str(tmp_path / "absent.txt")]))
    assert rc == 2


class Section16Client:
    """New-schema filer: doc is named section16.xml, no 'form4' anywhere."""

    def __init__(self):
        self.bytes_calls = []

    def get_json(self, url, max_age_hours=None):
        assert url.endswith("index.json")
        return {"directory": {"item": [
            {"name": "0001234567-26-000123-index-headers.html"},
            {"name": "0001234567-26-000123-index.xml"},      # never fetched
            {"name": "primary_doc.xml"},                     # wrong-doc decoy
            {"name": "section16.xml"},                       # the real one
        ]}}

    def get_bytes(self, url, max_age_hours=None):
        self.bytes_calls.append(url)
        if url.endswith("primary_doc.xml"):
            return b'<?xml version="1.0"?><edgarSubmission>nope</edgarSubmission>'
        if url.endswith("section16.xml"):
            return FORM4_XML.encode("utf-8")
        raise AssertionError(url)


def test_parse_filing_sniffs_section16_xml(tmp_path):
    fake = Section16Client()
    parsed = insider.parse_filing(fake, ADSH, ISSUER_CIK, "2026-08-26",
                                  docs_root=tmp_path)
    assert parsed is not None
    filing, trades = parsed
    assert filing["accession"] == ADSH and len(trades) == 11
    # -index.xml wrapper skipped without a request; decoy tried, then hit
    assert [u.rsplit("/", 1)[1] for u in fake.bytes_calls] == \
        ["primary_doc.xml", "section16.xml"]
    saved = tmp_path / f"CIK{ticker.cik10(ISSUER_CIK)}" / ADSH.replace("-", "")
    assert (saved / "section16.xml").exists()


def test_parse_filing_still_returns_none_without_ownership_doc():
    class NoDoc(Section16Client):
        def get_bytes(self, url, max_age_hours=None):
            self.bytes_calls.append(url)
            return b'<?xml version="1.0"?><foo>bar</foo>'

    assert insider.parse_filing(NoDoc(), ADSH, ISSUER_CIK) is None


class BulkClient:
    def __init__(self):
        self.bytes_calls = []

    def get_json(self, url, max_age_hours=None):
        raise AssertionError("bulk must not touch EFTS: " + url)

    def get_bytes(self, url, max_age_hours=None):
        self.bytes_calls.append((url, max_age_hours))
        if url.endswith("master.zip"):
            raise RuntimeError("no zip")           # exercise the idx fallback
        if url.endswith("master.idx"):
            return MASTER_IDX.encode("latin-1")
        if url.endswith(".txt"):
            return _sgml()
        raise AssertionError(url)


def test_bulk_flow(tmp_path, monkeypatch):
    fake = BulkClient()
    argv = ["--bulk", "--start-date", "2026-07-01", "--end-date", "2026-07-31"]
    rc = insider.run(_args(tmp_path, monkeypatch, fake, argv))
    assert rc == 0
    root = tmp_path / "insider"
    filings = list(storage.read_jsonl(root / "filings.jsonl"))
    trades = list(storage.read_jsonl(root / "trades.jsonl"))
    # as_completed order is nondeterministic; assert the set
    assert sorted(f["accession"] for f in filings) == \
        sorted([ADSH, "0001234567-26-000124"])
    fa = next(f for f in filings if f["accession"] == ADSH)  # completion order varies
    assert fa["filing_date"] == "2026-07-15"   # from the quarterly index
    assert fa["document_type"] == "4"
    assert len(trades) == 22
    assert ("https://www.sec.gov/Archives/edgar/data/222233/000123456726000123/"
            "0001234567-26-000123.txt", None) in fake.bytes_calls
    done = set((root / "checkpoint" / "done.txt").read_text().split())
    assert done == {ADSH, "0001234567-26-000124"}
    doc = root / "docs" / f"CIK{ISSUER_CIK}" / ADSH.replace("-", "") / "form4.xml"
    assert doc.exists()
    qstate = json.loads((root / "checkpoint" / "bulk_quarters.json").read_text())
    assert qstate["quarters_done"] == ["2026Q3"]
    # finished past quarters are skipped on the next run (resume of the index itself)
    fake2 = BulkClient()
    rc = insider.run(_args(tmp_path, monkeypatch, fake2,
                            argv + ["--resume"]))
    assert rc == 0
    assert fake2.bytes_calls == []
    assert len(list(storage.read_jsonl(root / "trades.jsonl"))) == 22  # no dupes


def test_bulk_limit(tmp_path, monkeypatch):
    fake = BulkClient()
    rc = insider.run(_args(tmp_path, monkeypatch, fake,
                           ["--bulk", "--start-date", "2026-07-01", "--end-date", "2026-07-31",
                            "--limit", "1"]))
    assert rc == 0
    assert len(list(storage.read_jsonl(tmp_path / "insider" / "filings.jsonl"))) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
