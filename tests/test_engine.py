"""Engine micro-fixtures: each scenario type on a handcrafted 2-6 record world.

These are the regression spec for the pass logic — every rule the engine
claims to implement has a fixture proving it, including the abstentions.
"""

from recon import schemas as S
from recon.engine import reconcile_leg_a

UTR1 = "UTIB250987654321"
UTR2 = "HDFC111122223333"
UTR3 = "ICIC999988887777"


def setl(sid="setl_A", amount=1_000_000, utr=UTR1, created="2025-04-02T01:15:00",
         settled="2025-04-03"):
    return S.Settlement(settlement_id=sid, amount_paise=amount, fees_paise=2000,
                        tax_paise=360, utr=utr, payment_count=3,
                        status="processed", created_at=created, settled_at=settled)


def credit(txn="BANK000001", amount=1_000_000, value_date="03/04/2025",
           narration=f"NEFT-{UTR1}-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT",
           ref=""):
    return S.BankRow(txn_id=txn, txn_date=value_date, value_date=value_date,
                     narration=narration, ref_no=ref, debit_paise=0,
                     credit_paise=amount, balance_paise=0)


def debit(txn="BANK000099", amount=50_000):
    return S.BankRow(txn_id=txn, txn_date="03/04/2025", value_date="03/04/2025",
                     narration="UPI-SWIGGY-12345@ybl", ref_no="",
                     debit_paise=amount, credit_paise=0, balance_paise=0)


def by_status(decisions, status):
    return [d for d in decisions if d.status == status]


def one(decisions, status):
    ds = by_status(decisions, status)
    assert len(ds) == 1, f"expected exactly one {status}, got {len(ds)}: " \
                         f"{[(d.status, d.settlement_ids, d.bank_txn_ids) for d in decisions]}"
    return ds[0]


class TestPass1ExactUtr:
    def test_clean_match_is_exact_confidence(self):
        ds = reconcile_leg_a([setl()], [credit()])
        d = one(ds, S.MATCHED)
        assert d.confidence == S.CONF_EXACT
        assert d.settlement_ids == ["setl_A"] and d.bank_txn_ids == ["BANK000001"]
        assert d.discrepancy_paise == 0
        assert {e["rule"] for e in d.evidence} >= {"utr_exact", "amount_exact"}

    def test_separator_mangled_utr_still_exact(self):
        c = credit(narration=f"NEFT-{UTR1[:4]}-{UTR1[4:8]} {UTR1[8:12]} {UTR1[12:]}-RAZORPAY")
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED)
        assert d.confidence == S.CONF_EXACT

    def test_delayed_settlement_matches_despite_window(self):
        c = credit(value_date="20/04/2025")  # T+18, far outside any window
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED)
        assert d.confidence == S.CONF_EXACT
        assert any(e["rule"] == "late_settlement" for e in d.evidence)

    def test_duplicate_credit_flagged_not_double_claimed(self):
        c1 = credit(txn="BANK000001", value_date="03/04/2025")
        c2 = credit(txn="BANK000002", value_date="04/04/2025",
                    narration=c1.narration)
        ds = reconcile_leg_a([setl()], [c1, c2])
        m = one(ds, S.MATCHED)
        dup = one(ds, S.DUPLICATE_CREDIT)
        assert m.bank_txn_ids == ["BANK000001"]      # earliest wins
        assert dup.bank_txn_ids == ["BANK000002"]
        assert dup.candidates[0]["record_id"] == "setl_A"

    def test_split_by_shared_utr_sums_exact(self):
        c1 = credit(txn="BANK000001", amount=600_000,
                    narration=f"RTGS-{UTR1}/1-RAZORPAY SOFTWARE PL-PART")
        c2 = credit(txn="BANK000002", amount=400_000,
                    narration=f"RTGS-{UTR1}/2-RAZORPAY SOFTWARE PL-PART")
        d = one(reconcile_leg_a([setl()], [c1, c2]), S.MATCHED_SPLIT)
        assert sorted(d.bank_txn_ids) == ["BANK000001", "BANK000002"]
        assert d.received_paise == 1_000_000 and d.discrepancy_paise == 0

    def test_bank_charge_decomposed(self):
        c = credit(amount=1_000_000 - 2_950)
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED_WITH_DISCREPANCY)
        assert d.confidence == S.CONF_HIGH
        assert d.discrepancy_paise == -2_950
        assert d.discrepancy_breakdown[0]["label"] == "bank_neft_rtgs_charge_incl_gst"

    def test_tds_1pct_decomposed(self):
        c = credit(amount=1_000_000 - 10_000)  # exactly 1% of net
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED_WITH_DISCREPANCY)
        assert d.discrepancy_breakdown[0]["label"] == "tds_1pct_of_net"

    def test_unexplained_residual_needs_review(self):
        c = credit(amount=1_000_000 - 7_777)   # decomposes to nothing known
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED_WITH_DISCREPANCY)
        assert d.confidence == S.CONF_NEEDS_REVIEW
        assert d.discrepancy_breakdown[0]["label"] == "unexplained"


class TestPass2FuzzyUtr:
    def test_truncated_utr_with_exact_amount(self):
        c = credit(narration=f"NEFT CR {UTR1[-11:]} RAZORPAY SOFTWARE")
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED)
        assert d.confidence == S.CONF_HIGH
        assert any(e["rule"] == "utr_fuzzy" for e in d.evidence)

    def test_typo_utr_with_exact_amount(self):
        bad = UTR1[:-1] + ("0" if UTR1[-1] != "0" else "9")
        c = credit(narration=f"NEFT-{bad}-RAZORPAY SOFTWARE PVT LTD")
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED)
        assert d.confidence == S.CONF_HIGH

    def test_fuzzy_without_amount_corroboration_refuses(self):
        bad = UTR1[:-1] + ("0" if UTR1[-1] != "0" else "9")
        c = credit(amount=999_123,  # neither exact nor decomposable
                   narration=f"NEFT-{bad}-RAZORPAY SOFTWARE PVT LTD")
        ds = reconcile_leg_a([setl()], [c])
        assert not by_status(ds, S.MATCHED)
        assert one(ds, S.EXCEPTION_MISSING_BANK).settlement_ids == ["setl_A"]
        assert one(ds, S.EXCEPTION_MISSING_SETTLEMENT).bank_txn_ids == ["BANK000001"]


class TestPass3Merge:
    def test_merge_resolved_by_exact_residual(self):
        s1, s2 = setl("setl_A", 1_000_000, UTR1), setl("setl_B", 700_000, UTR2)
        c = credit(amount=1_700_000)   # carries UTR1 only
        d = one(reconcile_leg_a([s1, s2], [c]), S.MATCHED_MERGED)
        assert sorted(d.settlement_ids) == ["setl_A", "setl_B"]
        assert d.confidence == S.CONF_HIGH
        assert any(e["rule"] == "merge_residual_exact" for e in d.evidence)

    def test_merge_with_two_possible_partners_abstains(self):
        s1 = setl("setl_A", 1_000_000, UTR1)
        s2 = setl("setl_B", 700_000, UTR2)
        s3 = setl("setl_C", 700_000, UTR3)
        c = credit(amount=1_700_000)
        ds = reconcile_leg_a([s1, s2, s3], [c])
        amb = by_status(ds, S.AMBIGUOUS_ABSTAIN)
        assert any(d.bank_txn_ids == ["BANK000001"] for d in amb)
        assert not by_status(ds, S.MATCHED_MERGED)


class TestPass4AmountUnique:
    def test_amount_unique_without_utr(self):
        c = credit(narration="NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT")
        d = one(reconcile_leg_a([setl()], [c]), S.MATCHED)
        assert d.confidence == S.CONF_MEDIUM
        assert any(e["rule"] == "amount_exact_unique" for e in d.evidence)

    def test_twins_all_abstain(self):
        s1 = setl("setl_A", 1_000_000, UTR1, created="2025-04-02T01:15:00")
        s2 = setl("setl_B", 1_000_000, UTR2, created="2025-04-03T01:15:00")
        c = credit(narration="NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT",
                   value_date="05/04/2025")
        ds = reconcile_leg_a([s1, s2], [c])
        amb = by_status(ds, S.AMBIGUOUS_ABSTAIN)
        assert len(amb) == 3  # both settlements and the credit
        credit_amb = next(d for d in amb if d.bank_txn_ids == ["BANK000001"])
        assert {c_["record_id"] for c_ in credit_amb.candidates} == {"setl_A", "setl_B"}
        assert not by_status(ds, S.MATCHED)

    def test_near_collision_resolved_by_exact_paise(self):
        s1 = setl("setl_A", 1_000_000, UTR1)
        s2 = setl("setl_B", 1_000_500, UTR2)  # Rs 5 apart
        c1 = credit(txn="BANK000001", amount=1_000_000)  # UTR1 clean
        c2 = credit(txn="BANK000002", amount=1_000_500,
                    narration="NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT")
        ds = reconcile_leg_a([s1, s2], [c1, c2])
        matches = by_status(ds, S.MATCHED)
        assert len(matches) == 2
        got = {tuple(d.settlement_ids): tuple(d.bank_txn_ids) for d in matches}
        assert got == {("setl_A",): ("BANK000001",), ("setl_B",): ("BANK000002",)}

    def test_amount_match_respects_window(self):
        c = credit(narration="NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT",
                   value_date="25/04/2025")  # 23 days after batch date
        ds = reconcile_leg_a([setl()], [c])
        assert not by_status(ds, S.MATCHED)


class TestPass5Residuals:
    def test_missing_bank_has_ranked_candidates(self):
        s = setl()
        near = credit(txn="BANK000002", amount=995_000,
                      narration="NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT")
        ds = reconcile_leg_a([s], [near])
        exc = one(ds, S.EXCEPTION_MISSING_BANK)
        assert exc.settlement_ids == ["setl_A"]
        assert exc.candidates[0]["record_id"] == "BANK000002"
        assert "5000 paise" in exc.candidates[0]["reason"]

    def test_orphan_shaped_credit(self):
        c = credit(narration=f"NEFT-{UTR2}-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT",
                   amount=555_555)
        ds = reconcile_leg_a([setl()], [c])
        exc = one(ds, S.EXCEPTION_MISSING_SETTLEMENT)
        assert exc.bank_txn_ids == ["BANK000001"]
        assert exc.candidates[0]["record_id"] == "setl_A"

    def test_noise_credit_never_claimed(self):
        c = credit(narration="IMPS-P2A CR-KHATRI TRADERS-INV 4521", amount=1_000_000)
        ds = reconcile_leg_a([setl()], [c])
        assert one(ds, S.NON_SETTLEMENT_CREDIT).bank_txn_ids == ["BANK000001"]
        # the settlement must NOT have matched the identical-amount noise row
        assert one(ds, S.EXCEPTION_MISSING_BANK).settlement_ids == ["setl_A"]

    def test_debits_out_of_scope(self):
        ds = reconcile_leg_a([], [debit()])
        assert one(ds, S.OUT_OF_SCOPE).bank_txn_ids == ["BANK000099"]


class TestStructuralInvariants:
    def test_no_record_decided_twice(self):
        # a busy little world exercising several passes at once
        s1 = setl("setl_A", 1_000_000, UTR1)
        s2 = setl("setl_B", 700_000, UTR2)
        s3 = setl("setl_C", 550_000, UTR3)
        rows = [
            credit(txn="BANK000001", amount=1_000_000),
            credit(txn="BANK000002", amount=700_000,
                   narration=f"NEFT CR {UTR2[-11:]} RAZORPAY"),
            credit(txn="BANK000003", amount=550_000,
                   narration="NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT"),
            credit(txn="BANK000004", amount=123_456,
                   narration="IMPS-P2A CR-KHATRI TRADERS-INV 11"),
            debit(txn="BANK000005"),
        ]
        ds = reconcile_leg_a([s1, s2, s3], rows)
        seen: set[str] = set()
        for d in ds:
            for rid in d.settlement_ids + d.bank_txn_ids:
                assert rid not in seen, f"{rid} decided twice"
                seen.add(rid)
        assert seen == {"setl_A", "setl_B", "setl_C",
                        "BANK000001", "BANK000002", "BANK000003",
                        "BANK000004", "BANK000005"}
