"""Offline tests for secdata.enforcement — canned litigation-release listing
HTML + fake client.  No network: fetches go through a FakeClient installed by
monkeypatching ``enforcement.default_client``.
"""
import argparse
import hashlib
import json
import os
import urllib.error
from collections import OrderedDict
from pathlib import Path

import pytest

from secdata import enforcement as enf

LISTING0 = enf.LISTING_URL.format(0)


def row_html(lr, name, when="2026-10-13T21:21:15Z", with_see_also=True):
    """Live-shape listing row: single-quoted respondent href, a
    ``Release No.`` *span* (no anchor) and a ``lr-N.pdf`` file anchor — the
    two things the canonical flat regexes double-counted."""
    see = ""
    if with_see_also:
        see = ("<a href='/enforcement-litigation/litigation-releases/"
               "lr-{}.pdf'>File</a>".format(lr))
    return ('<tr class="pr-list-page-row">'
            '<td><time datetime="{}" class="datetime">Oct. 13, 2026</time>'
            '</td><td><div class="release-view__respondents">'
            "<a href='/enforcement-litigation/litigation-releases/"
            "lr-{}'>{}</a></div>"
            '<span class="view-table_subfield_label">Release No.</span>'
            "<span class=\"view-table_subfield_value\">LR-{}</span>{}"
            "</td></tr>").format(when, lr, name, lr, see)


def listing_page(rows, max_page=2):
    links = "".join(
        '<a href="/enforcement-litigation/litigation-releases?page={}">'
        "</a>".format(p) for p in range(max_page + 1))
    return "<html><table>" + "".join(rows) + "</table>" + links + "</html>"


def detail_html(inner):
    return ("<html><main>NAVBAR</main><article><p>{}</p>"
            "<script>tracking()</script></article></html>").format(inner)


class FakeClient:
    def __init__(self, payloads=None):
        self.payloads = dict(payloads or {})
        self.calls = []

    def get_bytes(self, url, max_age_hours=None, min_cache_bytes=0,
                  timeout=None):
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
    monkeypatch.setattr(enf.time, "sleep", lambda s: None)  # skip pacing


def parse_args(*argv):
    p = argparse.ArgumentParser()
    enf.add_arguments(p)
    return p.parse_args(list(argv))


def install(monkeypatch, fake):
    monkeypatch.setattr(enf, "default_client", lambda: fake)
    return fake


def out_root():
    return Path(os.environ["SEC_EDGAR_OUT"], "enforcement")


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def sweep_payloads(pages=None):
    """3 listing pages, two rows each: LR 30..25."""
    pages = pages if pages is not None else {
        0: [row_html(30, "Alpha Corp"), row_html(29, "Beta LLC")],
        1: [row_html(28, "Gamma Inc"), row_html(27, "Delta Fund")],
        2: [row_html(26, "Echo SA"), row_html(25, "Foxtrot PLC")],
    }
    return {enf.LISTING_URL.format(p): listing_page(rows, max_page=2)
            for p, rows in pages.items()}


# ------------------------------------------------------------------ pure parses
def test_parse_list_rows_live_shape():
    html = listing_page([row_html(26673, "Matthew Brown and "
                                         "Matthew Brown Companies, LLC")],
                        max_page=0)
    entries = enf.parse_list_rows(html)
    assert len(entries) == 1          # span + pdf anchor did NOT add entries
    e = entries[0]
    assert e["source"] == "sec_litigation"
    assert e["source_id"] == "LR-26673"
    assert e["title"] == "Matthew Brown and Matthew Brown Companies, LLC"
    assert e["respondents"] == e["title"]
    assert e["action_date"] == "2026-10-13T21:21:15Z"   # full attr, verbatim
    assert e["action_type"] == "litigation_release"
    assert e["url"] == ("https://www.sec.gov/enforcement-litigation/"
                        "litigation-releases/lr-26673")


def test_parse_list_page_prefers_rows_then_classic():
    modern = listing_page([row_html(11, "One Co")], max_page=0)
    assert [e["source_id"] for e in enf.parse_list_page(modern)] == ["LR-11"]
    legacy = ("<html><a href='/enforcement-litigation/litigation-releases/"
              'lr-12\'>Old Layout Inc</a><time datetime="2024-01-01">x'
              "</time></html>")
    entries = enf.parse_list_page(legacy)      # no <tr>: classic zip fallback
    assert entries[0]["source_id"] == "LR-12"
    assert entries[0]["title"] == "Old Layout Inc"
    assert entries[0]["action_date"] == "2024-01-01"


def test_classic_parse_double_counts_live_markup():
    # documents WHY the row parse is primary: the canonical positional zip
    # inflates on today's pages (respondent anchor + Release-No + pdf)
    html = listing_page([row_html(30, "Alpha Corp")], max_page=0)
    assert len(enf.parse_list_page_classic(html)) > 1


def test_parse_total_pages():
    assert enf.parse_total_pages(listing_page([], max_page=119)) == 119
    assert enf.parse_total_pages("<html>no links</html>") == 118


def test_extract_detail_chain_and_strip():
    assert enf.extract_detail(detail_html("SEC Charges ACME")) == \
        "SEC Charges ACME"
    main_only = "<html><main>Body <script>bad()</script> text</main></html>"
    assert enf.extract_detail(main_only) == "Body text"
    assert enf.extract_detail("<html><style>a{}</style>Plain</html>") == \
        "Plain"
    capped = enf.extract_detail(detail_html("A" * 10500))
    assert len(capped) == 10000


def test_make_row_matches_production_columns():
    entry = enf._entry("7", "ACME", "2026-01-02T00:00:00Z")
    row = enf.make_row(entry, created_at="2026-01-02 00:00:00")
    expected = hashlib.sha256("LR-7".encode()).hexdigest()[:16]
    assert row["content_hash"] == expected      # description empty at insert
    assert row["has_detail"] == 0
    assert row["companies"] == ""
    assert row["created_at"] == "2026-01-02 00:00:00"
    assert list(row) == ["source", "source_id", "title", "respondents",
                         "companies", "action_type", "action_date",
                         "description", "url", "content_hash", "has_detail",
                         "created_at"]


def test_pending_details_order_limit_failed():
    rows = OrderedDict()
    for sid, hd in [("LR-1", 0), ("LR-10", 0), ("LR-2", 1), ("LR-3", 0)]:
        rows[sid] = {"source_id": sid, "has_detail": hd}
    assert enf.pending_details(rows, 10) == ["LR-10", "LR-3", "LR-1"]
    assert enf.pending_details(rows, 2) == ["LR-10", "LR-3"]
    assert enf.pending_details(rows, 10, failed={"LR-3"}) == ["LR-10", "LR-1"]
    assert enf.pending_details(rows, 10, failed={"LR-3"},
                               skip_failed=False) == ["LR-10", "LR-3", "LR-1"]


# --------------------------------------------------------------------- flow
def test_phase1_sweep_resume_and_wrap(monkeypatch):
    payloads = sweep_payloads()
    install(monkeypatch, FakeClient(payloads))
    rc = enf.run(parse_args())
    assert rc == 0
    rows = read_jsonl(out_root() / "releases.jsonl")
    assert len(rows) == 6
    assert {r["source_id"] for r in rows} == \
        {"LR-25", "LR-26", "LR-27", "LR-28", "LR-29", "LR-30"}
    st = json.loads((out_root() / "checkpoint" / "state.json").read_text())
    assert st["last_page"] == 3 and st["total_scraped"] == 6
    assert st["last_lr_num"] == 25        # last row of final page

    # next cron tick: cursor past the end wraps to page 0; dedupe means a
    # full re-scan adds nothing
    fake2 = install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args())
    assert len(read_jsonl(out_root() / "releases.jsonl")) == 6
    assert fake2.calls.count(LISTING0) == 1

    # a brand-new release on page 0 is picked up by the wrap-around sweep
    pages2 = {0: [row_html(31, "New One Inc"), row_html(30, "Alpha Corp")],
              1: [row_html(28, "Gamma Inc"), row_html(27, "Delta Fund")],
              2: [row_html(26, "Echo SA"), row_html(25, "Foxtrot PLC")]}
    install(monkeypatch, FakeClient(sweep_payloads(pages2)))
    enf.run(parse_args())
    rows = read_jsonl(out_root() / "releases.jsonl")
    assert len(rows) == 7 and rows[-1]["source_id"] == "LR-31"


def test_details_flow_fills_in_place(monkeypatch):
    payloads = sweep_payloads()
    payloads.update({enf.DETAIL_URL.format(n): detail_html("Set LR-{} text"
                                                           .format(n))
                     for n in range(25, 31)})
    install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details"))
    rows = {r["source_id"]: r for r in
            read_jsonl(out_root() / "releases.jsonl")}
    filled = [r for r in rows.values() if r["has_detail"] == 1]
    assert len(filled) == 6
    r30 = rows["LR-30"]
    assert r30["description"] == "Set LR-30 text"
    # production detail fill never recomputed the insert-time content hash
    assert r30["content_hash"] == hashlib.sha256(
        "LR-30".encode()).hexdigest()[:16]
    doc = out_root() / "docs" / "LR-30.txt"
    assert doc.read_text() == "Set LR-30 text"
    done = (out_root() / "checkpoint" / "done.txt").read_text()
    assert "LR-30" in done and "LR-25" in done
    st = json.loads((out_root() / "checkpoint" / "state.json").read_text())
    assert st["detail_fetched"] == 6

    # already-filled rows are not re-fetched
    fake2 = install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details-only"))
    assert not any("lr-" in c for c in fake2.calls)


def test_max_details_newest_first(monkeypatch):
    payloads = sweep_payloads()
    payloads.update({enf.DETAIL_URL.format(n): detail_html("T{}".format(n))
                     for n in (30, 29, 28)})
    install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details", "--max-details", "2"))
    rows = {r["source_id"]: r for r in
            read_jsonl(out_root() / "releases.jsonl")}
    assert [rows[s]["has_detail"] for s in ("LR-30", "LR-29", "LR-28")] == \
        [1, 1, 0]


def test_detail_failure_skipped_unless_retry(monkeypatch):
    payloads = sweep_payloads()                 # NO detail payloads: all 404
    fake = install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details-only"))       # need stored rows first? none
    assert fake.calls == []                     # nothing stored -> nothing due
    install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details"))            # sweep + failed details
    failed = (out_root() / "checkpoint" / "failed.txt").read_text()
    assert "LR-30" in failed
    rows = read_jsonl(out_root() / "releases.jsonl")
    assert all(r["has_detail"] == 0 for r in rows)

    fake2 = install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details-only"))
    assert not any("lr-" in c for c in fake2.calls)   # failed ones skipped
    fake3 = install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details-only", "--retry-failed"))
    assert any(c.endswith("/lr-30") for c in fake3.calls)   # retried


def test_resume_fills_from_doc_on_disk(monkeypatch):
    # crash between save_doc and the jsonl rewrite: done.txt + docs exist,
    # rows still say has_detail=0 -> resume must fill without any network
    install(monkeypatch, FakeClient(sweep_payloads()))
    enf.run(parse_args())
    rel = out_root() / "releases.jsonl"
    rows = read_jsonl(rel)
    for r in rows:                      # everything else already done
        if r["source_id"] != "LR-30":
            r["has_detail"] = 1
    rel.write_text("".join(json.dumps(r) + "\n" for r in rows))
    docs = out_root() / "docs"
    enf.storage.save_doc(docs, "LR-30.txt", b"from disk")
    (out_root() / "checkpoint" / "done.txt").write_text("LR-30\n")
    fake = install(monkeypatch, FakeClient(sweep_payloads()))
    enf.run(parse_args("--details-only"))
    rows = {r["source_id"]: r for r in
            read_jsonl(out_root() / "releases.jsonl")}
    assert rows["LR-30"]["has_detail"] == 1
    assert rows["LR-30"]["description"] == "from disk"
    assert not any("lr-" in c for c in fake.calls)


def test_details_only_without_rows(monkeypatch):
    install(monkeypatch, FakeClient(sweep_payloads()))
    assert enf.run(parse_args("--details-only")) == 0
    assert not (out_root() / "releases.jsonl").exists()


def test_dry_run_writes_nothing(monkeypatch):
    payloads = sweep_payloads()
    payloads[enf.DETAIL_URL.format(30)] = detail_html("X")
    install(monkeypatch, FakeClient(payloads))
    enf.run(parse_args("--details", "--dry-run"))
    assert not (out_root() / "releases.jsonl").exists()
    assert not (out_root() / "checkpoint" / "state.json").exists()
    assert not (out_root() / "checkpoint" / "done.txt").exists()
    assert not (out_root() / "docs" / "LR-30.txt").exists()
