"""Measure the resolver on held-out worlds: engine only vs engine + agent.

    python -m agent.eval_resolve --seeds 1001-1005 (--replay | --resume | --record)
        [--report reports/agent_eval] [--worlds data/holdout]
    python -m agent.eval_resolve --seeds 1000,1006 --replay --report reports/agent_eval_dev
    python -m agent.eval_resolve --fingerprint     # hashes for the pre-registration

Grading unit: one golden row per settlement and per bank credit (debits are
out of scope), as in `recon.benchmark`. The system's final state of a record:

  auto match   engine match at confidence exact/high/medium, or a
               verifier-accepted agent proposal (the agent's matches supersede
               the engine's queue items; they never touch engine matches)
  auto non-settlement  the engine classed the credit as not a settlement
               (such credits never reach the human queue)
  human        everything else (the exception queue)

Categories per row:
  auto-resolved correctly   auto match whose group (settlements + credits)
                            equals the golden group, or a correct
                            non-settlement class
  auto-resolved wrongly     any other auto outcome
  correctly abstained       left for a human and the golden answer is not an
                            auto resolution (abstain, exception, duplicate)
  wrongly abstained         left for a human although the golden answer is a
                            match or a non-settlement class

Golden groups are connected components over counterparty links where BOTH
ends are matched-family rows (a duplicate credit points at its settlement
but is not part of that match). A baseline match whose amounts differ
without an explanation counts as wrong: it closed money it could not explain.

Proposal metrics: submitted match proposals the verifier rejected, and
accepted proposals that are wrong against the golden file. Cost per item is
the list price of the recorded token usage (what Anthropic bills; a Gemini
free-tier run billed $0); latency is the recorded wall clock of the live calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
from collections import Counter, defaultdict

from recon import io_load
from recon import schemas as RS
from recon.baselines import naive_reconcile, utr_then_amount

from agent import resolve as R
from agent import resolve_verifier as RV
from agent import solver
from agent.pricing import Budget

WORLDS_ROOT = os.path.join("data", "holdout")
CATEGORIES = ("auto_correct", "auto_wrong", "abstain_correct", "abstain_wrong")
PREREG_JSON = os.path.join("reports", "preregistration.json")


def parse_seeds(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


# --- golden -----------------------------------------------------------------------------

def golden_rows(world_dir: str) -> list[dict]:
    return [r for r in io_load.load_golden(world_dir, "A")
            if r["record_type"] in (RS.RT_SETTLEMENT, RS.RT_BANK_CREDIT)]


def golden_groups(rows: list[dict]) -> dict[str, frozenset[str]]:
    matched = {r["record_id"] for r in rows
               if r["expected_disposition"] in RS.MATCHED_FAMILY}
    parent = {m: m for m in matched}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in rows:
        if r["record_id"] not in matched:
            continue
        for other in r["counterparty_set"]:
            if other in matched:
                ra, rb = find(r["record_id"]), find(other)
                if ra != rb:
                    parent[ra] = rb
    comps: dict[str, set[str]] = defaultdict(set)
    for m in matched:
        comps[find(m)].add(m)
    return {m: frozenset(comps[find(m)]) for m in matched}


def case_groups(rows: list[dict]) -> dict[str, str]:
    """record -> case id: components over ALL counterparty links (a case is
    one scenario instance: a twin pair plus its credit, a 3-way merge...)."""
    ids = {r["record_id"] for r in rows}
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in rows:
        for other in r["counterparty_set"]:
            if other in ids:
                a, b = find(r["record_id"]), find(other)
                if a != b:
                    parent[a] = b
    return {i: find(i) for i in ids}


# --- system state ------------------------------------------------------------------------

def _state_from_decisions(decisions: list[RS.Decision], source: str,
                          auto_conf=R.AUTO_CONFIDENCE) -> dict[str, dict]:
    state: dict[str, dict] = {}
    for d in decisions:
        if d.status == RS.OUT_OF_SCOPE:
            continue
        records = list(d.settlement_ids) + list(d.bank_txn_ids)
        if d.status in RS.MATCHED_FAMILY and d.confidence in auto_conf:
            disc = d.discrepancy_paise or 0
            explained = disc == 0 or bool(d.discrepancy_breakdown and all(
                b.get("label") != "unexplained" for b in d.discrepancy_breakdown))
            entry = {"kind": "match", "group": frozenset(records), "source": source,
                     "tier": d.confidence, "explained": explained}
        elif d.status == RS.NON_SETTLEMENT_CREDIT:
            entry = {"kind": "nsc", "source": source}
        else:
            entry = {"kind": "human", "source": source, "status": d.status}
        for r in records:
            state.setdefault(r, entry)
    return state


def _apply_proposals(state: dict[str, dict], accepted: list[dict], source: str) -> dict:
    out = dict(state)
    for p in accepted:
        group = frozenset(p["settlement_ids"]) | frozenset(p["bank_row_ids"])
        for r in group:
            out[r] = {"kind": "match", "group": group, "source": source,
                      "tier": "agent_verified", "explained": True}
    return out


def categorize(row: dict, state: dict[str, dict], groups: dict[str, frozenset]) -> str:
    rid, exp = row["record_id"], row["expected_disposition"]
    st = state.get(rid, {"kind": "human"})
    golden_auto = exp in RS.MATCHED_FAMILY or exp == RS.NON_SETTLEMENT_CREDIT
    if st["kind"] == "match":
        ok = (exp in RS.MATCHED_FAMILY and st["group"] == groups.get(rid)
              and st.get("explained", True))
        return "auto_correct" if ok else "auto_wrong"
    if st["kind"] == "nsc":
        return "auto_correct" if exp == RS.NON_SETTLEMENT_CREDIT else "auto_wrong"
    return "abstain_wrong" if golden_auto else "abstain_correct"


def proposal_is_wrong(p: dict, rows_by_id: dict[str, dict],
                      groups: dict[str, frozenset]) -> bool:
    group = frozenset(p["settlement_ids"]) | frozenset(p["bank_row_ids"])
    for r in group:
        row = rows_by_id.get(r)
        if row is None or row["expected_disposition"] not in RS.MATCHED_FAMILY:
            return True
        if groups.get(r) != group:
            return True
    return False


# --- one world ------------------------------------------------------------------------------

def _counts(rows, state, groups, case_tags) -> dict:
    per: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        tag = row["scenario_tag"]
        bucket = tag if tag in case_tags else "_filler"
        per[bucket][categorize(row, state, groups)] += 1
    return {k: dict(v) for k, v in per.items()}


def evaluate_world(world_dir: str, mode: str, case_tags: set[str],
                   transcripts_root: str = R.TRANSCRIPTS_ROOT, budget=None,
                   transport_factory=None) -> dict:
    name = os.path.basename(os.path.normpath(world_dir))
    rows = golden_rows(world_dir)
    rows_by_id = {r["record_id"]: r for r in rows}
    groups = golden_groups(rows)
    cases = case_groups(rows)

    wr = R.run_world(world_dir, mode, os.path.join(transcripts_root, name),
                     budget=budget, transport_factory=transport_factory)
    engine = _state_from_decisions(wr.snapshot.decisions, "engine")
    items = {i.item_id: i for i in wr.snapshot.items}

    verdicts = R.verify_world(world_dir, wr, strict=True)
    verdicts_min = R.verify_world(world_dir, wr, strict=False)
    accepted = [r.proposal for r in wr.runs
                if r.item_id in verdicts and verdicts[r.item_id].accepted]
    accepted_min = [r.proposal for r in wr.runs
                    if r.item_id in verdicts_min and verdicts_min[r.item_id].accepted]
    with_agent = _apply_proposals(engine, accepted, "agent")
    with_agent_min = _apply_proposals(engine, accepted_min, "agent")

    # no-LLM ablation: the unique verifiable explanation per item, same batch rule
    view = RV.load_world_view(world_dir)
    solver_props = []
    for item in wr.snapshot.items:
        p = solver.solve(view, item.records, wr.snapshot.claimed)
        if p["verdict"] == "match":
            solver_props.append((item.records, p))
    solver_verdicts = RV.verify_batch(view, solver_props, wr.snapshot.claimed)
    with_solver = _apply_proposals(
        engine, [p for (_, p), v in zip(solver_props, solver_verdicts) if v.accepted],
        "solver")

    settlements = io_load.load_settlements(world_dir)
    bank_rows = io_load.load_bank_rows(world_dir)
    baselines = {
        "naive": _state_from_decisions(naive_reconcile(settlements, bank_rows), "naive",
                                       auto_conf=(RS.CONF_EXACT, RS.CONF_HIGH, RS.CONF_MEDIUM)),
        "utr_then_amount": _state_from_decisions(utr_then_amount(settlements, bank_rows),
                                                 "utr_then_amount"),
    }

    item_rows = []
    for run in wr.runs:
        item = items[run.item_id]
        anchor_row = rows_by_id.get(item.anchor, {})
        v = verdicts.get(run.item_id)
        p = run.proposal
        entry = {
            "world": name, "item_id": run.item_id, "outcome": run.outcome,
            "engine_status": item.status,
            "scenario": anchor_row.get("scenario_tag", ""),
            "anchor_amount_paise": _anchor_amount(view, item.anchor),
            "transcript": run.transcript, "api_calls": run.api_calls,
            "cost_usd": run.cost_usd, "latency_s": run.latency_s,
            "proposal": p,
            "verdict": v.to_dict() if v else None,
            "verdict_brief_minimum": (verdicts_min[run.item_id].to_dict()
                                      if run.item_id in verdicts_min else None),
            "golden": {r: {"disposition": rows_by_id[r]["expected_disposition"],
                           "group": sorted(groups.get(r, []))}
                       for r in item.records if r in rows_by_id},
        }
        if p and p.get("verdict") == "match":
            entry["proposal_wrong"] = proposal_is_wrong(p, rows_by_id, groups)
        item_rows.append(entry)

    # case rows the combined system still leaves for a human although the
    # golden answer resolves them, with the item that covered each one
    run_of = {}
    for run in wr.runs:
        for r in items[run.item_id].records:
            run_of.setdefault(r, run)
    left_for_human = []
    for row in rows:
        if (row["scenario_tag"] not in case_tags
                or categorize(row, with_agent, groups) != "abstain_wrong"):
            continue
        run = run_of.get(row["record_id"])
        left_for_human.append({
            "world": name, "record_id": row["record_id"],
            "scenario": row["scenario_tag"], "expected": row["expected_disposition"],
            "amount_paise": _anchor_amount(view, row["record_id"]),
            "item_id": run.item_id if run else "",
            "item_outcome": run.outcome if run else "not_queued",
            "transcript": run.transcript if run else ""})

    engine_false = Counter(
        engine[r["record_id"]].get("tier", "nsc")
        for r in rows
        if categorize(r, engine, groups) == "auto_wrong")

    return {
        "world": name,
        "stopped": wr.stopped,
        "rows": len(rows),
        "case_rows": sum(1 for r in rows if r["scenario_tag"] in case_tags),
        "cases": len({cases[r["record_id"]] for r in rows
                      if r["scenario_tag"] in case_tags}),
        "engine": _counts(rows, engine, groups, case_tags),
        "engine_agent": _counts(rows, with_agent, groups, case_tags),
        "engine_agent_brief_minimum": _counts(rows, with_agent_min, groups, case_tags),
        "engine_solver": _counts(rows, with_solver, groups, case_tags),
        "baselines": {k: _counts(rows, st, groups, case_tags)
                      for k, st in baselines.items()},
        "engine_false_matches_by_tier": dict(engine_false),
        "items": item_rows,
        "left_for_human": left_for_human,
    }


def _anchor_amount(view: RV.WorldView, rid: str) -> int:
    if rid in view.settlements:
        return view.settlements[rid]["net"]
    if rid in view.credits:
        return view.credits[rid]["amount"]
    return 0


# --- aggregation and the report ----------------------------------------------------------------

def _sum_counts(blocks: list[dict], bucket_filter) -> dict:
    total: Counter = Counter()
    for b in blocks:
        for bucket, c in b.items():
            if bucket_filter(bucket):
                total.update(c)
    return {k: total.get(k, 0) for k in CATEGORIES}


def _proposal_stats(items: list[dict], verdict_key: str = "verdict") -> dict:
    submitted = [i for i in items if i["proposal"] and i["proposal"].get("verdict") == "match"]
    rejected = [i for i in submitted if not (i[verdict_key] or {}).get("accepted")]
    wrong_acc = [i for i in submitted
                 if (i[verdict_key] or {}).get("accepted") and i.get("proposal_wrong")]
    live = [i for i in items if i["api_calls"]]
    return {
        "match_proposals": len(submitted),
        "rejected_by_verifier": len(rejected),
        "wrong_accepted": len(wrong_acc),
        "items_sent_to_agent": len(live),
        "cost_per_item_usd": (round(sum(i["cost_usd"] for i in live) / len(live), 5)
                              if live else None),
        "median_latency_s": (round(statistics.median(i["latency_s"] for i in live), 2)
                             if live else None),
        "total_cost_usd": round(sum(i["cost_usd"] for i in live), 4),
        "api_calls": sum(i["api_calls"] for i in items),
    }


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def aggregate(worlds: list[dict], case_tags: set[str]) -> dict:
    def pick(key):
        return [w[key] for w in worlds]

    is_case = lambda b: b in case_tags          # noqa: E731
    scenarios = sorted({b for w in worlds for b in w["engine"] if b in case_tags})
    items = [i for w in worlds for i in w["items"]]
    out = {
        "total": {
            "engine": _sum_counts(pick("engine"), is_case),
            "engine_agent": _sum_counts(pick("engine_agent"), is_case),
            "engine_agent_brief_minimum": _sum_counts(pick("engine_agent_brief_minimum"), is_case),
            "engine_solver": _sum_counts(pick("engine_solver"), is_case),
            "baselines": {b: _sum_counts([w["baselines"][b] for w in worlds], is_case)
                          for b in ("naive", "utr_then_amount")},
            "proposals": _proposal_stats(items),
            "proposals_brief_minimum": _proposal_stats(items, "verdict_brief_minimum"),
        },
        "filler": {
            "engine": _sum_counts(pick("engine"), lambda b: not is_case(b)),
            "engine_agent": _sum_counts(pick("engine_agent"), lambda b: not is_case(b)),
        },
        "per_scenario": {},
        "engine_false_matches_by_tier": dict(sum(
            (Counter(w["engine_false_matches_by_tier"]) for w in worlds), Counter())),
        "rows": sum(w["case_rows"] for w in worlds),
        "cases": sum(w["cases"] for w in worlds),
        "stopped": [w["world"] for w in worlds if w["stopped"]],
    }
    for sc in scenarios:
        only = lambda b, sc=sc: b == sc          # noqa: E731
        sc_items = [i for i in items if i["scenario"] == sc]
        out["per_scenario"][sc] = {
            "engine": _sum_counts(pick("engine"), only),
            "engine_agent": _sum_counts(pick("engine_agent"), only),
            "proposals": _proposal_stats(sc_items),
        }
    t = out["total"]
    for col in ("engine", "engine_agent"):
        c = t[col]
        n = sum(c.values())
        t[col + "_intervals"] = {
            "auto_correct_rate": wilson(c["auto_correct"], n),
            "auto_wrong_rate": wilson(c["auto_wrong"], n),
        }
    return out


LEFT_REASONS = {
    "prefilter_skip": "never sent to the agent: the enumerator found no explanation "
                      "the verifier could accept",
    "dedupe_skip": "never sent to the agent: one of its records was in an earlier "
                   "proposal",
    "not_queued": "the engine did not put it in the queue",
}


def worst_failures(items: list[dict], notes: dict[str, str], n: int = 10,
                   left: list[dict] = ()) -> list[dict]:
    """Agent errors first (accepted wrong, caught wrong, right but rejected,
    a golden match the agent did not propose), then case rows the combined
    system still leaves for a human although the golden file resolves them;
    within a kind, by amount."""
    ranked = []
    for i in items:
        p, v = i["proposal"], i["verdict"] or {}
        golden_match = any(g["disposition"] in RS.MATCHED_FAMILY
                           for g in i["golden"].values())
        proposed = bool(p) and p.get("verdict") == "match"
        if proposed and v.get("accepted") and i.get("proposal_wrong"):
            kind, sev, why = "accepted_wrong", 1, "verifier accepted a match the golden file contradicts"
        elif proposed and not v.get("accepted") and i.get("proposal_wrong"):
            kind, sev, why = "wrong_proposal_caught", 2, "wrong match proposed; verifier rejected it (" + ", ".join(v.get("codes", [])) + ")"
        elif proposed and not v.get("accepted"):
            kind, sev, why = "right_proposal_rejected", 3, "the golden match was proposed but rejected (" + ", ".join(v.get("codes", [])) + ")"
        elif proposed:
            continue                  # accepted and right: not a failure
        elif golden_match and i["outcome"] in ("submitted", "no_submit", "max_calls", "refusal", "api_error"):
            verdict = (p or {}).get("verdict", i["outcome"])
            kind, sev, why = "missed_match", 4, f"golden answer is a match; the agent returned {verdict}"
        else:
            continue
        ranked.append({"world": i["world"], "item_id": i["item_id"],
                       "scenario": i["scenario"], "kind": kind, "severity": sev,
                       "amount_paise": i["anchor_amount_paise"],
                       "transcript": i["transcript"], "why": why,
                       "analyst_note": notes.get(f"{i['world']}/{i['item_id']}", "")})
    listed = {(r["world"], r["item_id"]) for r in ranked}
    by_item: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in left:                  # one entry per queue item, not per row
        by_item[(row["world"], row["item_id"] or row["record_id"])].append(row)
    for (world, key), rows in by_item.items():
        if (world, key) in listed:
            continue                  # already listed as an agent error
        top = max(rows, key=lambda r: r["amount_paise"])
        why = LEFT_REASONS.get(top["item_outcome"],
                               f"left for a human (item outcome: {top['item_outcome']})")
        if len(rows) > 1:
            why += f" ({len(rows)} golden rows)"
        ranked.append({"world": world, "item_id": key, "scenario": top["scenario"],
                       "kind": "left_for_human", "severity": 5,
                       "amount_paise": top["amount_paise"],
                       "transcript": top["transcript"] or "-", "why": why,
                       "analyst_note": notes.get(f"{world}/{key}", "")})
    ranked.sort(key=lambda r: (r["severity"], -r["amount_paise"], r["world"], r["item_id"]))
    return ranked[:n]


def _fmt_cost(p: dict, free_tier: bool = False) -> str:
    if p["cost_per_item_usd"] is None:
        return "n/a (no live calls)"
    billed = "list price ($0 billed, free tier)" if free_tier else "billed"
    return f"${p['cost_per_item_usd']:.4f} {billed} · {p['median_latency_s']} s"


def table(engine: dict, agent: dict, props: dict, free_tier: bool = False) -> list[str]:
    return [
        "| Metric | Engine only | Engine + agent |",
        "|---|---|---|",
        f"| Auto-resolved correctly | {engine['auto_correct']} | {agent['auto_correct']} |",
        f"| Auto-resolved wrongly | {engine['auto_wrong']} | {agent['auto_wrong']} |",
        f"| Correctly abstained | {engine['abstain_correct']} | {agent['abstain_correct']} |",
        f"| Wrongly abstained (left for human) | {engine['abstain_wrong']} | {agent['abstain_wrong']} |",
        f"| Agent proposals rejected by verifier | n/a | {props['rejected_by_verifier']} |",
        f"| **Wrong proposals the verifier accepted** | n/a | **{props['wrong_accepted']}** |",
        f"| Cost per item (USD), median latency (s) | n/a | {_fmt_cost(props, free_tier)} |",
    ]


def render_report(results: dict, title: str) -> str:
    agg = results["aggregate"]
    t = agg["total"]
    lines = [f"# {title}", ""]
    if agg["stopped"]:
        lines += [f"> **INCOMPLETE**: the run stopped early in {agg['stopped']} "
                  "(budget, daily quota or provider outage). Numbers cover the items "
                  "processed.", ""]
    meta = results["meta"]
    s = meta.get("settings") or {}
    free = meta["model"].startswith("gemini")
    rows_kind = "held-out" if meta["role"] == "heldout" else "dev"
    lines += [
        f"Worlds: {', '.join(meta['worlds'])} · role: {meta['role']} · model: "
        f"`{meta['model']}` (thinking level {', '.join(s.get('thinking_level', [])) or 'n/a'}, "
        f"function calling {', '.join(s.get('fc_mode', [])) or 'n/a'}) · {agg['rows']} "
        f"{rows_kind} case rows in {agg['cases']} cases "
        f"(settlement and bank-credit rows; clean filler reported separately).",
        "",
        f"Prompt/tools sha256 `{meta['prompt_sha256'][:16]}` · verifier "
        f"`{meta['hashes']['resolve_verifier.py'][:16]}` · holdout "
        f"`{meta['hashes'].get('holdout.py', 'n/a')[:16]}` · matches pre-registration: "
        f"**{meta['matches_preregistration']}**.",
        "",
        f"## Engine only vs engine + agent (all {rows_kind} case rows)",
        "",
        *table(t["engine"], t["engine_agent"], t["proposals"], free),
        "",
        "Units: the first four rows count golden rows (one per settlement and per "
        "bank credit); the proposal rows count agent proposals. Wilson 95% intervals "
        f"for the auto-resolved-wrongly rate: engine {t['engine_intervals']['auto_wrong_rate']}, "
        f"engine + agent {t['engine_agent_intervals']['auto_wrong_rate']}.",
        "",
        "## Per scenario",
        "",
    ]
    for sc, block in sorted(agg["per_scenario"].items()):
        lines += [f"### `{sc}`", "", *table(block["engine"], block["engine_agent"],
                                            block["proposals"], free), ""]
    fb = agg["filler"]
    lines += [
        "## Clean filler (sanity, not part of the totals)",
        "",
        f"Engine only: {fb['engine']} · engine + agent: {fb['engine_agent']}",
        "",
        "## What the LLM adds, and what the stricter verifier buys",
        "",
        "| Column | Auto correct | Auto wrong | Correctly abstained | Wrongly abstained |",
        "|---|---|---|---|---|",
    ]
    for label, c in (("Engine only", t["engine"]),
                     ("Engine + deterministic solver (no LLM)", t["engine_solver"]),
                     ("Engine + agent (strict verifier, the result)", t["engine_agent"]),
                     ("Engine + agent (brief-minimum verifier)", t["engine_agent_brief_minimum"]),
                     ("Baseline: naive (amount within Rs 1, 3 days)", t["baselines"]["naive"]),
                     ("Baseline: exact UTR, then exact amount", t["baselines"]["utr_then_amount"])):
        lines.append(f"| {label} | {c['auto_correct']} | {c['auto_wrong']} | "
                     f"{c['abstain_correct']} | {c['abstain_wrong']} |")
    pm = t["proposals_brief_minimum"]
    lines += [
        "",
        f"With the brief-minimum verifier the same proposals would give "
        f"{pm['wrong_accepted']} wrong accepted and {pm['rejected_by_verifier']} rejected "
        f"(strict: {t['proposals']['wrong_accepted']} and {t['proposals']['rejected_by_verifier']}).",
        "",
        f"Engine false matches on held-out rows, by confidence tier: "
        f"{agg['engine_false_matches_by_tier'] or 'none'}.",
        "",
        "## The 10 worst failures",
        "",
        "Agent errors first (a wrong match accepted, a wrong or right match rejected, a "
        "golden match not proposed), then queue items whose rows the combined system "
        "still leaves for a human although the golden file resolves them; by amount "
        "within each kind.",
        "",
        "| # | world | item | scenario | kind | amount (paise) | transcript | why |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for k, f in enumerate(results["worst_failures"], 1):
        why = f["why"] + (f" — {f['analyst_note']}" if f["analyst_note"] else "")
        lines.append(f"| {k} | {f['world']} | `{f['item_id']}` | {f['scenario']} | "
                     f"{f['kind']} | {f['amount_paise']:,} | `{f['transcript']}` | {why} |")
    if not results["worst_failures"]:
        lines.append("| - | - | - | - | none | - | - | - |")
    lines += ["", "## Notes", ""] + [f"- {n}" for n in meta["notes"]]
    return "\n".join(lines) + "\n"


# --- hashes / pre-registration -------------------------------------------------------------------

def _file_sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def fingerprints() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.dirname(here)
    files = {
        "resolve_verifier.py": os.path.join(here, "resolve_verifier.py"),
        "solver.py": os.path.join(here, "solver.py"),
        "eval_resolve.py": os.path.join(here, "eval_resolve.py"),
        "resolve.py": os.path.join(here, "resolve.py"),
        "holdout.py": os.path.join(src, "recon", "holdout.py"),
        "engine.py": os.path.join(src, "recon", "engine.py"),
    }
    out = {name: _file_sha(p) for name, p in files.items() if os.path.exists(p)}
    out["prompt"] = R.prompt_fingerprint()
    return out


SETTING_KEYS = ("model", "thinking_level", "fc_mode")


def run_settings(worlds: list[dict]) -> dict[str, list[str]]:
    """The provider settings the recorded transcripts actually used (from each
    transcript's meta line); a consistent run has one value per key."""
    seen: dict[str, set[str]] = {k: set() for k in SETTING_KEYS}
    for w in worlds:
        for i in w["items"]:
            if i["transcript"] and os.path.exists(i["transcript"]):
                with open(i["transcript"], encoding="utf-8") as f:
                    head = json.loads(f.readline())
                for k in SETTING_KEYS:
                    seen[k].add(str(head.get(k)))
    return {k: sorted(v) for k, v in seen.items()}


def _matches_prereg(hashes: dict, settings: dict[str, list[str]] | None = None,
                    path: str = PREREG_JSON) -> str:
    """Every hash and setting pinned in the pre-registration must hold."""
    if not os.path.exists(path):
        return "no pre-registration file"
    with open(path, encoding="utf-8") as f:
        pinned = json.load(f)
    diffs = [k for k, v in sorted(pinned.get("hashes", {}).items()) if hashes.get(k) != v]
    for k, v in sorted((pinned.get("settings") or {}).items()):
        used = (settings or {}).get(k)
        if used and used != [str(v)]:
            diffs.append(k)
    return "yes" if not diffs else "NO (changed: " + ", ".join(diffs) + ")"


PART1_KEYS = ("resolve_verifier.py", "solver.py", "eval_resolve.py", "holdout.py", "engine.py")
PART2_KEYS = ("prompt", "resolve.py")
AMENDABLE = ("eval_resolve.py", "solver.py")   # never the verifier, the holdout set or the engine
LATE_AMENDABLE = ("eval_resolve.py",)          # after part 2: the grading/report code only
HELDOUT_REPORT_JSON = os.path.join("reports", "agent_eval.json")


def aggregate_sha(results: dict) -> str:
    """Fingerprint of every number in a report (its aggregate block)."""
    blob = json.dumps(results["aggregate"], sort_keys=True, default=list)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _report_aggregate_sha(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return aggregate_sha(json.load(f))


def dev_run_settings(transcripts_root: str = R.TRANSCRIPTS_ROOT) -> dict[str, str]:
    """The provider settings the latest dev run used, read from the meta line
    of every dev transcript. Refuses a dev run that mixed settings."""
    from recon.holdout import DEV_SEEDS
    seen: dict[str, set[str]] = {k: set() for k in SETTING_KEYS}
    for seed in DEV_SEEDS:
        folder = os.path.join(transcripts_root, str(seed))
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if name.endswith(".jsonl"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    head = json.loads(f.readline())
                for k in SETTING_KEYS:
                    seen[k].add(str(head.get(k)))
    if not seen["model"]:
        raise SystemExit("no dev transcripts to take the settings from")
    mixed = {k: sorted(v) for k, v in seen.items() if len(v) != 1}
    if mixed:
        raise SystemExit(f"the dev transcripts mix settings: {mixed}")
    return {k: v.pop() for k, v in seen.items()}


def pin(part: str, path: str = PREREG_JSON, now: str | None = None,
        reason: str = "", transcripts_root: str = R.TRANSCRIPTS_ROOT,
        heldout_report_json: str = HELDOUT_REPORT_JSON) -> dict:
    """Pin the pre-registration. Part 1 (verifier, solver, grading, holdout
    generator, engine) before any resolver run; part 2 (prompt and tools,
    resolve.py, and the provider settings the final dev run used) after
    dev-seed prompt work and before the held-out run. Each part is pinned
    once. Between the two, `amend` re-pins a changed part-1 file with a
    recorded reason; only the grading code and the solver can be amended.
    After part 2 only the grading/report code can, and the amendment records
    the fingerprint of the held-out report's numbers from before the change,
    so the next report states whether any number moved."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hashes = fingerprints()
    doc: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    if part == "amend":
        if not doc.get("part1"):
            raise SystemExit("nothing to amend: part 1 is not pinned")
        if not reason.strip():
            raise SystemExit("an amendment needs a reason")
        late = bool(doc.get("part2"))
        keys = PART1_KEYS + (PART2_KEYS if late else ())
        changed = [k for k in keys if hashes.get(k) != doc["hashes"].get(k)]
        if not changed:
            raise SystemExit("nothing changed since it was pinned")
        forbidden = [k for k in changed if k not in (LATE_AMENDABLE if late else AMENDABLE)]
        if forbidden:
            raise SystemExit(f"{forbidden} can never be amended"
                             f"{' after part 2' if late else ''}; revert the change")
        entry = {"at": now, "files": changed, "reason": reason.strip(),
                 "previous": {k: doc["hashes"][k] for k in changed}}
        if late:
            entry["after_part2"] = True
            entry["heldout_aggregate_sha256"] = _report_aggregate_sha(heldout_report_json)
        doc.setdefault("amendments", []).append(entry)
        doc["hashes"].update({k: hashes[k] for k in changed})
    elif part == "1":
        if doc.get("part1"):
            raise SystemExit("part 1 is already pinned")
        missing = [k for k in PART1_KEYS if k not in hashes]
        if missing:
            raise SystemExit(f"cannot pin part 1, missing: {missing}")
        from recon.holdout import DEV_SEEDS, HELDOUT_SEEDS
        doc = {"part1": {"pinned_at": now, "keys": list(PART1_KEYS),
                         "dev_seeds": list(DEV_SEEDS), "heldout_seeds": list(HELDOUT_SEEDS)},
               "hashes": {k: hashes[k] for k in PART1_KEYS}}
    elif part == "2":
        if not doc.get("part1"):
            raise SystemExit("pin part 1 first")
        if doc.get("part2"):
            raise SystemExit("part 2 is already pinned")
        changed = [k for k, v in sorted(doc["hashes"].items()) if hashes.get(k) != v]
        if changed:
            raise SystemExit(f"part 1 changed since it was pinned: {changed}")
        doc["hashes"].update({k: hashes[k] for k in PART2_KEYS})
        doc["settings"] = dev_run_settings(transcripts_root)
        doc["part2"] = {"pinned_at": now, "keys": list(PART2_KEYS) + ["settings"]}
    else:
        raise SystemExit(f"unknown part {part!r}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    return doc


def run(seeds: list[int], mode: str, report: str, worlds_root: str = WORLDS_ROOT,
        role: str = "heldout", notes_path: str | None = None) -> dict:
    try:
        from recon.holdout import CASE_TAGS
        case_tags = set(CASE_TAGS)
    except ImportError:              # pragma: no cover - holdout not installed
        case_tags = set()
    budget = Budget()
    worlds = [evaluate_world(os.path.join(worlds_root, str(s)), mode, case_tags,
                             budget=budget) for s in seeds]
    notes = {}
    if notes_path and os.path.exists(notes_path):
        with open(notes_path, encoding="utf-8") as f:
            notes = json.load(f)
    hashes = fingerprints()
    settings = run_settings(worlds)
    model = ", ".join(settings["model"]) or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    if settings["model"] and all(m.startswith("gemini") for m in settings["model"]):
        cost_note = ("Cost is the paid list price of the recorded token usage; the Gemini "
                     "free tier billed $0.")
    else:
        cost_note = ("Cost is the list price of the recorded token usage, which is what the "
                     "provider billed (prompt caching included).")
    amendments = []
    if os.path.exists(PREREG_JSON):
        with open(PREREG_JSON, encoding="utf-8") as f:
            amendments = json.load(f).get("amendments", [])
    agg = aggregate(worlds, case_tags)
    amend_notes = []
    for a in amendments:
        note = (f"Pre-registration amended {a['at']} ({', '.join(a['files'])}"
                f"{', after part 2' if a.get('after_part2') else ''}): {a['reason']}")
        before = a.get("heldout_aggregate_sha256")
        if before and role == "heldout":
            same = aggregate_sha({"aggregate": agg}) == before
            note += (" Every held-out number is unchanged by this amendment: "
                     f"{'yes' if same else 'NO'} (fingerprint of the numbers before and after).")
        amend_notes.append(note)
    results = {
        "meta": {"worlds": [w["world"] for w in worlds], "role": role, "model": model,
                 "settings": settings,
                 "prompt_sha256": hashes["prompt"], "hashes": hashes,
                 "matches_preregistration": _matches_prereg(hashes, settings),
                 "mode": mode,
                 "notes": [
                     "Grading reads the golden files; the agent never does (its tools read "
                     "an allowlisted copy of settlements, bank statement and merchant "
                     "sidecar).",
                     "Prompt and tool work happened on the dev seeds only (family A "
                     "templates); the held-out seeds use family B and were run once.",
                     "The held-out set was checked by a separate reviewer agent: answers "
                     "sound; one known structural tell (merged-group settlements are all "
                     "created on weekends) changes no answer. See "
                     "docs/provenance/holdout_review.md.",
                     cost_note,
                     *amend_notes,
                 ]},
        "aggregate": agg,
        "worlds": worlds,
    }
    items = [i for w in worlds for i in w["items"]]
    results["worst_failures"] = worst_failures(
        items, notes, left=[r for w in worlds for r in w["left_for_human"]])
    os.makedirs(os.path.dirname(os.path.abspath(report + ".json")), exist_ok=True)
    with open(report + ".json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(results, f, indent=1, sort_keys=True, default=list)
        f.write("\n")
    title = ("Agent evaluation — held-out worlds" if role == "heldout"
             else "Agent evaluation — dev worlds (prompt work happens here)")
    with open(report + ".md", "w", encoding="utf-8", newline="\n") as f:
        f.write(render_report(results, title))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", default="1001-1005")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true")
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--replay", action="store_true")
    ap.add_argument("--report", default=None)
    ap.add_argument("--worlds", default=WORLDS_ROOT)
    ap.add_argument("--notes", default=os.path.join("reports", "agent_eval_notes.json"))
    ap.add_argument("--fingerprint", action="store_true",
                    help="print the hashes to pin in the pre-registration and exit")
    ap.add_argument("--pin", choices=("1", "2", "amend"), default=None,
                    help="pin pre-registration part 1 (before any resolver run), "
                         "part 2 (before the held-out run), or amend a pinned file "
                         "(grading or solver before part 2, the grading/report code "
                         "only after it; with --reason) and exit")
    ap.add_argument("--reason", default="", help="why a pinned file is amended")
    args = ap.parse_args()
    if args.fingerprint:
        print(json.dumps(fingerprints(), indent=1, sort_keys=True))
        return
    if args.pin:
        print(json.dumps(pin(args.pin, reason=args.reason), indent=1, sort_keys=True))
        return
    seeds = parse_seeds(args.seeds)
    try:
        from recon.holdout import DEV_SEEDS
        role = "dev" if set(seeds) <= set(DEV_SEEDS) else "heldout"
    except ImportError:              # pragma: no cover
        role = "heldout"
    m = "record" if args.record else "resume" if args.resume else "replay"
    report = args.report or os.path.join(
        "reports", "agent_eval" if role == "heldout" else "agent_eval_dev")
    results = run(seeds, m, report, args.worlds, role, args.notes)
    t = results["aggregate"]["total"]
    print(f"{role} seeds {seeds}: engine {t['engine']} | engine+agent {t['engine_agent']} | "
          f"proposals {t['proposals']['match_proposals']} rejected "
          f"{t['proposals']['rejected_by_verifier']} wrong-accepted "
          f"{t['proposals']['wrong_accepted']}"
          + (f" | STOPPED {results['aggregate']['stopped']}" if results['aggregate']['stopped'] else ""))
    print(f"report: {report}.md / .json")


if __name__ == "__main__":
    main()
