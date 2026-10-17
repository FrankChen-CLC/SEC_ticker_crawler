"""Weekly freshness scan for the proxy pipeline.

Flags a symbol for rebuild when EDGAR shows a filing that feeds the panel
(DEF 14A family, Form 4, 8-K, 10-K) dated AFTER that symbol's stored build
date, and queues tickers that appeared in company_tickers.json since the last
crawl. Submissions carry a 24h cache TTL (fetch.recent_filings), so a weekly
pass always refetches; the rebuild itself then runs from the warm disk cache
and costs seconds per symbol, not minutes.

Writes ``out/proxy/incremental_queue.txt`` (symbols, one per line). Feed it
to a forced bulk pass:

  secdata proxy --bulk --force --symbols "$(paste -sd, out/proxy/incremental_queue.txt)"

Production read the build dates from MySQL; here they come from the same
checkpoint state.json the bulk/single modes update.
"""
import sys
import time

from .. import ticker
from . import bulk, fetch

# forms that change panel content (promises consumes 8-K/EX-99; pvp/SCT/
# clawback key off the DEF 14A family; insiders = Form 4). "4" must be an
# EXACT match -- startswith("4") would catch 424B prospectuses.
FORM_EXACT = ("4", "4/A")
FORM_PREFIX = ("DEF 14A", "DEFA14A", "PRE 14A", "DEFM14A", "PX14A6G",
               "8-K", "8-K/A", "10-K")


def relevant(form):
    return form in FORM_EXACT or form.startswith(FORM_PREFIX)


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(args, ctx):
    limit = getattr(args, "limit", None)
    no_scan = getattr(args, "no_scan", False)

    recs = {s: r for s, r in ctx.records().items() if r.get("built_at")}
    skipped = {s for s, r in ctx.records().items() if r.get("status") == "skip"}

    queue = []
    scanned = fetch_fail = 0
    t0 = time.time()
    if not no_scan:
        for sym in sorted(recs):
            if limit and scanned >= limit:
                break
            scanned += 1
            r = recs[sym]
            try:
                fl = fetch.recent_filings(r["cik"])   # 24h TTL -> weekly refetch
            except Exception:
                fetch_fail += 1
                continue
            b = str(r.get("built_at") or "1970-01-01")[:10]
            if any(relevant(f["form"]) and f["date"] > b for f in fl):
                queue.append(sym)
            if scanned % 1000 == 0:
                _log(f"[incr] scanned {scanned}/{len(recs)} "
                     f"queue={len(queue)} t={time.time()-t0:.0f}s")

    # brand-new tickers (IPO / freshly mapped) since the last crawl —
    # company_tickers.json refetched unconditionally (max_age_hours=0)
    d = fetch.get_json(ticker.BRIDGE_TICKERS_URL, max_age_hours=0)
    tm = {str(v["ticker"]).upper() for v in d.values()}
    built = {s.upper() for s in recs}
    new_syms = sorted(tm - built - skipped)
    queue.extend(new_syms)

    uniq = sorted(set(queue))
    qpath = ctx.root / "incremental_queue.txt"
    qpath.write_text("\n".join(uniq))
    _log(f"[incr] scanned={scanned} fetch_fail={fetch_fail} "
         f"changed={len(uniq) - len(new_syms)} new={len(new_syms)} "
         f"queue={len(uniq)} in {time.time()-t0:.0f}s -> {qpath}")
    if uniq and getattr(args, "build_queue", False):
        # convenience: run the forced rebuild in the same invocation
        args.symbols = ",".join(uniq)
        args.force = True
        args.limit = None
        return bulk.run(args, ctx)
    return 0
