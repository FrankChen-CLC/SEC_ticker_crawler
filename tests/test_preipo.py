"""Offline tests for secdata.preipo — canned EDGAR index/submissions +
fake client.  No network: all fetches go through a FakeClient installed by
monkeypatching ``preipo.default_client``.
"""
import argparse
import datetime as _dt
import json
import os
import urllib.error
from pathlib import Path

import pytest

from secdata import preipo, storage, ticker

# --------------------------------------------------------------------- canned data
CIK_A = "1111111"
ACC_A = "0001111111-24-000005"
ACC_A2 = "0001111111-24-000011"
CIK_B = "2222222"
ACC_SPAC = "0001213900-26-084648"

TICKERS_URL = preipo.TICKERS_URL
TICKERS = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
           "1": {"cik_str": 9999999, "ticker": "IPOX", "title": "IPOX Corp"}}

SUB_URL_A = "https://data.sec.gov/submissions/CIK0001111111.json"
FULL_URL_2024Q1 = preipo.full_index_url(2024, 1)

SPAC_URL = preipo.submissions_url("2115818")


def subs(name="NEWCO INC", forms=("S-1",), dates=("2024-01-05",),
         accs=(ACC_A,), pdocs=("s1.htm",), tickers=(), exchanges=()):
    return {
        "name": name,
        "tickers": list(tickers),
        "exchanges": list(exchanges),
        "entityType": "CORP",
        "stateOfIncorporation": "DE",
        "sic": "2830",
        "sicDescription": "Industrial Organic Chemicals",
        "filings": {"recent": {
            "form": list(forms),
            "filingDate": list(dates),
            "accessionNumber": list(accs),
            "primaryDocument": list(pdocs),
        }},
    }


SPAC_SUBS = subs(name="1776 Acquisition Corp", forms=("S-1/A",),
                 dates=("2026-08-03",), accs=(ACC_SPAC,), pdocs=("",))

# live-shape company.idx content: padded name / form / CIK / dashed date /
# path; 8-K noise line; headers must be skipped
IDX_TEXT = "\n".join([
    "Company-Name-and-Form                             CIK       Date-Filed  "
    "Filename",
    "-" * 70,
    "Last Data Received: October 15, 2026",
    "NEWCO INC                                               S-1       "
    "1111111     2024-01-05  "
    "edgar/data/1111111/0001111111-24-000005/s1.htm",
    "1776 Acquisition Corp                                   S-1/A     "
    "2115818     2026-08-03  "
    "edgar/data/2115818/0001213900-26-084648.txt",
    "NOISE INC                                               8-K       "
    "3333333     2024-01-06  "
    "edgar/data/3333333/0003333333-24-000001/x.htm",
    "",
]) + "\n"

GOOD_DOC = ("<html><body>" + "PROSPECTUS TEXT " * 80 + "</body></html>"
            ).encode()          # comfortably over the 500-byte gate
SMALL_DOC = b"<html>error</html>"   # SEC's small generic error page shape

INDEX_JSON = {"directory": {"item": [
    {"name": "index.json"},
    {"name": ACC_A + "-index.htm"},
    {"name": "primary.htm"},
    {"name": "ex31.htm"},
]}}


class FakeClient:
    """payload values may be bytes/JSON-able or an Exception to raise."""

    def __init__(self, payloads=None):
        self.payloads = dict(payloads or {})
        self.calls = []

    def get_bytes(self, url, max_age_hours=None, min_cache_bytes=0,
                  timeout=None):
        self.calls.append(url)
        v = self.payloads.get(url)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return v if isinstance(v, bytes) else json.dumps(v).encode()

    def get_text(self, url, max_age_hours=None):
        return self.get_bytes(url, max_age_hours).decode()

    def get_json(self, url, max_age_hours=None):
        return json.loads(self.get_text(url, max_age_hours))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_UA", "Test Bot test@example.com")
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("SEC_EDGAR_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("SEC_EDGAR_OFFLINE", raising=False)


def parse_args(*argv):
    p = argparse.ArgumentParser()
    preipo.add_arguments(p)
    return p.parse_args(list(argv))


def install(monkeypatch, fake):
    monkeypatch.setattr(preipo, "default_client", lambda: fake)
    return fake


def base_payloads(extra=None):
    d = {TICKERS_URL: TICKERS, SUB_URL_A: subs()}
    if extra:
        d.update(extra)
    return d


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def out_root():
    return Path(os.environ["SEC_EDGAR_OUT"], "preipo")


def state_path():
    return out_root() / "checkpoint" / "state.json"


def seed_state(seen):
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"version": 2, "seen": seen, "last_run": None}))
    return sp


def entry(cik, form, date, adsh, pdoc="", **kw):
    d = {"cik": cik, "company": kw.get("company", "NEWCO INC"), "form": form,
         "date": date, "adsh": adsh, "pdoc": pdoc, "filename": "",
         "downloaded": kw.get("downloaded", False),
         "ipo_detected": kw.get("ipo_detected", False), "first_seen": "x"}
    return d


# ------------------------------------------------------------------ pure parses
def test_parse_full_index_line_live_shape():
    rec = preipo.parse_full_index_line(
        "NEWCO INC                                     S-1       1111111     "
        "2024-01-05  edgar/data/1111111/0001111111-24-000005/s1.htm")
    assert rec == {"cik": "1111111", "company": "NEWCO INC", "form": "S-1",
                   "date": "2024-01-05", "accession": ACC_A,
                   "filename": ("edgar/data/1111111/"
                                "0001111111-24-000005/s1.htm")}


def test_parse_full_index_line_multitoken_name_and_dashed_txt_path():
    rec = preipo.parse_full_index_line(
        "1776 Acquisition Corp                         S-1/A     2115818     "
        "2026-08-03  edgar/data/2115818/0001213900-26-084648.txt")
    assert rec["company"] == "1776 Acquisition Corp"
    assert rec["form"] == "S-1/A"
    assert rec["accession"] == ACC_SPAC


def test_parse_full_index_line_old_concatenated_path():
    # pre-2025 quarters used the un-dashed 18-digit dir name: no dashed
    # accession to find -> accession stays empty, dedupe falls back to the
    # synthetic key (row_from_entry)
    rec = preipo.parse_full_index_line(
        "OLD STYLE INC                                 S-1       1869821     "
        "2024-01-01  edgar/data/1869821/000186982124000001/a10s1.htm")
    assert rec["accession"] == ""
    row = preipo.row_from_entry("1869821_S-1", {
        "cik": "1869821", "form": "S-1", "date": "2024-01-01"})
    assert row["accession"] == "1869821-S-1-20240101"


def test_parse_full_index_line_filters_and_headers():
    assert preipo.parse_full_index_line("x" * 30) is None
    hdr = ("Company-Name-and-Form                             CIK       "
           "Date-Filed  Filename")
    assert preipo.parse_full_index_line(hdr) is None
    assert preipo.parse_full_index_line("-" * 70) is None
    assert preipo.parse_full_index_line(
        "Last Data Received: October 15, 2026                    ") is None
    noise = ("NOISE INC                                   8-K       3333333 "
             "    2024-01-06  edgar/data/3333333/0003333333-24-000001/x.htm")
    assert preipo.parse_full_index_line(noise) is None


def test_categorize_v2_regex():
    assert preipo.categorize("1776 Acquisition Corp") == "fund"
    assert preipo.categorize("Some Income ETF Trust") == "fund"
    assert preipo.categorize("Blue Sky Capital Corp") == "fund"
    assert preipo.categorize("Widget Fund LLC") == "fund"
    assert preipo.categorize("NEWCO INC") == "company"
    assert preipo.categorize("") == "company"


def test_is_listed_ticker_exact_only():
    listed = {"AAPL", "IPOX"}
    assert preipo.is_listed_ticker("aapl", listed)
    assert preipo.is_listed_ticker("IPOX", listed)
    # v2 disabled the INC/CORP suffix heuristic: a corporate-suffixed name is
    # listed only if the exact string is in the universe
    assert not preipo.is_listed_ticker("NEWCO INC", listed)
    assert not preipo.is_listed_ticker("", listed)


def test_parse_daily_index_defensive_keys():
    payload = {"filings": [
        {"form": "S-1", "cik": "0001111111", "name": "NEWCO INC",
         "filingDate": "2026-10-15",
         "accessionNumber": "0001111111-26-000001"},
        {"form": "F-1", "cik": "1993097", "company": "3 E Network",
         "filing-date": "2026-10-14",
         "accession-number": "0001993097-26-000002"},
        {"form": "8-K", "cik": "320193", "filingDate": "2026-10-15"},
    ]}
    rows = preipo.parse_daily_index(payload)
    assert len(rows) == 2
    assert rows[0]["cik"] == "1111111"
    assert rows[0]["accession"] == "0001111111-26-000001"
    assert rows[1]["company"] == "3 E Network"
    assert rows[1]["accession"] == "0001993097-26-000002"
    assert all(r["form"] in preipo.S1_FORMS for r in rows)


def test_company_from_display():
    assert preipo.company_from_display(
        "ACME CORP (ACME) (CIK 0001234567)") == "ACME CORP"
    assert preipo.company_from_display(
        "ACME CORP (CIK 0001234567)") == "ACME CORP"
    assert preipo.company_from_display("") == ""


def test_parse_efts_hits():
    payload = {"hits": {"hits": [
        {"_source": {"ciks": ["0001234567"],
                     "display_names": ["ACME LTD (ACME) (CIK 0001234567)"],
                     "file_date": "2026-10-15", "form": "S-1",
                     "adsh": "0001234567-26-000001"}},
        {"_source": {"ciks": [], "form": "S-1"}},
    ]}}
    rows = preipo.parse_efts_hits(payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["cik"] == "1234567" and r["company"] == "ACME LTD"
    assert r["accession"] == "0001234567-26-000001"
    assert r["date"] == "2026-10-15"


def test_parse_submissions_rows_and_enrichment():
    p = subs(name="NEWCO INC", forms=("S-1", "8-K", "S-1/A"),
             dates=("2024-01-05", "2024-02-01", "2024-03-01"),
             accs=(ACC_A, "0001111111-24-000007", ACC_A2),
             pdocs=("s1.htm", "8k.htm", "s1a.htm"),
             tickers=["IPOX"], exchanges=["NYSE"])
    parsed = preipo.parse_submissions(p, CIK_A)
    assert parsed["name"] == "NEWCO INC"
    assert parsed["ticker"] == "IPOX"
    assert parsed["state_of_incorporation"] == "DE"
    assert parsed["sic"] == "2830"
    assert [r["form"] for r in parsed["s1_rows"]] == ["S-1", "S-1/A"]
    assert parsed["s1_rows"][1]["accession"] == ACC_A2
    assert parsed["s1_rows"][0]["primary_document"] == "s1.htm"
    assert parsed["status"] == "listed"        # 8-K present
    assert parsed["ipo_form"] == "8-K"


def test_classify_status():
    assert preipo.classify_status([]) == "rejected"
    assert preipo.classify_status(["S-1"]) == "preipo"
    assert preipo.classify_status(["S-1", "10-K"]) == "listed"
    assert preipo.classify_status(["S-1", "POS AM"]) == "rejected"
    assert preipo.classify_status(["S-1", "MBW"]) == "rejected"


def test_likely_primary_from_index():
    items = INDEX_JSON["directory"]["item"]
    assert preipo.likely_primary_from_index(items, ACC_A) == "primary.htm"
    assert preipo.likely_primary_from_index([], ACC_A) == ""


def test_html_to_text():
    raw = ("<html><script>var x='<b>';y</script><style>p{}</style>"
           "<body><h1>Title</h1><p>Some   text</p>"
           "<div>More<br>lines\n\n\n\nhere</div></body></html>")
    t = preipo.html_to_text(raw)
    assert "var x" not in t and "p{}" not in t
    assert "Title" in t and "More" in t
    assert "\n\n\n" not in t


# --------------------------------------------------------------------- flow tests
def test_bulk_seed_flow(monkeypatch):
    install(monkeypatch, FakeClient(base_payloads({
        FULL_URL_2024Q1: IDX_TEXT.encode(),
        SPAC_URL: SPAC_SUBS,
    })))
    rc = preipo.run(parse_args("--days", "0"))
    assert rc == 0
    st = json.loads(state_path().read_text())
    assert st["version"] == 2
    assert set(st["seen"]) == {"1111111_S-1", "2115818_S-1/A"}
    e = st["seen"]["1111111_S-1"]
    assert e["adsh"] == ACC_A and e["downloaded"] is False
    # submissions enrichment folded in (state of incorporation / industry)
    assert e["state_of_incorporation"] == "DE"
    events = read_jsonl(out_root() / "new.jsonl")
    assert [ev["event"] for ev in events] == ["new_filing", "new_filing"]
    rows = read_jsonl(out_root() / "all.jsonl")
    assert {r["accession"] for r in rows} == {ACC_A, ACC_SPAC}
    assert next(r for r in rows if r["form"] == "S-1/A")["category"] == "fund"


def test_bulk_skips_listed_names(monkeypatch):
    idx = IDX_TEXT.replace("NEWCO INC", "IPOX")
    install(monkeypatch, FakeClient(base_payloads({
        FULL_URL_2024Q1: idx.encode(),
    })))
    preipo.run(parse_args("--days", "0"))
    st = json.loads(state_path().read_text())
    assert "1111111_S-1" not in st["seen"]


def test_amendment_refreshes_adsh_and_pdoc(monkeypatch):
    sp = seed_state({"1111111_S-1": entry(CIK_A, "S-1", "2024-01-05",
                                          ACC_A, "s1.htm",
                                          downloaded=True)})
    install(monkeypatch, FakeClient(base_payloads({
        SUB_URL_A: subs(forms=("S-1",), dates=("2024-03-01",),
                        accs=(ACC_A2,), pdocs=("s1a.htm",)),
    })))
    preipo.run(parse_args("--days", "0", "--no-ipo-check"))
    st = json.loads(sp.read_text())
    e = st["seen"]["1111111_S-1"]
    # preipo_s1_monitor fix kept: amendment refreshes accession + primary doc
    assert e["date"] == "2024-03-01"
    assert e["adsh"] == ACC_A2
    assert e["pdoc"] == "s1a.htm"
    assert e["downloaded"] is False
    events = read_jsonl(out_root() / "new.jsonl")
    assert events[0]["event"] == "amendment"
    assert events[0]["accession"] == ACC_A2
    rows = read_jsonl(out_root() / "all.jsonl")
    assert rows[-1]["accession"] == ACC_A2


def test_ipo_sweep_by_listed_ticker(monkeypatch):
    sp = seed_state({"1111111_S-1": entry(CIK_A, "S-1", "2024-01-05", ACC_A)})
    install(monkeypatch, FakeClient(base_payloads({
        SUB_URL_A: subs(tickers=["IPOX"], exchanges=["NYSE"]),
    })))
    preipo.run(parse_args("--days", "0"))
    events = read_jsonl(out_root() / "new.jsonl")
    assert [ev["event"] for ev in events] == ["ipo_detected"]
    assert events[0]["ticker"] == "IPOX"
    assert json.loads(sp.read_text())["seen"]["1111111_S-1"]["ipo_detected"]\
        is True


def test_public_report_counts_as_ipo_without_ticker(monkeypatch):
    # older desktop signal: a first public report confirms listing even with no
    # ticker present in the universe file
    seed_state({"1111111_S-1": entry(CIK_A, "S-1", "2024-01-05", ACC_A)})
    install(monkeypatch, FakeClient(base_payloads({
        SUB_URL_A: subs(forms=("S-1", "10-K"),
                        dates=("2024-01-05", "2024-06-01"),
                        accs=(ACC_A, ACC_A2),
                        pdocs=("s1.htm", "10k.htm")),
    })))
    preipo.run(parse_args("--days", "0"))
    events = read_jsonl(out_root() / "new.jsonl")
    assert events[0]["event"] == "ipo_detected"
    assert events[0]["ipo_form"] == "10-K"


def test_new_sibling_form_from_listed_name_inventoried_quietly(monkeypatch):
    # v2: submissions rows whose company name is listed enter the tracker
    # without a new_filing alert
    seed_state({"2222222_S-1": entry(CIK_B, "S-1", "2024-01-05",
                                     "0002222222-24-000001",
                                     company="IPOX")})
    sp = state_path()
    install(monkeypatch, FakeClient(base_payloads({
        preipo.submissions_url(CIK_B): subs(
            name="IPOX", forms=("S-1", "S-1/A"),
            dates=("2024-01-05", "2024-02-10"),
            accs=("0002222222-24-000001", "0002222222-24-000002"),
            pdocs=("s1.htm", "s1a.htm")),
    })))
    preipo.run(parse_args("--days", "0", "--no-ipo-check"))
    st = json.loads(sp.read_text())
    assert "2222222_S-1/A" in st["seen"]      # inventoried …
    assert read_jsonl(out_root() / "new.jsonl") == []   # … but no alert


def test_download_docs_flow(monkeypatch):
    seed_state({
        "1111111_S-1": entry(CIK_A, "S-1", "2024-01-05", ACC_A),
        "1111111_F-1": entry(CIK_A, "F-1", "2024-01-09",
                             "0001111111-24-000009", "tiny.htm"),
    })
    sp = state_path()
    doc_url = ("https://www.sec.gov/Archives/edgar/data/1111111/"
               + ACC_A.replace("-", "") + "/s1.htm")
    small_url = ("https://www.sec.gov/Archives/edgar/data/1111111/"
                 "000111111124000009/tiny.htm")
    install(monkeypatch, FakeClient(base_payloads({
        SUB_URL_A: subs(forms=("S-1", "F-1"),
                        dates=("2024-01-05", "2024-01-09"),
                        accs=(ACC_A, "0001111111-24-000009"),
                        pdocs=("s1.htm", "tiny.htm")),
        doc_url: GOOD_DOC,
        small_url: SMALL_DOC,
    })))
    preipo.run(parse_args("--days", "0", "--no-ipo-check",
                          "--download-docs"))
    docs = out_root() / "docs" / CIK_A / ACC_A.replace("-", "")
    assert (docs / "s1.htm").read_bytes() == GOOD_DOC
    assert (docs / "s1.htm.txt").read_text().startswith("PROSPECTUS TEXT")
    st = json.loads(sp.read_text())
    assert st["seen"]["1111111_S-1"]["downloaded"] is True
    assert "1111111_S-1|" + ACC_A + "|s1.htm" in \
        (sp.parent / "done.txt").read_text()
    # >500-byte gate (SEC serves tiny error pages under HTTP 200): the small
    # body is marked failed, nothing stored, downloaded flag stays False
    assert "1111111_F-1" in (sp.parent / "failed.txt").read_text()
    assert st["seen"]["1111111_F-1"]["downloaded"] is False
    assert not (out_root() / "docs" / CIK_A / "000111111124000009").exists()
    rows = read_jsonl(out_root() / "all.jsonl")
    dl = [r for r in rows if r["downloaded"]]
    assert dl and dl[0]["doc_path"].startswith("docs/" + CIK_A)


def test_empty_pdoc_resolved_via_index_json(monkeypatch):
    # older monitor edge case: submissions row without primaryDocument
    seed_state({"1111111_S-1": entry(CIK_A, "S-1", "2024-01-05", ACC_A)})
    accn = ACC_A.replace("-", "")
    idx_url = "https://www.sec.gov/Archives/edgar/data/1111111/" + accn + \
        "/index.json"
    doc_url = "https://www.sec.gov/Archives/edgar/data/1111111/" + accn + \
        "/primary.htm"
    install(monkeypatch, FakeClient(base_payloads({
        SUB_URL_A: subs(pdocs=("",)),
        idx_url: INDEX_JSON,
        doc_url: GOOD_DOC,
    })))
    preipo.run(parse_args("--days", "0", "--no-ipo-check",
                          "--download-docs"))
    assert (out_root() / "docs" / CIK_A / accn / "primary.htm") \
        .read_bytes() == GOOD_DOC


def test_rerun_is_idempotent(monkeypatch):
    payloads = base_payloads({FULL_URL_2024Q1: IDX_TEXT.encode(),
                              SPAC_URL: SPAC_SUBS})
    install(monkeypatch, FakeClient(payloads))
    args = parse_args("--days", "0")
    preipo.run(args)
    n_all = len(read_jsonl(out_root() / "all.jsonl"))
    n_new = len(read_jsonl(out_root() / "new.jsonl"))
    fake = install(monkeypatch, FakeClient(payloads))
    preipo.run(args)
    assert len(read_jsonl(out_root() / "all.jsonl")) == n_all
    assert len(read_jsonl(out_root() / "new.jsonl")) == n_new
    # one submissions fetch per CIK per run (memoized across all phases)
    assert fake.calls.count(SUB_URL_A) == 1


def test_dry_run_writes_nothing(monkeypatch):
    install(monkeypatch, FakeClient(base_payloads(
        {FULL_URL_2024Q1: IDX_TEXT.encode()})))
    preipo.run(parse_args("--days", "0", "--dry-run"))
    assert not state_path().exists()
    assert not (out_root() / "new.jsonl").exists()
    assert not (out_root() / "all.jsonl").exists()


def test_daily_index_403_falls_back_to_efts(monkeypatch):
    today = _dt.date.today()
    d0 = preipo.daily_index_url(today)
    boom = urllib.error.HTTPError(d0, 403, "Undeclared Automated Tool",
                                  {}, None)
    efts_s1 = preipo.efts_url("S-1", (today - _dt.timedelta(days=1))
                              .isoformat(), today.isoformat())
    install(monkeypatch, FakeClient(base_payloads({
        d0: boom,
        efts_s1: {"hits": {"hits": [{
            "_source": {"ciks": ["0003333333"],
                        "display_names": ["EFTS CO (CIK 0003333333)"],
                        "file_date": "2026-10-15", "form": "S-1",
                        "adsh": "0003333333-26-000001"}}]}},
    })))
    rc = preipo.run(parse_args("--days", "1", "--no-ipo-check",
                               "--bulk-start-year", "2026"))
    assert rc == 0
    st = json.loads(state_path().read_text())
    assert "3333333_S-1" in st["seen"]
    events = read_jsonl(out_root() / "new.jsonl")
    assert events[0]["source"] == "efts"


def test_state_v1_migration_keeps_seen(monkeypatch):
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({
        "version": 1, "seen": {"9999_S-1": entry("9999", "S-1",
                                                 "2023-01-01", "")},
        "last_check": "2023"}))
    install(monkeypatch, FakeClient(base_payloads()))
    preipo.run(parse_args("--days", "0", "--no-ipo-check"))
    st = json.loads(sp.read_text())
    assert st["version"] == 2
    assert "9999_S-1" in st["seen"]
    assert "last_check" not in st


def test_universe_filter(monkeypatch, tmp_path):
    uni = tmp_path / "uni.txt"
    uni.write_text("NCO\n# comment\nMISSING\n")
    monkeypatch.setattr(
        ticker, "cik_for",
        lambda sym, client=None, overrides_path=None:
        {"NCO": CIK_A}[sym.upper()])
    install(monkeypatch, FakeClient(base_payloads(
        {FULL_URL_2024Q1: IDX_TEXT.encode()})))
    preipo.run(parse_args("--days", "0", "--universe", str(uni)))
    st = json.loads(state_path().read_text())
    assert "1111111_S-1" in st["seen"]
    assert "2115818_S-1/A" not in st["seen"]   # outside the universe


def test_quarters_window():
    qs = preipo.quarters_to_date(2024, end=_dt.date(2025, 5, 1))
    assert qs[0] == (2024, 1) and qs[-1] == (2025, 2)
