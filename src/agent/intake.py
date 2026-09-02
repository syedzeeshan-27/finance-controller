"""Statement intake: an agent finds the structure, arithmetic proves it.

    python -m agent.intake <raw.xlsx|raw.csv> --out DIR
        (--record TRANSCRIPT | --replay TRANSCRIPT | --mapping MAPPING.json)
        [--report [PATH]]

Three ways to obtain the StatementMapping:
- --record   live agent run (needs ANTHROPIC_API_KEY); records a transcript
- --replay   re-runs a recorded transcript byte-for-byte, offline
- --mapping  no agent at all: use a hand-written mapping

Whatever the source, the SAME deterministic validator must accept the
mapping before anything is written, the canonical output is re-loaded
through the untouched production loader, and the balance chain is proven a
second time from the typed rows. The agent finds structure; it never gets
to assert correctness.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass

from recon import io_load
from recon.engine import reconcile_leg_a

from agent import report as report_mod
from agent import tools as T
from agent.loop import run_loop
from agent.mapping import MAPPING_SCHEMA, StatementMapping
from agent.provider import AgentError, LiveTransport, ReplayTransport
from agent.rawgrid import load_grid
from agent.validator import ValidationReport, validate_mapping
from agent.apply import apply_mapping, write_world

MAX_REJECTIONS = 5
MAX_CALLS = 12
_PREVIEW_ROWS = 25
_MAX_ERRORS_FED_BACK = 12

_SYSTEM = """\
You are mapping a messy real-world bank statement export onto a canonical
schema. You do NOT decide what any transaction means and you do NOT compute
balances — you only locate structure. A deterministic validator will prove
or reject your proposal: a mapping is accepted only if, within every
statement period, the opening balance plus credits minus debits reproduces
the running balance column row by row to the paisa, ending exactly on the
closing balance.

The grid is 0-indexed (rows and columns). Classify EVERY row exactly once:
- the single header row;
- noise rows (banners, blank rows, legends — nothing transaction-like);
- one or more periods, each with an opening-balance pseudo-row, a
  closing-balance pseudo-row, and its transaction rows in order.
Interest credits and reversals ARE transactions (they move the balance).

Also propose a reference_recipe for pulling a stable reference token out of
narrations (e.g. the 12-digit UPI RRN at a fixed delimited position), and
list reversal_pairs [debit_row, reversing_credit_row] where you see them.

Use peek_rows / find_rows to inspect, then submit_mapping. If the validator
rejects, fix exactly what its error codes say and resubmit."""

TOOLS = [
    T.strict_tool("peek_rows", "Return grid rows start..start+count-1 "
                  "(count capped at 40).",
                  {"start": {"type": "integer"},
                   "count": {"type": "integer"}}),
    T.strict_tool("find_rows", "Case-insensitive substring search over all "
                  "cells; returns at most 20 matching rows.",
                  {"contains": {"type": "string"}}),
    T.strict_tool("submit_mapping", "Submit the proposed statement mapping "
                  "for deterministic validation.",
                  {"mapping": MAPPING_SCHEMA}),
]


class IntakeError(Exception):
    def __init__(self, msg: str, report: ValidationReport | None = None):
        super().__init__(msg)
        self.report = report


@dataclass
class IntakeResult:
    mapping: StatementMapping
    validation: ValidationReport
    out_dir: str
    canonical_rows: int
    mode: str


def _preview(grid: list[list[str]]) -> str:
    head = [{"i": i, "cells": grid[i]}
            for i in range(min(_PREVIEW_ROWS, len(grid)))]
    return json.dumps(
        {"rows_total": len(grid), "cols": len(grid[0]) if grid else 0,
         "first_rows": head}, ensure_ascii=False)


def _agent_mapping(grid, transport) -> tuple[StatementMapping,
                                             ValidationReport]:
    state: dict = {"accepted": None, "rejections": 0, "last": None}

    def peek_rows(inp):
        start = max(0, inp["start"])
        count = max(1, min(40, inp["count"]))
        return {"rows": [{"i": i, "cells": grid[i]}
                         for i in range(start, min(start + count,
                                                   len(grid)))]}

    def find_rows(inp):
        needle = inp["contains"].lower()
        hits = [{"i": i, "cells": row} for i, row in enumerate(grid)
                if needle in " | ".join(row).lower()]
        return {"matches": hits[:20], "truncated": len(hits) > 20}

    def submit_mapping(inp):
        mapping = StatementMapping.from_dict(inp["mapping"])
        rep = validate_mapping(grid, mapping)
        state["last"] = rep
        if rep.accepted:
            state["accepted"] = (mapping, rep)
            return {"accepted": True, "stats": rep.stats,
                    "warnings": rep.warnings}
        state["rejections"] += 1
        return {"accepted": False,
                "errors": rep.errors[:_MAX_ERRORS_FED_BACK],
                "errors_total": len(rep.errors),
                "rejections_left": MAX_REJECTIONS - state["rejections"]}

    def done() -> bool:
        return (state["accepted"] is not None
                or state["rejections"] >= MAX_REJECTIONS)

    try:
        run_loop(transport, system=_SYSTEM, tools=TOOLS,
                 impls={"peek_rows": peek_rows, "find_rows": find_rows,
                        "submit_mapping": submit_mapping},
                 user_content=_preview(grid), max_calls=MAX_CALLS,
                 stop_when=done)
    except AgentError as exc:
        if state["accepted"] is None:
            raise IntakeError(f"agent run failed: {exc}", state["last"])
    if state["accepted"] is None:
        raise IntakeError(
            f"no accepted mapping after {state['rejections']} rejection(s)",
            state["last"])
    return state["accepted"]


def _reprove_from_loader(out_dir: str, mapping: StatementMapping,
                         validation: ValidationReport) -> None:
    """Post-apply defense in depth: reload the canonical CSV through the
    untouched production loader and prove the chain again from typed rows."""
    typed = io_load.load_bank_rows(out_dir)
    i = 0
    for pstat, period in zip(validation.stats["periods"], mapping.periods):
        running = pstat["opening_paise"]
        for _ in period.transaction_rows:
            row = typed[i]
            running = running + row.credit_paise - row.debit_paise
            if row.balance_paise != running:
                raise IntakeError(
                    f"post-apply re-proof failed at {row.txn_id}: loader "
                    f"sees balance {row.balance_paise}, chain expects "
                    f"{running}")
            i += 1
    if i != len(typed):
        raise IntakeError(f"post-apply re-proof covered {i} rows but the "
                          f"loader returned {len(typed)}")


def intake(raw_path: str, out_dir: str, *, transport=None,
           mapping_path: str | None = None, mode: str = "") -> IntakeResult:
    grid = load_grid(raw_path)
    if mapping_path:
        with open(mapping_path, encoding="utf-8") as f:
            mapping = StatementMapping.from_dict(json.load(f))
        validation = validate_mapping(grid, mapping)
        if not validation.accepted:
            raise IntakeError(
                f"mapping rejected with {len(validation.errors)} error(s); "
                f"first: {validation.errors[0]}", validation)
    else:
        mapping, validation = _agent_mapping(grid, transport)

    rows = apply_mapping(grid, mapping)
    write_world(out_dir, rows)
    with open(os.path.join(out_dir, "mapping.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(mapping.to_dict(), f, ensure_ascii=False, indent=1)
        f.write("\n")
    _reprove_from_loader(out_dir, mapping, validation)
    return IntakeResult(mapping=mapping, validation=validation,
                        out_dir=out_dir, canonical_rows=len(rows), mode=mode)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Agentic statement intake with deterministic proof.")
    ap.add_argument("raw", help="messy bank export (.xlsx or .csv)")
    ap.add_argument("--out", required=True, help="canonical world directory")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--record", metavar="TRANSCRIPT",
                     help="live agent run; records the transcript here")
    src.add_argument("--replay", metavar="TRANSCRIPT",
                     help="replay a recorded transcript (offline)")
    src.add_argument("--mapping", metavar="MAPPING",
                     help="skip the agent; use this mapping.json")
    ap.add_argument("--report", nargs="?", const="", default=None,
                    metavar="PATH",
                    help="write the real-data report (default "
                         "reports/real_data_report.md)")
    args = ap.parse_args()

    transport = None
    if args.record:
        if os.path.exists(args.record):
            os.remove(args.record)
        transport = LiveTransport(args.record, task="statement_intake")
        mode = "live(recorded)"
    elif args.replay:
        transport = ReplayTransport(args.replay)
        mode = "replay"
    else:
        mode = "hand-mapped"

    result = intake(args.raw, args.out, transport=transport,
                    mapping_path=args.mapping, mode=mode)
    st = result.validation.stats
    print(f"intake ok · {result.canonical_rows} canonical rows · "
          f"{len(st['periods'])} period(s) proven · mode {mode}")
    for p in st["periods"]:
        print(f"  period {p['period']}: {p['transaction_rows']} rows, "
              f"chain {'ok' if p['chain_ok'] else 'BROKEN'}")

    if args.report is not None:
        settlements = io_load.load_settlements(result.out_dir)
        bank_rows = io_load.load_bank_rows(result.out_dir)
        decisions = reconcile_leg_a(settlements, bank_rows)
        by_status = Counter(d.status for d in decisions)
        path = args.report or os.path.join("reports", "real_data_report.md")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        text = report_mod.render_real_data_report(
            raw_name=os.path.basename(args.raw), result=result,
            by_status=dict(by_status), bank_rows=bank_rows)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"  report: {path}")


if __name__ == "__main__":
    main()
