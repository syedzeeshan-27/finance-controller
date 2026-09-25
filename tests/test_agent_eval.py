"""The evaluation harness: golden groups, the four categories, proposal
metrics, and the brief's table, on a tiny world with a scripted agent."""

import csv
import json

import pytest

from agent import eval_resolve as E
from agent.gemini import GeminiLiveTransport
from recon import schemas as RS

S1, S2 = "setl_R1aaaaaaaaaaaa", "setl_R2bbbbbbbbbbbb"


def _golden(rows):
    return [{"record_type": t, "record_id": r, "expected_disposition": d,
             "counterparty_set": frozenset(c), "scenario_tag": tag,
             "expected_discrepancy_paise": disc} for t, r, d, c, tag, disc in rows]


def test_golden_groups_ignore_duplicate_links():
    rows = _golden([
        ("settlement", "s1", RS.MATCHED, ["b1"], "dup", 0),
        ("bank_credit", "b1", RS.MATCHED, ["s1"], "dup", 0),
        ("bank_credit", "b2", RS.DUPLICATE_CREDIT, ["s1"], "dup", 0),
        ("settlement", "s2", RS.MATCHED_MERGED, ["b3"], "m", 0),
        ("settlement", "s3", RS.MATCHED_MERGED, ["b3"], "m", 0),
        ("bank_credit", "b3", RS.MATCHED_MERGED, ["s2", "s3"], "m", 0),
    ])
    g = E.golden_groups(rows)
    assert g["s1"] == frozenset({"s1", "b1"}) and "b2" not in g
    assert g["b3"] == frozenset({"s2", "s3", "b3"})


@pytest.mark.parametrize("state,expected,category", [
    ({"kind": "match", "group": frozenset({"s1", "b1"}), "explained": True},
     RS.MATCHED, "auto_correct"),
    ({"kind": "match", "group": frozenset({"s1", "b9"}), "explained": True},
     RS.MATCHED, "auto_wrong"),
    ({"kind": "match", "group": frozenset({"s1", "b1"}), "explained": False},
     RS.MATCHED, "auto_wrong"),
    ({"kind": "match", "group": frozenset({"s1", "b1"}), "explained": True},
     RS.AMBIGUOUS_ABSTAIN, "auto_wrong"),
    ({"kind": "human"}, RS.MATCHED, "abstain_wrong"),
    ({"kind": "human"}, RS.EXCEPTION_MISSING_BANK, "abstain_correct"),
    ({"kind": "nsc"}, RS.NON_SETTLEMENT_CREDIT, "auto_correct"),
    ({"kind": "nsc"}, RS.EXCEPTION_MISSING_SETTLEMENT, "auto_wrong"),
    ({"kind": "human"}, RS.NON_SETTLEMENT_CREDIT, "abstain_wrong"),
])
def test_categories(state, expected, category):
    row = {"record_id": "s1", "expected_disposition": expected}
    groups = {"s1": frozenset({"s1", "b1"})}
    assert E.categorize(row, {"s1": state}, groups) == category


def test_wilson_interval():
    lo, hi = E.wilson(0, 150)
    assert lo == 0.0 and 0.02 < hi < 0.03


@pytest.fixture
def world(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    with open(d / "settlements.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["settlement_id", "amount_paise", "fees_paise", "tax_paise", "utr",
                    "payment_count", "status", "created_at", "settled_at"])
        w.writerow([S1, 1_234_567, 0, 0, "HDFC101010101010", 3, "processed",
                    "2025-05-01T01:15:00", "2025-05-02"])
        w.writerow([S2, 765_432, 0, 0, "ICIC202020202020", 2, "processed",
                    "2025-05-01T01:15:00", "2025-05-02"])
    with open(d / "bank_statement.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "txn_date", "value_date", "narration", "ref_no",
                    "debit_amount", "credit_amount", "balance"])
        w.writerow(["BANK000001", "03/05/2025", "03/05/2025",
                    "NEFT CR-HDFC101010101010-RAZORPAY less chgs 59.00", "",
                    "", "12,286.67", "1,12,286.67"])
    (d / "settlement_merchants.csv").write_text(
        f"settlement_id,merchant_name\n{S1},ACME\n{S2},ACME\n", "utf-8")
    with open(d / "golden_leg_a.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RS.GOLDEN_COLUMNS)
        w.writerow(["settlement", S1, RS.MATCHED_WITH_DISCREPANCY, "BANK000001",
                    "narrated_deduction", -5900, ""])
        w.writerow(["bank_credit", "BANK000001", RS.MATCHED_WITH_DISCREPANCY, S1,
                    "narrated_deduction", -5900, ""])
        w.writerow(["settlement", S2, RS.EXCEPTION_MISSING_BANK, "",
                    "never_paid", 0, ""])
    return str(d)


def _factory(reply):
    def make(path, task=""):
        return GeminiLiveTransport(path, task=task, model_name="gemini-3.5-flash-lite",
                                   api_key="k", rpm=0, post=lambda *a: reply,
                                   sleep=lambda s: None)
    return make


def _reply(args):
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "submit_resolution", "args": args}}]},
        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50}}


def test_world_evaluation_and_report(world, tmp_path):
    good = dict(verdict="match", settlement_ids=[S1], bank_row_ids=["BANK000001"],
                deduction_paise=5_900, deduction_evidence="less chgs 59.00", reason="r")
    tags = {"narrated_deduction", "never_paid"}
    w = E.evaluate_world(world, "record", tags, transcripts_root=str(tmp_path / "t"),
                         transport_factory=_factory(_reply(good)))
    assert w["engine"]["narrated_deduction"] == {"abstain_wrong": 2}
    assert w["engine_agent"]["narrated_deduction"] == {"auto_correct": 2}
    assert w["engine_agent"]["never_paid"] == {"abstain_correct": 1}
    assert w["engine_solver"]["narrated_deduction"] == {"auto_correct": 2}

    agg = E.aggregate([w], tags)
    assert agg["total"]["proposals"]["wrong_accepted"] == 0
    assert agg["total"]["proposals"]["rejected_by_verifier"] == 0
    results = {"meta": {"worlds": [w["world"]], "role": "dev",
                        "model": "gemini-3.5-flash-lite", "prompt_sha256": "0" * 64,
                        "hashes": {"resolve_verifier.py": "1" * 64},
                        "matches_preregistration": "yes", "notes": ["n"]},
               "aggregate": agg, "worlds": [w],
               "worst_failures": E.worst_failures(w["items"], {})}
    md = E.render_report(results, "t")
    assert "| Auto-resolved correctly | 0 | 2 |" in md
    assert "| **Wrong proposals the verifier accepted** | n/a | **0** |" in md
    assert "free tier" in md and "all dev case rows" in md
    json.dumps(results, default=list)
    # an accepted, correct proposal is not a failure, and nothing is left over
    assert w["left_for_human"] == []
    assert E.worst_failures(w["items"], {}, left=w["left_for_human"]) == []
    billed = E.render_report(dict(results, meta=dict(results["meta"], model="claude-sonnet-5")), "t")
    assert "free tier" not in billed and " billed · " in billed


def test_preregistration_check_covers_every_pinned_hash_and_setting(tmp_path):
    path = str(tmp_path / "prereg.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"hashes": {"resolve_verifier.py": "a", "resolve.py": "b"},
                   "settings": {"model": "gemini-3.5-flash-lite", "thinking_level": None}}, f)
    used = {"model": ["gemini-3.5-flash-lite"], "thinking_level": ["None"],
            "fc_mode": ["VALIDATED"]}
    hashes = {"resolve_verifier.py": "a", "resolve.py": "b", "solver.py": "x"}
    assert E._matches_prereg(hashes, used, path) == "yes"
    assert E._matches_prereg(dict(hashes, **{"resolve.py": "c"}), used, path) == \
        "NO (changed: resolve.py)"
    two_models = dict(used, model=["gemini-3.5-flash", "gemini-3.5-flash-lite"])
    assert E._matches_prereg(hashes, two_models, path) == "NO (changed: model)"
    assert E._matches_prereg(hashes, dict(used, thinking_level=["low"]), path) == \
        "NO (changed: thinking_level)"
    assert E._matches_prereg(hashes, used, str(tmp_path / "none.json")) == \
        "no pre-registration file"


def _dev_transcripts(root, metas):
    for seed, meta in zip((1000, 1006), metas):
        d = root / str(seed)
        d.mkdir(parents=True)
        (d / "credit__BANK000001.jsonl").write_text(
            json.dumps(dict({"kind": "meta"}, **meta)) + "\n", encoding="utf-8")
        (d / "manifest.json").write_text("{}", encoding="utf-8")
    return str(root)


def test_pinning_happens_in_two_parts_and_only_once(tmp_path):
    path = str(tmp_path / "prereg.json")
    sonnet = {"model": "claude-sonnet-5", "provider": "anthropic"}
    root = _dev_transcripts(tmp_path / "resolve", [sonnet, sonnet])
    with pytest.raises(SystemExit, match="part 1 first"):
        E.pin("2", path, transcripts_root=root)
    doc = E.pin("1", path, now="t1")
    assert sorted(doc["hashes"]) == sorted(E.PART1_KEYS)
    assert doc["part1"]["heldout_seeds"] == [1001, 1002, 1003, 1004, 1005]
    with pytest.raises(SystemExit, match="already pinned"):
        E.pin("1", path)
    doc = E.pin("2", path, now="t2", transcripts_root=root)
    assert set(E.PART2_KEYS) <= set(doc["hashes"])
    # the settings the dev run actually used, read from its transcripts
    assert doc["settings"] == {"model": "claude-sonnet-5", "thinking_level": "None",
                               "fc_mode": "None"}
    used = {"model": ["claude-sonnet-5"], "thinking_level": ["None"], "fc_mode": ["None"]}
    assert E._matches_prereg(E.fingerprints(), used, path) == "yes"
    with pytest.raises(SystemExit, match="nothing changed"):
        E.pin("amend", path, reason="late change")


def test_after_part_two_only_the_report_code_can_be_amended(tmp_path):
    path = str(tmp_path / "prereg.json")
    root = _dev_transcripts(tmp_path / "resolve", [{"model": "claude-sonnet-5"}] * 2)
    E.pin("1", path, now="t1")
    E.pin("2", path, now="t2", transcripts_root=root)
    report = tmp_path / "agent_eval.json"
    report.write_text(json.dumps({"aggregate": {"total": {"x": (1, 2)}}}), encoding="utf-8")
    doc = json.load(open(path, encoding="utf-8"))
    doc["hashes"]["solver.py"] = "a" * 64                 # the prefilter: would change results
    json.dump(doc, open(path, "w", encoding="utf-8"))
    with pytest.raises(SystemExit, match="can never be amended after part 2"):
        E.pin("amend", path, reason="r", heldout_report_json=str(report))
    doc["hashes"]["solver.py"] = E.fingerprints()["solver.py"]
    doc["hashes"]["eval_resolve.py"] = "b" * 64           # a report-wording fix
    json.dump(doc, open(path, "w", encoding="utf-8"))
    doc = E.pin("amend", path, now="t3", reason="wording", heldout_report_json=str(report))
    late = doc["amendments"][-1]
    assert late["after_part2"] and late["files"] == ["eval_resolve.py"]
    # the fingerprint survives a JSON round trip, so the next report can compare it
    assert late["heldout_aggregate_sha256"] == E.aggregate_sha({"aggregate": {"total": {"x": [1, 2]}}})


def test_part_two_refuses_a_dev_run_that_mixed_settings(tmp_path):
    path = str(tmp_path / "prereg.json")
    root = _dev_transcripts(tmp_path / "resolve", [
        {"model": "claude-sonnet-5"}, {"model": "gemini-3.5-flash-lite"}])
    E.pin("1", path, now="t1")
    with pytest.raises(SystemExit, match="mix settings"):
        E.pin("2", path, transcripts_root=root)


def test_amendments_are_recorded_and_limited_to_grading_and_solver(tmp_path):
    path = str(tmp_path / "prereg.json")
    E.pin("1", path, now="t1")
    with pytest.raises(SystemExit, match="nothing changed"):
        E.pin("amend", path, reason="r")
    doc = json.load(open(path, encoding="utf-8"))
    doc["hashes"]["eval_resolve.py"] = "a" * 64          # grading code edited after part 1
    json.dump(doc, open(path, "w", encoding="utf-8"))
    with pytest.raises(SystemExit, match="needs a reason"):
        E.pin("amend", path)
    doc = E.pin("amend", path, now="t2", reason="provider switch")
    assert doc["amendments"] == [{"at": "t2", "files": ["eval_resolve.py"],
                                  "reason": "provider switch",
                                  "previous": {"eval_resolve.py": "a" * 64}}]
    assert E._matches_prereg(E.fingerprints(), None, path) == "yes"
    doc["hashes"]["resolve_verifier.py"] = "b" * 64       # the verifier: never amendable
    json.dump(doc, open(path, "w", encoding="utf-8"))
    with pytest.raises(SystemExit, match="can never be amended"):
        E.pin("amend", path, reason="looser rules")


def test_part_two_refuses_when_part_one_changed(tmp_path):
    path = str(tmp_path / "prereg.json")
    doc = E.pin("1", path, now="t1")
    doc["hashes"]["resolve_verifier.py"] = "0" * 64       # the verifier was edited
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    with pytest.raises(SystemExit, match="resolve_verifier.py"):
        E.pin("2", path)


def test_rejected_proposal_is_counted(world, tmp_path):
    # the right pair with the deduction left out: the verifier rejects it
    bad = dict(verdict="match", settlement_ids=[S1], bank_row_ids=["BANK000001"],
               deduction_paise=0, deduction_evidence="", reason="r")
    tags = {"narrated_deduction", "never_paid"}
    w = E.evaluate_world(world, "record", tags, transcripts_root=str(tmp_path / "t"),
                         transport_factory=_factory(_reply(bad)))
    agg = E.aggregate([w], tags)
    assert agg["total"]["proposals"]["rejected_by_verifier"] == 1
    assert w["engine_agent"]["narrated_deduction"] == {"abstain_wrong": 2}
    kinds = [f["kind"] for f in E.worst_failures(w["items"], {})]
    assert kinds == ["right_proposal_rejected"]
    # both rows are still left for a human; they belong to the item already listed
    assert len(w["left_for_human"]) == 2
    kinds = [f["kind"] for f in E.worst_failures(w["items"], {}, left=w["left_for_human"])]
    assert kinds == ["right_proposal_rejected"]


def test_rows_never_sent_to_the_agent_are_listed_once_per_item():
    left = [{"world": "9", "record_id": r, "scenario": "merged_3plus",
             "expected": "matched_merged", "amount_paise": amt, "item_id": "credit:B1",
             "item_outcome": "prefilter_skip", "transcript": ""}
            for r, amt in (("B1", 900), ("S1", 400), ("S2", 500))]
    left.append({"world": "9", "record_id": "S9", "scenario": "x", "expected": "matched",
                 "amount_paise": 50, "item_id": "", "item_outcome": "not_queued",
                 "transcript": ""})
    out = E.worst_failures([], {"9/credit:B1": "note"}, left=left)
    assert [(f["item_id"], f["amount_paise"]) for f in out] == [("credit:B1", 900), ("S9", 50)]
    assert out[0]["why"].endswith("(3 golden rows)") and out[0]["analyst_note"] == "note"
    assert "did not put it in the queue" in out[1]["why"]
