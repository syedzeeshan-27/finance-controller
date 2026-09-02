"""The measured claim for the unified close: queue recall/precision vs the
minted ground truth. The ONLY controller module allowed to read the golden
files (the package canary test enforces this).

Metrics, per world:

  queue_recall     surfaced / total golden queue-worthy rows. A golden row is
                   queue-worthy when its expected disposition is in the
                   published queue policy; it is surfaced when the close's
                   queue holds an item with that exact status carrying the
                   row's record id. Target 1.000.
  money_recall     same, weighted by |expected_discrepancy_paise| over the
                   rows that carry one.
  queue_precision  justified items / (items - synthetics). An item is
                   justified when at least one golden row matches its status
                   and one of its record ids. Synthetics (needs_review, the
                   threshold breach) are policy overlays with no golden row
                   by construction and are excluded from the denominator.
  verify           for the daily_close row: verify_close violations — a hard
                   gate (exit 1). For the naive_engines contrast row: the
                   stage verifiers' violation counts, reported as evidence.

The contrast row feeds the SAME triage/queue layer with the naive Stage 1
and Stage 3 baselines (leg B has no baseline and keeps the real engine; the
forecaster contributes nothing gradable without a threshold, so it is
skipped there). An "empty queue" strawman would be performative; this
measures the honest thing — the controller surface is exactly as
trustworthy as the engines beneath it.

CLI:
    python -m controller.audit --seeds 42,43,44,45,46 --days 180
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time

from recon import io_load
from recon.baselines import naive_reconcile
from recon.generate import generate, seed_dir
from recon.leg_b import reconcile_leg_b
from recon.verify import verify_leg_a
from forecast import slicing
from tax.baselines import naive_tax
from tax.benchmark import load_golden_tax
from tax.io_tax import build_tax_input
from tax.verify import verify_tax
from controller import triage
from controller.close import daily_close
from controller.verify_close import verify_close

QUEUE_WORTHY = frozenset(triage.SEVERITY) - {
    triage.NEEDS_REVIEW, triage.FORECAST_BELOW_THRESHOLD}
SYNTHETICS = frozenset({triage.NEEDS_REVIEW, triage.FORECAST_BELOW_THRESHOLD})

_INPUT_FILES = ("settlements.csv", "bank_statement.csv", "payments.csv",
                "order_book.csv", "gstr2b.csv", "form26as.csv")


def _count_records(data_dir: str) -> int:
    n = 0
    for name in _INPUT_FILES:
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as f:
                n += sum(1 for _ in csv.DictReader(f))
    return n


def _load_goldens(data_dir: str) -> list[dict]:
    rows = io_load.load_golden(data_dir, "A") + io_load.load_golden(data_dir, "B")
    if os.path.exists(os.path.join(data_dir, "golden_tax.csv")):
        rows += load_golden_tax(data_dir)
    return rows


def grade_queue(queue: list[dict], golden: list[dict]) -> dict:
    """Recall over golden queue-worthy rows, precision over queue items."""
    by_status: dict[str, list[dict]] = {}
    for i in queue:
        by_status.setdefault(i["status"], []).append(i)

    def surfaced(status: str, record_id: str) -> bool:
        return any(record_id in i["record_ids"]
                   for i in by_status.get(status, []))

    worthy = [g for g in golden if g["expected_disposition"] in QUEUE_WORTHY]
    hits = sum(1 for g in worthy
               if surfaced(g["expected_disposition"], g["record_id"]))
    money_rows = [g for g in worthy if g["expected_discrepancy_paise"]]
    money_total = sum(abs(g["expected_discrepancy_paise"]) for g in money_rows)
    money_hit = sum(abs(g["expected_discrepancy_paise"]) for g in money_rows
                    if surfaced(g["expected_disposition"], g["record_id"]))

    golden_keys = {}
    for g in golden:
        golden_keys.setdefault(g["expected_disposition"], set()).add(
            g["record_id"])
    gradable = [i for i in queue if i["status"] not in SYNTHETICS]
    justified = sum(
        1 for i in gradable
        if any(rid in golden_keys.get(i["status"], ()) for rid in
               i["record_ids"]))

    missed = [
        {"status": g["expected_disposition"], "record_id": g["record_id"]}
        for g in worthy
        if not surfaced(g["expected_disposition"], g["record_id"])]
    return {
        "golden_queue_worthy": len(worthy),
        "surfaced": hits,
        "queue_recall": round(hits / len(worthy), 4) if worthy else None,
        "money_at_stake_paise": money_total,
        "money_surfaced_paise": money_hit,
        "money_recall": (round(money_hit / money_total, 4)
                         if money_total else None),
        "queue_items": len(queue),
        "gradable_items": len(gradable),
        "justified_items": justified,
        "queue_precision": (round(justified / len(gradable), 4)
                            if gradable else None),
        "missed": missed[:20],
    }


def audit_close(data_dir: str, horizon: int = 14) -> dict:
    """The daily_close product path, graded and independently verified."""
    t0 = time.perf_counter()
    close = daily_close(data_dir, horizon=horizon)
    elapsed = time.perf_counter() - t0
    close_dict = close.to_dict()
    result = grade_queue(close_dict["queue"], _load_goldens(data_dir))
    violations = verify_close(data_dir, close_dict)
    records = _count_records(data_dir)
    result.update({
        "by_severity": close_dict["counts"]["by_severity"],
        "verify_close_violations": violations[:25],
        "verify_violation_count": len(violations),
        "records_processed": records,
        "elapsed_seconds": round(elapsed, 4),
        "throughput_records_per_sec": (round(records / elapsed, 2)
                                       if elapsed > 0 else None),
    })
    return result


def naive_engines_queue(data_dir: str) -> tuple[list[dict], dict]:
    """The identical triage layer over the naive Stage 1 + Stage 3 baselines."""
    world = slicing.load_world(data_dir)
    orders = io_load.load_orders(data_dir)
    dec_a = naive_reconcile(world["settlements"], world["bank_rows"])
    dec_b = reconcile_leg_b(world["payments"], orders)
    a_dicts = [d.to_dict() for d in dec_a]
    b_dicts = [d.to_dict() for d in dec_b]
    items = triage.items_from_leg_a(a_dicts, attention=[])
    items += triage.items_from_leg_b(b_dicts, orders, world["payments"])
    stage_violations = {"leg_a": len(verify_leg_a(data_dir, a_dicts)),
                        "tax": None}
    if os.path.exists(os.path.join(data_dir, "gstr2b.csv")):
        tax_inp = build_tax_input(data_dir, decisions_a=dec_a,
                                  bank_rows=world["bank_rows"],
                                  settlements=world["settlements"],
                                  payments=world["payments"])
        tax_dicts = [d.to_dict() for d in naive_tax(tax_inp)]
        items += triage.items_from_tax(tax_dicts)
        stage_violations["tax"] = len(verify_tax(data_dir, tax_dicts))
    queue = [i.to_dict() for i in triage.sort_queue(items)]
    return queue, stage_violations


def audit_naive(data_dir: str) -> dict:
    queue, stage_violations = naive_engines_queue(data_dir)
    result = grade_queue(queue, _load_goldens(data_dir))
    result.update({
        "stage_verifier_violations": stage_violations,
        "verify_violation_count": ((stage_violations["leg_a"] or 0)
                                   + (stage_violations["tax"] or 0)),
    })
    return result


AGG_KEYS = ("queue_recall", "money_recall", "queue_precision",
            "verify_violation_count", "throughput_records_per_sec")


def _aggregate(rows: list[dict], key: str) -> dict:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {"mean": None, "min": None, "max": None}
    return {"mean": round(sum(vals) / len(vals), 4),
            "min": round(min(vals), 4), "max": round(max(vals), 4)}


def run_audit(seeds: list[int], days: int = 180,
              regenerate: bool = True) -> dict:
    for seed in seeds:
        data_dir = seed_dir(seed, days)
        if regenerate or not os.path.exists(
                os.path.join(data_dir, "golden_tax.csv")):
            generate(seed, data_dir, days)
    strategies: dict[str, dict] = {}
    for name, fn in (("daily_close", audit_close), ("naive_engines", audit_naive)):
        per_seed = {str(seed): fn(seed_dir(seed, days)) for seed in seeds}
        rows = list(per_seed.values())
        agg = {k: _aggregate(rows, k) for k in AGG_KEYS}
        agg["golden_queue_worthy_total"] = sum(
            r["golden_queue_worthy"] for r in rows)
        agg["surfaced_total"] = sum(r["surfaced"] for r in rows)
        agg["money_at_stake_total_paise"] = sum(
            r["money_at_stake_paise"] for r in rows)
        agg["money_surfaced_total_paise"] = sum(
            r["money_surfaced_paise"] for r in rows)
        agg["verify_violation_total"] = sum(
            r["verify_violation_count"] for r in rows)
        strategies[name] = {"per_seed": per_seed, "aggregate": agg}
    return {"seeds": seeds, "days": days, "strategies": strategies}


def _rs(paise: int) -> str:
    return f"Rs {paise // 100:,}"


def write_markdown_report(results: dict, path: str) -> None:
    lines = ["# Daily-close audit", ""]
    lines.append(
        f"Seeds {results['seeds']} on {results['days']}-day worlds. Every "
        "golden record whose expected disposition is queue-worthy under the "
        "published triage policy must surface in the unified queue under "
        "that exact status; every non-synthetic queue item must be "
        "justified by a golden row. The daily_close row is additionally "
        "gated by verify_close (independent re-typed policy + all three "
        "stage verifiers re-run on the embedded decisions); the "
        "naive_engines row feeds the SAME triage layer with the naive "
        "Stage 1 + Stage 3 baselines, and its stage-verifier violation "
        "counts are reported as evidence.")
    lines.append("")
    lines.append("| strategy | queue recall | money recall | queue precision "
                 "| money at stake | verify violations |")
    lines.append("|---|---|---|---|---|---|")
    for name, block in results["strategies"].items():
        agg = block["aggregate"]
        lines.append(
            f"| {name} "
            f"| {agg['surfaced_total']}/{agg['golden_queue_worthy_total']} "
            f"({agg['queue_recall']['mean']:.1%}) "
            f"| {agg['money_recall']['mean']:.1%} "
            f"| {agg['queue_precision']['mean']:.1%} "
            f"| {_rs(agg['money_at_stake_total_paise'])}, surfaced "
            f"{_rs(agg['money_surfaced_total_paise'])} "
            f"| {agg['verify_violation_total']} |")
    lines.append("")
    close_agg = results["strategies"]["daily_close"]["aggregate"]
    if close_agg["throughput_records_per_sec"]["mean"]:
        lines.append(
            f"One full daily close (all three loops, one pass, verifiers "
            f"included) processes ~"
            f"{int(close_agg['throughput_records_per_sec']['mean']):,} "
            "records/second.")
        lines.append("")
    lines.append(
        "The queue never sees the golden files (package canary test); the "
        "synthetics excluded from precision are `needs_review` — a "
        "confidence overlay on matched decisions — and the threshold-breach "
        "item, which exists only relative to a user-chosen threshold.")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", default="42,43,44,45,46")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--report-dir", default="reports")
    ap.add_argument("--no-regenerate", action="store_true")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    results = run_audit(seeds, args.days, regenerate=not args.no_regenerate)

    os.makedirs(args.report_dir, exist_ok=True)
    with open(os.path.join(args.report_dir, "close_audit.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    write_markdown_report(results,
                          os.path.join(args.report_dir, "close_audit.md"))

    print(f"seeds: {seeds} ({args.days}-day worlds)")
    header = (f"{'strategy':<14} {'recall':>7} {'money':>7} {'precision':>10} "
              f"{'verify':>7}")
    print(header)
    print("-" * len(header))
    for name, block in results["strategies"].items():
        agg = block["aggregate"]
        print(f"{name:<14} {agg['queue_recall']['mean']:>7.4f} "
              f"{agg['money_recall']['mean']:>7.4f} "
              f"{agg['queue_precision']['mean']:>10.4f} "
              f"{agg['verify_violation_total']:>7}")
    print(f"results written to "
          f"{os.path.join(args.report_dir, 'close_audit.json')}")
    engine_violations = results["strategies"]["daily_close"]["aggregate"][
        "verify_violation_total"]
    if engine_violations:
        raise SystemExit(
            f"FAIL: verify_close found {engine_violations} violations in "
            "the daily_close output")


if __name__ == "__main__":
    main()
