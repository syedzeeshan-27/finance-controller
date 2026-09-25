"""The deterministic enumerator behind the prefilter and the no-LLM ablation."""

import csv

import pytest

from agent import resolve_verifier as RV
from agent import solver

# Non-round amounts on purpose: with round figures, subsets of settlements
# add up to other settlements by coincidence (the enumerator finds them).
SETTLEMENTS = [
    ("setl_A1", 1_000_123, "HDFC111111111111", "2025-05-01T01:15:00", "ACME RETAIL"),
    ("setl_B2", 2_000_457, "ICIC222222222222", "2025-05-01T01:15:00", "ACME RETAIL"),
    ("setl_D4", 500_311, "UTIB444444444444", "2025-05-02T01:15:00", "ACME RETAIL"),
    ("setl_E5", 500_311, "KKBK555555555555", "2025-05-02T01:15:00", "ACME FOODS"),
    ("setl_F6", 700_529, "YESB666666666666", "2025-05-03T01:15:00", "ACME FOODS"),
    ("setl_G7", 800_683, "IDFB777777777777", "2025-05-03T01:15:00", "ACME FOODS"),
    ("setl_H8", 450_977, "SBIN888888888888", "2025-05-03T01:15:00", "ACME FOODS"),
    ("setl_Z9", 9_999_901, "PUNB999999999999", "2025-05-03T01:15:00", "ACME FOODS"),
]
CREDITS = [
    ("BANK000002", "03/05/2025", "NEFT CR RAZORPAY SETTL less chgs 59.00 on 03 MAY", 1_994_557),
    ("BANK000004", "04/05/2025", "NEFT CR RAZORPAY SETTLEMENT", 500_311),
    ("BANK000005", "04/05/2025", "NEFT CR RAZORPAY SETTLEMENT", 500_311),
    ("BANK000007", "05/05/2025", "RAZORPAY ACME FOODS SETTLEMENT", 1_501_212),
    ("BANK000010", "05/05/2025", "RTGS RAZORPAY SBIN888888888888/1", 200_000),
    ("BANK000011", "06/05/2025", "RTGS RAZORPAY SBIN888888888888/2", 250_977),
]


@pytest.fixture
def view(tmp_path):
    with open(tmp_path / "settlements.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "amount_paise", "fees_paise", "tax_paise",
                    "utr", "payment_count", "status", "created_at", "settled_at"])
        for sid, net, utr, created, _ in SETTLEMENTS:
            w.writerow([sid, net, 0, 0, utr, 1, "processed", created, created[:10]])
    with open(tmp_path / "bank_statement.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "txn_date", "value_date", "narration", "ref_no",
                    "debit_amount", "credit_amount", "balance"])
        for tid, vd, narr, credit in CREDITS:
            w.writerow([tid, vd, vd, narr, "", "",
                        f"{credit // 100}.{credit % 100:02d}", "1,00,000.00"])
    with open(tmp_path / "settlement_merchants.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "merchant_name"])
        for sid, *_, name in SETTLEMENTS:
            w.writerow([sid, name])
    return RV.load_world_view(str(tmp_path))


def _ids(p):
    return p["settlement_ids"], p["bank_row_ids"], p["deduction_paise"]


def test_unique_narrated_deduction(view):
    # the "03" in "03 MAY" is a date, not a deduction candidate
    e = solver.enumerate_explanations(view, ["BANK000002"], set())
    assert [_ids(p) for p in e.explanations] == [(["setl_B2"], ["BANK000002"], 5_900)]
    assert solver.solve(view, ["BANK000002"], set())["verdict"] == "match"


def test_clueless_twins_have_no_verifiable_explanation(view):
    for item in (["BANK000004"], ["setl_D4"]):
        e = solver.enumerate_explanations(view, item, set())
        assert e.explanations == []
    assert solver.solve(view, ["BANK000004"], set())["verdict"] != "match"


def test_merge_found(view):
    e = solver.enumerate_explanations(view, ["BANK000007"], set())
    assert (["setl_F6", "setl_G7"], ["BANK000007"], 0) in [_ids(p) for p in e.explanations]


def test_split_found_from_the_settlement_side(view):
    e = solver.enumerate_explanations(view, ["setl_H8"], set())
    assert (["setl_H8"], ["BANK000010", "BANK000011"], 0) in [_ids(p) for p in e.explanations]


def test_nothing_to_explain(view):
    e = solver.enumerate_explanations(view, ["setl_Z9"], set())
    assert e.explanations == [] and not e.truncated
    assert solver.solve(view, ["setl_Z9"], set())["verdict"] == "exception"


def test_claimed_records_are_never_used(view):
    e = solver.enumerate_explanations(view, ["BANK000002"], {"setl_B2"})
    assert e.explanations == []
