"""verify_close catches a clean close as clean and every class of tampering."""

from __future__ import annotations

import copy
import json
import os

import pytest

from controller.close import daily_close
from controller.verify_close import verify_close

SEED_42 = os.path.join(os.path.dirname(__file__), "..", "data", "seeds", "42")
# far above any plausible balance, so the threshold is always breached
HUGE_THRESHOLD = 10_000_000_000


@pytest.fixture(scope="module")
def close42() -> dict:
    return json.loads(json.dumps(daily_close(SEED_42).to_dict()))


@pytest.fixture(scope="module")
def close42_breach() -> dict:
    return json.loads(json.dumps(
        daily_close(SEED_42, threshold_paise=HUGE_THRESHOLD).to_dict()))


def test_clean_close_verifies_clean(close42):
    assert verify_close(SEED_42, close42) == []


def test_clean_close_with_breach_verifies_clean(close42_breach):
    assert verify_close(SEED_42, close42_breach) == []
    synth = [i for i in close42_breach["queue"]
             if i["status"] == "forecast_below_threshold"]
    assert len(synth) == 1 and synth[0]["severity"] == 1


def test_dropped_queue_item_caught(close42):
    t = copy.deepcopy(close42)
    dropped = t["queue"].pop(0)
    t["counts"]["queue_total"] -= 1
    t["counts"]["by_severity"][f"S{dropped['severity']}"] -= 1
    t["counts"]["by_source"][dropped["source"]] -= 1
    v = verify_close(SEED_42, t)
    assert any("queue is missing a decision" in s for s in v)


def test_duplicated_queue_item_caught(close42):
    t = copy.deepcopy(close42)
    t["queue"].insert(0, copy.deepcopy(t["queue"][0]))
    v = verify_close(SEED_42, t)
    assert any("surfaced 2 times" in s for s in v)


def test_retiered_severity_caught(close42):
    t = copy.deepcopy(close42)
    victim = next(i for i in t["queue"] if i["severity"] == 1)
    victim["severity"] = 3
    v = verify_close(SEED_42, t)
    assert any("severity wrong" in s for s in v)


def test_reordered_queue_caught(close42):
    t = copy.deepcopy(close42)
    assert len(t["queue"]) >= 2
    t["queue"][0], t["queue"][-1] = t["queue"][-1], t["queue"][0]
    v = verify_close(SEED_42, t)
    assert any("not in the published order" in s for s in v)


def test_inflated_money_caught(close42):
    t = copy.deepcopy(close42)
    t["queue"][0]["money_at_risk_paise"] += 1
    v = verify_close(SEED_42, t)
    assert any("money at risk wrong" in s for s in v)


def test_cash_off_by_one_paisa_caught(close42):
    t = copy.deepcopy(close42)
    t["cash"]["balance_paise"] += 1
    v = verify_close(SEED_42, t)
    assert any("last statement row" in s for s in v)


def test_doctored_embedded_decision_caught(close42):
    t = copy.deepcopy(close42)
    victim = next(d for d in t["decisions_a"] if d["status"] == "matched")
    victim["status"] = "exception_missing_bank"
    v = verify_close(SEED_42, t)
    assert any("stage verifier (leg A)" in s for s in v)
    assert any("recon summary status counts differ" in s for s in v)
    assert any("misreports the leg A verifier" in s for s in v)


def test_stripped_attention_enrichment_caught(close42):
    t = copy.deepcopy(close42)
    assert t["forecast"]["attention"], "seed 42 should have overdue settlements"
    for i in t["queue"]:
        i["days_overdue"] = None
    v = verify_close(SEED_42, t)
    assert any("fused without days_overdue" in s for s in v)


def test_injected_forecast_item_caught(close42):
    t = copy.deepcopy(close42)
    t["queue"].append({
        "source": "forecast", "status": "overdue_settlement", "severity": 3,
        "money_at_risk_paise": 0, "record_ids": ["setl_fake"], "title": "x",
        "detail": "", "suggested_action": "y", "evidence": [],
        "candidates": [], "due_date": None, "days_overdue": None,
    })
    t["counts"]["queue_total"] += 1
    t["counts"]["by_severity"]["S3"] += 1
    t["counts"]["by_source"]["forecast"] = \
        t["counts"]["by_source"].get("forecast", 0) + 1
    t["counts"]["by_source"] = dict(sorted(t["counts"]["by_source"].items()))
    v = verify_close(SEED_42, t)
    assert any("attention must fuse" in s for s in v)
    assert any("no decision behind it" in s for s in v)


def test_phantom_threshold_synthetic_caught(close42):
    t = copy.deepcopy(close42)
    assert t["forecast"]["first_below_threshold"] is None
    t["queue"].append({
        "source": "forecast", "status": "forecast_below_threshold",
        "severity": 1, "money_at_risk_paise": 5, "record_ids": ["x"],
        "title": "x", "detail": "", "suggested_action": "y", "evidence": [],
        "candidates": [], "due_date": None, "days_overdue": None,
    })
    v = verify_close(SEED_42, t)
    assert any("synthetic present without a breach" in s for s in v)


def test_tampered_tax_summary_caught(close42):
    t = copy.deepcopy(close42)
    t["tax_summary"]["itc_claimable_now_paise"] += 100
    v = verify_close(SEED_42, t)
    assert any("ITC claimable-now differs" in s for s in v)
