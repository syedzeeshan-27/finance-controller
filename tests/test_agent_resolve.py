"""The resolver end to end on a tiny world, through the real Gemini transport
with a scripted HTTP layer: items, prefilter, golden isolation, record,
offline replay, verification, and resume after a quota stop."""

import csv
import json
import os

import pytest

from agent import resolve as R
from agent.gemini import GeminiLiveTransport, ProviderUnavailable, QuotaExhausted

S1, S2 = "setl_R1aaaaaaaaaaaa", "setl_R2bbbbbbbbbbbb"
MARKER = "SECRET_GOLDEN_MARKER_7731"


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
        w.writerow(["BANK000002", "04/05/2025", "04/05/2025", "UPI-SWIGGY-1@ybl",
                    "", "100.00", "", "1,12,186.67"])
    with open(d / "settlement_merchants.csv", "w", newline="", encoding="utf-8") as f:
        f.write(f"settlement_id,merchant_name\n{S1},ACME\n{S2},ACME\n")
    # answer keys the agent must never see
    with open(d / "golden_leg_a.csv", "w", newline="", encoding="utf-8") as f:
        f.write("record_type,record_id,expected_disposition,counterparty_ids,"
                "scenario_tag,expected_discrepancy_paise,notes\n"
                f"settlement,{S1},matched_with_discrepancy,BANK000001,x,-5900,{MARKER}\n")
    (d / "golden_manifest.json").write_text(json.dumps({"note": MARKER}), "utf-8")
    return str(d)


def _submit(**kw):
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"id": "call_1", "name": "submit_resolution", "args": kw},
         "thoughtSignature": "SIG"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 80,
                          "thoughtsTokenCount": 40},
        "modelVersion": "gemini-3.5-flash-lite", "responseId": "r"}


GOOD = dict(verdict="match", settlement_ids=[S1], bank_row_ids=["BANK000001"],
            deduction_paise=5_900, deduction_evidence="less chgs 59.00",
            reason="UTR matches; the 59.00 charge closes the gap.")


def _factory(replies, calls):
    def make(path, task=""):
        def post(url, body, api_key, timeout):
            calls.append(body)
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return GeminiLiveTransport(path, task=task, model_name="gemini-3.5-flash-lite",
                                   api_key="k", rpm=0, post=post,
                                   sleep=lambda s: None)
    return make


def test_items_and_claims_come_from_the_frozen_engine(world):
    snap = R.engine_snapshot(world)
    assert [(i.item_id, i.status) for i in snap.items] == [
        ("credit:BANK000001", "needs_review"),
        (f"settlement:{S2}", "exception_missing_bank")]
    assert snap.items[0].records == [S1, "BANK000001"]
    assert snap.claimed == frozenset()


def test_first_message_carries_the_item_and_its_window(world):
    snap = R.engine_snapshot(world)
    agent_world = R.AgentWorld(world, snap.claimed)
    msg = json.loads(R.first_message(agent_world, snap.items[0]))
    assert msg["queue_item"]["engine_status"] == "needs_review"
    assert set(msg["queue_item"]["records"]) == {S1, "BANK000001"}
    ids = [s["settlement_id"] for s in msg["context"]["open_settlements"]["rows"]]
    assert ids == [S1, S2]
    assert [c["txn_id"] for c in msg["context"]["open_bank_credits"]["rows"]] == ["BANK000001"]


def test_record_verify_replay(world, tmp_path):
    tdir = str(tmp_path / "t")
    calls = []
    wr = R.run_world(world, "record", tdir, transport_factory=_factory([_submit(**GOOD)], calls))
    outcomes = {r.item_id: r.outcome for r in wr.runs}
    assert outcomes == {"credit:BANK000001": "submitted",
                        f"settlement:{S2}": "prefilter_skip"}
    assert len(calls) == 1                      # the prefiltered item cost nothing
    run = wr.runs[0]
    assert run.api_calls == 1 and run.cost_usd > 0

    verdicts = R.verify_world(world, wr)
    v = verdicts["credit:BANK000001"]
    assert v.accepted, v.details
    assert v.status == "matched_with_discrepancy" and v.deduction_paise == 5_900

    # offline replay: no transport factory, identical proposals
    again = R.run_world(world, "replay", tdir)
    assert [r.proposal for r in again.runs] == [r.proposal for r in wr.runs]
    assert R.verify_world(world, again)["credit:BANK000001"].accepted


def test_agent_never_sees_golden_files(world, tmp_path):
    tdir = tmp_path / "t"
    calls = []
    R.run_world(world, "record", str(tdir), transport_factory=_factory([_submit(**GOOD)], calls))
    assert MARKER not in json.dumps(calls)
    for name in os.listdir(tdir):
        assert MARKER not in (tdir / name).read_text(encoding="utf-8")


def test_quota_stop_then_resume(world, tmp_path):
    tdir = str(tmp_path / "t")
    stopped = R.run_world(world, "record", tdir,
                          transport_factory=_factory([QuotaExhausted("daily")], []))
    assert stopped.stopped == "quota"
    assert stopped.runs[0].outcome == "quota_stop"
    calls = []
    resumed = R.run_world(world, "resume", tdir,
                          transport_factory=_factory([_submit(**GOOD)], calls))
    assert resumed.runs[0].outcome == "submitted" and len(calls) == 1
    # a second resume replays everything and calls nothing
    again = R.run_world(world, "resume", tdir, transport_factory=_factory([], []))
    assert again.runs[0].proposal == resumed.runs[0].proposal


def test_provider_failure_stops_the_run_and_resume_reruns_the_item(world, tmp_path):
    tdir = tmp_path / "t"
    stopped = R.run_world(world, "record", str(tdir), transport_factory=_factory(
        [ProviderUnavailable("Gemini HTTP 503: overloaded")], []))
    assert stopped.stopped == "provider"
    assert stopped.runs[0].outcome == "provider_stop" and "503" in stopped.runs[0].error
    assert not (tdir / "credit__BANK000001.jsonl").exists()   # no half-recorded failure
    calls = []
    resumed = R.run_world(world, "resume", str(tdir),
                          transport_factory=_factory([_submit(**GOOD)], calls))
    assert resumed.runs[0].outcome == "submitted" and len(calls) == 1


def test_no_submission_leaves_the_item_for_a_human(world, tmp_path):
    text_only = {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "I think it is fine."}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}
    wr = R.run_world(world, "record", str(tmp_path / "t"),
                     transport_factory=_factory([text_only], []))
    assert wr.runs[0].outcome == "no_submit" and wr.runs[0].proposal is None
    assert R.verify_world(world, wr) == {}


def test_prompt_fingerprint_is_stable():
    assert R.prompt_fingerprint() == R.prompt_fingerprint()
    for tool in R.TOOLS:
        from agent.tools import assert_strict
        assert_strict(tool)
