"""The daily close: reconciliation in one pass, one queue.

`daily_close` loads a world once, runs the full-world leg A reconciliation
(settlements <-> bank credits) exactly once, and hands those decisions to
leg B (payments <-> orders) and the journey tracer. Every decision that
needs a human lands in one severity-ranked queue (policy in
controller.triage); the leg A verifier runs on the same output and its count
ships inside the close as the trust panel.

CLI:  python -m controller.close <data_dir> [--report [PATH]] [--json [PATH]]
          [--as-of YYYY-MM-DD] [--snapshot]
      python -m controller.close --merchants data/merchants.json
Exit code 1 when the verifier reports a violation.

`--as-of` closes the books as they stood on an earlier date by row-filtering
the raw files at that date. `--snapshot` freezes the queue under
data/state/; when a prior snapshot exists, the report gains a "Since last
close" delta keyed by stable item ids.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections import Counter

from recon import io_load
from recon import schemas as RS
from recon.engine import reconcile_leg_a
from recon.explain import attach_explanations
from recon.journey import build_journeys
from recon.leg_b import reconcile_leg_b
from recon.normalize import parse_bank_date, parse_iso_date
from recon.verify import verify_leg_a
from controller import triage
from controller.overdue import overdue_settlements
from controller.schemas import DailyClose

_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "reports")


def _inr(paise: int | None) -> str:
    """₹ with Indian digit grouping, from integer paise."""
    if paise is None:
        return "—"
    sign = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{sign}₹{s}.{rem:02d}"


_SLICE_RULES = {
    # canonical file -> (date column, parser) for the as-of cut
    "bank_statement.csv": ("value_date", "bank"),
    "settlements.csv": ("created_at", "iso"),
    "payments.csv": ("created_at", "iso"),
    "order_book.csv": ("created_at", "iso"),
}


def _slice_world_files(data_dir: str, out_dir: str, as_of) -> None:
    """Row-filter the raw CSVs at as_of, preserving the original strings.

    Textual slicing means an as-of close runs the UNMODIFIED close over
    files identical to what existed that day — the verifiers re-read the
    same raw world the engines saw, so their contract is untouched."""
    import csv as _csv
    for name, rule in _SLICE_RULES.items():
        src = os.path.join(data_dir, name)
        if not os.path.exists(src):
            continue
        with open(src, encoding="utf-8", newline="") as f:
            reader = _csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        column, kind = rule
        parse = parse_bank_date if kind == "bank" else parse_iso_date
        rows = [r for r in rows if parse(r[column]) <= as_of]
        with open(os.path.join(out_dir, name), "w", encoding="utf-8",
                  newline="") as f:
            w = _csv.DictWriter(f, fieldnames=fieldnames,
                                lineterminator="\n")
            w.writeheader()
            w.writerows(rows)


def daily_close(data_dir: str, as_of=None) -> DailyClose:
    if as_of is not None:
        # the close you WOULD have run that day: slice the raw files into a
        # temp world and run the unmodified close over it
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="asof_")
        try:
            _slice_world_files(data_dir, tmp, as_of)
            close = daily_close(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        close.world = os.path.basename(os.path.normpath(data_dir))
        return close

    warnings: list[str] = []
    orders = io_load.load_orders(data_dir)
    bank_rows = io_load.load_bank_rows(data_dir)
    settlements = io_load.load_settlements(data_dir)
    payments = io_load.load_payments(data_dir)

    dates = [parse_bank_date(r.value_date) for r in bank_rows]
    close_date = max(dates)
    first_date = min(dates)
    cash = {
        "balance_paise": bank_rows[-1].balance_paise,
        "statement_rows": len(bank_rows),
        "first_statement_date": first_date.isoformat(),
        "history_days": (close_date - first_date).days + 1,
    }

    # real exports can concatenate disjoint statement periods; say so out loud
    prev_row = None
    for r in bank_rows:
        d = parse_bank_date(r.value_date)
        if (prev_row is not None and (d - prev_row[0]).days > 7
                and prev_row[1] + r.credit_paise - r.debit_paise
                != r.balance_paise):
            warnings.append(
                f"statement gap {prev_row[0]} → {d}: balance continuity "
                "across the gap is unverifiable (disjoint statement periods)")
        prev_row = (d, r.balance_paise)

    # --- the ONE full-world Stage 1 run ---------------------------------------
    decisions_a = reconcile_leg_a(settlements, bank_rows)
    attach_explanations(decisions_a)
    decisions_b = reconcile_leg_b(payments, orders)
    journeys = build_journeys(orders, payments, decisions_a, decisions_b)
    a_dicts = [d.to_dict() for d in decisions_a]
    b_dicts = [d.to_dict() for d in decisions_b]

    # --- settlements past their expected credit date (queue enrichment) --------
    overdue = overdue_settlements(settlements, decisions_a, close_date)

    # --- the single queue -----------------------------------------------------
    items = triage.items_from_leg_a(a_dicts, overdue)
    items += triage.items_from_leg_b(b_dicts, orders, payments)
    queue = triage.sort_queue(items)

    # --- summaries ------------------------------------------------------------
    matched = [d for d in a_dicts if d["status"] in RS.MATCHED_FAMILY]
    # The brief's own metric, stated as one honest ratio: of the records this
    # loop is about (bank debits and unrelated inflows excluded), how many
    # were auto-reconciled and how many still need a human.
    in_scope = [d for d in a_dicts
                if d["status"] not in (RS.OUT_OF_SCOPE, RS.NON_SETTLEMENT_CREDIT)]
    unresolved = [d for d in in_scope if d["status"] not in RS.MATCHED_FAMILY]
    recon_summary = {
        "decisions": len(a_dicts),
        "in_scope": len(in_scope),
        "matched": len(matched),
        "match_rate": (round(len(matched) / len(in_scope), 4)
                       if in_scope else None),
        "unresolved": len(unresolved),
        "unresolved_by_status": dict(sorted(
            Counter(d["status"] for d in unresolved).items())),
        "matched_amount_paise": sum(d["received_paise"] or 0 for d in matched),
        "needs_review": sum(1 for d in matched
                            if d["confidence"] == RS.CONF_NEEDS_REVIEW),
        "by_status": dict(sorted(Counter(d["status"] for d in a_dicts).items())),
    }
    intact = sum(1 for j in journeys if not j["first_break"])
    leg_b_summary = {
        "by_status": dict(sorted(Counter(d["status"] for d in b_dicts).items())),
        "journeys_total": len(journeys),
        "journeys_intact": intact,
        "first_breaks": dict(sorted(Counter(
            j["first_break"] for j in journeys if j["first_break"]).items())),
    }

    # --- trust panel: the independent leg A verifier on this close's output ---
    v_leg_a = verify_leg_a(data_dir, a_dicts)
    verify_panel = {
        "leg_a": len(v_leg_a),
        "samples": v_leg_a[:10],
    }

    counts = {
        "queue_total": len(queue),
        "by_severity": {f"S{s}": sum(1 for i in queue if i.severity == s)
                        for s in (1, 2, 3)},
        "by_source": dict(sorted(Counter(i.source for i in queue).items())),
    }

    return DailyClose(
        close_date=close_date.isoformat(),
        world=os.path.basename(os.path.normpath(data_dir)),
        cash=cash, recon_summary=recon_summary, leg_b_summary=leg_b_summary,
        queue=queue, verify_panel=verify_panel, counts=counts,
        decisions_a=a_dicts, decisions_b=b_dicts, warnings=warnings,
    )


# --- markdown report ----------------------------------------------------------


def _item_line(i) -> str:
    ids = ";".join(i.record_ids)
    extra = ""
    if i.days_overdue is not None:
        extra += f" · {i.days_overdue}d overdue"
    if i.due_date and i.days_overdue is None:
        extra += f" · due {i.due_date}"
    return (f"- **[S{i.severity}] {i.title}** — `{ids}` · "
            f"{_inr(i.money_at_risk_paise)}{extra}\n  {i.suggested_action}")


def render_markdown(close: DailyClose) -> str:
    c = close
    panel = c.verify_panel
    sev = c.counts["by_severity"]
    r = c.recon_summary
    rate = ("n/a — no settlement-side records in this world"
            if r["match_rate"] is None else f"{r['match_rate']:.1%}")

    lines = [
        f"# Daily close — {c.close_date} · world {c.world}",
        "",
        "| Cash in bank | Auto-reconciled | Queue | Verifier violations |",
        "|---|---|---|---|",
        f"| {_inr(c.cash['balance_paise'])} | {r['matched']} of "
        f"{r['in_scope']} ({rate}) | {sev['S1']} S1 · {sev['S2']} S2 · "
        f"{sev['S3']} S3 | leg A {panel['leg_a']} |",
        "",
    ]
    for w in c.warnings:
        lines.append(f"> ⚠ {w}")
    if c.warnings:
        lines.append("")

    lines += ["## What needs a human today", ""]
    urgent = [i for i in c.queue if i.severity <= 2]
    if urgent:
        lines += [_item_line(i) for i in urgent]
    else:
        lines.append("Nothing at S1/S2 — the queue is review-only today.")
    s3 = [i for i in c.queue if i.severity == 3]
    if s3:
        lines += ["", f"### {len(s3)} review items (S3), grouped", "",
                  "| status | source | items | money |", "|---|---|---|---|"]
        groups: dict[tuple[str, str], list] = {}
        for i in s3:
            groups.setdefault((i.status, i.source), []).append(i)
        for (status, source), gi in sorted(groups.items()):
            lines.append(f"| {status} | {source} | {len(gi)} | "
                         f"{_inr(sum(x.money_at_risk_paise for x in gi))} |")

    lines += [
        "", "## Cash", "",
        f"- Closing balance: **{_inr(c.cash['balance_paise'])}** over "
        f"{c.cash['statement_rows']} statement rows since "
        f"{c.cash['first_statement_date']}",
    ]

    b = c.leg_b_summary
    lines += [
        "", "## Reconciliation, one pass", "",
        f"- **Match rate**: {r['matched']} of {r['in_scope']} in-scope records "
        f"auto-reconciled ({rate}); {r['unresolved']} could not be resolved "
        "automatically → queue"
        + (" (" + ", ".join(f"{k} {n}" for k, n in
                            r["unresolved_by_status"].items()) + ")"
           if r["unresolved_by_status"] else ""),
        f"- **Settlements ↔ bank**: {r['decisions']} decisions, "
        f"{_inr(r['matched_amount_paise'])} matched"
        + (f", {r['needs_review']} matched with unexplained residual"
           if r["needs_review"] else ""),
    ]
    for status, n in r["by_status"].items():
        lines.append(f"  - {status}: {n}")
    lines += [
        f"- **Payments ↔ orders**: {b['journeys_intact']}/"
        f"{b['journeys_total']} order journeys intact",
    ]
    for status, n in b["by_status"].items():
        lines.append(f"  - {status}: {n}")
    lines += [
        "", "## Why you can trust this page", "",
        f"- Leg A verifier on this exact output: {panel['leg_a']} "
        "violation(s). The verifier shares no code with the engine.",
        "- One pass: the full-world settlement reconciliation ran exactly "
        "once; leg B and the journeys reuse its decisions.",
        "- Deterministic: same world in, byte-identical close out. No "
        "wall-clock, no randomness, integer paise throughout.",
        "- Measured, not asserted: queue recall/precision vs minted ground "
        "truth is frozen in `reports/close_audit.md`.",
    ]
    return "\n".join(lines) + "\n"


def _merchants_rollup(registry_path: str) -> None:
    with open(registry_path, encoding="utf-8") as f:
        merchants = json.load(f)
    worst = 0
    for m in merchants:
        close = daily_close(m["data_dir"])
        sev = close.counts["by_severity"]
        violations = close.verify_panel["leg_a"]
        worst = max(worst, violations)
        print(f"{m['name']:<28} {close.close_date}  "
              f"cash {_inr(close.cash['balance_paise']):>16}  queue "
              f"{close.counts['queue_total']:>3} ({sev['S1']} S1) "
              f"{'· ' + str(violations) + ' VIOLATIONS' if violations else ''}")
    if worst:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Run the unified daily close over one world.")
    ap.add_argument("data_dir", nargs="?")
    ap.add_argument("--merchants", metavar="REGISTRY",
                    help="close every merchant in the registry JSON and "
                         "print a one-line rollup each")
    ap.add_argument("--as-of", dest="as_of", metavar="YYYY-MM-DD",
                    help="close the books as they stood on this date")
    ap.add_argument("--snapshot", action="store_true",
                    help="freeze this close's queue under data/state/ and "
                         "enable day-over-day deltas")
    ap.add_argument("--report", nargs="?", const="", default=None,
                    metavar="PATH",
                    help="write the markdown report (default path "
                         "reports/daily_close_<world>.md)")
    ap.add_argument("--json", nargs="?", const="", default=None,
                    metavar="PATH", dest="json_path",
                    help="write the full close as JSON, consumable by "
                         "controller.verify_close (default path "
                         "reports/daily_close_<world>.json)")
    args = ap.parse_args()

    if args.merchants:
        _merchants_rollup(args.merchants)
        return
    if not args.data_dir:
        ap.error("data_dir is required (or use --merchants)")

    as_of = (datetime.date.fromisoformat(args.as_of)
             if args.as_of else None)
    close = daily_close(args.data_dir, as_of=as_of)

    sev = close.counts["by_severity"]
    print(f"daily close {close.close_date} · world {close.world}")
    print(f"  cash {_inr(close.cash['balance_paise'])} · queue "
          f"{close.counts['queue_total']} ({sev['S1']} S1, {sev['S2']} S2, "
          f"{sev['S3']} S3)")
    panel = close.verify_panel
    print(f"  verifier: leg A {panel['leg_a']} violation(s)")
    for w in close.warnings:
        print(f"  warning: {w}")

    # day-over-day delta: compare against the latest snapshot BEFORE this
    # close date, then (optionally) freeze this close as the next baseline
    from controller import queue_state as QS
    from controller import snapshots as SNAP
    prev = SNAP.latest_before(args.data_dir, close.close_date)
    delta_info = (SNAP.delta(prev, close.to_dict(),
                             QS.load_state(args.data_dir))
                  if prev else None)
    if delta_info:
        print(f"  since {delta_info['prev_close_date']}: "
              f"{len(delta_info['new'])} new · "
              f"{len(delta_info['carried'])} carried · "
              f"{len(delta_info['gone'])} gone")
    if args.snapshot:
        print(f"  snapshot: {SNAP.write_snapshot(close.to_dict(), args.data_dir)}")

    if args.report is not None:
        path = args.report or os.path.join(
            _REPORTS_DIR, f"daily_close_{close.world}.md")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_markdown(close))
            if delta_info:
                f.write(SNAP.render_delta_md(delta_info))
        print(f"  report: {path}")

    if args.json_path is not None:
        path = args.json_path or os.path.join(
            _REPORTS_DIR, f"daily_close_{close.world}.json")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(close.to_dict(), f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"  json: {path}")

    if panel["leg_a"]:
        for s in panel["samples"]:
            print(f"  VIOLATION: {s}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
