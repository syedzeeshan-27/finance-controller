"""Parsers and money arithmetic: the foundation everything else trusts."""

from datetime import date

import pytest

from recon import normalize as nz


class TestPaiseFromRupeeStr:
    def test_plain(self):
        assert nz.paise_from_rupee_str("4500.00") == 450000

    def test_indian_grouping(self):
        assert nz.paise_from_rupee_str("1,52,340.50") == 15234050

    def test_no_decimals(self):
        assert nz.paise_from_rupee_str("4500") == 450000

    def test_one_decimal_digit(self):
        assert nz.paise_from_rupee_str("4500.5") == 450050

    def test_negative(self):
        assert nz.paise_from_rupee_str("-2950.00") == -295000

    def test_float_trap(self):
        # 0.29 is not representable in binary float; string parsing must not care.
        assert nz.paise_from_rupee_str("0.29") == 29

    def test_quoted_and_padded(self):
        assert nz.paise_from_rupee_str(' "12,500.75" ') == 1250075

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            nz.paise_from_rupee_str("")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            nz.paise_from_rupee_str("12.345")  # 3 decimal digits
        with pytest.raises(ValueError):
            nz.paise_from_rupee_str("abc")

    def test_optional_empty_is_zero(self):
        assert nz.paise_from_optional_rupee_str("") == 0
        assert nz.paise_from_optional_rupee_str("  ") == 0
        assert nz.paise_from_optional_rupee_str("1.00") == 100


class TestRupeeStrFromPaise:
    def test_plain(self):
        assert nz.rupee_str_from_paise(450000) == "4500.00"

    def test_indian_grouping(self):
        assert nz.rupee_str_from_paise(15234050, indian_grouping=True) == "1,52,340.50"

    def test_crore(self):
        assert nz.rupee_str_from_paise(1234567890, indian_grouping=True) == "1,23,45,678.90"

    def test_negative(self):
        assert nz.rupee_str_from_paise(-295000) == "-2950.00"

    def test_small_no_grouping_needed(self):
        assert nz.rupee_str_from_paise(99900, indian_grouping=True) == "999.00"

    def test_roundtrip(self):
        for p in (0, 1, 99, 100, 450050, 15234050, 999999999):
            s = nz.rupee_str_from_paise(p, indian_grouping=True)
            assert nz.paise_from_rupee_str(s) == p


class TestGstOnFee:
    def test_exact(self):
        assert nz.gst_on_fee(100) == 18

    def test_round_half_up(self):
        # 25 * 18 = 450 -> 4.50 -> rounds up to 5
        assert nz.gst_on_fee(25) == 5

    def test_round_down(self):
        # 24 * 18 = 432 -> 4.32 -> 4
        assert nz.gst_on_fee(24) == 4

    def test_zero(self):
        assert nz.gst_on_fee(0) == 0

    def test_never_recompute_at_batch_level(self):
        # Sum of per-payment GST != 18% of summed fees, in general. The batch
        # total MUST be the sum of per-payment values.
        fees = [25, 25, 25]
        per_payment = sum(nz.gst_on_fee(f) for f in fees)   # 5+5+5 = 15
        batch_level = nz.gst_on_fee(sum(fees))              # gst(75) = 13.5 -> 14
        assert per_payment == 15 and batch_level == 14      # they differ; that's the trap


class TestDates:
    def test_bank_date_roundtrip(self):
        d = date(2025, 4, 3)
        assert nz.parse_bank_date(nz.bank_date_str(d)) == d
        assert nz.bank_date_str(d) == "03/04/2025"

    def test_iso_ts(self):
        assert nz.parse_iso_ts("2025-04-03T14:22:10").date() == date(2025, 4, 3)

    def test_parse_iso_date_both_forms(self):
        assert nz.parse_iso_date("2025-04-03") == date(2025, 4, 3)
        assert nz.parse_iso_date("2025-04-03T14:22:10") == date(2025, 4, 3)

    def test_roll_off_sunday(self):
        sunday = date(2025, 4, 6)
        assert nz.roll_off_sunday(sunday) == date(2025, 4, 7)
        assert nz.roll_off_sunday(date(2025, 4, 7)) == date(2025, 4, 7)


class TestUtrTokens:
    UTR = "UTIB250987654321"  # 4 letters + 12 digits

    def test_utr_shape(self):
        assert nz.is_utr_shaped(self.UTR)
        assert not nz.is_utr_shaped("UTIB25098765432")     # 11 digits
        assert not nz.is_utr_shaped("UT1B250987654321")    # digit in prefix
        assert not nz.is_utr_shaped("RCPT202500071XXX")    # letters in tail

    def test_clean_narration(self):
        runs, full = nz.extract_tokens(
            f"NEFT-{self.UTR}-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT", "")
        assert self.UTR in runs
        assert self.UTR in full

    def test_separator_mangled_found_in_full_strip(self):
        runs, full = nz.extract_tokens("NEFT-UTIB-2509 8765 4321-RAZORPAY", "")
        assert self.UTR not in runs      # broken into short runs
        assert self.UTR in full          # but the strip contains it

    def test_truncated_run_extracted(self):
        runs, _ = nz.extract_tokens("NEFT CR 0987654321 RAZORPAY", "")
        assert "0987654321" in runs      # 10-char fragment kept

    def test_short_runs_dropped(self):
        # RAZORPAY (8), PVT (3), LTD (3): no alnum run reaches 10 chars.
        runs, _ = nz.extract_tokens("NEFT CR RAZORPAY PVT LTD", "")
        assert runs == []

    def test_ref_no_contributes(self):
        runs, _ = nz.extract_tokens("NEFT CR", self.UTR)
        assert self.UTR in runs

    def test_hamming(self):
        assert nz.hamming_le_1(self.UTR, self.UTR)
        assert nz.hamming_le_1(self.UTR, "UTIB250987654320")      # one substitution
        assert not nz.hamming_le_1(self.UTR, "UTIB250987654399")  # two substitutions
        assert not nz.hamming_le_1(self.UTR, self.UTR[:-1])       # different length

    def test_overlap_fragment(self):
        assert nz.overlap_suffix_prefix("0987654321", self.UTR)          # interior/suffix 10
        assert nz.overlap_suffix_prefix("UTIB250987", self.UTR)          # prefix 10
        assert not nz.overlap_suffix_prefix("098765432", self.UTR)       # only 9 chars
        assert not nz.overlap_suffix_prefix("0987654322", self.UTR)      # not a fragment
