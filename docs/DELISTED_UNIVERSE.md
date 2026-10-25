# Historical ticker and delisting universe

`secdata delisted` ports the research workflow for finding former U.S. public
company tickers. It combines SEC full indexes and submissions with historical
SEC ticker snapshots from the Internet Archive and Nasdaq symbol directories.
The output is a set of CSV files keyed by CIK. The workflow is resumable and
keeps downloads and generated data outside the installed Python package.

## Setup

Set `SEC_EDGAR_UA` to your own name and email, then choose a working directory.
The default is `<SEC_EDGAR_OUT or ./out>/delisted_universe`; `--data-dir` can
point at an existing `delisted_universe` data directory. For example:

```bash
export SEC_EDGAR_UA="Your Name you@example.com"
secdata delisted status --data-dir ./delisted_data
secdata delisted run --data-dir ./delisted_data
secdata delisted check --data-dir ./delisted_data
```

Before the full run, place the SEC `submissions.zip` file at
`<data-dir>/subs/submissions.zip`. Extract SEC `companyfacts.zip` so its
`CIK##########.json` files are in `<data-dir>/facts/`. The SEC publishes
both [bulk archive links](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
These archives and the resulting CSVs are intentionally excluded from Git.
The full workflow can need tens of gigabytes and many hours of network access.
If the SEC blocks `/Archives` from your IP, configure `--proxy` or `SEC_PROXY`.

`secdata delisted list` shows the 20 stages. `run --dry-run` previews them,
`run --from B1` resumes from a stage, and `run --only A7,A8` reruns named
stages. `status` reads per-stage state under `<data-dir>/out/.state`.
`check` verifies required columns and CIK relationships using the current
data instead of fixed historical row counts.

## Key outputs

| File under `<data-dir>/out` | Purpose |
|---|---|
| `delist_universe.csv` | All CIKs with annual-report activity and a filing-silence delisting proxy |
| `master_universe.csv` | CIK, best ticker, ticker observation window, listing evidence and delisting classification |
| `delist_companies.csv` | Delisted candidate companies after filtering clear non-company entities |
| `tickers_all_merged.csv` | Every recovered CIK/ticker pair with first and last observed dates and source |
| `filings_events.csv` | Form 15, merger and control-change evidence from SEC submissions |

The original research build on 2026-10-25 produced 48,490 master CIKs,
29,839 filtered delisted candidates, and 34,638 CIK/ticker pairs. These are
examples, not validation targets; refreshed SEC data will change them.

## Interpretation

`is_delisted` is a proxy derived from annual-report silence. Form 25 can
apply to a particular security rather than the company's common shares.
`delist_proxy_date` is the last annual report date, **not** an exchange
delisting date. `ticker_first_seen` and `ticker_last_seen` bound observations;
they are not guaranteed listing and delisting dates. Ticker symbols can be
reused, so use CIK plus a date window for historical joins.

`delist_nature` separates merger evidence, going private evidence,
deregistration, and companies that stopped filing. These are SEC evidence
classes, not authoritative delisting reason codes. Early-period ticker
coverage is limited by the start dates of archived symbol sources. The
workflow does not supply historical prices or delisting returns.
