"""Assemble the per-symbol executive-intelligence payload and land it as
JSONL files under ``out/proxy/`` — the open-source replacement for the
production ``data/{SYM}.json`` files + MySQL ingest.

Payload assembly semantics are preserved from the production report module:
- one bad filing must not void the other modules (per-module ``_try``);
- ``audit`` carries parse-yield rates, ``module_failures`` names the modules
  that raised ({} = every SEC module parsed clean).

Entity families (one file each, ``emit()``):
  executives.jsonl  named-exec profiles (latest SCT year + PEO flag)
  comp.jsonl        sct | pvp | pvp_alignment | metric_weights | related_party
  promises.jsonl    guidance promises + scorecard
  events.jsonl      8-K item events, tenure stats, EX-97, 10-K quality, 10b5-1
  capital.jsonl     per-period capital-allocation values + derived ratios
  insiders.jsonl    Form 3/4/5 trades, latest positions, activity summary
  builds.jsonl      per-symbol build audit (mirrors the production intel row)

A rebuild of the same symbol REPLACES that symbol's rows in every family file
(upsert semantics; see ``secdata.proxy._merge``).
"""
import time

from .. import ticker as _ticker
from . import capital, events, fetch, insiders, promises, proxy14a


def pvp_alignment(pvp):
    """Year-over-year sign agreement between CEO comp-actually-paid and TSR."""
    rows, prev = [], None
    for y in sorted(pvp.get("years") or [], key=lambda r: r["fy"]):
        if prev:
            cap = y.get("peo_comp_actually_paid")
            pcap = prev.get("peo_comp_actually_paid")
            tsr = y.get("tsr_index")
            ptsr = prev.get("tsr_index")
            if cap is not None and pcap is not None and tsr is not None \
                    and ptsr is not None:
                dc, dt = cap - pcap, tsr - ptsr
                rows.append({
                    "fy": y["fy"],
                    "cap_delta": round(dc, 0),
                    "tsr_delta": round(dt, 0),
                    "peer_tsr_delta": round((y.get("peer_tsr_index") or 0) -
                                            (prev.get("peer_tsr_index") or 0), 0),
                    "aligned": (dc >= 0) == (dt >= 0),
                })
        prev = y
    return rows


def latest_comp_mix(sct):
    """Most recent fiscal year per named exec, with comp mix percentages."""
    by = {}
    for r in sct or []:
        key = (r.get("name") or "").strip()
        if not key:
            continue
        if key not in by or r["year"] > by[key]["year"]:
            by[key] = r
    return sorted(by.values(), key=lambda r: -(r.get("total") or 0))


def audit_of(proxy, ins, ev, cap, prom):
    """Per-module SEC parsing rates, computed from the run already done —
    every entry has a measurable denominator where one exists. Rates are
    parse-yield rates, not 'SEC had data' rates (absence is reported too)."""
    def pct(part, whole):
        return round(100.0 * part / whole, 1) if whole else None

    sct = proxy.get("sct") or []
    pvp = proxy.get("pvp") or {}
    yrs = pvp.get("years") or []
    rp = proxy.get("related_party")
    series = cap.get("series") or {}
    keys = ["buybacks", "dividends_paid", "ma_spend", "capex", "rnd", "sbc"]
    cap_years = sorted({y for v in series.values() for y in v if isinstance(v, dict)})
    matched, scanned = prom.get("n_releases_matched", 0), \
        prom.get("n_releases_scanned", 0)
    sc = prom.get("scorecard") or {}
    forms = ins["n_forms"]
    ferr = len(ins.get("parse_errors_list") or ins.get("errors") or [])
    return {
        "def14a": {"found": bool(proxy.get("filing")),
                   "date": (proxy.get("filing") or {}).get("date")},
        "pvp_xbrl": {"years_parsed": len(yrs),
                     "peo_named": bool(pvp.get("peo_name")),
                     "rows_complete_pct": pct(
                         sum(1 for y in yrs
                             if y.get("peo_comp_actually_paid") is not None),
                         len(yrs))},
        "sct": {"rows_parsed": len(sct),
                "rows_with_total_pct": pct(
                    sum(1 for r in sct if r.get("total")), len(sct)),
                "executives": len({(r.get("name") or "").strip() for r in sct})},
        "metric_weights": {"tables_parsed": len(proxy.get("metric_weights") or [])},
        "related_party": {"section_found": bool(rp),
                          "amount_records": (rp or {}).get("n_amount_disclosures")},
        "form4_xml": {"forms_listed": forms, "forms_parse_errors": ferr,
                      "parse_rate_pct": pct(forms - ferr, forms),
                      "trades_parsed": len(ins.get("trades") or [])},
        "events": {"event_rows_parsed": len(ev.get("events") or []),
                   "clawback_ex97_on_file": bool(ev.get("clawback_ex97")),
                   "ten_k_quality_found": bool(ev.get("ten_k_quality")),
                   "plan_10b5_1_found": bool(ev.get("plan_10b5_1"))},
        "capital_xbrl": {"series_present": sum(1 for k in keys if series.get(k)),
                         "series_total": len(keys),
                         "fy_span": [cap_years[0][:4], cap_years[-1][:4]]
                         if cap_years else None},
        "promises": {"releases_matched": matched, "releases_harvested": scanned,
                     "harvest_rate_pct": pct(scanned, matched),
                     "quantified": sc.get("quantified", 0),
                     "qualitative": sc.get("qualitative", 0),
                     "bare_labels": sc.get("bare", 0),
                     "scored": sc.get("scored", 0),
                     "hit_rate_pct": round(sc["hit_rate"] * 100, 1)
                     if sc.get("hit_rate") is not None else None},
    }


def build(symbol, insider_since="2021-01-01", event_since="2019-01-01",
          promise_since="2022-01-01", overrides_path=None):
    cik = _ticker.cik_for(symbol, overrides_path=overrides_path)
    filings = fetch.recent_filings(cik)
    t0 = time.time()
    # One bad filing must not void the other modules of a healthy filer
    # (2026-10-10: a comma-int crash inside promises' range formatter marked
    # 100+ active large/mid caps "skip" across the whole universe build).
    failures = {}

    def _try(name, fn, default):
        try:
            return fn()
        except Exception as e:
            failures[name] = f"{type(e).__name__}: {e}"[:200]
            return default

    proxy = _try("proxy14a", lambda: proxy14a.build(cik, filings=filings), {})
    ins = _try("insiders",
               lambda: insiders.collect(cik, since=insider_since,
                                        filings=filings),
               {"n_forms": 0, "positions": [], "trades": [], "errors": []})
    ev = _try("events",
              lambda: events.build(cik, since=event_since, filings=filings), {})
    cap = _try("capital", lambda: capital.build(cik), {})
    prom = _try("promises", lambda: promises.build(cik, since=promise_since,
                                                   filings=filings), {})
    payload = {
        "symbol": symbol.upper(),
        "cik": cik,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "build_seconds": round(time.time() - t0, 1),
        "proxy": proxy,
        "insiders": {
            "n_forms": ins["n_forms"],
            "positions": ins["positions"],
            "activity_12m": insiders.classify_summary(ins["trades"]),
            "trades": ins["trades"][:150],
            "parse_errors": ins["errors"][:10],
        },
        "events": ev,
        "capital": cap,
        "promises": prom,
        "analysis": {
            "pvp_alignment": pvp_alignment((proxy or {}).get("pvp") or {}),
            "comp_latest": latest_comp_mix((proxy or {}).get("sct")),
            "pvp_measures": ((proxy or {}).get("pvp") or {}).get("measures", []),
        },
        "audit": audit_of(proxy or {}, ins, ev or {}, cap or {}, prom or {}),
        "module_failures": failures,   # {} = every SEC module parsed clean
    }
    return payload


# --------------------------------------------------------------------- emit
_SCT_KEYS = ("salary", "bonus", "stock_awards", "option_awards",
             "non_equity", "pension", "all_other")


def emit(p):
    """Split a build payload into entity-family rows (JSONL shapes).

    Returns {family_name: [row, ...]}; every row carries ``symbol`` so the
    caller can upsert per symbol when merging into the family files.
    """
    sym, cik = p["symbol"], p.get("cik")
    fam = {"executives": [], "comp": [], "promises": [], "events": [],
           "capital": [], "insiders": [], "builds": []}
    proxy = p.get("proxy") or {}
    pvp = proxy.get("pvp") or {}
    acc = (proxy.get("filing") or {}).get("accession")

    # -- executives (profiles) ------------------------------------------
    peo = pvp.get("peo_name")
    for r in p["analysis"].get("comp_latest") or []:
        fam["executives"].append({
            "symbol": sym, "cik": cik, "name": r.get("name"),
            "title": r.get("title"), "latest_fy": r.get("year"),
            "total": r.get("total"), "mix": r.get("mix"),
            "is_peo": bool(peo) and (r.get("name") or "").strip() == peo.strip(),
            "proxy_accession": acc,
        })
    if peo and not any(e["name"] == peo for e in fam["executives"]):
        fam["executives"].append({"symbol": sym, "cik": cik, "name": peo,
                                  "title": None, "latest_fy": None,
                                  "total": None, "mix": None, "is_peo": True,
                                  "source": "pvp", "proxy_accession": acc})

    # -- comp ------------------------------------------------------------
    for r in proxy.get("sct") or []:
        row = {"symbol": sym, "cik": cik, "kind": "sct",
               "proxy_accession": acc}
        for k in ("name", "title", "year", "total", "mix") + _SCT_KEYS:
            row[k] = r.get(k)
        fam["comp"].append(row)
    for y in pvp.get("years") or []:
        row = {"symbol": sym, "cik": cik, "kind": "pvp", "proxy_accession": acc}
        row.update(y)
        fam["comp"].append(row)
    for r in p["analysis"].get("pvp_alignment") or []:
        row = {"symbol": sym, "cik": cik, "kind": "pvp_alignment"}
        row.update(r)
        fam["comp"].append(row)
    for t in proxy.get("metric_weights") or []:
        fam["comp"].append({"symbol": sym, "cik": cik, "kind": "metric_weights",
                            "weights": t.get("weights"), "sum": t.get("sum"),
                            "proxy_accession": acc})
    rp = proxy.get("related_party")
    if rp:
        fam["comp"].append({"symbol": sym, "cik": cik, "kind": "related_party",
                            "records": rp.get("records"),
                            "total_usd": rp.get("total_usd"),
                            "n_amount_disclosures": rp.get("n_amount_disclosures"),
                            "review_policy_stated": rp.get("review_policy_stated"),
                            "proxy_accession": acc})

    # -- promises ---------------------------------------------------------
    prom = p.get("promises") or {}
    for pr in prom.get("promises") or []:
        # a promise dict carries its own "kind" (quantified/qualitative/bare)
        # -- keep "kind" as the row discriminator, stash the promise's under
        # "promise_kind" so neither shadows the other
        row = {"symbol": sym, "cik": cik, "kind": "promise"}
        for k, v in pr.items():
            row["promise_kind" if k == "kind" else k] = v
        fam["promises"].append(row)
    if prom:
        row = {"symbol": sym, "cik": cik, "kind": "scorecard",
               "n_releases_matched": prom.get("n_releases_matched"),
               "n_releases_scanned": prom.get("n_releases_scanned")}
        row.update(prom.get("scorecard") or {})
        fam["promises"].append(row)

    # -- events ------------------------------------------------------------
    ev = p.get("events") or {}
    for e in ev.get("events") or []:
        row = {"symbol": sym, "cik": cik, "kind": "event"}
        row.update(e)
        fam["events"].append(row)
    if ev.get("tenure"):
        row = {"symbol": sym, "cik": cik, "kind": "tenure"}
        row.update(ev["tenure"])
        fam["events"].append(row)
    fam["events"].append({"symbol": sym, "cik": cik, "kind": "clawback_ex97",
                          "hits": ev.get("clawback_ex97") or []})
    if ev.get("ten_k_quality"):
        row = {"symbol": sym, "cik": cik, "kind": "ten_k_quality"}
        row.update(ev["ten_k_quality"])
        fam["events"].append(row)
    if ev.get("plan_10b5_1"):
        row = {"symbol": sym, "cik": cik, "kind": "plan_10b5_1"}
        row.update(ev["plan_10b5_1"])
        fam["events"].append(row)

    # -- capital actions ------------------------------------------------------
    cap = p.get("capital") or {}
    series = cap.get("series") or {}
    ends = sorted({end for key, v in series.items()
                   if isinstance(v, dict) and not key.startswith("_")
                   for end in v})
    for end in ends:
        row = {"symbol": sym, "cik": cik, "end": end}
        for key, v in series.items():
            if key.startswith("_") or not isinstance(v, dict):
                continue
            hit = v.get(end)
            if hit:
                row[key] = hit.get("value")
        d = (cap.get("derived") or {}).get(end)
        if d:
            row.update({k: v for k, v in d.items() if k != "year"})
        fam["capital"].append(row)

    # -- insiders -----------------------------------------------------------
    ins = p.get("insiders") or {}
    for t in ins.get("trades") or []:
        # Form 4 trades carry their own "kind" (common/derivative) -- stash it
        # under "trade_kind" so "kind" stays the row discriminator
        row = {"symbol": sym, "cik": cik, "kind": "trade"}
        for k, v in t.items():
            row["trade_kind" if k == "kind" else k] = v
        fam["insiders"].append(row)
    for pos in ins.get("positions") or []:
        row = {"symbol": sym, "cik": cik, "kind": "position"}
        row.update(pos)
        fam["insiders"].append(row)
    fam["insiders"].append({"symbol": sym, "cik": cik, "kind": "summary",
                            "n_forms": ins.get("n_forms"),
                            "activity_12m": ins.get("activity_12m"),
                            "parse_errors": ins.get("parse_errors")})

    # -- build audit row -------------------------------------------------------
    fam["builds"].append({
        "symbol": sym, "cik": cik, "built_at": p.get("built_at"),
        "build_seconds": p.get("build_seconds"),
        "proxy_filing": (proxy.get("filing") or {}).get("date"),
        "module_failures": p.get("module_failures"),
        "audit": p.get("audit"),
    })
    return {k: v for k, v in fam.items() if v}


# --------------------------------------------------------------- markdown
def md(p):
    """Markdown digest of a payload (the production `--md` digest)."""
    L = []
    a = p["analysis"]
    L.append(f"# {p['symbol']}  (CIK {p['cik']})  built {p['built_at']} "
             f"in {p['build_seconds']}s")
    pr = p.get("proxy") or {}
    L.append(f"\n## DEF 14A  {pr['filing']['date'] if pr.get('filing') else '-'}  "
             f"{pr['filing']['accession'] if pr.get('filing') else ''}")
    pvp = pr.get("pvp") or {}
    if pvp.get("years"):
        L.append(f"\n### Pay vs Performance  (PEO: {pvp.get('peo_name')})")
        L.append("| FY | SCT total | Comp actually paid | NEO avg CAP | TSR idx | "
                 "Peer TSR | Net income | Co-measure |")
        L.append("|---|---|---|---|---|---|---|---|")
        for y in pvp["years"]:
            def f(v):
                if v is None:
                    return "-"
                return f"{v/1e6:,.1f}M" if abs(v) >= 1e4 else f"{v:,.0f}"
            L.append(f"| {y['fy']} | {f(y['peo_sct_total'])} | "
                     f"{f(y['peo_comp_actually_paid'])} | "
                     f"{f(y['neo_avg_comp_actually_paid'])} | "
                     f"{y['tsr_index'] or '-'} | {y['peer_tsr_index'] or '-'} | "
                     f"{f(y['net_income'])} | {y['company_selected_measure'] or '-'} |")
        L.append(f"\nmost-important measures (ecd:MeasureName): "
                 f"{', '.join(pvp.get('measures') or []) or '-'}")
    if a.get("pvp_alignment"):
        L.append("\n### CAP↔TSR alignment")
        for r in a["pvp_alignment"]:
            L.append(f"- FY{r['fy']}: ΔCAP {r['cap_delta']/1e6:+,.1f}M vs "
                     f"ΔTSR {r['tsr_delta']:+,.0f} (peer {r['peer_tsr_delta']:+,.0f}) "
                     f"→ {'ALIGNED' if r['aligned'] else 'MISALIGNED'}")
    if pr.get("sct"):
        L.append("\n### Summary Compensation Table (latest yr per exec)")
        for r in a["comp_latest"]:
            mix = r.get("mix") or {}
            tot = f"${r['total']:,.0f}" if r.get("total") else "n/a"
            L.append(f"- **{r['name']}** ({r['title'] or '-'}) FY{r['year']} "
                     f"total {tot}: " +
                     ", ".join(f"{k} {v:.0f}%" for k, v in mix.items() if v))
    if pr.get("metric_weights"):
        L.append("\n### Incentive metric weights (CD&A tables)")
        for t in pr["metric_weights"]:
            L.append("- " + ", ".join(f"{w['metric']} {w['weight_pct']:.0f}%"
                                      for w in t["weights"]))
    rp = pr.get("related_party")
    if rp:
        L.append(f"\n### Related-party transactions  "
                 f"(${rp['total_usd']/1e6:,.1f}M across "
                 f"{rp['n_amount_disclosures']} disclosures, policy stated: "
                 f"{'yes' if rp['review_policy_stated'] else 'no'})")
        for r in rp["records"][:6]:
            yr = f" FY{r['year']}" if r.get("year") else ""
            L.append(f"- ${r['amount_usd']/1e6:,.1f}M{yr}")
    ins = p.get("insiders") or {}
    L.append(f"\n## Insider activity  ({ins.get('n_forms', 0)} forms 3/4/5 parsed)")
    for k, v in sorted((ins.get("activity_12m") or {}).items(),
                       key=lambda kv: -abs(kv[1]["net_value"] or 0)):
        val = f"${v['net_value']/1e6:+,.2f}M" if v["net_value"] is not None else "-"
        L.append(f"- {k}: {v['count']} txn, net {v['net_shares']:,.0f} sh, {val}")
    if ins.get("positions"):
        L.append("\n### Latest reported positions")
        for pos in sorted(ins["positions"], key=lambda x: -(x["shares"] or 0))[:12]:
            L.append(f"- {pos['insider']} ({pos['title'] or '-'}): "
                     f"{pos['shares']:,.0f} {pos['security']} "
                     f"({'direct' if pos['direct'] else 'indirect'}) as of {pos['as_of']}")
    ev = p.get("events") or {}
    ten = ev.get("tenure") or {}
    L.append(f"\n## Governance events  (5.02 x{ten.get('n_502_events', 0)}, "
             f"CEO changes x{ten.get('ceo_change_events', 0)}, "
             f"CFO changes x{ten.get('cfo_change_events', 0)})")
    for e in (ev.get("events") or [])[:12]:
        det = ""
        if e.get("detail"):
            det = "; ".join(f"{d['who']} {d['verb']}" for d in e["detail"][:3])
        L.append(f"- {e['date']} item {e['item']} [{e['tag']}] {det}")
    L.append(f"\nclawback EX-97 on file: "
             f"{[h['date'] for h in ev.get('clawback_ex97') or []] or 'NO'}")
    q = ev.get("ten_k_quality") or {}
    if q:
        L.append(f"10-K {q.get('filing_date')}: material-weakness mentions "
                 f"{q.get('material_weakness_mentions')}, restatement mentions "
                 f"{q.get('restatement_mentions')}")
    pl = ev.get("plan_10b5_1")
    if pl:
        L.append(f"10b5-1 disclosure ({pl['filing']} {pl['date']}): "
                 f"{[e['verb'] for e in pl['adoption_events']] or 'section found, no adopt/terminate verbs'}")
        for e in pl["adoption_events"][:4]:
            L.append(f"  - {e['verb']} (disclosed in {e['form']} {e['filed']})")
    c = p.get("capital") or {}
    L.append("\n## Capital allocation ($M, FY)")
    keys = ["buybacks", "dividends_paid", "ma_spend", "capex", "rnd", "sbc"]
    years = sorted({k for key in (c.get("series") or {})
                    for k in (c.get("series") or {}).get(key, {})})[-6:]
    L.append("| FY | " + " | ".join(keys) + " | net sh chg % |")
    L.append("|---|" + "---|" * (len(keys) + 1))
    for yr in years:
        cells = []
        for key in keys:
            v = (c.get("series") or {}).get(key, {}).get(yr)
            cells.append(f"{v['value']/1e6:,.0f}" if v else "-")
        d = (c.get("derived") or {}).get(yr, {}).get("net_share_count_chg_pct")
        L.append(f"| {str(yr)[:4]} | " + " | ".join(cells) +
                 f" | {d if d is not None else '-'} |")
    pm = p.get("promises") or {}
    if pm:
        L.append(f"\n## Promise tracker  {pm.get('scorecard')}")
        for pr_ in (pm.get("promises") or [])[:10]:
            act = f", actual {pr_['actual']:,.0f}" if pr_.get("actual") else ""
            L.append(f"- [{pr_['status']}] {pr_['date']}: {pr_['summary']}{act}")
    au = p.get("audit") or {}
    if au:
        L.append("\n## Parsing audit (yield rates per module)")
        for mod, d in au.items():
            L.append(f"- {mod}: " + ", ".join(
                f"{k}={v}" for k, v in d.items() if v is not None))
    if p.get("module_failures"):
        L.append(f"\n## Module failures: {p['module_failures']}")
    return "\n".join(L)
