"""Transports for the agent loop: live (records) and replay (proves).

Both operate on plain wire-shaped dicts so live and replay are symmetric:
a `request` is exactly the kwargs of `client.messages.create`, a `response`
is `Message.to_dict()`. A transcript is JSONL — line 0 is a `meta` record,
then one `exchange` per API call:

    {"kind": "meta", "task": ..., "model": ..., ...}
    {"kind": "exchange", "n": 0, "request_sha256": ..., "request": {...},
     "response": {...}}

Replay integrity: before serving a recorded response, ReplayTransport
re-hashes the request the harness is about to send and compares it to the
recorded hash. Any drift in prompts, tool schemas, or tool results raises
ReplayDivergence instead of silently serving a stale answer — the committed
transcript is byte-for-byte the conversation the current code would have.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Protocol

import model


class Transport(Protocol):
    def create(self, request: dict) -> dict: ...


class ReplayDivergence(Exception):
    """The harness built a request that differs from the recording."""


class AgentError(Exception):
    """The agent run failed (budget exhausted, refusal, transcript end)."""


class RunStop(AgentError):
    """Stop the whole run (spend cap or provider quota), never a per-item
    failure: callers re-raise it so the run ends cleanly and can resume."""


class ProviderUnavailable(RunStop):
    """The provider kept failing (an HTTP error, an empty credit balance, or
    unreachable after the retries). That is the provider's failure, not the
    agent's: the run stops cleanly and `--resume` re-runs the item later."""


def request_sha(request: dict) -> str:
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LiveTransport:
    """Calls the Anthropic API and records every exchange to a transcript.

    The system prompt goes out as a cached block (prompt caching: repeat
    calls pay a tenth of the price for it). That changes the bill, not the
    request the transcript hashes. Like the Gemini transport, only exchange
    0 stores the full request; later ones store the messages added since.

    `ANTHROPIC_EFFORT` (low | medium | high | xhigh | max) sets how hard the
    model thinks (`output_config.effort`; Sonnet 5 thinks adaptively by
    default at "high"). Like Gemini's thinking level it goes on the wire
    only and is recorded in the meta line as `thinking_level`, where the
    pre-registration check reads it."""

    EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def __init__(self, transcript_path: str, *, task: str = "",
                 model_name: str | None = None, create=None,
                 clock=time.monotonic, effort: str | None = None):
        self.transcript_path = transcript_path
        self.model = model_name or model.LLM_MODEL
        self.effort = effort if effort is not None else (
            os.getenv("ANTHROPIC_EFFORT") or None)
        if self.effort is not None and self.effort not in self.EFFORTS:
            raise AgentError(f"ANTHROPIC_EFFORT must be one of {self.EFFORTS}, "
                             f"got {self.effort!r}")
        self._create = create
        self._clock = clock
        self._n = 0
        self._sent_messages = 0
        os.makedirs(os.path.dirname(os.path.abspath(transcript_path)),
                    exist_ok=True)
        self._write({"kind": "meta", "task": task, "model": self.model,
                     "provider": "anthropic",
                     "thinking_level": self.effort or "model default",
                     "fc_mode": "auto"})

    def _write(self, record: dict) -> None:
        with open(self.transcript_path, "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def create(self, request: dict) -> dict:
        sent = dict(request)
        if isinstance(sent.get("system"), str) and sent["system"]:
            sent["system"] = [{"type": "text", "text": sent["system"],
                               "cache_control": {"type": "ephemeral"}}]
        if self.effort:
            sent["output_config"] = {"effort": self.effort}
        t0 = self._clock()
        try:
            if self._create is not None:
                resp = self._create(**sent)
            else:
                resp = model.client().messages.create(**sent).to_dict()
        except Exception as exc:
            if _is_provider_error(exc):
                raise ProviderUnavailable(
                    f"Anthropic {type(exc).__name__}: {str(exc)[:500]}") from exc
            raise
        latency = self._clock() - t0
        messages = request.get("messages", [])
        record = {"kind": "exchange", "n": self._n,
                  "request_sha256": request_sha(request)}
        if self._n == 0:
            record["request"] = request
        else:
            record["request_delta"] = messages[self._sent_messages:]
        record.update({"response": resp, "latency_s": round(latency, 3)})
        self._write(record)
        self._sent_messages = len(messages)
        self._n += 1
        return resp


def _is_provider_error(exc: Exception) -> bool:
    """Anthropic SDK errors that outlasted its own retries (rate limits,
    overload, credit balance, connection)."""
    try:
        import anthropic
    except ImportError:                      # pragma: no cover
        return False
    return isinstance(exc, (anthropic.APIStatusError, anthropic.APIConnectionError))


def make_live_transport(transcript_path: str, *, task: str = ""):
    """A recording live transport for whichever provider is configured.

    `AGENT_PROVIDER` picks explicitly ("gemini" | "anthropic"); otherwise
    Gemini when `GEMINI_API_KEY` is set, else Anthropic. `APP_USE_MOCK=1`
    (forced by the test suite) refuses every live run."""
    if os.getenv("APP_USE_MOCK", "0") == "1":
        raise AgentError("live agent runs are disabled (APP_USE_MOCK=1)")
    provider = os.getenv("AGENT_PROVIDER") or (
        "gemini" if os.getenv("GEMINI_API_KEY") else "anthropic")
    if provider == "gemini":
        from agent.gemini import GeminiLiveTransport
        return GeminiLiveTransport(transcript_path, task=task)
    return LiveTransport(transcript_path, task=task)


class ReplayTransport:
    """Serves recorded responses; proves the harness hasn't drifted."""

    def __init__(self, transcript_path: str):
        self.transcript_path = transcript_path
        with open(transcript_path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        self.meta = records[0] if records and records[0].get(
            "kind") == "meta" else {}
        self._exchanges = [r for r in records if r.get("kind") == "exchange"]
        self._i = 0

    def create(self, request: dict) -> dict:
        if self._i >= len(self._exchanges):
            raise AgentError(
                f"transcript exhausted after {self._i} exchange(s): "
                f"{self.transcript_path}")
        rec = self._exchanges[self._i]
        got = request_sha(request)
        if got != rec["request_sha256"]:
            raise ReplayDivergence(
                f"exchange {rec.get('n', self._i)}: request hash {got[:12]}… "
                f"!= recorded {rec['request_sha256'][:12]}… — the harness "
                f"(prompt, tools, or tool results) has drifted from the "
                f"recording; re-record the transcript")
        self._i += 1
        return rec["response"]
