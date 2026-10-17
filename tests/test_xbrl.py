"""Offline tests for secdata.xbrl / secdata.xbrl_mapping (DESIGN rule 7).

All companyfacts payloads are small synthetic dicts built inline; the CLI
flow tests monkeypatch EdgarClient.get_bytes so nothing touches the network.
"""
import argparse
import json

import pytest

from secdata import client as client_mod
from secdata import ticker as ticker_mod
from secdata import xbrl
from secdata import xbrl_mapping as M


# --------------------------------------------------------------- factories

def _fact(start, end, val, form="10-K", filed="2024-02-20", fy=2023, fp="FY",
          accn="0000000000-24-000001", frame=None):
    f = {"end": end, "val": val, "form": form, "filed": filed,
         "fy": fy, "fp": fp, "accn": accn}
    if start is not None:
        f["start"] = start
    if frame is not None:
        f["frame"] = frame
    return f


def _tag(tag, facts_by_unit, label=None):
    return {"label": label or tag.replace("_", " "),
            "description": f"description of {tag}",
            "units": facts_by_unit}


def _facts_doc(gaap, ifrs=None, entityName="Test Co"):
    doc = {"cik": 12345, "entityName": entityName, "facts": {"us-gaap": gaap}}
    if ifrs is not None:
        doc["facts"]["ifrs-full"] = ifrs
    return doc


def fy2023_doc():
    """One fiscal year FY2023 (annual end 2023-12-31, quarters 03/06/09-30)
    with: duration facts, instants (real + note-date), weighted shares,
    split-restated EPS, cumulative-only cashflow, BS overlap cases."""
    A = "2023-01-01"; AE = "2023-12-31"
    q = [("2023-01-01", "2023-03-31", 100, "Q1", "2023-05-01"),   # start,end,val,fp,filed
         ("2023-04-01", "2023-06-30", 200, "Q2", "2023-08-01"),
         ("2023-07-01", "2023-09-30", 300, "Q3", "2023-11-01")]
    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": _tag(
            "Revenue", {
                "USD": [_fact(A, AE, 1000, filed="2024-02-20", accn="0000-24-1", fy=2023, fp="FY")]
                        + [_fact(s, e, r, form="10-Q", filed=fd, fy=2023, fp=fp,
                                 accn=f"0000-23-{i+2}")
                            for i, (s, e, r, fp, fd) in enumerate(q)]}),
        "NetIncomeLoss": _tag("Net Income", {
            "USD": [_fact(A, AE, 800, accn="0000-24-1"),
                    _fact("2023-01-01", "2023-03-31", 100, form="10-Q", filed="2023-05-01"),
                    _fact("2023-04-01", "2023-06-30", 200, form="10-Q", filed="2023-08-01"),
                    _fact("2023-07-01", "2023-09-30", 300, form="10-Q", filed="2023-11-01")]}),
        "OperatingIncomeLoss": _tag("Op Inc", {
            "USD": [_fact(A, AE, 600),
                    _fact("2023-01-01", "2023-03-31", 80, form="10-Q"),
                    _fact("2023-04-01", "2023-06-30", 150, form="10-Q"),
                    _fact("2023-07-01", "2023-09-30", 200, form="10-Q")]}),
        "GrossProfit": _tag("Gross", {
            "USD": [_fact(A, AE, 500),
                    _fact("2023-01-01", "2023-03-31", 50, form="10-Q"),
                    _fact("2023-04-01", "2023-06-30", 100, form="10-Q"),
                    _fact("2023-07-01", "2023-09-30", 150, form="10-Q")]}),
        "WeightedAverageNumberOfSharesOutstandingBasic": _tag("WAS", {
            "Shares": [_fact(A, AE, 1000),
                       _fact("2023-01-01", "2023-03-31", 1000, form="10-Q"),
                       _fact("2023-04-01", "2023-06-30", 1000, form="10-Q"),
                       _fact("2023-07-01", "2023-09-30", 1000, form="10-Q")]}),
        "Assets": _tag("Assets", {"USD": [
            _fact(None, AE, 5000), _fact(None, "2023-03-31", 4500, form="10-Q")]}),
        "StockSplitAdjustmentFactor": _tag("Split note", {"pure": [
            _fact(None, "2023-05-01", 2)]}),
        # split-restated EPS: 6.90 as-filed (in 366d window) vs 1.73 from a
        # later 10-K's comparative column (2026-09-02 basis fix)
        "EarningsPerShareDiluted": _tag("EPSd", {"USD/shares": [
            _fact("2021-01-01", "2021-12-31", 6.90, filed="2022-02-25", fy=2021,
                  fp="FY", accn="0000-22-1"),
            _fact("2021-01-01", "2021-12-31", 1.73, filed="2024-02-21", fy=2023,
                  fp="FY", accn="0000-24-1")]}),
        "NetCashProvidedByUsedInOperatingActivities": _tag("OCF", {"USD": [
            _fact(A, AE, 1000),
            _fact("2023-01-01", "2023-03-31", 100, form="10-Q"),
            _fact("2023-01-01", "2023-06-30", 400, form="10-Q"),   # cum Q2, 181d
            _fact("2023-01-01", "2023-09-30", 700, form="10-Q")]}),  # cum Q3, 273d
        "CashAndCashEquivalentsAtCarryingValue": _tag("Cash", {"USD": [
            _fact(A, AE, 1000),
            _fact("2023-10-02", AE, 1000, form="10-Q")]}),          # exact dup -> dedup
        "DeferredRevenueCurrent": _tag("DefRev", {"USD": [
            _fact(A, AE, 1000),
            _fact("2023-10-02", AE, 999, form="10-Q")]}),           # differs -> keep
    }
    return _facts_doc(gaap)


def simple_facts_doc(ni_end="2023-12-31", ni_val=500):
    """11-tag companyfacts so detect_standards passes; NI annual at ni_end."""
    gaap = {"NetIncomeLoss": _tag("Net Income", {
        "USD": [_fact("2023-01-01", ni_end, ni_val)]})}
    for i in range(10):
        gaap[f"RevenueTag{i}"] = _tag(f"R{i}", {"USD": [
            _fact("2023-01-01", "2023-12-31", 10 + i)]})
    return _facts_doc(gaap)


# --------------------------------------------------------- pure mapping bits


def test_duration_days():
    assert M.duration_days("2023-01-01", "2023-12-31") == 365
    assert M.duration_days("2023-01-01", "2023-03-31") == 90
    assert M.duration_days(None, "2023-12-31") is None
    assert M.duration_days("garbage", "2023-12-31") is None


def test_normalize_fact_keeps_metadata_including_frame():
    v = _fact("2023-01-01", "2023-12-31", 42, frame="CIK0000012345FY2023")
    n = M.normalize_fact(v, "USD", 365)
    assert n["frame"] == "CIK0000012345FY2023"
    assert n["duration_days"] == 365
    assert n["val"] == 42 and n["unit"] == "USD" and n["form"] == "10-K"


def test_classify_statement():
    assert M.classify_statement("NetCashProvidedByUsedInOperatingActivities",
                                "Cash, flow") == "cashflow"
    # cashflow priority beats income keywords on D&A (prod bug fix comment)
    assert M.classify_statement("Depreciation", "Depreciation") == "cashflow"
    # balance matched on TAG not label: label with "income" must not steal it
    assert M.classify_statement("IncomeTaxAssetsNet", "Income tax assets") == "balance"
    assert M.classify_statement("Revenues", "Revenues") == "income"
    assert M.classify_statement("ZzzUnmappedThing", "Whatever") == "income"  # default


def test_detect_unit():
    assert M.detect_unit({"USD": []}) == "currency"
    assert M.detect_unit({"Shares": []}) == "shares"
    assert M.detect_unit({"pure": []}) == "ratio"
    assert M.detect_unit({"USD/shares": []}) == "usd/shares"
    assert M.detect_unit({}) == "unknown"


def test_infer_period_ends_filters_note_dates():
    ns = {"A": _tag("A", {"USD": [_fact("2023-01-01", "2023-12-31", 1)]}),
          "B": _tag("B", {"USD": [_fact("2023-01-01", "2023-12-31", 2)]}),
          "C": _tag("C", {"USD": [_fact("2023-01-01", "2023-12-31", 3)]}),
          "N": _tag("N", {"USD": [_fact("2023-01-01", "2023-11-15", 4)]})}
    ann, qtr = M.infer_period_ends(ns)
    assert ann == {"2023-12-31"}   # >=3 supporting tags; note date dropped
    assert qtr == set()


def test_get_facts_by_duration_instant_gating_and_dedup():
    dupe = _fact(None, "2023-12-31", 999)
    ns = {"X": _tag("X", {"USD": [
        dupe, dupe,                                   # exact dup -> one raw entry
        _fact(None, "2023-11-15", 123),               # not a valid end -> dropped
    ]})}
    r = M.get_facts_by_duration(ns, "X", {"2023-12-31"}, set())
    assert r["annual"] == {"2023-12-31": 999}
    assert len(r["raw"]["annual"]["2023-12-31"]) == 1


def test_fact_rank_share_unit_basis_and_weighted_unit():
    asfiled = _fact("2021-01-01", "2021-12-31", 6.9, filed="2022-02-25")
    restated = _fact("2021-01-01", "2021-12-31", 1.73, filed="2024-02-21")
    assert M.fact_rank(asfiled, "annual", True) > M.fact_rank(restated, "annual", True)
    # non-share facts rank purely on primary form then filed date
    assert M.fact_rank(asfiled, "annual", False) < M.fact_rank(restated, "annual", False)
    assert M.is_weighted_share_unit("Shares") and not M.is_weighted_share_unit("USD/shares")


def test_extract_fy2023_duration_and_derivation():
    ext = M.extract_metrics(fy2023_doc(), "TEST")
    ann, qtr = ext["annual"], ext["quarterly"]

    ni = "us-gaap:NetIncomeLoss"
    assert ann[ni]["2023-12-31"] == 800
    assert qtr[ni] == {"2023-03-31": 100, "2023-06-30": 200, "2023-09-30": 300,
                       "2023-12-31": 200}          # derived Q4 = 800-100-200-300
    derived = [f for f in ext["raw_facts"]["quarterly"][ni]["2023-12-31"]
               if isinstance(f, dict) and "derived" in f]
    assert derived[0]["derived"] == "annual_minus_q1_q2_q3"

    # cumulative -> standalone
    ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
    assert qtr[ocf] == {"2023-03-31": 100, "2023-06-30": 300, "2023-09-30": 300,
                        "2023-12-31": 300}
    raw_ocf = ext["raw_facts"]["quarterly"][ocf]
    assert any(f.get("derived") == "cum_q2_minus_q1" for f in raw_ocf["2023-06-30"])
    assert any(f.get("derived") == "cum_q3_minus_cum_q2_or_q1_q2"
               for f in raw_ocf["2023-09-30"])

    # weighted-average share counts: never derive Q4
    was = "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"
    assert "2023-12-31" not in qtr[was] and "2023-12-31" in ann[was]

    # split restatement: as-filed basis wins
    eps = "us-gaap:EarningsPerShareDiluted"
    assert ann[eps]["2021-12-31"] == 6.90

    # note-date instant excluded entirely (even from meta)
    assert "us-gaap:StockSplitAdjustmentFactor" not in ann
    assert "us-gaap:StockSplitAdjustmentFactor" not in ext["metrics_meta"]
    assert ann["us-gaap:Assets"]["2023-12-31"] == 5000
    assert qtr["us-gaap:Assets"]["2023-03-31"] == 4500

    # exact annual==quarterly balance values dedup; differing values keep both
    assert "us-gaap:CashAndCashEquivalentsAtCarryingValue" not in qtr
    assert qtr["us-gaap:DeferredRevenueCurrent"]["2023-12-31"] == 999
    assert ann["us-gaap:DeferredRevenueCurrent"]["2023-12-31"] == 1000


def test_extract_sign_gate_blocks_mixed_basis_q4():
    gaap = {"NonoperatingIncomeExpense": _tag("NOI", {"USD": [
        _fact("2023-01-01", "2023-12-31", 1000),
        _fact("2023-01-01", "2023-03-31", 100, form="10-Q"),
        _fact("2023-04-01", "2023-06-30", 200, form="10-Q"),
        _fact("2023-07-01", "2023-09-30", 800, form="10-Q")]}),  # Q4 = -100: mixed basis
        # two filler tags to keep infer happy only for annual end
        "NetIncomeLoss": _tag("NI", {"USD": [
            _fact("2023-01-01", "2023-12-31", 1),
            _fact("2023-02-01", "2023-12-31", 1),
            _fact("2023-03-01", "2023-12-31", 1)]})}
    for i in range(9):  # pad namespaces past the 10-tag standard floor
        gaap[f"RevenueFiller{i}"] = _tag(f"RF{i}", {"USD": []})
    ext = M.extract_metrics(_facts_doc(gaap), "GATE")
    qtr = ext["quarterly"]["us-gaap:NonoperatingIncomeExpense"]
    assert "2023-12-31" not in qtr and "2023-09-30" in qtr


def test_extract_magnitude_gate_blocks_per_share_jump():
    gaap = {"EarningsPerShareBasic": _tag("EPS", {"USD/shares": [
        _fact("2023-01-01", "2023-12-31", 8.0),
        _fact("2023-01-01", "2023-03-31", 1.0, form="10-Q"),
        _fact("2023-04-01", "2023-06-30", 1.0, form="10-Q"),
        _fact("2023-07-01", "2023-09-30", 1.0, form="10-Q")]}),
        "NetIncomeLoss": _tag("NI", {"USD": [
            _fact("2023-01-01", "2023-12-31", 1),
            _fact("2023-02-01", "2023-12-31", 1),
            _fact("2023-03-01", "2023-12-31", 1)]})}
    for i in range(9):  # pad past the 10-tag standard floor
        gaap[f"RevenueFiller{i}"] = _tag(f"RF{i}", {"USD": []})
    ext = M.extract_metrics(_facts_doc(gaap), "MAG")
    assert "2023-12-31" not in ext["quarterly"]["us-gaap:EarningsPerShareBasic"]


def test_extract_ifrs_standard_and_empty_std_early_return():
    us = fy2023_doc()["facts"]["us-gaap"]
    ifrs = {}
    for i in range(11):
        ifrs[f"Assets{i}"] = _tag(f"A{i}", {"EUR": [
            _fact("2023-01-01", "2023-12-31", 700 + i, form="20-F")]})
    ext = M.extract_metrics(_facts_doc(us, ifrs=ifrs), "IFRS")
    assert ext["metrics_meta"]["ifrs-full:Assets0"]["standard"] == "ifrs-full"
    assert ext["annual"]["ifrs-full:Assets10"]["2023-12-31"] == 710
    # thin namespaces (<10 tags) are ignored: no us-gaap at all
    ext2 = M.extract_metrics({"facts": {"us-gaap": {"NetIncomeLoss": _tag(
        "NI", {"USD": [_fact("2023-01-01", "2023-12-31", 1)]})}}}, "T")
    assert ext2 == {"ticker": "T", "annual": {}, "quarterly": {}, "metrics_meta": {}}


# ---------------------------------------------------------- importance rules


def test_classify_importance():
    assert M.classify("us-gaap:NetIncomeLoss", "income") == (1, 8)
    assert M.classify("us-gaap:Assets", "balance") == (1, 0)
    assert M.classify("us-gaap:EarningsPerShareDiluted", "income") == (1, 11)
    assert M.classify("us-gaap:CurrentLongTermDebtCurrent", "balance") == (1, 12)
    assert M.classify("us-gaap:ShareBasedCompensation", "cashflow") == (1, 5)
    # ends matcher is namespace-agnostic (ifrs-full too)
    assert M.classify("ifrs-full:RevenueFromContractWithCustomerExcludingAssessedTax",
                      "income") == (1, 0)
    # statement_type gates the ruleset
    assert M.classify("us-gaap:Assets", "income") == (0, 99)
    assert M.classify("us-gaap:NotInAnyRule", "cashflow") == (0, 99)


# ------------------------------------------------------------- row builder


def test_metrics_rows_shape_and_truncation():
    ext = M.extract_metrics(fy2023_doc(), "TEST")
    rows = M.metrics_rows("TEST", ext)
    ni = [r for r in rows if r["metric_key"] == "us-gaap:NetIncomeLoss"
          and r["period_type"] == "annual"][0]
    assert ni["is_important"] == 1 and ni["display_order"] == 8
    assert ni["statement_type"] == "income" and ni["unit"] == "currency"
    assert ni["latest_value"] == 800.0
    assert ni["source"] == "xbrl_v6"
    assert ni["values"]["2023-12-31"] == 800
    assert ni["raw_facts"]["2023-12-31"][0]["form"] == "10-K"
    q4 = [r for r in rows if r["metric_key"] == "us-gaap:NetIncomeLoss"
          and r["period_type"] == "quarterly"][0]
    assert q4["latest_value"] == 200.0
    assert any(isinstance(f, dict) and "derived" in f
               for f in q4["raw_facts"]["2023-12-31"])

    long_key = "us-gaap:" + "X" * 200
    rows2 = M.metrics_rows("S", {"annual": {long_key: {"2023-01-01": 1}},
                                 "quarterly": {}, "metrics_meta": {},
                                 "raw_facts": {}})
    assert len(rows2[0]["metric_key"]) == 128


# ------------------------------------------------------------------ xbrl.py


def test_parse_exchange_map():
    m = xbrl.parse_exchange_map({"data": [[320193, "A", "aaa", "N"],
                                          [1, "no ticker"], [105054, "B", "msft", "X"]]})
    assert m == {"AAA": "0000320193", "MSFT": "0000105054"}


def test_merge_overrides_adds_and_wins():
    base = {"AAPL": "0000320193", "EA": "0000715153"}
    ovr = {"BK": 13968, "EA": 1}
    merged, added = xbrl.merge_overrides(base, ovr)
    assert added == ["BK"]
    assert merged["BK"] == "0000013968"
    assert merged["EA"] == "0000000001"  # overrides win (production update())


def test_sec_has_newer():
    sub = {"filings": {"recent": {
        "form": ["8-K", "10-Q", "S-1", "10-K/A"],
        "filingDate": ["2023-01-01", "2023-08-01", "2023-09-01", "2024-02-20"]}}}
    assert xbrl.sec_has_newer(sub, None) is True           # no local data
    assert xbrl.sec_has_newer(sub, "2023-12-31") is True   # 2024-02-20 newer
    assert xbrl.sec_has_newer(sub, "2024-12-31") is False
    assert xbrl.sec_has_newer(sub, "2023-06-30") is True   # 10-Q/A forms too
    no10k = {"filings": {"recent": {"form": ["8-K"], "filingDate": ["2025-01-01"]}}}
    assert xbrl.sec_has_newer(no10k, "2024-12-31") is False  # nothing to pull
    assert xbrl.sec_has_newer(no10k, None) is True


def test_local_latest_end():
    ext = {"annual": {"us-gaap:NetIncomeLoss": {"2022-12-31": 1, "2023-12-31": 2},
                      "us-gaap:Other": {"2024-06-30": 9}}}
    assert xbrl.local_latest_end(ext) == "2023-12-31"
    assert xbrl.local_latest_end({"annual": {}}) is None


def test_parse_failed_lines():
    assert xbrl.parse_failed_lines("AAPL\thttp_404\nMSFT\tcrash\n# comment\n\nGOOGL")\
        == ["AAPL", "MSFT", "GOOGL"]


def test_rewrite_metrics_rows_replaces_only_updated(tmp_path):
    p = tmp_path / "metrics.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"symbol": "KEEP", "metric_key": "a"}, {"symbol": "OLD", "metric_key": "b"},
    ]) + "\n")
    kept, new = xbrl.rewrite_metrics_rows(p, {"OLD": [{"symbol": "OLD", "metric_key": "c"}]})
    assert kept == 1 and new == 1
    syms = [json.loads(l) for l in p.read_text().splitlines()]
    assert {"symbol": "OLD", "metric_key": "c"} in syms
    assert not any(l.get("metric_key") == "b" for l in syms)


# -------------------------------------------------------------- CLI wiring


def _args(argv):
    ap = argparse.ArgumentParser()
    xbrl.add_arguments(ap)
    return ap.parse_args(argv)


def test_add_arguments_flags():
    a = _args(["--daily", "--workers", "8", "--limit", "5"])
    assert a.daily and a.workers == 8 and a.limit == 5
    a = _args(["AAPL", "MSFT"])
    assert a.symbols == ["AAPL", "MSFT"]


# ----------------------------------------------------------- flow (offline)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_UA", "Test Tester t@example.com")
    monkeypatch.setenv("SEC_EDGAR_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("SEC_EDGAR_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("SEC_EDGAR_OVERRIDES", raising=False)
    monkeypatch.delenv("SEC_EDGAR_OFFLINE", raising=False)
    monkeypatch.setattr(client_mod, "_default", None)
    monkeypatch.setattr(ticker_mod, "_MAP", None)
    return tmp_path


def _fake_http(monkeypatch, exchange, submissions, facts, company_tickers=None):
    calls = []

    def fake_get_bytes(self, url, *a, **kw):
        calls.append(url)
        if "company_tickers_exchange" in url:
            return json.dumps(exchange).encode()
        if "company_tickers.json" in url:
            return json.dumps(company_tickers or {}).encode()
        if "/submissions/" in url:
            for cik, sub in submissions.items():
                if url.endswith(f"{cik}.json"):
                    return json.dumps(sub).encode()
        if "/companyfacts/" in url:
            for cik, doc in facts.items():
                if url.endswith(f"{cik}.json"):
                    return json.dumps(doc).encode()
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(client_mod.EdgarClient, "get_bytes", fake_get_bytes)
    return calls


def test_daily_flow(tmp_path, monkeypatch, env):
    exchange = {"data": [[320193, "TEST A", "AAA", "Nasdaq"],
                         [105054, "TEST B", "BBB", "NYSE"]]}
    sub = lambda latest_form, latest_date: {"filings": {"recent": {  # noqa: E731
        "form": [latest_form, "8-K"], "filingDate": [latest_date, "2020-01-01"]}}}
    submissions = {"CIK0000320193": sub("10-K", "2024-02-20"),
                   "CIK0000105054": sub("8-K", "2025-06-01")}  # no 10-K/10-Q/20-F/40-F
    facts = {"CIK0000320193": simple_facts_doc(), "CIK0000105054": simple_facts_doc()}

    calls = _fake_http(monkeypatch, exchange, submissions, facts)
    assert xbrl.run(_args(["--daily"])) == 0

    out = tmp_path / "out" / "xbrl"
    raw = (out / "companyfacts" / "CIK0000320193.json").read_text()
    assert "Test Co" in raw  # raw bytes preserved as fetched
    rows = [json.loads(l) for l in (out / "metrics.jsonl").read_text().splitlines()]
    assert {r["symbol"] for r in rows} == {"AAA", "BBB"}
    assert all(r["source"] == "xbrl_v6" for r in rows)
    assert any(r["metric_key"] == "us-gaap:NetIncomeLoss" and r["is_important"] == 1
               for r in rows)
    state = json.loads((out / "checkpoint" / "state.json").read_text())
    assert state["watermark"] == {"AAA": "2023-12-31", "BBB": "2023-12-31"}
    assert "AAA" in (out / "checkpoint" / "done.txt").read_text()

    # second run: AAA's 10-K (2024-02-20) is newer than its annual end -> the
    # production check is deliberately conservative and refreshes it again;
    # BBB has no 10-K/10-Q/20-F/40-F at all -> must NOT be re-fetched, and
    # metrics.jsonl must stay duplicate-free after the rewrite.
    calls.clear()
    assert xbrl.run(_args(["--daily"])) == 0
    assert any("CIK0000320193" in u for u in calls)
    assert not any("companyfacts/CIK0000105054" in u for u in calls)
    rows2 = [json.loads(l) for l in (out / "metrics.jsonl").read_text().splitlines()]
    assert len(rows2) == len(rows)
    assert [r for r in rows2 if r["symbol"] == "BBB"] == [r for r in rows if r["symbol"] == "BBB"]


def test_explicit_ticker_flow_and_check_mode(tmp_path, monkeypatch, env):
    facts = {"CIK0000320193": simple_facts_doc()}
    monkeypatch.setattr(client_mod.EdgarClient, "get_bytes",
                        lambda self, url, *a, **kw: json.dumps(
                            {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "A"}}
                            if "company_tickers.json" in url else facts["CIK0000320193"]
                        ).encode())
    assert xbrl.run(_args(["AAPL", "--check"])) == 0
    out = tmp_path / "out" / "xbrl"
    assert not (out / "metrics.jsonl").exists()          # --check wrote nothing
    assert not (out / "companyfacts").exists()
    assert xbrl.run(_args(["AAPL"])) == 0
    rows = [json.loads(l) for l in (out / "metrics.jsonl").read_text().splitlines()]
    assert {r["symbol"] for r in rows} == {"AAPL"}


def test_unknown_ticker_exits_1(tmp_path, monkeypatch, env):
    def fake(self, url, *a, **kw):
        if "company_tickers.json" in url:
            return json.dumps({"0": {"cik_str": 1, "ticker": "AAA", "title": "A"}}).encode()
        raise AssertionError(url)
    monkeypatch.setattr(client_mod.EdgarClient, "get_bytes", fake)
    assert xbrl.run(_args(["NOPE"])) == 1


def test_no_args_no_universe_exits_2(tmp_path, monkeypatch, env):
    assert xbrl.run(_args([])) == 2
