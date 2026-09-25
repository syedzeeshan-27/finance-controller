"""The published triage policy: which decisions enter the unified queue,
at what severity, with how much money at risk, and what to do about them.

Severity is consequence-based and deterministic:

  S1  a realised bank-side cash error — act today.
  S2  quantified money at risk that needs an external chase (bank, PSP
      or customer).
  S3  review and follow-up — the exposure is contingent or informational.

Queue ordering within a tier: money at risk descending, then source, status
and first record id ascending. Fully deterministic; verify_close re-derives
it from its own copy of this policy.

Money-at-risk mapping (always >= 0; the underlying decision keeps its own
signed discrepancy):

  exception_missing_bank        expected_paise (the credit that never came)
  duplicate_credit              received_paise (the extra posting)
  ambiguous_abstain             expected_paise, else received_paise
  exception_missing_settlement  received_paise (unexplained inflow)
  needs_review                  |discrepancy_paise|
  exception_amount_mismatch     |captured total - order amount| (joined)
  exception_duplicate_payment   payment amount (joined)
  exception_unpaid_order        order amount (joined)
  exception_payment_no_order    payment amount (joined)

Statuses deliberately EXCLUDED from the queue (they live in summary tiles,
not on a human's plate): the matched families, out_of_scope (bank debits on
leg A), non_settlement_credit (benign, counted as such) and normal
order/payment lifecycle outcomes. Overdue-settlement entries (computed by
`controller.overdue`) are never separate queue entries: at the close date
they are by construction a subset of the leg A exceptions, so they FUSE onto
the matching recon item as days_overdue/due_date enrichment.
"""

from __future__ import annotations

from recon import schemas as RS
from controller.schemas import ExceptionItem, SOURCE_RECON_A, SOURCE_RECON_B

# Synthetic statuses (exist only at the controller layer).
NEEDS_REVIEW = RS.CONF_NEEDS_REVIEW            # matched, unexplained residual

SEVERITY: dict[str, int] = {
    # S1 — realised bank-side cash error
    RS.EXCEPTION_MISSING_BANK: 1,
    RS.DUPLICATE_CREDIT: 1,
    # S2 — quantified money at risk, external chase
    RS.AMBIGUOUS_ABSTAIN: 2,
    RS.EXCEPTION_MISSING_SETTLEMENT: 2,
    RS.EXCEPTION_AMOUNT_MISMATCH: 2,
    RS.EXCEPTION_DUPLICATE_PAYMENT: 2,
    # S3 — review / contingent
    NEEDS_REVIEW: 3,
    RS.EXCEPTION_UNPAID_ORDER: 3,
    RS.EXCEPTION_PAYMENT_NO_ORDER: 3,
}

EXCLUDED_STATUSES: frozenset[str] = frozenset({
    RS.MATCHED, RS.MATCHED_SPLIT, RS.MATCHED_MERGED,
    RS.MATCHED_WITH_DISCREPANCY,      # unless needs_review, which overlays
    RS.OUT_OF_SCOPE, RS.NON_SETTLEMENT_CREDIT,
    RS.ORDER_PAID, RS.ORDER_REFUNDED, RS.ORDER_CANCELLED,
    RS.PAYMENT_APPLIED, RS.PAYMENT_REFUNDED, RS.PAYMENT_FAILED,
})

SUGGESTED_ACTIONS: dict[str, str] = {
    RS.EXCEPTION_MISSING_BANK: "Raise with the PSP/bank: settlement processed but "
                               "no credit arrived. Share the UTR and expected amount.",
    RS.EXCEPTION_MISSING_SETTLEMENT: "Verify source of funds with the bank; "
                                     "confirm whether this is an out-of-band payout.",
    RS.DUPLICATE_CREDIT: "Ask the bank to reverse the duplicate posting; "
                         "park the amount in a suspense account meanwhile.",
    RS.AMBIGUOUS_ABSTAIN: "Confirm with the bank which settlement this credit "
                          "belongs to before claiming either.",
    NEEDS_REVIEW: "Matched, but a residual is unexplained — review the "
                  "breakdown and book the difference deliberately.",
    RS.EXCEPTION_AMOUNT_MISMATCH: "Captured amount differs from the order — "
                                  "confirm the intended price with the customer/ops "
                                  "and refund or collect the difference.",
    RS.EXCEPTION_DUPLICATE_PAYMENT: "Two captures against one order — refund the "
                                    "duplicate payment.",
    RS.EXCEPTION_UNPAID_ORDER: "Order created but never paid — follow up with the "
                               "customer or cancel the order.",
    RS.EXCEPTION_PAYMENT_NO_ORDER: "Payment captured with no order behind it — "
                                   "trace how it was created; refund if unowed.",
}

_TITLES: dict[str, str] = {
    RS.EXCEPTION_MISSING_BANK: "Settlement never hit the bank",
    RS.EXCEPTION_MISSING_SETTLEMENT: "Bank credit with no settlement",
    RS.DUPLICATE_CREDIT: "Duplicate bank posting",
    RS.AMBIGUOUS_ABSTAIN: "Ambiguous match — abstained",
    NEEDS_REVIEW: "Matched with unexplained residual",
    RS.EXCEPTION_AMOUNT_MISMATCH: "Payment and order amounts differ",
    RS.EXCEPTION_DUPLICATE_PAYMENT: "Duplicate payment on one order",
    RS.EXCEPTION_UNPAID_ORDER: "Order never paid",
    RS.EXCEPTION_PAYMENT_NO_ORDER: "Payment with no order",
}

def _norm_candidates(candidates: list[dict]) -> list[dict]:
    """One candidate shape whatever the source decision used
    (record_id/amount_paise/reason, or the id/note shape)."""
    out = []
    for c in candidates:
        out.append({
            "id": c.get("record_id") or c.get("id") or "",
            "amount_paise": c.get("amount_paise"),
            "note": c.get("reason") or c.get("note", ""),
        })
    return out


def _detail(d: dict) -> str:
    if d.get("explanation"):
        return d["explanation"]
    ev = d.get("evidence") or []
    return "; ".join(e.get("detail", "") for e in ev[:2])


def items_from_leg_a(decisions: list[dict],
                     attention: list[dict]) -> list[ExceptionItem]:
    """Queue items from Stage 1 decisions (as dicts), with overdue-settlement
    entries (controller.overdue) fused on as enrichment rather than emitted
    separately."""
    att_by_sid = {a["id"]: a for a in attention}
    items: list[ExceptionItem] = []
    for d in decisions:
        status = d["status"]
        if status in RS.MATCHED_FAMILY and d.get("confidence") == RS.CONF_NEEDS_REVIEW:
            status = NEEDS_REVIEW
        if status not in SEVERITY:
            continue
        if status == RS.EXCEPTION_MISSING_BANK:
            money = d.get("expected_paise") or 0
        elif status == RS.DUPLICATE_CREDIT:
            money = d.get("received_paise") or 0
        elif status == RS.AMBIGUOUS_ABSTAIN:
            money = (d.get("expected_paise") if d.get("expected_paise") is not None
                     else d.get("received_paise")) or 0
        elif status == RS.EXCEPTION_MISSING_SETTLEMENT:
            money = d.get("received_paise") or 0
        else:  # needs_review
            money = abs(d.get("discrepancy_paise") or 0)
        record_ids = list(d.get("settlement_ids", [])) + list(d.get("bank_txn_ids", []))
        item = ExceptionItem(
            source=SOURCE_RECON_A, status=status, severity=SEVERITY[status],
            money_at_risk_paise=money, record_ids=record_ids,
            title=_TITLES[status], detail=_detail(d),
            suggested_action=SUGGESTED_ACTIONS[status],
            evidence=list(d.get("evidence", [])),
            candidates=_norm_candidates(d.get("candidates", [])),
        )
        fused = [att_by_sid[s] for s in d.get("settlement_ids", []) if s in att_by_sid]
        if fused:
            item.days_overdue = max(a["days_overdue"] for a in fused)
            item.due_date = min(a["expected_date"] for a in fused)
        items.append(item)
    return items


def items_from_leg_b(decisions: list[dict], orders: list[dict],
                     payments: list[dict]) -> list[ExceptionItem]:
    """Queue items from leg B decisions (as dicts). Amounts are joined from
    the order book / payment file (LegBDecision carries no paise), and the
    order-side + payment-side halves of an amount mismatch fuse into one item."""
    order_amount = {o["order_id"]: o["amount_paise"] for o in orders}
    payment_amount = {p["payment_id"]: p["amount_paise"] for p in payments}
    items: list[ExceptionItem] = []
    mismatch_orders = [d for d in decisions
                       if d["status"] == RS.EXCEPTION_AMOUNT_MISMATCH
                       and d["record_type"] == RS.RT_ORDER]
    mismatch_payments = {d["record_id"]: d for d in decisions
                         if d["status"] == RS.EXCEPTION_AMOUNT_MISMATCH
                         and d["record_type"] == RS.RT_PAYMENT}
    fused_payment_ids: set[str] = set()

    for d in mismatch_orders:
        oid = d["record_id"]
        pids = sorted(pid for pid in d.get("counterparty_ids", [])
                      if pid in mismatch_payments)
        fused_payment_ids.update(pids)
        captured = sum(payment_amount.get(pid, 0) for pid in pids)
        money = abs(captured - order_amount.get(oid, 0))
        items.append(ExceptionItem(
            source=SOURCE_RECON_B, status=RS.EXCEPTION_AMOUNT_MISMATCH,
            severity=SEVERITY[RS.EXCEPTION_AMOUNT_MISMATCH],
            money_at_risk_paise=money, record_ids=[oid] + pids,
            title=_TITLES[RS.EXCEPTION_AMOUNT_MISMATCH], detail=_detail(d),
            suggested_action=SUGGESTED_ACTIONS[RS.EXCEPTION_AMOUNT_MISMATCH],
            evidence=list(d.get("evidence", [])),
            candidates=_norm_candidates(d.get("candidates", [])),
        ))

    for d in decisions:
        status = d["status"]
        if status not in SEVERITY:
            continue
        if status == RS.EXCEPTION_AMOUNT_MISMATCH:
            if d["record_type"] == RS.RT_ORDER or d["record_id"] in fused_payment_ids:
                continue  # already fused above
            money = payment_amount.get(d["record_id"], 0)
        elif status == RS.EXCEPTION_UNPAID_ORDER:
            money = order_amount.get(d["record_id"], 0)
        else:  # duplicate payment / payment with no order
            money = payment_amount.get(d["record_id"], 0)
        items.append(ExceptionItem(
            source=SOURCE_RECON_B, status=status, severity=SEVERITY[status],
            money_at_risk_paise=money,
            record_ids=[d["record_id"]] + sorted(d.get("counterparty_ids", [])),
            title=_TITLES[status], detail=_detail(d),
            suggested_action=SUGGESTED_ACTIONS[status],
            evidence=list(d.get("evidence", [])),
            candidates=_norm_candidates(d.get("candidates", [])),
        ))
    return items


def sort_queue(items: list[ExceptionItem]) -> list[ExceptionItem]:
    return sorted(items, key=lambda i: (
        i.severity, -i.money_at_risk_paise, i.source, i.status,
        i.record_ids[0] if i.record_ids else ""))
