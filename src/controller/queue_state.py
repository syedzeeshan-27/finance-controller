"""Operator workflow state for the exception queue — beside the close, never
inside it.

`daily_close` stays a pure function of the world: same CSVs in, byte-identical
close out (pinned by test). This module stores what OPERATORS do about queue
items — resolve / assign / snooze / note — in an append-only per-world state
file, and overlays that workflow onto a freshly computed queue.

Identity: an item is `sha1(source|sorted record_ids)` (first 12 hex chars).
Status is deliberately NOT part of the id — when the underlying records
change status after an operator resolved the item, the id survives and the
overlay flags it `reopened` instead of silently minting a new item.

State file: data/state/<world>/queue_state.json
    {"version": 1,
     "items": {"<id>": {"status_at_action": "...",
                        "actions": [{"action", "by", "at", "note",
                                     "assignee", "snooze_until",
                                     "status_at_action"}, ...]}}}
Wall-clock timestamps exist ONLY here. Writes are atomic (temp file +
os.replace). Single-operator by design; documented, not locked.

CLI:  python -m controller.queue_state <data_dir> list
      python -m controller.queue_state <data_dir> resolve|assign|snooze|note
          <item_id> [--note TEXT] [--assignee WHO] [--until YYYY-MM-DD]
          [--by WHO]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
STATE_ROOT = os.path.join(ROOT, "data", "state")

ACTIONS = ("resolve", "assign", "snooze", "note")
WORKFLOWS = ("open", "assigned", "snoozed", "resolved", "reopened")


def item_id(item: dict) -> str:
    key = item["source"] + "|" + "|".join(sorted(item["record_ids"]))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def world_name(data_dir: str) -> str:
    parts = os.path.normpath(os.path.abspath(data_dir)).split(os.sep)
    name = parts[-1] if parts[-2:-1] == ["seeds"] else "_".join(parts[-2:])
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def state_path(data_dir: str) -> str:
    return os.path.join(STATE_ROOT, world_name(data_dir),
                        "queue_state.json")


def load_state(data_dir: str) -> dict:
    path = state_path(data_dir)
    if not os.path.exists(path):
        return {"version": 1, "items": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def append_action(data_dir: str, target_id: str, *, action: str,
                  by: str = "operator", note: str = "",
                  assignee: str | None = None,
                  snooze_until: str | None = None,
                  status_at_action: str | None = None,
                  at: str | None = None) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r} (expected {ACTIONS})")
    state = load_state(data_dir)
    entry = state["items"].setdefault(
        target_id, {"status_at_action": status_at_action, "actions": []})
    if status_at_action is not None:
        entry["status_at_action"] = status_at_action
    entry["actions"].append({
        "action": action, "by": by,
        "at": at or _dt.datetime.now().isoformat(timespec="seconds"),
        "note": note, "assignee": assignee, "snooze_until": snooze_until,
        "status_at_action": status_at_action,
    })
    _atomic_write(state_path(data_dir), state)
    return state


def _fold_workflow(actions: list[dict]) -> tuple[str, str | None, str | None]:
    workflow, assignee, snooze_until = "open", None, None
    for a in actions:
        if a["action"] == "resolve":
            workflow = "resolved"
        elif a["action"] == "assign":
            workflow, assignee = "assigned", a.get("assignee")
        elif a["action"] == "snooze":
            workflow, snooze_until = "snoozed", a.get("snooze_until")
    return workflow, assignee, snooze_until


def overlay(queue: list[dict], state: dict) -> list[dict]:
    """Fresh deterministic queue + stored workflow -> operator view.

    Adds: item_id, workflow, assignee, snooze_until, last_note, actions,
    reopened. Never mutates the input items."""
    out = []
    for item in queue:
        view = dict(item)
        iid = item_id(item)
        view["item_id"] = iid
        entry = state.get("items", {}).get(iid)
        if not entry:
            view.update(workflow="open", assignee=None, snooze_until=None,
                        last_note="", actions=[], reopened=False)
        else:
            workflow, assignee, snooze_until = _fold_workflow(
                entry["actions"])
            notes = [a["note"] for a in entry["actions"] if a.get("note")]
            reopened = (entry.get("status_at_action") is not None
                        and entry["status_at_action"] != item["status"])
            if reopened and workflow == "resolved":
                workflow = "reopened"
            view.update(workflow=workflow, assignee=assignee,
                        snooze_until=snooze_until,
                        last_note=notes[-1] if notes else "",
                        actions=entry["actions"], reopened=reopened)
        out.append(view)
    return out


def build_queue_view(data_dir: str) -> list[dict]:
    """Run the (pure) close, then overlay the stored workflow."""
    from controller.close import daily_close
    close = daily_close(data_dir).to_dict()
    return overlay(close["queue"], load_state(data_dir))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Act on exception-queue items (workflow state only; "
                    "the close itself is never modified).")
    ap.add_argument("data_dir")
    ap.add_argument("verb", choices=("list",) + ACTIONS)
    ap.add_argument("item_id", nargs="?")
    ap.add_argument("--note", default="")
    ap.add_argument("--assignee")
    ap.add_argument("--until", help="snooze until YYYY-MM-DD")
    ap.add_argument("--by", default="operator")
    args = ap.parse_args()

    view = build_queue_view(args.data_dir)
    if args.verb == "list":
        for v in view:
            flag = " REOPENED" if v["reopened"] else ""
            who = f" -> {v['assignee']}" if v["assignee"] else ""
            print(f"{v['item_id']}  S{v['severity']}  "
                  f"{v['workflow']:<9}{flag}{who}  {v['title']}  "
                  f"[{v['status']}]")
        return

    if not args.item_id:
        ap.error(f"{args.verb} needs an item_id (see `list`)")
    match = next((v for v in view if v["item_id"] == args.item_id), None)
    if match is None:
        raise SystemExit(f"no queue item {args.item_id} in this world's "
                         f"current queue")
    append_action(args.data_dir, args.item_id, action=args.verb,
                  by=args.by, note=args.note, assignee=args.assignee,
                  snooze_until=args.until,
                  status_at_action=match["status"])
    print(f"{args.verb} recorded on {args.item_id} "
          f"({match['title']}) by {args.by}")


if __name__ == "__main__":
    main()
