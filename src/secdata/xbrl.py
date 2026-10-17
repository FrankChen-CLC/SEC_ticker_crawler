"""SEC Company Facts / XBRL ingestion — inventory component #3.

Upstream endpoints (all through the shared throttled/caching client):

- ``https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json`` — per-company
  XBRL facts (the only XBRL source this lineage uses)
- ``https://data.sec.gov/submissions/CIK{cik10}.json`` — filings list used for
  the daily staleness check (cheap "did anything get filed?" probe)
- ``https://www.sec.gov/files/company_tickers_exchange.json`` — full ticker→CIK
  universe map

Frames API note: production never calls ``api/xbrl/frames``; companyfacts
values that *originated* from frames carry a ``frame`` field, which the parser
preserves into raw facts. Segments are not present in companyfacts and the v6
lineage ignores dimensional contexts — units are the only dimension parsed.

Ported from the production lineage (Sep 2026): the full rebuild
(extract + mapping, via ``xbrl_mapping.py``) and the two-phase daily
increment. MySQL loader, server-side ingest POST, proxy hosts and log-dir
side files are stripped; rows land in JSONL instead.

Output layout (under ``--out`` / $SEC_EDGAR_OUT, subdir ``xbrl/``):

- ``out/xbrl/companyfacts/CIK{cik10}.json`` — raw companyfacts bytes exactly
  as SEC served them (audit copy; the client's URL cache is separate)
- ``out/xbrl/metrics.jsonl`` — normalized metric rows, one JSON object per
  line, replacing all previous rows for symbols refreshed this run
- ``out/xbrl/checkpoint/state.json``   — watermark: latest annual
  ``:NetIncomeLoss`` period-end per symbol (what production derived from
  MySQL ``financial_statement_rows``), plus last-run summary
- ``out/xbrl/checkpoint/done.txt|failed.txt`` — per-symbol outcome log.
  NOTE: like production after the 2026-08-07 incident, done.txt is written
  but NEVER gates a re-run — a frozen done-file once froze every update to
  zero; the submissions-staleness watermark replaced it.
- ``out/xbrl/checkpoint/cik_map_cache.json`` — 7-day ticker→CIK map cache with
  stale fallback (the 2026-08-05 SEC-429-emptied-the-run protection).

metrics.jsonl row schema (twin of production ``financial_statement_rows``):

    {
      "symbol":         ticker, or the companyfacts entityName when input was
                        a bare --cik,
      "statement_type": "income" | "balance" | "cashflow"   (auto-inferred),
      "period_type":    "annual" | "quarterly",
      "metric_key":     "<standard>:<Tag>" (us-gaap / ifrs-full), <=128 chars,
      "metric_label":   XBRL label,
      "latest_value":   value at the max period-end (float) or null,
      "unit":           coarse detected unit: currency|shares|ratio|<unit-id>,
      "standard":       us-gaap | ifrs-full,
      "original_tag":   bare tag name,
      "description":    XBRL description (<=500 chars),
      "is_important":   0/1 — important-metric mapping (see xbrl_mapping),
      "display_order":  0 = most important ... 99 = not important,
      "values":         {"YYYY-MM-DD": number}  period-end -> value,
      "raw_facts":      {"YYYY-MM-DD": [fact|derived,...]} or absent; facts
                        keep unit/val/start/end/fy/fp/form/filed/accn/frame/
                        duration_days, derived rows carry {"derived","val",
                        "source_dates"} instead,
      "source":         "xbrl_v6"   (extraction lineage tag)
    }

Modes:

- ``secdata xbrl AAPL MSFT --cik 105054`` — fetch/parse given entities
  directly (mirrors production single-ticker smoke; no staleness gate).
- ``secdata xbrl --all [--universe FILE] [--limit N]`` — whole ticker→CIK map
  (or just the symbols in your universe file) = full rebuild.
- ``secdata xbrl --daily`` — the cron mode: Phase 1 = one submissions probe per
  eligible symbol vs the watermark, Phase 2 = companyfacts fetch only for
  symbols where a 10-K/10-Q/20-F/40-F filing is newer than our latest annual
  NetIncomeLoss period-end.  ``--force`` bypasses Phase 1 and refreshes
  everything (production used this after basis-mapping fixes).
- ``secdata xbrl --retry-failed`` — re-run the symbols in failed.txt.
- ``secdata xbrl --only-overrides`` — refresh only the curated override-file
  symbols (production ``--only-extra`` for SEC-map-omitted filers).
- ``secdata xbrl AAPL --check`` — download + parse, print a quality report to
  stderr, write nothing.

Pacing: every request goes through the one shared client throttle. Production
ran ~8-10 req/s with 4-16 workers; with the default ``SEC_EDGAR_SLEEP=0.15``
you get ~6.6 req/s regardless of ``--workers`` — lower SEC_EDGAR_SLEEP only if
you know what you are doing (SEC fair-access caps 10 req/s). A full ``--all``
first pass is ~10k multi-MB companyfacts downloads; prefer ``--daily``.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import client as _client
from . import storage
from . import ticker as _ticker
from .xbrl_mapping import extract_metrics, metrics_rows

COMMAND = "xbrl"
HELP = "SEC Company Facts / XBRL: raw cache + normalized metrics JSONL, daily incremental"

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

CIK_MAP_TTL_SEC = 7 * 24 * 3600     # production: refresh map at most weekly
DEFAULT_WORKERS = 4                 # production cron MAX_WORKERS
DEFAULT_CHECK_WORKERS = 16          # production Phase-1 check pool
PROGRESS_EVERY = 100
SOURCE_TAG = "xbrl_v6"              # lineage marker kept on every row

# filings whose arrival can change XBRL facts — amendments included
# (daily-increment list: 10-K/10-Q/20-F/40-F and their /A variants)
STALENESS_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A")


def _err(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ pure bits


def parse_exchange_map(payload: dict) -> dict:
    """company_tickers_exchange.json -> {TICKER: CIK10}. Entry = [cik, name,
    ticker, exchange]; rows shorter than 3 fields are skipped (production
    guard)."""
    m = {}
    for entry in payload.get("data", []):
        if len(entry) >= 3:
            m[str(entry[2]).upper()] = str(entry[0]).zfill(10)
    return m


def merge_overrides(cik_map: dict, ovr: dict):
    """Superimpose the curated symbol->CIK supplement (production
    extra_cik.json; here the overrides file) onto the SEC map. SEC's aggregate
    file omits some real filers (EA/BK/AMWD/CVGW...), so curated entries merge
    on top — only after you confirm companyfacts has data for them.
    Returns (merged_map, added_symbols); never mutates inputs."""
    merged = dict(cik_map)
    added = []
    for sym, cik in ovr.items():
        s = str(sym).upper()
        if s not in merged:
            added.append(s)
        merged[s] = _ticker.cik10(cik)
    return merged, added


def sec_has_newer(submissions: dict, local_latest) -> bool:
    """Production ``check_sec_newer`` (daily increment, newest variant):
    does SEC list a 10-K/10-Q/20-F/40-F filing newer than our local latest
    annual period?  No local data -> must fetch. Has local data but no such
    filing at all -> nothing new to pull."""
    recent = submissions.get("filings", {}).get("recent", {}) or {}
    forms = recent.get("form") or []
    fdates = recent.get("filingDate") or []
    latest = None
    for i, form in enumerate(forms):
        if form in STALENESS_FORMS:
            fd = fdates[i] if i < len(fdates) else None
            if fd and (latest is None or fd > latest):
                latest = fd
    if not local_latest:
        return True
    if not latest:
        return False
    return latest[:10] > local_latest[:10]


def local_latest_end(extraction: dict):
    """Watermark production derived from MySQL: max annual period-end among
    ``*:NetIncomeLoss`` metric keys (covers us-gaap and ifrs-full)."""
    best = None
    for tag_key, vals in (extraction.get("annual") or {}).items():
        if tag_key.endswith(":NetIncomeLoss") and vals:
            m = max(vals.keys())
            if best is None or m > best:
                best = m
    return best


def parse_failed_lines(text: str) -> list:
    """checkpoint/failed.txt lines are ``SYMBOL\\treason``; take the symbol."""
    syms = []
    for line in text.splitlines():
        tok = line.split("#")[0].strip()
        if tok:
            syms.append(tok.split("\t")[0].split()[0].upper())
    return syms


def rewrite_metrics_rows(path, rows_by_symbol):
    """Replace rows of refreshed symbols, keep everyone else (file twin of
    production per-symbol DELETE+INSERT). Atomic via tmp+rename.
    Returns (kept_rows, new_rows)."""
    path = Path(path)
    tmp = path.with_suffix(".jsonl.part")
    kept = new = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for row in storage.read_jsonl(path):
            if row.get("symbol") in rows_by_symbol:
                continue
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            kept += 1
        for sym in sorted(rows_by_symbol):
            for row in rows_by_symbol[sym]:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                new += 1
    os.replace(tmp, path)
    return kept, new


# ------------------------------------------------------------- CIK map loading


def _read_map_cache(cache_file: Path, allow_stale: bool):
    """Return cached map, or None if missing/corrupt/expired. allow_stale
    ignores TTL (last resort — empty beats nothing, per 8/05 incident)."""
    if not cache_file.exists():
        return None
    try:
        meta = json.loads(cache_file.read_text())
        age = int(time.time() - meta.get("ts", 0))
        if age > CIK_MAP_TTL_SEC and not allow_stale:
            return None
        m = meta.get("map")
        return m or None
    except Exception:
        return None


def load_cik_map(client, cache_file: Path) -> dict:
    """Live ticker→CIK map via SEC, falling back to the local 7-day cache and
    then to the stale cache (production's 2026-08-05 SEC-429 protection:
    an empty map used to silently zero the whole run)."""
    try:
        payload = client.get_json(TICKER_MAP_URL, max_age_hours=CIK_MAP_TTL_SEC / 3600.0)
        m = parse_exchange_map(payload)
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "map": m}))
        except Exception as e:  # cache write failure must not kill the run
            _err(f"  cik map cache save failed: {e}")
        _err(f"CIK map live fetch: {len(m)} tickers")
        return m
    except Exception as e:
        _err(f"CIK map live fetch failed: {type(e).__name__}: {e}")
    fresh = _read_map_cache(cache_file, allow_stale=False)
    if fresh:
        _err(f"CIK map using CACHE: {len(fresh)} tickers (live fetch failed)")
        return fresh
    stale = _read_map_cache(cache_file, allow_stale=True)
    if stale:
        _err(f"CIK map using STALE CACHE (>7d): {len(stale)} tickers — live fetch failed AND fresh cache missing")
        return stale
    _err("CIK map: NO cache available — run will have 0 tickers")
    return {}


# ------------------------------------------------------------------ processing


def _process_target(client, sym, cik, cf_dir, write_files, max_age):
    """Fetch companyfacts for one CIK, extract, build rows. Returns dict."""
    try:
        raw = client.get_bytes(COMPANYFACTS_URL.format(cik=cik), max_age_hours=max_age)
        facts = json.loads(raw)
        if not facts:
            return {"sym": sym, "cik": cik, "status": "no_facts"}
        name = sym or str(facts.get("entityName") or "").strip() or f"CIK{cik}"
        if write_files:
            storage.save_doc(cf_dir, f"CIK{cik}.json", raw)
        ext = extract_metrics(facts, name)
        if not ext:
            return {"sym": name, "cik": cik, "status": "no_data"}
        if not ext.get("annual") and not ext.get("quarterly"):
            return {"sym": name, "cik": cik, "status": "no_metrics"}
        rows = metrics_rows(name, ext)
        return {"sym": name, "cik": cik, "status": "ok", "rows": rows,
                "end": local_latest_end(ext), "ext": ext}
    except urllib.error.HTTPError as e:
        return {"sym": sym, "cik": cik, "status": f"http_{getattr(e, 'code', '?')}"}
    except Exception as e:
        return {"sym": sym, "cik": cik, "status": f"err {type(e).__name__}: {e}"}


def _check_target(client, sym, cik, watermark, max_age):
    """Phase-1 staleness probe. Check errors mean *skip this run, retry next
    cycle* (production), never 'fetch anyway'."""
    try:
        sub = client.get_json(SUBMISSIONS_URL.format(cik=cik), max_age_hours=max_age)
        return sym if sec_has_newer(sub, watermark.get(sym)) else None
    except Exception as e:
        _err(f"  {sym}: submissions check failed ({type(e).__name__}) — skip this run, retry next cycle")
        return None


# ----------------------------------------------------------------------- CLI


def add_arguments(parser):
    parser.add_argument("symbols", nargs="*", metavar="SYMBOL",
                        help="tickers to ingest (resolved to CIK via SEC map/overrides)")
    parser.add_argument("--cik", nargs="*", metavar="CIK",
                        help="ingest by CIK directly (display name = companyfacts entityName)")
    parser.add_argument("--universe", metavar="FILE",
                        help="symbol list (one per line); filters --all/--daily, "
                             "otherwise acts as the target list")
    parser.add_argument("--all", action="store_true",
                        help="full rebuild over the whole ticker->CIK map universe")
    parser.add_argument("--daily", action="store_true",
                        help="incremental cron mode: submissions staleness check, "
                             "refresh only changed symbols")
    parser.add_argument("--force", action="store_true",
                        help="skip the staleness check and refresh every eligible "
                             "symbol (implies universe mode)")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the universe (sorted by symbol) — smoke runs")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"fetch/extract concurrency (default {DEFAULT_WORKERS}; "
                             "throttle is global, see module docstring)")
    parser.add_argument("--check-workers", dest="check_workers", type=int,
                        default=DEFAULT_CHECK_WORKERS,
                        help=f"Phase-1 submissions-check concurrency (default {DEFAULT_CHECK_WORKERS})")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-process symbols recorded in checkpoint/failed.txt")
    parser.add_argument("--only-overrides", action="store_true",
                        help="refresh only the symbols in the overrides file "
                             "(production --only-extra)")
    parser.add_argument("--overrides", metavar="FILE",
                        help="curated SYMBOL->CIK JSON merged on top of the SEC map "
                             "(default $SEC_EDGAR_OVERRIDES)")
    parser.add_argument("--check", action="store_true",
                        help="download + parse only; quality report to stderr, write nothing")
    return parser


def _read_universe(path):
    syms = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                syms.append(s.upper())
    return syms


def run(args) -> int:
    c = _client.default_client()
    # production never served companyfacts/submissions from cache — live runs
    # must refetch (max_age 0); offline mode instead reads whatever is cached.
    ma = None if c.offline else 0

    out = storage.out_root("xbrl")
    cf_dir = out / "companyfacts"
    ckpt = storage.Checkpoint(out / "checkpoint")
    state = storage.State(out / "checkpoint" / "state.json")
    map_cache = out / "checkpoint" / "cik_map_cache.json"
    metrics_path = out / "metrics.jsonl"

    universe_file = _read_universe(args.universe) if args.universe else None
    ovr = _ticker.overrides(args.overrides)

    # ---------- build target list [(symbol-or-"", cik10)] ----------
    explicit = [s.upper() for s in (args.symbols or [])]
    targets = []
    for s in explicit:
        try:
            targets.append((s, _ticker.cik10(_ticker.cik_for(s, client=c, overrides_path=args.overrides))))
        except KeyError as e:
            _err(f"error: {e}")
            return 1
    for raw_cik in (args.cik or []):
        try:
            targets.append(("", _ticker.cik10(raw_cik)))
        except (ValueError, TypeError):
            _err(f"error: bad --cik value {raw_cik!r}")
            return 1

    universe_mode = bool(args.all or args.daily or args.force
                         or args.retry_failed or args.only_overrides
                         or (universe_file and not explicit))
    if not targets and universe_mode:
        if args.only_overrides and not (args.all or args.daily or args.force):
            targets = sorted({(s.upper(), _ticker.cik10(v)) for s, v in ovr.items()})
        else:
            cik_map, _added = merge_overrides(load_cik_map(c, map_cache), ovr)
            if not cik_map:
                _err("error: no CIK map available (live fetch failed, no cache)")
                return 1
            if args.retry_failed:
                failed_file = out / "checkpoint" / "failed.txt"
                syms = parse_failed_lines(failed_file.read_text()) if failed_file.exists() else []
                targets = [(s, cik_map[s]) for s in syms if s in cik_map]
                _err(f"retry-failed: {len(targets)}/{len(syms)} symbols resolved")
            else:
                pool = universe_file if universe_file else sorted(cik_map)
                if args.only_overrides:
                    ov_up = {s.upper() for s in ovr}
                    pool = [s for s in pool if s in ov_up]
                eligible = [s for s in pool if s in cik_map]
                if args.limit:
                    eligible = eligible[:args.limit]
                _err(f"universe: {len(eligible)} eligible symbols"
                     + (f" (file {args.universe})" if universe_file else ""))
                targets = [(s, cik_map[s]) for s in eligible]

        # daily increment, Phase 1 (unless --force / retry / explicit inputs)
        if args.daily and not args.force:
            watermark = state.get("watermark") or {}
            _err(f"Phase 1: checking SEC submissions for {len(targets)} symbols "
                 f"(local watermark for {len(watermark)})...")
            t1 = time.time()
            changed = []
            checked = 0
            with ThreadPoolExecutor(max_workers=max(1, args.check_workers)) as ex:
                futs = {ex.submit(_check_target, c, s, cik, watermark, ma): s
                        for s, cik in targets}
                for fut in as_completed(futs):
                    r = fut.result()
                    if r:
                        changed.append(r)
                    checked += 1
                    if checked % 1000 == 0:
                        _err(f"  checked {checked}/{len(targets)}, updates so far: {len(changed)} "
                             f"({time.time()-t1:.0f}s)")
            targets = [(s, cik) for s, cik in targets if s in changed]
            _err(f"Phase 1 done: {len(targets)}/{checked} symbols need update "
                 f"({time.time()-t1:.0f}s)")
    elif not targets and not explicit:
        _err("nothing to do: give tickers/CIKs, --universe FILE, or --all/--daily")
        return 2
    if universe_file and explicit:
        _err(f"note: {len(explicit)} explicit symbols take precedence; ignoring --universe")

    if not targets:
        _err("no updates needed")
        return 0

    # ---------- Phase 2: fetch + extract ----------
    rows_by_symbol = {}
    ends = {}
    results = {"ok": 0, "fail": 0}
    failed = []
    lock = threading.Lock()
    t0 = time.time()
    done_n = [0]

    def _wrap(sym, cik):
        r = _process_target(c, sym, cik, cf_dir, not args.check, ma)
        with lock:
            done_n[0] += 1
            if r["status"] == "ok":
                results["ok"] += 1
                rows_by_symbol[r["sym"]] = r["rows"]
                if r.get("end"):
                    ends[r["sym"]] = r["end"]
            else:
                results["fail"] += 1
                failed.append((r["sym"], r["status"]))
            n = done_n[0]
        if n % PROGRESS_EVERY == 0 or n == len(targets):
            _err(f"  {n}/{len(targets)} ok={results['ok']} fail={results['fail']}")
        return r

    reports = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = [ex.submit(_wrap, s, cik) for s, cik in targets]
        for fut in as_completed(futs):
            r = fut.result()
            if args.check:
                reports.append(r)

    # ---------- write ----------
    if args.check:
        _err("quality report (--check: nothing written):")
        for r in sorted(reports, key=lambda x: x["sym"]):
            if r["status"] != "ok":
                _err(f"  {r['sym']}: {r['status']}")
                continue
            ext = r["ext"]
            stmt = {"income": 0, "balance": 0, "cashflow": 0}
            for mi in ext.get("metrics_meta", {}).values():
                stmt[mi.get("statement_type", "income")] += 1
            _err(f"  {r['sym']}: annual={len(ext.get('annual', {}))} tags, "
                 f"quarterly={len(ext.get('quarterly', {}))} tags, "
                 f"unique={len(ext.get('metrics_meta', {}))}, stmt_dist={stmt}")
        return 0 if results["ok"] else 1

    if rows_by_symbol:
        kept, new = rewrite_metrics_rows(metrics_path, rows_by_symbol)
        _err(f"metrics.jsonl: {new} rows for {len(rows_by_symbol)} symbols rewritten, "
             f"{kept} prior rows kept")

    if ends:
        watermark = state.get("watermark") or {}
        watermark.update(ends)
        state.set("watermark", watermark)
    state.set("last_run", {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "targets": len(targets), "ok": results["ok"],
                           "fail": results["fail"], "source": SOURCE_TAG})
    for sym in rows_by_symbol:
        ckpt.mark_done(sym)
    for sym, reason in failed:
        ckpt.mark_failed(sym, reason)

    _err(f"Done in {(time.time()-t0)/60:.1f}min. ok={results['ok']} fail={results['fail']}")
    if failed:
        _err("Failures (first 20):")
        for sym, reason in failed[:20]:
            _err(f"  {sym}: {reason}")
        return 1
    return 0
