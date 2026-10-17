"""Offline tests for secdata.events — 8-K item parsing + discovery filters +
full flow against a fake EdgarClient (monkeypatched default). No network."""
import argparse
import json

import pytest

from secdata import client as client_mod
from secdata import events


# ------------------------------------------------------------------ item channels
def test_parse_items_field():
    assert events.parse_items_field("2.02,7.01") == ["2.02", "7.01"]
    assert events.parse_items_field(" 5.02 , 9.01 ") == ["5.02", "9.01"]
    assert events.parse_items_field(None) == []
    assert events.parse_items_field("no items here") == []


def test_parse_index_head_items():
    txt = (
        "<SEC-DOCUMENT>\n<SEC-HEADER>\n"
        "ACCESSION NUMBER:\t\t0001230001-12-000001\n"
        "CONFORMED SUBMISSION TYPE:\t8-K\n"
        "ITEMS:\t\t\t\t2.02 8.01\n"
        "</SEC-HEADER>\n"
        "<DOCUMENT>\n<TEXT>\nITEMS: 9.99 should NOT count\n</TEXT>\n"
    )
    assert events.parse_index_head_items(txt) == ["2.02", "8.01"]
    # legacy <ITEMS> tag form
    txt2 = "<SEC-HEADER><ITEMS>1.01,2.03</ITEMS></SEC-HEADER><DOCUMENT>x"
    assert events.parse_index_head_items(txt2) == ["1.01", "2.03"]


def test_parse_body_item_headings():
    html = ("<html><body><p><b>Item 2.02 Results of Operations</b></p>"
            "<h1>ITEM&nbsp;5.02</h1><script>Item 99.99 hidden</script>"
            "<p>on August Item 8.01 Other Events</p></body></html>")
    assert events.parse_body_item_headings(html) == ["2.02", "5.02", "8.01"]


def test_collect_items_priority_and_dedupe():
    got = events.collect_items("2.02,7.01", ["7.01", "8.01"], ["2.02", "5.02"])
    # first channel wins per item, sorted ascending
    assert got == [("2.02", "submissions"), ("5.02", "body"),
                   ("7.01", "submissions"), ("8.01", "index_head")]


# ------------------------------------------------------------------ discovery
def _sub_json():
    return {
        "cik": 123456,
        "name": "Acme Inc.",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0001230001-12-000002",
                    "0001230001-12-000001",
                    "0001230001-12-000001",  # exact duplicate accession
                    "0001230001-12-000004",
                ],
                "form": ["10-Q", "8-K", "8-K", "8-K/A"],
                "filingDate": ["2024-12-01", "2024-11-01", "2024-11-01", "2024-11-05"],
                "reportDate": ["2024-09-30", "2024-11-01", "2024-11-01", ""],
                "acceptanceDateTime": ["", "", "", ""],
                "primaryDocument": ["x10q.htm", "a8k.htm", "a8k.htm", "a8ka.htm"],
                "items": ["", "2.02,7.01", "2.02,7.01", "5.02"],
            },
            "files": [],
        },
    }


def test_filings_from_submissions_filters_and_dedupes():
    rows = events.filings_from_submissions(_sub_json(), {"8-K", "8-K/A"})
    accs = [r["accession"] for r in rows]
    assert accs == ["0001230001-12-000001", "0001230001-12-000004"]  # dup + 10-Q dropped
    assert rows[0]["items_raw"] == "2.02,7.01"
    assert rows[1]["form_type"] == "8-K/A"  # amendments kept (production forms set)
    # date window
    rows2 = events.filings_from_submissions(_sub_json(), {"8-K", "8-K/A"},
                                            after="2024-11-03")
    assert [r["accession"] for r in rows2] == ["0001230001-12-000004"]


def test_parse_master_idx_lines():
    lines = [
        "#O=SEC Name|Format Type|Stock Exchange|CIK|Symbol",  # junk header
        "CIK|Company Name|Form Type|Date Filed|File Name",
        "---------------------------------------------------------------------------------------------",
        "0000123456|Acme Inc.|10-K|2024-02-01|edgar/data/123456/x/000123000124000009.txt",
        "0000123456|Acme Inc.|8-K|2024-11-01|edgar/data/123456/000123000112000001/0001230001-12-000001.txt",
        "0000123456|Acme Inc.|8-K|2024-11-01|edgar/data/123456/000123000112000001/0001230001-12-000001.txt",  # dup
        "0000999999|Other Corp|8-K/A|2024-11-02|edgar/data/999999/000099999924000001/0000999999-24-000001.txt",
        "0000123456|Acme Inc.|8-K/A|2024-12-01|edgar/data/123456/000123000112000007/0001230001-12-000007.txt",
        "too|few|parts",
    ]
    rows = events.parse_master_idx_lines(lines, {"8-K", "8-K/A"}, "0000123456")
    assert [r["accession"] for r in rows] == [
        "0001230001-12-000001", "0001230001-12-000007"]
    assert rows[1]["form_type"] == "8-K/A"
    assert rows[0]["archive_path"].startswith("edgar/data/")


def test_efts_hits_to_filings():
    hits = [
        {"_source": {"adsh": "0001230001-12-000001", "root_form": "8-K",
                     "ciks": ["0000123456"],
                     "display_names": ["Acme Inc. (ACME) (CIK 0000123456)"],
                     "file_date": "2024-11-01"}},
        {"_source": {"adsh": "", "ciks": ["0"], "display_names": []}},
    ]
    rows = events.efts_hits_to_filings(hits, {"8-K", "8-K/A"})
    assert len(rows) == 1
    r = rows[0]
    assert r["accession"] == "0001230001-12-000001"
    assert r["cik"] == "123456"  # production: str(int(ciks[0]))
    assert r["company_name"] == "Acme Inc."  # ' (File ...)' style suffix stripped


# ------------------------------------------------------------------ doc selection
def _listing():
    return [
        {"name": "0001230001-12-000001-index.htm", "size": 123},
        {"name": "a8k.htm", "size": 9000},
        {"name": "ex99_1.htm", "size": 4000},
        {"name": "results.pdf", "size": 80000},
        {"name": "coverpage.xml", "size": 100},
        {"name": "logo.gif", "size": 50},
    ]


def test_pick_primary_doc_priority():
    filing = {"accession": "0001230001-12-000001", "primary_doc": "a8k.htm"}
    assert events.pick_primary_doc(filing, _listing(), "other.htm",
                                   "0001230001-12-000001.txt") == "a8k.htm"
    filing2 = {"accession": "0001230001-12-000001", "primary_doc": ""}
    assert events.pick_primary_doc(filing2, _listing(), "listed.htm",
                                   "0001230001-12-000001.txt") == "listed.htm"
    assert events.pick_primary_doc(filing2, _listing(), "",
                                   "0001230001-12-000001.txt") == "a8k.htm"  # largest html


def test_pick_press_release_docs_order_and_exclusions():
    filing = {"accession": "0001230001-12-000001"}
    docs = events.pick_press_release_docs(filing, _listing(), "a8k.htm",
                                          "0001230001-12-000001.txt")
    names = [d["name"] for d in docs]
    assert names == ["ex99_1.htm", "results.pdf"]  # hinted exhibit ranks above PDF
    assert docs[0]["hinted"] and not docs[1]["hinted"]
    assert "a8k.htm" not in names and "coverpage.xml" not in names \
        and "logo.gif" not in names and "-index.htm" not in names


# ------------------------------------------------------------------ event rows
def test_build_event_rows_fields_and_dedupe():
    filing = {"accession": "0001230001-12-000001", "cik": "123456",
              "company_name": "Acme Inc.", "ticker": "ACME",
              "form_type": "8-K", "filing_date": "2024-11-01",
              "report_date": "2024-11-01", "acceptance_date": "",
              "items_raw": "2.02", "primary_doc": "a8k.htm"}
    meta = {"accession": "0001230001-12-000001", "cik": "123456",
            "company_name": "Acme Inc.", "form_type": "8-K",
            "filing_date": "2024-11-01",
            "archive_path": "edgar/data/123456/000123000112000001/0001230001-12-000001.txt",
            "status": "downloaded", "files_downloaded": 3, "total_bytes": 12345,
            "error": None, "downloaded_at": "2024-11-02T00:00:00+00:00",
            "primary_doc": "a8k.htm",
            "__index_items__": [{"name": "ex99_1.htm", "size": 4000}]}
    txt = "<SEC-HEADER>\nITEMS:\t\t\t\t7.01\n</SEC-HEADER>\n<DOCUMENT>x"
    body = "<html>Item 5.02 John Doe leaves</html>"
    rows = events.build_event_rows(filing, meta, txt, body, "docs/0000123456/x")
    by_item = {r["item"]: r for r in rows}
    assert set(by_item) == {"2.02", "5.02", "7.01"}
    assert by_item["2.02"]["item_source"] == "submissions"
    assert by_item["5.02"]["item_source"] == "body"
    assert by_item["7.01"]["item_source"] == "index_head"
    assert by_item["2.02"]["item_name"] == "Results of Operations and Financial Condition"
    assert by_item["2.02"]["press_release_doc"] == "ex99_1.htm"
    assert by_item["2.02"]["event_key"] == "0001230001-12-000001:2.02"
    # production meta fields preserved verbatim on every row
    for k in ("accession", "cik", "company_name", "form_type", "filing_date",
              "archive_path", "status", "files_downloaded", "total_bytes",
              "error", "downloaded_at"):
        assert by_item["2.02"][k] == meta[k]


def test_build_event_rows_no_items():
    filing = {"accession": "0000000000-99-000001", "cik": "1", "ticker": "",
              "items_raw": "", "primary_doc": ""}
    meta = {"status": "indexed", "company_name": "", "form_type": "8-K",
            "filing_date": "", "archive_path": "", "files_downloaded": 0,
            "total_bytes": 0, "error": None, "downloaded_at": "",
            "primary_doc": ""}
    rows = events.build_event_rows(filing, meta, "", "", "docs/0000000001/x")
    assert len(rows) == 1
    assert rows[0]["item"] == "" and rows[0]["item_source"] == "none"


# ------------------------------------------------------------------ flow (fake client)
SUB_URL = "https://data.sec.gov/submissions/CIK0000123456.json"
DIR = "https://www.sec.gov/Archives/edgar/data/123456/000123000112000001/"
TXT_URL = DIR + "0001230001-12-000001.txt"
IDX_URL = DIR + "index.json"
A8K_URL = DIR + "a8k.htm"
EX_URL = DIR + "ex99_1.htm"

FAKE_TXT = (
    "<SEC-DOCUMENT>\n<SEC-HEADER>\n"
    "ACCESSION NUMBER:\t\t0001230001-12-000001\n"
    "CONFORMED SUBMISSION TYPE:\t8-K\n"
    "ITEMS:\t\t\t\t7.01\n"
    "</SEC-HEADER>\n"
    "<DOCUMENT>\n<TEXT>\n"
    "<html><body><b>Item 2.02 Results of Operations</b>"
    "<p>ITEM 8.01 Other Events</p></body></html>\n"
    "</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n"
)

INDEX_JSON = {"directory": {
    "primaryDoc": "a8k.htm",
    "item": [
        {"name": "0001230001-12-000001-index.htm", "type": "file", "size": "123"},
        {"name": "a8k.htm", "type": "file", "size": "9000"},
        {"name": "ex99_1.htm", "type": "file", "size": "4000"},
        {"name": "coverpage.xml", "type": "file", "size": "100"},
        {"name": "logo.gif", "type": "file", "size": "50"},
        {"name": "xbrls", "type": "dir"},
    ]}}


class FakeClient:
    def __init__(self):
        self.docs = {
            SUB_URL: json.dumps(_sub_json_single()),
            IDX_URL: json.dumps(INDEX_JSON),
            TXT_URL: FAKE_TXT,
            A8K_URL: ("<html><body><b>Item 2.02 Results</b>"
                      "<p>ITEM&nbsp;8.01 Other Events</p></body></html>"),
            EX_URL: "<html>Acme beats estimates Item 2.02</html>",
        }
        self.hits = []

    def _get(self, url):
        self.hits.append(url)
        if url not in self.docs:
            raise KeyError(url)
        return self.docs[url]

    def get_text(self, url, max_age_hours=None):
        return self._get(url)

    def get_json(self, url, max_age_hours=None):
        return json.loads(self._get(url))

    def get_bytes(self, url, max_age_hours=None):
        return self._get(url).encode("utf-8")


def _sub_json_single():
    return {
        "cik": 123456, "name": "Acme Inc.",
        "filings": {"recent": {
            "accessionNumber": ["0001230001-12-000001"],
            "form": ["8-K"],
            "filingDate": ["2024-11-01"],
            "reportDate": ["2024-11-01"],
            "acceptanceDateTime": ["2024-11-01T21:00:00.000Z"],
            "primaryDocument": ["a8k.htm"],
            "items": ["2.02,5.02"],
        }, "files": []},
    }


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(client_mod, "_default", fake)
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    monkeypatch.delenv("SEC_EDGAR_SLEEP", raising=False)
    return fake, tmp_path / "out" / "events"


def _args(**kw):
    parser = argparse.ArgumentParser()
    events.add_arguments(parser)
    argv = ["--cik", "123456"]
    for k, v in kw.items():
        flag = "--" + k.replace("_", "-")
        if v is True:
            argv.append(flag)
        else:
            argv += [flag, str(v)]
    return parser.parse_args(argv)


def test_flow_downloads_indexes_and_resumes(fake_env):
    fake, root = fake_env
    assert events.run(_args()) == 0

    doc_dir = root / "docs" / "0000123456" / "000123000112000001"
    assert (doc_dir / "0001230001-12-000001.txt").exists()
    assert (doc_dir / "a8k.htm").exists()
    assert (doc_dir / "ex99_1.htm").exists()  # EX-99 press release pulled
    assert not (doc_dir / "coverpage.xml").exists()  # scope=press skips xml/index/img
    assert not (doc_dir / "logo.gif").exists()
    assert (doc_dir / "index.json").exists()

    meta = json.loads((doc_dir / "_meta.json").read_text())
    assert meta["status"] == "downloaded"
    assert meta["files_downloaded"] == 3
    for key in ("accession", "cik", "company_name", "form_type", "filing_date",
                "archive_path", "total_bytes", "error", "downloaded_at"):
        assert key in meta

    rows = list(json.loads(line) for line in
                (root / "events.jsonl").read_text().splitlines() if line)
    by_item = {r["item"]: r for r in rows}
    assert set(by_item) == {"2.02", "5.02", "7.01", "8.01"}
    assert by_item["2.02"]["item_source"] == "submissions"
    assert by_item["7.01"]["item_source"] == "index_head"
    assert by_item["8.01"]["item_source"] == "body"
    assert by_item["2.02"]["press_release_doc"] == "ex99_1.htm"
    assert by_item["2.02"]["ticker"] == "123456"  # --cik target label

    assert (root / "checkpoint" / "done.txt").read_text().split() == \
        ["0001230001-12-000001"]

    # idempotent re-run: checkpoint short-circuits, no new rows, no re-download
    hits_before = len(fake.hits)
    assert events.run(_args()) == 0
    n2 = len((root / "events.jsonl").read_text().splitlines())
    assert n2 == len(rows)  # JsonlWriter dedupe held the line
    assert len(fake.hits) - hits_before <= 1  # only the (cached) discovery call


def test_flow_discover_only(fake_env):
    fake, root = fake_env
    assert events.run(_args(discover_only=True)) == 0
    rows = [json.loads(l) for l in (root / "filings.jsonl").read_text().splitlines()]
    assert rows[0]["accession"] == "0001230001-12-000001"
    assert rows[0]["items"] == "2.02,5.02"
    assert not (root / "docs").exists()  # nothing downloaded
    assert TXT_URL not in fake.hits


def test_flow_scope_submission_txt_only(fake_env):
    fake, root = fake_env
    assert events.run(_args(document_scope="submission")) == 0
    doc_dir = root / "docs" / "0000123456" / "000123000112000001"
    assert (doc_dir / "0001230001-12-000001.txt").exists()
    assert not (doc_dir / "a8k.htm").exists()
    assert IDX_URL not in fake.hits
    rows = [json.loads(l) for l in (root / "events.jsonl").read_text().splitlines()]
    by_item = {r["item"]: r for r in rows}
    assert set(by_item) >= {"2.02", "5.02", "7.01"}  # items from JSON + index head


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
