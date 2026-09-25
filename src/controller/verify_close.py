"""Independent verifier for the daily close's aggregation layer.

Deliberately shares no policy code with controller.close / controller.triage:
the severity tiers, money-at-risk rules, ordering comparator, queue-worthy
status sets and rupee/date parsers below are re-typed from the published
policy, so a defect (or tampering) in either side surfaces as a violation
instead of agreeing with itself. Raw amounts come from the world's own CSVs
with this module's own parser.

It also re-runs the leg A stage verifier on the decisions embedded in the
close (defense in depth) and requires the close's trust panel to equal what
it re-measured — a close that under-reports its own violations is itself a
violation.

CLI:  python -m controller.verify_close <data_dir> <close.json>
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter
from datetime import date

# The stage verifier is already independent of its engine; reusing it here
# is the point of embedding the decisions in the close.
from recon.verify import verify_leg_a

# --- own parsers --------------------------------------------------------------


def _own_paise(s: str) -> int:
    s = (s or "").strip().replace(",", "")
    if not s:
        return 0
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    rupees, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    return sign * (int(rupees or "0") * 100 + int(frac or "0"))


def _own_date(ddmmyyyy: str) -> date:
    dd, mm, yyyy = ddmmyyyy.split("/")
    return date(int(yyyy), int(mm), int(dd))


def _own_iso_date(s: str) -> date:
    y, m, d = s.strip()[:10].split("-")
    return date(int(y), int(m), int(d))


def _read_csv(data_dir: str, name: str) -> list[dict]:
    with open(os.path.join(data_dir, name), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# --- own copy of the published policy ----------------------------------------

_TIER = {
    "exception_missing_bank": 1, "duplicate_credit": 1,
    "ambiguous_abstain": 2, "exception_missing_settlement": 2,
    "exception_amount_mismatch": 2, "exception_duplicate_payment": 2,
    "needs_review": 3, "exception_unpaid_order": 3,
    "exception_payment_no_order": 3,
}
_LEG_A_QUEUED = {"duplicate_credit", "ambiguous_abstain",
                 "exception_missing_bank", "exception_missing_settlement"}
_LEG_A_MATCHED = {"matched", "matched_split", "matched_merged",
                  "matched_with_discrepancy"}
_LEG_B_QUEUED = {"exception_unpaid_order", "exception_duplicate_payment",
                 "exception_payment_no_order", "exception_amount_mismatch"}
_OVERDUE_STATUSES = {"exception_missing_bank", "ambiguous_abstain"}


def _money_leg_a(status: str, d: dict) -> int:
    if status == "exception_missing_bank":
        return d.get("expected_paise") or 0
    if status == "duplicate_credit":
        return d.get("received_paise") or 0
    if status == "ambiguous_abstain":
        return (d.get("expected_paise") if d.get("expected_paise") is not None
                else d.get("received_paise")) or 0
    if status == "exception_missing_settlement":
        return d.get("received_paise") or 0
    return abs(d.get("discrepancy_paise") or 0)   # needs_review


# --- expected queue units, re-derived from the embedded decisions -------------


def _expected_units(close: dict, order_amt: dict,
                    pay_amt: dict) -> dict[tuple, int]:
    """(status, frozenset(record_ids)) -> expected money at risk."""
    units: dict[tuple, int] = {}

    for d in close["decisions_a"]:
        status = d["status"]
        if status in _LEG_A_MATCHED and d.get("confidence") == "needs_review":
            status = "needs_review"
        elif status not in _LEG_A_QUEUED:
            continue
        ids = frozenset(d["settlement_ids"]) | frozenset(d["bank_txn_ids"])
        units[(status, ids)] = _money_leg_a(status, d)

    dec_b = close["decisions_b"]
    mm_orders = [d for d in dec_b if d["status"] == "exception_amount_mismatch"
                 and d["record_type"] == "order"]
    mm_payments = {d["record_id"] for d in dec_b
                   if d["status"] == "exception_amount_mismatch"
                   and d["record_type"] == "payment"}
    fused: set[str] = set()
    for d in mm_orders:
        pids = sorted(p for p in d["counterparty_ids"] if p in mm_payments)
        fused.update(pids)
        captured = sum(pay_amt.get(p, 0) for p in pids)
        money = abs(captured - order_amt.get(d["record_id"], 0))
        units[("exception_amount_mismatch",
               frozenset([d["record_id"], *pids]))] = money
    for d in dec_b:
        status = d["status"]
        if status not in _LEG_B_QUEUED:
            continue
        if status == "exception_amount_mismatch":
            if d["record_type"] == "order" or d["record_id"] in fused:
                continue
            money = pay_amt.get(d["record_id"], 0)
        elif status == "exception_unpaid_order":
            money = order_amt.get(d["record_id"], 0)
        else:
            money = pay_amt.get(d["record_id"], 0)
        ids = frozenset([d["record_id"], *d["counterparty_ids"]])
        units[(status, ids)] = money

    return units


def _expected_overdue(close: dict, settlements: list[dict]) -> dict[str, int]:
    """settlement_id -> days overdue, re-derived from the raw settlements
    file: left unpaid (missing bank credit or abstained), created on or before
    the close date, expected credit date (settled_at, Sunday rolled to Monday)
    on or before the close date."""
    close_date = _own_iso_date(close["close_date"])
    unpaid: dict[str, str] = {}
    for d in close["decisions_a"]:
        for sid in d["settlement_ids"]:
            unpaid[sid] = d["status"]
    out: dict[str, int] = {}
    for r in settlements:
        if unpaid.get(r["settlement_id"]) not in _OVERDUE_STATUSES:
            continue
        if _own_iso_date(r["created_at"]) > close_date:
            continue
        expected = _own_iso_date(r["settled_at"])
        if expected.weekday() == 6:
            expected = date.fromordinal(expected.toordinal() + 1)
        if expected <= close_date:
            out[r["settlement_id"]] = (close_date - expected).days
    return out


# --- the verifier -------------------------------------------------------------


def verify_close(data_dir: str, close) -> list[str]:
    if hasattr(close, "to_dict"):
        close = close.to_dict()
    v: list[str] = []
    queue = [i if isinstance(i, dict) else i.to_dict() for i in close["queue"]]

    # 1. cash position, re-derived from the raw statement
    stmt = _read_csv(data_dir, "bank_statement.csv")
    if not stmt:
        return ["bank statement is empty"]
    if close["cash"]["balance_paise"] != _own_paise(stmt[-1]["balance"]):
        v.append("cash balance does not equal the last statement row")
    if close["cash"]["statement_rows"] != len(stmt):
        v.append("statement row count mismatch")
    dates = [_own_date(r["value_date"]) for r in stmt]
    if close["close_date"] != max(dates).isoformat():
        v.append("close_date is not the last statement value date")
    if close["cash"]["first_statement_date"] != min(dates).isoformat():
        v.append("first_statement_date mismatch")
    if close["cash"]["history_days"] != (max(dates) - min(dates)).days + 1:
        v.append("history_days mismatch")

    # 2. queue completeness + per-item severity/money, vs re-derived units
    order_amt = {r["order_id"]: int(r["amount_paise"])
                 for r in _read_csv(data_dir, "order_book.csv")}
    pay_amt = {r["payment_id"]: int(r["amount_paise"])
               for r in _read_csv(data_dir, "payments.csv")}
    units = _expected_units(close, order_amt, pay_amt)
    seen: Counter = Counter()
    for i in queue:
        status = i["status"]
        key = (status, frozenset(i["record_ids"]))
        if key not in units:
            v.append(f"queue item has no decision behind it: {status} "
                     f"{sorted(i['record_ids'])}")
            continue
        seen[key] += 1
        if i["money_at_risk_paise"] != units[key]:
            v.append(f"money at risk wrong for {status} "
                     f"{sorted(i['record_ids'])}: item says "
                     f"{i['money_at_risk_paise']}, decision implies {units[key]}")
        if i["severity"] != _TIER[status]:
            v.append(f"severity wrong for {status}: item says S{i['severity']},"
                     f" policy says S{_TIER[status]}")
        if not i.get("suggested_action"):
            v.append(f"queue item without a suggested action: {status}")
    for key, n in seen.items():
        if n > 1:
            v.append(f"decision surfaced {n} times in the queue: {key[0]}")
    for key in units:
        if key not in seen:
            v.append(f"queue is missing a decision: {key[0]} {sorted(key[1])}")

    # 3. overdue fusion: every overdue settlement enriches its recon item,
    # and no item claims to be overdue without one behind it
    overdue = _expected_overdue(close, _read_csv(data_dir, "settlements.csv"))
    recon_items = [i for i in queue if i["source"] == "recon_a"]
    for sid, days in overdue.items():
        hosts = [i for i in recon_items if sid in i["record_ids"]]
        if not hosts:
            v.append(f"overdue settlement {sid} not represented in the queue")
        elif all(i.get("days_overdue") is None for i in hosts):
            v.append(f"overdue settlement {sid} fused without days_overdue")
    for i in recon_items:
        mine = [overdue[s] for s in i["record_ids"] if s in overdue]
        want = max(mine) if mine else None
        if i.get("days_overdue") != want:
            v.append(f"days_overdue {i.get('days_overdue')} on "
                     f"{sorted(i['record_ids'])} but the files imply {want}")

    # 4. ordering per the published comparator, on the items' stored fields
    def sort_key(i: dict):
        return (i["severity"], -i["money_at_risk_paise"], i["source"],
                i["status"], i["record_ids"][0] if i["record_ids"] else "")
    if [sort_key(i) for i in queue] != sorted(sort_key(i) for i in queue):
        v.append("queue is not in the published order")

    # 5. summaries and counts re-added from the embedded decisions
    a_status = Counter(d["status"] for d in close["decisions_a"])
    if close["recon_summary"]["by_status"] != dict(sorted(a_status.items())):
        v.append("recon summary status counts differ from decisions")
    matched_amount = sum((d["received_paise"] or 0)
                         for d in close["decisions_a"]
                         if d["status"] in _LEG_A_MATCHED)
    if close["recon_summary"]["matched_amount_paise"] != matched_amount:
        v.append("recon summary matched amount differs from decisions")
    # the published match rate, recomputed with re-typed status names
    n_in_scope = sum(1 for d in close["decisions_a"]
                     if d["status"] not in ("out_of_scope",
                                            "non_settlement_credit"))
    n_matched = sum(1 for d in close["decisions_a"]
                    if d["status"] in _LEG_A_MATCHED)
    rs = close["recon_summary"]
    if ((rs.get("in_scope"), rs.get("matched"), rs.get("unresolved"))
            != (n_in_scope, n_matched, n_in_scope - n_matched)):
        v.append("recon summary match-rate counts differ from decisions")
    want_rate = round(n_matched / n_in_scope, 4) if n_in_scope else None
    if rs.get("match_rate") != want_rate:
        v.append("recon summary match rate differs from decisions")
    b_status = Counter(d["status"] for d in close["decisions_b"])
    if close["leg_b_summary"]["by_status"] != dict(sorted(b_status.items())):
        v.append("leg B summary status counts differ from decisions")
    if close["leg_b_summary"]["journeys_total"] != len(order_amt):
        v.append("journey count differs from the order book")

    counts = close["counts"]
    if counts["queue_total"] != len(queue):
        v.append("counts.queue_total differs from the queue")
    for s in (1, 2, 3):
        if counts["by_severity"].get(f"S{s}") != sum(
                1 for i in queue if i["severity"] == s):
            v.append(f"counts.by_severity S{s} differs from the queue")
    if counts["by_source"] != dict(sorted(
            Counter(i["source"] for i in queue).items())):
        v.append("counts.by_source differs from the queue")

    # 6. defense in depth: the stage verifier on the embedded decisions must
    # agree with the close's own trust panel
    va = verify_leg_a(data_dir, close["decisions_a"])
    v.extend(f"stage verifier (leg A): {s}" for s in va)
    if close["verify_panel"]["leg_a"] != len(va):
        v.append("trust panel misreports the leg A verifier")

    return v


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python -m controller.verify_close <data_dir> <close.json>")
        raise SystemExit(2)
    with open(sys.argv[2], encoding="utf-8") as f:
        close = json.load(f)
    violations = verify_close(sys.argv[1], close)
    for s in violations:
        print(f"VIOLATION: {s}")
    print(f"{len(violations)} violation(s)")
    raise SystemExit(1 if violations else 0)


if __name__ == "__main__":
    main()
