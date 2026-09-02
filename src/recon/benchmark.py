"""Benchmark: grades any strategy's decisions against the golden files.

Grading is per golden row (every settlement and every bank credit is graded
exactly once). Nothing here consults the engine's own scoring — ground truth
comes solely from generation-time labels.

Correctness rules per golden disposition:
  - matched family: engine status must EQUAL the golden disposition AND the
    linked counterparty set must equal the golden set exactly. Reporting a
    short-paid credit as cleanly matched, or matching 2 of 3 split parts, is
    wrong. A wrong match counts as both a false positive and a missed match
    (standard for matching tasks; noted in the report footnotes).
  - ambiguous_abstain / duplicate_credit: status must match and the decision's
    candidate list must cover the golden counterparties (the reviewer must be
    shown the right candidates).
  - missing/noise exceptions: status must match.

CLI:
    python -m recon.benchmark --seeds 42,43,44,45,46 [--report-dir reports]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time

from recon import schemas as S
from recon import io_load
from recon.baselines import STRATEGIES as BASELINES
from recon.engine import reconcile_leg_a
from recon.explain import llm_mode
from recon.generate import generate
from recon.leg_b import reconcile_leg_b
from recon.verify import verify_leg_a

ALL_STRATEGIES = dict(BASELINES)
ALL_STRATEGIES["recon_engine"] = reconcile_leg_a

UNREPORTED = "unreported"


# --- Grading ------------------------------------------------------------------

def _assignments(decisions: list[S.Decision]) -> dict[tuple[str, str], S.Decision]:
    """Map every referenced record to its (single) deciding Decision."""
    out: dict[tuple[str, str], S.Decision] = {}
    for d in decisions:
        for sid in d.settlement_ids:
            out.setdefault((S.RT_SETTLEMENT, sid), d)
        for tid in d.bank_txn_ids:
            out.setdefault((S.RT_BANK_CREDIT, tid), d)
    return out


def _counterparties(d: S.Decision, record_type: str) -> frozenset[str]:
    if record_type == S.RT_SETTLEMENT:
        return frozenset(d.bank_txn_ids)
    return frozenset(d.settlement_ids)


def _candidate_ids(d: S.Decision) -> frozenset[str]:
    return frozenset(c.get("record_id", "") for c in d.candidates)


def grade_leg_a(decisions: list[S.Decision], golden: list[dict]) -> dict:
    assign = _assignments(decisions)
    rows = []
    tp = fp = fn = 0
    n_correct = 0
    per_scenario: dict[str, list[bool]] = {}
    per_class_exceptions: dict[str, list[bool]] = {}
    ambiguous_total = ambiguous_correct = 0
    auto_resolved = 0

    for g in golden:
        d = assign.get((g["record_type"], g["record_id"]))
        status = d.status if d else UNREPORTED
        expected = g["expected_disposition"]
        golden_set = g["counterparty_set"]

        if expected in S.MATCHED_FAMILY:
            correct = (d is not None and status == expected
                       and _counterparties(d, g["record_type"]) == golden_set)
            if correct:
                tp += 1
            else:
                fn += 1
                if d is not None and status in S.MATCHED_FAMILY:
                    fp += 1  # wrongly linked / wrongly classified match
        elif expected in (S.AMBIGUOUS_ABSTAIN, S.DUPLICATE_CREDIT):
            correct = (d is not None and status == expected
                       and _candidate_ids(d) >= golden_set)
            if d is not None and status in S.MATCHED_FAMILY:
                fp += 1  # forced a match where none was defensible
            if expected == S.AMBIGUOUS_ABSTAIN:
                ambiguous_total += 1
                ambiguous_correct += int(correct)
        else:  # missing/noise exceptions
            correct = d is not None and status == expected
            if d is not None and status in S.MATCHED_FAMILY:
                fp += 1

        if expected not in S.MATCHED_FAMILY:
            per_class_exceptions.setdefault(expected, []).append(correct)
        n_correct += int(correct)
        per_scenario.setdefault(g["scenario_tag"], []).append(correct)

        if d is not None and (
                (status in S.MATCHED_FAMILY and d.confidence in
                 (S.CONF_EXACT, S.CONF_HIGH, S.CONF_MEDIUM))
                or status == S.NON_SETTLEMENT_CREDIT):
            auto_resolved += 1

        rows.append({
            "record_type": g["record_type"], "record_id": g["record_id"],
            "scenario": g["scenario_tag"], "expected": expected,
            "got": status, "correct": correct,
        })

    n = len(golden)
    n_matchable = sum(1 for g in golden if g["expected_disposition"] in S.MATCHED_FAMILY)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / n_matchable if n_matchable else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision not in (None, 0) and recall not in (None, 0) else 0.0)

    return {
        "golden_rows": n,
        "matchable_rows": n_matchable,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4),
        "false_match_rate": round(1 - precision, 4) if precision is not None else None,
        "disposition_accuracy": round(n_correct / n, 4) if n else None,
        "ambiguous_total": ambiguous_total,
        "ambiguous_correctly_abstained": ambiguous_correct,
        "auto_resolution_rate": round(auto_resolved / n, 4) if n else None,
        "exception_accuracy_by_class": {
            k: {"correct": sum(v), "total": len(v),
                "accuracy": round(sum(v) / len(v), 4)}
            for k, v in sorted(per_class_exceptions.items())},
        "per_scenario": {
            k: {"correct": sum(v), "total": len(v),
                "accuracy": round(sum(v) / len(v), 4)}
            for k, v in sorted(per_scenario.items())},
        "row_details": rows,
    }


def grade_leg_b(decisions: list[S.LegBDecision], golden: list[dict]) -> dict:
    """Leg B grading: per golden row, status AND counterparty set must match."""
    by_id = {(d.record_type, d.record_id): d for d in decisions}
    n_correct = 0
    per_scenario: dict[str, list[bool]] = {}
    per_class_exceptions: dict[str, list[bool]] = {}
    for g in golden:
        d = by_id.get((g["record_type"], g["record_id"]))
        correct = (d is not None and d.status == g["expected_disposition"]
                   and frozenset(d.counterparty_ids) == g["counterparty_set"])
        n_correct += int(correct)
        per_scenario.setdefault(g["scenario_tag"], []).append(correct)
        if g["expected_disposition"].startswith("exception_"):
            per_class_exceptions.setdefault(g["expected_disposition"], []).append(correct)
    n = len(golden)
    return {
        "golden_rows": n,
        "disposition_accuracy": round(n_correct / n, 4) if n else None,
        "exception_accuracy_by_class": {
            k: {"correct": sum(v), "total": len(v),
                "accuracy": round(sum(v) / len(v), 4)}
            for k, v in sorted(per_class_exceptions.items())},
        "per_scenario": {
            k: {"correct": sum(v), "total": len(v),
                "accuracy": round(sum(v) / len(v), 4)}
            for k, v in sorted(per_scenario.items())},
    }


# --- Running ------------------------------------------------------------------

def run_leg_b(data_dir: str) -> dict:
    t0 = time.perf_counter()
    payments = io_load.load_payments(data_dir)
    orders = io_load.load_orders(data_dir)
    decisions = reconcile_leg_b(payments, orders)
    elapsed = time.perf_counter() - t0
    graded = grade_leg_b(decisions, io_load.load_golden(data_dir, "B"))
    records = len(payments) + len(orders)
    graded.update({
        "records_processed": records,
        "elapsed_seconds": round(elapsed, 4),
        "throughput_records_per_sec": round(records / elapsed, 1) if elapsed else None,
    })
    return graded


def run_strategy(name: str, data_dir: str) -> dict:
    """Load, reconcile, grade and independently verify one strategy on one
    generated world. Timing covers parse + match (the work a real batch does),
    not grading or report writing."""
    strategy = ALL_STRATEGIES[name]

    t0 = time.perf_counter()
    settlements = io_load.load_settlements(data_dir)
    bank_rows = io_load.load_bank_rows(data_dir)
    decisions = strategy(settlements, bank_rows)
    elapsed = time.perf_counter() - t0

    golden = io_load.load_golden(data_dir, "A")
    graded = grade_leg_a(decisions, golden)
    violations = verify_leg_a(data_dir, [d.to_dict() for d in decisions])

    records = len(settlements) + len(bank_rows)
    graded.update({
        "records_processed": records,
        "elapsed_seconds": round(elapsed, 4),
        "throughput_records_per_sec": round(records / elapsed, 1) if elapsed else None,
        "verify_violations": violations,
        "verify_violation_count": len(violations),
    })
    return graded


def _aggregate(per_seed: dict[int, dict], key: str) -> dict | None:
    vals = [m[key] for m in per_seed.values() if m.get(key) is not None]
    if not vals:
        return None
    return {"mean": round(statistics.mean(vals), 4),
            "min": round(min(vals), 4), "max": round(max(vals), 4)}


AGG_KEYS = ["precision", "recall", "f1", "false_match_rate",
            "disposition_accuracy", "auto_resolution_rate",
            "throughput_records_per_sec"]


def run_benchmark(seeds: list[int], strategies: list[str] | None = None,
                  regenerate: bool = True) -> dict:
    strategies = strategies or list(ALL_STRATEGIES)
    worlds: dict[int, dict] = {}
    for seed in seeds:
        data_dir = os.path.join("data", "seeds", str(seed))
        if regenerate or not os.path.exists(os.path.join(data_dir, "golden_manifest.json")):
            generate(seed, data_dir)
        with open(os.path.join(data_dir, "golden_manifest.json"), encoding="utf-8") as f:
            worlds[seed] = json.load(f)

    results: dict[str, dict] = {}
    for name in strategies:
        per_seed: dict[int, dict] = {}
        for seed in seeds:
            data_dir = os.path.join("data", "seeds", str(seed))
            graded = run_strategy(name, data_dir)
            if name == "recon_engine" and graded["verify_violation_count"]:
                raise SystemExit(
                    f"verify.py found {graded['verify_violation_count']} invariant "
                    f"violations in the engine's output on seed {seed}:\n"
                    + "\n".join(f"  - {v}" for v in graded["verify_violations"][:10]))
            per_seed[seed] = graded
        results[name] = {
            "per_seed": {str(k): {kk: vv for kk, vv in v.items() if kk != "row_details"}
                         for k, v in per_seed.items()},
            "row_details_seed": str(seeds[0]),
            "row_details": per_seed[seeds[0]]["row_details"],
            "aggregate": {k: _aggregate(per_seed, k) for k in AGG_KEYS},
        }

    leg_b_per_seed = {
        str(seed): run_leg_b(os.path.join("data", "seeds", str(seed)))
        for seed in seeds}

    return {
        "seeds": seeds,
        "world_manifests": {str(k): v for k, v in worlds.items()},
        "strategies": results,
        "leg_b": {
            "per_seed": leg_b_per_seed,
            "aggregate": {
                "disposition_accuracy": _aggregate(
                    {int(k): v for k, v in leg_b_per_seed.items()},
                    "disposition_accuracy"),
                "throughput_records_per_sec": _aggregate(
                    {int(k): v for k, v in leg_b_per_seed.items()},
                    "throughput_records_per_sec"),
            },
        },
        "llm_mode": llm_mode(),
        "notes": [
            "Ground truth is generated deterministically at data-generation time; "
            "no LLM grades anything anywhere in this benchmark.",
            "The matching decisions themselves are fully deterministic; the LLM "
            "(when configured) only rephrases display-only explanations.",
            "A wrong match counts as both a false positive and a missed match.",
            "Timing covers CSV parse + matching, excludes grading/report writing.",
        ],
    }


# --- Markdown report ----------------------------------------------------------

def _fmt(v, pct: bool = False):
    if v is None:
        return "-"
    if isinstance(v, dict):  # aggregate {mean,min,max}
        if pct:
            return f"{v['mean']:.1%} ({v['min']:.1%}-{v['max']:.1%})"
        return f"{v['mean']:,.0f} ({v['min']:,.0f}-{v['max']:,.0f})"
    return f"{v:.1%}" if pct else f"{v:,}"


def write_markdown_report(results: dict, path: str) -> None:
    seeds = results["seeds"]
    lines: list[str] = []
    add = lines.append
    add("# Reconciliation Benchmark Report")
    add("")
    add(f"Seeds: {seeds} | leg A grading unit: one golden row per settlement "
        f"and per bank credit | LLM mode: {results['llm_mode']}")
    add("")
    m0 = results["world_manifests"][str(seeds[0])]["counts"]
    add(f"Per-seed world (seed {seeds[0]}): {m0['payments']} payments, "
        f"{m0['settlements']} settlements, {m0['bank_rows']} bank statement rows "
        f"({m0['bank_credits']} credits / {m0['bank_debits']} debits), "
        f"{m0['orders']} orders. Scenario mix is recorded in each world's "
        f"`golden_manifest.json`.")
    add("")
    add("## Leg A: settlement <-> bank credit (mean over seeds, min-max in brackets)")
    add("")
    add("| strategy | precision | recall | F1 | false-match rate | disposition accuracy | correct abstentions | auto-resolved | records/sec | invariant violations |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for name, r in results["strategies"].items():
        agg = r["aggregate"]
        amb_c = sum(r["per_seed"][str(s)]["ambiguous_correctly_abstained"] for s in seeds)
        amb_t = sum(r["per_seed"][str(s)]["ambiguous_total"] for s in seeds)
        vio = sum(r["per_seed"][str(s)]["verify_violation_count"] for s in seeds)
        add(f"| {name} | {_fmt(agg['precision'], pct=True)} "
            f"| {_fmt(agg['recall'], pct=True)} | {_fmt(agg['f1'], pct=True)} "
            f"| {_fmt(agg['false_match_rate'], pct=True)} "
            f"| {_fmt(agg['disposition_accuracy'], pct=True)} "
            f"| {amb_c}/{amb_t} | {_fmt(agg['auto_resolution_rate'], pct=True)} "
            f"| {_fmt(agg['throughput_records_per_sec'])} | {vio} |")
    add("")
    add(f"## Leg A per-scenario disposition accuracy (seed {seeds[0]})")
    add("")
    strategies = list(results["strategies"])
    add("| scenario | golden rows | " + " | ".join(strategies) + " |")
    add("|---|---|" + "|".join(["---"] * len(strategies)) + "|")
    scenarios = results["strategies"][strategies[0]]["per_seed"][str(seeds[0])]["per_scenario"]
    for tag in sorted(scenarios):
        cells = []
        total = scenarios[tag]["total"]
        for name in strategies:
            sc = results["strategies"][name]["per_seed"][str(seeds[0])]["per_scenario"].get(tag)
            cells.append(f"{sc['accuracy']:.0%}" if sc else "-")
        add(f"| {tag} | {total} | " + " | ".join(cells) + " |")
    add("")
    add("## Leg B: payment <-> order book")
    add("")
    lb = results["leg_b"]["aggregate"]
    add(f"Disposition accuracy: {_fmt(lb['disposition_accuracy'], pct=True)} | "
        f"throughput: {_fmt(lb['throughput_records_per_sec'])} records/sec")
    lb0 = results["leg_b"]["per_seed"][str(seeds[0])]
    add("")
    add(f"| leg B exception class (seed {seeds[0]}) | correct | total |")
    add("|---|---|---|")
    for k, v in lb0["exception_accuracy_by_class"].items():
        add(f"| {k} | {v['correct']} | {v['total']} |")
    add("")
    add("## Notes")
    add("")
    for n in results["notes"]:
        add(f"- {n}")
    add("")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", default="42",
                    help="comma-separated seeds, e.g. 42,43,44,45,46")
    ap.add_argument("--strategies", default=None,
                    help=f"comma-separated subset of {list(ALL_STRATEGIES)}")
    ap.add_argument("--report-dir", default="reports")
    ap.add_argument("--no-regenerate", action="store_true")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    strategies = args.strategies.split(",") if args.strategies else None
    results = run_benchmark(seeds, strategies, regenerate=not args.no_regenerate)

    os.makedirs(args.report_dir, exist_ok=True)
    out_path = os.path.join(args.report_dir, "benchmark_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    report_path = os.path.join(args.report_dir, "benchmark_report.md")
    write_markdown_report(results, report_path)

    # console summary
    print(f"seeds: {seeds}")
    header = f"{'strategy':<16} {'precision':>9} {'recall':>7} {'f1':>7} " \
             f"{'disp.acc':>8} {'abstain':>8} {'rec/s':>10} {'verify':>7}"
    print(header)
    print("-" * len(header))
    for name, r in results["strategies"].items():
        agg = r["aggregate"]
        first_seed = str(seeds[0])
        ps = r["per_seed"][first_seed]
        amb = f"{ps['ambiguous_correctly_abstained']}/{ps['ambiguous_total']}"
        vio = sum(r["per_seed"][str(s)]["verify_violation_count"] for s in seeds)
        print(f"{name:<16} "
              f"{(agg['precision'] or {}).get('mean', '-'):>9} "
              f"{(agg['recall'] or {}).get('mean', '-'):>7} "
              f"{(agg['f1'] or {}).get('mean', '-'):>7} "
              f"{(agg['disposition_accuracy'] or {}).get('mean', '-'):>8} "
              f"{amb:>8} "
              f"{(agg['throughput_records_per_sec'] or {}).get('mean', '-'):>10} "
              f"{vio:>7}")
    lb = results["leg_b"]["aggregate"]["disposition_accuracy"]
    print(f"\nleg B (payment<->order) disposition accuracy: "
          f"mean {lb['mean']} (min {lb['min']}, max {lb['max']})")
    print(f"results written to {out_path}")


if __name__ == "__main__":
    main()
