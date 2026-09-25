"""The Anthropic transport: the system prompt is cached on the wire while the
recorded request stays the loop's own, transcripts are compact and replay
offline, latency is recorded, and API errors become a clean run stop."""

import json

import pytest

from agent import tools as T
from agent.loop import run_loop
from agent.pricing import call_cost_usd
from agent.provider import (AgentError, LiveTransport, ProviderUnavailable, ReplayTransport,
                            RunStop)

LOOKUP = T.strict_tool("lookup", "look something up", {"key": {"type": "string"}})
SUBMIT = T.strict_tool("submit", "submit the answer", {"answer": {"type": "string"}})


def _msg(block):
    return {"id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [block], "stop_reason": "tool_use",
            "usage": {"input_tokens": 50, "output_tokens": 20,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1500}}


class FakeCreate:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def __call__(self, **kwargs):
        self.sent.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _run(transport, got):
    return run_loop(transport, system="sys", tools=[LOOKUP, SUBMIT],
                    impls={"lookup": lambda a: "v",
                           "submit": lambda a: got.update(a) or "ok"},
                    user_content="go", max_calls=3, stop_when=lambda: bool(got),
                    final_tool="submit")


def test_cached_system_prompt_compact_transcript_and_offline_replay(tmp_path):
    fake = FakeCreate([
        _msg({"type": "tool_use", "id": "t1", "name": "lookup", "input": {"key": "k"}}),
        _msg({"type": "tool_use", "id": "t2", "name": "submit", "input": {"answer": "42"}})])
    path = str(tmp_path / "t.jsonl")
    got: dict = {}
    live = _run(LiveTransport(path, task="t", model_name="claude-sonnet-5", create=fake), got)
    assert fake.sent[0]["system"] == [{"type": "text", "text": "sys",
                                       "cache_control": {"type": "ephemeral"}}]
    recs = [json.loads(line) for line in open(path, encoding="utf-8")]
    assert recs[0]["provider"] == "anthropic"
    assert recs[1]["request"]["system"] == "sys" and "latency_s" in recs[1]
    assert "request" not in recs[2] and recs[2]["request_delta"]
    got.clear()
    replay = _run(ReplayTransport(path), got)
    assert replay.messages == live.messages and got == {"answer": "42"}


def test_effort_goes_on_the_wire_and_into_meta_not_the_hashed_request(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_EFFORT", "medium")
    fake = FakeCreate([
        _msg({"type": "tool_use", "id": "t1", "name": "submit", "input": {"answer": "1"}})])
    path = str(tmp_path / "t.jsonl")
    _run(LiveTransport(path, task="t", model_name="claude-sonnet-5", create=fake), {})
    assert fake.sent[0]["output_config"] == {"effort": "medium"}
    recs = [json.loads(line) for line in open(path, encoding="utf-8")]
    assert recs[0]["thinking_level"] == "medium" and recs[0]["fc_mode"] == "auto"
    assert "output_config" not in recs[1]["request"]
    monkeypatch.setenv("ANTHROPIC_EFFORT", "extreme")
    with pytest.raises(AgentError, match="ANTHROPIC_EFFORT"):
        LiveTransport(str(tmp_path / "u.jsonl"), model_name="claude-sonnet-5", create=fake)


def test_api_errors_become_a_clean_run_stop(tmp_path):
    import anthropic
    import httpx2               # the HTTP client this SDK version is built on
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("Your credit balance is too low",
                                    response=httpx2.Response(400, request=request), body=None)
    t = LiveTransport(str(tmp_path / "t.jsonl"), model_name="claude-sonnet-5",
                      create=FakeCreate([err]))
    with pytest.raises(ProviderUnavailable, match="credit balance") as info:
        t.create({"model": "claude-sonnet-5", "messages": []})
    assert isinstance(info.value, RunStop)


def test_cache_writes_cost_more_and_cache_reads_less():
    million = 1_000_000
    usage = {"input_tokens": million, "output_tokens": million,
             "cache_creation_input_tokens": million, "cache_read_input_tokens": million}
    assert call_cost_usd("claude-sonnet-5", usage) == pytest.approx(2.00 + 2.50 + 0.20 + 10.00)
