"""World slicing: the ONLY place forecast code touches files, and the wall
that makes leakage structurally impossible.

`load_world` reads a generated world once; `build_input` cuts it strictly at
the cutoff date. Everything downstream operates on the in-memory
ForecastInput — a forecaster physically cannot see past the cutoff because
nothing past the cutoff exists in its input.
"""

from __future__ import annotations

from datetime import date

from recon import io_load
from recon.normalize import parse_bank_date, parse_iso_date
from forecast.schemas import ForecastInput


def load_world(data_dir: str) -> dict:
    return {
        "bank_rows": io_load.load_bank_rows(data_dir),
        "settlements": io_load.load_settlements(data_dir),
        "payments": io_load.load_payments(data_dir),
    }


def build_input(world: dict, cutoff: date) -> ForecastInput:
    bank_rows = [r for r in world["bank_rows"]
                 if parse_bank_date(r.value_date) <= cutoff]
    settlements = [s for s in world["settlements"]
                   if parse_iso_date(s.created_at) <= cutoff]
    payments = [p for p in world["payments"]
                if parse_iso_date(p["created_at"]) <= cutoff]
    opening = bank_rows[-1].balance_paise if bank_rows else 0
    first = parse_bank_date(bank_rows[0].value_date) if bank_rows else None
    return ForecastInput(cutoff=cutoff, opening_balance_paise=opening,
                         first_statement_date=first, bank_rows=bank_rows,
                         settlements=settlements, payments=payments)


def reslice(inp: ForecastInput, new_cutoff: date) -> ForecastInput:
    """An earlier view of an already-sliced input (used by band calibration:
    internal cutoffs only ever look further back, never forward)."""
    assert new_cutoff <= inp.cutoff
    bank_rows = [r for r in inp.bank_rows
                 if parse_bank_date(r.value_date) <= new_cutoff]
    return ForecastInput(
        cutoff=new_cutoff,
        opening_balance_paise=bank_rows[-1].balance_paise if bank_rows else 0,
        first_statement_date=(parse_bank_date(bank_rows[0].value_date)
                              if bank_rows else None),
        bank_rows=bank_rows,
        settlements=[s for s in inp.settlements
                     if parse_iso_date(s.created_at) <= new_cutoff],
        payments=[p for p in inp.payments
                  if parse_iso_date(p["created_at"]) <= new_cutoff],
    )


def daily_net_series(inp: ForecastInput) -> list[tuple[date, int]]:
    """Continuous daily net-flow series over the observed history, zeros for
    days with no statement rows. Shared by baselines and the residual layer."""
    if inp.first_statement_date is None:
        return []
    flows: dict[date, int] = {}
    for r in inp.bank_rows:
        d = parse_bank_date(r.value_date)
        flows[d] = flows.get(d, 0) + r.credit_paise - r.debit_paise
    out = []
    d = inp.first_statement_date
    while d <= inp.cutoff:
        out.append((d, flows.get(d, 0)))
        d = d.fromordinal(d.toordinal() + 1)
    return out
