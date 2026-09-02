"""The published triage policy: which decisions enter the unified queue,
at what severity, with how much money at risk, and what to do about them.

Severity is consequence-based and deterministic:

  S1  statutory exposure, a realised bank-side cash error, or projected
      insolvency — act today.
  S2  quantified money at risk that needs an external chase (bank, PSP,
      vendor, deductor or customer).
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
  itc_missing_in_2b             |discrepancy| (the GST the vendor never filed)
  duplicate_2b_line             filed_paise (would-be double claim)
  unknown_invoice_in_2b         filed_paise (credit with no purchase behind it)
  itc_amount_mismatch           |discrepancy| (chase the vendor for it)
  itc_head_mismatch             0 (amount right, head wrong)
  tds_missing_in_26as           |discrepancy| (deducted, never deposited)
  tds_duplicate_26as            filed_paise
  tds_unknown_entry             filed_paise
  tds_amount_mismatch           |discrepancy|
  tds_wrong_quarter             0 (right money, wrong bucket)
  not_paid                      books_paise (the whole unpaid liability)
  paid_short                    |discrepancy| (also covers over-payment)
  paid_late                     books_paise (interest-exposure basis)
  unverifiable_prior_period     filed_paise (paid, basis unverifiable)
  forecast_below_threshold      threshold - projected minimum balance

Statuses deliberately EXCLUDED from the queue (they live in summary tiles,
not on a human's plate): the matched families of every loop, out_of_scope
(bank debits on leg A), non_settlement_credit (benign, counted as such),
normal order/payment lifecycle outcomes, paid_on_time, and the deliberate
non-claims (blocked_credit_no_itc, no_itc_applicable) plus the timing note
itc_deferred_next_period, which is the ITC "deferred" tile. Forecast
in_flight/warnings/calibration feed the digest and trust panel. Forecast
`attention` items are never separate queue entries: at the close cutoff they
are by construction a subset of the leg A exceptions, so they FUSE onto the
matching recon item as days_overdue/due_date enrichment.
"""

from __future__ import annotations

from recon import schemas as RS
from tax import schemas as TS
from controller.schemas import (
    ExceptionItem, SOURCE_FORECAST, SOURCE_RECON_A, SOURCE_RECON_B,
    SOURCE_TAX_ITC, SOURCE_TAX_TDS, SOURCE_TAX_OBLIGATION,
)

# Synthetic statuses (exist only at the controller layer).
NEEDS_REVIEW = RS.CONF_NEEDS_REVIEW            # matched, unexplained residual
FORECAST_BELOW_THRESHOLD = "forecast_below_threshold"

SEVERITY: dict[str, int] = {
    # S1 — statutory / realised cash error / projected insolvency
    TS.NOT_PAID: 1,
    TS.PAID_SHORT: 1,
    TS.PAID_LATE: 1,
    RS.EXCEPTION_MISSING_BANK: 1,
    RS.DUPLICATE_CREDIT: 1,
    FORECAST_BELOW_THRESHOLD: 1,
    # S2 — quantified money at risk, external chase
    RS.AMBIGUOUS_ABSTAIN: 2,
    RS.EXCEPTION_MISSING_SETTLEMENT: 2,
    RS.EXCEPTION_AMOUNT_MISMATCH: 2,
    RS.EXCEPTION_DUPLICATE_PAYMENT: 2,
    TS.ITC_MISSING_IN_2B: 2,
    TS.DUPLICATE_2B_LINE: 2,
    TS.UNKNOWN_INVOICE_IN_2B: 2,
    TS.TDS_MISSING_IN_26AS: 2,
    TS.TDS_DUPLICATE_26AS: 2,
    # S3 — review / contingent
    NEEDS_REVIEW: 3,
    RS.EXCEPTION_UNPAID_ORDER: 3,
    RS.EXCEPTION_PAYMENT_NO_ORDER: 3,
    TS.ITC_AMOUNT_MISMATCH: 3,
    TS.ITC_HEAD_MISMATCH: 3,
    TS.TDS_AMOUNT_MISMATCH: 3,
    TS.TDS_WRONG_QUARTER: 3,
    TS.TDS_UNKNOWN_ENTRY: 3,
    TS.UNVERIFIABLE_PRIOR_PERIOD: 3,
}

EXCLUDED_STATUSES: frozenset[str] = frozenset({
    RS.MATCHED, RS.MATCHED_SPLIT, RS.MATCHED_MERGED,
    RS.MATCHED_WITH_DISCREPANCY,      # unless needs_review, which overlays
    RS.OUT_OF_SCOPE, RS.NON_SETTLEMENT_CREDIT,
    RS.ORDER_PAID, RS.ORDER_REFUNDED, RS.ORDER_CANCELLED,
    RS.PAYMENT_APPLIED, RS.PAYMENT_REFUNDED, RS.PAYMENT_FAILED,
    TS.ITC_MATCHED, TS.ITC_DEFERRED_NEXT_PERIOD,
    TS.BLOCKED_CREDIT_NO_ITC, TS.NO_ITC_APPLICABLE,
    TS.TDS_CREDIT_MATCHED, TS.PAID_ON_TIME,
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
    TS.ITC_MISSING_IN_2B: "Vendor has not filed this invoice — chase them before "
                          "the filing deadline; do not claim the credit yet.",
    TS.DUPLICATE_2B_LINE: "Same invoice filed twice — claim once, ask the vendor "
                          "to amend the duplicate line.",
    TS.UNKNOWN_INVOICE_IN_2B: "A credit is filed against you with no purchase "
                              "behind it — do NOT claim; query the vendor/GSTIN.",
    TS.ITC_AMOUNT_MISMATCH: "Vendor filed a different amount — claim the lower "
                            "figure now and chase the vendor for the difference.",
    TS.ITC_HEAD_MISMATCH: "Right amount, wrong tax head — ask the vendor to amend; "
                          "claim under the head as filed only after advice.",
    TS.TDS_MISSING_IN_26AS: "Tax was deducted but never showed up in 26AS — chase "
                            "the deductor for the certificate/deposit.",
    TS.TDS_DUPLICATE_26AS: "Deduction reported twice in 26AS — reconcile with the "
                           "deductor before returns are filed.",
    TS.TDS_UNKNOWN_ENTRY: "26AS shows a credit with no observed deduction — "
                          "verify with the deductor before relying on it.",
    TS.TDS_AMOUNT_MISMATCH: "Deposited TDS differs from what was deducted — "
                            "reconcile the difference with the deductor.",
    TS.TDS_WRONG_QUARTER: "Right deduction, wrong FY quarter in 26AS — ask the "
                          "deductor to file a correction statement.",
    TS.NOT_PAID: "Statutory liability unpaid — pay immediately with interest; "
                 "late fees accrue per day.",
    TS.PAID_SHORT: "Paid amount differs from the computed liability — pay the "
                   "shortfall (or reclaim the excess) with the next return.",
    TS.PAID_LATE: "Paid after the statutory deadline — compute and deposit "
                  "interest for the delay.",
    TS.UNVERIFIABLE_PRIOR_PERIOD: "Payment observed but its basis predates the "
                                  "statement — verify against prior-period books.",
    FORECAST_BELOW_THRESHOLD: "Projected balance breaches the working-capital "
                              "threshold — arrange funding or delay discretionary "
                              "spend before that date.",
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
    TS.ITC_MISSING_IN_2B: "ITC at risk — vendor has not filed",
    TS.DUPLICATE_2B_LINE: "Invoice filed twice in GSTR-2B",
    TS.UNKNOWN_INVOICE_IN_2B: "Unknown invoice filed against you",
    TS.ITC_AMOUNT_MISMATCH: "Vendor filed a different GST amount",
    TS.ITC_HEAD_MISMATCH: "Vendor filed the wrong tax head",
    TS.TDS_MISSING_IN_26AS: "TDS deducted but missing from 26AS",
    TS.TDS_DUPLICATE_26AS: "TDS reported twice in 26AS",
    TS.TDS_UNKNOWN_ENTRY: "26AS credit with no observed deduction",
    TS.TDS_AMOUNT_MISMATCH: "26AS amount differs from deduction",
    TS.TDS_WRONG_QUARTER: "TDS filed in the wrong quarter",
    TS.NOT_PAID: "Statutory payment missing",
    TS.PAID_SHORT: "Statutory payment differs from liability",
    TS.PAID_LATE: "Statutory payment made late",
    TS.UNVERIFIABLE_PRIOR_PERIOD: "Payment basis predates the statement",
    FORECAST_BELOW_THRESHOLD: "Projected balance below threshold",
}

_TAX_SOURCE = {"itc": SOURCE_TAX_ITC, "tds": SOURCE_TAX_TDS,
               "obligation": SOURCE_TAX_OBLIGATION}


def _norm_candidates(candidates: list[dict]) -> list[dict]:
    """One candidate shape whatever the source engine used
    (recon: record_id/amount_paise/reason; tax: id/gst_paise/rejected_because)."""
    out = []
    for c in candidates:
        out.append({
            "id": c.get("record_id") or c.get("id") or "",
            "amount_paise": c.get("amount_paise", c.get("gst_paise")),
            "note": c.get("reason") or c.get("rejected_because") or c.get("note", ""),
        })
    return out


def _detail(d: dict) -> str:
    if d.get("explanation"):
        return d["explanation"]
    ev = d.get("evidence") or []
    return "; ".join(e.get("detail", "") for e in ev[:2])


def items_from_leg_a(decisions: list[dict],
                     attention: list[dict]) -> list[ExceptionItem]:
    """Queue items from Stage 1 decisions (as dicts), with forecast attention
    entries fused on as enrichment rather than emitted separately."""
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


def items_from_tax(decisions: list[dict]) -> list[ExceptionItem]:
    """Queue items from Stage 3 decisions (as dicts)."""
    items: list[ExceptionItem] = []
    for d in decisions:
        status = d["status"]
        if status not in SEVERITY:
            continue
        disc = abs(d.get("discrepancy_paise") or 0)
        filed = d.get("filed_paise") or 0
        books = d.get("books_paise") or 0
        if status in (TS.ITC_MISSING_IN_2B, TS.TDS_MISSING_IN_26AS,
                      TS.ITC_AMOUNT_MISMATCH, TS.TDS_AMOUNT_MISMATCH,
                      TS.PAID_SHORT):
            money = disc
        elif status in (TS.DUPLICATE_2B_LINE, TS.UNKNOWN_INVOICE_IN_2B,
                        TS.TDS_DUPLICATE_26AS, TS.TDS_UNKNOWN_ENTRY,
                        TS.UNVERIFIABLE_PRIOR_PERIOD):
            money = filed
        elif status in (TS.NOT_PAID, TS.PAID_LATE):
            money = books
        else:  # head / quarter: right money, wrong bucket
            money = 0
        items.append(ExceptionItem(
            source=_TAX_SOURCE[d["loop"]], status=status,
            severity=SEVERITY[status], money_at_risk_paise=money,
            record_ids=list(d.get("book_ids", [])) + list(d.get("filed_ids", [])),
            title=_TITLES[status], detail=_detail(d),
            suggested_action=SUGGESTED_ACTIONS[status],
            evidence=list(d.get("evidence", [])),
            candidates=_norm_candidates(d.get("candidates", [])),
        ))
    return items


def threshold_item(forecast_dict: dict) -> ExceptionItem | None:
    """The one synthetic forecast queue entry: emitted only when a threshold
    was set and the projected balance path breaches it."""
    threshold = forecast_dict.get("threshold_paise")
    first_below = forecast_dict.get("first_below_threshold")
    if threshold is None or first_below is None:
        return None
    mb = forecast_dict.get("min_balance") or {}
    money = threshold - (mb.get("paise") or 0)
    return ExceptionItem(
        source=SOURCE_FORECAST, status=FORECAST_BELOW_THRESHOLD,
        severity=SEVERITY[FORECAST_BELOW_THRESHOLD],
        money_at_risk_paise=money, record_ids=["forecast_min_balance"],
        title=_TITLES[FORECAST_BELOW_THRESHOLD],
        detail=(f"projected balance first dips below the threshold on "
                f"{first_below}; minimum {mb.get('paise')} paise on {mb.get('date')}"),
        suggested_action=SUGGESTED_ACTIONS[FORECAST_BELOW_THRESHOLD],
        evidence=[{"rule": "balance_projection",
                   "detail": f"threshold {threshold} paise; min balance "
                             f"{mb.get('paise')} on {mb.get('date')}"}],
        due_date=first_below,
    )


def sort_queue(items: list[ExceptionItem]) -> list[ExceptionItem]:
    return sorted(items, key=lambda i: (
        i.severity, -i.money_at_risk_paise, i.source, i.status,
        i.record_ids[0] if i.record_ids else ""))
