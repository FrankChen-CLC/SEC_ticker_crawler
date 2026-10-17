"""Offline tests for the prexbrl pipeline (no network, no DB, no LLM).

Fixture ideas ported from production ``pre_xbrl_agent/tests/test_agent.py``:
small synthetic SGML/HTML documents, hand-built EvidenceTables, and regex
regression cases for the legacy-filing numeric quirks.
"""
from __future__ import annotations

import hashlib
import io
import json
import re

import pytest

from secdata import storage
from secdata.prexbrl import COMMAND, HELP, add_arguments, run
from secdata.prexbrl import download as dl
from secdata.prexbrl import extract as ex
from secdata.prexbrl import parser as pr
from secdata.prexbrl.catalog import infer_canonical_key, safe_canonical_key

# ------------------------------------------------------------------ fixtures

SAMPLE_HTML = b"""<SEC-DOCUMENT>test.txt
<DOCUMENT>
<TYPE>10-K
<SEQUENCE>1
<FILENAME>report.htm
<DESCRIPTION>ANNUAL REPORT
<TEXT>
<html><body>
<p>ACME CORP. CONSOLIDATED STATEMENTS OF OPERATIONS</p>
<p>(Dollars in thousands, except per share amounts)</p>
<table>
<tr><td></td><td colspan="2">2006</td><td colspan="2">2005</td></tr>
<tr><td>Revenue</td><td>$</td><td>1,000</td><td>$</td><td>900</td></tr>
<tr><td>Net income</td><td>$</td><td>(100)</td><td>$</td><td>80</td></tr>
</table>
</body></html>
</TEXT>
</DOCUMENT>"""

SAMPLE_ASCII = b"""<SEC-DOCUMENT>
<DOCUMENT>
<TYPE>10-K
<SEQUENCE>1
<FILENAME>main.txt
<DESCRIPTION>ANNUAL REPORT
<TEXT>
COMPANY CONFORMED NAME:   ACME OLDSTYLE INC
<TABLE>
<CAPTION>
CONSOLIDATED STATEMENTS OF INCOME
(In thousands, except per share data)
                      1994     1993     1992
<S>                   <C>      <C>      <C>
Net sales .......... $ 1,000     900     800
Cost of sales ........   600     550     500
Gross profit .........   400     350     300
Net income ...........   100      90      80
Earnings per share ...   $.36     $.08     $.19
</TABLE>
</TEXT>
</DOCUMENT>"""


def _primary(body: bytes) -> pr.SecDocument:
    docs = pr.parse_sec_documents(pr.decode_submission(body))
    return pr.select_primary_document(docs, "10-K")


# ---------------------------------------------------------------- module meta

def test_module_contract():
    assert COMMAND == "prexbrl"
    assert HELP


def test_add_arguments_defaults():
    import argparse

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args([])
    assert args.before == "2007-01-01"
    assert args.date_basis == "report"
    assert "10-K405" in args.forms
    assert args.scope == "core_statements"
    assert args.workers == 4


def test_default_forms_are_legacy_aware():
    # production archive needed the pre-2003 10-K405 variants
    assert "10-K405" in dl.DEFAULT_FORMS
    assert "10-K405/A" in dl.DEFAULT_FORMS


# ------------------------------------------------------------- submission

def test_decode_submission_utf8_then_cp1252():
    assert pr.decode_submission("café".encode("utf-8")) == "café"
    assert pr.decode_submission(b"caf\xe9") == "café"


def test_parse_sec_documents_headers():
    docs = pr.parse_sec_documents(pr.decode_submission(SAMPLE_HTML))
    assert len(docs) == 1
    doc = docs[0]
    assert doc.document_type == "10-K"
    assert doc.sequence == "1"
    assert doc.filename == "report.htm"
    assert doc.description == "ANNUAL REPORT"
    assert "<table>" in doc.content


def test_select_primary_document_rules():
    submission = """
<DOCUMENT><TYPE>GRAPHIC
<SEQUENCE>1
<FILENAME>a.gif
<TEXT>tiny</TEXT></DOCUMENT>
<DOCUMENT><TYPE>10-K
<SEQUENCE>2
<FILENAME>big.htm
<TEXT>""" + "x" * 100 + """</TEXT></DOCUMENT>
<DOCUMENT><TYPE>10-K
<SEQUENCE>3
<FILENAME>small.htm
<TEXT>yy</TEXT></DOCUMENT>
<DOCUMENT><TYPE>10-K405
<SEQUENCE>4
<FILENAME>legacy.txt
<TEXT>""" + "z" * 50 + """</TEXT></DOCUMENT>
"""
    docs = pr.parse_sec_documents(submission)
    assert pr.select_primary_document(docs, "10-K").filename == "big.htm"   # exact type, largest
    assert pr.select_primary_document(docs, "10-K405").filename == "legacy.txt"
    # no exact / no prefix match -> largest *.htm document wins over gif/txt
    assert pr.select_primary_document(docs, "10-K/A").filename == "big.htm"
    with pytest.raises(ValueError):
        pr.select_primary_document([], "10-K")


def test_parse_registrant_name_both_headers():
    assert pr.parse_registrant_name(
        "COMPANY CONFORMED NAME:   ACME HOLDINGS INC\n<DOCUMENT></DOCUMENT>"
    ) == "ACME HOLDINGS INC"
    assert pr.parse_registrant_name("<COMPANY-CONFORMED-NAME> ACME  &nbsp;CO</COMPANY-CONFORMED-NAME>") == "ACME CO"
    assert pr.parse_registrant_name("nothing here") is None


# ------------------------------------------------------------- html tables

def test_html_table_value_ids_and_geometry():
    table = pr.extract_evidence_tables(_primary(SAMPLE_HTML))[0]
    assert table.parser_kind == "html"
    assert table.value_map["T0001_V0003"].text == "1,000"
    assert table.value_map["T0001_V0005"].text == "(100)"
    periods = [c for c in table.cells if c.is_period]
    assert {c.text for c in periods} == {"2006", "2005"}
    cell_2006 = next(c for c in periods if c.text == "2006")
    assert (cell_2006.col_start, cell_2006.col_end) == (1, 2)   # colspan honored
    assert table.contexts[-1].is_unit is True                   # (Dollars in thousands...)
    assert table.contexts[0].text.startswith("ACME CORP.")


def test_html_table_rowspan_shifts_columns():
    doc = pr.SecDocument("10-K", "1", "r.htm", "", """
<table>
<tr><td rowspan="2">Total assets</td><td>2004</td><td>1,000</td></tr>
<tr><td>2003</td><td>900</td></tr>
</table>""")
    table = pr.extract_evidence_tables(doc)[0]
    row1 = table.rows[1]
    # the rowspan cell keeps column 0 reserved, so "2003" lands in column 1
    assert [(c.text, c.col_start, c.col_end) for c in row1.cells] == [("2003", 1, 1), ("900", 2, 2)]


def test_html_table_nested_rows_do_not_leak():
    doc = pr.SecDocument("10-K", "1", "n.htm", "", """
<table>
<tr><td>Outer</td><td>5</td></tr>
<tr><td><table><tr><td>Inner</td><td>7</td></tr></table></td></tr>
</table>""")
    outer, inner = pr.extract_evidence_tables(doc)
    assert {c.text for c in outer.cells} >= {"Outer", "5"}
    assert "Inner" not in {c.text for c in outer.cells}
    assert "Inner" in {c.text for c in inner.cells}


def test_html_table_tolerates_broken_markup():
    doc = pr.SecDocument("10-K", "1", "b.htm", "", """
<TABLE><TR><TD>Net income<TD><FONT size=2>123</FONT><TR><TD>Cash</td><td>456""")
    tables = pr.extract_evidence_tables(doc)
    assert tables, "broken legacy HTML must still yield the table"
    texts = {c.text for c in tables[0].cells}
    assert {"Net income", "123", "Cash", "456"} <= texts


def test_html_cell_text_joins_splitted_runs():
    doc = pr.SecDocument("10-K", "1", "t.htm", "", """
<table><tr><td>Net income<br>loss</td><td>42</td></tr></table>""")
    table = pr.extract_evidence_tables(doc)[0]
    assert table.rows[0].cells[0].text == "Net income loss"


# -------------------------------------------------------- numeric quirks

def test_numeric_re_leading_decimals_and_dashes():
    for raw in ("$.36", ".19", "(.02)", "--", "—", "–", "1,234.56", "(1,000)", "25%"):
        assert pr.NUMERIC_RE.fullmatch(raw), raw
    assert not pr.NUMERIC_RE.fullmatch("thousands of customers")
    assert not pr.NUMERIC_RE.fullmatch("$1 million")


def test_plain_value_re_keeps_decimal_point_on_leading_decimal():
    line = "     earnings per share....................          $.36          $.08          $.19"
    assert [m.group() for m in pr.PLAIN_VALUE_RE.finditer(line)] == ["$.36", "$.08", "$.19"]


def test_plain_value_re_dot_leaders_are_not_decimals():
    # the last leader dot must not be read as a decimal point (3,071.100!)
    line = "Inventories.......3,071,100"
    assert [m.group() for m in pr.PLAIN_VALUE_RE.finditer(line)] == ["3,071,100"]


def test_unit_text_detection_matrix():
    for raw in ("(thousands)", "(In thousands)", "(Thousands of Dollars)",
               "(millions)", "(billions)", "($ in thousands)", "Dollars in millions",
               "except per share amounts", "(000's)"):
        assert pr.is_explicit_unit_text(raw), raw
    for raw in ("several thousands of customers", "thousands of employees in factories",
               "Revenue in thousands of units sold", "Total revenues",
               "December 31, 2001", "Net income"):
        assert not pr.is_explicit_unit_text(raw), raw


# ----------------------------------------------------------- plain ASCII

def test_ascii_statement_is_parsed_and_selected():
    tables = pr.extract_evidence_tables(_primary(SAMPLE_ASCII))
    plain = [t for t in tables if t.parser_kind == "plain_text"]
    assert len(plain) == 1
    table = plain[0]
    assert len(table.value_map) == 18
    eps = {c.text for c in table.cells if c.value_id}
    assert "$.36" in eps, "leading-decimal EPS must survive dot leaders"
    cands = pr.select_candidate_tables(tables, "core_statements", "ACME OLDSTYLE INC")
    assert cands == [table]


def test_plain_statement_rejects_note_prose_without_periods():
    # "The accompanying notes ..." style context must not qualify as core
    table = pr.EvidenceTable(
        table_id="T1", contexts=[pr.ContextLine("X1", "The accompanying notes are an "
            "integral part of the consolidated financial statements")],
        rows=[pr.EvidenceRow("R", 0, [pr.EvidenceCell("R_C000", 0, 0, 5, "Cash")])],
        source_filename="x.htm", source_document_type="10-K", html_hash="h",
        financial_score=10, statement_score=10, parser_kind="html")
    assert not pr._is_core_statement_table(table)
    good = pr.EvidenceTable(
        table_id="T2", contexts=[pr.ContextLine("X1", "Consolidated Balance Sheets")],
        rows=[pr.EvidenceRow("R", 0, [pr.EvidenceCell("R_C000", 0, 0, 5, "Cash")])],
        source_filename="x.htm", source_document_type="10-K", html_hash="h",
        financial_score=10, statement_score=10, parser_kind="html")
    assert pr._is_core_statement_table(good)


def test_registrant_filter_keeps_anonymous_drops_other_entity():
    row = [pr.EvidenceRow("R", 0, [pr.EvidenceCell("R_C000", 0, 0, 5, "Cash")])]
    anon = pr.EvidenceTable("T1", [pr.ContextLine("X1", "Consolidated Balance Sheets")],
                            list(row), "x.htm", "10-K", "h", 10, 10, "plain_text")
    other = pr.EvidenceTable("T2", [pr.ContextLine("X1",
        "Acme Corporation and Subsidiaries Balance Sheet")],
        list(row), "x.htm", "10-K", "h", 10, 10, "plain_text")
    out = pr._filter_to_registrant([anon, other], "ACME OLDSTYLE INC")
    assert anon in out
    assert other not in out


# ------------------------------------------------------------- extraction

def test_parse_decimal_quirks():
    assert str(ex._parse_decimal("($1,000)")) == "-1000"
    assert str(ex._parse_decimal("$(1,000)")) == "-1000"
    assert str(ex._parse_decimal("(US$1,000)")) == "-1000"
    assert str(ex._parse_decimal("(€1,234.5)")) == "-1234.5"
    for dash in ("--", "—", "–", ""):
        assert ex._parse_decimal(dash) is None
    assert str(ex._parse_decimal("—100")) == "-100"
    assert str(ex._parse_decimal("25%")) == "0.25"
    assert str(ex._parse_decimal("(25%)")) == "-0.25"
    assert str(ex._parse_decimal("$.36")) == "0.36"


def test_unit_details_scale_and_kind():
    assert ex._unit_details("(thousands) | $", "Net Income", "net_income")["scale"] == 1000
    assert ex._unit_details("(thousands) | $", "Net Income", "net_income")["kind"] == "monetary"
    assert ex._unit_details("USD", "Revenue", "revenue")["kind"] == "monetary"
    assert ex._unit_details("(percent)", "Margin", "other")["scale"] == 1
    assert ex._unit_details("per share", "EPS", "eps_basic")["kind"] == "per_share"


def test_column_currency_propagation_and_usd_block():
    rows = [
        pr.EvidenceRow("T0001_R0000", 0, [
            pr.EvidenceCell("T0001_R0000_C000", 0, 0, 10, "Revenue"),
            pr.EvidenceCell("T0001_R0000_C001", 0, 11, 11, "$"),
            pr.EvidenceCell("T0001_R0000_C002", 0, 12, 18, "1,000", "T0001_V0001"),
        ]),
        pr.EvidenceRow("T0001_R0001", 1, [
            pr.EvidenceCell("T0001_R0001_C000", 1, 0, 10, "Cost"),
            pr.EvidenceCell("T0001_R0001_C002", 1, 12, 18, "600", "T0001_V0002"),
        ]),
    ]
    table = pr.EvidenceTable("T0001", [pr.ContextLine("T0001_X001", "(in thousands)", True)],
                             rows, "x.htm", "10-K", "h", 10, 10, "html")
    part = ex._column_currency_part(table, table.value_map["T0001_V0002"])
    assert part["raw"] == "$" and part["evidence"] == "column_propagated"
    assert ex._assumed_currency_part(table) is not None
    table.rows[0].cells.append(pr.EvidenceCell(
        "T0001_R0000_C099", 0, 100, 102, "€"))
    assert ex._assumed_currency_part(table) is None, "foreign currency blocks USD default"


def test_catalog_conservative_mapping():
    assert infer_canonical_key("Net sales") == "revenue"
    assert infer_canonical_key("Revenue from contracts with customers") == "other"
    assert safe_canonical_key("revenue", "Accounts receivable") == ("other", False)
    assert safe_canonical_key("net_income", "Net income") == ("net_income", True)


def test_extract_sample_html_accepted_and_gated():
    result = ex.extract_submission(
        SAMPLE_HTML, {"cik": "0000001750", "date": "2006-05-31", "form": "10-K",
                      "accession": "0001104659-06-047248"})
    assert result["schema_version"] == "pre_xbrl_metrics.v1"
    assert result["coverage"]["accepted_metrics"] == 4
    revenue = [m for m in result["metrics"] if m["canonical_key"] == "revenue"][0]
    assert revenue["value_raw"] == "1,000"
    assert revenue["normalized_value_in_reported_unit"] == "1000"
    assert all(revenue["verification"][k] for k in ex.REQUIRED_VERIFICATION_CHECKS)
    assert revenue["verification"]["linking_method"] == "deterministic_row_geometry"
    assert revenue["source"]["value_ref"] == "T0001_V0003"
    margins = {d["period_key"]: d["calculated_value"] for d in result["derived_metrics"]
               if d["canonical_key"] == "net_margin"}
    assert margins["2006"] == "-0.1"
    for metric in result["metrics"]:
        assert metric["normalized_value_in_base_unit"] is not None


def test_extract_dash_placeholder_routed_to_review():
    body = SAMPLE_HTML.replace(
        b"<tr><td>Net income</td><td>$</td><td>(100)</td>",
        b"<tr><td>Net income</td><td>$</td><td>--</td>")
    result = ex.extract_submission(body, {"cik": "1", "date": "2006-12-31",
                                         "form": "10-K", "accession": "x"})
    dashes = [m for m in result["needs_review"] if m["value_raw"] == "--"]
    assert dashes
    assert not dashes[0]["verification"]["value_parsed"]
    assert dashes[0]["normalized_value_in_base_unit"] is None
    for metric in result["metrics"]:
        assert metric["normalized_value_in_base_unit"] is not None


def test_extract_ascii_eps_not_inflated():
    result = ex.extract_submission(
        SAMPLE_ASCII, {"cik": "1", "date": "1994-12-31", "form": "10-K",
                       "accession": "x"})
    eps = [m for m in result["metrics"] if m["canonical_key"] == "eps_basic"
           and m["period_raw"] == "1994"]
    assert eps and eps[0]["normalized_value_in_reported_unit"] == "0.36"


def test_derived_only_consumes_accepted():
    result = ex.extract_submission(
        SAMPLE_HTML, {"cik": "1", "date": "2006-12-31", "form": "10-K", "accession": "x"})
    accepted_ids = {m["metric_id"] for m in result["metrics"]}
    for derived in result["derived_metrics"]:
        for metric_id in derived["input_metric_ids"]:
            assert metric_id in accepted_ids


# ---------------------------------------------------------------- download

def _columnar(rows, keys):
    out = {k: [r.get(k, "") for r in rows] for k in keys}
    return out


COLS = ("form", "accessionNumber", "filingDate", "reportDate", "primaryDocument")


def test_select_rows_semantics():
    rows = [
        {"form": "10-K", "accessionNumber": "a1", "filingDate": "2007-03-01", "reportDate": "2006-12-31"},
        {"form": "10-K405", "accessionNumber": "a2", "filingDate": "1998-03-15", "reportDate": "1997-12-31"},
        {"form": "10-K", "accessionNumber": "a3", "filingDate": "2008-03-01", "reportDate": "2007-12-31"},
        {"form": "20-F", "accessionNumber": "a4", "filingDate": "2005-06-30", "reportDate": ""},
        {"form": "8-K", "accessionNumber": "a5", "filingDate": "2000-01-01", "reportDate": "2000-01-01"},
    ]
    forms = dl.parse_forms(",".join(dl.DEFAULT_FORMS))
    sel = dl.select_rows(rows, forms=forms, before="2007-01-01", after=None, date_basis="report")
    acc = [r["accessionNumber"] for r in sel]
    # ascending effective date; a4 (blank reportDate) sorts by filingDate fallback
    assert acc == ["a2", "a4", "a1"], "sorted by report date; 2007-filed FY2006 included; 8-K excluded"
    sel = dl.select_rows(rows, forms=forms, before="2007-01-01", after=None, date_basis="filing")
    assert "a1" not in [r["accessionNumber"] for r in sel]
    sel = dl.select_rows(rows, forms={"10-K"}, before="2020-01-01", after="2006-12-31", date_basis="report")
    assert [r["accessionNumber"] for r in sel] == ["a1", "a3"], "after is inclusive, before exclusive"
    assert dl.parse_forms("ALL") is None
    with pytest.raises(ValueError):
        dl.parse_forms(" , ")


def test_filing_urls_match_edgar():
    complete, index = dl.filing_urls("0000000175", "0001104659-06-047248")
    assert complete == "https://www.sec.gov/Archives/edgar/data/175/0001104659-06-047248.txt"
    assert index == "https://www.sec.gov/Archives/edgar/data/175/000110465906047248/0001104659-06-047248-index.html"


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = {"json": [], "bytes": []}

    def get_json(self, url, max_age_hours=None):
        self.calls["json"].append(url)
        return json.loads(self.payloads[url])

    def get_bytes(self, url, max_age_hours=None):
        self.calls["bytes"].append(url)
        return self.payloads.get(url, b"")

    def get_text(self, url, max_age_hours=None):
        return self.get_bytes(url).decode("utf-8", "replace")


def test_download_filing_records_and_resume(tmp_path):
    client = FakeClient({"u1": b"complete submission bytes"})
    row = {"accessionNumber": "0000000001-95-000001", "form": "10-K405",
           "filingDate": "1995-03-31", "reportDate": "1994-12-31",
           "primaryDocument": "x.txt"}
    client.payloads = {}
    url = dl.filing_urls("1", "0000000001-95-000001")[0]
    client.payloads[url] = b"complete submission bytes"

    rec = dl.download_filing(client, row, label="OLD", company_name="Old Co", cik="1",
                             docs_root=tmp_path, overwrite=False)
    assert rec["status"] == "downloaded"
    assert rec["sha256"] == hashlib.sha256(b"complete submission bytes").hexdigest()
    assert rec["local_path"] == str(tmp_path / "1" / "0000000001-95-000001.txt")

    before = len(client.calls["bytes"])
    rec = dl.download_filing(client, row, label="OLD", company_name="Old Co", cik="1",
                             docs_root=tmp_path, overwrite=False)
    assert rec["status"] == "existing"
    assert len(client.calls["bytes"]) == before, "resume must not refetch"

    rec = dl.download_filing(client, row, label="OLD", company_name="Old Co", cik="1",
                             docs_root=tmp_path, overwrite=True)
    assert rec["status"] == "downloaded"
    assert len(client.calls["bytes"]) == before + 1


def test_download_filing_empty_response_is_error_not_crash(tmp_path):
    client = FakeClient({})
    row = {"accessionNumber": "0000000001-95-000002", "form": "10-K",
           "filingDate": "1995-03-31", "reportDate": ""}
    rec = dl.download_filing(client, row, label="X", company_name="X", cik="1",
                             docs_root=tmp_path, overwrite=False)
    assert rec["status"] == "error"
    assert "Empty SEC response" in rec["error"]


def test_get_all_submission_rows_follows_history():
    root = {"name": "Aar Corp", "filings": {
        "recent": _columnar([{"form": "10-K", "accessionNumber": "new1"}], COLS),
        "files": [{"name": "CIK0000001750-submissions-001.json"}]}}
    history = _columnar([
        {"form": "10-K405", "accessionNumber": "old1", "reportDate": "1997-12-31",
         "filingDate": "1998-03-15"},
        {"form": "10-K", "accessionNumber": "new1"},  # duplicate accession deduped
    ], COLS)
    payloads = {
        "https://data.sec.gov/submissions/CIK0000001750.json": json.dumps(root),
        "https://data.sec.gov/submissions/CIK0000001750-submissions-001.json": json.dumps(history),
    }
    client = FakeClient(payloads)
    got_root, rows = dl.get_all_submission_rows(client, "1750")
    assert got_root["name"] == "Aar Corp"
    accs = {r["accessionNumber"] for r in rows}
    assert accs == {"new1", "old1"}, "history-file rows must be followed"


# ---------------------------------------------------------------- run() e2e

def _run_args(parser_kwargs):
    import argparse

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    return parser.parse_args(parser_kwargs)


def test_run_end_to_end_with_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    root = {"name": "ACME OLDSTYLE INC", "filings": {
        "recent": _columnar([], COLS),
        "files": [{"name": "CIK0000001750-submissions-001.json"}]}}
    history = _columnar([
        {"form": "10-K", "accessionNumber": "0001104659-06-047248",
         "filingDate": "2006-07-15", "reportDate": "2006-05-31",
         "primaryDocument": "a06-15961_110k.htm"},
        {"form": "10-K405", "accessionNumber": "0000950123-98-000004",
         "filingDate": "1998-03-15", "reportDate": "1997-12-31",
         "primaryDocument": "main.txt"},
        {"form": "10-K", "accessionNumber": "0000000000-08-000001",
         "filingDate": "2008-03-01", "reportDate": "2007-12-31",
         "primaryDocument": "x.htm"},   # after cutoff -> excluded
    ], COLS)
    payloads = {
        "https://data.sec.gov/submissions/CIK0000001750.json": json.dumps(root),
        "https://data.sec.gov/submissions/CIK0000001750-submissions-001.json": json.dumps(history),
        "https://www.sec.gov/Archives/edgar/data/1750/0001104659-06-047248.txt": SAMPLE_HTML,
        "https://www.sec.gov/Archives/edgar/data/1750/0000950123-98-000004.txt": SAMPLE_ASCII,
    }
    client = FakeClient(payloads)
    monkeypatch.setattr("secdata.prexbrl.default_client", lambda: client)

    args = _run_args(["--cik", "1750", "--workers", "2"])
    rc = run(args)
    assert rc == 0, "clean run"

    out = tmp_path / "out" / "prexbrl"
    doc = out / "docs" / "1750" / "0001104659-06-047248.txt"
    assert doc.exists() and doc.read_bytes() == SAMPLE_HTML
    assert (out / "docs" / "1750" / "0000950123-98-000004.txt").exists()

    manifest = list(storage.read_jsonl(out / "submissions.jsonl"))
    assert len(manifest) == 2
    assert {m["status"] for m in manifest} == {"downloaded"}
    assert {m["accession_number"] for m in manifest} == {
        "0001104659-06-047248", "0000950123-98-000004"}
    assert manifest[0]["complete_submission_url"].endswith(".txt")

    assert (out / "extractions" / "1750" / "000110465906047248.json").exists()
    metrics = list(storage.read_jsonl(out / "metrics.jsonl"))
    assert metrics, "accepted metric rows must land in metrics.jsonl"
    assert all(m["canonical_key"] for m in metrics)
    assert {m["accession"] for m in metrics} <= {
        "0001104659-06-047248", "0000950123-98-000004"}

    done = (out / "checkpoint" / "done.txt").read_text()
    assert "dl|1750|0001104659-06-047248" in done
    assert "ex|1750|0001104659-06-047248" in done

    # ---- re-run: cheap resume, no new HTTP, statuses become "existing"
    json_calls_before = len(client.calls["json"])
    bytes_calls_before = len(client.calls["bytes"])
    rc = run(_run_args(["--cik", "1750", "--workers", "2"]))
    assert rc == 0
    assert len(client.calls["bytes"]) == bytes_calls_before, "re-run must not refetch docs"
    manifest2 = list(storage.read_jsonl(out / "submissions.jsonl"))
    assert len(manifest2) == 4
    assert {m["status"] for m in manifest2[2:]} == {"existing"}
    assert len(client.calls["json"]) >= json_calls_before  # metadata revalidated is ok

    # ---- --list-only never downloads
    args = _run_args(["--cik", "1750", "--list-only"])
    buf = io.StringIO()
    saved = __import__("sys").stdout
    __import__("sys").stdout = buf
    try:
        assert run(args) == 0
    finally:
        __import__("sys").stdout = saved
    listing = buf.getvalue()
    assert "0001104659-06-047248" in listing
    assert "0000000000-08-000001" not in listing


def test_run_no_targets_returns_2(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    client = FakeClient({})
    monkeypatch.setattr("secdata.prexbrl.default_client", lambda: client)
    assert run(_run_args([])) == 2


def test_run_download_error_marks_failed_and_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    root = {"name": "X", "filings": {
        "recent": _columnar([{"form": "10-K", "accessionNumber": "0000000001-95-000009",
                              "filingDate": "1995-03-31", "reportDate": "1994-12-31"}], COLS),
        "files": []}}
    client = FakeClient({
        "https://data.sec.gov/submissions/CIK0000000001.json": json.dumps(root),
        # no archive payload -> empty -> error record
    })
    monkeypatch.setattr("secdata.prexbrl.default_client", lambda: client)
    rc = run(_run_args(["--cik", "1"]))
    assert rc == 1
    out = tmp_path / "out" / "prexbrl"
    assert "dl|1|0000000001-95-000009" in (out / "checkpoint" / "failed.txt").read_text()
    manifest = list(storage.read_jsonl(out / "submissions.jsonl"))
    assert manifest[0]["status"] == "error"
