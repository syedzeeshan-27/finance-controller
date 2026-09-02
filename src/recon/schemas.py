"""Shared record shapes and vocabulary for the reconciliation pipeline.

Everything that crosses a module boundary is defined here once: CSV column
orders, the disposition/status vocabulary shared by the generator's ground
truth and the engine's output, and the Decision record that the engine, both
baselines, the benchmark grader and the UI all speak.

Amounts are integer paise everywhere in memory. Only the bank statement CSV
carries rupee-decimal strings (that heterogeneity is deliberate: normalising a
real bank export is part of the job the engine is graded on).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# --- Disposition / status vocabulary ----------------------------------------
# One shared vocabulary: the generator writes these into the golden files as
# *expected* dispositions, the engine emits them as *decided* statuses, and the
# benchmark grades one against the other. Keeping them identical by
# construction removes a whole class of mapping bugs.

MATCHED = "matched"
MATCHED_SPLIT = "matched_split"                     # one settlement -> N bank credits
MATCHED_MERGED = "matched_merged"                   # N settlements -> one bank credit
MATCHED_WITH_DISCREPANCY = "matched_with_discrepancy"
DUPLICATE_CREDIT = "duplicate_credit"               # second posting of an already-settled credit
AMBIGUOUS_ABSTAIN = "ambiguous_abstain"             # candidates indistinguishable; do not guess
EXCEPTION_MISSING_BANK = "exception_missing_bank"   # settlement never hit the bank
EXCEPTION_MISSING_SETTLEMENT = "exception_missing_settlement"  # credit with no settlement
NON_SETTLEMENT_CREDIT = "non_settlement_credit"     # unrelated inflow; must not be claimed
OUT_OF_SCOPE = "out_of_scope"                       # bank debits (leg A reconciles credits)

MATCHED_FAMILY = frozenset({
    MATCHED, MATCHED_SPLIT, MATCHED_MERGED, MATCHED_WITH_DISCREPANCY,
})

# Everything the engine can emit for leg A.
LEG_A_STATUSES = MATCHED_FAMILY | {
    DUPLICATE_CREDIT, AMBIGUOUS_ABSTAIN, EXCEPTION_MISSING_BANK,
    EXCEPTION_MISSING_SETTLEMENT, NON_SETTLEMENT_CREDIT, OUT_OF_SCOPE,
}

# Leg B (payment <-> order) vocabulary.
# Order-side dispositions:
ORDER_PAID = "order_paid"
ORDER_REFUNDED = "order_refunded"
ORDER_CANCELLED = "order_cancelled"
EXCEPTION_UNPAID_ORDER = "exception_unpaid_order"
# Payment-side dispositions:
PAYMENT_APPLIED = "payment_applied"
PAYMENT_REFUNDED = "payment_refunded"
PAYMENT_FAILED = "payment_failed"
EXCEPTION_DUPLICATE_PAYMENT = "exception_duplicate_payment"
EXCEPTION_PAYMENT_NO_ORDER = "exception_payment_no_order"
# Both sides of an amount disagreement need review:
EXCEPTION_AMOUNT_MISMATCH = "exception_amount_mismatch"

LEG_B_ORDER_STATUSES = frozenset({
    ORDER_PAID, ORDER_REFUNDED, ORDER_CANCELLED, EXCEPTION_UNPAID_ORDER,
    EXCEPTION_AMOUNT_MISMATCH,
})
LEG_B_PAYMENT_STATUSES = frozenset({
    PAYMENT_APPLIED, PAYMENT_REFUNDED, PAYMENT_FAILED,
    EXCEPTION_DUPLICATE_PAYMENT, EXCEPTION_PAYMENT_NO_ORDER,
    EXCEPTION_AMOUNT_MISMATCH,
})

# Confidence tiers (matched-family decisions only; exceptions carry none).
CONF_EXACT = "exact"
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_NEEDS_REVIEW = "needs_review"
CONFIDENCE_TIERS = (CONF_EXACT, CONF_HIGH, CONF_MEDIUM, CONF_NEEDS_REVIEW)

# --- CSV column orders (single source of truth for writers and readers) ------

PAYMENTS_COLUMNS = [
    "payment_id", "order_id", "method", "amount_paise", "fee_paise",
    "tax_paise", "status", "created_at", "settlement_id",
]

SETTLEMENTS_COLUMNS = [
    "settlement_id", "amount_paise", "fees_paise", "tax_paise", "utr",
    "payment_count", "status", "created_at", "settled_at",
]

BANK_COLUMNS = [
    "txn_id", "txn_date", "value_date", "narration", "ref_no",
    "debit_amount", "credit_amount", "balance",
]

ORDER_BOOK_COLUMNS = [
    "order_id", "receipt", "amount_paise", "status", "created_at",
]

GOLDEN_COLUMNS = [
    "record_type", "record_id", "expected_disposition", "counterparty_ids",
    "scenario_tag", "expected_discrepancy_paise", "notes",
]

# record_type values in golden files
RT_SETTLEMENT = "settlement"
RT_BANK_CREDIT = "bank_credit"
RT_ORDER = "order"
RT_PAYMENT = "payment"

COUNTERPARTY_SEP = ";"


# --- In-memory records --------------------------------------------------------

@dataclass
class Settlement:
    settlement_id: str
    amount_paise: int          # net amount expected to hit the bank
    fees_paise: int
    tax_paise: int
    utr: str
    payment_count: int
    status: str
    created_at: str            # ISO timestamp
    settled_at: str            # ISO timestamp (expected credit date)


@dataclass
class BankRow:
    txn_id: str
    txn_date: str              # DD/MM/YYYY
    value_date: str            # DD/MM/YYYY
    narration: str
    ref_no: str
    debit_paise: int           # 0 when the row is a credit
    credit_paise: int          # 0 when the row is a debit
    balance_paise: int


@dataclass
class Decision:
    """One reconciliation decision. The engine, the baselines and the grader
    all use this exact shape, so every strategy is graded identically."""
    leg: str                              # "A" | "B"
    kind: str                             # "match" | "exception" | "out_of_scope"
    status: str                           # one of the vocabulary constants above
    settlement_ids: list[str] = field(default_factory=list)
    bank_txn_ids: list[str] = field(default_factory=list)
    confidence: str = ""                  # tier for matched-family, else ""
    expected_paise: int | None = None
    received_paise: int | None = None
    discrepancy_paise: int | None = None  # received - expected
    discrepancy_breakdown: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)    # [{"rule":..., "detail":...}]
    candidates: list[dict] = field(default_factory=list)  # for exceptions/ambiguous
    pass_name: str = ""
    explanation: str = ""                 # display-only; excluded from grading/verification

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LegBDecision:
    """One leg B (payment <-> order) decision. Deliberately simpler than the
    leg A Decision: leg B records carry explicit references, so the work is
    rule application, not candidate search."""
    record_type: str                      # RT_ORDER | RT_PAYMENT
    record_id: str
    status: str
    counterparty_ids: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    explanation: str = ""                 # display-only

    def to_dict(self) -> dict:
        return asdict(self)


def decisions_grading_view(decisions: list[Decision]) -> list[dict]:
    """The gradable content of a decision list: everything except the
    display-only explanation. Used to assert LLM-on/off equivalence."""
    out = []
    for d in decisions:
        row = d.to_dict()
        row.pop("explanation", None)
        out.append(row)
    return out
