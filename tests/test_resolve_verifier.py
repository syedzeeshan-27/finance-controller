"""The resolver's verifier: every rule accepts what it should and rejects what
it should, on a hand-built world. These tests are frozen with the verifier
before any agent run (see reports/preregistration.md)."""

import ast
import csv
import os

import pytest

from agent import resolve_verifier as RV

SETTLEMENTS = [
    # id, net paise, utr, created
    ("setl_AAAAAAAAAAAAA1", 1_000_000, "HDFC111111111111", "2025-05-01T01:15:00"),
    ("setl_BBBBBBBBBBBBB2", 2_000_000, "ICIC222222222222", "2025-05-01T01:15:00"),
    ("setl_CCCCCCCCCCCCC3", 3_000_000, "SBIN333333333333", "2025-05-02T01:15:00"),
    ("setl_DDDDDDDDDDDDD4", 500_000, "UTIB444444444444", "2025-05-02T01:15:00"),
    ("setl_EEEEEEEEEEEEE5", 500_000, "KKBK555555555555", "2025-05-02T01:15:00"),
    ("setl_FFFFFFFFFFFFF6", 700_000, "YESB666666666666", "2025-05-03T01:15:00"),
    ("setl_GGGGGGGGGGGGG7", 800_000, "IDFB777777777777", "2025-05-03T01:15:00"),
    ("setl_HHHHHHHHHHHHH8", 900_000, "HDFC888888888888", "2025-04-01T01:15:00"),
    ("setl_IIIIIIIIIIIII9", 1_000_000, "PUNB121212121212", "2025-05-01T01:15:00"),
]
MERCHANTS = {
    "setl_AAAAAAAAAAAAA1": "ACME RETAIL", "setl_BBBBBBBBBBBBB2": "ACME RETAIL",
    "setl_CCCCCCCCCCCCC3": "ACME FOODS", "setl_DDDDDDDDDDDDD4": "ACME RETAIL",
    "setl_EEEEEEEEEEEEE5": "ACME FOODS", "setl_FFFFFFFFFFFFF6": "ACME FOODS",
    "setl_GGGGGGGGGGGGG7": "ACME FOODS", "setl_HHHHHHHHHHHHH8": "ACME RETAIL",
    "setl_IIIIIIIIIIIII9": "ACME RETAIL",
}
CREDITS = [
    # txn_id, value date, narration, ref, credit paise (debit rows have 0)
    ("BANK000001", "03/05/2025", "NEFT CR-HDFC111111111111-RAZORPAY SOFTWARE", "", 1_000_000),
    ("BANK000002", "03/05/2025", "NEFT CR RAZORPAY SETTL less chgs 59.00", "NEFTIN1", 1_994_100),
    ("BANK000003", "04/05/2025", "RZP SETTLEMENT  LESS  CHG 25.00 GST 4.50", "", 2_997_050),
    ("BANK000004", "04/05/2025", "NEFT CR RAZORPAY SETTLEMENT", "", 500_000),
    ("BANK000005", "04/05/2025", "NEFT CR RAZORPAY setl_DDDDDDDDDDDDD4", "", 500_000),
    ("BANK000006", "05/05/2025", "IMPS-P2A CR-KHATRI TRADERS-INV 4471", "", 700_000),
    ("BANK000007", "05/05/2025", "RAZORPAY ACME FOODS SETTLEMENT", "", 1_500_000),
    ("BANK000008", "06/05/2025", "NEFT CR RAZORPAY HDFC888888888888", "", 900_000),
    ("BANK000009", "06/05/2025", "UPI-SWIGGY-123@ybl", "", 0),
]


@pytest.fixture
def world(tmp_path):
    with open(tmp_path / "settlements.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "amount_paise", "fees_paise", "tax_paise",
                    "utr", "payment_count", "status", "created_at", "settled_at"])
        for sid, net, utr, created in SETTLEMENTS:
            w.writerow([sid, net, 0, 0, utr, 1, "processed", created, created[:10]])
    with open(tmp_path / "bank_statement.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "txn_date", "value_date", "narration", "ref_no",
                    "debit_amount", "credit_amount", "balance"])
        for tid, vd, narr, ref, credit in CREDITS:
            debit = "" if credit else "100.00"
            amount = f"{credit // 100}.{credit % 100:02d}" if credit else ""
            w.writerow([tid, vd, vd, narr, ref, debit, amount, "1,00,000.00"])
    with open(tmp_path / "settlement_merchants.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "merchant_name"])
        for sid, name in MERCHANTS.items():
            w.writerow([sid, name])
    return RV.load_world_view(str(tmp_path))


def _match(sids, tids, ded=0, evidence="", reason="because"):
    return {"verdict": "match", "settlement_ids": sids, "bank_row_ids": tids,
            "deduction_paise": ded, "deduction_evidence": evidence,
            "reason": reason}


def _check(world, proposal, items, claimed=(), strict=True):
    return RV.verify_proposal(world, proposal, items, set(claimed), strict=strict)


# --- accepts ---------------------------------------------------------------------

def test_exact_utr_match_accepted(world):
    v = _check(world, _match(["setl_AAAAAAAAAAAAA1"], ["BANK000001"]), ["BANK000001"])
    assert v.accepted, v.details
    assert v.status == "matched" and v.deduction_paise == 0


def test_narrated_deduction_accepted(world):
    p = _match(["setl_BBBBBBBBBBBBB2"], ["BANK000002"], 5_900, "less chgs 59.00")
    v = _check(world, p, ["BANK000002"])
    assert v.accepted, v.details
    assert v.status == "matched_with_discrepancy"


def test_zero_deduction_ignores_evidence(world):
    p = _match(["setl_AAAAAAAAAAAAA1"], ["BANK000001"], 0, "anything at all")
    assert _check(world, p, ["BANK000001"]).accepted


def test_settlement_id_breaks_a_twin_tie(world):
    p = _match(["setl_DDDDDDDDDDDDD4"], ["BANK000005"])
    v = _check(world, p, ["BANK000005"])
    assert v.accepted, v.details


def test_merge_accepted(world):
    p = _match(["setl_FFFFFFFFFFFFF6", "setl_GGGGGGGGGGGGG7"], ["BANK000007"])
    v = _check(world, p, ["BANK000007"])
    assert v.accepted, v.details
    assert v.status == "matched_merged"


# --- the brief's minimum rules ------------------------------------------------------

@pytest.mark.parametrize("proposal,code", [
    ({"verdict": "maybe"}, "shape"),
    (_match([], ["BANK000001"]), "shape"),
    (_match(["setl_AAAAAAAAAAAAA1"], ["BANK000001"], ded=True), "shape"),
    (_match(["setl_AAAAAAAAAAAAA1", "setl_AAAAAAAAAAAAA1"], ["BANK000001"]), "shape"),
    (_match(["setl_NOPE"], ["BANK000001"]), "unknown_id"),
    (_match(["setl_AAAAAAAAAAAAA1"], ["BANK000009"]), "unknown_id"),
])
def test_malformed_or_unknown_rejected(world, proposal, code):
    v = _check(world, proposal, ["BANK000001"])
    assert not v.accepted and code in v.codes


def test_item_records_must_be_covered(world):
    v = _check(world, _match(["setl_AAAAAAAAAAAAA1"], ["BANK000001"]),
               ["setl_CCCCCCCCCCCCC3"])
    assert "item" in v.codes


def test_claimed_record_rejected(world):
    v = _check(world, _match(["setl_AAAAAAAAAAAAA1"], ["BANK000001"]),
               ["BANK000001"], claimed={"setl_AAAAAAAAAAAAA1"})
    assert "claimed" in v.codes


def test_many_to_many_rejected(world):
    p = _match(["setl_DDDDDDDDDDDDD4", "setl_EEEEEEEEEEEEE5"],
               ["BANK000004", "BANK000005"])
    v = _check(world, p, ["BANK000004"])
    assert "many_to_many" in v.codes


def test_stated_deduction_must_equal_arithmetic(world):
    p = _match(["setl_BBBBBBBBBBBBB2"], ["BANK000002"], 59, "less chgs 59.00")
    v = _check(world, p, ["BANK000002"])
    assert "arithmetic" in v.codes


def test_credit_above_settlement_rejected(world):
    p = _match(["setl_DDDDDDDDDDDDD4"], ["BANK000007"], -1_000_000, "")
    v = _check(world, p, ["BANK000007"])
    assert "arithmetic" in v.codes


def test_evidence_must_be_verbatim(world):
    p = _match(["setl_BBBBBBBBBBBBB2"], ["BANK000002"], 5_900, "less charges 59.00")
    assert "evidence" in _check(world, p, ["BANK000002"]).codes


def test_evidence_must_contain_the_figure(world):
    p = _match(["setl_BBBBBBBBBBBBB2"], ["BANK000002"], 5_900, "RAZORPAY SETTL")
    assert "evidence" in _check(world, p, ["BANK000002"]).codes


def test_evidence_whitespace_is_collapsed(world):
    # the narration has double spaces; a single-spaced quote still matches,
    # but the deduction is two figures (25.00 + 4.50) and is rejected
    p = _match(["setl_CCCCCCCCCCCCC3"], ["BANK000003"], 2_950,
               "LESS CHG 25.00 GST 4.50")
    v = _check(world, p, ["BANK000003"])
    assert v.codes == ["evidence"]
    assert "does not appear as a money figure" in v.details[0]


def test_window_enforced(world):
    p = _match(["setl_HHHHHHHHHHHHH8"], ["BANK000008"])
    v = _check(world, p, ["BANK000008"])
    assert "window" in v.codes


def test_exception_and_abstain_are_not_resolutions(world):
    for verdict in ("exception", "abstain"):
        v = _check(world, {"verdict": verdict}, ["BANK000004"])
        assert not v.accepted and v.codes == ["no_match_proposed"]


# --- the stricter rules ------------------------------------------------------------

def test_twin_without_clue_is_a_rival(world):
    p = _match(["setl_DDDDDDDDDDDDD4"], ["BANK000004"])
    v = _check(world, p, ["BANK000004"])
    assert "rival" in v.codes
    # the brief-minimum verifier would have let it through
    assert _check(world, p, ["BANK000004"], strict=False).accepted


def test_credit_side_rival_detected(world):
    # D4 is named in BANK000005; proposing it against the unlabelled twin
    # credit leaves the labelled one as a same-amount rival
    p = _match(["setl_DDDDDDDDDDDDD4"], ["BANK000004"])
    v = _check(world, p, ["BANK000004"])
    assert "rival" in v.codes


def test_provenance_required(world):
    p = _match(["setl_FFFFFFFFFFFFF6"], ["BANK000006"])
    v = _check(world, p, ["BANK000006"])
    assert "provenance" in v.codes
    assert _check(world, p, ["BANK000006"], strict=False).accepted


def test_contradicting_settlement_id(world):
    p = _match(["setl_EEEEEEEEEEEEE5"], ["BANK000005"])
    v = _check(world, p, ["BANK000005"])
    assert "contradiction" in v.codes


def test_contradicting_utr(world):
    # I9 has the same net as A1, but the credit carries A1's UTR
    p = _match(["setl_IIIIIIIIIIIII9"], ["BANK000001"])
    v = _check(world, p, ["BANK000001"])
    assert "contradiction" in v.codes
    assert _check(world, p, ["BANK000001"], strict=False).accepted


def test_contradicting_merchant_name(world):
    p = _match(["setl_AAAAAAAAAAAAA1", "setl_DDDDDDDDDDDDD4"], ["BANK000007"])
    v = _check(world, p, ["BANK000007"])
    assert "contradiction" in v.codes   # ACME FOODS named, both are ACME RETAIL


def test_foreign_utr_token(world, tmp_path):
    # rewrite one credit to carry a well-formed UTR that belongs to nobody
    path = tmp_path / "bank_statement.csv"
    text = path.read_text(encoding="utf-8").replace(
        "NEFT CR RAZORPAY SETTLEMENT", "NEFT CR RAZORPAY PUNB999999999999")
    path.write_text(text, encoding="utf-8")
    view = RV.load_world_view(str(tmp_path))
    v = RV.verify_proposal(view, _match(["setl_DDDDDDDDDDDDD4"], ["BANK000004"]),
                           ["BANK000004"], set())
    assert "contradiction" in v.codes


def test_batch_conflict_rejects_both(world):
    a = (["BANK000001"], _match(["setl_AAAAAAAAAAAAA1"], ["BANK000001"]))
    b = (["setl_AAAAAAAAAAAAA1"], _match(["setl_AAAAAAAAAAAAA1"], ["BANK000001"]))
    va, vb = RV.verify_batch(world, [a, b], set())
    assert not va.accepted and not vb.accepted
    assert "conflict" in va.codes and "conflict" in vb.codes


# --- the grammar ---------------------------------------------------------------------

@pytest.mark.parametrize("text,figures", [
    ("less chgs 59.00", [5_900]),
    ("TDS 194O 412.30", [41_230]),
    ("Rs.59/-", [5_900]),
    ("INR1,234.50", [123_450]),
    ("₹ 1,23,456.78", [12_345_678]),
    ("CHG 25.00 GST 4.50", [2_500, 450]),
    ("59.00Dr", [5_900]),
    ("net of refund 450.00", [45_000]),
    ("on 12 MAY", []),
    ("MAY 12 settl", []),
    ("dated 12/05/2025", []),
    ("at 10:45:10", []),
    ("UTIB0001234 ref", []),
    ("fee 5%", []),
    ("setl_Ab12Cd34", []),
    ("amt 59.123", []),
    ("bars59", []),
])
def test_money_grammar(text, figures):
    assert RV.money_figures(text) == figures


@pytest.mark.parametrize("source,quote,paise,ok", [
    ("NEFT UTIB0001234 RAZORPAY", "0001234", 123_400, False),   # slice of a ref
    ("TDS 194O 412.30", "412", 41_200, False),                    # slice of a figure
    ("TDS 194O 412.30", "TDS 194O 412.30", 41_230, True),
    ("less  chgs 59.00", "less chgs 59.00", 5_900, True),         # whitespace collapsed
    ("CHG 25.00 GST 4.50", "GST 4.50", 450, True),
    ("CHG 25.00 GST 4.50", "CHG 25.00 GST 4.50", 2_950, False),  # sum of two figures
])
def test_figures_are_read_in_context(source, quote, paise, ok):
    assert RV.figure_in_quote(source, quote, paise) is ok


# --- independence ---------------------------------------------------------------------

def test_verifier_imports_only_the_standard_library():
    path = os.path.join(os.path.dirname(__file__), os.pardir, "src", "agent",
                        "resolve_verifier.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"__future__", "csv", "os", "re", "dataclasses", "datetime"}, roots
