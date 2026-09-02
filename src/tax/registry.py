"""Deterministic counterparty identities for the tax loops.

This is the merchant's vendor master — the knowledge a real finance team
keeps: who each recurring narration belongs to, whether the supplier is
GST-registered, and whether input credit on that spend is claimable at all.
The generator uses the same table to decide who files GSTR-2B lines, so the
registry is world truth, not a heuristic.

ITC posture per vendor (both flags are registry knowledge, not answer-key data):
- `files_2b=True,  itc_eligible=True`  — normal registered supplier; a 2B
  line is expected and the credit is claimable.
- `files_2b=True,  itc_eligible=False` — Section 17(5) BLOCKED credit (food,
  vacation travel): the vendor files, the line appears in 2B, and the
  merchant must still not claim it.
- `files_2b=False, itc_eligible=False` — no ITC applicable: exempt supply
  (life insurance), non-GST supply (fuel), or unregistered supplier
  (freelancer paid by IMPS). No 2B line exists and none should be chased.

Classification everywhere is `vendor.marker in narration.upper()`. Obligation
payments (payroll, GST, TDS deposits) are compliance debits, never purchases.
"""

from __future__ import annotations

from dataclasses import dataclass

from tax.rules import MERCHANT_STATE


@dataclass(frozen=True)
class Vendor:
    key: str
    display_name: str
    marker: str            # uppercase substring identifying the vendor in a narration
    gstin: str             # "" for unregistered / out-of-GST suppliers
    state: str             # 2-digit GST state code ("" when no GSTIN)
    inv_prefix: str        # vendor's invoice-number prefix in GSTR-2B
    files_2b: bool         # does a 2B line exist for this vendor's invoices?
    itc_eligible: bool     # may the merchant claim the credit?
    no_itc_reason: str = ""  # why not, when itc_eligible is False


# Razorpay is special-cased: its "purchases" are the monthly consolidated fee
# invoices derived from settlements (fees_paise/tax_paise), not bank debits,
# so it carries no narration marker for debit classification.
RAZORPAY = Vendor("razorpay", "RAZORPAY SOFTWARE PVT LTD", "", "29AAGCR4375J1ZU",
                  "29", "RZP", files_2b=True, itc_eligible=True)

# Debit-classified vendors, scheduled and one-off. Markers are stable
# substrings of the generator's narration templates.
DEBIT_VENDORS: tuple[Vendor, ...] = (
    Vendor("aws", "AMAZON WEB SERVICES INDIA PL", "AMAZON WEB SERVICES",
           "27AABCA9603P1ZL", "27", "AWSIN", True, True),
    Vendor("airtel", "BHARTI AIRTEL LTD", "BHARTI AIRTEL",
           "07AAACB2894G1ZH", "07", "BAL", True, True),
    Vendor("landlord", "URBAN LADDER PROPERTIES", "URBAN LADDER RENT",
           "29AAGFU7331Q1ZC", "29", "ULP", True, True),
    Vendor("dtdc", "DTDC EXPRESS LTD", "COURIER DTDC",
           "29AAACD8017H1ZN", "29", "DTDC", True, True),
    Vendor("vistaprint", "VISTAPRINT MARKETING", "VISTAPRINT",
           "27AAECV4031B1ZQ", "27", "VP", True, True),
    Vendor("bigbazaar", "BIG BAZAAR RETAIL", "BIG BAZAAR",
           "29AABCF4571C1ZW", "29", "BBR", True, True),
    Vendor("bluedart", "BLUE DART EXPRESS LTD", "BLUEDART EXPRESS",
           "27AAACB0446L1ZS", "27", "BDE", True, True),
    # Section 17(5) blocked credits: filed in 2B, must not be claimed.
    Vendor("swiggy", "SWIGGY INSTAMART", "SWIGGY INSTAMART",
           "29AAFCB7707D1ZP", "29", "SWG", True, False,
           "blocked_17_5_food_and_beverages"),
    Vendor("makemytrip", "MAKEMYTRIP HOLIDAYS", "MAKEMYTRIP",
           "06AAECM4763T1ZF", "06", "MMT", True, False,
           "blocked_17_5_vacation_travel"),
    # No ITC applicable: no 2B line exists and none should be chased.
    Vendor("lic", "LIC OF INDIA", "LIC PREMIUM",
           "", "", "", False, False, "exempt_life_insurance_premium"),
    Vendor("indianoil", "INDIAN OIL FUEL STATION", "INDIAN OIL",
           "", "", "", False, False, "non_gst_supply_fuel"),
    Vendor("freelance", "FREELANCE DESIGNER", "FREELANCE DESIGN",
           "", "", "", False, False, "unregistered_supplier"),
)

ALL_VENDORS: tuple[Vendor, ...] = (RAZORPAY,) + DEBIT_VENDORS
VENDORS_BY_KEY = {v.key: v for v in ALL_VENDORS}
VENDORS_BY_GSTIN = {v.gstin: v for v in ALL_VENDORS if v.gstin}

# Compliance debits: never purchases, never in 2B. Classification is by
# narration marker alone — the generator's obligation keys are not consulted.
OBLIGATION_MARKERS: tuple[str, ...] = (
    "STAFF PAYROLL", "GST PAYMENT-CBIC", "TDS PAYMENT-CBDT",
)

# The 194-O style deductor behind the 1%-TDS-at-credit deductions.
DEDUCTOR_TAN = "BLRR12345E"
DEDUCTOR_NAME = "RAZORPAY SOFTWARE PVT LTD"
TDS_SECTION = "194O"


def classify_debit(narration: str) -> Vendor | None:
    """Vendor for a bank debit narration, or None (unclassified spend /
    compliance debit). First match wins; markers are disjoint by test."""
    up = narration.upper()
    for marker in OBLIGATION_MARKERS:
        if marker in up:
            return None
    for v in DEBIT_VENDORS:
        if v.marker and v.marker in up:
            return v
    return None


def _consistent() -> None:
    for v in ALL_VENDORS:
        if v.files_2b:
            assert v.gstin and v.state and v.inv_prefix, v.key
            assert len(v.gstin) == 15 and v.gstin[:2] == v.state, v.key
        else:
            assert not v.itc_eligible, v.key
        if not v.itc_eligible:
            assert v.no_itc_reason, v.key
    assert RAZORPAY.state == MERCHANT_STATE


_consistent()
