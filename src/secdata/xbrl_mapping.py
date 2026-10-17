"""Pure XBRL companyfacts -> metrics mapping for ``secdata xbrl``.

Ported verbatim (modulo MySQL/DB specifics) from the production
Sep 2026 full-rebuild + metric-importance lineage. Every parser rule here encodes a real SEC companyfacts quirk;
see the comments for *why* each threshold exists. No I/O: everything takes
plain dicts parsed from the companyfacts JSON and returns row dicts.

Mapping schema
--------------
An *extraction* (``extract_metrics``) is::

    {"ticker": str,
     "annual":    {metric_key: {period_end: value}},
     "quarterly": {metric_key: {period_end: value}},
     "cumulative":{metric_key: {"cum_q2": {end: v}, "cum_q3": {end: v}}},
     "raw_facts": {"annual"|"quarterly": {metric_key: {end: [fact|derived]}}},
     "metrics_meta": {metric_key: {"label", "description", "unit",
                                   "standard", "statement_type",
                                   "original_tag"}}}

``metric_key`` is ``"<standard>:<Tag>"`` (standard in us-gaap | ifrs-full,
per tag namespace). A *fact* in ``raw_facts`` keeps the source metadata
(unit/val/start/end/fy/fp/form/filed/accn/frame/duration_days) so rows
stay auditable against SEC; *derived* entries carry
``{"derived": <rule>, "val": ..., "source_dates": {...}}`` instead.

``metrics_rows`` flattens an extraction into normalized JSONL row dicts
(one row per symbol x metric_key x period_type) — the file-world twin of
the production ``financial_statement_rows`` table (columns preserved:
statement_type, period_type, metric_key<=128, metric_label, latest_value,
unit, is_important, display_order, values map, raw facts, source='xbrl_v6').

Importance rules (``classify``) are a first-match-wins ordered list of
``(statement_type, kind, target, display_order)`` with kind in
{"eq", "ends", "in"} — the declarative encoding of production's lambda
rules. Returns ``(1, order)`` (0 = most important) or ``(0, 99)``.
"""
from __future__ import annotations

from datetime import datetime

# ---------------------------------------------------------------- thresholds
# duration buckets (days, inclusive, end-start+1 as in production)
QUARTERLY_MIN = 60
QUARTERLY_MAX = 130
ANNUAL_MIN = 300

VALID_ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A")
VALID_QTR_FORMS = ("10-Q", "10-Q/A")

# cumulative-period windows for companies whose 10-Qs only report year-to-date
# figures (dur ~182 = H1, ~273 = 9 months)
CUM_Q2_MIN = 170
CUM_Q2_MAX = 200
CUM_Q3_MIN = 260
CUM_Q3_MAX = 290

# Per-share as-filed basis windows (2026-09-02 fix). companyfacts stores
# split-restated comparatives in later-year filings next to the original
# as-filed value (NVDA FY2021 diluted EPS: 6.90 as-filed vs 1.73 restated
# after the 4:1 split). Mixing bases made every downstream subtraction
# (Q4 = annual - Q1..Q3) produce negative EPS/shares. Rule: for share-unit
# metrics prefer facts filed within the as-filed window of the period end;
# only fall back to later restatements when the window has no value.
QTR_ASFILED_WINDOW_DAYS = 150    # 10-Q lands ~40-100 days after period end; /A amendments too
ANNUAL_ASFILED_WINDOW_DAYS = 366  # late 10-K/20-F + same-year /A; restatements gap >= 400d

# ---------------------------------------------------------------- duration


def duration_days(start, end):
    if not start or not end:
        return None
    try:
        dt_start = datetime.strptime(start, "%Y-%m-%d")
        dt_end = datetime.strptime(end, "%Y-%m-%d")
        return (dt_end - dt_start).days + 1
    except (ValueError, OverflowError, TypeError):
        return None


# ---------------------------------------------------------------- fact utils


def normalize_fact(v, unit, duration=None):
    """Keep source metadata so rows can be audited against SEC companyfacts."""
    return {
        "unit": unit,
        "val": v.get("val"),
        "start": v.get("start"),
        "end": v.get("end"),
        "fy": v.get("fy"),
        "fp": v.get("fp"),
        "form": v.get("form"),
        "filed": v.get("filed"),
        "accn": v.get("accn"),
        "frame": v.get("frame"),
        "duration_days": duration,
    }


def fact_identity(v, unit):
    return (
        unit,
        v.get("start"),
        v.get("end"),
        v.get("val"),
        v.get("form"),
        v.get("fy"),
        v.get("fp"),
        v.get("filed"),
        v.get("accn"),
        v.get("frame"),
    )


def is_share_unit(unit):
    return bool(unit) and "share" in str(unit).lower()


def is_weighted_share_unit(unit):
    # raw companyfacts unit id: "Shares" = weighted-average share *count*
    # (not additive: annual mean != sum of four quarterly means).
    # "USD/shares" = per-share *value* (subtractive Q4 derivation OK).
    return str(unit or "").lower().strip() == "shares"


def asfiled_gap_days(v):
    try:
        end = datetime.strptime(v.get("end") or "", "%Y-%m-%d")
        filed = datetime.strptime(v.get("filed") or "", "%Y-%m-%d")
    except ValueError:
        return None
    return (filed - end).days


def fact_rank(v, period_type, share_unit=False):
    """Rank duplicate facts without using the numeric value as quality signal."""
    form = v.get("form") or ""
    filed = v.get("filed") or ""
    accn = v.get("accn") or ""
    primary_forms = VALID_ANNUAL_FORMS if period_type == "annual" else VALID_QTR_FORMS
    rank = (
        1 if form in primary_forms else 0,
        filed,
        1 if form.endswith("/A") else 0,
        accn,
    )
    if share_unit:
        gap = asfiled_gap_days(v)
        window = ANNUAL_ASFILED_WINDOW_DAYS if period_type == "annual" else QTR_ASFILED_WINDOW_DAYS
        in_window = 1 if gap is not None and 0 <= gap <= window else 0
        # in_window leads primary_form: an as-filed Q4 per-share fact inside
        # the 10-K (primary=0 but correct basis) must beat the split-restated
        # comparative carried by a later 10-Q.
        rank = (in_window,) + rank
    return rank


def store_selected_fact(result, raw, period_type, end, val, fact, unit, duration):
    raw[period_type].setdefault(end, []).append(normalize_fact(fact, unit, duration))
    existing_fact = result[f"{period_type}_facts"].get(end)
    su = is_share_unit(unit)
    if existing_fact is None or fact_rank(fact, period_type, su) >= fact_rank(existing_fact, period_type, su):
        result[period_type][end] = val
        result[f"{period_type}_facts"][end] = fact


# ---------------------------------------------------------------- period ends


def infer_period_ends(ns_data):
    """Infer real fiscal period-end dates from duration facts.

    companyfacts also contains event/note instant dates inside 10-K/10-Q
    filings (stock-split dates, repurchase authorization dates...). Those
    stay auditable in raw facts but must not become matrix columns.
    """
    annual_counts = {}
    quarterly_counts = {}

    for tag_info in ns_data.values():
        for vals in tag_info.get("units", {}).values():
            for v in vals:
                end = v.get("end")
                val = v.get("val")
                form = v.get("form", "")
                if not end or val is None:
                    continue
                dur = duration_days(v.get("start"), end)
                if dur is None:
                    continue
                if form in VALID_ANNUAL_FORMS and dur >= ANNUAL_MIN:
                    annual_counts[end] = annual_counts.get(end, 0) + 1
                elif form in VALID_QTR_FORMS and QUARTERLY_MIN <= dur <= QUARTERLY_MAX:
                    quarterly_counts[end] = quarterly_counts.get(end, 0) + 1

    # a genuine fiscal period end is supported by many tags; keep the floor
    # low so sparse small filers pass while one-off note dates do not.
    annual_ends = {end for end, count in annual_counts.items() if count >= 3}
    quarterly_ends = {end for end, count in quarterly_counts.items() if count >= 3}
    return annual_ends, quarterly_ends


# ------------------------------------------------- statement type inference
# priority match, first hit wins; default is "income" (production behaviour)

_BALANCE_KEYWORDS = [
    "Asset", "Liability", "Equity", "Debt", "Payable", "Receivable",
    "Inventory", "Goodwill", "Intangible", "PropertyPlant", "Cash",
    "TreasuryStock", "RetainedEarning", "AccumulatedOtherComprehensive",
    "Deposit", "Accrued", "Prepaid", "DeferredRevenue", "DeferredTax",
    "ContractWithCustomerLiability", "ContractAsset", "Lease",
    "AllowanceForDoubtful", "DerivativeAsset", "DerivativeLiability",
    "LongTermDebt", "ShortTermBorrowing", "LineOfCredit",
    "CommonStock", "PreferredStock", "AdditionalPaidInCapital",
    "OtherComprehensiveIncome", "AccumulatedDepreciation",
]
_INCOME_KEYWORDS = [
    "Revenue", "Income", "Expense", "Earning", "Margin", "Cost",
    "Tax", "Interest", "Dividend", "ShareBasedCompensation",
    "GainLoss", "Writeoff", "WriteDown", "Impairment", "Amortization",
    "Depreciation", "Restructuring", "Litigation", "SellingGeneral",
    "ResearchAndDevelopment", "MarketingExpense",
    "WeightedAverage", "EarningsPerShare",
]
_CASHFLOW_KEYWORDS = [
    "CashFlow", "CashProvided", "CashUsed", "NetCash",
    "CapitalExpenditure", "PaymentsToAcquire", "PaymentsOf",          # dividends, interest
    "ProceedsFrom", "Repayment", "Repurchase",
    "IssuanceOf", "EffectOfExchangeRate", "IncreaseDecrease",
    # CF line items also commonly called Depreciation/Amortization/Dividends.
    # Without cashflow priority + broad match, _INCOME_KEYWORDS grabbed these
    # first and mis-routed them to "income".
    "DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
    "Depreciation", "Amortization", "DividendsPaid", "Dividend",
    "StockIssued", "StockRepurchased",
]


def classify_statement(tag, label):
    """Infer statement_type from tag name + label (v6 rule set)."""
    combined = f"{tag} {label}"
    tag_only = tag.lower()
    for kw in _CASHFLOW_KEYWORDS:
        if kw.lower() in combined.lower():
            return "cashflow"
    # balance matches on the underlying *tag* name only — a BS concept whose
    # label happens to contain "income"/"expense" must not fall to income.
    for kw in _BALANCE_KEYWORDS:
        if kw.lower() in tag_only:
            return "balance"
    for kw in _INCOME_KEYWORDS:
        if kw.lower() in combined.lower():
            return "income"
    return "income"  # production default (the docstring said "other"; the code said income — preserved)


# ---------------------------------------------------------------- unit detect


def detect_unit(units_dict):
    """First unit key wins (dict order == JSON order), mapped to a coarse kind."""
    if not units_dict:
        return "unknown"
    keys = list(units_dict.keys())
    if not keys:
        return "unknown"
    unit = keys[0].upper()
    currency_codes = {"USD", "EUR", "GBP", "JPY", "CNY", "CAD", "CHF", "AUD", "HKD", "KRW"}
    if unit in currency_codes:
        return "currency"
    if unit == "SHARES":
        return "shares"
    if unit in ("PURE", "NUMBER", "RATE"):
        return "ratio"
    return unit.lower()


# ---------------------------------------------------------------- duration buckets


def get_facts_by_duration(ns_data, tag, valid_annual_ends=None, valid_quarterly_ends=None):
    if tag not in ns_data:
        return {"annual": {}, "quarterly": {}, "raw": {"annual": {}, "quarterly": {}}}

    result = {"annual": {}, "quarterly": {}, "annual_facts": {}, "quarterly_facts": {}}
    raw = {"annual": {}, "quarterly": {}}
    seen = set()

    for unit, vals in ns_data[tag].get("units", {}).items():
        for v in vals:
            start = v.get("start")
            end = v.get("end")
            val = v.get("val")
            if not all([end, val is not None]):
                continue

            key = fact_identity(v, unit)
            if key in seen:
                continue
            seen.add(key)

            form = v.get("form", "")
            dur = duration_days(start, end)

            if dur is not None and dur >= ANNUAL_MIN:
                store_selected_fact(result, raw, "annual", end, val, v, unit, dur)
            elif dur is not None and QUARTERLY_MIN <= dur <= QUARTERLY_MAX:
                store_selected_fact(result, raw, "quarterly", end, val, v, unit, dur)
            elif dur is not None and dur <= 3:
                # near-instant duration: keep only if the period end is a
                # real fiscal end supported by other duration facts
                if form in VALID_ANNUAL_FORMS:
                    if not valid_annual_ends or end in valid_annual_ends:
                        store_selected_fact(result, raw, "annual", end, val, v, unit, dur)
                elif form in VALID_QTR_FORMS:
                    if not valid_quarterly_ends or end in valid_quarterly_ends:
                        store_selected_fact(result, raw, "quarterly", end, val, v, unit, dur)
            elif dur is None and start is None:
                # pure instant fact (no start)
                if form in VALID_ANNUAL_FORMS:
                    if not valid_annual_ends or end in valid_annual_ends:
                        store_selected_fact(result, raw, "annual", end, val, v, unit, dur)
                elif form in VALID_QTR_FORMS:
                    if not valid_quarterly_ends or end in valid_quarterly_ends:
                        store_selected_fact(result, raw, "quarterly", end, val, v, unit, dur)

    return {"annual": result["annual"], "quarterly": result["quarterly"], "raw": raw}


def get_cumulative_quarters(ns_data, tag):
    """Extract cumulative Q2 (dur ~182) / Q3 (dur ~273) facts for metrics
    where the SEC only reports year-to-date figures in 10-Qs."""
    if tag not in ns_data:
        return {"cum_q2": {}, "cum_q3": {}}

    result = {"cum_q2": {}, "cum_q3": {}}
    seen = set()

    for unit, vals in ns_data[tag].get("units", {}).items():
        su = is_share_unit(unit)
        for v in vals:
            start = v.get("start")
            end = v.get("end")
            val = v.get("val")
            if not all([start, end, val is not None]):
                continue

            key = fact_identity(v, unit)
            if key in seen:
                continue
            seen.add(key)

            dur = duration_days(start, end)
            if dur is None:
                continue

            if CUM_Q2_MIN <= dur <= CUM_Q2_MAX:
                existing = result["cum_q2"].get(end)
                existing_fact = result.setdefault("cum_q2_facts", {}).get(end)
                if existing is None or existing_fact is None or fact_rank(v, "quarterly", su) >= fact_rank(existing_fact, "quarterly", su):
                    result["cum_q2"][end] = val
                    result["cum_q2_facts"][end] = v
            elif CUM_Q3_MIN <= dur <= CUM_Q3_MAX:
                existing = result["cum_q3"].get(end)
                existing_fact = result.setdefault("cum_q3_facts", {}).get(end)
                if existing is None or existing_fact is None or fact_rank(v, "quarterly", su) >= fact_rank(existing_fact, "quarterly", su):
                    result["cum_q3"][end] = val
                    result["cum_q3_facts"][end] = v

    return result


# ---------------------------------------------------------------- standards


def detect_standards(facts):
    facts_data = facts.get("facts", {})
    standards = []
    for ns in ("us-gaap", "ifrs-full"):
        if ns in facts_data and len(facts_data[ns]) > 10:
            standards.append(ns)
    return standards


# ---------------------------------------------------------------- v6 core


def extract_metrics(facts, ticker):
    """Walk ALL tags the SEC returned (no whitelist), both standards.

    v6 headline change over the old METRIC_TAGS pipeline: every tag becomes
    a metric_key namespaced by standard, with auto statement_type/unit and
    the annual/quarterly duration bucketing above, then:
      - annual-vs-quarterly exact-value overlap dedup (balance instants),
      - cumulative -> standalone quarter conversion,
      - Q4 = annual - Q1 - Q2 - Q3 with basis-safety gates.
    """
    if not facts:
        return None

    facts_data = facts.get("facts", {})
    standards = detect_standards(facts)
    if not standards:
        return {"ticker": ticker, "annual": {}, "quarterly": {}, "metrics_meta": {}}

    annual_data = {}
    quarterly_data = {}
    cumulative_data = {}  # {tag_key: {cum_q2: {}, cum_q3: {}}}
    raw_fact_data = {"annual": {}, "quarterly": {}}
    metrics_meta = {}  # {tag_key: {label, description, unit, standard, statement_type}}

    for std in standards:
        ns_data = facts_data.get(std, {})
        if not ns_data:
            continue
        valid_annual_ends, valid_quarterly_ends = infer_period_ends(ns_data)

        for tag in sorted(ns_data.keys()):
            tag_info = ns_data[tag]
            extracted = get_facts_by_duration(ns_data, tag, valid_annual_ends, valid_quarterly_ends)
            extracted_cum = get_cumulative_quarters(ns_data, tag)

            if not extracted["annual"] and not extracted["quarterly"] and not extracted_cum["cum_q2"] and not extracted_cum["cum_q3"]:
                continue  # tag with nothing usable

            tag_key = f"{std}:{tag}"
            label = tag_info.get("label", tag)
            desc = (tag_info.get("description") or "")[:500]
            units = tag_info.get("units", {})
            unit = detect_unit(units)
            stmt = classify_statement(tag, label)

            metrics_meta[tag_key] = {
                "label": label,
                "description": desc,
                "unit": unit,
                "standard": std,
                "statement_type": stmt,
                "original_tag": tag,
            }

            if extracted["annual"]:
                existing = annual_data.get(tag_key, {})
                for k, v in extracted["annual"].items():
                    if k not in existing:
                        existing[k] = v
                annual_data[tag_key] = existing

            if extracted["quarterly"]:
                existing = quarterly_data.get(tag_key, {})
                for k, v in extracted["quarterly"].items():
                    if k not in existing:
                        existing[k] = v
                quarterly_data[tag_key] = existing

            for period_type in ("annual", "quarterly"):
                raw_period = extracted.get("raw", {}).get(period_type, {})
                if raw_period:
                    dest = raw_fact_data[period_type].setdefault(tag_key, {})
                    for period_end, facts_for_date in raw_period.items():
                        dest.setdefault(period_end, []).extend(facts_for_date)

            if extracted_cum["cum_q2"] or extracted_cum["cum_q3"]:
                cum_existing = cumulative_data.get(tag_key, {"cum_q2": {}, "cum_q3": {}})
                for ck in ("cum_q2", "cum_q3"):
                    for k, v in extracted_cum[ck].items():
                        if k not in cum_existing[ck]:
                            cum_existing[ck][k] = v
                cumulative_data[tag_key] = cum_existing

    # dedup: drop a quarterly entry only where annual/quarterly hold the
    # *exact same value* at the same date (balance-sheet instants).
    # Income/CF Q4 != full year, so those dates must both survive!
    for key in list(annual_data.keys()):
        if key in quarterly_data:
            overlap = set(annual_data[key].keys()) & set(quarterly_data[key].keys())
            if overlap:
                for d in overlap:
                    av = annual_data[key].get(d)
                    qv = quarterly_data[key].get(d)
                    if av is not None and qv is not None and av == qv:
                        del quarterly_data[key][d]
            if not quarterly_data[key]:
                del quarterly_data[key]

    # ---- cumulative -> standalone + Q4 derivation -------------------------
    # Q2_standalone = Q2_cumulative - Q1
    # Q3_standalone = Q3_cumulative - Q2_cumulative (same end date), else
    #                 Q3_cumulative - Q1 - Q2_standalone
    # Q4 = FullYear - Q1 - Q2 - Q3
    for tag_key, mi in metrics_meta.items():
        stmt_type = mi.get("statement_type", "income")
        if stmt_type == "balance":
            continue  # balance instants need no Q4

        unit_meta = str(mi.get("unit") or "")
        share_unit = is_share_unit(unit_meta) or "pershare" in tag_key.lower() or "perstock" in tag_key.lower()
        # weighted-average share counts are not additive: annual average !=
        # sum of four quarterly averages, so any subtraction derivation is
        # guaranteed garbage (once produced NVDA -49.7B shares).
        weighted_shares = is_weighted_share_unit(unit_meta)

        ann = annual_data.get(tag_key, {})
        qtr = quarterly_data.get(tag_key, {})
        cum = cumulative_data.get(tag_key, {})
        if not ann or not qtr:
            continue

        ann_dates = sorted(ann.keys())
        qtr_dates = sorted(qtr.keys())

        cum_q2 = cum.get("cum_q2", {})
        cum_q3 = cum.get("cum_q3", {})

        for i, ann_date in enumerate(ann_dates):
            prev_ann = ann_dates[i - 1] if i > 0 else None

            fy_qs = []
            for qd in qtr_dates:
                if prev_ann and qd <= prev_ann:
                    continue
                if qd >= ann_date:
                    break
                fy_qs.append(qd)

            if not fy_qs:
                continue

            q1_date = fy_qs[0]
            q1_val = qtr.get(q1_date)

            # Q2: standalone if present, else derive from cumulative
            q2_date = fy_qs[1] if len(fy_qs) >= 2 else None
            q2_val = qtr.get(q2_date) if q2_date else None
            if q2_val is None and q1_val is not None and not weighted_shares:
                for cq2_date, cq2_val in sorted(cum_q2.items()):
                    if (not prev_ann or cq2_date > prev_ann) and cq2_date <= ann_date:
                        q2_val = cq2_val - q1_val
                        q2_date = cq2_date
                        qtr[q2_date] = q2_val
                        raw_fact_data["quarterly"].setdefault(tag_key, {}).setdefault(q2_date, []).append({
                            "derived": "cum_q2_minus_q1",
                            "val": q2_val,
                            "source_dates": {"q1": q1_date, "cum_q2": cq2_date},
                        })
                        if q2_date not in qtr_dates:
                            qtr_dates.append(q2_date)
                            qtr_dates.sort()
                        break

            # Q3: standalone if present, else derive from cumulative
            q3_date = fy_qs[2] if len(fy_qs) >= 3 else None
            q3_val = qtr.get(q3_date) if q3_date else None
            if q3_val is None and not weighted_shares:
                for cq3_date, cq3_val in sorted(cum_q3.items()):
                    if (not prev_ann or cq3_date > prev_ann) and cq3_date <= ann_date:
                        # prefer Q3_cum - Q2_cum when the same end date has both
                        cq2_val = cum_q2.get(cq3_date)
                        if cq2_val is not None:
                            q3_val = cq3_val - cq2_val
                        elif q1_val is not None and q2_val is not None:
                            q3_val = cq3_val - q1_val - q2_val
                        else:
                            break
                        if q3_val is not None:
                            q3_date = cq3_date
                            qtr[q3_date] = q3_val
                            raw_fact_data["quarterly"].setdefault(tag_key, {}).setdefault(q3_date, []).append({
                                "derived": "cum_q3_minus_cum_q2_or_q1_q2",
                                "val": q3_val,
                                "source_dates": {"q1": q1_date, "q2": q2_date, "cum_q3": cq3_date},
                            })
                            if q3_date not in qtr_dates:
                                qtr_dates.append(q3_date)
                                qtr_dates.sort()
                        break

            # ---- Q4 derivation
            if ann_date in qtr:
                continue  # Q4 already present

            ann_val = ann.get(ann_date)
            if ann_val is None:
                continue

            # re-find quarters after possible cumulative insertions
            fy_qs = []
            for qd in qtr_dates:
                if prev_ann and qd <= prev_ann:
                    continue
                if qd >= ann_date:
                    break
                fy_qs.append(qd)

            if len(fy_qs) < 3:
                continue

            q1v = qtr.get(fy_qs[0])
            q2v = qtr.get(fy_qs[1])
            q3v = qtr.get(fy_qs[2])
            if q1v is None or q2v is None or q3v is None:
                continue

            if weighted_shares:
                continue  # weighted share counts are not additive

            q4_val = ann_val - q1v - q2v - q3v
            # basis safety gate 1: Q4 opposite sign while all three reported
            # quarters share the annual sign => annual and Q1..Q3 come from
            # different restatement bases (pre/post split); result is junk.
            if (ann_val != 0 and q4_val * ann_val < 0
                    and q1v * ann_val > 0 and q2v * ann_val > 0 and q3v * ann_val > 0):
                continue
            # basis safety gate 2: residual mixed basis shows as a magnitude
            # jump (2x/4x/10x/20x split ratios) for per-share metrics.
            if share_unit:
                _mx = max(abs(q1v), abs(q2v), abs(q3v))
                if _mx and abs(q4_val) > 4 * _mx:
                    continue
            quarterly_data[tag_key][ann_date] = q4_val
            raw_fact_data["quarterly"].setdefault(tag_key, {}).setdefault(ann_date, []).append({
                "derived": "annual_minus_q1_q2_q3",
                "val": q4_val,
                "source_dates": {"annual": ann_date, "q1": fy_qs[0], "q2": fy_qs[1], "q3": fy_qs[2]},
            })

    return {
        "ticker": ticker,
        "annual": annual_data,
        "quarterly": quarterly_data,
        "cumulative": cumulative_data,
        "raw_facts": raw_fact_data,
        "metrics_meta": metrics_meta,
    }


# ---------------------------------------------------------------- importance
# first match wins, per (statement_type); declarative twin of the production
# importance-rules table. kind: eq=exact metric_key, ends=metric_key suffix,
# in=membership in target tuple. display_order: 0 = most important.

_RULES = [
    # ═══ Income Statement ═══
    ("income", "ends", "RevenueFromContractWithCustomerExcludingAssessedTax", 0),
    ("income", "eq", "us-gaap:Revenues", 0),
    ("income", "eq", "us-gaap:SalesRevenueNet", 0),
    ("income", "in", ("us-gaap:CostOfGoodsAndServicesSold", "us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsSold"), 1),
    ("income", "eq", "us-gaap:GrossProfit", 2),
    ("income", "in", ("us-gaap:OperatingExpenses", "us-gaap:CostsAndExpenses"), 3),
    ("income", "eq", "us-gaap:ResearchAndDevelopmentExpense", 4),
    ("income", "in", ("us-gaap:SellingGeneralAndAdministrativeExpense", "us-gaap:GeneralAndAdministrativeExpense"), 5),
    ("income", "eq", "us-gaap:SellingAndMarketingExpense", 5),
    ("income", "eq", "us-gaap:OperatingIncomeLoss", 6),
    ("income", "ends", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", 7),
    ("income", "eq", "us-gaap:NetIncomeLoss", 8),
    ("income", "eq", "us-gaap:ComprehensiveIncomeNetOfTax", 9),
    ("income", "eq", "us-gaap:EarningsPerShareBasic", 10),
    ("income", "eq", "us-gaap:EarningsPerShareDiluted", 11),
    ("income", "eq", "us-gaap:InterestExpense", 12),
    ("income", "eq", "us-gaap:InterestCostsIncurred", 12),
    ("income", "eq", "us-gaap:InvestmentIncomeInterestAndDividend", 13),
    ("income", "eq", "us-gaap:IncomeTaxExpenseBenefit", 14),
    ("income", "in", ("us-gaap:DepreciationDepletionAndAmortization", "us-gaap:DepreciationAndAmortization"), 15),
    ("income", "eq", "us-gaap:Depreciation", 15),
    ("income", "eq", "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic", 16),
    ("income", "eq", "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding", 16),
    ("income", "eq", "us-gaap:NonoperatingIncomeExpense", 17),
    ("income", "eq", "us-gaap:OtherNonoperatingIncomeExpense", 17),
    ("income", "eq", "us-gaap:MarketingExpense", 18),
    ("income", "eq", "us-gaap:AdvertisingExpense", 18),

    # ═══ Balance Sheet ═══
    ("balance", "eq", "us-gaap:Assets", 0),
    ("balance", "eq", "us-gaap:AssetsCurrent", 1),
    ("balance", "in", ("us-gaap:CashAndCashEquivalentsAtCarryingValue", "us-gaap:Cash",
                       "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), 2),
    ("balance", "eq", "us-gaap:MarketableSecuritiesCurrent", 3),
    ("balance", "eq", "us-gaap:AccountsReceivableNetCurrent", 4),
    ("balance", "eq", "us-gaap:InventoryNet", 5),
    ("balance", "eq", "us-gaap:PropertyPlantAndEquipmentNet", 6),
    ("balance", "eq", "us-gaap:Goodwill", 7),
    ("balance", "in", ("us-gaap:IntangibleAssetsNetExcludingGoodwill", "us-gaap:IntangibleAssetsNet"), 8),
    ("balance", "eq", "us-gaap:Liabilities", 9),
    ("balance", "eq", "us-gaap:LiabilitiesAndStockholdersEquity", 9),
    ("balance", "eq", "us-gaap:LiabilitiesCurrent", 10),
    ("balance", "eq", "us-gaap:AccountsPayableCurrent", 11),
    ("balance", "ends", "LongTermDebtCurrent", 12),
    ("balance", "eq", "us-gaap:ShortTermBorrowings", 12),
    ("balance", "eq", "us-gaap:LongTermDebtNoncurrent", 13),
    ("balance", "eq", "us-gaap:LongTermDebt", 13),
    ("balance", "eq", "us-gaap:DebtInstrumentCarryingAmount", 13),
    ("balance", "in", ("us-gaap:StockholdersEquity", "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), 14),
    ("balance", "eq", "us-gaap:CommonStocksIncludingAdditionalPaidInCapital", 14),
    ("balance", "eq", "us-gaap:RetainedEarningsAccumulatedDeficit", 15),
    ("balance", "eq", "us-gaap:DeferredRevenueCurrent", 17),
    ("balance", "eq", "us-gaap:ContractWithCustomerLiabilityCurrent", 17),
    ("balance", "eq", "us-gaap:CommonStockSharesOutstanding", 18),
    ("balance", "eq", "us-gaap:AssetsNoncurrent", 19),
    ("balance", "eq", "us-gaap:LiabilitiesNoncurrent", 19),
    ("balance", "eq", "us-gaap:DeferredIncomeTaxAssetsNet", 20),
    ("balance", "eq", "us-gaap:DeferredIncomeTaxLiabilitiesNet", 20),
    ("balance", "eq", "us-gaap:AccumulatedOtherComprehensiveIncomeLossNetOfTax", 21),

    # ═══ Cash Flow ═══
    ("cashflow", "in", ("us-gaap:NetCashProvidedByUsedInOperatingActivities",
                        "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"), 0),
    ("cashflow", "in", ("us-gaap:NetCashProvidedByUsedInInvestingActivities",
                        "us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"), 1),
    ("cashflow", "in", ("us-gaap:NetCashProvidedByUsedInFinancingActivities",
                        "us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"), 2),
    ("cashflow", "eq", "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", 3),
    ("cashflow", "eq", "us-gaap:PaymentsToAcquireProductiveAssets", 3),
    ("cashflow", "in", ("us-gaap:DepreciationDepletionAndAmortization", "us-gaap:DepreciationAndAmortization",
                        "us-gaap:DepreciationAmortizationAndAccretionNet"), 4),
    ("cashflow", "eq", "us-gaap:ShareBasedCompensation", 5),
    ("cashflow", "eq", "us-gaap:AllocatedShareBasedCompensationExpense", 5),
    ("cashflow", "eq", "us-gaap:PaymentsForRepurchaseOfCommonStock", 6),
    ("cashflow", "eq", "us-gaap:PaymentsForRepurchaseOfEquity", 6),
    ("cashflow", "eq", "us-gaap:PaymentsOfDividends", 7),
    ("cashflow", "eq", "us-gaap:ProceedsFromIssuanceOfLongTermDebt", 8),
    ("cashflow", "eq", "us-gaap:RepaymentsOfLongTermDebt", 8),
    ("cashflow", "eq", "us-gaap:ProceedsFromRepaymentsOfCommercialPaper", 9),
    ("cashflow", "eq", "us-gaap:ProceedsFromRepaymentsOfShortTermDebt", 9),
    ("cashflow", "eq", "us-gaap:IncreaseDecreaseInAccountsReceivable", 10),
    ("cashflow", "eq", "us-gaap:IncreaseDecreaseInInventories", 10),
    ("cashflow", "eq", "us-gaap:IncreaseDecreaseInAccountsPayable", 10),
    ("cashflow", "eq", "us-gaap:IncreaseDecreaseInDeferredRevenue", 11),
    ("cashflow", "eq", "us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired", 12),
    ("cashflow", "eq", "us-gaap:PaymentsToAcquireIntangibleAssets", 12),
    ("cashflow", "eq", "us-gaap:ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities", 13),
    ("cashflow", "eq", "us-gaap:ProceedsFromSaleOfAvailableForSaleSecurities", 13),
    ("cashflow", "eq", "us-gaap:ProceedsFromSaleAndMaturityOfMarketableSecurities", 13),
    ("cashflow", "eq", "us-gaap:ProceedsFromIssuanceOfCommonStock", 14),
    ("cashflow", "eq", "us-gaap:ProceedsFromStockOptionsExercised", 14),
    ("cashflow", "eq", "us-gaap:CashAndCashEquivalentsPeriodIncreaseDecrease", 15),
    ("cashflow", "eq", "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect", 15),
]

SOURCE_TAG = "xbrl_v6"


def _rule_match(metric_key, kind, target):
    if kind == "eq":
        return metric_key == target
    if kind == "ends":
        return metric_key.endswith(target)
    if kind == "in":
        return metric_key in target
    return False


def classify(metric_key, statement_type):
    """Twin of the production importance classifier: (is_important, display_order).

    is_important: 1 when the key matches a rule for this statement_type.
    display_order: 0 = most important ... 99 = not important.
    """
    for stmt, kind, target, order in _RULES:
        if stmt == statement_type and _rule_match(metric_key, kind, target):
            return 1, order
    return 0, 99


# ---------------------------------------------------------------- row builder


def latest_val(clean):
    """Latest (max period-end) numeric value, or None."""
    for k in sorted(clean.keys(), reverse=True):
        v = clean[k]
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def select_raw_facts(raw_facts, period_type, tag_key, clean):
    """Raw fact entries for exactly the kept period ends (auditable subset)."""
    raw_dates = raw_facts.get(period_type, {}).get(tag_key, {})
    selected = {k: raw_dates.get(k, []) for k in clean if k in raw_dates}
    return selected or None


def metrics_rows(symbol, extraction):
    """Flatten an extraction into normalized JSONL row dicts.

    File-world twin of the production ``financial_statement_rows`` writer
    (write_to_db / write_ticker): one row per symbol x metric_key x
    period_type, sorted, None values cleaned out.
    """
    rows = []
    annual = extraction.get("annual", {}) or {}
    quarterly = extraction.get("quarterly", {}) or {}
    meta = extraction.get("metrics_meta", {}) or {}
    raw_facts = extraction.get("raw_facts", {}) or {}

    for period_type, pdata in (("annual", annual), ("quarterly", quarterly)):
        for tag_key, vals in sorted(pdata.items()):
            clean = {k: v for k, v in vals.items() if v is not None}
            if not clean:
                continue
            mi = meta.get(tag_key, {})
            stmt_type = mi.get("statement_type", "income")
            is_imp, disp_order = classify(tag_key, stmt_type)
            row = {
                "symbol": symbol,
                "statement_type": stmt_type,
                "period_type": period_type,
                "metric_key": tag_key[:128],
                "metric_label": mi.get("label") or tag_key,
                "latest_value": latest_val(clean),
                "unit": mi.get("unit", "unknown"),
                "standard": mi.get("standard"),
                "original_tag": mi.get("original_tag"),
                "description": mi.get("description", ""),
                "is_important": is_imp,
                "display_order": disp_order,
                "values": clean,
                "source": SOURCE_TAG,
            }
            raw = select_raw_facts(raw_facts, period_type, tag_key, clean)
            if raw is not None:
                row["raw_facts"] = raw
            rows.append(row)
    return rows
