"""The agent runtime: loop discipline, budgets, transcript replay integrity."""

import json

import pytest

from agent import tools as T
from agent.loop import run_loop
from agent.provider import (AgentError, ReplayDivergence, ReplayTransport,
                            request_sha)

PING = T.strict_tool("ping", "reply with pong", {"n": {"type": "integer"}})
SUBMIT = T.strict_tool("submit", "submit the answer",
                       {"answer": {"type": "string"}})


def _tool_use(name, tool_id, **kwargs):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": kwargs}


def _resp(stop_reason, content):
    return {"stop_reason": stop_reason, "content": content}


class FakeTransport:
    """Scripted responses; captures every request the loop builds."""

    model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def test_loop_returns_all_tool_results_in_one_message():
    fake = FakeTransport([
        _resp("tool_use", [_tool_use("ping", "t1", n=1),
                           _tool_use("ping", "t2", n=2)]),
        _resp("end_turn", [{"type": "text", "text": "done"}]),
    ])
    out = run_loop(fake, system="s", tools=[PING],
                   impls={"ping": lambda inp: f"pong {inp['n']}"},
                   user_content="go")
    assert out.stop_reason == "end_turn" and out.final_text == "done"
    follow_up = fake.requests[1]["messages"][-1]
    assert follow_up["role"] == "user"
    assert [b["type"] for b in follow_up["content"]] == ["tool_result"] * 2
    assert {b["tool_use_id"] for b in follow_up["content"]} == {"t1", "t2"}


def test_failing_tool_becomes_is_error_result_not_a_crash():
    def boom(_inp):
        raise ValueError("nope")

    fake = FakeTransport([
        _resp("tool_use", [_tool_use("ping", "t1", n=1)]),
        _resp("end_turn", [{"type": "text", "text": "ok"}]),
    ])
    run_loop(fake, system="s", tools=[PING], impls={"ping": boom},
             user_content="go")
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "ValueError" in result["content"]


def test_loop_bounds_api_calls():
    always_tools = [_resp("tool_use", [_tool_use("ping", f"t{i}", n=i)])
                    for i in range(99)]
    fake = FakeTransport(always_tools)
    with pytest.raises(AgentError, match="budget"):
        run_loop(fake, system="s", tools=[PING],
                 impls={"ping": lambda inp: "pong"},
                 user_content="go", max_calls=5)
    assert len(fake.requests) == 5


def test_loop_stops_after_acceptance_without_extra_call():
    accepted = {}

    def submit(inp):
        accepted["answer"] = inp["answer"]
        return {"accepted": True}

    fake = FakeTransport([
        _resp("tool_use", [_tool_use("submit", "t1", answer="42")]),
        _resp("end_turn", [{"type": "text", "text": "never reached"}]),
    ])
    out = run_loop(fake, system="s", tools=[SUBMIT], impls={"submit": submit},
                   user_content="go", stop_when=lambda: "answer" in accepted)
    assert out.stop_reason == "accepted"
    assert out.api_calls == 1 and len(fake.requests) == 1


def test_loop_aborts_on_refusal():
    fake = FakeTransport([_resp("refusal", [])])
    with pytest.raises(AgentError, match="refusal"):
        run_loop(fake, system="s", tools=[PING], impls={}, user_content="go")


def _write_transcript(path, exchanges, meta=None):
    records = [{"kind": "meta", "model": "fake-model", **(meta or {})}]
    for n, (request, response) in enumerate(exchanges):
        records.append({"kind": "exchange", "n": n,
                        "request_sha256": request_sha(request),
                        "request": request, "response": response})
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")


def test_replay_serves_recorded_responses_in_order(tmp_path):
    req_a = {"model": "fake-model", "messages": [1]}
    req_b = {"model": "fake-model", "messages": [1, 2]}
    p = tmp_path / "t.jsonl"
    _write_transcript(p, [(req_a, _resp("tool_use", [])),
                          (req_b, _resp("end_turn", []))])
    replay = ReplayTransport(str(p))
    assert replay.create(req_a)["stop_reason"] == "tool_use"
    assert replay.create(req_b)["stop_reason"] == "end_turn"


def test_replay_divergence_raises_on_tampered_request(tmp_path):
    req_a = {"model": "fake-model", "messages": [1]}
    p = tmp_path / "t.jsonl"
    _write_transcript(p, [(req_a, _resp("end_turn", []))])
    replay = ReplayTransport(str(p))
    with pytest.raises(ReplayDivergence, match="drifted"):
        replay.create({"model": "fake-model", "messages": [999]})


def test_replay_exhausted_transcript_raises(tmp_path):
    req_a = {"model": "fake-model", "messages": [1]}
    p = tmp_path / "t.jsonl"
    _write_transcript(p, [(req_a, _resp("end_turn", []))])
    replay = ReplayTransport(str(p))
    replay.create(req_a)
    with pytest.raises(AgentError, match="exhausted"):
        replay.create(req_a)


def test_request_sha_is_order_insensitive_for_keys():
    assert request_sha({"a": 1, "b": 2}) == request_sha({"b": 2, "a": 1})
    assert request_sha({"a": 1}) != request_sha({"a": 2})


def test_tool_schemas_are_strict():
    for tool in (PING, SUBMIT):
        T.assert_strict(tool)
    with pytest.raises(AssertionError):
        T.assert_strict({"name": "loose", "strict": False,
                         "input_schema": {"type": "object", "properties": {},
                                          "required": [],
                                          "additionalProperties": True}})
