"""verify.py must pass clean engine output and catch every injected corruption."""

import copy

import pytest

from recon import io_load
from recon.engine import reconcile_leg_a
from recon.generate import generate
from recon.verify import verify_leg_a


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    data_dir = str(tmp_path_factory.mktemp("world"))
    generate(11, data_dir)
    settlements = io_load.load_settlements(data_dir)
    bank_rows = io_load.load_bank_rows(data_dir)
    decisions = [d.to_dict() for d in reconcile_leg_a(settlements, bank_rows)]
    return data_dir, decisions


def test_clean_output_passes(world):
    data_dir, decisions = world
    assert verify_leg_a(data_dir, decisions) == []


def _first_match(decisions):
    return next(d for d in decisions if d["status"] == "matched")


def test_double_claim_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    m = _first_match(mutated)
    extra = copy.deepcopy(m)
    mutated.append(extra)
    violations = verify_leg_a(data_dir, mutated)
    assert any("claimed by 2 decisions" in v for v in violations)


def test_dropped_record_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    m = _first_match(mutated)
    mutated.remove(m)
    violations = verify_leg_a(data_dir, mutated)
    assert any("not covered by any decision" in v for v in violations)


def test_wrong_discrepancy_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    _first_match(mutated)["discrepancy_paise"] = 12345
    violations = verify_leg_a(data_dir, mutated)
    assert any("discrepancy" in v for v in violations)


def test_wrong_expected_amount_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    _first_match(mutated)["expected_paise"] += 1
    violations = verify_leg_a(data_dir, mutated)
    assert any("expected_paise" in v for v in violations)


def test_breakdown_mismatch_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    d = next(x for x in mutated if x["status"] == "matched_with_discrepancy")
    d["discrepancy_breakdown"] = [{"label": "made_up", "amount_paise": 1}]
    violations = verify_leg_a(data_dir, mutated)
    assert any("breakdown" in v for v in violations)


def test_empty_evidence_on_match_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    _first_match(mutated)["evidence"] = []
    violations = verify_leg_a(data_dir, mutated)
    assert any("no evidence" in v for v in violations)


def test_unbacked_confidence_tier_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    m = _first_match(mutated)
    m["evidence"] = [{"rule": "vibes", "detail": "trust me"}]
    violations = verify_leg_a(data_dir, mutated)
    assert any("lacks any of the qualifying evidence" in v for v in violations)


def test_exception_without_candidates_caught(world):
    data_dir, decisions = world
    mutated = copy.deepcopy(decisions)
    d = next(x for x in mutated if x["status"] == "exception_missing_bank")
    d["candidates"] = []
    violations = verify_leg_a(data_dir, mutated)
    assert any("without candidates" in v for v in violations)
