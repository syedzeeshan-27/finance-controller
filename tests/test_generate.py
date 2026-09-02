"""Generator invariants: if these hold, the golden files are trustworthy."""

import csv
import io
from datetime import timedelta

import pytest

from recon import schemas as S
from recon import generate as G
from recon.normalize import paise_from_optional_rupee_str, paise_from_rupee_str

SEED = 7  # deliberately not 42: invariants must hold for any seed


@pytest.fixture(scope="module")
def world():
    return G.build_world(SEED)


def test_determinism_same_seed(world):
    again = G.build_world(SEED)
    assert world["manifest"] == again["manifest"]
    assert world["statement"] == again["statement"]
    assert world["golden_a"] == again["golden_a"]
    assert world["golden_b"] == again["golden_b"]


def test_different_seed_differs(world):
    other = G.build_world(SEED + 1)
    assert other["statement"] != world["statement"]


def test_settlement_arithmetic(world):
    for s in world["settlements"]:
        members = [p for p in s.members]
        assert all(p.status == "captured" for p in members)
        assert s.amount_paise == sum(p.amount_paise - p.fee_paise - p.tax_paise
                                     for p in members)
        assert s.fees_paise == sum(p.fee_paise for p in members)
        assert s.tax_paise == sum(p.tax_paise for p in members)
        assert s.amount_paise > 0


def test_golden_leg_a_complete(world):
    golden_ids = {(r["record_type"], r["record_id"]) for r in world["golden_a"]}
    assert len(golden_ids) == len(world["golden_a"]), "duplicate golden rows"
    for s in world["settlements"]:
        assert (S.RT_SETTLEMENT, s.settlement_id) in golden_ids
    credit_rows = [r for r in world["statement"] if r["credit_amount"]]
    for row in credit_rows:
        assert (S.RT_BANK_CREDIT, row["txn_id"]) in golden_ids
    # and exactly those: no golden row for a nonexistent record
    assert len(world["golden_a"]) == len(world["settlements"]) + len(credit_rows)


def test_balance_continuity(world):
    balance = G.OPENING_BALANCE_PAISE
    for row in world["statement"]:
        credit = paise_from_optional_rupee_str(row["credit_amount"])
        debit = paise_from_optional_rupee_str(row["debit_amount"])
        assert (credit > 0) != (debit > 0), "exactly one side must be set"
        balance += credit - debit
        assert paise_from_rupee_str(row["balance"]) == balance


def test_narrations_never_leak_internal_ids(world):
    """The UTR is the only legitimate join key; settlement/payment/order ids
    must never appear in the bank statement."""
    blob = "\n".join(r["narration"] + "|" + r["ref_no"] for r in world["statement"])
    for s in world["settlements"]:
        assert s.settlement_id not in blob
        assert s.settlement_id[5:] not in blob  # even without the prefix
    for p in world["payments"][:50]:
        assert p.payment_id not in blob


def _by_scenario(world, tag, record_type=None):
    return [r for r in world["golden_a"]
            if r["scenario_tag"] == tag
            and (record_type is None or r["record_type"] == record_type)]


def test_twins_truly_indistinguishable(world):
    setl_by_id = {s.settlement_id: s for s in world["settlements"]}
    twin_rows = _by_scenario(world, G.SC_TWINS, S.RT_SETTLEMENT)
    assert len(twin_rows) == 4  # two pairs
    credits = _by_scenario(world, G.SC_TWINS, S.RT_BANK_CREDIT)
    assert len(credits) == 2
    for c in credits:
        pair_ids = c["counterparty_ids"].split(S.COUNTERPARTY_SEP)
        assert len(pair_ids) == 2
        a, b = (setl_by_id[i] for i in pair_ids)
        assert a.amount_paise == b.amount_paise
        assert c["expected_disposition"] == S.AMBIGUOUS_ABSTAIN
        # the arriving credit's amount equals both
        row = next(r for r in world["statement"] if r["txn_id"] == c["record_id"])
        assert paise_from_rupee_str(row["credit_amount"]) == a.amount_paise
        # no UTR of either settlement anywhere in the credit row
        assert a.utr not in row["narration"] + row["ref_no"]
        assert b.utr not in row["narration"] + row["ref_no"]


def test_amount_only_scenario_is_provably_unique(world):
    """For utr_absent_amount_unique settlements the amount must identify them
    uniquely among ALL settlements (global separation audit guarantees it)."""
    nets = sorted(s.amount_paise for s in world["settlements"])
    targets = {r["record_id"] for r in _by_scenario(world, G.SC_AMOUNT_ONLY, S.RT_SETTLEMENT)}
    for s in world["settlements"]:
        if s.settlement_id in targets:
            assert nets.count(s.amount_paise) == 1


def test_near_collisions_are_close_but_distinct(world):
    setl_by_id = {s.settlement_id: s for s in world["settlements"]}
    rows = _by_scenario(world, G.SC_NEAR_COLLISION, S.RT_SETTLEMENT)
    assert len(rows) == 6  # three pairs
    seen_pairs = set()
    amounts = [setl_by_id[r["record_id"]].amount_paise for r in rows]
    for a in amounts:
        close = [b for b in amounts if b != a and abs(a - b) < 1000]
        assert close, "each collision member has a partner within Rs 10"
        assert all(abs(a - b) >= 100 for b in close), "but never identical"


def test_split_parts_sum_exactly(world):
    setl_by_id = {s.settlement_id: s for s in world["settlements"]}
    stmt_by_id = {r["txn_id"]: r for r in world["statement"]}
    for r in _by_scenario(world, G.SC_SPLIT, S.RT_SETTLEMENT):
        s = setl_by_id[r["record_id"]]
        parts = r["counterparty_ids"].split(S.COUNTERPARTY_SEP)
        assert len(parts) in (2, 3)
        total = sum(paise_from_rupee_str(stmt_by_id[t]["credit_amount"]) for t in parts)
        assert total == s.amount_paise


def test_merged_credit_sums_exactly(world):
    setl_by_id = {s.settlement_id: s for s in world["settlements"]}
    stmt_by_id = {r["txn_id"]: r for r in world["statement"]}
    for r in _by_scenario(world, G.SC_MERGED, S.RT_BANK_CREDIT):
        pair = r["counterparty_ids"].split(S.COUNTERPARTY_SEP)
        assert len(pair) == 2
        total = sum(setl_by_id[i].amount_paise for i in pair)
        assert paise_from_rupee_str(stmt_by_id[r["record_id"]]["credit_amount"]) == total


def test_missing_bank_settlements_have_no_credit(world):
    for r in _by_scenario(world, G.SC_MISSING_BANK, S.RT_SETTLEMENT):
        assert r["expected_disposition"] == S.EXCEPTION_MISSING_BANK
        assert r["counterparty_ids"] == ""


def test_discrepancy_scenario_decomposes(world):
    setl_by_id = {s.settlement_id: s for s in world["settlements"]}
    stmt_by_id = {r["txn_id"]: r for r in world["statement"]}
    for r in _by_scenario(world, G.SC_DISCREPANCY, S.RT_SETTLEMENT):
        s = setl_by_id[r["record_id"]]
        credit_id = r["counterparty_ids"]
        received = paise_from_rupee_str(stmt_by_id[credit_id]["credit_amount"])
        disc = int(r["expected_discrepancy_paise"])
        assert disc < 0
        assert received - s.amount_paise == disc
        # decomposable: either the flat bank charge or exact 1% round-half-up
        assert disc in (-G.BANK_CHARGE_PAISE, -((s.amount_paise + 50) // 100))


def test_duplicate_scenario_shape(world):
    dup_credits = [r for r in _by_scenario(world, G.SC_DUPLICATE, S.RT_BANK_CREDIT)
                   if r["expected_disposition"] == S.DUPLICATE_CREDIT]
    matched = [r for r in _by_scenario(world, G.SC_DUPLICATE, S.RT_BANK_CREDIT)
               if r["expected_disposition"] == S.MATCHED]
    assert len(dup_credits) == len(matched) >= 2
    for r in dup_credits:
        assert r["counterparty_ids"]  # duplicate points at its settlement


def test_manifest_counts_match_files(world):
    m = world["manifest"]["counts"]
    assert m["settlements"] == len(world["settlements"])
    assert m["bank_rows"] == len(world["statement"])
    assert m["golden_leg_a_rows"] == len(world["golden_a"])
    assert m["golden_leg_b_rows"] == len(world["golden_b"])
    assert m["payments"] == len(world["payments"])
    # Track 04 asks for a 50+ record batch; we are far beyond it
    assert m["settlements"] + m["bank_rows"] > 300


def test_golden_leg_b_complete(world):
    ids = {(r["record_type"], r["record_id"]) for r in world["golden_b"]}
    for o in world["orders"]:
        if o.order_id:
            assert (S.RT_ORDER, o.order_id) in ids
    for p in world["payments"]:
        assert (S.RT_PAYMENT, p.payment_id) in ids


def test_orphan_credits_match_no_settlement_utr(world):
    utrs = {s.utr for s in world["settlements"]}
    stmt_by_id = {r["txn_id"]: r for r in world["statement"]}
    for r in _by_scenario(world, G.SC_ORPHAN_CREDIT, S.RT_BANK_CREDIT):
        row = stmt_by_id[r["record_id"]]
        for u in utrs:
            assert u not in row["narration"] + row["ref_no"]
