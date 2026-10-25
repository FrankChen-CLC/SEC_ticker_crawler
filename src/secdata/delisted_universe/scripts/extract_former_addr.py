#!/usr/bin/env python3
"""extract_former_addr.py — 从 SEC submissions.zip 二次抽取 formerNames / addresses / 全量 CIK 名录

用法（在 .39 上，脚本放 scripts/，数据和 zip 各在别处）：
  python3 -I extract_former_addr.py <submissions.zip> <delist_universe.csv> <outdir>

产出（写 outdir）：
  former_names.csv   cik,former_name,from_date,to_date          只含 universe 内 48,490 CIK
  addresses.csv      cik,street1,street2,city,state,zip          同上，business 优先否则 mailing
  all_ciks.csv       cik,name,entity_type,sic,state_inc,last_10k,business_street1,business_zip
                                                              全量 99 万 CIK（B1/B2 匹配底座）
  all_addr.csv       cik,kind,street1,street2,city,state,zip,country  全量所有地址
  all_former_names.csv cik,former_name,from_date,to_date         全量 formerNames（B1 反查底座）

只读 zip 内主档 CIK##########.json，跳过 CIK##########-submissions-NNN.json（附加申报文件）。
"""
import csv
import json
import os
import sys
import time
import zipfile

MAIN_PREFIX = "CIK"


def is_main_entry(n):
    if not n.startswith(MAIN_PREFIX) or not n.endswith(".json"):
        return False
    base = n[3:-5]
    return len(base) == 10 and base.isdigit()


def dt(s):
    """2020-01-02T05:00:00.000Z -> 2020-01-02"""
    if not s:
        return ""
    return str(s)[:10]


def addr_fields(a):
    if not isinstance(a, dict):
        return ("",) * 7
    st = (a.get("street1") or "").strip()
    st2 = (a.get("street2") or "").strip()
    city = (a.get("city") or "").strip()
    state = (a.get("stateOrCountry") or "").strip()
    zipc = (a.get("zipCode") or "").strip()
    country = (a.get("countryCode") or a.get("country") or "").strip()
    return st, st2, city, state, zipc, country, (a.get("isForeignLocation") or 0)


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    zpath, uni_csv, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(outdir, exist_ok=True)

    uni = set()
    for r in csv.DictReader(open(uni_csv)):
        c = (r.get("cik") or "").strip().lstrip("0")
        if c:
            uni.add(int(c))
    print(f"universe CIK: {len(uni):,}", flush=True)

    f_fn = open(os.path.join(outdir, "former_names.csv"), "w", newline="")
    w_fn = csv.writer(f_fn)
    w_fn.writerow(["cik", "former_name", "from_date", "to_date"])
    f_ad = open(os.path.join(outdir, "addresses.csv"), "w", newline="")
    w_ad = csv.writer(f_ad)
    w_ad.writerow(["cik", "street1", "street2", "city", "state", "zip"])
    f_ac = open(os.path.join(outdir, "all_ciks.csv"), "w", newline="")
    w_ac = csv.writer(f_ac)
    w_ac.writerow(["cik", "name", "entity_type", "sic", "state_inc", "last_10k",
                   "business_street1", "business_zip"])
    f_aa = open(os.path.join(outdir, "all_addr.csv"), "w", newline="")
    w_aa = csv.writer(f_aa)
    w_aa.writerow(["cik", "kind", "street1", "street2", "city", "state", "zip", "country"])
    f_af = open(os.path.join(outdir, "all_former_names.csv"), "w", newline="")
    w_af = csv.writer(f_af)
    w_af.writerow(["cik", "former_name", "from_date", "to_date"])

    t0 = time.time()
    n_ent = n_cik = n_bad = 0
    n_fn = n_ad = 0
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            n = info.filename
            if not is_main_entry(n):
                continue
            n_ent += 1
            try:
                with zf.open(info) as fh:
                    d = json.load(fh)
            except Exception as e:
                n_bad += 1
                if n_bad <= 20:
                    print(f"  parse fail {n}: {e}", flush=True)
                continue
            cik_norm = int(str(d.get("cik") or n[3:13]).lstrip("0") or 0)
            n_cik += 1
            in_uni = cik_norm in uni
            name = (d.get("name") or "").strip()

            addrs = d.get("addresses") or {}
            biz = addrs.get("business") if isinstance(addrs, dict) else None
            mail = addrs.get("mailing") if isinstance(addrs, dict) else None
            seen_ad = set()
            for kind, a in (("business", biz), ("mailing", mail)):
                if not isinstance(a, dict):
                    continue
                af = addr_fields(a)
                key = af[:6]
                if key in seen_ad or not af[0]:
                    continue
                seen_ad.add(key)
                w_aa.writerow([cik_norm, kind, af[0], af[1], af[2], af[3], af[4], af[5]])
                if in_uni:
                    w_ad.writerow([cik_norm, af[0], af[1], af[2], af[3], af[4]])
                    n_ad += 1
            bfields = addr_fields(biz) if isinstance(biz, dict) else ("",) * 7

            # last 10-K date from filings.recent (up to 1000 recent filings)
            last10k = ""
            try:
                rec = (d.get("filings") or {}).get("recent") or {}
                forms = rec.get("form") or []
                dates = rec.get("filingDate") or []
                for fm, dtv in zip(forms, dates):
                    if isinstance(fm, str) and fm.startswith("10-K") and dtv and str(dtv) > last10k:
                        last10k = str(dtv)
            except Exception:
                last10k = ""

            w_ac.writerow([cik_norm, name, d.get("entityType", ""), d.get("sic", ""),
                           d.get("stateOfIncorporation", ""), last10k, bfields[0], bfields[4]])

            for fn in (d.get("formerNames") or []):
                if not isinstance(fn, dict):
                    continue
                nm = (fn.get("name") or "").strip()
                if not nm:
                    continue
                w_af.writerow([cik_norm, nm, dt(fn.get("from")), dt(fn.get("to"))])
                if in_uni:
                    w_fn.writerow([cik_norm, nm, dt(fn.get("from")), dt(fn.get("to"))])
                    n_fn += 1

            if n_ent % 100000 == 0:
                el = time.time() - t0
                print(f"  {n_ent:,} entries, {n_cik:,} cik, former={n_fn:,}, addr={n_ad:,}, "
                      f"bad={n_bad}, {el:.0f}s", flush=True)

    for fh in (f_fn, f_ad, f_ac, f_aa, f_af):
        fh.close()
    el = time.time() - t0
    print(f"DONE entries={n_ent:,} cik={n_cik:,} bad={n_bad} former_uni={n_fn:,} "
          f"addr_uni={n_ad:,} {el:.0f}s", flush=True)


if __name__ == "__main__":
    main()
