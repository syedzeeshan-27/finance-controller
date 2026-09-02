"""Unified controller: triage policy, adapters, and the one-pass daily close."""

from __future__ import annotations

import os

import pytest

from recon import schemas as RS
from tax import schemas as TS
from controller import triage as T
from controller.schemas import ExceptionItem

SEED_42 = os.path.join(os.path.dirname(__file__), "..", "data", "seeds", "42")


# --- The policy is total: every engine status is queued or excluded, never both


def test_vocabulary_totality_and_disjointness():
    engine_vocab = (set(RS.LEG_A_STATUSES) | set(RS.LEG_B_ORDER_STATUSES)
                    | set(RS.LEG_B_PAYMENT_STATUSES) | set(TS.ITC_STATUSES)
                    | set(TS.TDS_STATUSES) | set(TS.OBLIGATION_STATUSES))
    severity_keys = set(T.SEVERITY)
    assert severity_keys & T.EXCLUDED_STATUSES == set()
    synthetics = {T.NEEDS_REVIEW, T.FORECAST_BELOW_THRESHOLD}
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


def test_attention_fuses_as_enrichment_not_items():
    d = _leg_a(RS.EXCEPTION_MISSING_BANK, settlement_ids=["s1"], expected_paise=500)
    att = [{"kind": "overdue_settlement", "id": "s1", "amount_paise": 500,
            "expected_date": "2025-06-02", "days_overdue": 17, "note": "n"}]
    items = T.items_from_leg_a([d], attention=att)
    assert len(items) == 1
    assert items[0].days_overdue == 17
    assert items[0].due_date == "2025-06-02"
    # an attention entry never becomes a queue item on its own
    assert all(i.source != "forecast" for i in items)


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


# --- Tax adapter --------------------------------------------------------------


def _tax(loop, status, **kw):
    return TS.TaxDecision(loop=loop, kind="exception", status=status, **kw).to_dict()


@pytest.mark.parametrize("loop,status,fields,money,severity", [
    ("itc", TS.ITC_MISSING_IN_2B,
     {"books_paise": 1800, "discrepancy_paise": -1800}, 1800, 2),
    ("itc", TS.DUPLICATE_2B_LINE, {"filed_paise": 900}, 900, 2),
    ("itc", TS.UNKNOWN_INVOICE_IN_2B, {"filed_paise": 450}, 450, 2),
    ("itc", TS.ITC_AMOUNT_MISMATCH, {"discrepancy_paise": -55}, 55, 3),
    ("itc", TS.ITC_HEAD_MISMATCH, {"books_paise": 700, "filed_paise": 700}, 0, 3),
    ("tds", TS.TDS_MISSING_IN_26AS, {"discrepancy_paise": -625}, 625, 2),
    ("tds", TS.TDS_DUPLICATE_26AS, {"filed_paise": 625}, 625, 2),
    ("tds", TS.TDS_UNKNOWN_ENTRY, {"filed_paise": 300}, 300, 3),
    ("tds", TS.TDS_AMOUNT_MISMATCH, {"discrepancy_paise": 40}, 40, 3),
    ("tds", TS.TDS_WRONG_QUARTER, {"books_paise": 625, "filed_paise": 625}, 0, 3),
    ("obligation", TS.NOT_PAID, {"books_paise": 12_000}, 12_000, 1),
    ("obligation", TS.PAID_SHORT,
     {"books_paise": 10_000, "filed_paise": 9_600, "discrepancy_paise": -400}, 400, 1),
    ("obligation", TS.PAID_LATE,
     {"books_paise": 10_000, "filed_paise": 10_000, "discrepancy_paise": 0}, 10_000, 1),
    ("obligation", TS.UNVERIFIABLE_PRIOR_PERIOD, {"filed_paise": 7_700}, 7_700, 3),
])
def test_tax_money_and_severity(loop, status, fields, money, severity):
    items = T.items_from_tax([_tax(loop, status, **fields)])
    assert len(items) == 1
    assert items[0].money_at_risk_paise == money
    assert items[0].severity == severity
    assert items[0].source == f"tax_{loop}"


def test_tax_tiles_statuses_excluded_from_queue():
    ds = [_tax("itc", TS.ITC_MATCHED, books_paise=100, filed_paise=100),
          _tax("itc", TS.BLOCKED_CREDIT_NO_ITC, books_paise=100),
          _tax("itc", TS.NO_ITC_APPLICABLE),
          _tax("itc", TS.ITC_DEFERRED_NEXT_PERIOD, books_paise=100, filed_paise=100),
          _tax("obligation", TS.PAID_ON_TIME, books_paise=100, filed_paise=100)]
    assert T.items_from_tax(ds) == []


def test_candidate_normalisation_both_shapes():
    recon = _leg_a(RS.AMBIGUOUS_ABSTAIN, settlement_ids=["s1"], expected_paise=10,
                   candidates=[{"record_id": "b7", "amount_paise": 10,
                                "reason": "two equal credits"}])
    tax = _tax("itc", TS.ITC_MISSING_IN_2B, discrepancy_paise=-5,
               candidates=[{"id": "2B000001", "gst_paise": 5,
                            "rejected_because": "different vendor"}])
    [ri] = T.items_from_leg_a([recon], attention=[])
    [ti] = T.items_from_tax([tax])
    assert ri.candidates == [{"id": "b7", "amount_paise": 10,
                              "note": "two equal credits"}]
    assert ti.candidates == [{"id": "2B000001", "amount_paise": 5,
                              "note": "different vendor"}]


# --- Threshold synthetic and ordering ----------------------------------------


def test_threshold_item_only_on_breach():
    base = {"threshold_paise": 30_000_000,
            "min_balance": {"date": "2025-07-04", "paise": 28_000_000}}
    assert T.threshold_item({**base, "first_below_threshold": None}) is None
    assert T.threshold_item({**base, "threshold_paise": None,
                             "first_below_threshold": "2025-07-01"}) is None
    item = T.threshold_item({**base, "first_below_threshold": "2025-07-01"})
    assert item is not None
    assert item.severity == 1
    assert item.money_at_risk_paise == 2_000_000
    assert item.due_date == "2025-07-01"


def test_sort_queue_published_order():
    def mk(sev, money, source, status, rid):
        return ExceptionItem(source=source, status=status, severity=sev,
                             money_at_risk_paise=money, record_ids=[rid],
                             title="t")
    items = [
        mk(2, 500, "recon_a", "ambiguous_abstain", "s2"),
        mk(1, 100, "tax_obligation", "not_paid", "gst:2025-05"),
        mk(2, 500, "recon_a", "ambiguous_abstain", "s1"),
        mk(1, 900, "recon_a", "exception_missing_bank", "s3"),
        mk(3, 999, "recon_b", "exception_unpaid_order", "o1"),
        mk(2, 700, "tax_itc", "itc_missing_in_2b", "tx1"),
    ]
    out = T.sort_queue(items)
    key = [(i.severity, i.money_at_risk_paise, i.record_ids[0]) for i in out]
    assert key == [(1, 900, "s3"), (1, 100, "gst:2025-05"), (2, 700, "tx1"),
                   (2, 500, "s1"), (2, 500, "s2"), (3, 999, "o1")]
    # deterministic under shuffling
    assert [i.record_ids for i in T.sort_queue(list(reversed(items)))] == \
           [i.record_ids for i in out]


# --- Injection seams: identical results with and without ----------------------


def test_tax_injection_equivalence():
    from recon import io_load
    from recon.engine import reconcile_leg_a
    from tax.io_tax import build_tax_input

    bank_rows = io_load.load_bank_rows(SEED_42)
    settlements = io_load.load_settlements(SEED_42)
    payments = io_load.load_payments(SEED_42)
    decisions = reconcile_leg_a(settlements, bank_rows)

    plain = build_tax_input(SEED_42)
    seamed = build_tax_input(SEED_42, decisions_a=decisions, bank_rows=bank_rows,
                             settlements=settlements, payments=payments)
    assert seamed.purchases == plain.purchases
    assert seamed.gstr2b == plain.gstr2b
    assert seamed.tds_events == plain.tds_events
    assert seamed.form26as == plain.form26as
    assert seamed.periods == plain.periods


def test_forecast_injection_equivalence():
    from forecast import slicing
    from forecast.forecaster import forecast
    from forecast.pipeline import run_recon
    from recon.normalize import parse_bank_date

    world = slicing.load_world(SEED_42)
    cutoff = max(parse_bank_date(r.value_date) for r in world["bank_rows"])
    inp = slicing.build_input(world, cutoff)
    plain = forecast(inp, 14, threshold_paise=30_000_000)
    seamed = forecast(inp, 14, threshold_paise=30_000_000,
                      decisions=run_recon(inp))
    assert seamed.to_dict() == plain.to_dict()


# --- The one-pass daily close -------------------------------------------------


def test_daily_close_byte_identical_determinism():
    import json
    from controller.close import daily_close
    a = json.dumps(daily_close(SEED_42, threshold_paise=30_000_000).to_dict(),
                   sort_keys=True)
    b = json.dumps(daily_close(SEED_42, threshold_paise=30_000_000).to_dict(),
                   sort_keys=True)
    assert a == b


def test_full_world_leg_a_runs_exactly_once(monkeypatch):
    from collections import Counter
    import controller.close as CC
    import tax.io_tax as TIO
    import forecast.pipeline as FP
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
    monkeypatch.setattr(TIO, "reconcile_leg_a", counting("tax"))
    monkeypatch.setattr(FP, "reconcile_leg_a", counting("forecast"))
    CC.daily_close(SEED_42)
    # exactly one full-world run, in the close itself; the tax and forecast
    # surfaces consume the injection. (Band calibration may re-run SUB-sliced
    # history through FP — those calls have fewer rows and are by design.)
    assert calls[("close", True)] == 1
    assert calls[("tax", True)] == 0
    assert calls[("forecast", True)] == 0


def test_queue_covers_every_minted_defect():
    """Completeness against the answer keys on a freshly generated world:
    every record the generator marked with a queue-worthy disposition must
    appear in the close's queue under that exact status."""
    import tempfile

    from recon import io_load
    from recon.generate import generate
    from tax.benchmark import load_golden_tax
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
        for row in load_golden_tax(out):
            if row["expected_disposition"] in T.SEVERITY:
                assert surfaced(row["expected_disposition"], row["record_id"]), row
                checked += 1
        assert checked > 10  # the world actually minted defects


def test_world_without_tax_files_still_closes():
    import shutil
    import tempfile

    from controller.close import daily_close, render_markdown

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "old")
        shutil.copytree(SEED_42, out)
        for name in ("gstr2b.csv", "form26as.csv", "golden_tax.csv"):
            os.remove(os.path.join(out, name))
        close = daily_close(out)
        assert close.tax_summary is None
        assert close.tax_decisions is None
        assert close.verify_panel["tax"] is None
        assert any("predates the tax stage" in w for w in close.warnings)
        assert all(not i.source.startswith("tax_") for i in close.queue)
        report = render_markdown(close)
        assert "n/a — world predates the tax stage" in report


def test_close_cli_smoke(tmp_path):
    import subprocess
    import sys

    repo = os.path.join(os.path.dirname(__file__), "..")
    report = tmp_path / "close.md"
    env = {**os.environ, "PYTHONPATH": "src"}
    r = subprocess.run(
        [sys.executable, "-m", "controller.close", os.path.abspath(SEED_42),
         "--threshold-lakh", "3", "--report", str(report)],
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
        {"expected_disposition": "itc_missing_in_2b", "record_id": "tx1",
         "expected_discrepancy_paise": -1800},
        {"expected_disposition": "exception_missing_bank", "record_id": "s1",
         "expected_discrepancy_paise": -50_000},
        {"expected_disposition": "itc_matched", "record_id": "tx2",
         "expected_discrepancy_paise": 0},  # not queue-worthy
    ]
    item = {"status": "itc_missing_in_2b", "record_ids": ["tx1"],
            "money_at_risk_paise": 1800}
    synth = {"status": "needs_review", "record_ids": ["s9"],
             "money_at_risk_paise": 5}
    r = grade_queue([item, synth], golden)
    assert r["golden_queue_worthy"] == 2 and r["surfaced"] == 1
    assert r["queue_recall"] == 0.5
    assert r["money_recall"] == round(1800 / 51_800, 4)
    # the synthetic is excluded from the precision denominator
    assert r["gradable_items"] == 1 and r["queue_precision"] == 1.0

    crossed = {"status": "duplicate_2b_line", "record_ids": ["tx1"],
               "money_at_risk_paise": 1800}
    r2 = grade_queue([crossed], golden)
    assert r2["surfaced"] == 0 and r2["queue_precision"] == 0.0


def test_audit_engine_row_is_perfect_on_committed_world():
    from controller.audit import audit_close

    r = audit_close(SEED_42)
    assert r["queue_recall"] == 1.0
    assert r["queue_precision"] == 1.0
    assert r["money_recall"] == 1.0
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
    assert naive["verify_violation_count"] > 0


def test_controller_is_blind_to_answer_keys():
    """Canary #3: no controller module except the audit may even mention the
    answer-key files (mirrors the tax package's canary)."""
    src = os.path.join(os.path.dirname(__file__), "..", "src", "controller")
    scanned = 0
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py") or name == "audit.py":
            continue
        with open(os.path.join(src, name), encoding="utf-8") as f:
            assert "golden" not in f.read().lower(), name
        scanned += 1
    assert scanned >= 5
