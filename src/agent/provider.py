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
from typing import Protocol

import model


class Transport(Protocol):
    def create(self, request: dict) -> dict: ...


class ReplayDivergence(Exception):
    """The harness built a request that differs from the recording."""


class AgentError(Exception):
    """The agent run failed (budget exhausted, refusal, transcript end)."""


def request_sha(request: dict) -> str:
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LiveTransport:
    """Calls the Anthropic API and records every exchange to a transcript."""

    def __init__(self, transcript_path: str, *, task: str = "",
                 model_name: str | None = None):
        self.transcript_path = transcript_path
        self.model = model_name or model.LLM_MODEL
        self._n = 0
        os.makedirs(os.path.dirname(os.path.abspath(transcript_path)),
                    exist_ok=True)
        self._write({"kind": "meta", "task": task, "model": self.model})

    def _write(self, record: dict) -> None:
        with open(self.transcript_path, "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def create(self, request: dict) -> dict:
        resp = model.client().messages.create(**request).to_dict()
        self._write({"kind": "exchange", "n": self._n,
                     "request_sha256": request_sha(request),
                     "request": request, "response": resp})
        self._n += 1
        return resp


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
