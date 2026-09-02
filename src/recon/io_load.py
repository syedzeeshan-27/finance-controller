"""CSV loaders: the only place raw generated files become typed records.

Both the engine and every baseline consume these, so no strategy is
advantaged or penalised by parsing differences.
"""

from __future__ import annotations

import csv
import os

from recon import schemas as S
from recon.normalize import paise_from_optional_rupee_str, paise_from_rupee_str


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_settlements(data_dir: str) -> list[S.Settlement]:
    out = []
    for r in _read(os.path.join(data_dir, "settlements.csv")):
        out.append(S.Settlement(
            settlement_id=r["settlement_id"],
            amount_paise=int(r["amount_paise"]),
            fees_paise=int(r["fees_paise"]),
            tax_paise=int(r["tax_paise"]),
            utr=r["utr"],
            payment_count=int(r["payment_count"]),
            status=r["status"],
            created_at=r["created_at"],
            settled_at=r["settled_at"],
        ))
    return out


def load_bank_rows(data_dir: str) -> list[S.BankRow]:
    out = []
    for r in _read(os.path.join(data_dir, "bank_statement.csv")):
        out.append(S.BankRow(
            txn_id=r["txn_id"],
            txn_date=r["txn_date"],
            value_date=r["value_date"],
            narration=r["narration"],
            ref_no=r["ref_no"],
            debit_paise=paise_from_optional_rupee_str(r["debit_amount"]),
            credit_paise=paise_from_optional_rupee_str(r["credit_amount"]),
            balance_paise=paise_from_rupee_str(r["balance"]),
        ))
    return out


def load_payments(data_dir: str) -> list[dict]:
    rows = _read(os.path.join(data_dir, "payments.csv"))
    for r in rows:
        r["amount_paise"] = int(r["amount_paise"])
        r["fee_paise"] = int(r["fee_paise"])
        r["tax_paise"] = int(r["tax_paise"])
    return rows


def load_orders(data_dir: str) -> list[dict]:
    rows = _read(os.path.join(data_dir, "order_book.csv"))
    for r in rows:
        r["amount_paise"] = int(r["amount_paise"])
    return rows


def load_golden(data_dir: str, leg: str) -> list[dict]:
    name = "golden_leg_a.csv" if leg.upper() == "A" else "golden_leg_b.csv"
    rows = _read(os.path.join(data_dir, name))
    for r in rows:
        r["expected_discrepancy_paise"] = int(r["expected_discrepancy_paise"])
        r["counterparty_set"] = frozenset(
            x for x in r["counterparty_ids"].split(S.COUNTERPARTY_SEP) if x)
    return rows
