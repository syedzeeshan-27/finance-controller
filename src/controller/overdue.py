"""Settlements past their expected credit date: queue enrichment, not items.

A settlement the engine left unpaid (no bank credit found, or abstained
between twins) whose expected credit date (settled_at, rolled off Sunday) is
on or before the close date is overdue. These entries never become queue
items of their own: at the close date they are by construction a subset of
the leg A exceptions, so `triage.items_from_leg_a` fuses them onto the
matching recon item as `days_overdue` / `due_date`.

This is the rule the retired cash forecaster used for its "attention" list
(`forecast/pipeline.py` on the `full-scope` branch), kept verbatim so queue
items stay byte-identical.
"""

from __future__ import annotations

from datetime import date

from recon import schemas as RS
from recon.normalize import parse_iso_date, roll_off_sunday

_UNPAID_STATUSES = frozenset({RS.EXCEPTION_MISSING_BANK, RS.AMBIGUOUS_ABSTAIN})


def overdue_settlements(settlements: list[RS.Settlement],
                        decisions: list[RS.Decision],
                        close_date: date) -> list[dict]:
    """-> [{"id", "amount_paise", "expected_date", "days_overdue"}], in
    settlement-file order. Only settlements created on or before the close
    date are considered (a close cannot know about later batches)."""
    status_by_settlement: dict[str, str] = {}
    for d in decisions:
        for sid in d.settlement_ids:
            status_by_settlement[sid] = d.status

    out: list[dict] = []
    for s in settlements:
        if parse_iso_date(s.created_at) > close_date:
            continue
        if status_by_settlement.get(s.settlement_id, "") not in _UNPAID_STATUSES:
            continue
        expected = roll_off_sunday(parse_iso_date(s.settled_at))
        if expected <= close_date:
            out.append({
                "id": s.settlement_id,
                "amount_paise": s.amount_paise,
                "expected_date": expected.isoformat(),
                "days_overdue": (close_date - expected).days,
            })
    return out
