"""Leg B: payment <-> order-book reconciliation.

Simpler than leg A (payments carry an order_id), but the same discipline:
deterministic rules, evidence on every decision, and explicit exceptions for
everything that does not line up — unpaid orders, duplicate captures, orphan
payments, amount mismatches.
"""

from __future__ import annotations

from recon import schemas as S
from recon.normalize import parse_iso_ts


def reconcile_leg_b(payments: list[dict], orders: list[dict]) -> list[S.LegBDecision]:
    decisions: list[S.LegBDecision] = []
    orders_by_id = {o["order_id"]: o for o in orders}

    # Captured payments per order, in capture order: the FIRST equal-amount
    # capture pays the order; later ones are duplicates.
    captured_by_order: dict[str, list[dict]] = {}
    for p in payments:
        if p["status"] == "captured" and p["order_id"] in orders_by_id:
            captured_by_order.setdefault(p["order_id"], []).append(p)
    for plist in captured_by_order.values():
        plist.sort(key=lambda p: (parse_iso_ts(p["created_at"]), p["payment_id"]))

    applied: dict[str, dict] = {}
    mismatched: dict[str, dict] = {}
    refunded_by_order: dict[str, dict] = {}

    def payment_decision(p: dict, status: str, counterparties: list[str],
                         evidence: list[dict],
                         candidates: list[dict] | None = None) -> None:
        decisions.append(S.LegBDecision(
            record_type=S.RT_PAYMENT, record_id=p["payment_id"], status=status,
            counterparty_ids=counterparties, evidence=evidence,
            candidates=candidates or []))

    for p in payments:
        oid = p["order_id"]
        order = orders_by_id.get(oid)

        if p["status"] == "failed":
            payment_decision(p, S.PAYMENT_FAILED, [oid] if order else [],
                             [{"rule": "status_failed",
                               "detail": "failed capture; never settled, "
                                         "nothing to apply"}])
            continue
        if order is None:
            payment_decision(p, S.EXCEPTION_PAYMENT_NO_ORDER, [],
                             [{"rule": "unknown_order",
                               "detail": f"order_id {oid!r} matches no order "
                                         "in the book"}],
                             candidates=[{"record_id": "",
                                          "reason": "no order reference to follow; "
                                                    "needs manual investigation"}])
            continue
        if p["status"] == "refunded":
            refunded_by_order[oid] = p
            payment_decision(p, S.PAYMENT_REFUNDED, [oid],
                             [{"rule": "refunded",
                               "detail": "captured then fully refunded before "
                                         "settlement"}])
            continue

        # captured against a known order
        if p["amount_paise"] != order["amount_paise"]:
            mismatched.setdefault(oid, p)
            payment_decision(p, S.EXCEPTION_AMOUNT_MISMATCH, [oid],
                             [{"rule": "amount_mismatch",
                               "detail": f"captured {p['amount_paise']} paise vs "
                                         f"order {order['amount_paise']} paise"}])
            continue
        first = captured_by_order[oid][0]
        if first["payment_id"] != p["payment_id"]:
            payment_decision(p, S.EXCEPTION_DUPLICATE_PAYMENT, [oid],
                             [{"rule": "duplicate_capture",
                               "detail": f"order already paid by {first['payment_id']} "
                                         f"at {first['created_at']}"}],
                             candidates=[{"record_id": first["payment_id"],
                                          "reason": "earlier capture that already "
                                                    "paid this order"}])
            continue
        applied[oid] = p
        payment_decision(p, S.PAYMENT_APPLIED, [oid],
                         [{"rule": "applied",
                           "detail": "amounts equal; first capture against "
                                     "the order"}])

    for o in orders:
        oid = o["order_id"]
        if o["status"] == "cancelled":
            status, cps = S.ORDER_CANCELLED, []
            ev = [{"rule": "cancelled", "detail": "order cancelled before payment"}]
        elif oid in applied:
            status, cps = S.ORDER_PAID, [applied[oid]["payment_id"]]
            ev = [{"rule": "paid", "detail": f"paid by {applied[oid]['payment_id']}"}]
        elif oid in mismatched:
            status, cps = S.EXCEPTION_AMOUNT_MISMATCH, [mismatched[oid]["payment_id"]]
            ev = [{"rule": "amount_mismatch",
                   "detail": "the only capture against this order is for a "
                             "different amount"}]
        elif oid in refunded_by_order:
            status, cps = S.ORDER_REFUNDED, [refunded_by_order[oid]["payment_id"]]
            ev = [{"rule": "refunded", "detail": "payment captured then refunded"}]
        else:
            status, cps = S.EXCEPTION_UNPAID_ORDER, []
            ev = [{"rule": "no_capture",
                   "detail": "no captured payment exists for this order"}]
        decisions.append(S.LegBDecision(
            record_type=S.RT_ORDER, record_id=oid, status=status,
            counterparty_ids=cps, evidence=ev))
    return decisions
