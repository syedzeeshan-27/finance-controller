"""The deterministic proof over an agent-proposed StatementMapping.

The agent proposes structure; this module disposes. The core is V3, the
running-balance chain: within every period, opening balance plus credits
minus debits must reproduce the balance column row by row, to the paisa,
and land exactly on the closing balance. Hiding a real transaction among
noise, swapping the debit/credit columns, misplacing the header, or picking
the wrong balance column all break the chain — so a mapping that passes is
proven, not trusted.

Every check emits a machine-readable code; rejections are fed verbatim back
to the agent so it can repair its proposal. No LLM output is ever consumed
here — cells and mapping in, verdict out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent.cells import parse_amount_cell, parse_date_cell
from agent.mapping import (COLUMN_ROLES, OPTIONAL_COLUMN_ROLES,
                           StatementMapping, extract_reference)


@dataclass
class ValidationReport:
    accepted: bool
    errors: list = field(default_factory=list)     # {"code","row","detail"}
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "errors": self.errors,
                "warnings": self.warnings, "stats": self.stats}


def _err(errors, code, row, detail):
    errors.append({"code": code, "row": row, "detail": detail})


def _cell(grid, row, col):
    return grid[row][col] if col is not None and col < len(grid[row]) else ""


def _rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    return f"{sign}{p // 100}.{p % 100:02d}"


def validate_mapping(grid: list[list[str]],
                     mapping: StatementMapping) -> ValidationReport:
    errors: list = []
    warnings: list = []
    n_rows = len(grid)
    n_cols = len(grid[0]) if grid else 0
    cols = mapping.columns

    # --- V1 structure: columns and the row partition -------------------------
    used_cols = [cols.get(r) for r in COLUMN_ROLES]
    for role in COLUMN_ROLES:
        c = cols.get(role)
        if not isinstance(c, int) or not (0 <= c < n_cols):
            _err(errors, "E_COLUMN_RANGE", -1,
                 f"column {role!r} = {c!r} outside 0..{n_cols - 1}")
    for role in OPTIONAL_COLUMN_ROLES:
        c = cols.get(role)
        if c is not None and (not isinstance(c, int) or not (0 <= c < n_cols)):
            _err(errors, "E_COLUMN_RANGE", -1,
                 f"column {role!r} = {c!r} outside 0..{n_cols - 1}")
    if len(set(used_cols)) != len(used_cols):
        _err(errors, "E_COLUMN_COLLISION", -1,
             f"duplicate column assignment in {cols}")

    seen: dict[int, str] = {}

    def claim(row, label):
        if not (0 <= row < n_rows):
            _err(errors, "E_ROW_RANGE", row, f"{label} outside 0..{n_rows-1}")
            return
        if row in seen:
            _err(errors, "E_ROW_DOUBLE_CLASSIFIED", row,
                 f"row is both {seen[row]} and {label}")
        seen[row] = label

    claim(mapping.header_row, "header")
    for row in mapping.noise_rows:
        claim(row, "noise")
    for k, p in enumerate(mapping.periods):
        claim(p.opening_row, f"period{k}.opening")
        claim(p.closing_row, f"period{k}.closing")
        for row in p.transaction_rows:
            claim(row, f"period{k}.transaction")
    for row in range(n_rows):
        if row not in seen:
            _err(errors, "E_ROW_UNCLASSIFIED", row,
                 f"row not classified: {grid[row]!r}")
    if errors:
        return ValidationReport(False, errors, warnings)

    # --- V2 parse totality per transaction row ------------------------------
    txn_amounts: dict[int, tuple[int, int]] = {}   # row -> (debit, credit)
    for k, p in enumerate(mapping.periods):
        prev_date = None
        for row in p.transaction_rows:
            try:
                d = parse_date_cell(_cell(grid, row, cols["date"]),
                                    mapping.date_format)
                if prev_date is not None and d < prev_date:
                    _err(errors, "E_DATE_ORDER", row,
                         f"{d} before previous row's {prev_date}")
                prev_date = d
            except ValueError:
                _err(errors, "E_DATE_PARSE", row,
                     f"{_cell(grid, row, cols['date'])!r} does not match "
                     f"{mapping.date_format!r}")
            debit_cell = _cell(grid, row, cols["debit"]).strip()
            credit_cell = _cell(grid, row, cols["credit"]).strip()
            if debit_cell and credit_cell:
                _err(errors, "E_BOTH_SIDES", row,
                     "both debit and credit populated")
                continue
            if not debit_cell and not credit_cell:
                _err(errors, "E_NO_AMOUNT", row,
                     "neither debit nor credit populated")
                continue
            try:
                amount, _ = parse_amount_cell(debit_cell or credit_cell)
                txn_amounts[row] = ((amount, 0) if debit_cell
                                    else (0, amount))
            except ValueError:
                _err(errors, "E_AMOUNT_PARSE", row,
                     f"unparseable amount {(debit_cell or credit_cell)!r}")

    # --- V4 the DR/CR indicator qualifies the balance -----------------------
    def balance_at(row) -> int:
        paise, qualifier = parse_amount_cell(_cell(grid, row, cols["balance"]))
        ind_col = cols.get("drcr_indicator")
        if ind_col is not None:
            ind = _cell(grid, row, ind_col).strip().upper().rstrip(".")
            if ind not in ("DR", "CR", ""):
                raise ValueError(f"indicator {ind!r} is neither Dr nor Cr")
            if ind == "DR" or qualifier == "DR":
                return -abs(paise)
        elif qualifier == "DR":
            return -abs(paise)
        return paise

    # --- V3 the balance chain (the core proof) ------------------------------
    period_stats = []
    for k, p in enumerate(mapping.periods):
        try:
            running = balance_at(p.opening_row)
            opening = running
        except ValueError as exc:
            _err(errors, "E_BALANCE_PARSE", p.opening_row, str(exc))
            continue
        credits = debits = 0
        broken = False
        for row in p.transaction_rows:
            if row not in txn_amounts:
                broken = True       # V2 already reported why
                continue
            debit, credit = txn_amounts[row]
            debits += debit
            credits += credit
            running = running + credit - debit
            try:
                stated = balance_at(row)
            except ValueError as exc:
                _err(errors, "E_BALANCE_PARSE", row, str(exc))
                broken = True
                continue
            if stated != running:
                _err(errors, "E_BALANCE_CHAIN_BREAK", row,
                     f"period {k}: expected balance {_rupees(running)} "
                     f"but the statement says {_rupees(stated)}")
                broken = True
                running = stated    # resynchronise: report every break once
        try:
            closing = balance_at(p.closing_row)
            if closing != running:
                _err(errors, "E_CLOSING_MISMATCH", p.closing_row,
                     f"period {k}: chain ends at {_rupees(running)} but "
                     f"closing balance says {_rupees(closing)}")
                broken = True
        except ValueError as exc:
            _err(errors, "E_BALANCE_PARSE", p.closing_row, str(exc))
            broken = True
        period_stats.append({
            "period": k, "opening_paise": opening,
            "closing_paise": closing if not broken else None,
            "transaction_rows": len(p.transaction_rows),
            "credits_paise": credits, "debits_paise": debits,
            "chain_ok": not broken,
        })

    # --- V5 anti-hiding: noise must not look like a transaction -------------
    for row in mapping.noise_rows:
        date_cell = _cell(grid, row, cols["date"])
        amount_cell = (_cell(grid, row, cols["debit"]).strip()
                       or _cell(grid, row, cols["credit"]).strip())
        if not date_cell or not amount_cell:
            continue
        try:
            parse_date_cell(date_cell, mapping.date_format)
            parse_amount_cell(amount_cell)
        except ValueError:
            continue
        _err(errors, "E_TRANSACTION_LIKE_NOISE", row,
             f"noise row has a parseable date and amount: {grid[row]!r}")

    # --- V6 gaps between periods are a fact, not a fault --------------------
    for k in range(1, len(mapping.periods)):
        warnings.append({"code": "W_PERIOD_GAP", "row": -1,
                         "detail": f"statement holds {len(mapping.periods)} "
                                   f"disjoint periods; continuity across the "
                                   f"gap before period {k} is not required"})
        break

    # --- V7 reference recipe and reversal pairs -----------------------------
    tokens: dict[int, str] = {}
    if mapping.reference_recipe.get("kind") != "none":
        try:
            re.compile(mapping.reference_recipe.get("token_regex", ""))
        except re.error as exc:
            _err(errors, "E_TOKEN_REGEX", -1, f"bad token_regex: {exc}")
        else:
            for p in mapping.periods:
                for row in p.transaction_rows:
                    token = extract_reference(
                        _cell(grid, row, cols["narration"]),
                        mapping.reference_recipe)
                    if token:
                        tokens[row] = token
            n_txn = sum(len(p.transaction_rows) for p in mapping.periods)
            if n_txn and not tokens:
                _err(errors, "E_RECIPE_YIELD", -1,
                     "reference recipe extracts nothing from any "
                     "transaction row")
            elif n_txn and len(tokens) < n_txn // 2:
                warnings.append({
                    "code": "W_LOW_RECIPE_YIELD", "row": -1,
                    "detail": f"recipe extracts references from only "
                              f"{len(tokens)}/{n_txn} transaction rows"})
    for pair in mapping.reversal_pairs:
        if len(pair) != 2:
            _err(errors, "E_REVERSAL_PAIR", -1,
                 f"a reversal pair must be exactly two row indices, "
                 f"got {pair!r}")
            continue
        a, b = pair
        ta, tb = tokens.get(a), tokens.get(b)
        amt_a, amt_b = txn_amounts.get(a), txn_amounts.get(b)
        ok = (ta and ta == tb and amt_a and amt_b
              and amt_a[0] == amt_b[1] and amt_a[1] == amt_b[0]
              and amt_a != (0, 0))
        if not ok:
            _err(errors, "E_REVERSAL_PAIR", a,
                 f"rows {a} and {b} do not share a reference with equal "
                 f"and opposite amounts")

    # --- V8 a balance row is a balance row ----------------------------------
    # No debit or credit of its own, and it brackets the period's
    # transactions. Otherwise a real first or last transaction could be
    # renamed "opening" or "closing" and drop out of the chain while the
    # chain still closes. Runs last, only on an otherwise clean mapping, so
    # the error text fed back to the agent on earlier failures is unchanged
    # (the recorded transcripts replay byte for byte).
    if not errors:
        for k, p in enumerate(mapping.periods):
            for label, row in (("opening", p.opening_row),
                               ("closing", p.closing_row)):
                if (_cell(grid, row, cols.get("debit")).strip()
                        or _cell(grid, row, cols.get("credit")).strip()):
                    _err(errors, "E_BALANCE_ROW_HAS_AMOUNT", row,
                         f"period {k}: {label} row carries a debit or "
                         f"credit; a balance row has neither")
            if p.transaction_rows:
                lo, hi = min(p.transaction_rows), max(p.transaction_rows)
                if not (p.opening_row < lo and hi < p.closing_row):
                    _err(errors, "E_PERIOD_ORDER", p.opening_row,
                         f"period {k}: opening row {p.opening_row} and "
                         f"closing row {p.closing_row} must bracket the "
                         f"transaction rows {lo}..{hi}")

    stats = {
        "rows": n_rows,
        "periods": period_stats,
        "transaction_rows": sum(len(p.transaction_rows)
                                for p in mapping.periods),
        "noise_rows": len(mapping.noise_rows),
        "tokens_extracted": len(tokens),
    }
    return ValidationReport(not errors, errors, warnings, stats)
