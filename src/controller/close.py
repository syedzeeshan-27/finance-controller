"""The unified daily close: three loops, one pass, one queue.

`daily_close` loads a world once, runs the full-world Stage 1 reconciliation
exactly once, and hands those decisions to leg B, the journey tracer, the tax
loops and the cash forecaster through their injection seams. Every decision
that needs a human lands in one severity-ranked queue (policy in
controller.triage); the three stage verifiers run on the same outputs and
their counts ship inside the close as the trust panel.

Injection is guarded, not assumed: the forecast input sliced at the close
date must contain exactly the records the full-world run saw (true whenever
nothing is created after the last statement date). If it ever differs, the
close falls back to letting the forecaster reconcile its own slice and says
so in `warnings` — correctness always, one-pass in practice.

CLI:  python -m controller.close <data_dir> [--horizon N]
          [--threshold-lakh X] [--report [PATH]] [--json [PATH]]
          [--as-of YYYY-MM-DD] [--snapshot]
      python -m controller.close --merchants data/merchants.json
Exit code 1 when any stage verifier reports a violation.

`--as-of` closes the books as they stood on an earlier date, slicing the
world through the forecast's leakage wall (filed-side tax files are not
time-sliced; day-over-day deltas are unaffected because both days see the
same filings). `--snapshot` freezes the queue under data/state/; when a
prior snapshot exists, the report gains a "Since last close" delta keyed
by stable item ids.
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
from forecast import slicing
from forecast.forecaster import forecast as run_forecast
from forecast.verify import verify_result
from tax import schemas as TS
from tax.engine import reconcile_tax
from tax.io_tax import build_tax_input
from tax.verify import verify_tax
from controller import triage
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


def _claim_amount(d: dict) -> int:
    sides = [x for x in (d["books_paise"], d["filed_paise"]) if x is not None]
    return min(sides) if sides else 0


def _tax_summary(tax_dicts: list[dict], purchases=None) -> dict:
    itc = [d for d in tax_dicts if d["loop"] == "itc"]
    tds = [d for d in tax_dicts if d["loop"] == "tds"]
    obl = [d for d in tax_dicts if d["loop"] == "obligation"]
    split_by_id = {p.purchase_id: p.head_split for p in (purchases or [])
                   if p.head_split}
    by_head = {"igst_paise": 0, "cgst_paise": 0, "sgst_paise": 0}
    for d in itc:
        if d["status"] in TS.CLAIMABLE_NOW:
            for bid in d["book_ids"]:
                for k, v in (split_by_id.get(bid) or {}).items():
                    by_head[k] += v
    return {
        "itc_claimable_now_paise": sum(_claim_amount(d) for d in itc
                                       if d["status"] in TS.CLAIMABLE_NOW),
        "itc_claimable_by_head": by_head,   # books-side split (published rule)
        "itc_deferred_paise": sum(_claim_amount(d) for d in itc
                                  if d["status"] == TS.ITC_DEFERRED_NEXT_PERIOD),
        "itc_at_risk_paise": sum(-(d["discrepancy_paise"] or 0) for d in itc
                                 if d["status"] == TS.ITC_MISSING_IN_2B),
        "itc_blocked_paise": sum(d["books_paise"] or 0 for d in itc
                                 if d["status"] == TS.BLOCKED_CREDIT_NO_ITC),
        "tds_matched": sum(1 for d in tds
                           if d["status"] == TS.TDS_CREDIT_MATCHED),
        "tds_total": sum(1 for d in tds
                         if d["book_ids"] and d["status"] != TS.TDS_DUPLICATE_26AS),
        "obligations_on_time": sum(1 for d in obl
                                   if d["status"] == TS.PAID_ON_TIME),
        "obligations_total": len(obl),
        "by_status": dict(sorted(Counter(d["status"] for d in tax_dicts).items())),
    }


_SLICE_RULES = {
    # canonical file -> (date column, parser) for the as-of cut; None copies
    # the file verbatim (filed-side tax data is published monthly and is not
    # time-sliced — both days of a delta see the same filings)
    "bank_statement.csv": ("value_date", "bank"),
    "settlements.csv": ("created_at", "iso"),
    "payments.csv": ("created_at", "iso"),
    "order_book.csv": ("created_at", "iso"),
    "gstr2b.csv": None,
    "form26as.csv": None,
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
        if rule is not None:
            column, kind = rule
            parse = parse_bank_date if kind == "bank" else parse_iso_date
            rows = [r for r in rows if parse(r[column]) <= as_of]
        with open(os.path.join(out_dir, name), "w", encoding="utf-8",
                  newline="") as f:
            w = _csv.DictWriter(f, fieldnames=fieldnames,
                                lineterminator="\n")
            w.writeheader()
            w.writerows(rows)


def daily_close(data_dir: str, horizon: int = 14,
                threshold_paise: int | None = None,
                as_of=None) -> DailyClose:
    if as_of is not None:
        # the close you WOULD have run that day: slice the raw files into a
        # temp world and run the unmodified close over it
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="asof_")
        try:
            _slice_world_files(data_dir, tmp, as_of)
            close = daily_close(tmp, horizon=horizon,
                                threshold_paise=threshold_paise)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        close.world = os.path.basename(os.path.normpath(data_dir))
        return close

    warnings: list[str] = []
    world = slicing.load_world(data_dir)
    orders = io_load.load_orders(data_dir)
    bank_rows = world["bank_rows"]
    settlements = world["settlements"]
    payments = world["payments"]

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

    # --- forecast over the same records, decisions injected when valid --------
    inp = slicing.build_input(world, close_date)
    injectable = (
        {s.settlement_id for s in inp.settlements}
        == {s.settlement_id for s in settlements}
        and {p["payment_id"] for p in inp.payments}
        == {p["payment_id"] for p in payments}
        and len(inp.bank_rows) == len(bank_rows))
    if not injectable:
        warnings.append("records exist past the last statement date; the "
                        "forecaster re-reconciled its own slice")
    fc = run_forecast(inp, horizon, threshold_paise=threshold_paise,
                      decisions=decisions_a if injectable else None)
    forecast_dict = fc.to_dict()

    # --- tax loops, when the world has filed-side data ------------------------
    tax_dicts: list[dict] | None = None
    tax_purchases = None
    if os.path.exists(os.path.join(data_dir, "gstr2b.csv")):
        tax_inp = build_tax_input(data_dir, decisions_a=decisions_a,
                                  bank_rows=bank_rows, settlements=settlements,
                                  payments=payments)
        tax_purchases = tax_inp.purchases
        tax_dicts = [d.to_dict() for d in reconcile_tax(tax_inp)]
    else:
        warnings.append("world predates the tax stage (no gstr2b.csv): "
                        "tax loops skipped")

    # --- the single queue -----------------------------------------------------
    items = triage.items_from_leg_a(a_dicts, forecast_dict["attention"])
    items += triage.items_from_leg_b(b_dicts, orders, payments)
    if tax_dicts is not None:
        items += triage.items_from_tax(tax_dicts)
    synthetic = triage.threshold_item(forecast_dict)
    if synthetic is not None:
        items.append(synthetic)
    queue = triage.sort_queue(items)

    # --- summaries ------------------------------------------------------------
    matched = [d for d in a_dicts if d["status"] in RS.MATCHED_FAMILY]
    recon_summary = {
        "decisions": len(a_dicts),
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

    # --- trust panel: the three stage verifiers on this close's outputs -------
    v_leg_a = verify_leg_a(data_dir, a_dicts)
    v_tax = verify_tax(data_dir, tax_dicts) if tax_dicts is not None else None
    v_forecast = verify_result(forecast_dict)
    verify_panel = {
        "leg_a": len(v_leg_a),
        "tax": len(v_tax) if v_tax is not None else None,
        "forecast": len(v_forecast),
        "samples": (v_leg_a + (v_tax or []) + v_forecast)[:10],
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
        tax_summary=(_tax_summary(tax_dicts, tax_purchases)
                     if tax_dicts is not None else None),
        forecast=forecast_dict, queue=queue, verify_panel=verify_panel,
        counts=counts, decisions_a=a_dicts, decisions_b=b_dicts,
        tax_decisions=tax_dicts, warnings=warnings,
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
    mb = c.forecast["min_balance"]
    band = ("—" if mb.get("lo80") is None
            else f"{_inr(mb['lo80'])} … {_inr(mb['hi80'])}")
    itc = (_inr(c.tax_summary["itc_claimable_now_paise"])
           if c.tax_summary else "n/a")
    panel = c.verify_panel
    tax_v = "n/a" if panel["tax"] is None else str(panel["tax"])
    sev = c.counts["by_severity"]

    lines = [
        f"# Daily close — {c.close_date} · world {c.world}",
        "",
        "| Cash in bank | Min balance (next "
        f"{c.forecast['horizon']}d) | ITC claimable now | Queue | Verifier "
        "violations |",
        "|---|---|---|---|---|",
        f"| {_inr(c.cash['balance_paise'])} | {_inr(mb['paise'])} on "
        f"{mb['date']} | {itc} | {sev['S1']} S1 · {sev['S2']} S2 · "
        f"{sev['S3']} S3 | leg A {panel['leg_a']} · tax {tax_v} · "
        f"forecast {panel['forecast']} |",
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

    fc = c.forecast
    known = sum(x["amount_paise"] for x in fc["in_flight"])
    obligations = sum(o["amount_paise"] for o in fc["obligations"])
    lines += [
        "", f"## Cash and the next {fc['horizon']} days", "",
        f"- Closing balance: **{_inr(c.cash['balance_paise'])}** over "
        f"{c.cash['statement_rows']} statement rows since "
        f"{c.cash['first_statement_date']}",
        f"- Projected minimum: **{_inr(mb['paise'])}** on {mb['date']} "
        f"(80% band {band})",
        f"- First day below threshold: "
        f"{fc['first_below_threshold'] or '—'}"
        + (f" (threshold {_inr(fc['threshold_paise'])})"
           if fc.get("threshold_paise") is not None else ""),
        f"- Known in-flight inflows: {_inr(known)} across "
        f"{len(fc['in_flight'])} settlements/batches",
        f"- Upcoming recurring obligations: {_inr(obligations)} across "
        f"{len(fc['obligations'])} due dates",
    ]
    for w in fc["warnings"]:
        lines.append(f"- Forecast note: {w}")

    r = c.recon_summary
    b = c.leg_b_summary
    lines += [
        "", "## Three loops, one pass", "",
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
    if c.tax_summary:
        t = c.tax_summary
        lines += [
            "- **Tax loops**: "
            f"ITC claimable now {_inr(t['itc_claimable_now_paise'])} · "
            f"deferred {_inr(t['itc_deferred_paise'])} · "
            f"at risk {_inr(t['itc_at_risk_paise'])} · "
            f"blocked (do not claim) {_inr(t['itc_blocked_paise'])} · "
            f"TDS credits {t['tds_matched']}/{t['tds_total']} · "
            f"obligations on time {t['obligations_on_time']}/"
            f"{t['obligations_total']}",
        ]
        heads = t.get("itc_claimable_by_head")
        if heads:
            lines.append(
                f"  - claimable by head (books-side split): IGST "
                f"{_inr(heads['igst_paise'])} · CGST "
                f"{_inr(heads['cgst_paise'])} · SGST "
                f"{_inr(heads['sgst_paise'])}")
    else:
        lines.append("- **Tax loops**: n/a — world predates the tax stage")

    lines += [
        "", "## Why you can trust this page", "",
        f"- Stage verifiers on this exact output: leg A {panel['leg_a']} "
        f"violation(s) · tax {tax_v} · forecast {panel['forecast']}. The "
        "verifiers share no code with the engines.",
        "- One pass: the full-world settlement reconciliation ran exactly "
        "once and was injected into the tax and forecast surfaces "
        "(band calibration re-runs historical slices by design).",
        "- Deterministic: same world in, byte-identical close out. No "
        "wall-clock, no randomness, integer paise throughout.",
        "- Measured, not asserted: queue recall/precision vs minted ground "
        "truth is frozen in `reports/close_audit.md`.",
    ]
    return "\n".join(lines) + "\n"


def _merchants_rollup(registry_path: str, horizon: int) -> None:
    with open(registry_path, encoding="utf-8") as f:
        merchants = json.load(f)
    worst = 0
    for m in merchants:
        close = daily_close(m["data_dir"], horizon=horizon)
        sev = close.counts["by_severity"]
        panel = close.verify_panel
        violations = panel["leg_a"] + (panel["tax"] or 0) + panel["forecast"]
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
    ap.add_argument("--horizon", type=int, default=14)
    ap.add_argument("--threshold-lakh", type=float, default=None,
                    help="working-capital floor in lakh rupees (e.g. 3)")
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
        _merchants_rollup(args.merchants, args.horizon)
        return
    if not args.data_dir:
        ap.error("data_dir is required (or use --merchants)")

    threshold = (int(args.threshold_lakh * 10_000_000)
                 if args.threshold_lakh is not None else None)
    as_of = (datetime.date.fromisoformat(args.as_of)
             if args.as_of else None)
    close = daily_close(args.data_dir, horizon=args.horizon,
                        threshold_paise=threshold, as_of=as_of)

    sev = close.counts["by_severity"]
    print(f"daily close {close.close_date} · world {close.world}")
    print(f"  cash {_inr(close.cash['balance_paise'])} · queue "
          f"{close.counts['queue_total']} ({sev['S1']} S1, {sev['S2']} S2, "
          f"{sev['S3']} S3)")
    panel = close.verify_panel
    print(f"  verifiers: leg A {panel['leg_a']} · tax "
          f"{'n/a' if panel['tax'] is None else panel['tax']} · "
          f"forecast {panel['forecast']}")
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

    violations = panel["leg_a"] + (panel["tax"] or 0) + panel["forecast"]
    if violations:
        for s in panel["samples"]:
            print(f"  VIOLATION: {s}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
