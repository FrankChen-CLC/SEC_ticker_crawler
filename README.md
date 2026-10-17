# secdata

Fetch U.S. stock data from [SEC EDGAR](https://www.sec.gov/edgar) — one CLI,
ten pipelines, plain files. No database server, no API keys: give it a
ticker, get filings, XBRL facts, insider trades, holdings, proxy-statement
intelligence, new S-1s, and enforcement releases into JSONL you can grep,
`pandas.read_json`, or load anywhere.

`secdata` is the extracted, sanitized open-source edition of the SEC EDGAR
pipelines that run daily in a production market-research stack (2026). All
private infrastructure (databases, ingest endpoints, hosts) was removed; the
request pacing, EDGAR endpoints, parsers, incremental/resume logic and the
field-level bug workarounds that encode real SEC quirks were preserved.

## Pipelines

| Command | What it fetches | Key outputs (under `--out`, default `./out`) |
|---|---|---|
| `secdata filings` | Bulk filing index + documents for symbols or whole-market quarterly scans, filtered by form/date | `filings/manifest.jsonl`, `filings/docs/…` |
| `secdata reports` | 10-K / 10-Q / 20-F archive: index-first incremental refresh, or `--full` backfill | `reports/docs/TICKER/FORM/ACCN/primary-document.html`, `reports/manifest.jsonl` |
| `secdata xbrl` | Company Facts (XBRL) raw cache + normalized financial metrics with importance ranks (v6 lineage) | `xbrl/companyfacts/CIK##########.json`, `xbrl/metrics.jsonl` |
| `secdata prexbrl` | Pre-XBRL (SGML-era, pre-2009) complete submissions + deterministic evidence-linked metric extraction | `prexbrl/docs/…`, `prexbrl/metrics.jsonl`, `prexbrl/extractions/…` |
| `secdata holdings` | 13F-HR and N-PORT: bulk quarterly download, XML/legacy-table parsing, quarter/institution comparison | `holdings/holdings_13f.jsonl`, `holdings/holdings_nport.jsonl`, `holdings/compare_*.jsonl` |
| `secdata insider` | Form 4 daily incremental (EFTS search) or `--bulk` history; full-fidelity trades with 10b5-1 / economic-reason classification | `insider/filings.jsonl`, `insider/trades.jsonl`, `insider/docs/…` |
| `secdata events` | 8-K download with press-release exhibits + item-code event indexing | `events/events.jsonl`, `events/docs/…` |
| `secdata proxy` | DEF 14A extraction → executive intelligence: comp tables (incl. hidden-layer SCT recovery), shareholder promises, capital actions, insider cluster events | `proxy/executives.jsonl`, `proxy/comp.jsonl`, `proxy/promises.jsonl`, `proxy/capital.jsonl`, `proxy/insiders.jsonl` |
| `secdata preipo` | S-1 / F-1 registration monitor: discover, track amendments, confirm IPOs | `preipo/all.jsonl`, `preipo/new.jsonl`, `preipo/docs/…` |
| `secdata enforcement` | SEC enforcement / litigation releases: listing sweep + full text | `enforcement/releases.jsonl`, `enforcement/docs/LR-####.txt` |

Every pipeline is also an importable module (`secdata.filings`,
`secdata.xbrl`, …) sharing one throttled, disk-cached HTTP client
(`secdata.client`) and one ticker→CIK resolver (`secdata.ticker`).

What else does SEC disclose per ticker? [docs/SEC_DATA_COVERAGE.md](docs/SEC_DATA_COVERAGE.md)
is a live-verified audit of the full EDGAR surface — the highest-value gaps
(Schedule 13D/G stakes, Forms 3/5/144, the DERA bulk data library) and the
traps (`SC 13D` facet renamed to `SCHEDULE 13D`, EFTS silent 10 000 ceiling,
dead foia paths).

## Install

```bash
git clone https://github.com/FrankChen-CLC/SEC_ticker_crawler.git secdata
cd secdata
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Python ≥ 3.9. The only runtime dependency is `requests` (a stdlib `urllib`
fallback keeps the client working without it).

## Configuration (required)

SEC's Fair Access policy requires every request to carry a User-Agent with
your contact information:

```bash
export SEC_EDGAR_UA="Your Name you@example.com"     # required
```

Optional environment knobs (see [.env.example](.env.example)):

| Variable | Default | Meaning |
|---|---|---|
| `SEC_EDGAR_SLEEP` | `0.15` | seconds between uncached requests (global throttle; keep × workers < 10 req/s) |
| `SEC_EDGAR_TIMEOUT` | `60` | per-request timeout |
| `SEC_EDGAR_RETRIES` | `4` | attempts on transient errors (429/503 back off exponentially) |
| `SEC_EDGAR_CACHE` | `~/.cache/secdata` | response cache dir (filings cached forever — they're immutable) |
| `SEC_EDGAR_OUT` | `out` | output root for every pipeline |
| `SEC_EDGAR_OFFLINE` | `0` | `1` = serve from cache only, never hit the network |
| `SEC_EDGAR_OVERRIDES` | – | JSON file mapping symbol→CIK for tickers SEC's map omits |
| `SEC_PROXY` | – | optional http(s) proxy if your hosting IP is rate-limited by SEC |

## Quick start

```bash
# AAPL recent 8-Ks (index + primary docs)
secdata filings AAPL --forms 8-K --include-amendments --docs primary

# 10-K/10-Q/20-F for your universe: daily incremental, index-first
secdata reports --days 3 --universe my_symbols.txt

# XBRL metrics for one company, or the daily cron-style refresh
secdata xbrl AAPL
secdata xbrl --all --daily

# Pre-XBRL history (2001-2006 filings), download + deterministic extraction
secdata prexbrl AAPL --before 2007-01-01

# 13F: parse a known filer, then compare two institutions
secdata holdings download --cik 1067983 --forms 13F-HR
secdata holdings compare --between 1067983 1067364

# Form 4: yesterday's insider trades, or bulk history
secdata insider --days-back 1 --resume
secdata insider --bulk --start-date 2024-01-01

# 8-K event index (items → rows)
secdata events AAPL --document-scope press

# DEF 14A executive intelligence for one company
secdata proxy AAPL

# New S-1s / F-1s since the last run (cron-friendly, exit-code aware)
secdata preipo --days 3

# Enforcement releases with full texts
secdata enforcement --details
```

Re-runs are cheap: responses land in the disk cache, done-headers live in
`out/<pipeline>/checkpoint/`, and every pipeline resumes from where it
stopped. Add `--offline` to replay against the cache with zero network.

## Output conventions

```
out/
├── <pipeline>/
│   ├── *.jsonl                # one row-family per file, append/dedupe safe
│   ├── docs/…                 # raw filings mirroring EDGAR archive paths
│   └── checkpoint/            # done.txt, failed.txt, state.json (resumable)
```

Rows use the same field names as the production pipelines (e.g. Form 4
trades carry `transaction_code`, `primary_classification`,
`shares_owned_after`, `plan_status`, `evidence`…), so downstream code ported
from the original stack keeps working — minus the MySQL layer.

## Being polite to SEC

- one global throttle governs all threads (default 0.15 s ⇒ ≤ ~7 req/s);
- every response is cached forever unless you ask for freshness;
- 429/503 trigger exponential backoff; tiny HTTP-200 error pages are never
  cached;
- bulk modes shard work but share the throttle — if you raise `--workers`,
  raise `SEC_EDGAR_SLEEP` with them.

EDGAR data is public; `secdata` redistributes nothing — it stores what you
fetch locally. Review [SEC's disclaimers](https://www.sec.gov/edgar/searchedgar/accessing-edgar-data)
before republishing documents you download.

## Development

```bash
pip install -e ".[test]"
pytest -q              # 200+ offline tests — no network needed
```

[DESIGN.md](DESIGN.md) documents the module contract (each pipeline =
`COMMAND / HELP / add_arguments / run`) and the porting rules used to strip
infrastructure while preserving pipeline semantics. Tests are hermetic:
synthetic SEC fixtures + fake clients.

## License

MIT — see [LICENSE](LICENSE).
