"""The controller: triage policy, adapters, and the one-pass daily close."""

from __future__ import annotations

import os

import pytest

from recon import schemas as RS
from controller import triage as T
from controller.schemas import ExceptionItem

SEED_42 = os.path.join(os.path.dirname(__file__), "..", "data", "seeds", "42")


# --- The policy is total: every engine status is queued or excluded, never both


def test_vocabulary_totality_and_disjointness():
    engine_vocab = (set(RS.LEG_A_STATUSES) | set(RS.LEG_B_ORDER_STATUSES)
                    | set(RS.LEG_B_PAYMENT_STATUSES))
    severity_keys = set(T.SEVERITY)
    assert severity_keys & T.EXCLUDED_STATUSES == set()
    synthetics = {T.NEEDS_REVIEW}
    assert synthetics & engine_vocab == set()
    for status in engine_vocab:
        assert (status in severity_keys) != (status in T.EXCLUDED_STATUSES), status
    # nothing in the policy that no engine can emit (synthetics aside)
    assert severity_keys - synthetics <= engine_vocab


def test_every_queued_status_has_action_and_title():
    for status in T.SEVERITY:
        assert T.SUGGESTED_ACTIONS[status].strip()
        assert T._TITLES[status].strip()
    assert set(T.SEVERITY.values()) == {1, 2, 3}


# --- Leg A adapter ------------------------------------------------------------


def _leg_a(status, **kw):
    return RS.Decision(leg="A", kind="exception", status=status, **kw).to_dict()


@pytest.mark.parametrize("status,fields,money", [
    (RS.EXCEPTION_MISSING_BANK, {"settlement_ids": ["s1"], "expected_paise": 500}, 500),
    (RS.DUPLICATE_CREDIT, {"bank_txn_ids": ["b1"], "received_paise": 700}, 700),
    (RS.AMBIGUOUS_ABSTAIN, {"settlement_ids": ["s1"], "expected_paise": 300}, 300),
    (RS.AMBIGUOUS_ABSTAIN, {"bank_txn_ids": ["b1"], "received_paise": 250}, 250),
    (RS.EXCEPTION_MISSING_SETTLEMENT, {"bank_txn_ids": ["b1"], "received_paise": 900}, 900),
])
def test_leg_a_money_mapping(status, fields, money):
    items = T.items_from_leg_a([_leg_a(status, **fields)], attention=[])
    assert len(items) == 1
    assert items[0].money_at_risk_paise == money
    assert items[0].severity == T.SEVERITY[status]
    assert items[0].source == "recon_a"


def test_needs_review_overlay_and_matched_excluded():
    matched = RS.Decision(leg="A", kind="match", status=RS.MATCHED,
                          confidence=RS.CONF_EXACT, settlement_ids=["s1"]).to_dict()
    review = RS.Decision(leg="A", kind="match", status=RS.MATCHED_WITH_DISCREPANCY,
                         confidence=RS.CONF_NEEDS_REVIEW, settlement_ids=["s2"],
                         discrepancy_paise=-120).to_dict()
    scope = RS.Decision(leg="A", kind="out_of_scope", status=RS.OUT_OF_SCOPE,
                        bank_txn_ids=["b9"]).to_dict()
    items = T.items_from_leg_a([matched, review, scope], attention=[])
    assert [i.status for i in items] == [T.NEEDS_REVIEW]
    assert items[0].severity == 3 and items[0].money_at_risk_paise == 120


def test_overdue_fuses_as_enrichment_not_items():
    d = _leg_a(RS.EXCEPTION_MISSING_BANK, settlement_ids=["s1"], expected_paise=500)
    att = [{"id": "s1", "amount_paise": 500,
            "expected_date": "2025-06-02", "days_overdue": 17}]
    items = T.items_from_leg_a([d], attention=att)
    assert len(items) == 1
    assert items[0].days_overdue == 17
    assert items[0].due_date == "2025-06-02"
    # an overdue entry never becomes a queue item on its own
    assert all(i.source == "recon_a" for i in items)


def test_overdue_settlements_rule():
    from datetime import date

    from controller.overdue import overdue_settlements

    def setl(sid, created, settled):
        return RS.Settlement(settlement_id=sid, amount_paise=100, fees_paise=0,
                             tax_paise=0, utr="U", payment_count=1,
                             status="processed", created_at=created,
                             settled_at=settled)

    decisions = [
        RS.Decision(leg="A", kind="exception", status=RS.EXCEPTION_MISSING_BANK,
                    settlement_ids=["late"]),
        RS.Decision(leg="A", kind="exception", status=RS.AMBIGUOUS_ABSTAIN,
                    settlement_ids=["sunday"]),
        RS.Decision(leg="A", kind="exception", status=RS.EXCEPTION_MISSING_BANK,
                    settlement_ids=["future"]),
        RS.Decision(leg="A", kind="match", status=RS.MATCHED,
                    settlement_ids=["paid"]),
    ]
    settlements = [
        setl("late", "2025-06-01T01:15:00", "2025-06-02"),
        setl("sunday", "2025-06-06T01:15:00", "2025-06-08"),   # Sunday -> Mon 9th
        setl("future", "2025-06-09T01:15:00", "2025-06-11"),   # not yet due
        setl("paid", "2025-06-01T01:15:00", "2025-06-02"),
        setl("later", "2025-06-20T01:15:00", "2025-06-21"),    # created after close
    ]
    out = overdue_settlements(settlements, decisions, date(2025, 6, 10))
    assert out == [
        {"id": "late", "amount_paise": 100, "expected_date": "2025-06-02",
         "days_overdue": 8},
        {"id": "sunday", "amount_paise": 100, "expected_date": "2025-06-09",
         "days_overdue": 1},
    ]


# --- Leg B adapter ------------------------------------------------------------


def _orders():
    return [{"order_id": "o1", "amount_paise": 10_000},
            {"order_id": "o2", "amount_paise": 5_000}]


def _payments():
    return [{"payment_id": "p1", "amount_paise": 9_000},
            {"payment_id": "p2", "amount_paise": 5_000},
            {"payment_id": "p3", "amount_paise": 5_000}]


def test_leg_b_amount_mismatch_fuses_both_sides():
    ds = [
        RS.LegBDecision(record_type=RS.RT_ORDER, record_id="o1",
                        status=RS.EXCEPTION_AMOUNT_MISMATCH,
                        counterparty_ids=["p1"]).to_dict(),
        RS.LegBDecision(record_type=RS.RT_PAYMENT, record_id="p1",
                        status=RS.EXCEPTION_AMOUNT_MISMATCH,
                        counterparty_ids=["o1"]).to_dict(),
    ]
    items = T.items_from_leg_b(ds, _orders(), _payments())
    assert len(items) == 1
    assert items[0].record_ids == ["o1", "p1"]
    assert items[0].money_at_risk_paise == 1_000  # |9000 - 10000|


@pytest.mark.parametrize("rt,rid,status,money", [
    (RS.RT_ORDER, "o2", RS.EXCEPTION_UNPAID_ORDER, 5_000),
    (RS.RT_PAYMENT, "p3", RS.EXCEPTION_DUPLICATE_PAYMENT, 5_000),
    (RS.RT_PAYMENT, "p2", RS.EXCEPTION_PAYMENT_NO_ORDER, 5_000),
])
def test_leg_b_amounts_joined_from_raw_records(rt, rid, status, money):
    d = RS.LegBDecision(record_type=rt, record_id=rid, status=status).to_dict()
    items = T.items_from_leg_b([d], _orders(), _payments())
    assert len(items) == 1
    assert items[0].money_at_risk_paise == money
    assert items[0].source == "recon_b"


def test_leg_b_normal_lifecycle_excluded():
    ds = [RS.LegBDecision(record_type=RS.RT_ORDER, record_id="o1",
                          status=RS.ORDER_PAID).to_dict(),
          RS.LegBDecision(record_type=RS.RT_PAYMENT, record_id="p1",
                          status=RS.PAYMENT_FAILED).to_dict()]
    assert T.items_from_leg_b(ds, _orders(), _payments()) == []


def test_candidate_normalisation_both_shapes():
    recon = _leg_a(RS.AMBIGUOUS_ABSTAIN, settlement_ids=["s1"], expected_paise=10,
                   candidates=[{"record_id": "b7", "amount_paise": 10,
                                "reason": "two equal credits"}])
    [ri] = T.items_from_leg_a([recon], attention=[])
    assert ri.candidates == [{"id": "b7", "amount_paise": 10,
                              "note": "two equal credits"}]
    assert T._norm_candidates([{"id": "x1", "amount_paise": 5,
                                "note": "already normalised"}]) == \
        [{"id": "x1", "amount_paise": 5, "note": "already normalised"}]


# --- Ordering ------------------------------------------------------------------


def test_sort_queue_published_order():
    def mk(sev, money, source, status, rid):
        return ExceptionItem(source=source, status=status, severity=sev,
                             money_at_risk_paise=money, record_ids=[rid],
                             title="t")
    items = [
        mk(2, 500, "recon_a", "ambiguous_abstain", "s2"),
        mk(1, 100, "recon_a", "duplicate_credit", "b4"),
        mk(2, 500, "recon_a", "ambiguous_abstain", "s1"),
        mk(1, 900, "recon_a", "exception_missing_bank", "s3"),
        mk(3, 999, "recon_b", "exception_unpaid_order", "o1"),
        mk(2, 700, "recon_b", "exception_duplicate_payment", "p1"),
    ]
    out = T.sort_queue(items)
    key = [(i.severity, i.money_at_risk_paise, i.record_ids[0]) for i in out]
    assert key == [(1, 900, "s3"), (1, 100, "b4"), (2, 700, "p1"),
                   (2, 500, "s1"), (2, 500, "s2"), (3, 999, "o1")]
    # deterministic under shuffling
    assert [i.record_ids for i in T.sort_queue(list(reversed(items)))] == \
           [i.record_ids for i in out]


# --- The one-pass daily close -------------------------------------------------


def test_daily_close_byte_identical_determinism():
    import json
    from controller.close import daily_close
    a = json.dumps(daily_close(SEED_42).to_dict(), sort_keys=True)
    b = json.dumps(daily_close(SEED_42).to_dict(), sort_keys=True)
    assert a == b


def test_full_world_leg_a_runs_exactly_once(monkeypatch):
    from collections import Counter
    import controller.close as CC
    from recon import io_load

    full_rows = len(io_load.load_bank_rows(SEED_42))
    calls: Counter = Counter()
    real = CC.reconcile_leg_a

    def counting(site):
        def wrapped(settlements, bank_rows):
            calls[(site, len(bank_rows) == full_rows)] += 1
            return real(settlements, bank_rows)
        return wrapped

    monkeypatch.setattr(CC, "reconcile_leg_a", counting("close"))
    CC.daily_close(SEED_42)
    # exactly one full-world run, in the close itself; leg B, the journeys
    # and the overdue enrichment all reuse its decisions
    assert calls == Counter({("close", True): 1})


def test_queue_covers_every_minted_defect():
    """Completeness against the answer keys on a freshly generated world:
    every record the generator marked with a queue-worthy disposition must
    appear in the close's queue under that exact status."""
    import tempfile

    from recon import io_load
    from recon.generate import generate
    from controller.close import daily_close

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "w99")
        generate(99, out)
        close = daily_close(out)
        by_status: dict[str, list] = {}
        for i in close.queue:
            by_status.setdefault(i.status, []).append(i)

        def surfaced(status: str, record_id: str) -> bool:
            return any(record_id in i.record_ids
                       for i in by_status.get(status, []))

        checked = 0
        for row in io_load.load_golden(out, "A"):
            if row["expected_disposition"] in T.SEVERITY:
                assert surfaced(row["expected_disposition"], row["record_id"]), row
                checked += 1
        for row in io_load.load_golden(out, "B"):
            if row["expected_disposition"] in T.SEVERITY:
                assert surfaced(row["expected_disposition"], row["record_id"]), row
                checked += 1
        assert checked > 10  # the world actually minted defects


def test_close_carries_only_reconciliation():
    """The retired forecast and tax loops leave no trace in the close."""
    from controller.close import daily_close, render_markdown

    close = daily_close(SEED_42)
    assert set(close.to_dict()) == {
        "close_date", "world", "cash", "recon_summary", "leg_b_summary",
        "queue", "verify_panel", "counts", "decisions_a", "decisions_b",
        "warnings"}
    assert set(close.verify_panel) == {"leg_a", "samples"}
    assert {i.source for i in close.queue} <= {"recon_a", "recon_b"}
    report = render_markdown(close).lower()
    for phrase in ("forecast", "itc claimable", "tax loop", "min balance"):
        assert phrase not in report, phrase


def test_close_cli_smoke(tmp_path):
    import subprocess
    import sys

    repo = os.path.join(os.path.dirname(__file__), "..")
    report = tmp_path / "close.md"
    env = {**os.environ, "PYTHONPATH": "src"}
    r = subprocess.run(
        [sys.executable, "-m", "controller.close", os.path.abspath(SEED_42),
         "--report", str(report)],
        capture_output=True, text=True, env=env, cwd=repo)
    assert r.returncode == 0, r.stderr
    assert "daily close" in r.stdout
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Daily close")
    assert "## What needs a human today" in text


# --- The audit: the measured claim -------------------------------------------


def test_grade_queue_math():
    from controller.audit import grade_queue

    golden = [
        {"expected_disposition": "duplicate_credit", "record_id": "b1",
         "expected_discrepancy_paise": -1800},
        {"expected_disposition": "exception_missing_bank", "record_id": "s1",
         "expected_discrepancy_paise": -50_000},
        {"expected_disposition": "matched", "record_id": "s2",
         "expected_discrepancy_paise": 0},  # not queue-worthy
    ]
    item = {"status": "duplicate_credit", "record_ids": ["b1"],
            "money_at_risk_paise": 1800}
    synth = {"status": "needs_review", "record_ids": ["s9"],
             "money_at_risk_paise": 5}
    r = grade_queue([item, synth], golden)
    assert r["golden_queue_worthy"] == 2 and r["surfaced"] == 1
    assert r["queue_recall"] == 0.5
    assert r["money_recall"] == round(1800 / 51_800, 4)
    # the synthetic is excluded from the precision denominator
    assert r["gradable_items"] == 1 and r["queue_precision"] == 1.0

    crossed = {"status": "exception_missing_settlement", "record_ids": ["b1"],
               "money_at_risk_paise": 1800}
    r2 = grade_queue([crossed], golden)
    assert r2["surfaced"] == 0 and r2["queue_precision"] == 0.0


def test_audit_engine_row_is_perfect_on_committed_world():
    from controller.audit import audit_close

    r = audit_close(SEED_42)
    assert r["queue_recall"] == 1.0
    assert r["queue_precision"] == 1.0
    # no queue-worthy leg A/B golden row carries a discrepancy (only the
    # retired tax rows did), so money recall has nothing to weigh
    assert r["money_recall"] is None
    assert r["verify_violation_count"] == 0
    assert r["missed"] == []


def test_naive_engines_fail_the_audit():
    """Hardness canary: the same triage layer over the naive baselines must
    collapse measurably — if it didn't, the queue metric would be trivial."""
    from controller.audit import audit_close, audit_naive

    engine = audit_close(SEED_42)
    naive = audit_naive(SEED_42)
    assert naive["queue_recall"] < engine["queue_recall"]
    assert naive["queue_precision"] < 0.9
    # the naive matcher forces the ambiguous twins into matches, so the
    # abstentions a human should see never reach the queue
    assert any(m["status"] == "ambiguous_abstain" for m in naive["missed"])


def test_controller_is_blind_to_answer_keys():
    """Canary #3: no controller module except the audit may even mention the
    answer-key files."""
    src = os.path.join(os.path.dirname(__file__), "..", "src", "controller")
    scanned = 0
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py") or name == "audit.py":
            continue
        with open(os.path.join(src, name), encoding="utf-8") as f:
            assert "golden" not in f.read().lower(), name
        scanned += 1
    assert scanned >= 5
