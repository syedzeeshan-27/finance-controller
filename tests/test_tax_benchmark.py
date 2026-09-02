"""Tax grader math on hand-built fixtures."""

from tax import schemas as TS
from tax.benchmark import _aggregate, grade_tax


def golden_row(rt, rid, disp, cps="", sc="tax_clean", disc=0):
    return {"record_type": rt, "record_id": rid,
            "expected_disposition": disp,
            "counterparty_set": frozenset(x for x in cps.split(";") if x),
            "scenario_tag": sc, "expected_discrepancy_paise": disc}


def match(pid, lid, status=TS.ITC_MATCHED, books=18_000, filed=18_000):
    return TS.TaxDecision(loop="itc", kind="match", status=status,
                          book_ids=[pid], filed_ids=[lid],
                          confidence=TS.CONF_EXACT, books_paise=books,
                          filed_paise=filed, discrepancy_paise=filed - books)


class TestGradeTax:
    def test_wrong_counterparty_is_wrong_and_counts_as_false_positive(self):
        golden = [
            golden_row(TS.RT_PURCHASE, "P1", TS.ITC_MATCHED, "L1"),
            golden_row(TS.RT_PURCHASE, "P2", TS.ITC_MATCHED, "L2"),
            golden_row(TS.RT_2B_LINE, "L1", TS.ITC_MATCHED, "P1"),
            golden_row(TS.RT_2B_LINE, "L2", TS.ITC_MATCHED, "P2"),
        ]
        crossed = [match("P1", "L2"), match("P2", "L1")]
        r = grade_tax(crossed, golden)
        assert r["disposition_accuracy"] == 0.0
        assert r["false_positives"] == 4 and r["true_positives"] == 0
        # money: both claims pair records golden says belong elsewhere
        assert r["money"]["false_claim_paise"] == 36_000

    def test_correct_pairing_grades_perfectly(self):
        golden = [
            golden_row(TS.RT_PURCHASE, "P1", TS.ITC_MATCHED, "L1"),
            golden_row(TS.RT_2B_LINE, "L1", TS.ITC_MATCHED, "P1"),
        ]
        r = grade_tax([match("P1", "L1")], golden)
        assert r["disposition_accuracy"] == 1.0
        assert r["precision"] == 1.0 and r["recall"] == 1.0
        assert r["money"]["false_claim_paise"] == 0
        assert r["money"]["claimed_itc_paise"] == 18_000

    def test_claim_on_blocked_golden_is_false_claim(self):
        golden = [
            golden_row(TS.RT_PURCHASE, "P1", TS.BLOCKED_CREDIT_NO_ITC, "L1",
                       sc="tax_blocked_credit"),
            golden_row(TS.RT_2B_LINE, "L1", TS.BLOCKED_CREDIT_NO_ITC, "P1",
                       sc="tax_blocked_credit"),
        ]
        r = grade_tax([match("P1", "L1")], golden)
        assert r["disposition_accuracy"] == 0.0
        assert r["money"]["false_claim_paise"] == 18_000
        assert r["false_positives"] == 2

    def test_unreported_record_is_wrong(self):
        golden = [golden_row(TS.RT_OBLIGATION_PERIOD, "gst:2025-05",
                             TS.PAID_ON_TIME, "BANK000070",
                             sc="tax_obligation_clean")]
        r = grade_tax([], golden)
        assert r["disposition_accuracy"] == 0.0

    def test_duplicate_line_graded_by_its_own_decision(self):
        golden = [
            golden_row(TS.RT_PURCHASE, "P1", TS.ITC_MATCHED, "L1",
                       sc="tax_duplicate_2b"),
            golden_row(TS.RT_2B_LINE, "L1", TS.ITC_MATCHED, "P1",
                       sc="tax_duplicate_2b"),
            golden_row(TS.RT_2B_LINE, "L2", TS.DUPLICATE_2B_LINE, "P1",
                       sc="tax_duplicate_2b"),
        ]
        dup = TS.TaxDecision(loop="itc", kind="exception",
                             status=TS.DUPLICATE_2B_LINE,
                             book_ids=["P1"], filed_ids=["L2"])
        r = grade_tax([match("P1", "L1"), dup], golden)
        assert r["disposition_accuracy"] == 1.0


def test_aggregate_math():
    rows = [{"f1": 0.5}, {"f1": 1.0}, {"f1": None}]
    agg = _aggregate(rows, "f1")
    assert agg == {"mean": 0.75, "min": 0.5, "max": 1.0}
    assert _aggregate([], "f1") == {"mean": None, "min": None, "max": None}
