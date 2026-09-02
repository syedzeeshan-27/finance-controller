"""Three-way transaction journey: order -> payment -> settlement -> bank.

Composes leg A and leg B decisions into one trace per order, identifying the
first broken link — the view a finance operator actually wants when asking
"where is my money for this order?".
"""

from __future__ import annotations

from recon import schemas as S

_LEG_B_BREAKS = {
    S.EXCEPTION_UNPAID_ORDER: "payment never captured",
    S.EXCEPTION_AMOUNT_MISMATCH: "captured amount differs from order",
}
_TERMINAL_OK = {S.ORDER_CANCELLED: "cancelled (no money expected)",
                S.ORDER_REFUNDED: "refunded (money returned)"}


def build_journeys(orders: list[dict], payments: list[dict],
                   decisions_a: list[S.Decision],
                   decisions_b: list[S.LegBDecision]) -> list[dict]:
    pay_by_id = {p["payment_id"]: p for p in payments}
    legb_by_order = {d.record_id: d for d in decisions_b
                     if d.record_type == S.RT_ORDER}
    lega_by_setl: dict[str, S.Decision] = {}
    for d in decisions_a:
        for sid in d.settlement_ids:
            lega_by_setl[sid] = d

    rows: list[dict] = []
    for o in orders:
        ob = legb_by_order.get(o["order_id"])
        row = {
            "order_id": o["order_id"], "receipt": o["receipt"],
            "order_amount_paise": o["amount_paise"],
            "order_status": ob.status if ob else "unknown",
            "payment_id": "", "settlement_id": "", "settlement_status": "",
            "bank_txn_ids": "", "first_break": "",
        }
        if ob is None:
            row["first_break"] = "order not assessed"
            rows.append(row)
            continue
        if ob.status in _TERMINAL_OK:
            row["first_break"] = ""
            row["payment_id"] = ";".join(ob.counterparty_ids)
            rows.append(row)
            continue
        if ob.status in _LEG_B_BREAKS:
            row["payment_id"] = ";".join(ob.counterparty_ids)
            row["first_break"] = _LEG_B_BREAKS[ob.status]
            rows.append(row)
            continue

        # order_paid: follow the money
        pid = ob.counterparty_ids[0] if ob.counterparty_ids else ""
        row["payment_id"] = pid
        setl_id = pay_by_id.get(pid, {}).get("settlement_id", "")
        row["settlement_id"] = setl_id
        if not setl_id:
            row["first_break"] = "captured but not yet settled"
            rows.append(row)
            continue
        da = lega_by_setl.get(setl_id)
        if da is None:
            row["settlement_status"] = "unassessed"
            row["first_break"] = "settlement not assessed"
        else:
            row["settlement_status"] = da.status
            row["bank_txn_ids"] = ";".join(da.bank_txn_ids)
            if da.status in S.MATCHED_FAMILY:
                row["first_break"] = ""
            elif da.status == S.EXCEPTION_MISSING_BANK:
                row["first_break"] = "settlement never hit the bank"
            elif da.status == S.AMBIGUOUS_ABSTAIN:
                row["first_break"] = "bank credit ambiguous - needs review"
            else:
                row["first_break"] = f"settlement issue: {da.status}"
        rows.append(row)
    return rows
