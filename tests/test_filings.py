"""Offline tests for secdata.filings — canned EDGAR JSON/index + fake client.

No network: every fetch goes through a FakeClient installed by monkeypatching
``filings.default_client`` (and ``ticker.cik_for`` for symbol resolution).
"""
import argparse
import gzip
import json
import urllib.error

import pytest

from secdata import filings, storage, ticker

# --------------------------------------------------------------------- canned data
CIK = "320193"
ACC_K = "0000320193-24-000123"
ACC_KA = "0000320193-24-000150"
ACC_8K = "0000320193-24-000130"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
PRIMARY_URL = ("https://www.sec.gov/Archives/edgar/data/320193/"
               "000032019324000123/aapl-20241031.htm")
FULL_URL = ("https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019324000123/" + ACC_K + ".txt")
IDX_URL_2024Q4 = ("https://www.sec.gov/Archives/edgar/full-index/"
                  "2024/QTR4/master.idz")

SUB = {
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "accessionNumber": [ACC_K, ACC_KA, ACC_8K],
            "form": ["10-K", "10-K/A", "8-K"],
            "filingDate": ["2024-11-01", "2024-12-05", "2024-11-15"],
            "reportDate": ["2024-09-28", "2024-09-28", "2024-11-14"],
            "acceptanceDateTime": ["2024-11-01T18:04:12-05:00",
                                   "2024-12-05T09:00:00-05:00",
                                   "2024-11-15T08:30:00-05:00"],
            "primaryDocument": ["aapl-20241031.htm", "aapl-20241031a.htm",
                                "aapl-8k.htm"],
            "items": ["", "", "2.02"],
        },
        "files": [{
            "type": "historical",
            "name": "CIK0000320193-submissions-001.json",
            "url": "CIK0000320193-submissions-001.json",
        }],
    },
}

HIST = {  # flat column block (EDGAR history-file shape)
    "accessionNumber": ["0000320193-09-000123"],
    "form": ["10-K"],
    "filingDate": ["2009-12-31"],
    "reportDate": ["2009-09-26"],
    "acceptanceDateTime": ["2009-12-31T17:00:00-05:00"],
    "primaryDocument": ["old10k.htm"],
    "items": ["8.01"],
}
HIST_URL = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"

GOOD_PRIMARY = b"<html>" + b"x" * 600  # production guard: > 500 bytes
GOOD_FULL = b"Content-Type: TEXT/PLAIN\r\n" + b"y" * 2000
MASTER_IDX = (
    "CIK|Company Name|Form Type|Date Filed|Filename\r\n"
    "-----------------------------------------------------------------------\r\n"
    "320193|APPLE INC.|8-K|2024-11-15|"
    "edgar/data/320193/000032019324000130/aapl-8k.htm\r\n"
    "1018724|GOOGLE INC.|10-K|2024-02-01|"
    "edgar/data/1018724/000101872424000013/abc.htm\r\n"
    "\r\n")


class FakeClient:
    def __init__(self, payloads=None):
        self.payloads = dict(payloads or {})
        self.calls = []

    def get_bytes(self, url, max_age_hours=None):
        self.calls.append(url)
        v = self.payloads.get(url)
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
    filings.add_arguments(p)
    return p.parse_args(list(argv))


def install(monkeypatch, fake):
    monkeypatch.setattr(filings, "default_client", lambda: fake)

    def _cik_for(symbol, client=None, overrides_path=None):
        return {"AAPL": "320193", "GOOGL": "1018724"}[symbol.upper()]

    monkeypatch.setattr(ticker, "cik_for", _cik_for)
    return fake


def default_payloads():
    return {SUBMISSIONS_URL: SUB, PRIMARY_URL: GOOD_PRIMARY,
            FULL_URL: GOOD_FULL}


# --------------------------------------------------------------------- pure helpers
def test_quarters_between():
    from datetime import date
    assert filings.quarters_between(date(2024, 1, 1), date(2024, 12, 31)) == \
        [(2024, 1), (2024, 2), (2024, 3), (2024, 4)]
    # production enumeration: wraps q>4 -> next year, inclusive window
    assert filings.quarters_between(date(2023, 11, 5), date(2024, 2, 1)) == \
        [(2023, 4), (2024, 1)]
    assert filings.quarters_between(date(2024, 5, 1), date(2024, 5, 1)) == \
        [(2024, 2)]


def test_parse_index():
    row = filings.parse_index("320193|APPLE INC.|8-K|2024-11-15|"
                              "edgar/data/320193/x/aapl-8k.htm")
    assert row == {"cik": "320193", "company": "APPLE INC.", "form": "8-K",
                   "filed_at": "2024-11-15",
                   "filename": "edgar/data/320193/x/aapl-8k.htm"}
    assert filings.parse_index("") is None
    assert filings.parse_index("1|two|three|four") is None  # < 5 fields


def test_decode_index_bytes():
    assert filings.decode_index_bytes("a\r\nb".encode("utf-8"))[:2] == \
        ["a\r", "b"]
    # non-utf8 bytes (cp1252 company names in production indexes) -> latin-1
    out = filings.decode_index_bytes("1|CAFEé INC.|10-K|2024-01-01|x|".encode("latin-1"))
    assert "CAFEé" in out[0]


def test_select_rows_exact_is_production_semantics():
    rows = filings.select_rows([SUB["filings"]["recent"]], forms=["10-K"])
    assert [r["accession"] for r in rows] == [ACC_K]  # 10-K/A NOT matched
    rows = filings.select_rows([SUB["filings"]["recent"]],
                               forms=["10-K", "10-K/A"])
    assert [r["accession"] for r in rows] == [ACC_K, ACC_KA]


def test_select_rows_include_amendments_and_window():
    rows = filings.select_rows([SUB["filings"]["recent"]], forms=["10-K"],
                               include_amendments=True)
    assert [r["accession"] for r in rows] == [ACC_K, ACC_KA]
    # window inclusive on both ends
    rows = filings.select_rows([SUB["filings"]["recent"]],
                               since="2024-11-15", until="2024-11-15")
    assert [r["accession"] for r in rows] == [ACC_8K]
    # empty blocks are skipped without KeyError
    assert filings.select_rows([{}, None]) == []


def test_select_rows_historical_block():
    rows = filings.select_rows([SUB["filings"]["recent"], HIST],
                               forms=["10-K"], since="2000-01-01")
    assert ACC_K in [r["accession"] for r in rows]
    assert "0000320193-09-000123" in [r["accession"] for r in rows]


def test_plan_docs_mirrors_edgar_paths():
    row = {"accession": ACC_K, "primary_document": "aapl-20241031.htm"}
    plans = filings.plan_docs(CIK, row, "both")
    assert plans == [("primary", PRIMARY_URL, "aapl-20241031.htm"),
                     ("full", FULL_URL, ACC_K + ".txt")]
    # missing primaryDocument (production: skip it)
    assert filings.plan_docs(CIK, {"accession": ACC_K,
                                   "primary_document": ""}, "primary") == []
    assert filings.plan_docs(CIK, row, "none") == []


def test_is_valid_doc_body():
    assert filings.is_valid_doc_body("primary", GOOD_PRIMARY)
    assert not filings.is_valid_doc_body("primary", b"x" * 500)  # need > 500
    assert not filings.is_valid_doc_body("primary", b"")
    assert filings.is_valid_doc_body("full", b"y")
    assert not filings.is_valid_doc_body("full", b"")


def test_resolve_targets(tmp_path, monkeypatch):
    uni = tmp_path / "uni.txt"
    uni.write_text("# comment\nMSFT-not-mapped\n\n")  # unresolvable, counted
    monkeypatch.setattr(ticker, "cik_for",
                        lambda s, client=None, overrides_path=None:
                        {"AAPL": "320193"}[s.upper()])
    targets, unresolved = filings.resolve_targets(
        symbols=["AAPL", "1018724", "AAPL", "BAD"],
        ciks=["320193"], universe=str(uni))
    # raw-numeric tokens resolve directly to CIKs (no ticker map needed)
    assert {"symbol": None, "cik": "1018724"} in targets
    # dedupe across CIK/ticker forms: 320193 listed once (CIK token wins)
    assert [t for t in targets if t["cik"] == "320193"] == \
        [{"symbol": None, "cik": "320193"}]
    # BAD + MSFT-not-mapped fail but do not abort the run
    assert not any(t["symbol"] in ("BAD", "MSFT-NOT-MAPPED") for t in targets)
    assert unresolved == 2


def test_resolve_targets_missing_universe(tmp_path, monkeypatch):
    monkeypatch.setattr(ticker, "cik_for",
                        lambda s, client=None, overrides_path=None: "320193")
    with pytest.raises(FileNotFoundError):
        filings.resolve_targets(universe=str(tmp_path / "nope.txt"))


# --------------------------------------------------------------------- flow tests
def test_run_downloads_manifest_checkpoint_and_resumes(tmp_path, monkeypatch):
    fake = install(monkeypatch, FakeClient(default_payloads()))
    args = parse_args("AAPL", "--forms", "10-K", "--docs", "both",
                      "--since", "2024-01-01", "--until", "2024-12-31",
                      "--workers", "1")
    assert filings.run(args) == 0

    root = tmp_path / "out" / "filings"
    rows = list(storage.read_jsonl(root / "manifest.jsonl"))
    assert len(rows) == 1
    row = rows[0]
    assert row["accession"] == ACC_K and row["status"] == "ok"
    assert row["symbol"] == "AAPL" and row["company"] == "Apple Inc."
    assert row["form"] == "10-K" and row["filing_date"] == "2024-11-01"
    # local layout mirrors EDGAR archive paths (unpadded CIK, dash-free dir)
    p = root / "docs" / "320193" / "000032019324000123"
    assert (p / "aapl-20241031.htm").read_bytes() == GOOD_PRIMARY
    assert (p / (ACC_K + ".txt")).read_bytes() == GOOD_FULL
    assert row["docs"] == {"full": "docs/320193/000032019324000123/"
                                   + ACC_K + ".txt",
                           "primary": "docs/320193/000032019324000123/"
                                      "aapl-20241031.htm"}
    done = (root / "checkpoint" / "done.txt").read_text()
    assert "320193_" + ACC_K in done  # production checkpoint key
    state = json.loads((root / "state.json").read_text())
    assert state["runs"] == 1 and state["last_run"]["filings"] == 1

    # re-run: checkpoint skips the accession, no doc re-fetches
    fake2 = install(monkeypatch, FakeClient(default_payloads()))
    assert filings.run(args) == 0
    assert fake2.calls == [SUBMISSIONS_URL]  # only submissions, docs skipped


def test_run_inventory_only(tmp_path, monkeypatch):
    fake = install(monkeypatch, FakeClient(default_payloads()))
    args = parse_args("--cik", "320193", "--forms", "10-K,8-K",
                      "--docs", "none")
    assert filings.run(args) == 0
    assert fake.calls == [SUBMISSIONS_URL]  # inventory never fetches docs
    rows = list(storage.read_jsonl(tmp_path / "out" / "filings" /
                                   "manifest.jsonl"))
    assert [r["accession"] for r in rows] == [ACC_K, ACC_8K]
    assert all(r["status"] == "inventory" for r in rows)


def test_run_tiny_primary_marked_failed_then_retry_failed(tmp_path,
                                                          monkeypatch):
    payloads = default_payloads()
    payloads[PRIMARY_URL] = b"<html>error</html>"  # small SEC error page body
    install(monkeypatch, FakeClient(payloads))
    args = parse_args("AAPL", "--forms", "10-K", "--docs", "primary")
    assert filings.run(args) == 1  # production treated this as a failure
    root = tmp_path / "out" / "filings"
    assert "320193_" + ACC_K in (root / "checkpoint" / "failed.txt").read_text()
    assert not (root / "docs" / "320193" / "000032019324000123" /
                 "aapl-20241031.htm").exists()

    # second run WITHOUT --retry-failed: skipped, stays failed (production)
    fake = install(monkeypatch, FakeClient(default_payloads()))
    assert filings.run(args) == 0
    assert fake.calls == [SUBMISSIONS_URL]

    # --retry-failed re-attempts and now succeeds
    fake = install(monkeypatch, FakeClient(default_payloads()))
    args = parse_args("AAPL", "--forms", "10-K", "--docs", "primary",
                      "--retry-failed")
    assert filings.run(args) == 0
    assert fake.calls == [SUBMISSIONS_URL, PRIMARY_URL]
    assert (root / "docs" / "320193" / "000032019324000123" /
            "aapl-20241031.htm").exists()


def test_run_scan_uses_quarterly_full_index(tmp_path, monkeypatch):
    payloads = default_payloads()
    payloads[IDX_URL_2024Q4] = gzip.compress(MASTER_IDX.encode("utf-8"))
    # the 8-K's primary doc lives under its own accession dir
    payloads["https://www.sec.gov/Archives/edgar/data/320193/"
             "000032019324000130/aapl-8k.htm"] = GOOD_PRIMARY
    fake = install(monkeypatch, FakeClient(payloads))
    args = parse_args("--scan", "--forms", "8-K",
                      "--since", "2024-10-01", "--until", "2024-12-31",
                      "--docs", "primary", "--workers", "1")
    assert filings.run(args) == 0
    assert IDX_URL_2024Q4 in fake.calls
    # scan discovered only CIK 320193 (GOOGL 10-K row filtered out by form)
    assert SUBMISSIONS_URL in fake.calls
    assert "https://data.sec.gov/submissions/CIK0001018724.json" not in fake.calls
    assert (tmp_path / "out" / "filings" / "docs" / "320193" /
            "000032019324000130" / "aapl-8k.htm").exists()


def test_run_include_historical(tmp_path, monkeypatch):
    payloads = default_payloads()
    payloads[HIST_URL] = HIST
    fake = install(monkeypatch, FakeClient(payloads))
    args = parse_args("AAPL", "--forms", "10-K", "--docs", "none",
                      "--include-historical", "--since", "2000-01-01")
    assert filings.run(args) == 0
    assert fake.calls == [SUBMISSIONS_URL, HIST_URL]
    rows = list(storage.read_jsonl(tmp_path / "out" / "filings" /
                                   "manifest.jsonl"))
    assert len(rows) == 2


def test_run_usage_errors(tmp_path, monkeypatch):
    install(monkeypatch, FakeClient(default_payloads()))
    assert filings.run(parse_args()) == 2                    # no targets
    assert filings.run(parse_args("--scan", "--docs", "none")) == 2  # no forms
    assert filings.run(parse_args("--scan", "--forms", "10-K",
                                  "--docs", "none")) == 2    # no --since
    assert filings.run(parse_args("AAPL", "--since", "2025-01-01",
                                  "--until", "2024-01-01")) == 2
    bad = parse_args("--scan", "--forms", "10-K", "--since", "2024-10-01",
                     "--until", "nonsense")
    assert filings.run(bad) == 2


def test_run_unresolved_ticker_exits_1(tmp_path, monkeypatch):
    install(monkeypatch, FakeClient(default_payloads()))
    args = parse_args("AAPL", "NOTREAL", "--forms", "10-K", "--docs", "none")
    assert filings.run(args) == 1  # production kept going but flagged errors
