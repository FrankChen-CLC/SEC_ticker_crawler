# secdata — internal module contract (for contributors)

`secdata` packages the SEC EDGAR pipelines proven in a production
market-research stack (2026) into a clean, self-contained open-source project. Every
pipeline is one module under `src/secdata/`, one CLI subcommand, and writes
plain files — no database server, no private endpoints.

## Shared foundation (read first, do not edit casually)

- `src/secdata/config.py` — env knobs: `SEC_EDGAR_UA` (required),
  `SEC_EDGAR_SLEEP/TIMEOUT/RETRIES/CACHE/OUT/OFFLINE`, `SEC_PROXY`,
  `SEC_EDGAR_OVERRIDES`.
- `src/secdata/client.py` — `EdgarClient` / `default_client()`:
  throttled (>= 0.15 s default, under SEC's 10 req/s), disk-cached by URL
  sha1 (filings cached forever; index/metadata by `max_age_hours`),
  retries with backoff incl. 429/503 exponential, requests session w/
  urllib fallback, optional proxy, `SEC_EDGAR_OFFLINE=1` = cache-only.
- `src/secdata/ticker.py` — `cik_for(symbol)` via company_tickers.json +
  overrides file, `cik10`, `filings_of(submissions_json, forms=…, after=…,
  before=…)` (amendment-aware `form_matches`), `archive_url`, `index_json_url`.
- `src/secdata/storage.py` — `out_root(sub)`, `JsonlWriter` (append +
  dedupe by key), `read_jsonl`, `Checkpoint` (done.txt/failed.txt resume),
  `State` (atomic JSON watermark), `save_doc`.

## Module contract

Every pipeline module MUST define:

```python
COMMAND = "insider"            # subcommand name
HELP    = "one-line help"      # shown by `secdata -h`
def add_arguments(parser): ... # argparse wiring
def run(args) -> int: ...      # returns exit code; 0 on success
```

`run()` uses `default_client()` (constructed *inside* run, after cli.py has
applied `--ua/--out/--proxy` to the environment), resolves tickers with
`ticker.cik_for`, and writes under `storage.out_root("<command>")`.

## Rules when porting a production pipeline

1. **Preserve** the production behaviour that makes it work: exact EDGAR
   endpoints & query params, form filters, pacing, resume/checkpoint
   semantics, parser field names, classification/edge-case rules, bug
   workarounds (they encode real SEC quirks). Put the upstream endpoint(s)
   in the module docstring.
2. **Strip** all of: MySQL/Postgres, SSH hosts, VPS IPs, private HTTP
   endpoints, ingest tokens, personal emails/paths, log dirs outside `--out`,
   watchdog/`ps aux` glue, LLM calls. Replace DB writes with JSONL/raw-doc
   files under `out/<command>/`; replace fixed ticker universes with an
   optional `--universe FILE` (one symbol per line; absent = no filter).
3. **No secrets, no hosts.** Nothing may reference real server IPs/domains,
   tailnet addresses, internal service names, or any password. Default UA
   must never be a real person's — require `SEC_EDGAR_UA` (client raises),
   `--ua` overrides.
4. Python **3.9 compatible** (no `match`, no `X | Y` runtime typing without
   `from __future__ import annotations`). Stdlib + `requests` only (and
   `requests` only via client). If a component truly needs bs4/lxml, list it
   as an extra in your report — don't add deps to pyproject yourself.
5. Progress to stderr (`print(..., file=sys.stderr)`), data to files.
   Idempotent re-runs must be cheap (cache + checkpoints).
6. Threads: `ThreadPoolExecutor` allowed (production uses 4–8 workers), but
   all HTTP through the shared `default_client()` so one throttle governs
   the whole process; document `SEC_EDGAR_SLEEP` scaling for many workers.
7. Tests: `tests/test_<module>.py`, **offline only** — feed canned JSON/XML
   (small, synthetic, pasted in the test or `tests/fixtures/`) into the
   pure parse/build functions; monkeypatch `client.get_json/get_text` for
   flow tests. Verify with the repo venv:
   `cd <repo> && .venv/bin/python -m pytest tests/test_<module>.py -q`
   and `.venv/bin/python -m py_compile src/secdata/<module>.py`.
8. Do NOT edit shared foundation files or other modules; if you need a
   foundation change, note it in your final report instead.

## Output conventions

- rows: `out/<command>/<entity>.jsonl` (e.g. `out/insider/trades.jsonl`)
- raw docs: `out/<command>/docs/…` mirroring EDGAR paths (accession dirs)
- resume: `out/<command>/checkpoint/done.txt|failed.txt`, `state.json`
