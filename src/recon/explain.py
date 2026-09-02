"""Human-readable explanations for reconciliation decisions.

Strictly a presentation layer. The deterministic template renderer is the
source of truth; when an LLM key is configured it may rewrite the phrasing,
but it NEVER changes a decision — explanations live in a display-only field
that grading and verification ignore, and a test asserts the decision list is
identical with the LLM on or off.
"""

from __future__ import annotations

from recon import schemas as S
from recon.normalize import rupee_str_from_paise

try:  # model.py sits at the src/ root; optional for explanations
    import model as _model
except Exception:  # pragma: no cover
    _model = None


def llm_mode() -> str:
    if _model is None:
        return "unavailable"
    return "mock(deterministic templates)" if _model.USING_MOCK else _model.LLM_MODEL


def _inr(paise: int | None) -> str:
    return "?" if paise is None else "Rs " + rupee_str_from_paise(paise, indian_grouping=True)


def _render_template(d: S.Decision) -> str:
    """Deterministic explanation, pure function of the decision record."""
    ev = "; ".join(e["detail"] for e in d.evidence[:3])
    if d.status == S.MATCHED:
        return (f"Matched ({d.confidence}): settlement {d.settlement_ids[0]} is "
                f"covered by bank credit {d.bank_txn_ids[0]} for "
                f"{_inr(d.received_paise)}. Evidence: {ev}.")
    if d.status == S.MATCHED_SPLIT:
        return (f"Split settlement: {len(d.bank_txn_ids)} bank credits together "
                f"cover settlement {d.settlement_ids[0]} exactly "
                f"({_inr(d.expected_paise)}). Evidence: {ev}.")
    if d.status == S.MATCHED_MERGED:
        return (f"Consolidated credit: bank credit {d.bank_txn_ids[0]} covers "
                f"{len(d.settlement_ids)} settlements exactly. Evidence: {ev}.")
    if d.status == S.MATCHED_WITH_DISCREPANCY:
        parts = ", ".join(f"{b['label']} {_inr(b['amount_paise'])}"
                          for b in d.discrepancy_breakdown)
        return (f"Matched with a {_inr(d.discrepancy_paise)} discrepancy "
                f"({parts}). Expected {_inr(d.expected_paise)}, received "
                f"{_inr(d.received_paise)}. Evidence: {ev}.")
    if d.status == S.DUPLICATE_CREDIT:
        return (f"Duplicate posting: {d.bank_txn_ids[0]} repeats an "
                f"already-reconciled credit. Ask the bank to reverse it.")
    if d.status == S.AMBIGUOUS_ABSTAIN:
        cands = ", ".join(c["record_id"] for c in d.candidates if c["record_id"])
        return (f"Abstained: candidates ({cands}) cannot be told apart on the "
                f"available evidence. A human should confirm with the bank "
                f"before any of them is claimed.")
    if d.status == S.EXCEPTION_MISSING_BANK:
        best = d.candidates[0] if d.candidates else None
        near = (f" Nearest miss: {best['record_id']} ({best['reason']})."
                if best and best.get("record_id") else "")
        return (f"Settlement {d.settlement_ids[0]} ({_inr(d.expected_paise)}) "
                f"never arrived in the bank.{near} Chase with the PSP/bank.")
    if d.status == S.EXCEPTION_MISSING_SETTLEMENT:
        return (f"Bank credit {d.bank_txn_ids[0]} ({_inr(d.received_paise)}) "
                f"looks like a PSP settlement but matches no known settlement. "
                f"Verify the source of funds.")
    if d.status == S.NON_SETTLEMENT_CREDIT:
        return "Unrelated inflow; excluded from settlement reconciliation."
    if d.status == S.OUT_OF_SCOPE:
        return "Bank debit; out of scope for settlement reconciliation."
    return f"{d.status}: {ev}"


_LLM_SYSTEM = (
    "You rewrite one reconciliation finding as one clear sentence for a "
    "finance operator. Do not add, remove, or change any identifier, amount, "
    "or conclusion. Do not compute anything.")


def attach_explanations(decisions: list[S.Decision], use_llm: bool = False) -> str:
    """Fill the display-only explanation field on every decision.

    Returns the mode used. Deterministic templates always run; the LLM (when
    available AND requested) only rephrases the already-final text."""
    mode = "templates"
    for d in decisions:
        d.explanation = _render_template(d)
    if use_llm and _model is not None and not _model.USING_MOCK:
        mode = llm_mode()
        for d in decisions:
            if d.kind in ("exception",) or d.confidence == S.CONF_NEEDS_REVIEW:
                rewritten = _model.complete(_LLM_SYSTEM, d.explanation)
                if rewritten:
                    d.explanation = rewritten.strip()
    return mode
