"""Shared shapes and vocabulary for the tax-matching loops.

Exactly like `recon.schemas`: one vocabulary that the generator records as
*expected* dispositions at mint time and the engine emits as *decided*
statuses, so the grader compares like with like. Amounts are integer paise.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from recon.schemas import (  # noqa: F401  (re-exported tiers)
    CONF_EXACT, CONF_HIGH, CONF_MEDIUM, CONF_NEEDS_REVIEW, COUNTERPARTY_SEP,
)

# --- Disposition vocabulary ---------------------------------------------------

# Loop 1 — input GST credit (merchant books vs GSTR-2B)
ITC_MATCHED = "itc_matched"
ITC_AMOUNT_MISMATCH = "itc_amount_mismatch"          # vendor filed a different amount
ITC_HEAD_MISMATCH = "itc_head_mismatch"              # vendor filed the wrong tax head
ITC_DEFERRED_NEXT_PERIOD = "itc_deferred_next_period"  # filed late; claim next period
ITC_MISSING_IN_2B = "itc_missing_in_2b"              # vendor never filed; ITC at risk
DUPLICATE_2B_LINE = "duplicate_2b_line"              # same invoice filed twice; claim once
UNKNOWN_INVOICE_IN_2B = "unknown_invoice_in_2b"      # no purchase behind it; do NOT claim
BLOCKED_CREDIT_NO_ITC = "blocked_credit_no_itc"      # Sec 17(5): in 2B, must not claim
NO_ITC_APPLICABLE = "no_itc_applicable"              # exempt / non-GST / unregistered

# Loop 2 — TDS credits (observed deductions vs Form 26AS)
TDS_CREDIT_MATCHED = "tds_credit_matched"
TDS_AMOUNT_MISMATCH = "tds_amount_mismatch"
TDS_WRONG_QUARTER = "tds_wrong_quarter"              # right deduction, wrong FY quarter
TDS_MISSING_IN_26AS = "tds_missing_in_26as"          # deducted but never deposited/filed
TDS_DUPLICATE_26AS = "tds_duplicate_26as"            # same deduction reported twice
TDS_UNKNOWN_ENTRY = "tds_unknown_entry"              # filed credit with no observed deduction

# Loop 3 — obligation compliance (recomputed liability vs actual payment)
PAID_ON_TIME = "paid_on_time"
PAID_SHORT = "paid_short"
PAID_LATE = "paid_late"
NOT_PAID = "not_paid"
UNVERIFIABLE_PRIOR_PERIOD = "unverifiable_prior_period"  # basis precedes the world

# Matched family: decisions that positively link a books record to a filed
# record (grading requires status AND counterparty-set equality).
TAX_MATCHED_FAMILY = frozenset({
    ITC_MATCHED, ITC_AMOUNT_MISMATCH, ITC_HEAD_MISMATCH,
    ITC_DEFERRED_NEXT_PERIOD, TDS_CREDIT_MATCHED, TDS_AMOUNT_MISMATCH,
    TDS_WRONG_QUARTER,
})

ITC_STATUSES = frozenset({
    ITC_MATCHED, ITC_AMOUNT_MISMATCH, ITC_HEAD_MISMATCH,
    ITC_DEFERRED_NEXT_PERIOD, ITC_MISSING_IN_2B, DUPLICATE_2B_LINE,
    UNKNOWN_INVOICE_IN_2B, BLOCKED_CREDIT_NO_ITC, NO_ITC_APPLICABLE,
})
TDS_STATUSES = frozenset({
    TDS_CREDIT_MATCHED, TDS_AMOUNT_MISMATCH, TDS_WRONG_QUARTER,
    TDS_MISSING_IN_26AS, TDS_DUPLICATE_26AS, TDS_UNKNOWN_ENTRY,
})
OBLIGATION_STATUSES = frozenset({
    PAID_ON_TIME, PAID_SHORT, PAID_LATE, NOT_PAID, UNVERIFIABLE_PRIOR_PERIOD,
})

# Statuses under which the engine recommends claiming the credit NOW.
# Everything else is either not claimable or deferred.
CLAIMABLE_NOW = frozenset({ITC_MATCHED, ITC_HEAD_MISMATCH, ITC_AMOUNT_MISMATCH})
# (amount mismatch: claim the LOWER of books/2B — the conservative figure —
#  and chase the vendor for the difference; the decision carries both sides.)

# --- CSV columns --------------------------------------------------------------

GSTR2B_COLUMNS = [
    "line_id", "period", "gstin", "vendor_name", "invoice_no", "invoice_date",
    "taxable_value_paise", "gst_paise", "tax_head",
]

FORM26AS_COLUMNS = [
    "entry_id", "tan", "deductor_name", "section", "fy_quarter",
    "credit_date", "amount_paid_paise", "tds_paise",
]

# record_type values in the generator's expected-disposition file for tax
# records (same 7-column shape recon's answer keys use). Only the benchmark
# grader reads that file — a test asserts no other tax module mentions it.
RT_PURCHASE = "purchase"
RT_2B_LINE = "gstr2b_line"
RT_TDS_EVENT = "tds_event"
RT_26AS_ENTRY = "form26as_line"
RT_OBLIGATION_PERIOD = "obligation_period"


# --- In-memory records --------------------------------------------------------

@dataclass
class TaxDecision:
    """One tax-matching decision. The engine, the naive baseline and the
    grader all use this exact shape, so every strategy is graded identically.
    discrepancy_paise is filed - books (negative = filed/paid short)."""
    loop: str                             # "itc" | "tds" | "obligation"
    kind: str                             # "match" | "exception" | "info"
    status: str
    book_ids: list[str] = field(default_factory=list)   # purchase ids / settlement ids / period ids
    filed_ids: list[str] = field(default_factory=list)  # 2B line ids / 26AS entry ids / bank txn ids
    confidence: str = ""                  # tier for matched-family, else ""
    books_paise: int | None = None
    filed_paise: int | None = None
    discrepancy_paise: int | None = None
    discrepancy_breakdown: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)    # [{"rule":..., "detail":...}]
    candidates: list[dict] = field(default_factory=list)
    pass_name: str = ""
    explanation: str = ""                 # display-only; excluded from grading

    def to_dict(self) -> dict:
        return asdict(self)


def tax_decisions_grading_view(decisions: list[TaxDecision]) -> list[dict]:
    out = []
    for d in decisions:
        row = d.to_dict()
        row.pop("explanation", None)
        out.append(row)
    return out
