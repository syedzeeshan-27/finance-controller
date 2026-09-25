"""Gemini transport for the agent loop (Gemini API, REST, stdlib only).

`agent.loop.run_loop` builds provider-neutral requests in the Messages-API
shape (system, tools with JSON-Schema inputs, user/assistant turns with
tool_use / tool_result blocks). This transport translates each request to
Gemini's `generateContent`, calls it, and translates the reply back into
the same shape, so the loop, the replay machinery and the transcripts do
not care which provider answered.

Round-trip rules:
- every Gemini part the model returns rides inside the echoed assistant
  block as `_gemini_part`, so thought signatures and function-call ids go
  back to Gemini verbatim on the next turn;
- a tool_result goes back as a `functionResponse` carrying the call's name
  (looked up from the matching tool_use) and id;
- `finishReason` maps to a stop reason: tool calls -> "tool_use", STOP ->
  "end_turn", MAX_TOKENS -> "max_tokens", blocked content -> "refusal";
  malformed or unexpected tool calls end the turn ("malformed_tool_call").

Transcripts: same JSONL format as `agent.provider.LiveTransport` (meta line,
then one exchange per call with `request_sha256` over the loop's request and
the translated response), so `ReplayTransport` replays them offline with no
key. To keep files small, only exchange 0 stores the full request; later
exchanges store the messages added since the previous call. Each exchange
also stores the raw Gemini reply and the wall-clock latency.

Quota handling (free tier): calls are spaced to `GEMINI_RPM` requests per
minute; a 429 with a retry delay waits and retries; a per-day quota raises
`QuotaExhausted`, so a run stops cleanly and can resume the next day. Any
other HTTP error, or a 429/5xx/network failure that outlasts the retries,
raises `ProviderUnavailable`: also a clean stop, never counted against the
agent.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from agent.provider import AgentError, ProviderUnavailable, RunStop, request_sha  # noqa: F401

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent")
DEFAULT_MODEL = "gemini-3.5-flash-lite"

_REFUSAL_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT",
                    "SPII", "IMAGE_SAFETY", "LANGUAGE"}
_MALFORMED_REASONS = {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"}


class QuotaExhausted(RunStop):
    """The API key's daily quota is used up; resume the run tomorrow."""


# --- request translation ------------------------------------------------------------

def _tool_names_by_id(messages: list) -> dict[str, str]:
    names = {}
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
    return names


def _result_payload(block: dict):
    content = block.get("content", "")
    if isinstance(content, list):      # text blocks
        content = "".join(c.get("text", "") for c in content)
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        value = content
    if block.get("is_error"):
        return {"error": value}
    return value if isinstance(value, dict) else {"result": value}


def to_gemini_request(request: dict, *, thinking_level: str | None = None,
                      fc_mode: str = "VALIDATED") -> dict:
    names = _tool_names_by_id(request.get("messages", []))
    contents = []
    for m in request.get("messages", []):
        content = m.get("content")
        if m["role"] == "user":
            if isinstance(content, str):
                contents.append({"role": "user", "parts": [{"text": content}]})
                continue
            parts = []
            for b in content:
                if b.get("type") == "tool_result":
                    call_id = b["tool_use_id"]
                    fr = {"name": names.get(call_id, ""),
                          "response": _result_payload(b)}
                    if not call_id.startswith("g_"):      # Gemini-issued id
                        fr["id"] = call_id
                    parts.append({"functionResponse": fr})
                elif b.get("type") == "text":
                    parts.append({"text": b["text"]})
            contents.append({"role": "user", "parts": parts})
        else:
            parts = []
            for b in content:
                part = b.get("_gemini_part")
                if part is None:          # not produced by this transport
                    if b.get("type") == "text":
                        part = {"text": b["text"]}
                    elif b.get("type") == "tool_use":
                        part = {"functionCall": {"name": b["name"],
                                                 "args": b.get("input", {})}}
                    else:
                        continue
                parts.append(part)
            contents.append({"role": "model", "parts": parts})

    body: dict = {"contents": contents}
    system = request.get("system")
    if system:
        text = system if isinstance(system, str) else "".join(
            b.get("text", "") for b in system)
        body["systemInstruction"] = {"parts": [{"text": text}]}
    tools = request.get("tools") or []
    if tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["name"], "description": t.get("description", ""),
             "parametersJsonSchema": t["input_schema"]} for t in tools]}]
        forced = request.get("tool_choice") or {}
        if forced.get("type") == "tool":        # the loop's last allowed call
            body["toolConfig"] = {"functionCallingConfig": {
                "mode": "ANY", "allowedFunctionNames": [forced["name"]]}}
        else:
            body["toolConfig"] = {"functionCallingConfig": {"mode": fc_mode}}
    gen: dict = {}
    if request.get("max_tokens"):
        gen["maxOutputTokens"] = request["max_tokens"]
    if thinking_level:
        gen["thinkingConfig"] = {"thinkingLevel": thinking_level}
    if gen:
        body["generationConfig"] = gen
    return body


# --- response translation -------------------------------------------------------------

def from_gemini_response(raw: dict, n: int) -> dict:
    candidates = raw.get("candidates") or []
    blocks: list[dict] = []
    finish = None
    if candidates:
        cand = candidates[0]
        finish = cand.get("finishReason")
        for i, part in enumerate((cand.get("content") or {}).get("parts") or []):
            if "functionCall" in part:
                fc = part["functionCall"]
                blocks.append({"type": "tool_use",
                               "id": fc.get("id") or f"g_{n}_{i}",
                               "name": fc.get("name", ""),
                               "input": fc.get("args") or {},
                               "_gemini_part": part})
            elif part.get("thought"):
                blocks.append({"type": "thinking",
                               "thinking": part.get("text", ""),
                               "_gemini_part": part})
            elif "text" in part:
                blocks.append({"type": "text", "text": part["text"],
                               "_gemini_part": part})
            else:
                blocks.append({"type": "other", "_gemini_part": part})

    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    elif not candidates or finish in _REFUSAL_REASONS:
        stop = "refusal"
    elif finish == "MAX_TOKENS":
        stop = "max_tokens"
    elif finish in _MALFORMED_REASONS:
        stop = "malformed_tool_call"
    else:
        stop = "end_turn"

    um = raw.get("usageMetadata") or {}
    cached = um.get("cachedContentTokenCount", 0)
    thoughts = um.get("thoughtsTokenCount", 0)
    usage = {
        "input_tokens": um.get("promptTokenCount", 0) - cached
                        + um.get("toolUsePromptTokenCount", 0),
        "cache_read_input_tokens": cached,
        "output_tokens": um.get("candidatesTokenCount", 0) + thoughts,
        "output_tokens_details": {"thinking_tokens": thoughts},
        "provider": "gemini",
    }
    return {"id": raw.get("responseId", ""), "model": raw.get("modelVersion", ""),
            "role": "assistant", "stop_reason": stop,
            "stop_details": {"finish_reason": finish,
                             "block_reason": (raw.get("promptFeedback") or {})
                             .get("blockReason")},
            "content": blocks, "usage": usage}


# --- the transport ------------------------------------------------------------------

def _post_json(url: str, body: dict, api_key: str, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _retry_delay_s(err_body: dict) -> float | None:
    for d in (err_body.get("error") or {}).get("details") or []:
        delay = d.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                return None
    return None


def _is_daily_quota(err_body: dict) -> bool:
    text = json.dumps(err_body).lower()
    return "perday" in text.replace("_", "").replace("-", "") or "per day" in text


class GeminiLiveTransport:
    """Calls Gemini and records every exchange (see module docstring)."""

    def __init__(self, transcript_path: str, *, task: str = "",
                 model_name: str | None = None, api_key: str | None = None,
                 rpm: float | None = None, thinking_level: str | None = None,
                 fc_mode: str | None = None, post=None, sleep=time.sleep,
                 clock=time.monotonic, max_retries: int = 8, timeout: float = 120):
        self.transcript_path = transcript_path
        self.model = model_name or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self.api_key:
            raise AgentError("GEMINI_API_KEY is not set")
        self.rpm = rpm if rpm is not None else float(os.getenv("GEMINI_RPM", "10"))
        self.thinking_level = (thinking_level if thinking_level is not None
                               else os.getenv("GEMINI_THINKING_LEVEL") or None)
        self.fc_mode = fc_mode or os.getenv("GEMINI_FC_MODE", "VALIDATED")
        self._post = post or _post_json
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        self.max_retries = max_retries
        self.timeout = timeout
        self._n = 0
        self._sent_messages = 0
        os.makedirs(os.path.dirname(os.path.abspath(transcript_path)), exist_ok=True)
        self._write({"kind": "meta", "task": task, "model": self.model,
                     "provider": "gemini", "thinking_level": self.thinking_level,
                     "fc_mode": self.fc_mode})

    def _write(self, record: dict) -> None:
        with open(self.transcript_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _throttle(self) -> None:
        if self.rpm and self._last_call is not None:
            wait = 60.0 / self.rpm - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def create(self, request: dict) -> dict:
        body = to_gemini_request(request, thinking_level=self.thinking_level,
                                 fc_mode=self.fc_mode)
        url = ENDPOINT.format(model=request.get("model") or self.model)
        attempt = 0
        while True:
            self._throttle()
            t0 = self._clock()
            try:
                raw = self._post(url, body, self.api_key, self.timeout)
                latency = self._clock() - t0
                break
            except urllib.error.HTTPError as exc:
                try:
                    err = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    err = {}
                if exc.code == 429 and _is_daily_quota(err):
                    raise QuotaExhausted(
                        "Gemini daily quota exhausted; resume tomorrow") from exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    attempt += 1
                    self._sleep(min(_retry_delay_s(err) or 2.0 ** attempt, 90.0))
                    continue
                raise ProviderUnavailable(f"Gemini HTTP {exc.code}: "
                                          f"{json.dumps(err)[:500]}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self._sleep(2.0 ** attempt)
                    continue
                raise ProviderUnavailable(f"Gemini unreachable: {exc}") from exc

        resp = from_gemini_response(raw, self._n)
        record = {"kind": "exchange", "n": self._n,
                  "request_sha256": request_sha(request)}
        messages = request.get("messages", [])
        if self._n == 0:
            record["request"] = request
        else:
            record["request_delta"] = messages[self._sent_messages:]
        record.update({"response": resp, "provider_response": raw,
                       "latency_s": round(latency, 3)})
        self._write(record)
        self._sent_messages = len(messages)
        self._n += 1
        return resp
