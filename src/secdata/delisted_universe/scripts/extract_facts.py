#!/usr/bin/env python3
"""Extract filtered us-gaap ANNUAL facts from SEC companyfacts JSON tree.

stdlib-only; run with python3 -I (isolated; archive tree is untrusted data).
Usage: python3 -I extract_facts.py <companyfacts_dir> <fundamentals_csv> <coverage_csv> <failed_txt>

Structure (2026-10-25 verified): facts["us-gaap"][tag]["units"][unit] = [obs],
obs = {accn, end, filed, form, fp, fy, start, val, (frame)}.
Quirks handled:
  - fy/fp describe the FILING, not the fact: a 10-K carries 2-3 comparative
    years under one fy -> dedup by (tag, end, unit) keeping latest `filed`
    (restatement semantics).
  - Selected/quarterly data tables inside 10-Ks tag quarterly facts with
    fp=FY: duration tags require (end - start) >= 300 days (applied BEFORE
    dedup so a late refiled Q4 fact can't beat the annual one); instant
    tags require end within +/-10 days-of-year of the company's annual
    period ends.
  - date field is `filed` (fallback `filing_date`).
  - output fy column = calendar year of `end`.
"""
import csv
import json
import os
import sys
import time
from datetime import date

DURATION_TAGS = {
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "NetIncomeLoss",
}
INSTANT_TAGS = {
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
}
TAGS = DURATION_TAGS | INSTANT_TAGS
FORMS = {"10-K", "10-K/A", "20-F", "20-F/A"}


def pdate(s):
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def process_file(path):
    """Return (cik, [row, ...]) for one CIK json; raises on parse error."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    cik = data.get("cik")
    if cik in (None, ""):
        cik = os.path.basename(path)[3:13]
    cik = int(cik)
    gaap = data.get("facts", {}).get("us-gaap")
    if not isinstance(gaap, dict):
        return cik, []
    best = {}  # (tag, end_d, unit) -> (filed, obs) latest annual-only obs
    annual_doys = set()
    for tag, tdata in gaap.items():
        if tag not in TAGS or not isinstance(tdata, dict):
            continue
        units = tdata.get("units")
        if not isinstance(units, dict):
            continue  # observations live under units[unit] list
        is_dur = tag in DURATION_TAGS
        for unit, obs in units.items():
            if not isinstance(obs, list):
                continue
            for o in obs:
                if not isinstance(o, dict):
                    continue
                if o.get("form") not in FORMS:
                    continue
                if o.get("fp") not in ("FY", "", None):
                    continue
                fd = o.get("filed") or o.get("filing_date") or ""
                if o.get("val") is None:
                    continue
                d_end = pdate(o.get("end"))
                if d_end is None:
                    continue
                if is_dur:
                    d_st = pdate(o.get("start"))
                    if d_st is None or (d_end - d_st).days < 300:
                        continue  # quarterly/segment-span fact in a 10-K
                    annual_doys.add(d_end.timetuple().tm_yday)
                key = (tag, d_end, unit)
                prev = best.get(key)
                if prev is None or fd > prev[0]:
                    best[key] = (fd, o)
    rows = []
    for (tag, d_end, unit), (fd, o) in best.items():
        if tag in INSTANT_TAGS and annual_doys:
            doy = d_end.timetuple().tm_yday
            if min(min(abs(doy - a), 365 - abs(doy - a))
                   for a in annual_doys) > 10:
                continue  # quarterly balance-sheet date, not an FY end
        rows.append((
            cik, d_end.year, d_end.isoformat(), tag, o["val"], unit,
            o["form"], o.get("accn", ""), fd,
        ))
    rows.sort(key=lambda r: (r[1], r[3]))
    return cik, rows


def main():
    if len(sys.argv) != 5:
        print(__doc__)
        sys.exit(2)
    src, out_csv, cov_csv, fail_txt = sys.argv[1:5]
    files = sorted(f for f in os.listdir(src)
                   if f.startswith("CIK") and f.endswith(".json"))
    total = len(files)
    print("files=%d start=%s" % (total, time.strftime("%F %T")), flush=True)
    t0 = time.time()
    parsed = 0
    withdata = 0
    total_rows = 0
    fails = []
    with open(out_csv, "w", newline="") as f1, open(cov_csv, "w", newline="") as f2:
        w1 = csv.writer(f1)
        w2 = csv.writer(f2)
        w1.writerow(["cik", "fy", "period_end", "tag", "value", "unit",
                     "form", "accn", "filing_date"])
        w2.writerow(["cik", "n_obs", "fy_min", "fy_max"])
        for i, fn in enumerate(files, 1):
            try:
                cik, rows = process_file(os.path.join(src, fn))
                parsed += 1
            except Exception as e:
                fails.append("%s\t%s: %s" % (fn, type(e).__name__, e))
                rows = []
            if rows:
                withdata += 1
                total_rows += len(rows)
                for r in rows:
                    w1.writerow(r)
                fys = [r[1] for r in rows]
                w2.writerow([cik, len(rows), min(fys), max(fys)])
            if i % 1000 == 0 or i == total:
                el = time.time() - t0
                print("%d/%d parsed=%d withdata=%d rows=%d fails=%d "
                      "elapsed=%.0fs eta=%.0fs"
                      % (i, total, parsed, withdata, total_rows, len(fails),
                         el, el / i * (total - i)), flush=True)
    with open(fail_txt, "w") as f3:
        f3.write("\n".join(fails) + ("\n" if fails else ""))
    print("DONE parsed=%d withdata=%d rows=%d fails=%d total=%.0fs"
          % (parsed, withdata, total_rows, len(fails), time.time() - t0),
          flush=True)


if __name__ == "__main__":
    main()
