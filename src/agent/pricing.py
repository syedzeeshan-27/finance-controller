"""List prices, per-call cost, and the hard budget cap for live agent runs.

Cost is computed from the usage block every recorded response carries, so
it is the same number live and on offline replay. Prices are USD per
million tokens at the providers' standard paid tier. Anthropic runs are
billed at these prices; on the Gemini free tier nothing is billed and the
figures are the "list-price equivalent" of the run.

`AGENT_MAX_USD` (default 10) is enforced before every live call: once the
run's spend reaches it, `BudgetedTransport` refuses the next request.
"""

from __future__ import annotations

import os

from agent.provider import RunStop

# model id -> (input, output, cached input) USD per 1M tokens
PRICES: dict[str, tuple[float, float, float]] = {
    # Gemini, standard paid tier (ai.google.dev/gemini-api/docs/pricing, 2026-09-24)
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.03),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
    "gemini-3.8-flash": (0.75, 3.75, 0.075),
    "gemini-3.7-flash": (0.75, 3.75, 0.075),
    "gemini-3.5-flash": (1.50, 9.00, 0.15),
    # Anthropic (platform.claude.com/docs/en/about-claude/pricing, 2026-09-24)
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25   # Anthropic 5-minute cache writes cost 1.25x base input


class BudgetExceeded(RunStop):
    """The run hit AGENT_MAX_USD; no further live calls are made."""


def max_usd() -> float:
    return float(os.getenv("AGENT_MAX_USD", "10"))


def call_cost_usd(model: str, usage: dict) -> float:
    """Cost of one response from its usage block. Handles both the Anthropic
    usage shape and the normalised Gemini shape the Gemini transport writes
    (input_tokens excludes cached tokens in both)."""
    price = PRICES.get(model)
    if price is None:
        # versioned ids ("gemini-3.5-flash-lite-001") map to the longest
        # listed family prefix
        family = max((m for m in PRICES if model.startswith(m)), key=len,
                     default=None)
        price = PRICES.get(family) if family else None
    if price is None:
        raise KeyError(f"no list price for model {model!r}; add it to PRICES")
    p_in, p_out, p_cached = price
    fresh = usage.get("input_tokens", 0)
    written = usage.get("cache_creation_input_tokens", 0) or 0
    cached = usage.get("cache_read_input_tokens", 0) or 0
    out = usage.get("output_tokens", 0)
    return (fresh * p_in + written * p_in * CACHE_WRITE_MULTIPLIER
            + cached * p_cached + out * p_out) / 1_000_000


class BudgetedTransport:
    """Wraps a transport; refuses calls once the shared spend reaches the cap.

    `spent` is shared across every item of a run (pass the same Budget)."""

    def __init__(self, inner, budget: "Budget"):
        self.inner = inner
        self.budget = budget
        self.model = getattr(inner, "model", None)
        self.meta = getattr(inner, "meta", None)

    def create(self, request: dict) -> dict:
        if self.budget.spent_usd >= self.budget.cap_usd:
            raise BudgetExceeded(
                f"AGENT_MAX_USD={self.budget.cap_usd} reached "
                f"(spent {self.budget.spent_usd:.4f} USD)")
        resp = self.inner.create(request)
        model = request.get("model") or resp.get("model", "")
        self.budget.spent_usd += call_cost_usd(model, resp.get("usage") or {})
        return resp


class Budget:
    def __init__(self, cap_usd: float | None = None, spent_usd: float = 0.0):
        self.cap_usd = max_usd() if cap_usd is None else cap_usd
        self.spent_usd = spent_usd
