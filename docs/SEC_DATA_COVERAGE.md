# SEC EDGAR data coverage audit

*Audited 2026-10-17 against live EDGAR endpoints (two independent probe
rounds + conflict re-verification, ~380 uncached requests; AAPL CIK 320193,
2026 IPOs, 13D/G filers, TCGX, Vanguard funds). Every "works" claim below is
backed by a real HTTP 200 and parsed record; every "dead" claim by a
403/404/zero-facet you can re-run yourself.*

## Bottom line

**No — the ten shipped pipelines are not everything SEC discloses per
ticker**, but they cover most of the document corpus. The highest-value gaps,
all cheap because the discovery metadata rides endpoints secdata already
uses:

1. **Schedule 13D/13G** — ≥5% ownership stakes and activist entries, with
   amendment chains that form a stake trajectory. ⚠️ The EFTS token was
   renamed mid-2024: `forms=SCHEDULE 13D`, **not** `SC 13D` (the old token
   silently returns 0).
2. **Forms 3, 5 and Form 144** — completing the Section 16 lifecycle with
   the existing `insider` machinery: Forms 3/5 share Form 4's
   `ownershipDocument` XML schema verbatim, and Form 144's `primary_doc.xml`
   is fully structured (person, relationship, broker, units, market value —
   *intent* to sell, ahead of the Form 4 *fact*).
3. **The DERA "Data Library"** (`sec.gov/data-research/sec-markets-data/*`)
   — a bulk universe most crawlers miss: quarterly Form 3/4/5 CSVs back to
   2006, Form D since 2008, 13F three-month windows, N-CEN, Reg A, monthly
   XBRL notes — one download per period instead of millions of per-filer
   requests.

Second tier: exhibit-level archiving (EX-10/21/23, one request per filing you
already index), the Frames API and Financial Statement Data Sets for
cross-company and dimensional XBRL, 424B4/424B2 pricing docs, NT 10-K/Q
delinquency watch.

## S tier — build next

| dataset | endpoint | live evidence |
|---|---|---|
| **13D / 13G + amendments** | EFTS `forms=SCHEDULE 13D` (and `SCHEDULE 13G`, `/A` variants); issuer docs `Archives/edgar/data/{cik}/{adsh_nodash}/` | 30-day facets (re-verified): 13D 400, 13G 763, 13D/A 300, 13G/A 435. Sample 13D/A doc (13 KB, X0202 XML): CUSIP, aggregate value, **45.3% class stake**, itemized source of funds, all prior filing dates. Old `forms=SC 13D` → 0 (renamed mid-2024; DERA type counts show the switch). |
| **Forms 3 & 5** | discovery **free**: issuer `submissions/CIK##########.json` already lists forms `3`,`3/A`,`5` with dates; doc join via EFTS `forms=3&q=`→ person-CIK archive folder (`form3.xml`) — do NOT fetch under the accession-prefix CIK (agent folder), that 404s | AAPL submissions recent: 12× Form 3, 1× Form 5. Market 30 d: 3→1 142, 5→22 (DERA 2025: 3≈12.2 k, 5≈1.2 k/yr). Form 3 doc (12 KB) parses with the existing `insider` ownershipDocument parser (`documentType 3`, `noSecuritiesOwned`, holding tables). |
| **Form 144** | EFTS `forms=144`; doc `…/{acn}/primary_doc.xml` (edgar/ownership schema — **no text-mining needed**) | 30 d: 1 705 (≈34.4 k/yr per DERA). AAPL-window sample re-parsed first-hand (3 287 B): `issuerCik 0000320193`, `noOfUnitsSold`, `aggregateMarketValue` present; broker block only when the filer fills it. Forward-looking insider supply signal. |
| **DERA bulk: insider 345 CSV** | page `data-research/sec-markets-data/insider-transactions-data-sets` → `https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{yyyy}q{n}_form345.zip` | HEAD re-verified 2026-10-17: `2026q3_form345.zip` → 200, 8 660 776 B (quarters back to 2006Q1). Whole-market Section 16 in ~4 downloads/quarter. |

## A tier — high value, modest cost

| dataset | endpoint | live evidence |
|---|---|---|
| **Form D** (private placements) | EFTS `forms=D` + `primary_doc.xml`; bulk `{yyyy}q{n}_d.zip` under `/files/structureddata/data/form-d-data-sets/` (listed on `…/form-d-data-sets` page) | 30 d: 6 072 filings. Structured doc fields verified: `totalOfferingAmount`, `totalAmountSold`, `dateOfFirstSale`, `salesCommissions`, exemption items, related persons. 2026q2/q3 zips confirmed on the listing page today. Feeds `preipo` with private-stage history. |
| **13F bulk windows** | `/files/datastandardsinnovation/data/form-13f-data-sets/{window}_form13f.zip` (page `…/form-13f-data-sets`) | Latest `01jun2026-31aug2026_form13f.zip` (≈2-month publication lag). One download/quarter vs ~30 k per-filer requests. (Old foia `13fbulk.htm` path is 404 — the DERA page is the living door.) |
| **N-PORT bulk** | `/files/edgar/downloads/form-nport/holdings/structured/period/{yyyy}/{mm}/NPORT-P-All-Period-{yyyymm}-Holdings-Data.part-*.zip` on `files.sec.gov` (idx next to it) | Verified earlier 2026-10-17: `…-00001.zip` → HTTP 200, 57 962 833 bytes; 45 parts for 2026-08. Note: `files.sec.gov` reachability is network-dependent (some networks hang); fall back to per-filer `holdings` then. |
| **Financial Statement Data Sets** (dimensional XBRL) | `files/data/financial-statement-data-sets/sfdatasetdirectory.txt` → quarterly zips 2009q1→2026q3 | Directory 200, ~245 quarter entries. `num.txt` carries segment/dimension facts that `companyfacts` flattens — closes the segment-data gap. |
| **XBRL frames** (cross-market by tag-quarter) | `data.sec.gov/api/xbrl/frames/{us-gaap\|dei}/{Tag}/{UOM}/{CYyQn[\|I]}.json` — **the UOM path segment is mandatory** (no-UOM → 404, re-verified) | `Assets/USD/CY2024Q4I` → 200, 6 274 rows (response keys: taxonomy/tag/ccp/uom/label/data; rows: accn/cik/entityName/end/val). One request buys a whole-market cross-section for factor backfills. |
| **8-K/10-K exhibit sets** | per-filing `…/{acn}/index.json` → EX-21/EX-23/EX-31/EX-10 files (already resolved by `reports`, stored only primary) | AAPL FY2025 10-K index: `a10-kexhibit21…htm`, `…23…htm`, EX-31/32/41 + R-file rendering set. Subsidiary trees, material contracts, auditor consents — +1 req/filing. |
| **424B4/424B5/424B2** (IPO/shelf/ATM pricing) | EFTS `forms=424B4` etc. → doc (no XBRL in the 424B4 folder — pricing needs text/table parse of the .htm) | 30 d: 424B4 29, 424B5 224, 424B2 hits the 10 k EFTS cap (DERA: ≈98.9 k/yr ATM flow). Machine-readable offering size lives elsewhere: `ffd:TtlOfferingAmt`/`NetFeeAmt` fee-table facts in the company's **S-1 companyfacts** (verified on TCGX CIK 2149916: $100 000 000 offered). |
| **N-CEN / N-CSR / N-CSRS** | EFTS + `primary_doc.xml` (X0505 edgar/ncen schema, fully structured) | 30 d: N-CEN 175 (one doc = 60 KB structured), N-CSR 186, N-CSRS 79. N-CEN quarterly bulk zips also live under DERA. Only if fund-level holdings matter to you. |

## B tier — mostly parsing/UX wins on data you already fetch

- **Restatement/conflicting-fact detection — zero new requests**: AAPL's
  existing `companyfacts` download contains **1 701 (tag, end-date) pairs with
  more than one filed value** (e.g. `AccruedLiabilitiesCurrent` 3.719 B →
  4.224 B via 10-K/A). A restatement-timeline feature is a parser change in
  `xbrl`, not a fetch. (No "restatements API" exists — `data.sec.gov/api/xbrl/restatements` → 404.)
- **dei time series**: `companyfacts` carries **exactly two** dei tags per
  company — `EntityPublicFloat`, `EntityCommonStockSharesOutstanding`
  (verified on AAPL; auditor names and EGC/shell flags are **not** in
  companyfacts — they live in filing cover-page/R-file XBRL). Use the
  companyconcept API, 1 req per tag, for float/EGC graduation series.
- **Delinquency watch**: EFTS `forms=NT 10-K` (30/30 d), `NT 10-Q` (11/30 d) —
  earliest hard earnings-delay flag; piggybacks the `filings` scan.
- **Lifecycle/delisting detector**: `15-15D` (17/30 d; `15-12`, `25-NSE` also
  live), deregistrations + NYSE delisting notices = "quietly gone" signal.
- **Person-centric insider search** (1 req): EFTS `q="TIMOTHY D COOK"&forms=4`
  returns hits whose `ciks` carry BOTH the person and issuer CIKs +
  `display_names` facets — executive career tracking on existing machinery.
- **M&A/tender stream**: `SC TO-I`, `SC 14D9` tokens (DERA: 577 + 55/yr) —
  deal completions beyond 8-K items. Low volume, high event value.

## Dead ends — verified 403/404/silent-zero, don't build on these

| thing | evidence (2026-10-17) |
|---|---|
| Old 13D/G EFTS tokens `SC 13D` / `SC 13G` | facet 0 — renamed to `SCHEDULE …` mid-2024 (silent-zero trap) |
| `foia/docs/13fbulk.htm`, `foia/docs/foia-data.htm`, `data.sec.gov/foia/13f/` | 404 — 13F bulk lives at the DERA form-13f-data-sets page |
| `Archives/edgar/data/0000000000/`, `ftp.sec.gov/formd/`, `files/formd.csv` | 404/403 — Form D bulk is the DERA quarterly `_d.zip` files |
| `data.sec.gov/api/xbrl/restatements/` | 404 (detect restatements offline from companyfacts instead) |
| Frames without the `{UOM}` segment; entity-scoped frames paths | 404 (re-verified) |
| `dera/data/financial-statements-notes-data` (old path), `dera/data/` root | 404 — notes moved to `/data-research/financial-statement-notes-data-sets`, monthly `{yyyy}_{mm}_notes.zip` (53–314 MB) — itself an A-tier corpus for text research |
| EFTS facets `13H`, `SH`, `CRS`, `N-PAN`, `2-A`, `15-12B`, `15-15E` | 0 — tokens dead or renamed (Reg A 2-A genuinely discontinued) |
| XBRL facts for N-PORT/N-CEN/144/SD/13H in companyfacts | none — those forms need document parsing (their `primary_doc.xml` IS structured, so parsing is cheap) |
| Comment letters (`CORRESP`) stream | indexed letters **stale since 2026-08-31** (0 in last 30 d; DERA counts collapsing) — do not build on this feed yet |
| EFTS deep paging | `hits.total` and paging cap at **10 000, silently returning 0 beyond** — anything bigger must use daily-index or DERA bulk, never EFTS |

## Outside SEC EDGAR (not secdata's job)

Short interest and TRACE bond prints live under **FINRA**, options volume
under **OCC**, European transparency filings under local regulators. SEC
hosts none of them — the gap there is agency coverage, not pipeline coverage.

## Reproducing this audit

```bash
export SEC_EDGAR_UA="Your Name you@example.com"
python - <<'EOF'
from secdata import client
c = client.default_client()
j = c.get_json("https://data.sec.gov/submissions/CIK0000320193.json")
print(sorted(set(j["filings"]["recent"]["form"])))   # what one company files
EOF
```

Every answer above is either a `form` value in that array, a file under
`Archives/edgar/data/{cik}/{adsh_nodash}/`, or a listing under
`sec.gov/data-research/sec-markets-data/`. That triple — submissions list →
archive dir → DERA library — is the whole public surface SEC offers per
ticker.
