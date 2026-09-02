"""The merchant-books side of every tax loop, derived blind from world data.

Nothing here reads an answer key: purchases come from the bank statement and
the vendor registry, the fee invoice expectation from settlement records, TDS
events from Stage 1's reconciliation decisions, and compliance expectations
from the payment records — exactly the sources a real finance team has.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from recon import schemas as RS
from recon.normalize import parse_bank_date, parse_iso_date
from tax import registry as TR
from tax import rules as TX


@dataclass(frozen=True)
class Purchase:
    """One expected input-credit entry in the merchant's books."""
    purchase_id: str           # bank txn_id, or RZPFEE-<period> for fee invoices
    vendor_key: str
    period: str
    invoice_date: date | None
    ref: str | None            # narration invoice reference; None for fee invoices
    taxable_paise: int
    gst_paise: int
    expected_head: str
    itc_eligible: bool
    files_2b: bool
    no_itc_reason: str = ""
    head_split: dict | None = None   # rules.split_heads of gst_paise


@dataclass(frozen=True)
class TdsEvent:
    """One TDS deduction observed at credit by the reconciliation engine."""
    settlement_id: str
    txn_id: str
    credit_date: date
    amount_paid_paise: int     # the pre-deduction settlement net
    tds_paise: int


@dataclass(frozen=True)
class PeriodCheck:
    """One statutory payment obligation the engine can (or cannot) verify."""
    kind: str                  # "gst" | "tds"
    period: str
    due_date: date
    expected_paise: int | None   # None: basis precedes the statement
    paid_txn_id: str
    paid_paise: int | None
    paid_date: date | None


def derive_purchases(bank_rows: list[RS.BankRow],
                     settlements: list[RS.Settlement]) -> list[Purchase]:
    out: list[Purchase] = []
    for r in bank_rows:
        if r.debit_paise <= 0:
            continue
        v = TR.classify_debit(r.narration)
        if v is None:
            continue
        d = parse_bank_date(r.value_date)
        out.append(Purchase(
            purchase_id=r.txn_id, vendor_key=v.key, period=TX.period_of(d),
            invoice_date=d, ref=TX.invoice_ref(r.narration),
            taxable_paise=TX.taxable_from_inclusive(r.debit_paise),
            gst_paise=TX.gst_from_inclusive(r.debit_paise),
            expected_head=TX.head_for(v.state) if v.files_2b else "",
            itc_eligible=v.itc_eligible, files_2b=v.files_2b,
            no_itc_reason=v.no_itc_reason,
            head_split=(TX.split_heads(TX.gst_from_inclusive(r.debit_paise),
                                       TX.head_for(v.state))
                        if v.files_2b else None),
        ))

    monthly: dict[str, list[int]] = {}
    for s in settlements:
        agg = monthly.setdefault(TX.period_of(parse_iso_date(s.created_at)),
                                 [0, 0])
        agg[0] += s.fees_paise
        agg[1] += s.tax_paise
    for period in sorted(monthly):
        fees, tax = monthly[period]
        out.append(Purchase(
            purchase_id=TX.fee_purchase_id(period),
            vendor_key=TR.RAZORPAY.key, period=period, invoice_date=None,
            ref=None, taxable_paise=fees, gst_paise=tax,
            expected_head=TX.head_for(TR.RAZORPAY.state),
            itc_eligible=True, files_2b=True,
            head_split=TX.split_heads(tax, TX.head_for(TR.RAZORPAY.state)),
        ))
    return out


def observed_tds_events(decisions: list[RS.Decision],
                        bank_rows: list[RS.BankRow]) -> list[TdsEvent]:
    """TDS deductions the Stage 1 engine decomposed at credit — the loop-2
    books side is literally the reconciliation's output."""
    date_by_txn = {r.txn_id: parse_bank_date(r.value_date) for r in bank_rows}
    out: list[TdsEvent] = []
    for d in decisions:
        if d.status != RS.MATCHED_WITH_DISCREPANCY:
            continue
        tds = sum(-b["amount_paise"] for b in d.discrepancy_breakdown
                  if b.get("label") == "tds_1pct_of_net")
        if tds <= 0 or not d.settlement_ids or not d.bank_txn_ids:
            continue
        txn = d.bank_txn_ids[0]
        out.append(TdsEvent(
            settlement_id=d.settlement_ids[0], txn_id=txn,
            credit_date=date_by_txn[txn],
            amount_paid_paise=d.expected_paise, tds_paise=tds,
        ))
    out.sort(key=lambda e: (e.credit_date, e.settlement_id))
    return out


def _months_between(start: date, end: date) -> list[tuple[int, int, int]]:
    out = []
    d = start.replace(day=1)
    idx = 0
    while d <= end:
        out.append((idx, d.year, d.month))
        d = (d + timedelta(days=32)).replace(day=1)
        idx += 1
    return out


def compliance_periods(payments: list[dict],
                       bank_rows: list[RS.BankRow]) -> list[PeriodCheck]:
    """Recompute what should have been paid each month, and find what was.

    A period is gradeable only when its statutory deadline lies within the
    observed DEBIT horizon: straggler credits (delayed settlements) can post
    days past the last debit, and calling an obligation "not paid" before
    its due date has even passed inside the observed window would be a false
    alarm, not a finding."""
    if not bank_rows:
        return []
    stmt_dates = [parse_bank_date(r.value_date) for r in bank_rows]
    pay_dates = [parse_iso_date(p["created_at"]) for p in payments]
    start = min(stmt_dates + pay_dates)
    debit_dates = [parse_bank_date(r.value_date) for r in bank_rows
                   if r.debit_paise > 0]
    if not debit_dates:
        return []
    horizon = max(debit_dates)

    gross_by_month: dict[int, int] = {}
    first30 = 0
    for p in payments:
        if p["status"] != "captured":
            continue
        d = parse_iso_date(p["created_at"])
        m = (d.year - start.year) * 12 + d.month - start.month
        gross_by_month[m] = gross_by_month.get(m, 0) + p["amount_paise"]
        if (d - start).days < 30:
            first30 += p["amount_paise"]

    payroll: dict[int, int] = {}
    paid: dict[tuple[str, int], RS.BankRow] = {}
    for r in bank_rows:
        if r.debit_paise <= 0:
            continue
        up = r.narration.upper()
        d = parse_bank_date(r.value_date)
        m = (d.year - start.year) * 12 + d.month - start.month
        if "STAFF PAYROLL" in up and m not in payroll:
            payroll[m] = r.debit_paise
        elif "GST PAYMENT-CBIC" in up and ("gst", m) not in paid:
            paid[("gst", m)] = r
        elif "TDS PAYMENT-CBDT" in up and ("tds", m) not in paid:
            paid[("tds", m)] = r

    out: list[PeriodCheck] = []
    for m, year, month in _months_between(start, horizon):
        period = f"{year:04d}-{month:02d}"
        for kind, due_day in (("gst", TX.GST_DUE_DAY), ("tds", TX.TDS_DUE_DAY)):
            if TX.statutory_deadline(date(year, month, due_day)) > horizon:
                continue                    # not yet due inside the window
            if kind == "gst":
                expected = TX.gst_liability(
                    first30 if m == 0 else gross_by_month.get(m - 1, 0))
            elif m == 0 or (m - 1) not in payroll:
                expected = None            # basis precedes the statement
            else:
                expected = TX.tds_deposit(payroll[m - 1])
            row = paid.get((kind, m))
            out.append(PeriodCheck(
                kind=kind, period=period, due_date=date(year, month, due_day),
                expected_paise=expected,
                paid_txn_id=row.txn_id if row else "",
                paid_paise=row.debit_paise if row else None,
                paid_date=parse_bank_date(row.value_date) if row else None,
            ))
    return out
