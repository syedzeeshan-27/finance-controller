"""Published tax rules — the single source both sides of Stage 3 compute from.

The generator imports this module to mint the world's tax files, and the tax
engine's books derivation imports it to recompute the merchant-side
expectations, so in clean cases the two agree to the paisa by construction.
`tax/verify.py` deliberately does NOT import this module: it re-implements
every rule with its own arithmetic, mirroring `recon/verify.py`.

Conventions this module publishes (also recorded in the world manifest):

- Inclusive-GST split: a vendor debit is a GST-inclusive total. The pair rule
  is `taxable = (total*100 + 59) // 118` (round-half-up division by 1.18) and
  `gst = total - taxable`. For any total minted as `x + gst_on_fee-style 18%
  round-half-up of x` the rule recovers x exactly; verification checks the
  PAIR (taxable + gst == total, taxable == the published quotient), never
  "gst == 18% of taxable", which can be off by a paisa for arbitrary totals.
- Razorpay fee invoice: one consolidated line per calendar month M, taxable =
  sum(fees_paise) and gst = sum(tax_paise) over settlements with created_at in
  M. Those sums are already tax-exclusive — the split rule does not apply.
- Invoice reference: the LAST run of >= 5 consecutive digits in a narration.
  Every purchase-bearing narration template ends with a 5-digit number; the
  only other digit run ("POS 4287XXXXXX ...") is 4 long, so the rule is safe.
- Periods are calendar months "YYYY-MM"; FY quarters follow the Indian
  financial year (Apr-Jun = Q1 of FY starting that April).
- GST liability for month M = 3% of month M-1's gross captured payment
  amount, floored to Rs 10. Month 0 falls back to the gross of world days
  0-29 (no in-world predecessor — a documented generator convention).
- TDS deposit for month M = 10% of month M-1's ACTUAL posted payroll debit,
  floored to Rs 1. Month 0's basis (the prior month's payroll) is out of
  world, so month 0 is graded `unverifiable_prior_period`.
- Statutory payment deadline = the due date rolled off Sunday (banks don't
  post on Sundays), same roll rule as settlements.
"""

from __future__ import annotations

import re
from datetime import date

from recon.normalize import roll_off_sunday

MERCHANT_STATE = "29"                      # Karnataka
MERCHANT_GSTIN = "29AAHCM8815R1ZK"
MERCHANT_NAME = "MERIDIAN CRAFTWORKS PVT LTD"

HEAD_IGST = "igst"
HEAD_CGST_SGST = "cgst_sgst"

GST_LIABILITY_RATE_PCT = 3                 # of prev-month gross captured
TDS_DEPOSIT_RATE_PCT = 10                  # of prev-month posted payroll
GST_DUE_DAY = 20                           # statutory GST payment day of month
TDS_DUE_DAY = 7                            # statutory TDS deposit day of month

_INVOICE_REF_RE = re.compile(r"\d{5,}")


# --- Money -------------------------------------------------------------------

def taxable_from_inclusive(total_paise: int) -> int:
    """Taxable value inside an 18%-GST-inclusive total: round-half-up division
    by 1.18, in integer paise."""
    return (total_paise * 100 + 59) // 118


def gst_from_inclusive(total_paise: int) -> int:
    """GST component of an 18%-inclusive total. Defined as the complement of
    `taxable_from_inclusive` so the pair always sums to the total exactly."""
    return total_paise - taxable_from_inclusive(total_paise)


def gst_liability(prev_month_gross_paise: int) -> int:
    """GST payable for a month: 3% of the previous month's gross captured
    payments, floored to Rs 10 (same rule the generator schedules with)."""
    return (prev_month_gross_paise * GST_LIABILITY_RATE_PCT // 100) // 1_000 * 1_000


def tds_deposit(prev_month_payroll_paise: int) -> int:
    """TDS deposit for a month: 10% of the previous month's actual posted
    payroll debit, floored to Rs 1."""
    return (prev_month_payroll_paise // TDS_DEPOSIT_RATE_PCT) // 100 * 100


# --- Identifiers -------------------------------------------------------------

def invoice_ref(narration: str) -> str | None:
    """Merchant-side invoice reference: the last run of >= 5 consecutive
    digits in the narration, or None if there is no such run."""
    runs = _INVOICE_REF_RE.findall(narration)
    return runs[-1] if runs else None


def fee_purchase_id(period: str) -> str:
    """Books-side id for the monthly consolidated Razorpay fee invoice."""
    return f"RZPFEE-{period}"


# --- Periods -----------------------------------------------------------------

def period_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def next_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


def fy_quarter(d: date) -> str:
    """Indian financial-year quarter, e.g. 2025-06-15 -> '2025-26Q1'."""
    fy_start = d.year if d.month >= 4 else d.year - 1
    q = (d.month - 4) // 3 + 1 if d.month >= 4 else (d.month + 8) // 3 + 1
    return f"{fy_start}-{(fy_start + 1) % 100:02d}Q{q}"


def statutory_deadline(due: date) -> date:
    """Latest acceptable posting date for a payment due on `due`."""
    return roll_off_sunday(due)


def head_for(vendor_state: str) -> str:
    """Tax head on a purchase invoice: intra-state supplies split CGST+SGST,
    inter-state supplies charge IGST."""
    return HEAD_CGST_SGST if vendor_state == MERCHANT_STATE else HEAD_IGST


def split_heads(gst_paise: int, head: str) -> dict:
    """The published head-split rule, in integer paise.

    IGST is never split. Intra-state GST divides equally into CGST and
    SGST; when the total is an odd number of paise the extra paisa goes to
    SGST (a fixed convention so the split is deterministic and the
    components always sum back to the total exactly). Re-typed
    independently in tax/verify.py; the two implementations are forced to
    agree by test."""
    if head == HEAD_IGST:
        return {"igst_paise": gst_paise, "cgst_paise": 0, "sgst_paise": 0}
    cgst = gst_paise // 2
    return {"igst_paise": 0, "cgst_paise": cgst,
            "sgst_paise": gst_paise - cgst}
