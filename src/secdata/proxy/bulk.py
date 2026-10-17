"""Full-market proxy bulk build.

Same per-symbol pipeline as ``secdata proxy SYM`` (report.build -> JSONL
under out/proxy/), run across every ticker in SEC company_tickers.json (or an
optional ``--universe`` file), with production semantics preserved:

  resumable   progress in out/proxy/checkpoint/state.json (ok/skip/fail +
              tries per symbol) and done.txt/failed.txt; re-running the same
              command continues where it stopped
  fatigue-    a shared egress line rate-limited by SEC degrades under
    aware     sustained load, so the runner works BULK_WORK_SECS (default
              2h) then rests BULK_REST_SECS (default 25min) on a cycle
  breaker     BULK_BREAKER (default 12) consecutive symbol-level failures
              trigger a long cooldown (BULK_BREAKER_REST, default 45min)
  retries     a sweep covers the whole universe; failed symbols get up to
              BULK_MAX_TRIES (default 3) attempts across sweeps in the same
              process. 404/no-identity symbols are marked skip (never retried,
              never burn requests again — even under --force).

HTTP pacing is governed by the shared EdgarClient: for a wide sweep raise
SEC_EDGAR_SLEEP as usual (one worker is the production bulk shape).

Usage:
  secdata proxy --bulk                 # full universe, resumable
  secdata proxy --bulk --symbols NVDA  # smoke, explicit list
  secdata proxy --bulk --status        # progress snapshot
  secdata proxy --bulk --limit 400     # one batch, then exit
  secdata proxy --bulk --force         # rebuild 'ok' symbols (weekly freshness)
"""
import os
import sys
import time
import traceback
import urllib.error

from .. import ticker

WORK_SECS = float(os.environ.get("BULK_WORK_SECS", 2 * 3600))
REST_SECS = float(os.environ.get("BULK_REST_SECS", 25 * 60))
BREAKER_N = int(os.environ.get("BULK_BREAKER", "12"))
BREAKER_REST = float(os.environ.get("BULK_BREAKER_REST", 45 * 60))
MAX_TRIES = int(os.environ.get("BULK_MAX_TRIES", "3"))
# --force: rebuild symbols already 'ok' (weekly freshness). identity/404
# 'skip' rows stay excluded so a forced pass can never burn requests on them.
FORCE = False


def _progress(msg):
    print(msg, file=__import__("sys").stderr, flush=True)


def print_status(ctx):
    from .. import storage
    recs = ctx.records()
    counts = {}
    for r in recs.values():
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
    tot_secs = sum(r.get("secs") or 0 for r in recs.values())
    for st in ("ok", "skip", "fail"):
        n = counts.get(st, 0)
        avg = (sum(r.get("secs") or 0 for r in recs.values()
                   if r.get("status") == st) / n) if n else 0
        _progress(f"  {st:6s} {n:6d}  avg {avg:.1f}s")
    built = sum(1 for l in storage.read_jsonl(ctx.root / "builds.jsonl"))
    _progress(f"  build rows on disk: {built}  total work {tot_secs/3600:.1f}h")
    fails = sorted(((r.get("ts") or 0, s, r) for s, r in recs.items()
                    if r.get("status") == "fail"), reverse=True)[:10]
    if fails:
        _progress("  recent fails:")
        for _ts, s, r in fails:
            _progress(f"    {s} tries={r.get('tries')} err={(r.get('err') or '')[:90]}")
    return 0


def _mark(ctx, sym, cik, status, secs, err, tries):
    rec = ctx.records().setdefault(sym, {})
    rec.update({"cik": cik, "status": status, "secs": round(secs, 1),
                "tries": tries, "err": err, "ts": time.time()})
    ctx.mark_checkpoint(sym, status)
    ctx.state.save()


def _pending(ctx, universe):
    """Symbols not yet settled (ok/skip) and still under MAX_TRIES."""
    recs = ctx.records()
    n = 0
    for sym in universe:
        st = recs.get(sym)
        if st is None or (st.get("status") not in ("ok", "skip")
                          and (st.get("tries") or 1) < MAX_TRIES):
            n += 1
    return n


def sweep(ctx, universe, stats, limit=None):
    """One pass over `universe`, skipping settled/exhausted symbols.
    stats = shared dict {ok, skip, fail, attempted, consec_fail, work}."""
    t_start = time.time()
    recs = ctx.records()
    for sym in universe:
        st = recs.get(sym)
        if st and ((st.get("status") in ("ok", "skip")
                    and not (FORCE and st.get("status") == "ok"))
                   or (st.get("tries") or 1) >= MAX_TRIES):
            continue
        if limit and stats["attempted"] >= limit:
            _progress(f"[bulk] --limit {limit} reached, exiting (resumable)")
            return False  # not finished this sweep on purpose
        if stats["work"] >= WORK_SECS:
            _progress(f"[bulk] fatigue guard: worked {stats['work']/3600:.2f}h -> "
                      f"resting {REST_SECS/60:.0f}min")
            time.sleep(REST_SECS)
            stats["work"] = 0.0
        try:  # cik only recorded in state; overrides included
            cik = ticker.cik_for(sym, overrides_path=ctx.overrides_path)
        except KeyError:
            cik = ""
        t0 = time.time()
        try:
            ctx.build_symbol(sym)
            status, err = "ok", None
            stats["ok"] += 1
            stats["consec_fail"] = 0
        except urllib.error.HTTPError as e:
            if e.code == 404:
                status, err = "skip", "HTTP 404"
                stats["skip"] += 1
                stats["consec_fail"] = 0
            else:
                status, err = "fail", f"HTTP {e.code}"
                stats["fail"] += 1
                stats["consec_fail"] += 1
        except (KeyError, ValueError) as e:
            status, err = "skip", f"identity: {e}"  # no CIK / odd shape
            stats["skip"] += 1
            stats["consec_fail"] = 0
        except Exception as e:
            status = "fail"
            err = traceback.format_exc()[-300:].replace("\n", " ")
            stats["fail"] += 1
            stats["consec_fail"] += 1
        secs = time.time() - t0
        stats["work"] += secs
        stats["attempted"] += 1
        _mark(ctx, sym.upper(), cik, status, secs, err,
              (st.get("tries", 0) + 1) if st else 1)
        _progress(f"[bulk {stats['attempted']}] {sym} {status} {secs:.1f}s"
                  + (f"  err={err[:90]}" if err else ""))
        ctx.maybe_flush()
        if stats["consec_fail"] >= BREAKER_N:
            _progress(f"[bulk] breaker: {stats['consec_fail']} consecutive fails "
                      f"-> cooldown {BREAKER_REST/60:.0f}min")
            time.sleep(BREAKER_REST)
            stats["consec_fail"] = 0
            stats["work"] = 0.0
    _progress(f"[bulk] sweep done: ok={stats['ok']} skip={stats['skip']} "
              f"fail={stats['fail']} elapsed {(time.time()-t_start)/3600:.2f}h")
    return True


def run(args, ctx):
    """Bulk entry called by secdata.proxy.run()."""
    global FORCE
    FORCE = bool(getattr(args, "force", False))
    if getattr(args, "status", False):
        return print_status(ctx)
    if getattr(args, "symbols", None):
        universe = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif getattr(args, "universe", None):
        universe = ctx.load_universe(args.universe)
    else:
        universe = sorted(ticker.ticker_map(ctx.client))[args.worker::args.workers]
    _progress(f"[bulk w{args.worker}/{args.workers}] start "
              f"sleep={ctx.client.sleep}s work={WORK_SECS/60:.0f}m/"
              f"rest={REST_SECS/60:.0f}m slice={len(universe)} "
              f"ua={ctx.client.ua}")
    stats = {"ok": 0, "skip": 0, "fail": 0, "attempted": 0,
             "consec_fail": 0, "work": 0.0}
    ctx.stats = stats
    sweep(ctx, universe, stats, limit=args.limit)
    rc = 0 if stats["fail"] == 0 else 1
    if args.symbols or args.limit:
        return rc
    # retry sweep: failed symbols with tries left
    while _pending(ctx, universe):
        stats["attempted"] = 0
        sweep(ctx, universe, stats)
    _progress(f"[bulk] ALL DONE. remaining pending: {_pending(ctx, universe)}")
    return rc
