"""Deterministic enumeration of every verifiable explanation of a queue item.

Two uses, both LLM-free:

1. Prefilter. An item with no explanation the verifier could accept is not
   sent to the agent: whatever it proposed would be rejected. This saves
   free-tier quota and changes no outcome.
2. Ablation. "Engine + solver": propose an item's explanation exactly when
   it is the only one the verifier accepts. Comparing that row with
   "engine + agent" shows what the LLM adds beyond exhaustive search under
   the same verifier.

Search space (bounded, reported when truncated):
- one credit <-> 1..6 settlements created 0..10 days before it, deduction
  d in {0} + every money figure in the credit's narration/reference;
- one settlement <-> 2..4 credits within its window (no deduction).
Every candidate is checked by `resolve_verifier.verify_proposal`, so the
solver can never accept what the verifier would reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent import resolve_verifier as RV

MAX_SETTLEMENTS = 6
MAX_SPLIT_CREDITS = 4
NODE_BUDGET = 200_000


@dataclass
class Enumeration:
    explanations: list[dict] = field(default_factory=list)
    truncated: bool = False


def _subsets_summing(pool: list[tuple[str, int]], target: int, k: int,
                     must: tuple[str, ...], budget: list[int]) -> list[list[str]]:
    """Subsets of `pool` (id, amount; amounts > 0) of size <= k that include
    every id in `must` and sum to `target`. Depth-first over ascending
    amounts with sum pruning; `budget` is a one-element node counter."""
    must_set = set(must)
    must_sum = sum(a for i, a in pool if i in must_set)
    rest = sorted(((i, a) for i, a in pool if i not in must_set),
                  key=lambda x: (x[1], x[0]))
    need = target - must_sum
    slots = k - len(must_set)
    out: list[list[str]] = []
    if need == 0 and must_set:
        out.append(sorted(must_set))
    if need <= 0 or slots <= 0:
        return out

    def dfs(start: int, remaining: int, chosen: list[str], left: int) -> None:
        for j in range(start, len(rest)):
            budget[0] -= 1
            if budget[0] < 0:
                return
            i, a = rest[j]
            if a > remaining:
                break
            if a == remaining:
                out.append(sorted(list(must_set) + chosen + [i]))
                continue
            if left > 1:
                dfs(j + 1, remaining - a, chosen + [i], left - 1)

    dfs(0, need, [], slots)
    return out


def enumerate_explanations(view: RV.WorldView, item_records: list[str],
                           claimed, strict: bool = True) -> Enumeration:
    claimed = set(claimed)
    item_s = tuple(sorted(r for r in item_records if r in view.settlements))
    item_c = [r for r in item_records if r in view.credits]
    budget = [NODE_BUDGET]
    candidates: list[dict] = []

    def add(sids, tids, d, evidence):
        candidates.append({"verdict": "match", "settlement_ids": sorted(sids),
                           "bank_row_ids": sorted(tids), "deduction_paise": d,
                           "deduction_evidence": evidence if d else "",
                           "reason": "enumerated"})

    def open_settlements_for(tid):
        c = view.credits[tid]
        return [(s, v["net"]) for s, v in view.settlements.items()
                if s not in claimed
                and 0 <= (c["value_date"] - v["created"]).days <= RV.WINDOW_DAYS]

    def deductions(tid):
        c = view.credits[tid]
        figs = set()
        for text in (c["narration"], c["ref"]):
            for value, _, _ in RV.money_figure_spans(RV.collapse_ws(text)):
                figs.add((value, text))
        return [(0, "")] + sorted(figs)

    credit_anchors = item_c if item_c else []
    for tid in credit_anchors:
        pool = open_settlements_for(tid)
        for d, text in deductions(tid):
            target = view.credits[tid]["amount"] + d
            for subset in _subsets_summing(pool, target, MAX_SETTLEMENTS,
                                           item_s, budget):
                add(subset, [tid], d, text)

    if not item_c:
        for sid in item_s:
            s = view.settlements[sid]
            window = [(t, c["amount"]) for t, c in view.credits.items()
                      if t not in claimed
                      and 0 <= (c["value_date"] - s["created"]).days <= RV.WINDOW_DAYS]
            # one credit carrying this settlement (possibly merged with others)
            for tid, _ in window:
                pool = open_settlements_for(tid)
                for d, text in deductions(tid):
                    target = view.credits[tid]["amount"] + d
                    for subset in _subsets_summing(pool, target, MAX_SETTLEMENTS,
                                                   (sid,), budget):
                        add(subset, [tid], d, text)
            # one settlement split over several credits
            for split in _subsets_summing(window, s["net"], MAX_SPLIT_CREDITS,
                                          (), budget):
                if len(split) > 1:
                    add([sid], split, 0, "")

    seen, explanations = set(), []
    for p in candidates:
        key = (tuple(p["settlement_ids"]), tuple(p["bank_row_ids"]),
               p["deduction_paise"])
        if key in seen:
            continue
        seen.add(key)
        if RV.verify_proposal(view, p, item_records, claimed, strict=strict).accepted:
            explanations.append(p)
    return Enumeration(explanations=explanations, truncated=budget[0] < 0)


def solve(view: RV.WorldView, item_records: list[str], claimed,
          strict: bool = True) -> dict:
    """The no-LLM resolver: the unique verifiable explanation, else abstain."""
    e = enumerate_explanations(view, item_records, claimed, strict=strict)
    if len(e.explanations) == 1 and not e.truncated:
        return e.explanations[0]
    verdict = "exception" if not e.explanations and not e.truncated else "abstain"
    return {"verdict": verdict, "settlement_ids": [], "bank_row_ids": [],
            "deduction_paise": 0, "deduction_evidence": "",
            "reason": f"{len(e.explanations)} verifiable explanation(s)"
                      + (" (search truncated)" if e.truncated else "")}
