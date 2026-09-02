"""Loaders and the shared TaxInput builder.

Every strategy — the tax engine, the naive baseline, the dashboard — consumes
`build_tax_input`, so parsing is never the differentiator. The answer keys
are NOT loaded here; only the benchmark grader reads those.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field

from recon import io_load
from recon.engine import reconcile_leg_a
from tax import books


@dataclass
class TaxInput:
    purchases: list[books.Purchase] = field(default_factory=list)
    gstr2b: list[dict] = field(default_factory=list)
    tds_events: list[books.TdsEvent] = field(default_factory=list)
    form26as: list[dict] = field(default_factory=list)
    periods: list[books.PeriodCheck] = field(default_factory=list)


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_gstr2b(data_dir: str) -> list[dict]:
    rows = _read(os.path.join(data_dir, "gstr2b.csv"))
    for r in rows:
        r["taxable_value_paise"] = int(r["taxable_value_paise"])
        r["gst_paise"] = int(r["gst_paise"])
    return rows


def load_form26as(data_dir: str) -> list[dict]:
    rows = _read(os.path.join(data_dir, "form26as.csv"))
    for r in rows:
        r["amount_paid_paise"] = int(r["amount_paid_paise"])
        r["tds_paise"] = int(r["tds_paise"])
    return rows


def build_tax_input(data_dir: str, *, decisions_a=None, bank_rows=None,
                    settlements=None, payments=None) -> TaxInput:
    """Derive the books side (running Stage 1 reconciliation for the TDS
    events) and load the filed side.

    The keyword seams let the unified controller inject records and leg A
    decisions it already computed so one close runs one reconciliation; any
    argument left as None falls back to loading/computing here, and the
    result is identical either way (the injection-equivalence test pins it).
    """
    if bank_rows is None:
        bank_rows = io_load.load_bank_rows(data_dir)
    if settlements is None:
        settlements = io_load.load_settlements(data_dir)
    if payments is None:
        payments = io_load.load_payments(data_dir)
    decisions = (decisions_a if decisions_a is not None
                 else reconcile_leg_a(settlements, bank_rows))
    return TaxInput(
        purchases=books.derive_purchases(bank_rows, settlements),
        gstr2b=load_gstr2b(data_dir),
        tds_events=books.observed_tds_events(decisions, bank_rows),
        form26as=load_form26as(data_dir),
        periods=books.compliance_periods(payments, bank_rows),
    )
