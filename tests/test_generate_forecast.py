"""Stage 2 generator invariants: forecastable structure and its golden schedule."""

from collections import Counter
from datetime import timedelta

import pytest

from recon import generate as G
from recon.normalize import paise_from_optional_rupee_str, parse_iso_date, roll_off_sunday

SEED = 7
DAYS = 180


@pytest.fixture(scope="module")
def world():
    return G.build_world(SEED, days=DAYS)


def test_default_days_backcompat():
    w = G.build_world(SEED)
    assert w["manifest"]["period"]["days"] == 90


def test_180d_determinism(world):
    again = G.build_world(SEED, days=DAYS)
    assert world["statement"] == again["statement"]
    assert world["golden_obligations"] == again["golden_obligations"]


def test_every_obligation_is_a_real_statement_row(world):
    stmt_by_id = {r["txn_id"]: r for r in world["statement"]}
    assert world["golden_obligations"], "no obligations minted"
    for o in world["golden_obligations"]:
        row = stmt_by_id[o["txn_id"]]
        assert paise_from_optional_rupee_str(row["debit_amount"]) == o["amount_paise"]
        assert o["narration"] == row["narration"]
        # posted date respects the Sunday roll of the due date (+ up to 2 days
        # of business-day jitter for payroll/aws; +2 days and a Sunday roll
        # for the one late-paid GST month Stage 3 injects deliberately)
        due = parse_iso_date(o["due_date"])
        posted = parse_iso_date(o["posted_date"])
        assert roll_off_sunday(due) <= posted <= roll_off_sunday(due) + timedelta(days=3)
        assert posted.weekday() != 6


def test_scheduled_keys_never_appear_as_random_noise(world):
    """The count of statement rows matching a scheduled template must equal the
    golden count exactly — no extra random draws from the same template."""
    golden_narrations = {o["narration"] for o in world["golden_obligations"]}
    markers = ["STAFF PAYROLL", "URBAN LADDER RENT", "GST PAYMENT-CBIC",
               "TDS PAYMENT-CBDT", "AMAZON WEB SERVICES", "LIC PREMIUM",
               "BHARTI AIRTEL"]
    for marker in markers:
        stmt_rows = [r for r in world["statement"] if marker in r["narration"]]
        golden_rows = [n for n in golden_narrations if marker in n]
        assert len(stmt_rows) == len(golden_rows), marker


def test_schedule_shape(world):
    by_key = Counter(o["obligation_key"] for o in world["golden_obligations"])
    months = 6  # 180 days from Apr 1 -> Apr..Sep
    assert by_key["payroll"] == months
    assert by_key["rent"] == months
    assert by_key["telecom"] == months
    assert by_key["gst"] == months
    assert by_key["tds"] == months
    assert by_key["insurance"] == 2  # Apr + Jul

    amounts = {}
    for o in world["golden_obligations"]:
        amounts.setdefault(o["obligation_key"], []).append(o["amount_paise"])
    assert len(set(amounts["rent"])) == 1
    assert len(set(amounts["telecom"])) == 1
    assert len(set(amounts["insurance"])) == 1
    # payroll grows month over month (jitter is +-0.8%, growth ~1.2%)
    payroll = [o["amount_paise"] for o in sorted(
        world["golden_obligations"], key=lambda x: x["due_date"])
        if o["obligation_key"] == "payroll"]
    assert payroll[-1] > payroll[0]


def test_weekday_and_trend_structure(world):
    by_wd = Counter()
    by_half = Counter()
    for p in world["payments"]:
        if p.status != "captured":
            continue
        d = p.created_at.date()
        by_wd[d.weekday()] += 1
        by_half[(d - G.START_DATE).days // 90] += 1
    assert by_wd[5] > by_wd[6] * 1.5      # Saturday well above Sunday
    assert by_half[1] > by_half[0] * 1.10  # second half >10% above first


def test_balance_continuity_at_180(world):
    balance = G.OPENING_BALANCE_PAISE
    for row in world["statement"]:
        balance += (paise_from_optional_rupee_str(row["credit_amount"])
                    - paise_from_optional_rupee_str(row["debit_amount"]))
        assert paise_from_optional_rupee_str(row["balance"]) == balance


def test_golden_leg_a_still_complete_at_180(world):
    golden_ids = {(r["record_type"], r["record_id"]) for r in world["golden_a"]}
    credit_rows = [r for r in world["statement"] if r["credit_amount"]]
    assert len(world["golden_a"]) == len(world["settlements"]) + len(credit_rows)
    for s in world["settlements"]:
        assert ("settlement", s.settlement_id) in golden_ids
