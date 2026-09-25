"""The investigator agent: reads evidence, drafts a note, decides nothing.

    python -m agent.investigate <data_dir> <item_id>
        (--record TRANSCRIPT | --replay TRANSCRIPT)

For one exception-queue item, an agent with READ-ONLY tools over the
precomputed close (the queue item, the decisions that mention its records,
the bank statement around a date, the cash headline) drafts an
investigation note with a recommended next step. The note lands in the
operator workflow state via `queue_state.append_action(action="note",
by="agent:investigator")` — advisory text on an already-final item. The
close itself, and every decision in it, is untouched by construction: the
tools return copies of a dict, and the only write is the note.
"""

from __future__ import annotations

import argparse
import json
import os

from recon import io_load
from recon.normalize import parse_bank_date

from agent import tools as T
from agent.loop import run_loop
from agent.provider import LiveTransport, ReplayTransport
from controller import queue_state as QS

MAX_CALLS = 8
_MAX_MATCHES = 5
_MAX_WINDOW_ROWS = 40

_SYSTEM = """\
You are a finance-operations investigator. One exception-queue item from a
daily close needs a short investigation note for the human operator.

You have read-only evidence tools. You cannot change any decision, match,
or amount — the reconciliation engine already decided everything
deterministically, and your note is advisory display text.

Investigate the item (what happened, what the evidence shows, what the
candidates were and why they were rejected), then END with your note as
plain text: 3-6 sentences — what you found, what it means in rupees, and
the single most useful next step for the operator. No headings, no bullet
lists, no restating of raw JSON."""


def _mentions(obj, needle: str) -> bool:
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(_mentions(v, needle) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_mentions(v, needle) for v in obj)
    return False


def build_tools(data_dir: str, close: dict, view: list[dict]):
    by_id = {v["item_id"]: v for v in view}
    bank_rows = io_load.load_bank_rows(data_dir)
    decision_pools = [("leg_a", close["decisions_a"]),
                      ("leg_b", close["decisions_b"])]

    def get_queue_item(inp):
        item = by_id.get(inp["item_id"])
        if item is None:
            return {"error": f"no item {inp['item_id']} in this queue"}
        item = dict(item)
        item.pop("actions", None)
        return item

    def find_decisions(inp):
        needle = inp["record_id"]
        hits = []
        for pool_name, pool in decision_pools:
            for d in pool:
                if _mentions(d, needle):
                    hits.append({"pool": pool_name, **d})
                if len(hits) >= _MAX_MATCHES:
                    break
            if len(hits) >= _MAX_MATCHES:
                break
        return {"matches": hits, "truncated": len(hits) >= _MAX_MATCHES}

    def statement_window(inp):
        import datetime as _dt
        center = _dt.date.fromisoformat(inp["date_iso"])
        lo = center - _dt.timedelta(days=min(10, max(0, inp["days_before"])))
        hi = center + _dt.timedelta(days=min(10, max(0, inp["days_after"])))
        rows = [{"txn_id": r.txn_id, "value_date": r.value_date,
                 "narration": r.narration, "ref_no": r.ref_no,
                 "debit_paise": r.debit_paise, "credit_paise": r.credit_paise}
                for r in bank_rows
                if lo <= parse_bank_date(r.value_date) <= hi]
        return {"rows": rows[:_MAX_WINDOW_ROWS],
                "truncated": len(rows) > _MAX_WINDOW_ROWS}

    def cash_headline(_inp):
        return {"close_date": close["close_date"], "cash": close["cash"],
                "queue_counts": close["counts"]}

    schemas = [
        T.strict_tool("get_queue_item", "The queue item under investigation, "
                      "with evidence and rejected candidates.",
                      {"item_id": {"type": "string"}}),
        T.strict_tool("find_decisions", "Every close decision that mentions "
                      "this record id (settlement, bank txn, payment, order, "
                      "invoice…).", {"record_id": {"type": "string"}}),
        T.strict_tool("statement_window", "Bank statement rows around a "
                      "date (both bounds capped at 10 days).",
                      {"date_iso": {"type": "string"},
                       "days_before": {"type": "integer"},
                       "days_after": {"type": "integer"}}),
        # The description still names the retired forecast: tool schemas are
        # part of every recorded request, so changing this text would break
        # the hash-checked replay of the committed investigator transcript.
        # The tool now returns close date, cash position and queue counts.
        T.strict_tool("cash_headline", "Close date, cash position, forecast "
                      "minimum balance and queue counts.",
                      {}),
    ]
    impls = {"get_queue_item": get_queue_item,
             "find_decisions": find_decisions,
             "statement_window": statement_window,
             "cash_headline": cash_headline}
    return schemas, impls


def run_investigation(data_dir: str, item_id: str, transport) -> str:
    from controller.close import daily_close
    close = daily_close(data_dir).to_dict()
    view = QS.overlay(close["queue"], QS.load_state(data_dir))
    if not any(v["item_id"] == item_id for v in view):
        raise SystemExit(f"no queue item {item_id} in this world")

    schemas, impls = build_tools(data_dir, close, view)
    result = run_loop(
        transport, system=_SYSTEM, tools=schemas, impls=impls,
        user_content=json.dumps({"investigate_item_id": item_id},
                                ensure_ascii=False),
        max_calls=MAX_CALLS)
    note = result.final_text.strip()
    if not note:
        raise SystemExit("agent produced no note")

    before = daily_close(data_dir).to_dict()
    assert before == close, "invariant broken: investigation altered the close"
    status = next(v["status"] for v in view if v["item_id"] == item_id)
    QS.append_action(data_dir, item_id, action="note",
                     by="agent:investigator", note=note,
                     status_at_action=status)
    return note


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Draft an advisory investigation note for one queue "
                    "item (read-only tools; never a disposition).")
    ap.add_argument("data_dir")
    ap.add_argument("item_id")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--record", metavar="TRANSCRIPT")
    src.add_argument("--replay", metavar="TRANSCRIPT")
    args = ap.parse_args()

    if args.record:
        if os.path.exists(args.record):
            os.remove(args.record)
        transport = LiveTransport(args.record, task="investigate")
    else:
        transport = ReplayTransport(args.replay)

    note = run_investigation(args.data_dir, args.item_id, transport)
    print(f"note attached to {args.item_id} by agent:investigator:\n\n{note}")


if __name__ == "__main__":
    main()
