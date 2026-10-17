"""Promise Tracker (lite): guidance sentences from earnings-release exhibits
(8-K Item 2.02 / Ex 99.1), then scored against XBRL actuals where the promise
is machine-comparable (revenue / EPS with a fiscal-year anchor).

Heuristic by design — every promise carries `status`:
  met | missed | pending | unparsed | tracked   (never silently dropped)

Ported rule-for-rule from the production promises module: the verb windows, the
backward-looking-number skips, the prior/updated table-pair handling, the
qualitative-subject ranking and the exhibit-filename ladders all encode
specific issuer filing quirks observed in production.
"""
import re
from datetime import date

from . import fetch

# sentence windows split only at ". " boundaries so "$3.25"-style decimals
# don't truncate the match
GUIDE_RE = re.compile(
    r"(?:(?!\.\s)[\s\S]){0,140}\b(expect(?:s|ed|ing)?|anticipate[sd]?|guidance|guided|"
    r"target[s]?|goal[s]?|forecast|reaffirm[s]?|on\s+track\s+to|aim[s]?\s+to|"
    r"plan[s]?\s+to|project[s]?|intend[s]?|launch(?:s|ed|ing)?|"
    r"roll(?:s|ed)?\s+out|should\s+be|"
    r"will\s+be\s+(?:approximately|around|above|below))\b"
    r"(?:(?!\.\s)[\s\S]){0,220}", re.I)
MONEY_RE = re.compile(r"\$\s?([\d.,]+)\s*(billion|million|bn|m\b)?", re.I)
PCT_RE = re.compile(r"([\d.]+)\s*(?:%|percent)")
FY_RE = re.compile(r"(?:fiscal|fy|full[- ]year|full\s+year|year)\s*(20\d\d)", re.I)
METRIC_RE = re.compile(r"\b(revenue[s]?|sales|gross\s+margin|operating\s+margin|"
                       r"eps|earnings\s+per\s+share|free\s+cash\s+flow|deliveries|"
                       r"units|subscribers|members|net\s+interest\s+income|nii|"
                       r"expenses?|costs?|cash\s+flow|dividends?|tax\s+rate|"
                       r"capex|capital\s+expenditure[s]?)\b", re.I)
# the forward verb inside a GUIDE_RE window — a target number must sit AFTER
# it, else we are reading the backward-looking half of a complex sentence
_GV_RE = re.compile(r"(?i)\b(expect(?:s|ed|ing)?|anticipate[sd]?|guidance|"
                    r"guided|target[s]?|goal[s]?|forecast|reaffirm[s]?|"
                    r"on\s+track\s+to|aim[s]?\s+to|plan[s]?\s+to|project[s]?|"
                    r"intend[s]?|launch(?:s|ed|ing)?|roll(?:s|ed)?\s+out|"
                    r"should\s+be|will\s+be\s+(?:approximately|around|above|below))\b")
# flattened guidance TABLES ("Prior Updated … $80 to $83 billion $82 to $85
# billion"): two consecutive ranges = prior + updated columns, take the later
_PAIR_M = re.compile(r"\$[\d.,]+\s*(?:billion|million|bn|m\b)?(?:\s+to|\s+-)\s*"
                     r"\$?[\d.,]+\s*(?:billion|million|bn|m\b)?\s+\$"
                     r"[\d.,]+\s*(?:billion|million|bn|m\b)?(?:\s+to|\s+-)\s*"
                     r"\$?[\d.,]+\s*(?:billion|million|bn|m\b)?", re.I)
_PAIR_P = re.compile(r"[\d.,]+\s*(?:%|percent)\s+to\s+[\d.,]+\s*(?:%|percent)\s+"
                     r"[\d.,]+\s*(?:%|percent)\s+to\s+[\d.,]+\s*(?:%|percent)", re.I)
# subject vocabulary for the QUALITATIVE tier: issuers like TSLA commit in
# product/production terms, which no XBRL concept can score
SUBJECT_RE = re.compile(r"\b(revenue[s]?|sales|margin[s]?|profit[s]?|eps|earnings|"
                        r"free\s+cash|capex|capital\s+expenditure[s]?|spend\w*|"
                        r"deliver\w*|volume[s]?|unit[s]?|producti\w+|produc(?:e|ing)|"
                        r"launch\w*|start\w*|begin\w*|ramp\w*|deploy\w*|expand\w*|"
                        r"expansion|growth|reach\w*|available|network[s]?|robotaxi|"
                        r"autonomous|subscri\w+|station[s]?|charger[s]?|gwh|"
                        r"subscription[s]?|user[s]?|customer[s]?)\b", re.I)
# rank for the qualitative tier: real deliverables beat verb-ish matches
# (lower value = more meaningful). Unknown stems rank last.
_SUBJECT_RANK = {
    "revenue": 0, "sale": 0, "capex": 0, "production": 1, "volume": 1,
    "deployment": 1, "launch": 1, "delivery": 1, "expansion": 1, "margin": 1,
    "profit": 2, "unit": 2, "robotaxi": 2, "network": 2, "station": 2,
    "charger": 2, "subscri": 2, "autonomous": 3, "customer": 3, "user": 3,
    "gwh": 3, "spend": 3, "available": 4, "reach": 4, "growth": 4, "grow": 4,
    "start": 5, "begin": 5, "ramp": 5, "produc": 5, "expand": 5, "deliver": 5,
}


def _subject_rank(word):
    for stem, rank in _SUBJECT_RANK.items():
        if word.startswith(stem):
            return rank
    return 6


# equity-plan / legal boilerplate that trips the guidance verbs
NOISE_RE = re.compile(r"\b(vest|vesting|restricted\s+stock|forfeit|indemnif|"
                      r"severance|accelerat|clawback|award\s+agreement|"
                      r"performance\s+vesting|customer\s+stor|newsroom|"
                      r"sign\s*(?:up|in))\b", re.I)
# backlog boilerplate ("guidance excludes remaining performance obligations")
# poisons revenue promises — but the phrase alone shouldn't drop an otherwise
# clean commitment sentence, so require the exclusion framing too
RPO_EXCL_RE = re.compile(r"(remaining\s+performance\s+obligation|unrecognized|"
                         r"deferred\s+revenue)\b[^.]{0,80}\b(exclud\w*|"
                         r"not\s+includ\w*|differs)\b|\b(exclud\w*|not\s+includ\w*|"
                         r"differs)\b[^.]{0,80}\b(remaining\s+performance\s+obligation|"
                         r"unrecognized|deferred\s+revenue)\b", re.I)


def match_earnings_8k(filings, since="2022-01-01"):
    # 2.02 required: 9.01 alone matches dozens of non-earnings 8-Ks (JPM)
    return [f for f in filings
            if f["form"] == "8-K" and f["date"] >= since
            and "2.02" in (f["items"] or "")]


def earnings_releases(cik, since="2022-01-01", filings=None, max_docs=14):
    filings = filings or fetch.recent_filings(cik)
    rel = match_earnings_8k(filings, since)
    out = []
    for f in rel[:max_docs]:
        idx = None
        for _ in range(2):                      # transient TLS/5xx seen in-run
            try:
                idx = fetch.filing_index(cik, f["accession"])
                break
            except Exception:
                continue
        if idx is None:
            continue
        htm = [i["name"] for i in idx["directory"]["item"]
               if i["name"].endswith((".htm", ".html"))]
        # exhibit naming varies wildly: ex99-1 / ex-99.1 / ex_991 / exhibit991 /
        # SEC-typo exhbit991 / …991111111 — match the ex…99 family
        base = lambda n: n.split("/")[-1]
        ex = [n for n in htm if "xsl" not in n and
              not base(n).startswith(("R", "wd")) and re.search(
              r"ex[\w.\-]{0,8}99|x99|99\d?\.html?$", base(n), re.I)]
        if len(ex) > 1:
            # prefer the 99.1 press release over 99.2 supplements ("narrative")
            def rank(n):
                s = n.split("/")[-1].lower()
                if re.search(r"99[-_.]?1(?![\d])", s):
                    return 0
                if re.search(r"narrative|press|earn|result", s):
                    return 1
                return 2
            ex.sort(key=rank)
        if not ex:
            # some issuers (TSLA 2024) name it plainly "exhibit.htm", JPM-style
            # docs "pressrelease…" — take any non-boilerplate doc, never the
            # 8-K body itself (named like the accession stem / R*.htm)
            # NVDA-style "...fy27pr.htm" press releases, plain "exhibit.htm",
            # "pressrelease…" docs; segment-anchored so "corporate.htm" won't hit
            stem = f["accession"].replace("-", "") + ".htm"
            ex = [n for n in htm if re.search(
                r"(?:^|[^a-z])(exhibit?|press|pr)[\w.\-]*\.html?$"
                r"|earn[\w.\-]*\.html?$",
                n.split("/")[-1].lower())
                and not n.split("/")[-1].startswith(("R", "xsl", "wd"))
                and not n.endswith(stem)]
        if not ex:
            # last resort: keyword BURIED mid-filename — LLY files its press
            # releases as "jpm_2025guidancexpressrele.htm",
            # "lly-2023financialguidancep.htm" (no segment boundary to anchor)
            kw = re.compile(r"press|earn|guid|result|revenue|outlook|financial",
                            re.I)
            ex = [n for n in htm if kw.search(base(n)) and
                  not base(n).startswith(("R", "wd", "0", "index")) and
                  "-index" not in base(n)]
        if not ex:
            continue
        txt = None
        for doc in ex[:2]:                      # try alternates once
            try:
                txt = fetch.strip_tags(fetch.get_text(
                    fetch.filing_url(cik, f["accession"], doc)))
                break
            except Exception:
                continue
        if not txt:
            continue
        out.append({"date": f["date"], "accession": f["accession"],
                    "exhibit": ex[0], "text": txt})
    return out


def _clean_phrase(phrase, when):
    # cut at time anchors and at capitalized function words (mid-phrase
    # Capitalized stopwords signal a heading/next-clause, not product
    # names — those stay: "Tesla Semi", "Megapack 3")
    phrase = re.split(r"\s+(?:this|next|later|earlier|soon|remain\w*|in\s+20\d|"
                      r"by\s+(?:the\s+)?end|for\s+the|"
                      r"to\s+(?:happen|begin|start|commence|continue)|"
                      r"as\s+(?:the|a|an)\b)\b"
                      r"|\s+(?:is|are|was|were|will|would|shall|that|which|"
                      r"who|whose)\b"
                      r"|\s+(?=(?:With|Without|Every|Each|Our|Your|This|"
                      r"That|These|Those|Despite|Across|Among)\b)", phrase)[0]
    if when != "undated" and when in phrase.lower():
        phrase = phrase[:phrase.lower().index(when)].rstrip()
    prev = None
    while prev != phrase:                        # strip trailing junk words
        prev, phrase = phrase, re.sub(
            r"\s+(?:of|and|the|to|for|with|in|at|by|on|as|both|our|its|be|"
            r"all|a|an|slight(?:ly)?|modest(?:ly)?|include\w*|targeting|"
            r"begins?|(?:in|during|through)\s+\d{4}|\d{1,3}\s*%?|"
            r"by\s+[A-Z][\w]*|by\s+\d{4}|"
            r"approx(?:i?mately|imation|imate)?|estimat(?:e|ed|ion)?|"
            r"expect(?:ed|s|ing)?|anticinat\w+)$"
            r"|\s+(?:and\s+)?(?:raised|lowered|cut|increased|decreased|"
            r"announced|reported|introduced|launched|expanding|expanded|"
            r"adding|added|grew|plans?|planning|builds?|building|unveil\w*|"
            r"deploys?|scales?|ships?|delivers?|rolls?\s*out|continues?)\w*$",
            "", phrase.strip(), flags=re.I)
    return re.sub(r"^[•·–—\s]+", "", phrase.strip())


def _qual_phrase(sent, candidates, when):
    """Subject-noun phrase for the qualitative tier. Complex sentences often
    put the object BEFORE the noun ("…for Optimus… anticipated production"),
    so try each ranked candidate, and when the window yields a bare word,
    backfill the object from the preceding clause ("Optimus production")."""
    bare = None
    for word in candidates:
        pm = re.search(r"(?i)\b" + re.escape(word) + r"\b[^,;.]{0,48}", sent)
        if not pm:
            continue
        raw = pm.group(0)
        if len(raw) >= len(word) + 48:       # window cut mid-word — drop fragment
            raw = re.sub(r"\s+\S*$", "", raw)
        phrase = _clean_phrase(raw, when)
        if len(phrase.split()) > 1:
            return word, phrase
        if bare is None:
            bare = (word, phrase, pm)
    if not bare:
        return None, None
    word, phrase, pm = bare
    pre = sent[max(0, pm.start() - 120):pm.start()].strip()
    m2 = re.search(r"((?:[A-Z][\w'.-]+\s+){0,2}[A-Z][\w'.-]+)$", pre)
    if not m2:                       # else last "of/for/at/our X" object upstream
        obs = re.findall(r"(?:for|of|at|to|including)\s+"
                         r"(?:(?:the|a|an|our|its|their)\s+)?"
                         r"((?:[A-Z][\w'.-]*)(?:\s+[A-Z][\w'.-]*){0,2})", pre)
        if obs:
            m2 = [obs[-1]]
    if m2:
        obj = m2[0].strip(" ,.")
        if len(obj.split()) > 3:
            obj = " ".join(obj.split()[-3:])
        if obj.split()[0] not in ("We", "They", "You", "It", "This", "That",
                                  "These", "Those", "Our", "Their", "His",
                                  "All", "Both", "As", "While", "When"):
            phrase = obj + " " + word
    else:                            # lowercase noun modifier: "revenue growth"
        mods = re.search(r"(revenue|operating|margin|subscriber|membership|net|"
                         r"free|volume|unit|adjusted|gross|interest|cash|energy|"
                         r"streaming|advertising|content|core|services?)\s*$",
                         pre.lower())
        if mods:
            return word, mods.group(1) + " " + phrase
    # context-less bare noun ("launch" alone) — useless to a reader; drop it
    return (word, phrase) if len(phrase.split()) > 1 else (None, None)


def _noun_before_num(sent, pos):
    """Noun phrase right before a target number: 'our target CET1 ratio of
    13%' -> 'cet1 ratio'. Fallback label when the sentence has no GAAP metric
    word and only a weak verb ('expect to reach') to hang the promise on."""
    m = re.search(r"((?:[A-Za-z][\w'.-]*[ \t]+){0,3}[A-Za-z][\w'.-]+)"
                  r"[ \t]*(?:of|at|to)?[ \t]*$", sent[max(0, pos - 48):pos])
    if not m:
        return None
    phrase = m.group(1)
    prev = None
    while prev != phrase:
        prev, phrase = phrase, re.sub(
            r"^(?:our|the|a|an|current|target[s]?|annual|full[- ]year|expected|"
            r"guidance|same|overall|approximately|around|about|total)\s+", "",
            phrase.strip(), flags=re.I)
    words = phrase.split()
    while len(words) > 1 and words[-1].lower() in (
            "of", "at", "to", "and", "or", "in", "for", "with", "on", "by"):
        words.pop()                          # greedy capture may end on a prep
    if words[-1].lower() in ("to", "of", "at", "and", "or", "the", "a", "in",
                             "on", "by", "for", "be", "is", "are", "will",
                             "more", "less", "than", "over", "under", "above",
                             "below", "nearly", "just", "approximately",
                             "around", "about", "between", "within", "up",
                             "down", "reach", "exceed", "grow", "expect"):
        return None
    if re.match(r"^[\d.,-]", words[-1]):      # never name a promise by a digit
        return None
    return " ".join(words[-3:]).lower()


# backward-looking results embedded in guidance narratives: "NII declined 4%
# sequentially", "allowed us to exceed our CET1 target of 13%". Movement verbs
# must sit directly before the number; accomplished verbs may appear anywhere
# in the 110 chars before it.
_PAST_NEAR_RE = re.compile(r"\b(declined?|decreased|increased|grew|rose|fell|"
                           r"dropped|jumped|slipped|improved|worsened|ended|"
                           r"remained|was|were)\b(?:\s+(?:to|of|at|about|"
                           r"approximately|around|nearly|just|over|under))*"
                           r"[ \t]*$", re.I)
_PAST_DONE_RE = re.compile(r"\b(allowed|enabled|delivered|achieved|exceeded|"
                           r"surpassed|accomplished|completed|realized|reached|"
                           r"attained|exceeding)\b", re.I)


def extract_promises(release):
    promises = []
    for m in GUIDE_RE.finditer(release["text"]):
        sent = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(sent) < 40 or NOISE_RE.search(sent) or RPO_EXCL_RE.search(sent):
            continue
        metric = METRIC_RE.search(sent)
        # wholly-retrospective sentences that only tripped GUIDE_RE on a noun
        # ("our CET1 target"): "allowed us to exceed our target of 13%" — gone
        if (_PAST_DONE_RE.search(sent) and not re.search(
                r"\b(expect\w*|anticipate\w*|forecast\w*|project\w*|intend\w*|"
                r"plan\w*|guidance|goal|on\s+track|aim\w*|will|should)\b",
                sent, re.I)):
            continue
        # candidate target numbers, earliest first; numbers that are just
        # reported past results are skipped (never promises)
        nums = sorted([(mm.start(), mm, "m") for mm in
                       MONEY_RE.finditer(sent)] +
                      [(mm.start(), mm, "p") for mm in
                       PCT_RE.finditer(sent)])
        gv = _GV_RE.search(sent)
        vpos = gv.start() if gv else 0
        num, nkind = None, None
        for pos, mm, t in nums:
            if pos < vpos:        # number belongs to the backward half
                continue
            pre = sent[max(0, pos - 110):pos]
            if _PAST_NEAR_RE.search(pre) or _PAST_DONE_RE.search(pre):
                continue
            num, nkind = mm, t
            break
        pr = _PAIR_M.search(sent) or _PAIR_P.search(sent)
        if pr:                    # prior/updated table pair: updated wins
            half = re.compile(r"\$?\s*[\d.,]+\s*(?:billion|million|bn|m\b)?\s*"
                              r"to\s+\$?\s*[\d.,]+\s*(?:billion|million|bn"
                              r"|m\b)?", re.I)
            first = half.search(sent, pr.start())
            second = half.search(sent, first.end()) if first else None
            if second:
                cand = MONEY_RE.search(sent, second.start()) or \
                    PCT_RE.search(sent, second.start())
                if cand and cand.start() >= vpos:
                    num, nkind = cand, ("m" if cand.re is MONEY_RE else "p")
        # anchor to the fiscal year / time phrase NEAREST the target number —
        # a trailing base year ("grew 6%, versus fiscal 2025") must not hijack
        # forward guidance in a next-year release
        npos = num.start() if num is not None else -1

        def _nearest(matches):
            best = None
            for mm in matches:
                if best is None or (npos >= 0 and abs(mm.start() - npos) <
                                    abs(best.start() - npos)):
                    best = mm
            return best
        fym = _nearest(FY_RE.finditer(sent))
        fy = int(fym.group(1)) if fym else None
        anchor = f"FY{fy}" if fy else None
        if fy is None:
            am = _nearest(re.finditer(r"\b(this year|next year|this quarter|next quarter|"
                           r"by (?:the )?end of 20\d\d|in 20\d\d|for (?:fiscal )?20\d\d|"
                           r"20\d\d\s+(?:fiscal|net|full|revenue|sales|expenses?|"
                           r"capex|nii|eps|earnings)|"
                           r"during (?:the )?(?:first|second|third|fourth) quarter(?:\s+of)?\s+20\d\d|"
                           r"in (?:the )?(?:first|second|third|fourth) quarter(?:\s+of)?\s+20\d\d|"
                           r"(?:january|february|march|april|may|june|july|august|"
                           r"september|october|november|december)\s+(?:fiscal\s+)?quarter"
                           r"(?:\s+of)?\s+20\d\d|"
                           r"in (?:the )?(?:first|second|third|fourth) quarter|"
                           r"(?:january|february|march|april|may|june|july|august|"
                           r"september|october|november|december)\s+(?:fiscal\s+)?quarter|"
                           r"over the next [\w-]+ (?:months?|years?|quarters?|weeks?)|"
                           r"during (?:the )?(?:first|second|third|fourth) quarter|"
                           r"in (?:the )?(?:first|second|third|fourth) (?:half|quarter)|"
                           r"full[- ]year|this fiscal year|the current fiscal year|"
                           r"(?:next|following|coming) fiscal year)\b",
                           sent, re.I))
            if am:
                anchor = re.sub(r"\s+", " ", am.group(0)).lower()
                ym = re.search(r"20\d\d", anchor)     # any year inside anchor
                if ym:
                    fy = int(ym.group(0))
                    if not FY_RE.search(sent):
                        anchor = f"FY{fy}"
        # historic/base-year references are not promises: "launched streaming
        # in 2007", "growth of 6%, versus fiscal 2025" — the year must not be
        # over at release time
        if fy and fy < int(release["date"][:4]):
            continue
        quantified = bool(num)
        found = [s.group(1).lower() for s in SUBJECT_RE.finditer(sent)]
        ranked = max(found, key=lambda w: -_subject_rank(
            re.sub(r"s$", "", w))) if found else None
        if quantified:
            # a time anchor is mandatory; FY-anchored ones additionally get
            # scored vs 10-K XBRL actuals
            if not (fy or anchor):
                continue
        else:
            # qualitative tier: a subject + a time anchor make it trackable.
            # Pick the most meaningful subject noun, not the first verb-ish
            # match ("begin production" → production, not begin).
            if not ranked or not anchor:
                continue
        if quantified:
            # label by PROXIMITY to the target first: "revenue rose X and we
            # expect margins to reach 40%" is a margins promise, not revenue
            cand = [(s.end(), s.group(1).lower(), rx is METRIC_RE)
                    for rx in (METRIC_RE, SUBJECT_RE)
                    for s in rx.finditer(sent)
                    if 0 <= num.start() - s.end() <= 60]
            cand = [c for c in cand
                    if c[2] or _subject_rank(re.sub(r"s$", "", c[1])) <= 3]
            if cand:
                metric_word = max(cand)[1]      # closest to the number
            elif metric:
                metric_word = metric.group(1).lower()
            elif ranked and _subject_rank(re.sub(r"s$", "", ranked)) <= 3:
                metric_word = ranked
            else:   # only a weak verb ("expect to reach") — name the noun
                metric_word = _noun_before_num(sent, num.start()) or "guidance"
        else:
            metric_word = ranked
        kind = "quantified" if quantified else "qualitative"
        dirw = re.search(r"\b(at\s+least|above|exceed\w*|more\s+than|greater\s+than|"
                         r"up\s+to|below|under|less\s+than|approximately|around)\b",
                         sent, re.I)
        direction = {"at least": ">=", "above": ">=", "exceed": ">", "more than": ">",
                     "greater than": ">", "up to": "<=", "below": "<=", "under": "<=",
                     "less than": "<", "approximately": "~",
                     "around": "~"}.get((dirw.group(1) if dirw else "").lower(), "~")
        tgt, num_pct, target, dollar_v = None, None, "qualitative", None
        if num is not None and nkind == "m":
            tgt = num.group(1).replace(",", "")
            if not re.sub(r"[.,]", "", tgt):
                continue
            rg = None
            unit = (num.group(2) or "").upper()[:1]
            lo = format(int(tgt), ",") if "." not in tgt else tgt
            rg = re.match(r"\s*to\s+\$?([\d.,]+)\s*(billion|million|bn|m\b)?",
                          sent[num.end():], re.I)
            if rg and re.sub(r"[.,]", "", rg.group(1)):
                up = re.sub(r"[.,]+$", "", rg.group(1))
                unit = unit or (rg.group(2) or "").upper()[:1]
                # '1,300 to 2,850' arrives with commas already -> strip first
                # (mirror of `tgt` above; this crash skip'd 100+ big names)
                upi = up.replace(",", "")
                lo2 = format(int(upi), ",") if "." not in upi else up
                target = "$%s-$%s%s" % (lo, lo2, unit)
            else:
                target = "$" + lo + unit
            dollar_v = float(re.sub(r"[.,]+$", "", tgt) or 0) * \
                {"billion": 1e9, "million": 1e6, "bn": 1e9}.get(
                    (num.group(2) or (rg.group(2) if rg else "") or
                     "").lower(), 1)
        elif num is not None:
            ps = re.sub(r"[.,]+$", "", num.group(1))
            if not ps:
                continue
            num_pct = float(ps)
            rgp = re.match(r"\s*to\s+([\d.,]+)\s*(?:%|percent)",
                           sent[num.end():], re.I)
            if rgp and re.sub(r"[.,]", "", rgp.group(1)):
                target = ps + "-" + re.sub(r"[.,]+$", "",
                                           rgp.group(1)) + "%"
            else:
                target = ps + "%"
        when = anchor or "undated"
        if kind == "qualitative":
            # short noun phrase around the subject ("production of Megapack 3")
            uniq = list(dict.fromkeys(found))
            cands = sorted(uniq, key=lambda w: _subject_rank(re.sub(r"s$", "", w)))
            word, phrase = _qual_phrase(sent, cands, when)
            if not word:
                continue
            metric_word = word
            summary = f"{phrase} ({when})"
        else:
            summary = f"{metric_word} {direction} {target} ({when})"
        promises.append({
            "date": release["date"],
            # presentation layer: generated one-liner, never filing prose
            "summary": summary,
            "kind": kind,
            "anchor": when,
            "direction": direction,
            "metric": metric_word,
            "dollar": dollar_v,
            "pct": num_pct,
            "fy": fy,
            # only quantified + FY-anchored promises can be scored later
            "status": "unparsed" if (kind == "quantified" and fy) else "tracked",
        })
    return promises


def _actual(cik, concept, fy):
    d = fetch.companyconcept(cik, "us-gaap", concept)
    if not d:
        return None
    for unit, facts in d.get("units", {}).items():
        for f in facts:
            if f.get("fp") == "FY" and f.get("fy") == fy and f.get("form") == "10-K":
                return f["val"]
    return None


def score(cik, promises):
    """Compare revenue/EPS dollar promises against 10-K actuals."""
    today = date.today().isoformat()
    for p in promises:
        if not p["fy"] or not p["dollar"] or not p["metric"]:
            continue
        if "revenue" in p["metric"] or "sales" in p["metric"]:
            actual = _actual(cik, "Revenues", p["fy"]) or \
                _actual(cik, "RevenueFromContractWithCustomerExcludingAssessedTax",
                        p["fy"])
        elif "eps" in p["metric"] or "earnings per share" in p["metric"]:
            actual = _actual(cik, "EarningsPerShareDiluted", p["fy"])
        else:
            continue
        if actual is None:
            # FY not reported yet?
            p["status"] = "pending" if f"{p['fy']}-12-31" >= today else "unparsed"
            continue
        tol = 0.97  # guidance usually "approximately"/"at least"
        p["actual"] = actual
        p["status"] = "met" if actual >= p["dollar"] * tol else "missed"
    return promises


def build(cik, since="2022-01-01", filings=None):
    filings = filings or fetch.recent_filings(cik)
    matched = match_earnings_8k(filings, since)
    rels = earnings_releases(cik, since=since, filings=filings, max_docs=40)
    promises = []
    for r in rels:
        promises.extend(extract_promises(r))
    promises = score(cik, promises)
    # GUIDE_RE windows overlap; same release can yield the same promise twice
    seen, uniq = set(), []
    for p in sorted(promises, key=lambda x: x["date"], reverse=True):
        key = (p["date"], p["summary"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    promises = uniq
    n_scored = len([p for p in promises if p["status"] in ("met", "missed")])
    n_met = len([p for p in promises if p["status"] == "met"])
    n_tracked = len([p for p in promises if p["status"] == "tracked"])
    return {
        "promises": promises[:120],
        "n_releases_matched": len(matched),
        "n_releases_scanned": len(rels),
        "scorecard": {"scored": n_scored, "met": n_met,
                      "missed": n_scored - n_met,
                      "tracked": n_tracked,
                      "quantified": len([p for p in promises if p["kind"] == "quantified"]),
                      "qualitative": len([p for p in promises if p["kind"] == "qualitative"]),
                      "bare": len([p for p in promises if p["kind"] == "qualitative"
                                   and len(p["summary"].rsplit(" (", 1)[0].split()) <= 1]),
                      "hit_rate": round(n_met / n_scored, 3) if n_scored else None},
    }
