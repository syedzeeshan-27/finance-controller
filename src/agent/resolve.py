"""The resolver agent: the LLM in the decision path, behind a verifier.

    python -m agent.resolve <world_dir> (--record | --resume | --replay)
        [--transcripts DIR]

For every item the FROZEN engine leaves for a human (missing bank credit,
missing settlement, ambiguous, duplicate, matched-with-unexplained-residual),
an agent with read-only tools proposes exactly one resolution as strict JSON:

    {verdict: match | exception | abstain, settlement_ids, bank_row_ids,
     deduction_paise, deduction_evidence, reason}

The proposal is judged afterwards by `agent.resolve_verifier`, which shares
no code with this module. The agent never sees the verdict and gets no
retry. Accepted matches are the only way the agent changes the books.

What the agent can see: an allowlisted copy of `settlements.csv`,
`bank_statement.csv` and `settlement_merchants.csv`, loaded once into memory.
Golden files are never copied, so no tool can read them. Records the engine
already matched with confidence are hidden: they cannot be proposed.

Order and dedupe: credit-side items first (value date, id), then
settlement-side (created date, id). An item whose records already appear in
an earlier item's match PROPOSAL is skipped (dedupe by proposal, never by
verdict, so replays do not depend on the verifier). Items for which no
verifiable explanation exists at all (`agent.solver`) are not sent to the
model (`prefilter_skip`).

Transcripts: one JSONL per item under `data/agent_transcripts/resolve/
<world>/`, replayable offline. `--resume` replays finished items and runs the
rest live, so a run interrupted by the free-tier daily quota continues the
next day with identical earlier state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta

from recon import io_load
from recon import schemas as RS
from recon.engine import reconcile_leg_a
from recon.normalize import parse_bank_date, parse_iso_date

from agent import resolve_verifier as RV
from agent import solver
from agent import tools as T
from agent.gemini import QuotaExhausted
from agent.loop import run_loop
from agent.pricing import Budget, BudgetExceeded, BudgetedTransport, call_cost_usd
from agent.provider import AgentError, ReplayTransport, RunStop, make_live_transport

ALLOWED_FILES = ("settlements.csv", "bank_statement.csv", "settlement_merchants.csv")
AUTO_CONFIDENCE = (RS.CONF_EXACT, RS.CONF_HIGH, RS.CONF_MEDIUM)
QUEUED_EXCEPTIONS = (RS.EXCEPTION_MISSING_BANK, RS.EXCEPTION_MISSING_SETTLEMENT,
                     RS.AMBIGUOUS_ABSTAIN, RS.DUPLICATE_CREDIT)
WINDOW_DAYS = 10
NEIGHBOUR_DAYS = 5
MAX_SEARCH_SPAN_DAYS = 31
MAX_ROWS = 40
MAX_CALLS = 4
MAX_TOKENS = 8000          # room for the model's reasoning as well as the answer
TRANSCRIPTS_ROOT = os.path.join("data", "agent_transcripts", "resolve")

SYSTEM = """\
You resolve bank-reconciliation exceptions for an Indian business that
receives Razorpay settlements into its current account. A deterministic
engine has already matched everything it could prove. The queue item you are
given is one it left for a human. Decide whether it can be resolved as a
match, is a genuine exception, or is ambiguous.

Data: settlements (id, net amount, UTR, created date, expected credit date,
merchant brand) and bank credits (txn id, value date, amount, narration,
reference). All amounts are integer paise (Rs 1 = 100 paise). Records the
engine already matched are not shown and cannot be used.

An independent checker verifies every match you propose, and rejects it
unless all of this holds:
- one bank credit is paired with one or more settlements, or one settlement
  with several bank credits;
- sum of the settlement nets minus deduction_paise equals the sum of the
  credit amounts, exactly;
- a deduction (bank charges, TDS, a refund or chargeback netted off) must be
  written in the credit's narration or reference as a single figure. Quote
  the exact characters containing it as deduction_evidence, and set
  deduction_paise to that figure in paise. A deduction that is the sum of
  several figures cannot be verified. With no deduction, use 0 and "";
- each credit's value date is 0 to 10 days after each paired settlement's
  created date;
- if another open settlement has the same amount (or another open credit has
  the same amount) and would fit equally well, the credit must name the one
  you choose: its settlement id, its UTR, or a merchant brand the other one
  does not have;
- a credit that names a different settlement, UTR or merchant contradicts
  the match.
A UTR in a narration may be damaged (characters missing, extra, swapped or
wrong); it can support a match when the amounts close exactly and nothing
else fits.

How to look for a match:
- a credit smaller than a settlement by a figure written in its narration is
  that settlement minus a deduction;
- one credit can pay several settlements at once. If no single open
  settlement fits a credit, look for a small group (two to six) of open
  settlements whose nets add up exactly to the credit, less any deduction its
  narration states. Add the paise amounts carefully;
- for a settlement with no credit, also consider larger credits that could
  pay it together with other open settlements;
- use the search tools when the first message does not show enough
  candidates.

Verdicts:
- "match": you found a match the checker can verify; list every settlement
  and bank credit id it involves;
- "exception": the evidence shows there is no counterpart (a settlement that
  never arrived, a credit that is not from a settlement, a duplicate posting);
- "abstain": a counterpart may exist but the evidence cannot single it out.
A wrong match is worse than leaving the item for a human. When unsure,
abstain.

You get one submission, no feedback and at most """ + str(MAX_CALLS) + """ calls; the last
allowed call can only be submit_resolution. The context in the first message is
usually enough; use the search tools only when it is not. Finish by calling
submit_resolution exactly once, with a one- or two-sentence reason."""

_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_PAISE = {"type": "integer", "description": "paise; 0 means no bound"}
_CONTAINS = {"type": "string", "description": "case-insensitive text filter; "
                                              "empty string means none"}

TOOLS = [
    T.strict_tool("get_queue_item", "The queue item under review, with the "
                  "engine's evidence and the candidates it rejected.", {}),
    T.strict_tool("find_settlements", "Open settlements created in a date range "
                  f"(at most {MAX_SEARCH_SPAN_DAYS} days), optionally filtered by "
                  "net amount and by text in the id, UTR or merchant brand. "
                  f"At most {MAX_ROWS} rows, oldest first.",
                  {"date_from": _DATE, "date_to": _DATE,
                   "min_amount_paise": _PAISE, "max_amount_paise": _PAISE,
                   "contains": _CONTAINS}),
    T.strict_tool("find_bank_credits", "Open bank credits with a value date in a "
                  f"range (at most {MAX_SEARCH_SPAN_DAYS} days), optionally "
                  "filtered by amount and by text in the narration or reference. "
                  f"At most {MAX_ROWS} rows, oldest first.",
                  {"date_from": _DATE, "date_to": _DATE,
                   "min_amount_paise": _PAISE, "max_amount_paise": _PAISE,
                   "contains": _CONTAINS}),
    T.strict_tool("get_narration", "The exact narration and reference of one bank "
                  "credit, character for character, and whether the engine "
                  "already matched it.",
                  {"txn_id": {"type": "string"}}),
    T.strict_tool("submit_resolution", "Submit your one resolution for this "
                  "queue item. Call exactly once.",
                  {"verdict": {"type": "string",
                               "enum": ["match", "exception", "abstain"]},
                   "settlement_ids": {"type": "array", "items": {"type": "string"}},
                   "bank_row_ids": {"type": "array", "items": {"type": "string"}},
                   "deduction_paise": {"type": "integer"},
                   "deduction_evidence": {"type": "string"},
                   "reason": {"type": "string"}}),
]


def prompt_fingerprint() -> str:
    """sha256 over the system prompt and tool schemas: pinned in the
    pre-registration before the held-out run."""
    blob = json.dumps({"system": SYSTEM, "tools": TOOLS, "max_calls": MAX_CALLS,
                       "max_tokens": MAX_TOKENS}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- the engine snapshot and the queue items ------------------------------------------

@dataclass
class Item:
    item_id: str
    side: str                     # "credit" | "settlement"
    status: str                   # engine status, or "needs_review"
    records: list[str]            # the engine decision's settlement + bank ids
    anchor: str
    anchor_date: date
    evidence: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)


@dataclass
class Snapshot:
    decisions: list[RS.Decision]
    claimed: frozenset[str]
    items: list[Item]


def engine_snapshot(world_dir: str) -> Snapshot:
    settlements = io_load.load_settlements(world_dir)
    bank_rows = io_load.load_bank_rows(world_dir)
    decisions = reconcile_leg_a(settlements, bank_rows)
    created = {s.settlement_id: parse_iso_date(s.created_at) for s in settlements}
    value = {r.txn_id: parse_bank_date(r.value_date) for r in bank_rows}

    claimed: set[str] = set()
    items: list[Item] = []
    for d in decisions:
        if d.status in RS.MATCHED_FAMILY and d.confidence in AUTO_CONFIDENCE:
            claimed.update(d.settlement_ids)
            claimed.update(d.bank_txn_ids)
            continue
        if d.status in RS.MATCHED_FAMILY:            # needs_review overlay
            status = "needs_review"
        elif d.kind == "exception" and d.status in QUEUED_EXCEPTIONS:
            status = d.status
        else:
            continue
        if d.bank_txn_ids:
            side, anchor = "credit", d.bank_txn_ids[0]
            anchor_date = value[anchor]
        else:
            side, anchor = "settlement", d.settlement_ids[0]
            anchor_date = created[anchor]
        items.append(Item(item_id=f"{side}:{anchor}", side=side, status=status,
                          records=list(d.settlement_ids) + list(d.bank_txn_ids),
                          anchor=anchor, anchor_date=anchor_date,
                          evidence=[{"rule": e.get("rule", ""),
                                     "detail": e.get("detail", "")}
                                    for e in d.evidence],
                          candidates=[{"id": c.get("record_id", ""),
                                       "amount_paise": c.get("amount_paise"),
                                       "note": c.get("reason", "")}
                                      for c in d.candidates]))
    items.sort(key=lambda i: (i.side != "credit", i.anchor_date, i.anchor))
    return Snapshot(decisions=decisions, claimed=frozenset(claimed), items=items)


# --- what the agent may see -----------------------------------------------------------

class AgentWorld:
    """The allowlisted, in-memory view the agent's tools read from."""

    def __init__(self, world_dir: str, claimed: frozenset[str]):
        tmp = tempfile.mkdtemp(prefix="agent_view_")
        try:
            for name in ALLOWED_FILES:
                src = os.path.join(world_dir, name)
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(tmp, name))
            settlements = io_load.load_settlements(tmp)
            bank_rows = io_load.load_bank_rows(tmp)
            merchants = {}
            side = os.path.join(tmp, "settlement_merchants.csv")
            if os.path.exists(side):
                with open(side, encoding="utf-8", newline="") as f:
                    merchants = {r["settlement_id"]: r["merchant_name"]
                                 for r in csv.DictReader(f)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.claimed = claimed
        self.settlements = {s.settlement_id: {
            "settlement_id": s.settlement_id, "net_paise": s.amount_paise,
            "utr": s.utr, "created_date": parse_iso_date(s.created_at).isoformat(),
            "expected_credit_date": parse_iso_date(s.settled_at).isoformat(),
            "merchant": merchants.get(s.settlement_id, "")} for s in settlements}
        self.credits = {r.txn_id: {
            "txn_id": r.txn_id, "value_date": parse_bank_date(r.value_date).isoformat(),
            "amount_paise": r.credit_paise, "narration": r.narration,
            "ref_no": r.ref_no} for r in bank_rows if r.credit_paise > 0}

    def open_settlements(self, lo: date, hi: date) -> list[dict]:
        rows = [s for sid, s in self.settlements.items()
                if sid not in self.claimed
                and lo.isoformat() <= s["created_date"] <= hi.isoformat()]
        return sorted(rows, key=lambda s: (s["created_date"], s["settlement_id"]))

    def open_credits(self, lo: date, hi: date) -> list[dict]:
        rows = [c for tid, c in self.credits.items()
                if tid not in self.claimed
                and lo.isoformat() <= c["value_date"] <= hi.isoformat()]
        return sorted(rows, key=lambda c: (c["value_date"], c["txn_id"]))


def _capped(rows: list[dict]) -> dict:
    return {"rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}


def item_payload(world: AgentWorld, item: Item) -> dict:
    records = {}
    for r in item.records:
        if r in world.settlements:
            records[r] = dict(world.settlements[r])
        elif r in world.credits:
            records[r] = dict(world.credits[r])
    return {"item_id": item.item_id, "engine_status": item.status,
            "records": records, "engine_evidence": item.evidence,
            "engine_rejected_candidates": item.candidates}


def first_message(world: AgentWorld, item: Item) -> str:
    a = item.anchor_date
    if item.side == "credit":
        settlements = world.open_settlements(a - timedelta(days=WINDOW_DAYS), a)
        credits = world.open_credits(a - timedelta(days=NEIGHBOUR_DAYS),
                                     a + timedelta(days=NEIGHBOUR_DAYS))
    else:
        settlements = world.open_settlements(a - timedelta(days=NEIGHBOUR_DAYS),
                                             a + timedelta(days=NEIGHBOUR_DAYS))
        credits = world.open_credits(a, a + timedelta(days=WINDOW_DAYS))
    payload = {
        "queue_item": item_payload(world, item),
        "context": {"open_settlements": _capped(settlements),
                    "open_bank_credits": _capped(credits)},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_impls(world: AgentWorld, item: Item, state: dict) -> dict:
    def _range(inp) -> tuple[date, date]:
        lo = date.fromisoformat(inp["date_from"])
        hi = date.fromisoformat(inp["date_to"])
        if hi < lo:
            lo, hi = hi, lo
        if (hi - lo).days > MAX_SEARCH_SPAN_DAYS:
            hi = lo + timedelta(days=MAX_SEARCH_SPAN_DAYS)
        return lo, hi

    def _amount_ok(value: int, inp) -> bool:
        lo, hi = inp["min_amount_paise"], inp["max_amount_paise"]
        return (not lo or value >= lo) and (not hi or value <= hi)

    def find_settlements(inp):
        lo, hi = _range(inp)
        needle = inp["contains"].strip().upper()
        rows = [s for s in world.open_settlements(lo, hi)
                if _amount_ok(s["net_paise"], inp)
                and (not needle or needle in f"{s['settlement_id']} {s['utr']} "
                                             f"{s['merchant']}".upper())]
        return _capped(rows)

    def find_bank_credits(inp):
        lo, hi = _range(inp)
        needle = inp["contains"].strip().upper()
        rows = [c for c in world.open_credits(lo, hi)
                if _amount_ok(c["amount_paise"], inp)
                and (not needle or needle in f"{c['narration']} {c['ref_no']}".upper())]
        return _capped(rows)

    def get_narration(inp):
        c = world.credits.get(inp["txn_id"])
        if c is None:
            return {"error": f"no bank credit {inp['txn_id']}"}
        return {**c, "already_matched_by_engine": inp["txn_id"] in world.claimed}

    def submit_resolution(inp):
        if state["proposal"] is None:
            state["proposal"] = dict(inp)
        return {"received": True}

    return {"get_queue_item": lambda _inp: item_payload(world, item),
            "find_settlements": find_settlements,
            "find_bank_credits": find_bank_credits,
            "get_narration": get_narration,
            "submit_resolution": submit_resolution}


# --- one item, one run --------------------------------------------------------------------

@dataclass
class ItemRun:
    item_id: str
    outcome: str      # submitted | no_submit | max_calls | refusal | api_error |
                      # prefilter_skip | dedupe_skip | budget_stop | quota_stop |
                      # provider_stop
    proposal: dict | None = None
    transcript: str = ""
    api_calls: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def run_item(world: AgentWorld, item: Item, transport) -> ItemRun:
    state = {"proposal": None}
    run = ItemRun(item_id=item.item_id, outcome="no_submit")
    try:
        out = run_loop(transport, system=SYSTEM, tools=TOOLS,
                       impls=build_impls(world, item, state),
                       user_content=first_message(world, item),
                       max_tokens=MAX_TOKENS, max_calls=MAX_CALLS,
                       stop_when=lambda: state["proposal"] is not None,
                       final_tool="submit_resolution")
        run.api_calls = out.api_calls
        run.outcome = "submitted" if state["proposal"] is not None else "no_submit"
    except RunStop:
        raise
    except AgentError as exc:
        msg = str(exc)
        run.error = msg
        run.outcome = ("max_calls" if "call budget" in msg
                       else "refusal" if "refusal" in msg or "max_tokens" in msg
                       else "api_error")
        if state["proposal"] is not None:
            run.outcome = "submitted"
    run.proposal = state["proposal"]
    return run


def transcript_stats(path: str) -> tuple[int, float, float]:
    """(api calls, list-price cost USD, summed latency s) from a transcript."""
    calls, cost, latency = 0, 0.0, 0.0
    model = ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("kind") == "meta":
                model = r.get("model", "")
                continue
            calls += 1
            cost += call_cost_usd(model, r["response"].get("usage") or {})
            latency += r.get("latency_s", 0.0)
    return calls, round(cost, 6), round(latency, 3)


# --- a whole world ---------------------------------------------------------------------------

def _safe(item_id: str) -> str:
    return item_id.replace(":", "__")


STOP_OUTCOMES = ("budget_stop", "quota_stop", "provider_stop")


def _stop_kind(exc: RunStop) -> str:
    if isinstance(exc, BudgetExceeded):
        return "budget"
    if isinstance(exc, QuotaExhausted):
        return "quota"
    return "provider"          # ProviderUnavailable: HTTP error or unreachable


@dataclass
class WorldRun:
    world: str
    snapshot: Snapshot
    runs: list[ItemRun]
    stopped: str = ""          # "", "budget", "quota", "provider"


def run_world(world_dir: str, mode: str, transcripts_dir: str | None = None,
              budget: Budget | None = None, only_items: set[str] | None = None,
              transport_factory=None) -> WorldRun:
    """mode: "record" (fresh live run), "resume" (replay finished items, live
    for the rest) or "replay" (offline only). `transport_factory(path, task)`
    replaces the live provider (tests)."""
    assert mode in ("record", "resume", "replay"), mode
    name = os.path.basename(os.path.normpath(world_dir))
    tdir = transcripts_dir or os.path.join(TRANSCRIPTS_ROOT, name)
    manifest_path = os.path.join(tdir, "manifest.json")
    done: dict[str, dict] = {}
    if mode == "record" and os.path.isdir(tdir):
        shutil.rmtree(tdir)
    if mode in ("resume", "replay") and os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            done = {r["item_id"]: r for r in json.load(f)["items"]}
    os.makedirs(tdir, exist_ok=True)

    snap = engine_snapshot(world_dir)
    world = AgentWorld(world_dir, snap.claimed)
    view = RV.load_world_view(world_dir)
    budget = budget or Budget()
    proposed: set[str] = set()
    runs: list[ItemRun] = []
    stopped = ""

    def save_manifest() -> None:
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"world": name, "prompt_sha256": prompt_fingerprint(),
                       "items": [r.to_dict() for r in runs]}, f, indent=1,
                      sort_keys=True)
            f.write("\n")

    for item in snap.items:
        if only_items is not None and item.item_id not in only_items:
            continue
        if stopped:
            runs.append(ItemRun(item.item_id, outcome=f"{stopped}_stop"))
            continue
        if any(r in proposed for r in item.records):
            runs.append(ItemRun(item.item_id, outcome="dedupe_skip"))
            continue
        if not solver.enumerate_explanations(view, item.records, snap.claimed).explanations:
            runs.append(ItemRun(item.item_id, outcome="prefilter_skip"))
            continue
        path = os.path.join(tdir, _safe(item.item_id) + ".jsonl")
        finished = (item.item_id in done
                    and done[item.item_id]["outcome"] not in STOP_OUTCOMES
                    and os.path.exists(path))
        if mode == "replay" or finished:
            if not os.path.exists(path):
                runs.append(ItemRun(item.item_id, outcome="api_error",
                                    error="no recorded transcript to replay"))
                continue
            transport = ReplayTransport(path)
        else:
            if os.path.exists(path):
                os.remove(path)            # a partial run: start this item over
            factory = transport_factory or make_live_transport
            transport = BudgetedTransport(
                factory(path, task=f"resolve {name} {item.item_id}"), budget)
        try:
            run = run_item(world, item, transport)
        except RunStop as exc:
            stopped = _stop_kind(exc)
            runs.append(ItemRun(item.item_id, outcome=f"{stopped}_stop",
                                error=str(exc)[:300]))
            if os.path.exists(path) and not isinstance(transport, ReplayTransport):
                os.remove(path)            # the item re-runs live on --resume
            save_manifest()
            continue
        run.transcript = os.path.relpath(path).replace(os.sep, "/")
        calls, cost, latency = transcript_stats(path)
        run.api_calls, run.cost_usd, run.latency_s = calls, cost, latency
        runs.append(run)
        if run.proposal and run.proposal.get("verdict") == "match":
            proposed.update(run.proposal.get("settlement_ids", []))
            proposed.update(run.proposal.get("bank_row_ids", []))
        if mode != "replay":
            save_manifest()
    if mode != "replay":
        save_manifest()
    return WorldRun(world=name, snapshot=snap, runs=runs, stopped=stopped)


def verify_world(world_dir: str, wr: WorldRun, strict: bool = True) -> dict[str, RV.Verdict]:
    """item_id -> verdict for every submitted proposal, verified as one batch."""
    view = RV.load_world_view(world_dir)
    items = {i.item_id: i for i in wr.snapshot.items}
    submitted = [r for r in wr.runs if r.proposal is not None]
    verdicts = RV.verify_batch(
        view, [(items[r.item_id].records, r.proposal) for r in submitted],
        wr.snapshot.claimed, strict=strict)
    return {r.item_id: v for r, v in zip(submitted, verdicts)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("world_dir")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true")
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--replay", action="store_true")
    ap.add_argument("--transcripts", default=None)
    args = ap.parse_args()
    m = "record" if args.record else "resume" if args.resume else "replay"
    wr = run_world(args.world_dir, m, args.transcripts)
    verdicts = verify_world(args.world_dir, wr)
    accepted = sum(1 for v in verdicts.values() if v.accepted)
    outcomes: dict[str, int] = {}
    for r in wr.runs:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    print(f"{wr.world}: {len(wr.snapshot.items)} queue items · outcomes "
          f"{dict(sorted(outcomes.items()))} · proposals accepted {accepted}"
          + (f" · STOPPED ({wr.stopped})" if wr.stopped else ""))


if __name__ == "__main__":
    main()
