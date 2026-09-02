"""Close snapshots and the day-over-day delta.

A snapshot freezes one close's queue (plus cash and counts) under
data/state/<world>/closes/<close_date>.json. The delta between two closes
is keyed by `queue_state.item_id` — stable across days and status changes —
so "new / carried / gone" is well-defined even as the underlying records
evolve. Items that left the queue are split by consulting the operator
state file: gone-and-resolved-by-an-operator vs gone-because-the-data-moved.

Snapshots live beside the close (data/state/), never inside it: writing or
reading them cannot change what `daily_close` computes.
"""

from __future__ import annotations

import json
import os

from controller import queue_state as QS


def closes_dir(data_dir: str) -> str:
    return os.path.join(QS.STATE_ROOT, QS.world_name(data_dir), "closes")


def write_snapshot(close: dict, data_dir: str) -> str:
    payload = {
        "close_date": close["close_date"],
        "cash": close["cash"],
        "counts": close["counts"],
        "queue": [{"item_id": QS.item_id(i), "severity": i["severity"],
                   "status": i["status"], "source": i["source"],
                   "title": i["title"],
                   "money_at_risk_paise": i["money_at_risk_paise"]}
                  for i in close["queue"]],
    }
    path = os.path.join(closes_dir(data_dir), f"{close['close_date']}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    return path


def latest_before(data_dir: str, close_date: str) -> dict | None:
    d = closes_dir(data_dir)
    if not os.path.isdir(d):
        return None
    candidates = sorted(name[:-5] for name in os.listdir(d)
                        if name.endswith(".json") and name[:-5] < close_date)
    if not candidates:
        return None
    with open(os.path.join(d, f"{candidates[-1]}.json"),
              encoding="utf-8") as f:
        return json.load(f)


def delta(prev_snap: dict, close: dict, state: dict) -> dict:
    cur = {QS.item_id(i): i for i in close["queue"]}
    prev = {i["item_id"]: i for i in prev_snap["queue"]}

    def _resolved_by(item_id: str) -> str:
        actions = state.get("items", {}).get(item_id, {}).get("actions", [])
        return ("resolved_by_operator"
                if any(a["action"] == "resolve" for a in actions)
                else "resolved_by_data")

    return {
        "prev_close_date": prev_snap["close_date"],
        "new": [dict(i, item_id=iid) for iid, i in cur.items()
                if iid not in prev],
        "carried": [dict(i, item_id=iid) for iid, i in cur.items()
                    if iid in prev],
        "gone": [dict(prev[iid], resolved_by=_resolved_by(iid))
                 for iid in prev if iid not in cur],
    }


def render_delta_md(d: dict) -> str:
    ops = sum(1 for g in d["gone"] if g["resolved_by"] == "resolved_by_operator")
    lines = [
        "", f"## Since last close ({d['prev_close_date']})", "",
        f"- **{len(d['new'])} new** queue item(s) · {len(d['carried'])} "
        f"carried · {len(d['gone'])} gone "
        f"({ops} resolved by an operator, {len(d['gone']) - ops} resolved "
        "by the data)",
    ]
    urgent = sorted((i for i in d["new"] if i["severity"] <= 2),
                    key=lambda i: (i["severity"], -i["money_at_risk_paise"]))
    for i in urgent:
        lines.append(f"  - NEW S{i['severity']} `{i['item_id']}` — "
                     f"{i['title']} [{i['status']}]")
    return "\n".join(lines) + "\n"
