"""Turn a PROVEN mapping into a canonical world the untouched pipeline reads.

Only runs after `agent.validator` accepted the mapping. Output is written
with the production schemas' own column orders and money formatter, so the
canonical CSV round-trips through `recon.io_load` — a second, independent
code path over the same numbers (re-proven by intake's post-apply check).
"""

from __future__ import annotations

import csv
import os

from recon import schemas as S
from recon.normalize import rupee_str_from_paise

from agent.cells import parse_amount_cell, parse_date_cell
from agent.mapping import StatementMapping, extract_reference


def _fmt(paise: int) -> str:
    return rupee_str_from_paise(paise, indian_grouping=True)


def apply_mapping(grid: list[list[str]],
                  mapping: StatementMapping) -> list[dict]:
    """-> canonical bank rows (dicts in S.BANK_COLUMNS order)."""
    cols = mapping.columns
    rows: list[dict] = []
    n = 0
    for p in mapping.periods:
        for row in p.transaction_rows:
            n += 1
            date = parse_date_cell(grid[row][cols["date"]],
                                   mapping.date_format)
            narration = grid[row][cols["narration"]]
            debit_cell = grid[row][cols["debit"]].strip()
            credit_cell = grid[row][cols["credit"]].strip()
            amount, _ = parse_amount_cell(debit_cell or credit_cell)
            balance, _ = parse_amount_cell(grid[row][cols["balance"]])
            ref = extract_reference(narration, mapping.reference_recipe)
            if not ref and cols.get("chq_ref") is not None:
                ref = grid[row][cols["chq_ref"]].strip()
            rows.append({
                "txn_id": f"REAL{n:06d}",
                "txn_date": date.strftime("%d/%m/%Y"),
                "value_date": date.strftime("%d/%m/%Y"),
                "narration": narration,
                "ref_no": ref,
                "debit_amount": _fmt(amount) if debit_cell else "",
                "credit_amount": _fmt(amount) if credit_cell else "",
                "balance": _fmt(balance),
            })
    return rows


def write_world(out_dir: str, bank_rows: list[dict]) -> None:
    """Write the canonical world: the real bank statement plus header-only
    PSP-side files (a personal statement has no settlements to declare)."""
    os.makedirs(out_dir, exist_ok=True)

    def _write(name: str, columns: list[str], rows: list[dict]) -> None:
        with open(os.path.join(out_dir, name), "w", encoding="utf-8",
                  newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)

    _write("bank_statement.csv", S.BANK_COLUMNS, bank_rows)
    _write("settlements.csv", S.SETTLEMENTS_COLUMNS, [])
    _write("payments.csv", S.PAYMENTS_COLUMNS, [])
    _write("order_book.csv", S.ORDER_BOOK_COLUMNS, [])
