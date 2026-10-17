"""Deterministic metric extraction for pre-XBRL submissions.

Ported from production ``pre_xbrl_agent/agent.py`` with the LLM removed:
only the *deterministic row geometry* linking path is kept (the upstream
"semantic verifier" branch already linked every number with source
geometry; the model only attested that a table was financial, which is out
of scope here). A model never invents numbers in this pipeline — and now
there is no model at all: labels, values, periods and units are all
read back from immutable evidence IDs assigned by ``parser.py``.

The output keeps the production contract ``pre_xbrl_metrics.v1``
(``tables`` / ``metrics`` / ``derived_metrics`` / ``needs_review`` /
``errors`` / ``coverage``). Differences from production, all deliberate:
- ``extraction_policy.model`` is null and the reasoning-effort/token knobs
  are gone (no LLM glue);
- ``verification.semantic_table_consensus`` is always ``false`` — there is
  no second semantic attestation; the five deterministic checks still
  gate acceptance (``REQUIRED_VERIFICATION_CHECKS``);
- ``tables[].statement_type`` is ``"unknown"`` (production got it from the
  verifier consensus).

Every accepted metric carries the same verification booleans as production:
``source_refs_valid``, ``label_value_alignment_valid``,
``period_alignment_valid``, ``unit_resolved``, ``value_parsed`` — plus the
unit-evidence ladder (``source`` / ``column_propagated`` pass,
``assumed_usd_default`` only with column-currency confirmation, ``none``
rejects to ``needs_review``).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import re

from .catalog import infer_canonical_key, safe_canonical_key
from .parser import (
    EvidenceCell,
    EvidenceTable,
    decode_submission,
    extract_evidence_tables,
    parse_registrant_name,
    parse_sec_documents,
    select_candidate_tables,
    select_filing_documents,
    select_primary_document,
)


YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
MARKER_BEFORE = {"$", "US$", "€", "£", "¥", "(", "-", "+"}
MARKER_AFTER = {")", "%"}

# Every one of these must hold before a metric may leave `needs_review`.
# `value_parsed` is included because a metric whose number could not be parsed
# carries a null normalized value, and a null in `metrics` would violate the
# contract that every accepted number is source-verified. `unit_resolved` is
# satisfied by source evidence, by the currency printed at the head of the same
# column, or by the USD reporting convention of domestic SEC filings; which one
# applied is always reported in `verification.unit_evidence`.
REQUIRED_VERIFICATION_CHECKS = (
    "source_refs_valid",
    "label_value_alignment_valid",
    "period_alignment_valid",
    "unit_resolved",
    "value_parsed",
)


CURRENCY_PREFIX_RE = re.compile(r"^(?:US\s*)?[$€£¥]\s*", flags=re.IGNORECASE)
DASH_PLACEHOLDERS = {"", "-", "—", "–", "--"}


def _parse_decimal(raw: str) -> "Decimal | None":
    text = raw.strip()
    # A currency symbol can sit outside or inside the accounting parentheses:
    # both `$(1,000)` and `($1,000)` occur in legacy filings. Strip on both
    # sides of the parenthesis removal so neither ordering is lost.
    text = CURRENCY_PREFIX_RE.sub("", text).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = CURRENCY_PREFIX_RE.sub("", text).strip()
    text = text.replace(",", "")
    is_percent = text.endswith("%")
    text = text.rstrip("% ").strip()
    if text in DASH_PLACEHOLDERS:
        return None
    # NUMERIC_RE accepts an em/en dash used as a minus sign, so normalize it
    # before Decimal sees it. A bare dash was already returned as None above.
    text = re.sub(r"^[—–]\s*", "-", text)
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if negative:
        value = -value
    if is_percent:
        value /= Decimal(100)
    return value


def _display_parts(table: EvidenceTable, value_cell: EvidenceCell) -> "list[dict]":
    row = next(row for row in table.rows if row.index == value_cell.row_index)
    index = next(i for i, cell in enumerate(row.cells) if cell.cell_id == value_cell.cell_id)
    selected: "list[EvidenceCell]" = []
    if index > 0 and row.cells[index - 1].text in MARKER_BEFORE:
        selected.append(row.cells[index - 1])
    selected.append(value_cell)
    if index + 1 < len(row.cells) and row.cells[index + 1].text in MARKER_AFTER:
        selected.append(row.cells[index + 1])
    return [{"cell_ref": cell.cell_id, "raw": cell.text} for cell in selected]


def _display_raw(parts: "list[dict]") -> str:
    return "".join(part["raw"] for part in parts)


def _unit_details(raw: str, label: str, canonical_key: str) -> dict:
    lower = raw.lower()
    scale = 1
    if "thousand" in lower or re.search(r"\$\s*0{3}(?:['’]s)?|0{3}s?\s+omitted", lower):
        scale = 1_000
    elif "million" in lower:
        scale = 1_000_000
    elif "billion" in lower:
        scale = 1_000_000_000
    if "%" in raw or "percent" in lower:
        kind = "ratio"
        scale = 1
    elif "per share" in lower or "per share" in label.lower() or canonical_key.startswith("eps_"):
        kind = "per_share"
        scale = 1
    elif "share" in lower and "dollar" not in lower:
        kind = "shares"
    elif any(token in lower for token in ("dollar", "usd", "$", "€", "£", "¥")):
        kind = "monetary"
    else:
        kind = "unspecified"
    return {"kind": kind, "scale": scale, "interpretation_is_programmatic": True}


CURRENCY_LEAD_RE = re.compile(r"\s*((?:US\s*)?[$€£¥])", re.IGNORECASE)
FOREIGN_CURRENCY = ("€", "£", "¥")


def _column_currency_part(
    table: EvidenceTable, value_cell: EvidenceCell
) -> "dict | None":
    """Find the currency symbol printed at the head of this value's column.

    Accounting typography prints `$` on the first row of a block and omits it on
    every row below. The symbol is still the source's own statement of the
    currency for that whole column, so following it is reading the filing's
    convention rather than assuming one.
    """
    best: "tuple[EvidenceCell, str] | None" = None
    for cell in table.cells:
        if cell.row_index >= value_cell.row_index:
            continue
        aligned = (
            abs(cell.col_end - value_cell.col_end) <= 1
            or abs(cell.col_start - value_cell.col_start) <= 1
            or cell.col_end == value_cell.col_start - 1
        )
        if not aligned:
            continue
        match = CURRENCY_LEAD_RE.match(cell.text)
        if not match:
            continue
        if best is None or cell.row_index > best[0].row_index:
            best = (cell, match.group(1).strip())
    if best is None:
        return None
    return {"ref": best[0].cell_id, "raw": best[1], "evidence": "column_propagated"}


def _assumed_currency_part(table: EvidenceTable) -> "dict | None":
    """Fall back to the USD reporting convention of domestic SEC filings.

    Only applied when the table already shows at least one explicit `$` mark
    (so the table is genuinely monetary) but no currency is in scope for this
    particular value, and when no other currency appears anywhere in the
    table or its context. This stops a footnote of "Effective January 1" from
    being read as $1.
    """
    has_dollar_evidence = any(
        "$" in cell.text or "USD" in cell.text.upper()
        for cell in table.cells
    ) or any("$" in line.text or "USD" in line.text.upper() for line in table.contexts)
    if not has_dollar_evidence:
        return None
    for cell in table.cells:
        if any(symbol in cell.text for symbol in FOREIGN_CURRENCY):
            return None
    for line in table.contexts:
        if any(symbol in line.text for symbol in FOREIGN_CURRENCY):
            return None
    return {"ref": None, "raw": "USD", "evidence": "assumed_usd_default"}


def _explicit_unit_parts(
    table: EvidenceTable,
    value_cell: EvidenceCell,
    proposed_ref: "str | None" = None,
) -> "list[dict]":
    """Collect exact currency/scale evidence without inventing any."""
    candidates: "list[tuple[str, str]]" = []
    if proposed_ref in table.context_map:
        candidates.append((proposed_ref, table.context_map[proposed_ref].text))
    elif proposed_ref in table.cell_map:
        candidates.append((proposed_ref, table.cell_map[proposed_ref].text))

    row = next(row for row in table.rows if row.index == value_cell.row_index)
    value_index = next(i for i, cell in enumerate(row.cells) if cell.cell_id == value_cell.cell_id)
    if value_index > 0:
        prior = row.cells[value_index - 1]
        if prior.text in {"$", "US$", "€", "£", "¥"}:
            candidates.append((prior.cell_id, prior.text))
    embedded_currency = re.match(r"\s*((?:US\s*)?[$€£¥])", value_cell.text, re.IGNORECASE)
    if embedded_currency:
        candidates.append((value_cell.cell_id, embedded_currency.group(1)))
    if value_cell.text.endswith("%"):
        candidates.append((value_cell.cell_id, "%"))
    prior_unit_cells = [
        cell for cell in table.cells if cell.is_unit and cell.row_index <= value_cell.row_index
    ]
    if prior_unit_cells:
        nearest = max(prior_unit_cells, key=lambda cell: cell.row_index)
        candidates.append((nearest.cell_id, nearest.text))
    for line in reversed(table.contexts):
        if line.is_unit:
            candidates.append((line.context_id, line.text))
            break
    seen = set()
    seen_raw = set()
    result = []
    for ref, raw in candidates:
        if ref not in seen and raw not in seen_raw:
            result.append({"ref": ref, "raw": raw, "evidence": "source"})
            seen.add(ref)
            seen_raw.add(raw)
    if not any(CURRENCY_LEAD_RE.match(part["raw"]) for part in result):
        # No currency on this row: inherit the one printed at the top of the
        # column before falling back to the filing-level USD convention.
        propagated = _column_currency_part(table, value_cell)
        if propagated is not None and propagated["ref"] not in seen:
            result.append(propagated)
        elif not result or not any(part["raw"] == "USD" for part in result):
            assumed = _assumed_currency_part(table)
            if assumed is not None:
                result.append(assumed)
    return result


def unit_evidence_level(parts: "list[dict]") -> str:
    """Strongest-to-weakest provenance of the unit attached to a value."""
    levels = {part.get("evidence", "source") for part in parts}
    if "source" in levels:
        return "source"
    if "column_propagated" in levels:
        return "column_propagated"
    if "assumed_usd_default" in levels:
        return "assumed_usd_default"
    return "none"


def _period_alignment(
    table: EvidenceTable,
    value_cell: EvidenceCell,
    refs: "list[str]",
    layout: str,
) -> "tuple[bool, str]":
    cell_map = table.cell_map
    selected = [cell_map[ref] for ref in refs if ref in cell_map]
    year_cells = [cell for cell in selected if YEAR_RE.search(cell.text)]
    if not year_cells:
        return False, "no explicit four-digit period in period_refs"
    if layout == "metrics_in_columns":
        if not any(cell.row_index == value_cell.row_index for cell in year_cells):
            return False, "transposed table period is not on the value row"
        return True, "explicit row period in transposed table"
    if any(cell.row_index >= value_cell.row_index for cell in year_cells):
        return False, "period header is not above the value row"

    all_headers = [
        cell
        for cell in table.cells
        if cell.row_index < value_cell.row_index and YEAR_RE.search(cell.text)
    ]
    if not all_headers:
        return False, "table has no period headers"
    latest_header_row = max(cell.row_index for cell in all_headers)
    active_headers = [cell for cell in all_headers if cell.row_index >= latest_header_row - 2]

    def distance(cell: EvidenceCell) -> int:
        if cell.col_start <= value_cell.col_end and cell.col_end >= value_cell.col_start:
            return 0
        return min(abs(cell.col_start - value_cell.col_end), abs(value_cell.col_start - cell.col_end))

    best_distance = min(distance(cell) for cell in active_headers)
    chosen_distance = min(distance(cell) for cell in year_cells)
    if chosen_distance > best_distance + 1:
        return False, f"period/value column mismatch ({chosen_distance} vs nearest {best_distance})"
    return True, "explicit period header aligned to value column"


def _label_alignment(
    table: EvidenceTable,
    label_cell: EvidenceCell,
    value_cell: EvidenceCell,
    layout: str,
) -> "tuple[bool, str]":
    if layout == "metrics_in_rows":
        if label_cell.row_index != value_cell.row_index:
            return False, "row label is not on value row"
        if label_cell.col_start >= value_cell.col_start:
            return False, "row label is not left of value"
        row = next(row for row in table.rows if row.index == value_cell.row_index)
        meaningful = [
            cell
            for cell in row.cells
            if not cell.value_id
            and cell.text not in MARKER_BEFORE | MARKER_AFTER
            and not cell.is_unit
            and cell.col_start < value_cell.col_start
        ]
        if not meaningful:
            return False, "row has no explicit text label"
        first_label = min(meaningful, key=lambda cell: cell.col_start)
        valid = first_label.cell_id == label_cell.cell_id
        return valid, "same-row source label" if valid else "label is not the row's primary text label"
    if label_cell.row_index >= value_cell.row_index:
        return False, "column metric label is not above the value"
    overlap = label_cell.col_start <= value_cell.col_end and label_cell.col_end >= value_cell.col_start
    distance = min(
        abs(label_cell.col_start - value_cell.col_end),
        abs(value_cell.col_start - label_cell.col_end),
    )
    valid = overlap or distance <= 1
    return valid, "column header aligned to value" if valid else "column metric label mismatch"


def _nearest_period_ref(table: EvidenceTable, value_cell: EvidenceCell) -> "str | None":
    header_rows: "dict[int, list[EvidenceCell]]" = defaultdict(list)
    for cell in table.cells:
        if cell.is_period and cell.row_index < value_cell.row_index:
            header_rows[cell.row_index].append(cell)
    if not header_rows:
        return None
    latest_row = max(header_rows)
    headers = header_rows[latest_row]

    def distance(cell: EvidenceCell) -> int:
        if cell.col_start <= value_cell.col_end and cell.col_end >= value_cell.col_start:
            return 0
        return min(
            abs(cell.col_start - value_cell.col_end),
            abs(value_cell.col_start - cell.col_end),
        )

    return min(headers, key=lambda cell: (distance(cell), cell.col_start)).cell_id


def _deterministic_row_output(table: EvidenceTable) -> dict:
    """Link ordinary row-oriented statements using source geometry only."""
    mappings = []
    for row in table.rows:
        values = [cell for cell in row.cells if cell.value_id and not cell.is_period]
        if not values:
            continue
        first_value_col = min(cell.col_start for cell in values)
        labels = [
            cell
            for cell in row.cells
            if not cell.value_id
            and not cell.is_unit
            and cell.text not in MARKER_BEFORE | MARKER_AFTER
            and cell.col_start < first_value_col
        ]
        if not labels:
            continue
        label = min(labels, key=lambda cell: cell.col_start)
        links = []
        for value in values:
            period_ref = _nearest_period_ref(table, value)
            if period_ref:
                links.append(
                    {
                        "value_ref": value.value_id,
                        "period_refs": [period_ref],
                        "unit_ref": None,
                    }
                )
        if links:
            mappings.append(
                {
                    "canonical_key": infer_canonical_key(label.text),
                    "label_ref": label.cell_id,
                    "value_links": links,
                }
            )
    return {
        "is_financial_table": True,
        "statement_type": "unknown",
        "layout": "metrics_in_rows",
        "title_context_refs": [],
        "mappings": mappings,
    }


def _statement_consensus(outputs: "list[dict]") -> str:
    values = [item.get("statement_type", "unknown") for item in outputs]
    if not values:
        return "unknown"
    winner, count = Counter(values).most_common(1)[0]
    return winner if count == len(values) else "unknown"


def _hydrate_pass(table: EvidenceTable, output: dict, pass_index: int) -> "list[dict]":
    if not output.get("is_financial_table", False):
        return []
    cell_map = table.cell_map
    value_map = table.value_map
    layout = output.get("layout", "metrics_in_rows")
    hydrated: "list[dict]" = []
    for mapping_index, mapping in enumerate(output.get("mappings", [])):
        label_ref = mapping.get("label_ref")
        label_cell = cell_map.get(label_ref)
        if label_cell is None:
            continue
        proposed_key = mapping.get("canonical_key", "other")
        canonical_key, canonical_ok = safe_canonical_key(proposed_key, label_cell.text)
        for link_index, link in enumerate(mapping.get("value_links", [])):
            value_ref = link.get("value_ref")
            value_cell = value_map.get(value_ref)
            period_refs = list(dict.fromkeys(link.get("period_refs") or []))
            refs_valid = bool(value_cell) and all(ref in cell_map for ref in period_refs)
            label_aligned, label_note = (
                _label_alignment(table, label_cell, value_cell, layout)
                if value_cell
                else (False, "missing value reference")
            )
            aligned, period_note = (
                _period_alignment(table, value_cell, period_refs, layout)
                if value_cell and period_refs
                else (False, "missing value or period reference")
            )
            proposed_unit_ref = link.get("unit_ref")
            unit_parts = _explicit_unit_parts(table, value_cell, proposed_unit_ref) if value_cell else []
            unit_raw = " | ".join(part["raw"] for part in unit_parts) or None
            unit_evidence = unit_evidence_level(unit_parts)
            # `unit_resolved` is the gate; `assumed_usd_default` is an
            # assumption, not source evidence, and is only allowed when the
            # table as a whole is already known to be monetary. A footnote
            # cell like "1" in a doc-wide-policy table has no column-currency
            # to inherit and must not be silently relabelled as dollars.
            explicit_unit = unit_evidence in {"source", "column_propagated"}
            unit_resolved = unit_evidence != "none" and unit_evidence != "assumed_usd_default"
            if unit_evidence == "assumed_usd_default":
                # Upgrade to resolved only when this value lives in a table
                # whose own column currency propagation succeeded elsewhere,
                # confirming the table is monetary.
                if _column_currency_part(table, value_cell) is not None:
                    unit_resolved = True
            if not value_cell:
                continue
            parts = _display_parts(table, value_cell)
            display_raw = _display_raw(parts)
            decimal_value = _parse_decimal(display_raw)
            period_raw = " | ".join(cell_map[ref].text for ref in period_refs if ref in cell_map)
            unit_info = _unit_details(unit_raw or "", label_cell.text, canonical_key)
            base_value = (
                decimal_value * Decimal(unit_info["scale"])
                if decimal_value is not None
                else None
            )
            identity_period_refs = tuple(
                sorted(ref for ref in period_refs if ref in cell_map and YEAR_RE.search(cell_map[ref].text))
            )
            identity = (label_ref, value_ref, identity_period_refs)
            hydrated.append(
                {
                    "identity": identity,
                    "pass_index": pass_index,
                    "mapping_index": mapping_index,
                    "link_index": link_index,
                    "canonical_key": canonical_key,
                    "canonical_key_proposed": proposed_key,
                    "label_ref": label_ref,
                    "label_raw": label_cell.text,
                    "value_ref": value_ref,
                    "value_raw": value_cell.text,
                    "value_display_raw": display_raw,
                    "value_source_parts": parts,
                    "period_refs": period_refs,
                    "period_raw": period_raw,
                    "unit_refs": [part["ref"] for part in unit_parts if part["ref"]],
                    "unit_source_parts": unit_parts,
                    "unit_raw": unit_raw,
                    "normalized_value_in_reported_unit": str(decimal_value) if decimal_value is not None else None,
                    "normalized_value_in_base_unit": str(base_value) if base_value is not None else None,
                    "unit": unit_info,
                    "verification": {
                        "source_refs_valid": refs_valid,
                        "label_value_alignment_valid": label_aligned,
                        "label_value_alignment_note": label_note,
                        "period_alignment_valid": aligned,
                        "period_alignment_note": period_note,
                        "explicit_unit": explicit_unit,
                        "unit_resolved": unit_resolved,
                        "unit_evidence": unit_evidence,
                        "value_parsed": decimal_value is not None,
                        "canonical_label_match": canonical_ok,
                    },
                }
            )
    return hydrated


def _public_metric(table: EvidenceTable, item: dict, status: str) -> dict:
    identity_text = "|".join([item["label_ref"], item["value_ref"], *item["period_refs"]])
    metric_id = hashlib.sha256(f"{table.table_id}|{identity_text}".encode()).hexdigest()[:20]
    verification = dict(item["verification"])
    verification.update({
        "consensus_count": 1,
        "consensus_required": 1,
        "linking_method": "deterministic_row_geometry",
        "semantic_table_consensus": False,
    })
    return {
        "metric_id": metric_id,
        "status": status,
        "canonical_key": item["canonical_key"],
        "canonical_key_proposed": item["canonical_key_proposed"],
        "label_raw": item["label_raw"],
        "value_raw": item["value_raw"],
        "value_display_raw": item["value_display_raw"],
        "unit_raw": item["unit_raw"],
        "period_raw": item["period_raw"],
        "normalized_value_in_reported_unit": item["normalized_value_in_reported_unit"],
        "normalized_value_in_base_unit": item["normalized_value_in_base_unit"],
        "unit_interpretation": item["unit"],
        "source": {
            "table_id": table.table_id,
            "label_ref": item["label_ref"],
            "value_ref": item["value_ref"],
            "value_source_parts": item["value_source_parts"],
            "period_refs": item["period_refs"],
            "unit_refs": item["unit_refs"],
            "unit_source_parts": item["unit_source_parts"],
            "source_filename": table.source_filename,
            "table_html_sha256": table.html_hash,
        },
        "verification": verification,
    }


def _derive_metrics(accepted: "list[dict]") -> "list[dict]":
    by_period: "dict[str, dict[str, dict]]" = defaultdict(dict)
    for metric in accepted:
        key = metric["canonical_key"]
        years = YEAR_RE.findall(metric["period_raw"])
        period_key = years[-1] if years else metric["period_raw"]
        if key != "other" and key not in by_period[period_key]:
            by_period[period_key][key] = metric

    results = []
    formulas = (
        ("gross_margin", "gross_profit", "revenue", "divide"),
        ("operating_margin", "operating_income", "revenue", "divide"),
        ("net_margin", "net_income", "revenue", "divide"),
        ("free_cash_flow", "operating_cf", "capex", "subtract"),
    )
    for period_key, metrics in by_period.items():
        for derived_key, left_key, right_key, operation in formulas:
            if left_key not in metrics or right_key not in metrics:
                continue
            try:
                left = Decimal(metrics[left_key]["normalized_value_in_base_unit"])
                right = Decimal(metrics[right_key]["normalized_value_in_base_unit"])
            except (InvalidOperation, TypeError):
                continue
            if operation == "divide":
                if right == 0:
                    continue
                value = left / right
                unit = "ratio (programmatically derived)"
                formula = f"{left_key} / {right_key}"
            else:
                # Capex is normally displayed as an outflow in parentheses. If it
                # is already negative, adding it is equivalent to OCF - abs(capex).
                value = left + right if right < 0 else left - right
                unit = "base monetary unit (programmatically normalized)"
                formula = f"{left_key} - abs({right_key})"
            results.append(
                {
                    "canonical_key": derived_key,
                    "source_type": "programmatically_derived_not_copied_from_source",
                    "period_key": period_key,
                    "period_raw_inputs": [metrics[left_key]["period_raw"], metrics[right_key]["period_raw"]],
                    "calculated_value": str(value),
                    "unit": unit,
                    "formula": formula,
                    "input_metric_ids": [metrics[left_key]["metric_id"], metrics[right_key]["metric_id"]],
                }
            )

    # ROE/ROA require average beginning/ending balances. Using only ending
    # equity/assets would be a silent methodological error, so the first year
    # without a prior-year balance is intentionally omitted.
    numeric_years = sorted(int(key) for key in by_period if key.isdigit() and len(key) == 4)
    for year in numeric_years:
        current = by_period[str(year)]
        prior = by_period.get(str(year - 1), {})
        if "net_income" not in current:
            continue
        try:
            net_income = Decimal(current["net_income"]["normalized_value_in_base_unit"])
        except (InvalidOperation, TypeError):
            continue
        for derived_key, balance_key in (("roe", "equity"), ("roa", "total_assets")):
            if balance_key not in current or balance_key not in prior:
                continue
            try:
                end_balance = Decimal(current[balance_key]["normalized_value_in_base_unit"])
                begin_balance = Decimal(prior[balance_key]["normalized_value_in_base_unit"])
            except (InvalidOperation, TypeError):
                continue
            average = (begin_balance + end_balance) / Decimal(2)
            if average == 0:
                continue
            results.append(
                {
                    "canonical_key": derived_key,
                    "source_type": "programmatically_derived_not_copied_from_source",
                    "period_key": str(year),
                    "period_raw_inputs": [
                        current["net_income"]["period_raw"],
                        current[balance_key]["period_raw"],
                        prior[balance_key]["period_raw"],
                    ],
                    "calculated_value": str(net_income / average),
                    "unit": "ratio (programmatically derived)",
                    "formula": f"net_income / average(beginning_{balance_key}, ending_{balance_key})",
                    "input_metric_ids": [
                        current["net_income"]["metric_id"],
                        current[balance_key]["metric_id"],
                        prior[balance_key]["metric_id"],
                    ],
                }
            )
    return results


def extract_submission(
    body: bytes,
    filing: "dict",
    scope: str = "core_statements",
    max_tables: "int | None" = None,
) -> dict:
    """Full deterministic pipeline over one complete-submission document.

    ``filing`` carries identity fields (cik, date, form, accession); the
    production LLM consensus/verifier stage is intentionally absent — see
    the module docstring for the contract differences.
    """
    submission_sha = hashlib.sha256(body).hexdigest()
    text = decode_submission(body)
    registrant_name = parse_registrant_name(text)
    documents = parse_sec_documents(text)
    primary = select_primary_document(documents, filing.get("form") or "10-K")
    financial_documents = select_filing_documents(documents, filing.get("form") or "10-K")
    all_tables: "list[EvidenceTable]" = []
    seen_table_hashes = set()
    next_table_number = 1
    for document in financial_documents:
        document_tables = extract_evidence_tables(document, start_number=next_table_number)
        for table in document_tables:
            if table.html_hash not in seen_table_hashes:
                all_tables.append(table)
                seen_table_hashes.add(table.html_hash)
        next_table_number += len(document_tables)
    candidates = select_candidate_tables(
        all_tables,
        scope=scope,
        registrant_name=registrant_name,
    )
    if max_tables is not None:
        candidates = candidates[:max_tables]

    accepted: "list[dict]" = []
    review: "list[dict]" = []
    table_results: "list[dict]" = []
    errors: "list[dict]" = []

    for table in candidates:
        table_accepted = 0
        table_review = 0
        try:
            deterministic_output = _deterministic_row_output(table)
            hydrated = _hydrate_pass(table, deterministic_output, pass_index=0)
        except Exception as exc:  # one broken table must not sink the filing
            errors.append({
                "table_id": table.table_id,
                "purpose": "deterministic_linking",
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            hydrated = []
        for best in hydrated:
            checks = best["verification"]
            deterministic_ok = all(
                checks[key] for key in REQUIRED_VERIFICATION_CHECKS
            )
            status = "accepted" if deterministic_ok else "needs_review"
            public = _public_metric(table, best, status)
            if deterministic_ok:
                accepted.append(public)
                table_accepted += 1
            else:
                review.append(public)
                table_review += 1

        table_results.append(
            {
                "table_id": table.table_id,
                # deterministic linking carries no semantic verdict; the
                # production verifier that could name the statement is dropped
                "statement_type": "unknown",
                "title_raw": [line.text for line in table.contexts[:3]],
                "source_filename": table.source_filename,
                "financial_score": table.financial_score,
                "statement_score": table.statement_score,
                "numeric_source_cells": len(table.value_map),
                "accepted_metrics": table_accepted,
                "review_metrics": table_review,
            }
        )

    derived = _derive_metrics(accepted)
    used_values = {item["source"]["value_ref"] for item in accepted}
    candidate_value_count = sum(len(table.value_map) for table in candidates)
    return {
        "schema_version": "pre_xbrl_metrics.v1",
        "filing": {
            **filing,
            "submission_sha256": submission_sha,
            "registrant_name_raw": registrant_name,
            "downloaded_bytes": len(body),
            "primary_document_type": primary.document_type,
            "primary_document_filename": primary.filename,
            "primary_document_sha256": hashlib.sha256(primary.content.encode(errors="replace")).hexdigest(),
            "financial_documents": [
                {
                    "document_type": document.document_type,
                    "filename": document.filename,
                    "description": document.description,
                }
                for document in financial_documents
            ],
        },
        "extraction_policy": {
            "scope": scope,
            "model": None,
            "deterministic_only": True,
            "linking_method": "deterministic_row_geometry",
            "strict_checks": True,
            "raw_values_are_hydrated_from_source_refs": True,
            "abstain_on_missing_period_or_unit": True,
            "abstain_on_unparsable_value": True,
            "derived_metrics_are_never_labeled_as_source_values": True,
        },
        "tables": table_results,
        "metrics": accepted,
        "derived_metrics": derived,
        "needs_review": review,
        "errors": errors,
        "coverage": {
            "all_html_tables": len(all_tables),
            "candidate_tables": len(candidates),
            "candidate_numeric_cells": candidate_value_count,
            "distinct_accepted_value_refs": len(used_values),
            "accepted_metrics": len(accepted),
            "needs_review_metrics": len(review),
            "note": "Candidate numeric cells include year/page/header cells and are not all financial values.",
        },
    }
