"""Layer (a): pipeline-known inflows — the finance-controller edge.

Money already captured by the PSP is not a statistical guess, it is a known
future bank credit. This layer reuses the Stage 1 reconciliation engine to
find which settlements are already paid, then projects:

  - processed-but-unpaid settlements at their expected credit date
  - captured payments whose settlement batch does not exist yet, using the
    same T+2 / Sunday-roll mechanics the settlement pipeline uses

Settlements that SHOULD have been paid by the cutoff but were not (the
delayed/missing scenarios) are deliberately excluded from the forecast path
and surfaced on an `attention` list instead: the honest answer is "this money
is at risk", not a silently-forecast credit.
"""

from __future__ import annotations

from datetime import date, timedelta

from recon import schemas as RS
from recon.engine import reconcile_leg_a
from recon.normalize import parse_iso_date, roll_off_sunday
from forecast.schemas import ForecastInput


def run_recon(inp: ForecastInput) -> list[RS.Decision]:
    """The Stage 1 engine over the observable world; shared by this layer and
    the residual layer's credit classification."""
    return reconcile_leg_a(inp.settlements, inp.bank_rows)


def known_inflows(inp: ForecastInput, horizon: int,
                  decisions: list[RS.Decision]) -> dict:
    """Returns {"by_date": {date: paise}, "in_flight": [...], "attention": [...]}."""
    end = inp.cutoff + timedelta(days=horizon)
    by_date: dict[date, int] = {}
    in_flight: list[dict] = []
    attention: list[dict] = []

    unpaid_status = {RS.EXCEPTION_MISSING_BANK, RS.AMBIGUOUS_ABSTAIN}
    status_by_settlement: dict[str, str] = {}
    for d in decisions:
        for sid in d.settlement_ids:
            status_by_settlement[sid] = d.status

    for s in inp.settlements:
        status = status_by_settlement.get(s.settlement_id, "")
        if status not in unpaid_status:
            continue  # already reconciled against a bank credit
        expected = roll_off_sunday(parse_iso_date(s.settled_at))
        if expected <= inp.cutoff:
            attention.append({
                "kind": "overdue_settlement",
                "id": s.settlement_id,
                "amount_paise": s.amount_paise,
                "expected_date": expected.isoformat(),
                "days_overdue": (inp.cutoff - expected).days,
                "note": ("expected credit has not arrived; could be delayed or "
                         "missing - excluded from the forecast, chase with the "
                         "PSP/bank"),
            })
            continue
        if expected <= end:
            by_date[expected] = by_date.get(expected, 0) + s.amount_paise
        in_flight.append({
            "kind": "settlement",
            "id": s.settlement_id,
            "amount_paise": s.amount_paise,
            "expected_date": expected.isoformat(),
        })

    # Captured payments whose settlement batch is not visible yet: they settle
    # at T+2 from capture day, rolled off Sunday — Razorpay mechanics, not
    # statistics. (Method-split batching lands on the same date with the same
    # total, so grouping by capture day alone is exact.)
    known_settlements = {s.settlement_id for s in inp.settlements}
    by_capture_day: dict[date, int] = {}
    for p in inp.payments:
        if p["status"] != "captured" or p["settlement_id"] in known_settlements:
            continue
        d = parse_iso_date(p["created_at"])
        net = p["amount_paise"] - p["fee_paise"] - p["tax_paise"]
        by_capture_day[d] = by_capture_day.get(d, 0) + net

    for capture_day in sorted(by_capture_day):
        expected = roll_off_sunday(capture_day + timedelta(days=2))
        amount = by_capture_day[capture_day]
        if inp.cutoff < expected <= end:
            by_date[expected] = by_date.get(expected, 0) + amount
            in_flight.append({
                "kind": "unsettled_batch",
                "id": f"captures_{capture_day.isoformat()}",
                "amount_paise": amount,
                "expected_date": expected.isoformat(),
            })

    return {"by_date": by_date, "in_flight": in_flight, "attention": attention}
