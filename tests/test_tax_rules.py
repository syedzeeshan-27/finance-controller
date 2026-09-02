"""Published tax rules, the vendor registry, and the golden-blindness canary."""

from datetime import date
from pathlib import Path

from tax import registry as R
from tax import rules as T

TAX_SRC = Path(__file__).resolve().parents[1] / "src" / "tax"


class TestInclusiveSplit:
    def test_pair_recovers_every_minted_total(self):
        # For any taxable x, a total minted as x + 18%-round-half-up must
        # split back to exactly (x, total - x).
        for x in range(0, 250_000):
            total = x + (x * 18 + 50) // 100
            assert T.taxable_from_inclusive(total) == x
            assert T.gst_from_inclusive(total) == total - x

    def test_pair_always_sums_to_total(self):
        for total in (0, 1, 29, 117, 118, 119, 999_999_999):
            assert (T.taxable_from_inclusive(total)
                    + T.gst_from_inclusive(total)) == total

    def test_arbitrary_total_documents_pair_semantics(self):
        # 29 paise was never minted from an integer taxable: the pair rule
        # gives (25, 4) while 18% of 25 rounds to 5. This is WHY verification
        # checks the pair, never "gst == 18% of taxable".
        assert T.taxable_from_inclusive(29) == 25
        assert T.gst_from_inclusive(29) == 4


class TestLiabilityRules:
    def test_gst_liability_floors_to_ten_rupees(self):
        assert T.gst_liability(155_566_700) == 4_667_000   # 4_667_001 -> floor
        assert T.gst_liability(0) == 0
        assert T.gst_liability(33_000) == 0                # 990 paise -> floor to 0

    def test_tds_deposit_floors_to_one_rupee(self):
        assert T.tds_deposit(42_336_000) == 4_233_600
        assert T.tds_deposit(42_333_333) == 4_233_300
        assert T.tds_deposit(0) == 0


class TestInvoiceRef:
    def test_all_purchase_templates_yield_the_trailing_run(self):
        cases = [
            "UPI-SWIGGY INSTAMART-48213@ybl",
            "POS 4287XXXXXX INDIAN OIL-48213",     # "4287" run is 4 < 5
            "IMPS-P2A-COURIER DTDC-48213",
            "UPI-MAKEMYTRIP HOLIDAYS-48213@okhdfc",
            "NEFT DR-VISTAPRINT MARKETING-48213",
            "IMPS-P2A-FREELANCE DESIGN-48213",
            "POS 4287XXXXXX BIG BAZAAR-48213",
            "UPI-BLUEDART EXPRESS-48213@paytm",
            "NEFT DR-AMAZON WEB SERVICES INDIA PL-INV48213",
            "NEFT DR-URBAN LADDER RENT-48213",
            "UPI-BHARTI AIRTEL-48213@icici",
            "ACH-D-LIC PREMIUM-48213",
        ]
        for narration in cases:
            assert T.invoice_ref(narration) == "48213", narration

    def test_last_run_wins_and_short_runs_dont(self):
        assert T.invoice_ref("INV 11111 CORRECTED 22222") == "22222"
        assert T.invoice_ref("POS 4287 NO REF HERE") is None
        assert T.invoice_ref("") is None


class TestPeriods:
    def test_period_of_and_next(self):
        assert T.period_of(date(2025, 4, 1)) == "2025-04"
        assert T.next_period("2025-04") == "2025-05"
        assert T.next_period("2025-12") == "2026-01"

    def test_fy_quarters(self):
        assert T.fy_quarter(date(2025, 4, 1)) == "2025-26Q1"
        assert T.fy_quarter(date(2025, 6, 30)) == "2025-26Q1"
        assert T.fy_quarter(date(2025, 7, 1)) == "2025-26Q2"
        assert T.fy_quarter(date(2025, 10, 5)) == "2025-26Q3"
        assert T.fy_quarter(date(2026, 1, 15)) == "2025-26Q4"
        assert T.fy_quarter(date(2026, 3, 31)) == "2025-26Q4"

    def test_statutory_deadline_rolls_off_sunday(self):
        assert T.statutory_deadline(date(2025, 7, 20)) == date(2025, 7, 21)
        assert T.statutory_deadline(date(2025, 7, 21)) == date(2025, 7, 21)


class TestHeads:
    def test_head_for(self):
        assert T.head_for("29") == T.HEAD_CGST_SGST
        assert T.head_for("27") == T.HEAD_IGST
        assert T.head_for("07") == T.HEAD_IGST


class TestRegistry:
    def test_classify_every_generator_template(self):
        expected = {
            "UPI-SWIGGY INSTAMART-40213@ybl": "swiggy",
            "POS 4287XXXXXX INDIAN OIL-40213": "indianoil",
            "IMPS-P2A-COURIER DTDC-40213": "dtdc",
            "UPI-MAKEMYTRIP HOLIDAYS-40213@okhdfc": "makemytrip",
            "NEFT DR-VISTAPRINT MARKETING-40213": "vistaprint",
            "IMPS-P2A-FREELANCE DESIGN-40213": "freelance",
            "POS 4287XXXXXX BIG BAZAAR-40213": "bigbazaar",
            "UPI-BLUEDART EXPRESS-40213@paytm": "bluedart",
            "NEFT DR-AMAZON WEB SERVICES INDIA PL-INV40213": "aws",
            "NEFT DR-URBAN LADDER RENT-40213": "landlord",
            "UPI-BHARTI AIRTEL-40213@icici": "airtel",
            "ACH-D-LIC PREMIUM-40213": "lic",
        }
        for narration, key in expected.items():
            v = R.classify_debit(narration)
            assert v is not None and v.key == key, narration

    def test_obligation_and_unknown_debits_are_not_purchases(self):
        for narration in ("SAL-NEFT-STAFF PAYROLL-48213",
                          "GST PAYMENT-CBIC-48213",
                          "TDS PAYMENT-CBDT-48213",
                          "NEFT DR-SOME RANDOM SHOP-48213"):
            assert R.classify_debit(narration) is None, narration

    def test_markers_are_disjoint_across_templates(self):
        # No vendor's marker may appear in another vendor's narration or in
        # an obligation narration — classification must be unambiguous.
        narrations = {
            "swiggy": "UPI-SWIGGY INSTAMART-40213@ybl",
            "indianoil": "POS 4287XXXXXX INDIAN OIL-40213",
            "dtdc": "IMPS-P2A-COURIER DTDC-40213",
            "makemytrip": "UPI-MAKEMYTRIP HOLIDAYS-40213@okhdfc",
            "vistaprint": "NEFT DR-VISTAPRINT MARKETING-40213",
            "freelance": "IMPS-P2A-FREELANCE DESIGN-40213",
            "bigbazaar": "POS 4287XXXXXX BIG BAZAAR-40213",
            "bluedart": "UPI-BLUEDART EXPRESS-40213@paytm",
            "aws": "NEFT DR-AMAZON WEB SERVICES INDIA PL-INV40213",
            "landlord": "NEFT DR-URBAN LADDER RENT-40213",
            "airtel": "UPI-BHARTI AIRTEL-40213@icici",
            "lic": "ACH-D-LIC PREMIUM-40213",
        }
        for v in R.DEBIT_VENDORS:
            for key, narration in narrations.items():
                if key != v.key:
                    assert v.marker not in narration.upper(), (v.key, key)
        for marker in R.OBLIGATION_MARKERS:
            for narration in narrations.values():
                assert marker not in narration.upper()

    def test_gstins_unique_and_states_consistent(self):
        filed = [v for v in R.ALL_VENDORS if v.files_2b]
        gstins = [v.gstin for v in filed]
        assert len(set(gstins)) == len(gstins)
        for v in filed:
            assert T.head_for(v.state) in (T.HEAD_IGST, T.HEAD_CGST_SGST)


class TestGoldenBlindness:
    def test_only_the_benchmark_module_mentions_golden_files(self):
        # The tax engine, books derivation, IO, verifier and dashboard code
        # must never read (or even name) the expected-disposition files;
        # grading is the benchmark's job alone.
        offenders = []
        for py in TAX_SRC.glob("*.py"):
            if py.name == "benchmark.py":
                continue
            if "golden" in py.read_text(encoding="utf-8").lower():
                offenders.append(py.name)
        assert offenders == []
