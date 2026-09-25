"""Thin provider wrapper around the LLM (Anthropic Claude).

Two reasons this layer exists:

1. **Provider independence.** Every model call goes through here (and through
   `agent.provider` for the tool-use loop, which builds on this module's
   client), so swapping providers is a one-file change. In a production
   financial system you do not want the provider hard-wired through your code.

2. **An offline mock.** If no API key is present (or APP_USE_MOCK=1), we fall
   back to deterministic behaviour: `complete` returns None and callers use
   their own deterministic fallback. That keeps the whole project runnable,
   free, and reproducible for a reviewer with no key.

Nothing here ever does financial arithmetic. The LLM's only jobs are
free-text narrative and (in `src/agent/`) *proposing* structure that
deterministic code then proves or rejects; numbers are always the
engines' responsibility.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

LLM_MODEL = os.getenv("APP_LLM_MODEL", "claude-sonnet-5")
_FORCE_MOCK = os.getenv("APP_USE_MOCK", "0") == "1"
_HAS_KEY = bool(os.getenv("ANTHROPIC_API_KEY"))

USING_MOCK = _FORCE_MOCK or not _HAS_KEY

_client = None


def client():
    """The shared Anthropic client, constructed lazily.

    Raises RuntimeError in mock mode — callers that can run offline must
    check USING_MOCK first (or use `complete`, which returns None)."""
    global _client
    if USING_MOCK:
        raise RuntimeError("LLM client requested in mock mode "
                           "(no ANTHROPIC_API_KEY, or APP_USE_MOCK=1)")
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(max_retries=6)   # rate limits and overload back off, then retry
    return _client


def complete(system: str, user: str) -> str | None:
    """Free-text completion for narrative work (e.g. explaining a decision a
    human can read). Returns None in mock mode or on any provider failure —
    callers must have their own deterministic fallback, because narrative
    must never be a load-bearing part of a financial decision."""
    if USING_MOCK:
        return None
    try:
        resp = client().messages.create(
            model=LLM_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception:
        return None
    return "".join(b.text for b in resp.content if b.type == "text")
