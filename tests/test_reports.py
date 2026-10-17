"""Offline tests for secdata.reports (DESIGN.md rule 7).

Pure parse/filter/missing-detection functions get canned index & submissions
text; the two engines get flow tests against a fake client (no network, no
cache dir) with SEC_EDGAR_OUT pointed at tmp_path.
"""
import json
import sys
import urllib.error
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:  # tolerate a venv without an editable install
    sys.path.insert(0, str(SRC))

from secdata import reports, ticker  # noqa: E402


# --------------------------------------------------------------------- fixtures
def make_args(**kw):
    base = dict(full=False, days=0, ticker="", universe="", after="",
                forms="", overrides="", workers=0, limit=0, dry_run=False,
                backfill_meta=False)
    base.update(kw)
    return Namespace(**base)


class FakeClient:
    """URL -> payload dict; missing key raises HTTP 404 like EDGAR does for
    non-trading days / absent docs."""

    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.calls = []

    def get_bytes(self, url, max_age_hours=None):
        self.calls.append(url)
        v = self.payloads.get(url)
        if v is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return v if isinstance(v, bytes) else str(v).encode()

    def get_json(self, url, max_age_hours=None):
        return json.loads(self.get_bytes(url, max_age_hours).decode("utf-8"))


BIG_HTML = (b"<html><head><Title>AAPL-20260630 10-Q</Title>"
            b"<xbrl><dei:DocumentFiscalYearFocus contextRef='c'>2026"
            b"</dei:DocumentFiscalYearFocus>"
            b"<dei:DocumentFiscalPeriodFocus contextRef='c'>Q3"
            b"</dei:DocumentFiscalPeriodFocus></xbrl></head><body>"
            + b"x" * 4096 + b"</body></html>")

IDX_ROW = ("0000320193|APPLE INC|10-K|2026-10-01|"
           "edgar/data/320193/000032019326000101/0000320193-26-000101.txt")


def subs_url(cik10):
    return f"https://data.sec.gov/submissions/CIK{cik10}.json"


def sub_json(acc="0000320193-26-000101", doc="aapl10k.htm"):
    return json.dumps({"filings": {"recent": {
        "form": ["10-K"],
        "accessionNumber": [acc],
        "filingDate": ["2026-10-01"],
        "primaryDocument": [doc],
    }}})


# ------------------------------------------------------------------ pure: index
def test_qtr_of():
    assert reports.qtr_of("20260115") == "QTR1"
    assert reports.qtr_of("20260401") == "QTR2"
    assert reports.qtr_of("20260630") == "QTR2"
    assert reports.qtr_of("20260701") == "QTR3"
    assert reports.qtr_of("20261231") == "QTR4"


def test_parse_master_idx_filters_and_normalises():
    body = (
        "CIK|Company Name|Form Type|Date Filed|File Name\n"
        "Forms 8-K, 12b-25 etc.\n"
        "-------------------------------------------------------------\n"
        + IDX_ROW + "\n"
        "1823887|MICROSOFT CORP|10-Q|2026-10-01|"
        "edgar/data/1823887/000182388726000045/0001823887-26-000045.txt\n"
        "0000320193|APPLE INC|8-K|2026-10-01|"
        "edgar/data/320193/000032019326000102/0000320193-26-000102.txt\n"
        "0000320193|APPLE INC|NT 10-K|2026-10-01|"
        "edgar/data/320193/000032019326000103/0000320193-26-000103.txt\n"
        "0001045810|MEI CHEM|20-F/A|2026-10-01|"
        "edgar/data/1045810/000104581026000003/0001045810-26-000003.txt\n"
        + IDX_ROW + "\n"          # duplicate accession -> deduped
        "0000831001|SHORT LINE|10-K\n"   # <5 fields
    ).encode("latin-1")
    out = reports.parse_master_idx(body)
    assert out == [
        ("0000320193", "10-K", "0000320193-26-000101"),
        ("0001823887", "10-Q", "0001823887-26-000045"),
        ("0001045810", "20-F/A", "0001045810-26-000003"),
    ]


def test_parse_master_idx_custom_forms():
    body = ("0000320193|APPLE INC|8-K|2026-10-01|"
            "edgar/data/320193/000032019326000102/0000320193-26-000102.txt"
            ).encode()
    assert reports.parse_master_idx(body, forms={"8-K"}) == [
        ("0000320193", "8-K", "0000320193-26-000102")]


def test_select_missing_skips_unmapped_and_have():
    parsed = [("0000320193", "10-K", "A1"),
              ("0000913407", "10-K", "A2"),   # CIK not in map (fund etc.)
              ("0001045810", "20-F/A", "A3"),
              ("0000320193", "10-Q", "A4")]
    c2t = {"0000320193": "AAPL", "0001045810": "NVDA"}
    have = {("NVDA", "20-F/A", "A3")}
    out = reports.select_missing(parsed, c2t,
                                 lambda t, f, a: (t, f, a) in have)
    assert out == [("AAPL", "0000320193", "10-K", "A1"),
                   ("AAPL", "0000320193", "10-Q", "A4")]


def test_select_missing_shared_seen_across_days():
    parsed = [("0000320193", "10-K", "A1")]
    c2t = {"0000320193": "AAPL"}
    seen = set()
    always_missing = lambda t, f, a: False  # noqa: E731
    assert len(reports.select_missing(parsed, c2t, always_missing, seen)) == 1
    assert reports.select_missing(parsed, c2t, always_missing, seen) == []


def test_reverse_cik_map_last_symbol_wins_sorted():
    t2c = {"GOOG": "0001652044", "GOOGL": "0001652044", "AAPL": "0000320193"}
    assert reports.reverse_cik_map(t2c) == {
        "0001652044": "GOOGL", "0000320193": "AAPL"}


@pytest.mark.parametrize("last,today,expected", [
    (None, date(2026, 10, 17), 5),
    ("garbage", date(2026, 10, 17), 5),
    ("2026-10-17", date(2026, 10, 17), 3),   # floor 3
    ("2026-10-15", date(2026, 10, 17), 3),
    ("2026-10-13", date(2026, 10, 17), 5),
    ("2026-09-01", date(2026, 10, 17), 7),   # ceiling 7
])
def test_default_window_days(last, today, expected):
    assert reports.default_window_days(last, today=today) == expected


# ------------------------------------------------------------ pure: submissions
SUB = {"filings": {"recent": {
    "form": ["10-K", "8-K", "10-Q/A", "10-K", "20-F", "10-K"],
    "accessionNumber": ["A1", "A2", "A3", "A4", "A5", "A6"],
    "filingDate": ["2025-11-01", "2025-12-01", "2026-02-01", "2023-11-01",
                   "2026-04-01", ""],
    "primaryDocument": ["a1.htm", "a2.htm", "a3.htm", "a4.htm", "", ""],
}}}


def test_filings_from_submissions_exact_set_and_required_fields():
    rows = reports.filings_from_submissions(SUB)
    accs = [r["accession"] for r in rows]
    # A5/A6: empty primaryDocument -> dropped (production required doc)
    # 8-K (A2) not in the exact set; amendment A3 kept; A4 kept (no cutoff)
    assert accs == ["A1", "A3", "A4"]


def test_filings_from_submissions_after_cutoff():
    sub = json.loads(json.dumps(SUB))
    sub["filings"]["recent"]["primaryDocument"][3] = "a4.htm"
    rows = reports.filings_from_submissions(sub, after="2024-01-01")
    assert [r["accession"] for r in rows] == ["A1", "A3"]
    rows = reports.filings_from_submissions(sub)
    assert [r["accession"] for r in rows] == ["A1", "A3", "A4"]


def test_primary_from_submissions():
    data = {"filings": {"recent": {
        "accessionNumber": ["A1", "A2"], "primaryDocument": ["d1.htm", ""]}}}
    assert reports.primary_from_submissions(data, "A1") == "d1.htm"
    assert reports.primary_from_submissions(data, "A2") is None
    assert reports.primary_from_submissions(data, "ZZ") is None


def test_primary_from_dir_index():
    data = {"directory": {"item": [{"name": "x.txt"}, {"name": "R.HTM"},
                                   {"name": "y.html"}]}}
    assert reports.primary_from_dir_index(data) == "R.HTM"
    assert reports.primary_from_dir_index({"directory": {"item": []}}) is None


# ------------------------------------------------------------------- pure: meta
def test_extract_meta_annual_fy():
    html = (b"<Title>AAPL-20251231 10-K</Title>"
            b"<dei:DocumentFiscalYearFocus contextRef='c'>2025</dei>"
            b"<dei:DocumentFiscalPeriodFocus contextRef='c'>FY</dei>")
    m = reports.extract_meta(html)
    assert m["period_end"] == "2025-12-31"
    assert m["fiscal_year"] == 2025
    assert m["fiscal_quarter"] == "FY"
    assert m["label"] == "FY2025 Annual · 2025-12-31"


def test_extract_meta_quarter():
    m = reports.extract_meta(BIG_HTML)
    assert m["period_end"] == "2026-06-30"
    assert m["fiscal_year"] == 2026
    assert m["fiscal_quarter"] == "Q3"
    assert m["label"] == "FY2026 Q3 · 2026-06-30"


def test_extract_meta_period_end_fallback_and_label():
    html = (b"<html><head><Title>no date here</Title>"
            b"<dei:DocumentPeriodEndDate contextRef='c'>2026-03-31"
            b"</dei:DocumentPeriodEndDate></head>")
    m = reports.extract_meta(html)
    assert m == {"period_end": "2026-03-31", "label": "2026-03-31"}


def test_extract_meta_garbage_is_none():
    assert reports.extract_meta(b"<html>nothing of interest</html>") is None


# ------------------------------------------------------------------ pure: misc
def test_parse_forms():
    assert reports.parse_forms("") is reports.TARGET_FORMS
    assert reports.parse_forms("8-k, 6-K ") == {"8-K", "6-K"}


def test_read_universe(tmp_path):
    p = tmp_path / "uni.txt"
    p.write_text("# comment\n\n AAPL \nbrk.b\n")
    assert reports.read_universe(p) == {"AAPL", "BRK-B"}
    empty = tmp_path / "e.txt"
    empty.write_text("# nothing\n")
    with pytest.raises(ValueError):
        reports.read_universe(empty)
    with pytest.raises(OSError):
        reports.read_universe(tmp_path / "nope.txt")


def test_lookup_symbol_normalises():
    t2c = {"AAPL": "0000320193", "BRK-B": "0001067983"}
    assert reports.lookup_symbol(t2c, "aapl") == "AAPL"
    assert reports.lookup_symbol(t2c, "BRK.B") == "BRK-B"
    assert reports.lookup_symbol(t2c, "NOPE") is None


# ---------------------------------------------------------------- filesystem
def test_local_has_size_floor(tmp_path):
    big = tmp_path / "AAPL" / "10-K" / "ACC1"
    big.mkdir(parents=True)
    (big / "primary-document.html").write_bytes(b"x" * 2000)
    small = tmp_path / "AAPL" / "10-Q" / "ACC2"
    small.mkdir(parents=True)
    (small / "primary-document.html").write_bytes(b"x" * 10)
    assert reports.local_has(tmp_path, "AAPL", "10-K", "ACC1")
    assert not reports.local_has(tmp_path, "AAPL", "10-Q", "ACC2")
    assert not reports.local_has(tmp_path, "AAPL", "10-Q", "NOPE")


def test_scan_local_ticker_handles_amendment_nesting(tmp_path):
    docs = tmp_path / "AAPL"
    for form, acc in [("10-K", "A1"), ("10-K/A", "A2"), ("10-Q", "A3")]:
        d = docs / form / acc
        d.mkdir(parents=True)
        (d / "primary-document.html").write_bytes(b"x" * 2000)
    tiny = docs / "10-Q" / "A4"
    tiny.mkdir(parents=True)
    (tiny / "primary-document.html").write_bytes(b"x")   # below 1 KiB floor
    assert reports.scan_local_ticker(tmp_path, "AAPL") == {
        ("10-K", "A1"), ("10-K/A", "A2"), ("10-Q", "A3")}


# --------------------------------------------------------------------- flow
@pytest.fixture
def flow(tmp_path, monkeypatch):
    """Incremental-engine wiring: fake client, canned ticker map, out in tmp."""
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    monkeypatch.setattr(ticker, "_MAP", {"AAPL": "320193", "MSFT": "1823887"})
    y = date.today() - timedelta(days=1)
    ymd = y.strftime("%Y%m%d")
    idx_url = reports.INDEX_URL.format(year=y.strftime("%Y"),
                                       qtr=reports.qtr_of(ymd), ymd=ymd)
    acc = "0000320193-26-000101"
    doc_url = ticker.archive_url("320193", acc, "aapl10k.htm")
    fake = FakeClient({
        idx_url: IDX_ROW + "\n",
        subs_url("0000320193"): sub_json(acc=acc),
        doc_url: BIG_HTML,
    })
    monkeypatch.setattr(reports, "default_client", lambda: fake)
    return tmp_path, fake, acc


def test_flow_incremental_download_resume_watermark(flow):
    tmp_path, fake, acc = flow
    doc_url = ticker.archive_url("320193", acc, "aapl10k.htm")
    rc = reports.run(make_args(days=1))
    assert rc == 0
    docs = tmp_path / "out" / "reports" / "docs"
    html = docs / "AAPL" / "10-K" / acc / "primary-document.html"
    assert html.exists() and html.stat().st_size > 1024
    meta = json.loads((html.parent / "meta.json").read_text())
    assert meta["label"] == "FY2026 Q3 · 2026-06-30"
    assert meta["accession"] == acc and not meta["unparsed"]
    rows = list(reports.storage.read_jsonl(
        tmp_path / "out" / "reports" / "manifest.jsonl"))
    assert len(rows) == 1 and rows[0]["accession"] == acc
    assert rows[0]["ticker"] == "AAPL" and rows[0]["path"].endswith(acc + "/" + "primary-document.html")
    done = (tmp_path / "out" / "reports" / "checkpoint" / "done.txt").read_text()
    assert f"AAPL|0000320193|10-K|{acc}" in done
    state = json.loads((tmp_path / "out" / "reports" / "state.json").read_text())
    assert state["last_scan_date"] == date.today().isoformat()
    # second run: local_has short-circuits, nothing downloaded again
    fake.calls.clear()
    rc = reports.run(make_args(days=1))
    assert rc == 0
    rows2 = list(reports.storage.read_jsonl(
        tmp_path / "out" / "reports" / "manifest.jsonl"))
    assert len(rows2) == 1                     # manifest deduped by accession
    assert doc_url not in fake.calls           # no re-download
    assert len(fake.calls) == 1                # only the index GET


def test_flow_incremental_dry_run_writes_nothing(flow, tmp_path):
    rc = reports.run(make_args(days=1, dry_run=True))
    assert rc == 0
    assert not (tmp_path / "out" / "reports" / "docs" / "AAPL").exists()
    # production quirk preserved: watermark still advanced (an index fetched)
    state = json.loads((tmp_path / "out" / "reports" / "state.json").read_text())
    assert state["last_scan_date"] == date.today().isoformat()


def test_flow_incremental_404_only_window_signals_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    monkeypatch.setattr(ticker, "_MAP", {"AAPL": "320193"})
    fake = FakeClient({})          # every URL 404s (incl. no cache offline-ish)
    monkeypatch.setattr(reports, "default_client", lambda: fake)
    rc = reports.run(make_args(days=1))
    assert rc == 1
    state_f = tmp_path / "out" / "reports" / "state.json"
    assert not state_f.exists() or "last_scan_date" not in \
        json.loads(state_f.read_text())


def test_flow_single_ticker_submissions_diff_and_amendment_nesting(
        tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    monkeypatch.setattr(ticker, "_MAP", {"AAPL": "320193"})
    docs = tmp_path / "out" / "reports" / "docs"
    old_dir = docs / "AAPL" / "10-K" / "OLDACC"
    old_dir.mkdir(parents=True)
    (old_dir / "primary-document.html").write_bytes(b"x" * 2000)
    sub = {"filings": {"recent": {
        "form": ["10-K", "8-K", "10-K/A"],
        "accessionNumber": ["OLDACC", "SKIP", "AMCACC"],
        "filingDate": ["2023-10-01", "2026-01-02", "2026-02-01"],
        "primaryDocument": ["old.htm", "x.htm", "amc.htm"],
    }}}
    amc_url = ticker.archive_url("320193", "AMCACC", "amc.htm")
    fake = FakeClient({subs_url("0000320193"): json.dumps(sub),
                       amc_url: BIG_HTML})
    monkeypatch.setattr(reports, "default_client", lambda: fake)
    rc = reports.run(make_args(ticker="AAPL", after="2024-01-01"))
    assert rc == 0
    assert (docs / "AAPL" / "10-K" / "A" / "AMCACC" /
            "primary-document.html").exists()      # production 10-K/A nesting
    assert (old_dir / "primary-document.html").stat().st_size == 2000
    rows = list(reports.storage.read_jsonl(
        tmp_path / "out" / "reports" / "manifest.jsonl"))
    assert [r["accession"] for r in rows] == ["AMCACC"]
    assert rows[0]["form"] == "10-K/A"
    assert ticker.archive_url("320193", "OLDACC", "old.htm") not in fake.calls
    done = (tmp_path / "out" / "reports" / "checkpoint" / "done.txt").read_text()
    assert f"AAPL|0000320193|10-K/A|AMCACC" in done


def test_flow_failed_checkpoint_requeues_without_index_hit(
        tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    monkeypatch.setattr(ticker, "_MAP", {"AAPL": "320193"})
    root = tmp_path / "out" / "reports"
    ck = root / "checkpoint"
    ck.mkdir(parents=True)
    # one yesterday-index with nothing new + a stale failed item
    y = date.today() - timedelta(days=1)
    ymd = y.strftime("%Y%m%d")
    idx_url = reports.INDEX_URL.format(year=y.strftime("%Y"),
                                       qtr=reports.qtr_of(ymd), ymd=ymd)
    header = "CIK|Company Name|Form Type|Date Filed|File Name\n"
    acc = "0001823887-26-000007"
    doc_url = ticker.archive_url("1823887", acc, "msft10q.htm")
    fake = FakeClient({idx_url: header,
                       subs_url("0001823887"):
                           sub_json(acc=acc, doc="msft10q.htm"),
                       doc_url: BIG_HTML})
    monkeypatch.setattr(reports, "default_client", lambda: fake)
    (ck / "failed.txt").write_text(f"MSFT|0001823887|10-Q|{acc}\n")
    rc = reports.run(make_args(days=1))
    assert rc == 0
    assert (root / "docs" / "MSFT" / "10-Q" / acc /
            "primary-document.html").exists()
    done = (ck / "done.txt").read_text()
    assert f"MSFT|0001823887|10-Q|{acc}" in done


# ---------------------------------------------------------------- backfill
def test_backfill_meta(tmp_path):
    docs = tmp_path / "docs"
    fresh = docs / "AAPL" / "10-K" / "ACCX"
    fresh.mkdir(parents=True)
    (fresh / "primary-document.html").write_bytes(BIG_HTML)
    nested = docs / "AAPL" / "10-K" / "A" / "ACCF"   # 10-K/A nesting
    nested.mkdir(parents=True)
    (nested / "primary-document.html").write_bytes(BIG_HTML)
    complete = docs / "MSFT" / "10-K" / "ACCC"
    complete.mkdir(parents=True)
    (complete / "primary-document.html").write_bytes(BIG_HTML)
    (complete / "meta.json").write_text(json.dumps(
        {"fiscal_year": 2026, "fiscal_quarter": "FY", "accession": "ACCC"}))
    msgs = []
    rc = reports.run_backfill_meta(docs, msgs.append)
    assert rc == 0
    m1 = json.loads((fresh / "meta.json").read_text())
    assert m1["label"].startswith("FY2026 Q3")
    m2 = json.loads((nested / "meta.json").read_text())
    assert m2["form"] == "10-K/A"           # nesting recovered from the walk
    m3 = json.loads((complete / "meta.json").read_text())
    assert m3 == {"fiscal_year": 2026, "fiscal_quarter": "FY",
                  "accession": "ACCC"}       # complete meta untouched
    assert any("Backfill done" in m for m in msgs)
