"""The hand-rolled tool-use loop — deliberately visible, bounded, replayable.

One function, no framework: build a request, send it through a Transport
(live or replay), execute the tool calls the model makes, feed the results
back, stop on completion / acceptance / budget. The loop itself never
interprets domain content; tool implementations are plain deterministic
functions supplied by the caller.

Wire rules honoured here (Claude 5 family):
- the assistant's content is echoed back verbatim (thinking blocks included);
- all tool_results for one assistant turn go back in ONE user message;
- a failing tool implementation becomes a tool_result with is_error, never
  a dropped call;
- `pause_turn` is resumed by resending; `refusal` and `max_tokens` abort.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from agent.provider import AgentError, Transport


@dataclass
class LoopResult:
    stop_reason: str
    api_calls: int
    final_text: str
    messages: list = field(default_factory=list)


def transport_model(transport: Transport) -> str:
    """The model name a transport speaks for (live: configured; replay:
    whatever the recording used — keeps rebuilt requests byte-identical)."""
    name = getattr(transport, "model", None)
    if name:
        return name
    meta = getattr(transport, "meta", None) or {}
    return meta.get("model", "")


def _result_content(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def run_loop(transport: Transport, *, system: str, tools: list[dict],
             impls: dict[str, Callable[[dict], object]],
             user_content: str, max_tokens: int = 8000,
             max_calls: int = 12,
             stop_when: Callable[[], bool] | None = None) -> LoopResult:
    """Drive one agent task to completion. Returns the full message history
    for transcripting/inspection; raises AgentError on any abnormal stop."""
    messages: list = [{"role": "user", "content": user_content}]
    model_name = transport_model(transport)

    for call in range(max_calls):
        # snapshot the history so recorded requests can't mutate retroactively
        request = {"model": model_name, "max_tokens": max_tokens,
                   "system": system, "tools": tools,
                   "messages": list(messages)}
        resp = transport.create(request)
        stop = resp.get("stop_reason")
        content = resp.get("content", [])
        # echo the assistant turn back verbatim — required for thinking blocks
        messages.append({"role": "assistant", "content": content})

        if stop == "pause_turn":
            continue
        if stop in ("refusal", "max_tokens"):
            raise AgentError(f"model stopped with {stop!r} on call {call}")
        if stop != "tool_use":
            text = "".join(b.get("text", "") for b in content
                           if b.get("type") == "text")
            return LoopResult(stop_reason=stop, api_calls=call + 1,
                              final_text=text, messages=messages)

        results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name, tool_id = block["name"], block["id"]
            impl = impls.get(name)
            try:
                if impl is None:
                    raise KeyError(f"no implementation for tool {name!r}")
                value = impl(block.get("input", {}))
                results.append({"type": "tool_result", "tool_use_id": tool_id,
                                "content": _result_content(value)})
            except Exception as exc:  # surfaced to the model, never dropped
                results.append({"type": "tool_result", "tool_use_id": tool_id,
                                "content": _result_content(
                                    {"error": f"{type(exc).__name__}: {exc}"}),
                                "is_error": True})
        messages.append({"role": "user", "content": results})

        if stop_when is not None and stop_when():
            return LoopResult(stop_reason="accepted", api_calls=call + 1,
                              final_text="", messages=messages)

    raise AgentError(f"call budget exhausted ({max_calls} API calls)")
