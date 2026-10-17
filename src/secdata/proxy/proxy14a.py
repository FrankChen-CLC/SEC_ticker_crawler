"""DEF 14A parsing: Pay-vs-Performance (ecd inline XBRL), Summary Compensation
Table (heuristic HTML table parse), related-party section, metric weights.

All values straight from the filing; nothing inferred silently — every parser
returns confidence/coverage notes so the consumer can show what is real vs
absent.

Port notes: production used ``BeautifulSoup(doc, "lxml")``; this port uses the
stdlib ``hsoup.parse_html`` stand-in (same traversal surface) so the package
stays stdlib+requests. The table/section/hidden-layer parse RULES below are
kept exactly as coded in the production proxy14a module — each branch encodes a real
DEF 14A quirk (CEG title-rows, ABBV legend rows, XOM image-layer tables,
JPM/AES colspan shapes).
"""
import re
from collections import defaultdict

from . import fetch
from .hsoup import parse_html

NIL = object()


# ------------------------------------------------------------------ fetch
def latest_def14a(cik, filings=None):
    filings = filings or fetch.recent_filings(cik)
    for f in filings:
        if f["form"] == "DEF 14A":
            return f
    return None


def fetch_doc(cik, filing):
    url = fetch.filing_url(cik, filing["accession"], filing["doc"])
    return fetch.get_text(url)


# ------------------------------------------------------------------ inline XBRL
def parse_contexts(doc):
    ctx = {}
    for cid, body in re.findall(
            r'<xbrli:context\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</xbrli:context>',
            doc, re.S):
        members = re.findall(r"explicitMember[^>]*>([^<]+)</", body)
        inst = re.findall(r"<xbrli:instant>([^<]+)</xbrli:instant>", body)
        per = re.findall(
            r"<xbrli:startDate>([^<]+)</xbrli:startDate>\s*"
            r"<xbrli:endDate>([^<]+)</xbrli:endDate>", body)
        ctx[cid] = {"members": members,
                    "instant": inst[0] if inst else None,
                    "start": per[0][0] if per else None,
                    "end": per[0][1] if per else None}
    return ctx


def _num(raw, scale):
    raw = raw.replace(",", "").strip()
    if raw in ("", "-"):
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    return v * (10 ** int(scale or 0))


def parse_ix_facts(doc):
    """-> {(name, ctx): value}  (numbers scaled, strings cleaned, nil skipped)."""
    out = {}
    for attrs, body in re.findall(r"<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>",
                                  doc, re.S):
        at = dict(re.findall(r'([\w:-]+)="([^"]*)"', attrs))
        if at.get("xsi:nil") == "true":
            continue
        val = _num(fetch.strip_tags(body), at.get("scale", "0"))
        if val is None:
            continue
        out[(at.get("name"), at.get("contextRef"))] = val
    for attrs, body in re.findall(r"<ix:nonNumeric\b([^>]*)>(.*?)</ix:nonNumeric>",
                                  doc, re.S):
        at = dict(re.findall(r'([\w:-]+)="([^"]*)"', attrs))
        if at.get("xsi:nil") == "true":
            continue
        key = (at.get("name"), at.get("contextRef"))
        if key not in out:
            out[key] = fetch.strip_tags(body)
    return out


def _ctx_year(ctx, cid):
    c = ctx.get(cid) or {}
    end = c.get("end") or c.get("instant") or ""
    return end[:4] if end else None


def pvp_table(cik, doc=None, filing=None):
    """Pay-versus-performance: one row per covered fiscal year.

    Returns dict {years: [...], peo_name, measures, notes}.
    """
    notes = []
    if doc is None:
        filing = filing or latest_def14a(cik)
        if not filing:
            return {"years": [], "notes": ["no DEF 14A found"]}
        doc = fetch_doc(cik, filing)
    ctx = parse_contexts(doc)
    facts = parse_ix_facts(doc)
    by = defaultdict(dict)
    for (name, cid), val in facts.items():
        by[name][cid] = val
    peo_names = {cid: v for (n, d) in by.items() for (cid, v) in d.items()
                 if n == "ecd:PeoName"}
    peo_name = next(iter(peo_names.values()), None)

    def series(concept):
        return {cid: v for cid, v in by.get(concept, {}).items()}

    peo_sct = series("ecd:PeoTotalCompAmt")
    peo_cap = series("ecd:PeoActuallyPaidCompAmt")
    neo_sct = series("ecd:NonPeoNeoAvgTotalCompAmt")
    neo_cap = series("ecd:NonPeoNeoAvgCompActuallyPaidAmt")
    tsr = series("ecd:TotalShareholderRtnAmt")
    ptsr = series("ecd:PeerGroupTotalShareholderRtnAmt")
    ni = series("us-gaap:NetIncomeLoss")
    co_name = next(iter(series("ecd:CoSelectedMeasureName").values()), None)
    co_amt = series("ecd:CoSelectedMeasureAmt")
    measure_names = series("ecd:MeasureName")

    year_ctx = {}
    cands = set(list(peo_sct) + list(peo_cap) + list(tsr) + list(neo_cap))
    # prefer dimensionless (default-axis) contexts: individual-member contexts
    # (ecd:IndividualAxis=...) share the same period and would poison the year map
    for cid in sorted(cands, key=lambda c: 0 if not (ctx.get(c) or {}).get("members") else 1):
        y = _ctx_year(ctx, cid)
        if y:
            year_ctx.setdefault(y, cid)
    years = []
    for y, cid in sorted(year_ctx.items(), reverse=True):
        row = {
            "fy": int(y),
            "peo_sct_total": peo_sct.get(cid),
            "peo_comp_actually_paid": peo_cap.get(cid),
            "neo_avg_sct_total": neo_sct.get(cid),
            "neo_avg_comp_actually_paid": neo_cap.get(cid),
            "tsr_index": tsr.get(cid),
            "peer_tsr_index": ptsr.get(cid),
            "net_income": ni.get(cid),
            "company_selected_measure": co_name,
            "company_selected_measure_value": co_amt.get(cid),
        }
        years.append(row)
    if not years:
        notes.append("no ecd PvP facts tagged in this proxy")
    measures = sorted({v for v in measure_names.values() if isinstance(v, str)})
    return {"peo_name": peo_name, "years": years, "measures": measures,
            "notes": notes, "filing": filing and
            {"accession": filing["accession"], "date": filing["date"]}}


# ------------------------------------------------------------------ SCT
COL_ALIASES = [
    ("year", [r"^year$", r"^fiscal( year)?$"]),
    ("salary", [r"salary"]),
    ("bonus", [r"^bonus", r"annual bonus"]),
    ("stock_awards", [r"stock\s*award"]),
    ("option_awards", [r"option\s*award"]),
    ("non_equity", [r"non-?equity\s+incentive", r"incentive\s+plan\s+comp"]),
    ("pension", [r"pension", r"nonqualified\s+deferred"]),
    ("all_other", [r"all\s+other"]),
    ("total", [r"^total"]),
]

# a name cell whose FIRST line reads like a job title (CEG-style rowspan puts
# "President and Chief Executive Officer" in every year row while the NEO's
# actual name lives in a separate name-only row above)
_TITLE_RE = re.compile(r"(?i)\b(chief|president|chairman|chairwoman|chair|"
                       r"officer|vice|director|executive|secretary|treasurer|"
                       r"counsel|ceo|cfo|coo|cio|cpo)\b")


def _looks_like_name(s):
    """Two+ nameish tokens with at least one non-acronym word ('D.W. Woods'
    yes, 'EVP &' no, all-caps company strings no)."""
    toks = s.replace(",", " ").split()
    if len(toks) < 2:
        return False
    has_plain = False
    for t in toks:
        a = t.strip(".'’\"-()").replace(".", "").replace("-", "").replace("'", "")
        if not a or not a.isalpha():
            return False
        if len(a) == 1 or not a.isupper():
            has_plain = True
    return has_plain


def _clean_title_lines(lines):
    out = []
    for x in lines:
        x = x.replace("\xa0", " ").strip()
        if x and _zw_clean(x) and not re.fullmatch(r"\(\w{1,4}\)", x):
            out.append(x)
    return out


def _norm_cell(t):
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", " ", t)).lower().strip(" -$")


def _zw_clean(s):
    return s.replace("​", "").replace("‌", "").strip()


def _cell_number(t):
    """Numeric value of a table cell, or None (skips $, footnotes, years)."""
    t = _zw_clean(t).replace("\xa0", "").replace(",", "").replace("$", "").strip()
    t = re.sub(r"(?:\(\d{1,3}\))+$", "", t).strip()   # trailing footnote refs
    if not t or re.fullmatch(r"(19|20)\d\d", t):
        return None
    m = re.fullmatch(r"\(([\d.]+)\)", t)               # (1,234) = negative
    if m:
        return -float(m.group(1))
    if re.fullmatch(r"-?[\d.]+", t):
        return float(t)
    return None


def _grid(tbl):
    """Expand colspan/rowspan into a 2-D list of cell elements."""
    grid = []
    for tr in tbl.find_all("tr"):
        r = len(grid)
        grid.append({})
        col = 0
        for c in tr.find_all(["td", "th"]):
            while col in grid[r]:
                col += 1
            cs = max(1, int(c.get("colspan") or 1))
            rs = max(1, int(c.get("rowspan") or 1))
            for dr in range(rs):
                while len(grid) <= r + dr:
                    grid.append({})
                for dc in range(cs):
                    grid[r + dr][col + dc] = c
            col += cs
    width = max((len(g) for g in grid), default=0)
    return [[g.get(c) for c in range(width)] for g in grid]


def _layer_num(t):
    """Float of a flattened text token ('2,058,000', '(1,234)'), None if not numeric."""
    neg = t.startswith("(") and t.endswith(")")
    v = t.strip("()").replace(",", "")
    try:
        f = float(v)
    except ValueError:
        return None
    return -f if neg else f


_SCT7 = ("salary", "bonus", "stock_awards", "option_awards",
         "non_equity", "pension", "all_other")
_SCT6 = ("salary", "bonus", "stock_awards", "non_equity",
         "pension", "all_other")      # option-plan column dropped
_SCT5 = ("salary", "bonus", "stock_awards", "non_equity",
         "all_other")                 # option + pension columns dropped


def _hidden_layer_sct(soup):
    """Recover an SCT rendered as a page IMAGE whose data sits in an
    invisible white-0.5pt text layer inside a single <p> (XOM 2026 DEF 14A).
    There are no <table> elements at all; the flattened token stream is
    recovered as: 4-digit year + k numbers where the first k-1 sum to the
    k-th (SEC mandates Total = sum of columns), name = the alphabetic run
    before the year minus title words."""
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not (len(txt) > 400 and "summary compensation table" in txt.lower()
                and "salary" in txt.lower()):
            continue
        toks = txt.split()
        rows, pending = [], None
        i = 0
        while i < len(toks):
            if re.fullmatch(r"(19|20)\d\d", toks[i]):
                nums, j = [], i + 1
                while j < len(toks):
                    v = _layer_num(toks[j])
                    if v is None or len(nums) >= 10:
                        break
                    nums.append(v)
                    j += 1
                ok = None
                if 4 <= len(nums) <= 9:
                    vals, tot = nums[:-1], nums[-1]
                    if abs(sum(vals) - tot) <= max(2.0, 0.005 * abs(tot)):
                        ok = (vals, tot)
                elif len(nums) >= 10:
                    for cut in range(8, 11):
                        vals, tot = nums[:cut - 1], nums[cut - 1]
                        if abs(sum(vals) - tot) <= max(2.0, 0.005 * abs(tot)):
                            ok = (vals, tot)
                            j = i + cut
                            break
                if ok:
                    run, k = [], i - 1
                    while k >= 0 and re.fullmatch(
                            r"[A-Za-z][\w.'’-]*,?", toks[k]):
                        w = toks[k].rstrip(",")
                        if _TITLE_RE.search(w) or w.lower() in ("and", "the"):
                            if run:
                                break
                        else:
                            run.insert(0, w)
                        k -= 1
                    nm = " ".join(run).strip(" ,;")
                    if nm and _looks_like_name(nm):
                        pending = nm
                    colset = {7: _SCT7, 6: _SCT6, 5: _SCT5} \
                        .get(len(ok[0])) or ()
                    rec = {"name": pending, "title": None,
                           "year": int(toks[i]), "total": ok[1]}
                    for key in _SCT7:
                        rec[key] = None
                    for key, v in zip(colset, ok[0]):
                        rec[key] = v
                    rows.append(rec)
                    i = j
                    continue
            i += 1
        good = [r for r in rows if r["name"]]
        if len(good) >= 3 and len({r["year"] for r in good}) >= 2:
            return good
    return []


def summary_compensation_table(doc):
    """Heuristic parse of the Summary Compensation Table.

    Handles the standard SCT shape: name cell uses rowspan=3 (one row per
    fiscal year), so continuation rows arrive one cell short and start with
    the year — those get the previous name re-attached.

    Returns list of {name, title, year, salary, bonus, stock_awards,
    option_awards, non_equity, pension, all_other, total} plus mix percents.
    """
    soup = parse_html(doc)
    best, best_score = [], -1
    for tbl in soup.find_all("table"):
        grid = _grid(tbl)
        if len(grid) < 4:
            continue
        head_row, head_cells = -1, []
        # window 8: ABBV-style tables put a legend row ("(a)(b)(c)…" column
        # keys) above the real header and wrap column names over 6+ <tr>s,
        # pushing the row containing "salary" far past the 3rd
        for ri, row in enumerate(grid[:8]):
            cells = [_norm_cell(_zw_clean(fetch.strip_tags(c.get_text())))
                     if c else "" for c in row]
            if any("salary" in c for c in cells):
                head_row, head_cells = ri, cells
                break
        if head_row < 0:
            continue
        if head_row > 0:
            # fold every header row above into its column so 'stock' +
            # 'awards' or 'change in pension value and non-equity' split
            # across several <tr>s still map to one column key
            fold = []
            for i in range(len(head_cells)):
                parts = []
                for rj in range(head_row + 1):
                    if i < len(grid[rj]) and grid[rj][i] is not None:
                        t = _norm_cell(_zw_clean(
                            fetch.strip_tags(grid[rj][i].get_text())))
                        if t and t not in parts:
                            parts.append(t)
                fold.append(" ".join(parts))
            head_cells = fold
        # two-tier headers: fill blanks from the row below
        if head_row + 1 < len(grid):
            below = [_norm_cell(_zw_clean(fetch.strip_tags(c.get_text())))
                     if c else "" for c in grid[head_row + 1]]
            for i, h in enumerate(head_cells):
                if not h and i < len(below):
                    head_cells[i] = below[i]
        colmap = {}
        for idx, cell in enumerate(head_cells):
            # whitespace-free match: PDF-extracted headers glue words together
            # ('allothercompensation', 'stockawards' — ABM) exactly as often as
            # they space them, so every alias pattern is matched with spaces
            # removed from both sides.
            cell_ns = cell.replace(" ", "")
            for key, pats in COL_ALIASES:
                if key in colmap.values():
                    continue
                if any(re.search(p.replace("\\s+", "").replace("\\s*", "")
                                 .replace(" ", ""), cell_ns) for p in pats):
                    colmap[idx] = key
                    break
        score = len(set(colmap.values()))
        if score < 4 or score <= best_score:
            continue
        best_score = score
        best = []
        last_name = last_title = None
        # Range-based column mapping: each header key owns the grid columns
        # from its first cell up to the next key's first cell (colspan-heavy
        # tables like JPM's repeat one <td> across many grid columns).
        kcols = sorted((i, k) for i, k in colmap.items() if k != "year")
        ranges = []
        for n, (i, k) in enumerate(kcols):
            end = kcols[n + 1][0] if n + 1 < len(kcols) else len(head_cells)
            ranges.append((k, i, end))
        year_hdr = min((i for i, k in colmap.items() if k == "year"),
                       default=None)
        for row in grid[head_row + 1:]:
            texts = [_zw_clean(fetch.strip_tags(c.get_text())) if c else ""
                     for c in row]
            # Name capture runs BEFORE the year-row test: tables like AES's put
            # each NEO's name in a row of its own (col0 spanning), with the
            # fiscal-year rows after it; skipping those name rows first left
            # every year row unnamed and the whole SCT parsed as 0 rows.
            # The name is the first cell that is neither a year, a numeric/
            # dash marker, a "(a)" legend key, nor header text — ARRY-style
            # tables carry a leading blank/legend column that shifts the name
            # off col0, so reading row[0] alone would find nothing.
            lines = []
            for c in row:
                if c is None:
                    continue
                t = _zw_clean(fetch.strip_tags(c.get_text())).replace("\xa0", " ").strip()
                if (not t or re.fullmatch(r"(19|20)\d\d", t)
                        or re.fullmatch(r"[\d.,%$()\-–—―\s]+", t)
                        or re.fullmatch(r"\(([A-Za-z0-9]{1,3})\)", t)
                        or re.search(r"(?i)principal (compensation )?position|name and", t)):
                    continue
                lines = [x.strip() for x in
                         c.get_text("\n").split("\n") if _zw_clean(x)]
                break
            if lines and not re.fullmatch(r"(19|20)\d\d", lines[0].strip()):
                head = lines[0].strip()
                tm_ = _TITLE_RE.search(head)
                if tm_:
                    # a title keyword in the FIRST line. Two shapes: ACLS
                    # single-line "James G. Coogan, Executive Vice President"
                    # (words before the keyword are the name) and CEG pure
                    # title cells repeating every year row (name lives in a
                    # separate row above -> must not overwrite). Cut first,
                    # fall back to title-slot-only.
                    pre = head[:tm_.start()].strip().strip(",; ")
                    if pre and not _TITLE_RE.search(pre) and _looks_like_name(pre):
                        last_name = pre.replace("\xa0", " ").rstrip(",;")
                        tail = head[tm_.start():].strip().strip(",; ")
                        last_title = " / ".join(
                            _clean_title_lines(([tail] if tail else [])
                                               + lines[1:])) or None
                    else:
                        # title-only cell: may fill a missing title, never the
                        # name captured from the name row above
                        joined = " / ".join(_clean_title_lines(lines))
                        if last_title is None and joined:
                            last_title = joined
                else:
                    last_name = lines[0].replace("\xa0", " ").strip().rstrip(",;")
                    last_title = " / ".join(
                        _clean_title_lines(lines[1:])) or None
            ycol = next((i for i, t in enumerate(texts)
                         if re.fullmatch(r"(19|20)\d\d", t)), None)
            if ycol is None:
                continue
            year = int(texts[ycol])
            # continuation rows in rowspan tables drop the name cells, so the
            # whole row shifts left; realign using the year column as anchor
            off = ycol - year_hdr if year_hdr is not None else 0
            rec = {"name": last_name, "title": last_title, "year": year}
            for key, s, e in ranges:
                s2, e2 = max(0, s + off), min(len(row), e + off)
                val, seen = None, set()
                for ci in range(s2, e2):
                    c = row[ci]
                    if c is None or id(c) in seen:
                        continue
                    seen.add(id(c))
                    v = _cell_number(texts[ci])
                    if v is not None:
                        val = v
                        break
                rec[key] = val
            if rec["name"]:
                best.append(rec)
        if best:
            break
    if not best:
        # XOM-2026 style: the SCT is a page image with the data in an
        # invisible (white 0.5pt) text layer inside one <p> — no <table>
        # elements to find. The flattened stream is still reliably parsable
        # because SEC mandates Total = sum of the compensation columns.
        best = _hidden_layer_sct(soup)
    for rec in best:
        parts = {k: rec.get(k) for k in
                 ("salary", "bonus", "stock_awards", "option_awards",
                  "non_equity", "pension", "all_other")}
        tot = rec.get("total") or sum(v for v in parts.values() if v)
        rec["total"] = tot or None
        if tot:
            rec["mix"] = {k: round(100.0 * v / tot, 1) if v else 0.0
                          for k, v in parts.items()}
    return best


# ------------------------------------------------------------------ sections
def _section_text(doc, patterns, stop_patterns=None, max_chars=6000):
    soup = parse_html(doc)
    stop_patterns = stop_patterns or [r"^proposal\s", r"^the board of directors",
                                      r"^stock ownership", r"^other matters",
                                      r"^annual meeting", r"^compensation of directors",
                                      r"^pay versus performance", r"^ceo pay ratio"]
    heads = soup.find_all(re.compile(r"^h[1-6]$"))
    for h in heads:
        txt = fetch.strip_tags(h.get_text())
        if any(re.search(p, txt, re.I) for p in patterns):
            chunks = []
            node = h.find_next_sibling()
            while node is not None and len(" ".join(chunks)) < max_chars:
                t = fetch.strip_tags(node.get_text())
                if node.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    if any(re.search(s, t, re.I) for s in stop_patterns):
                        break
                if t:
                    chunks.append(t)
                node = node.find_next_sibling()
            return " ".join(chunks)
    return None


def related_party_section(doc):
    txt = _section_text(doc, [r"related\s+(person|party)\s+trans",
                              r"certain\s+relationships"])
    if not txt:
        # proxies that style headings as <div>/<span>: fall back to flat text.
        # The heading also appears in the table of contents — keep the match
        # whose body segment (until the next major heading) is the longest.
        flat = fetch.strip_tags(doc)
        best_len, best_seg = 0, None
        for m in re.finditer(r"(CERTAIN\s+RELATIONSHIPS\s+AND\s+RELATED|"
                             r"RELATED\s+PERSON\s+TRANSACTIONS)", flat, re.I):
            seg = flat[m.start(): m.start() + 6000]
            stop = re.search(r"(PROPOSAL\s+\d|STOCK\s+OWNERSHIP|PAY\s+VERSUS|"
                             r"OTHER\s+MATTERS|DIRECTOR\s+COMPENSATION|"
                             r"ANNUAL\s+MEETING|AUDIT\s+RELATED)", seg[120:], re.I)
            seg = seg[: 120 + stop.start()] if stop else seg
            if len(seg) > best_len:
                best_len, best_seg = len(seg), seg
        txt = best_seg
    if not txt:
        return None
    # Presentation layer: sentence-level records only — the raw section text
    # never reaches the payload (consumers show generated summaries, not filings).
    mult = {"million": 1e6, "billion": 1e9, "thousand": 1e3}
    records = []
    for s in re.split(r"(?<=[.;])\s+", txt):
        ms = re.findall(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|thousand)?",
                        s, re.I)
        if not ms:
            continue
        vals = [float(a.replace(",", "")) * mult.get((m or "").lower(), 1)
                for a, m in ms]
        yr = re.search(r"\b(19|20)(\d\d)\b", s)
        records.append({"amount_usd": max(vals), "n_amounts": len(vals),
                        "year": int(yr.group(0)) if yr else None})
    records.sort(key=lambda r: -r["amount_usd"])
    return {"records": records[:20],
            "total_usd": sum(r["amount_usd"] for r in records),
            "n_amount_disclosures": len(records),
            "review_policy_stated": bool(re.search(
                r"(polic\w+|procedures|audit\s+committee|approv\w+|review\w+)",
                txt, re.I))}


def metric_weight_tables(doc):
    """CD&A incentive-metric weight tables: rows of (metric, weight%).

    Heuristic: a table where >=3 rows have a trailing percent cell and a
    text first cell, and the percent column sums to 90-110.
    """
    soup = parse_html(doc)
    out = []
    for tbl in soup.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [fetch.strip_tags(c.get_text()) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            pct = None
            for c in reversed(cells):
                m = re.fullmatch(r"([\d.]+)\s*%", c)
                if m:
                    pct = float(m.group(1))
                    break
            if pct is None:
                continue
            label = next((c for c in cells if c and not re.fullmatch(r"[\d.%\s-]+", c)),
                         None)
            if label:
                rows.append((label, pct))
        if len(rows) >= 3:
            s = sum(p for _, p in rows)
            if 90 <= s <= 110:
                out.append({"weights": [{"metric": l, "weight_pct": p}
                                         for l, p in rows], "sum": round(s, 1)})
    return out


def build(cik, filings=None):
    filing = latest_def14a(cik, filings)
    if not filing:
        return {"filing": None, "pvp": None, "sct": [], "related_party": None,
                "metric_weights": [], "notes": ["no DEF 14A"]}
    doc = fetch_doc(cik, filing)
    pvp = pvp_table(cik, doc=doc, filing=filing)
    sct = summary_compensation_table(doc)
    rp = related_party_section(doc)
    mw = metric_weight_tables(doc)
    return {
        "filing": {"accession": filing["accession"], "date": filing["date"],
                   "doc": filing["doc"]},
        "pvp": pvp,
        "sct": sct,
        "related_party": rp,
        "metric_weights": mw,
        "notes": [],
    }
