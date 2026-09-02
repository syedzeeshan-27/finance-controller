"""Benchmark math on a tiny hand-computed world: the grader itself is graded."""

from recon import schemas as S
from recon.benchmark import grade_leg_a


def g(record_type, record_id, disposition, counterparties=(), scenario="t"):
    return {
        "record_type": record_type, "record_id": record_id,
        "expected_disposition": disposition,
        "counterparty_set": frozenset(counterparties),
        "scenario_tag": scenario, "expected_discrepancy_paise": 0, "notes": "",
    }


def match(sids, tids, status=S.MATCHED, confidence=S.CONF_EXACT):
    return S.Decision(leg="A", kind="match", status=status,
                      settlement_ids=list(sids), bank_txn_ids=list(tids),
                      confidence=confidence,
                      evidence=[{"rule": "utr_exact", "detail": "t"}])


def abstain(sids=(), tids=(), candidates=()):
    return S.Decision(leg="A", kind="exception", status=S.AMBIGUOUS_ABSTAIN,
                      settlement_ids=list(sids), bank_txn_ids=list(tids),
                      candidates=[{"record_id": c, "reason": "t"} for c in candidates])


def exception(status, sids=(), tids=()):
    return S.Decision(leg="A", kind="exception", status=status,
                      settlement_ids=list(sids), bank_txn_ids=list(tids),
                      candidates=[{"record_id": "", "reason": "t"}])


class TestHandComputed:
    def test_perfect_two_matches(self):
        golden = [
            g(S.RT_SETTLEMENT, "s1", S.MATCHED, ["c1"]),
            g(S.RT_BANK_CREDIT, "c1", S.MATCHED, ["s1"]),
        ]
        r = grade_leg_a([match(["s1"], ["c1"])], golden)
        assert (r["true_positives"], r["false_positives"], r["false_negatives"]) == (2, 0, 0)
        assert r["precision"] == 1.0 and r["recall"] == 1.0 and r["f1"] == 1.0

    def test_forced_matches_cost_precision(self):
        # s1<->c1 correct; s2 truly missing-bank; c2 truly ambiguous.
        # Engine wrongly force-matches s2<->c2.
        golden = [
            g(S.RT_SETTLEMENT, "s1", S.MATCHED, ["c1"]),
            g(S.RT_BANK_CREDIT, "c1", S.MATCHED, ["s1"]),
            g(S.RT_SETTLEMENT, "s2", S.EXCEPTION_MISSING_BANK),
            g(S.RT_BANK_CREDIT, "c2", S.AMBIGUOUS_ABSTAIN, ["s2", "s3"]),
        ]
        decisions = [match(["s1"], ["c1"]), match(["s2"], ["c2"])]
        r = grade_leg_a(decisions, golden)
        # TP: s1, c1. FP: s2 (matched a missing), c2 (matched an ambiguous).
        assert (r["true_positives"], r["false_positives"]) == (2, 2)
        assert r["precision"] == 0.5
        assert r["recall"] == 1.0            # both matchable rows were found
        assert r["disposition_accuracy"] == 0.5
        assert r["ambiguous_correctly_abstained"] == 0

    def test_correct_abstention_scores(self):
        golden = [
            g(S.RT_SETTLEMENT, "s1", S.AMBIGUOUS_ABSTAIN, ["c1"]),
            g(S.RT_SETTLEMENT, "s2", S.AMBIGUOUS_ABSTAIN, ["c1"]),
            g(S.RT_BANK_CREDIT, "c1", S.AMBIGUOUS_ABSTAIN, ["s1", "s2"]),
        ]
        decisions = [
            abstain(sids=["s1"], candidates=["c1", "s2"]),
            abstain(sids=["s2"], candidates=["c1", "s1"]),
            abstain(tids=["c1"], candidates=["s1", "s2"]),
        ]
        r = grade_leg_a(decisions, golden)
        assert r["ambiguous_correctly_abstained"] == 3
        assert r["disposition_accuracy"] == 1.0
        assert r["false_positives"] == 0

    def test_abstention_without_candidates_is_wrong(self):
        golden = [g(S.RT_BANK_CREDIT, "c1", S.AMBIGUOUS_ABSTAIN, ["s1", "s2"])]
        r = grade_leg_a([abstain(tids=["c1"], candidates=["s1"])], golden)
        assert r["ambiguous_correctly_abstained"] == 0  # candidate set incomplete

    def test_wrong_split_subset_is_fp_and_fn(self):
        golden = [
            g(S.RT_SETTLEMENT, "s1", S.MATCHED_SPLIT, ["c1", "c2", "c3"]),
        ]
        # engine found only 2 of the 3 parts
        r = grade_leg_a([match(["s1"], ["c1", "c2"], status=S.MATCHED_SPLIT,
                               confidence=S.CONF_HIGH)], golden)
        assert r["true_positives"] == 0
        assert r["false_positives"] == 1
        assert r["false_negatives"] == 1

    def test_status_must_match_exactly(self):
        # right links, wrong classification: short-paid credit reported clean
        golden = [g(S.RT_SETTLEMENT, "s1", S.MATCHED_WITH_DISCREPANCY, ["c1"])]
        r = grade_leg_a([match(["s1"], ["c1"], status=S.MATCHED)], golden)
        assert r["true_positives"] == 0 and r["false_positives"] == 1

    def test_unreported_record_is_a_miss(self):
        golden = [
            g(S.RT_SETTLEMENT, "s1", S.MATCHED, ["c1"]),
            g(S.RT_BANK_CREDIT, "c1", S.MATCHED, ["s1"]),
        ]
        r = grade_leg_a([], golden)
        assert r["false_negatives"] == 2 and r["true_positives"] == 0
        assert r["disposition_accuracy"] == 0.0

    def test_needs_review_not_counted_auto_resolved(self):
        golden = [
            g(S.RT_SETTLEMENT, "s1", S.MATCHED_WITH_DISCREPANCY, ["c1"]),
            g(S.RT_BANK_CREDIT, "c1", S.MATCHED_WITH_DISCREPANCY, ["s1"]),
        ]
        d = match(["s1"], ["c1"], status=S.MATCHED_WITH_DISCREPANCY,
                  confidence=S.CONF_NEEDS_REVIEW)
        r = grade_leg_a([d], golden)
        assert r["auto_resolution_rate"] == 0.0
        assert r["true_positives"] == 2  # still correct, just not auto-approved
