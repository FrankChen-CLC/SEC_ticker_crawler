"""Canonical metric catalog and conservative label matching rules.

Ported verbatim from production ``pre_xbrl_agent/catalog.py``. The alias
patterns encode hard-won rules: a bad canonical mapping is worse than leaving
a correctly extracted raw line item as ``other``. (The upstream prompt-build
string was LLM glue and is not ported.)
"""
from __future__ import annotations

import re


CANONICAL_KEYS = (
    "revenue",
    "cogs",
    "gross_profit",
    "operating_income",
    "net_income",
    "eps_basic",
    "eps_diluted",
    "total_assets",
    "current_assets",
    "total_liabilities",
    "current_liabilities",
    "equity",
    "cash",
    "long_term_debt",
    "ppe_net",
    "goodwill",
    "inventory",
    "operating_cf",
    "investing_cf",
    "financing_cf",
    "capex",
    "depreciation",
    "buybacks",
    "dividends",
    "other",
)


# These patterns are deliberately narrow. A bad canonical mapping is worse than
# leaving a correctly extracted raw line item as `other`.
ALIASES: "dict[str, tuple[str, ...]]" = {
    "revenue": (
        r"^(net )?(sales|revenue|revenues)$",
        r"^total (net )?(sales|revenue|revenues)$",
        r"^sales and service revenue$",
    ),
    "cogs": (
        r"^cost of (sales|revenue|revenues|goods sold)$",
        r"^cost of products sold$",
    ),
    "gross_profit": (r"^gross profit$", r"^gross margin$"),
    "operating_income": (
        r"^(income|loss) from operations$",
        r"^operating (income|loss)$",
    ),
    "net_income": (
        r"^net (income|loss)$",
        r"^net earnings( \(loss\))?$",
        r"^net (income|loss) attributable to .* shareholders$",
    ),
    "eps_basic": (
        r"^(basic )?(earnings|income|loss) per (common )?share$",
        r"^(earnings|income|loss) per (common )?share- ?basic$",
        r"^basic$",
    ),
    "eps_diluted": (
        r"^diluted (earnings|income|loss) per (common )?share$",
        r"^(earnings|income|loss) per (common )?share- ?diluted$",
        r"^diluted$",
    ),
    "total_assets": (r"^total assets$",),
    "current_assets": (r"^total current assets$",),
    "total_liabilities": (r"^total liabilities$",),
    "current_liabilities": (r"^total current liabilities$",),
    "equity": (
        r"^total (stockholders'|shareholders'|owners') equity$",
        r"^total equity$",
        r"^stockholders' equity$",
    ),
    "cash": (
        r"^cash and cash equivalents$",
        r"^cash and equivalents$",
    ),
    "long_term_debt": (
        r"^long[- ]term debt(,? less current maturities)?$",
        r"^long[- ]term borrowings$",
    ),
    "ppe_net": (
        r"^property,? plant and equipment,? net$",
        r"^property and equipment,? net$",
    ),
    "goodwill": (r"^goodwill$",),
    "inventory": (r"^(total )?inventor(y|ies)(,? net)?$",),
    "operating_cf": (
        r"^net cash (provided by|used in) operating activities$",
        r"^net cash flows? (from|used in) operating activities$",
    ),
    "investing_cf": (
        r"^net cash (provided by|used in) investing activities$",
        r"^net cash flows? (from|used in) investing activities$",
    ),
    "financing_cf": (
        r"^net cash (provided by|used in) financing activities$",
        r"^net cash flows? (from|used in) financing activities$",
    ),
    "capex": (
        r"^(capital expenditures|additions to property,? plant and equipment)$",
        r"^(purchases|acquisitions) of property,? plant and equipment$",
        r"^property,? plant and equipment expenditures$",
    ),
    "depreciation": (
        r"^depreciation( and amortization)?$",
        r"^depreciation,? depletion and amortization$",
    ),
    "buybacks": (
        r"^(repurchases|purchases) of (common )?(stock|shares)$",
        r"^treasury stock (purchases|acquired)$",
    ),
    "dividends": (
        r"^(cash )?dividends( paid)?$",
        r"^dividends paid on common stock$",
    ),
}


def normalize_label(label: str) -> str:
    value = label.lower().replace("’", "'").replace("–", "-")
    value = re.sub(r"\[[0-9a-z]+\]|\([0-9a-z]+\)$", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .:\t")
    return value


def safe_canonical_key(proposed: str, label: str) -> "tuple[str, bool]":
    """Keep a canonical key only when the raw label supports it."""
    if proposed not in CANONICAL_KEYS or proposed == "other":
        return "other", proposed == "other"
    normalized = normalize_label(label)
    for pattern in ALIASES.get(proposed, ()):
        if re.fullmatch(pattern, normalized, flags=re.IGNORECASE):
            return proposed, True
    return "other", False


def infer_canonical_key(label: str) -> str:
    """Apply the same narrow aliases without relying on a model proposal."""
    normalized = normalize_label(label)
    matches = [
        key
        for key, patterns in ALIASES.items()
        if any(re.fullmatch(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns)
    ]
    return matches[0] if len(matches) == 1 else "other"
