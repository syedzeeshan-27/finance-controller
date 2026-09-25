"""Gemini transport: translation both ways, quota handling, budget, and a live
loop recorded through a fake HTTP layer that replays offline byte-for-byte."""

import io
import json
import os
import urllib.error

import pytest

from agent import tools as T
from agent.gemini import (GeminiLiveTransport, ProviderUnavailable, QuotaExhausted,
                          from_gemini_response, to_gemini_request)
from agent.loop import run_loop
from agent.pricing import Budget, BudgetedTransport, BudgetExceeded, call_cost_usd
from agent.provider import AgentError, ReplayTransport, RunStop, make_live_transport

LOOKUP = T.strict_tool("lookup", "look something up", {"key": {"type": "string"}})
SUBMIT = T.strict_tool("submit", "submit the answer", {"answer": {"type": "string"}})


def _raw(parts, finish="STOP", usage=None):
    return {"candidates": [{"content": {"role": "model", "parts": parts},
                            "finishReason": finish}],
            "usageMetadata": usage or {"promptTokenCount": 100,
                                       "candidatesTokenCount": 20,
                                       "thoughtsTokenCount": 5},
            "modelVersion": "gemini-3.5-flash-lite", "responseId": "r1"}


class FakeHTTP:
    """Scripted replies (dicts) or HTTP errors (status, body dict)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies = []

    def __call__(self, url, body, api_key, timeout):
        self.bodies.append(body)
        r = self.replies.pop(0)
        if isinstance(r, tuple):
            status, err = r
            raise urllib.error.HTTPError(url, status, "err", {},
                                         io.BytesIO(json.dumps(err).encode()))
        return r


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _transport(tmp_path, replies, rpm=0, **kw):
    clock = FakeClock()
    http = FakeHTTP(replies)
    t = GeminiLiveTransport(str(tmp_path / "t.jsonl"), task="t",
                            model_name="gemini-3.5-flash-lite", api_key="k",
                            rpm=rpm, post=http, sleep=clock.sleep, clock=clock,
                            **kw)
    return t, http, clock


# --- translation ---------------------------------------------------------------------

def test_request_translation():
    req = {"model": "m", "max_tokens": 500, "system": "be careful",
           "tools": [LOOKUP],
           "messages": [
               {"role": "user", "content": "start"},
               {"role": "assistant", "content": [
                   {"type": "tool_use", "id": "call_7", "name": "lookup",
                    "input": {"key": "a"},
                    "_gemini_part": {"functionCall": {"id": "call_7",
                                                      "name": "lookup",
                                                      "args": {"key": "a"}},
                                     "thoughtSignature": "SIG"}}]},
               {"role": "user", "content": [
                   {"type": "tool_result", "tool_use_id": "call_7",
                    "content": json.dumps({"rows": [1, 2]})}]},
           ]}
    body = to_gemini_request(req, thinking_level="low")
    assert body["systemInstruction"] == {"parts": [{"text": "be careful"}]}
    decl = body["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "lookup"
    assert decl["parametersJsonSchema"] == LOOKUP["input_schema"]
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "VALIDATED"}}
    assert body["generationConfig"] == {"maxOutputTokens": 500,
                                        "thinkingConfig": {"thinkingLevel": "low"}}
    user, model_turn, results = body["contents"]
    assert user == {"role": "user", "parts": [{"text": "start"}]}
    # the model's own part goes back verbatim, thought signature included
    assert model_turn["parts"][0]["thoughtSignature"] == "SIG"
    fr = results["parts"][0]["functionResponse"]
    assert fr == {"name": "lookup", "id": "call_7", "response": {"rows": [1, 2]}}


def test_error_result_and_synthetic_ids():
    req = {"model": "m", "messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "g_0_0", "name": "lookup", "input": {},
             "_gemini_part": {"functionCall": {"name": "lookup", "args": {}}}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "g_0_0",
             "content": json.dumps({"error": "nope"}), "is_error": True}]}]}
    fr = to_gemini_request(req)["contents"][1]["parts"][0]["functionResponse"]
    assert "id" not in fr                    # synthetic ids stay local
    assert fr["response"] == {"error": {"error": "nope"}}


@pytest.mark.parametrize("finish,parts,stop", [
    ("STOP", [{"text": "done"}], "end_turn"),
    ("STOP", [{"functionCall": {"name": "lookup", "args": {"key": "x"}}}], "tool_use"),
    ("MAX_TOKENS", [{"text": "cut"}], "max_tokens"),
    ("SAFETY", [], "refusal"),
    ("MALFORMED_FUNCTION_CALL", [], "malformed_tool_call"),
])
def test_response_translation(finish, parts, stop):
    resp = from_gemini_response(_raw(parts, finish), n=3)
    assert resp["stop_reason"] == stop
    assert resp["usage"] == {"input_tokens": 100, "cache_read_input_tokens": 0,
                             "output_tokens": 25,
                             "output_tokens_details": {"thinking_tokens": 5},
                             "provider": "gemini"}
    if stop == "tool_use":
        block = resp["content"][0]
        assert block["id"] == "g_3_0" and block["input"] == {"key": "x"}
        assert block["_gemini_part"] == parts[0]


def test_thought_parts_kept_and_blocked_prompt_is_refusal():
    resp = from_gemini_response(_raw([{"text": "hmm", "thought": True},
                                      {"text": "answer"}]), n=0)
    assert [b["type"] for b in resp["content"]] == ["thinking", "text"]
    blocked = from_gemini_response({"promptFeedback": {"blockReason": "SAFETY"}}, 0)
    assert blocked["stop_reason"] == "refusal"


# --- a live loop through fake HTTP, then an offline replay ------------------------------

def test_record_then_replay_byte_identical(tmp_path):
    replies = [
        _raw([{"functionCall": {"id": "c1", "name": "lookup", "args": {"key": "a"}},
               "thoughtSignature": "S1"}]),
        _raw([{"functionCall": {"id": "c2", "name": "submit", "args": {"answer": "42"}}}]),
    ]
    live, http, _ = _transport(tmp_path, replies)
    got = {}

    def run(transport):
        got.clear()
        return run_loop(transport, system="sys", tools=[LOOKUP, SUBMIT],
                        impls={"lookup": lambda i: {"value": i["key"].upper()},
                               "submit": lambda i: got.update(i) or {"ok": True}},
                        user_content="go", stop_when=lambda: "answer" in got)

    live_out = run(live)
    assert got == {"answer": "42"} and live_out.api_calls == 2
    # the second request carried the thought signature and the named result
    second = http.bodies[1]["contents"]
    assert second[1]["parts"][0]["thoughtSignature"] == "S1"
    assert second[2]["parts"][0]["functionResponse"]["name"] == "lookup"

    records = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text("utf-8").splitlines()]
    assert records[0]["provider"] == "gemini"
    assert "request" in records[1] and "request_delta" in records[2]
    assert all("latency_s" in r for r in records[1:])

    replay_out = run(ReplayTransport(str(tmp_path / "t.jsonl")))
    assert got == {"answer": "42"}
    assert replay_out.messages == live_out.messages


def test_last_allowed_call_is_forced_to_the_final_tool(tmp_path):
    lookup = _raw([{"functionCall": {"id": "c1", "name": "lookup", "args": {"key": "k"}}}])
    t, http, _ = _transport(tmp_path, [lookup, lookup])
    with pytest.raises(AgentError, match="call budget"):
        run_loop(t, system="s", tools=[LOOKUP, SUBMIT], impls={"lookup": lambda a: "v"},
                 user_content="go", max_calls=2, final_tool="submit")
    first, last = (b["toolConfig"]["functionCallingConfig"] for b in http.bodies)
    assert first == {"mode": "VALIDATED"}
    assert last == {"mode": "ANY", "allowedFunctionNames": ["submit"]}


def test_requests_without_a_final_tool_are_unchanged():
    req = {"model": "m", "max_tokens": 10, "system": "s", "tools": [LOOKUP],
           "messages": [{"role": "user", "content": "hi"}]}
    body = to_gemini_request(req)
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "VALIDATED"}}
    assert "tool_choice" not in req


# --- quota, retries, throttle, budget ------------------------------------------------

def test_retry_after_rate_limit(tmp_path):
    err = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                     "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                  "retryDelay": "7s"}]}}
    t, http, clock = _transport(tmp_path, [(429, err), _raw([{"text": "ok"}])])
    resp = t.create({"model": "gemini-3.5-flash-lite", "messages": []})
    assert resp["stop_reason"] == "end_turn"
    assert 7.0 in clock.slept


def test_daily_quota_stops_the_run(tmp_path):
    err = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    t, _, _ = _transport(tmp_path, [(429, err)])
    with pytest.raises(QuotaExhausted):
        t.create({"model": "gemini-3.5-flash-lite", "messages": []})


def test_bad_request_is_not_retried(tmp_path):
    t, http, _ = _transport(tmp_path, [(400, {"error": {"message": "bad schema"}})])
    with pytest.raises(ProviderUnavailable, match="HTTP 400"):
        t.create({"model": "gemini-3.5-flash-lite", "messages": []})
    assert len(http.bodies) == 1


def test_server_errors_outlasting_the_retries_stop_the_run(tmp_path):
    overloaded = (503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
    t, http, clock = _transport(tmp_path, [overloaded] * 5, max_retries=4)
    with pytest.raises(ProviderUnavailable, match="HTTP 503") as info:
        t.create({"model": "gemini-3.5-flash-lite", "messages": []})
    assert isinstance(info.value, RunStop)       # a clean run stop, not an item failure
    assert len(http.bodies) == 5 and clock.slept == [2.0, 4.0, 8.0, 16.0]


def test_throttle_spaces_calls(tmp_path):
    t, _, clock = _transport(tmp_path, [_raw([{"text": "a"}]), _raw([{"text": "b"}])],
                             rpm=6)
    t.create({"model": "gemini-3.5-flash-lite", "messages": []})
    t.create({"model": "gemini-3.5-flash-lite", "messages": []})
    assert clock.slept and abs(clock.slept[-1] - 10.0) < 1e-9


def test_budget_cap_refuses_further_calls(tmp_path):
    big = {"promptTokenCount": 2_000_000, "candidatesTokenCount": 0}
    t, _, _ = _transport(tmp_path, [_raw([{"text": "a"}], usage=big),
                                    _raw([{"text": "b"}])])
    budget = Budget(cap_usd=0.5)
    bt = BudgetedTransport(t, budget)
    bt.create({"model": "gemini-3.5-flash-lite", "messages": []})
    assert budget.spent_usd == pytest.approx(0.60)
    with pytest.raises(BudgetExceeded):
        bt.create({"model": "gemini-3.5-flash-lite", "messages": []})


def test_prices_cover_versioned_ids():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert call_cost_usd("gemini-3.5-flash-lite-001", usage) == pytest.approx(2.80)
    assert call_cost_usd("gemini-3.5-flash-002", usage) == pytest.approx(10.50)


SMOKE = os.path.join(os.path.dirname(__file__), os.pardir, "data", "agent_transcripts",
                     "gemini_smoke.jsonl")


def test_a_real_gemini_recording_replays_and_translates():
    """The 2-call live smoke test (2026-09-24, gemini-3.5-flash-lite) pins the
    real wire format: raw replies translate to the recorded responses, and the
    loop rebuilds every request byte-for-byte (thought signatures echoed)."""
    with open(SMOKE, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    exchanges = [r for r in records if r["kind"] == "exchange"]
    for r in exchanges:
        assert from_gemini_response(r["provider_response"], r["n"]) == r["response"]
        part = r["provider_response"]["candidates"][0]["content"]["parts"][0]
        assert part["thoughtSignature"] and part["functionCall"]["id"]
    first = exchanges[0]["request"]
    submitted = {}
    impls = {"get_narration": lambda a: {
                 "txn_id": a.get("txn_id"), "reference": "SMOKE0001",
                 "narration": "NEFT CR RAZORPAY SOFTWARE PVT LTD SMOKETEST",
                 "engine_matched": False},
             "submit_resolution": lambda a: submitted.update(a) or {"received": True}}
    out = run_loop(ReplayTransport(SMOKE), system=first["system"], tools=first["tools"],
                   impls=impls, user_content=first["messages"][0]["content"],
                   max_tokens=first["max_tokens"], max_calls=3,
                   stop_when=lambda: bool(submitted))
    assert out.api_calls == 2 and submitted["verdict"] == "abstain"


def test_live_runs_refused_under_the_test_suite(tmp_path):
    with pytest.raises(AgentError, match="disabled"):
        make_live_transport(str(tmp_path / "x.jsonl"))
