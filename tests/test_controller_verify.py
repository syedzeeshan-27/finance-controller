"""verify_close catches a clean close as clean and every class of tampering."""

from __future__ import annotations

import copy
import json
import os

import pytest

from controller.close import daily_close
from controller.verify_close import verify_close

SEED_42 = os.path.join(os.path.dirname(__file__), "..", "data", "seeds", "42")


@pytest.fixture(scope="module")
def close42() -> dict:
    return json.loads(json.dumps(daily_close(SEED_42).to_dict()))


def test_clean_close_verifies_clean(close42):
    assert verify_close(SEED_42, close42) == []


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


def test_stripped_overdue_enrichment_caught(close42):
    t = copy.deepcopy(close42)
    assert any(i["days_overdue"] is not None for i in t["queue"]), \
        "seed 42 should have overdue settlements"
    for i in t["queue"]:
        i["days_overdue"] = None
    v = verify_close(SEED_42, t)
    assert any("fused without days_overdue" in s for s in v)


def test_fabricated_days_overdue_caught(close42):
    t = copy.deepcopy(close42)
    victim = next(i for i in t["queue"] if i["days_overdue"] is not None)
    victim["days_overdue"] += 3
    v = verify_close(SEED_42, t)
    assert any("but the files imply" in s for s in v)


def test_injected_item_without_decision_caught(close42):
    t = copy.deepcopy(close42)
    t["queue"].append({
        "source": "recon_a", "status": "exception_missing_bank", "severity": 1,
        "money_at_risk_paise": 0, "record_ids": ["setl_fake"], "title": "x",
        "detail": "", "suggested_action": "y", "evidence": [],
        "candidates": [], "due_date": None, "days_overdue": None,
    })
    t["counts"]["queue_total"] += 1
    t["counts"]["by_severity"]["S1"] += 1
    t["counts"]["by_source"]["recon_a"] += 1
    v = verify_close(SEED_42, t)
    assert any("no decision behind it" in s for s in v)
