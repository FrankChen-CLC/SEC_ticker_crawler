"""secdata proxy — DEF 14A proxy statements and executive intelligence.

Ported from the production executive-intelligence package (2026). One symbol build
runs the full engine:

  proxy14a   latest DEF 14A -> Pay-vs-Performance inline-XBRL table, heuristic
             Summary Compensation Table (incl. the XOM image-layer quirk),
             related-party section, CD&A metric-weight tables
  promises   guidance sentences from 8-K Item 2.02 / EX-99.1 releases, scored
             against XBRL actuals (met | missed | pending | unparsed | tracked)
  events     8-K item stream (5.02/2.01/4.01/4.02/...), EX-97 clawback check,
             material-weakness/restatement scan, Rule 10b5-1 disclosure
  capital    capital-allocation + dilution series from companyconcept XBRL
  insiders   Forms 3/4/5 XML: transactions, 10b5-1 flags, latest positions

Upstream EDGAR endpoints (via the shared throttled/cached client):
  https://data.sec.gov/submissions/CIK##########.json (+ older files pages)
  https://www.sec.gov/Archives/edgar/data/<cik>/<accn>/...      (docs, index.json)
  https://data.sec.gov/api/xbrl/companyconcept/...
  https://www.sec.gov/files/company_tickers.json

Outputs (replacing the production MySQL ingest; one file per entity family,
a rebuild of a symbol upserts — replaces — that symbol's rows):

  out/proxy/executives.jsonl  out/proxy/comp.jsonl       out/proxy/promises.jsonl
  out/proxy/events.jsonl      out/proxy/capital.jsonl    out/proxy/insiders.jsonl
  out/proxy/builds.jsonl      out/proxy/state|checkpoint/done.txt,failed.txt,
                              state.json  out/proxy/incremental_queue.txt

Modes:
  secdata proxy AAPL MSFT              build these symbols
  secdata proxy --bulk                 resumable full-universe sweep (bulk.py)
  secdata proxy --incremental          weekly freshness queue (incremental.py)

Progress goes to stderr; the optional ``--md`` digest prints to stdout.
"""
import argparse
import json
import os
import sys
import time

from .. import storage
from ..client import default_client
from . import bulk, incremental, report

COMMAND = "proxy"
HELP = "DEF 14A proxy statements + executive intelligence -> out/proxy/*.jsonl"

FAMILIES = ("executives", "comp", "promises", "events", "capital",
            "insiders", "builds")
# flush the JSONL merge every N symbols during bulk (crash-resumable, cheap)
FLUSH_EVERY = int(os.environ.get("PROXY_FLUSH_EVERY", "25"))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class Ctx:
    """Shared run context: output root, checkpoint state, buffered rows."""

    def __init__(self, args):
        self.args = args
        self.client = default_client()
        self.root = storage.out_root("proxy")
        self.cp = storage.Checkpoint(self.root / "checkpoint")
        self.state = storage.State(self.root / "checkpoint" / "state.json",
                                   default={"symbols": {}})
        self.overrides_path = getattr(args, "overrides", None)
        self._batch = {}
        self._batch_syms = set()
        self._n = 0

    # ------------------------------------------------------------ state
    def records(self):
        return self.state.data.setdefault("symbols", {})

    def mark_checkpoint(self, sym, status):
        if status == "fail":
            self.cp.mark_failed(sym)
        else:
            self.cp.mark_done(sym)

    # ------------------------------------------------------------ build
    def build_symbol(self, sym):
        """One symbol: report.build (raises on identity/HTTP failure) ->
        buffer entity rows -> update the watermark record."""
        payload = report.build(
            sym,
            insider_since=self.args.insider_since,
            event_since=self.args.event_since,
            promise_since=self.args.promise_since,
            overrides_path=self.overrides_path)
        self._buffer(report.emit(payload))
        rec = self.records().setdefault(payload["symbol"], {})
        rec.update({"cik": payload["cik"], "built_at": payload["built_at"],
                    "status": "ok", "ts": time.time()})
        self.state.save()
        if getattr(self.args, "md", False):
            print(report.md(payload))
        return payload

    # ------------------------------------------------------------ merge
    def _buffer(self, fam):
        for name, rows in fam.items():
            self._batch.setdefault(name, []).extend(rows)
            for r in rows:
                self._batch_syms.add(r["symbol"])
        self._n += 1

    def maybe_flush(self):
        if self._n >= FLUSH_EVERY:
            self.flush()

    def flush(self):
        if not self._batch:
            return
        for name in FAMILIES:
            rows = self._batch.get(name)
            if rows:
                _merge(self.root / (name + ".jsonl"), self._batch_syms, rows)
        _log(f"[proxy] flushed {self._n} symbols -> "
             + ", ".join(sorted(self._batch)) + ".jsonl")
        self._batch, self._batch_syms, self._n = {}, set(), 0

    # ------------------------------------------------------------ universe
    def load_universe(self, path):
        out, seen = [], set()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip().upper()
                if s and not s.startswith("#") and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out


def _merge(path, symbols, new_rows):
    """Upsert semantics for JSONL: drop existing rows belonging to the
    rebuilt symbols, append the fresh ones, atomic replace."""
    kept = []
    if path.exists():
        for row in storage.read_jsonl(path):
            if row.get("symbol") not in symbols:
                kept.append(row)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        for row in new_rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.description = (__doc__ or "").splitlines()[0]
    parser.add_argument("symbols", nargs="*",
                        help="tickers to build (single mode)")
    parser.add_argument("--universe", metavar="FILE",
                        help="one symbol per line; filters --bulk "
                             "(default --bulk = whole SEC ticker map)")
    parser.add_argument("--overrides", metavar="FILE",
                        help="JSON {SYMBOL: CIK} for tickers SEC's ticker map "
                             "omits (renamed/delisted issuers)")
    parser.add_argument("--md", action="store_true",
                        help="print the markdown digest to stdout per symbol")
    parser.add_argument("--insider-since", default="2021-01-01",
                        help="Form 3/4/5 collection start (YYYY-MM-DD)")
    parser.add_argument("--event-since", default="2019-01-01",
                        help="8-K event stream start (YYYY-MM-DD)")
    parser.add_argument("--promise-since", default="2022-01-01",
                        help="earnings-release promise scan start (YYYY-MM-DD)")
    # ---- bulk mode (production bulk semantics)
    bg = parser.add_argument_group("bulk mode")
    bg.add_argument("--bulk", action="store_true",
                    help="resumable sweep over the universe")
    bg.add_argument("--symbols", dest="symbols_csv", metavar="A,B,C",
                    help="comma list (smoke/explicit rebuild)")
    bg.add_argument("--limit", type=int, help="max tickers this invocation")
    bg.add_argument("--status", action="store_true", help="progress snapshot")
    bg.add_argument("--force", action="store_true",
                    help="rebuild symbols already marked ok (weekly freshness)")
    bg.add_argument("--worker", type=int, default=0, help="shard index")
    bg.add_argument("--workers", type=int, default=1, help="shard count")
    # ---- incremental mode
    ig = parser.add_argument_group("incremental mode")
    ig.add_argument("--incremental", action="store_true",
                    help="flag symbols with new panel-relevant filings since "
                         "their last build; write incremental_queue.txt")
    ig.add_argument("--no-scan", action="store_true", dest="no_scan",
                    help="skip the per-symbol scan (collect new tickers only)")
    ig.add_argument("--build-queue", action="store_true", dest="build_queue",
                    help="after scanning, immediately force-build the queue")


def run(args) -> int:
    ctx = Ctx(args)
    try:
        if args.bulk:
            args.symbols = args.symbols_csv
            rc = bulk.run(args, ctx)
        elif args.incremental:
            rc = incremental.run(args, ctx)
        else:
            syms = list(args.symbols)
            if not syms and args.universe:
                syms = ctx.load_universe(args.universe)
            if not syms:
                _log("error: give SYMBOL(s), or use --bulk / --incremental / "
                     "--universe FILE")
                return 2
            rc = 0
            for sym in syms:
                t0 = time.time()
                try:
                    payload = ctx.build_symbol(sym)
                    ctx.flush()
                    _log(f"[ok] {sym} -> {ctx.root} "
                         f"({payload['build_seconds']}s, "
                         f"failures={payload['module_failures'] or 'none'})")
                except Exception as e:
                    rc = 1
                    _log(f"[fail] {sym}: {type(e).__name__}: {e} "
                         f"({time.time()-t0:.1f}s)")
        return rc
    finally:
        ctx.flush()


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser(prog="secdata proxy")
    add_arguments(ap)
    sys.exit(run(ap.parse_args()))
