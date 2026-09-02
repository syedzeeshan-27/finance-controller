"""Tax benchmark: golden-row grading, money metrics, canary contrast.

The ONLY tax module allowed to read golden_tax.csv (a test enforces this).
Grading mirrors Stage 1: per golden record, the deciding decision must carry
the expected disposition AND the expected counterparty set. Money metrics
make the stakes concrete: how many rupees of input credit each strategy
claims falsely, and how many at-risk rupees it surfaces.

CLI:
    python -m tax.benchmark --seeds 42,43,44,45,46 --days 180
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time

from recon.generate import generate, seed_dir
from tax import schemas as TS
from tax.baselines import BASELINES
from tax.engine import reconcile_tax
from tax.io_tax import build_tax_input
from tax.verify import verify_tax

ALL_STRATEGIES = dict(BASELINES)
ALL_STRATEGIES["tax_engine"] = reconcile_tax

UNREPORTED = "unreported"

_FILED_SIDE = {TS.RT_2B_LINE, TS.RT_26AS_ENTRY}
_DUP_STATUSES = {TS.DUPLICATE_2B_LINE, TS.TDS_DUPLICATE_26AS}


def load_golden_tax(data_dir: str) -> list[dict]:
    with open(os.path.join(data_dir, "golden_tax.csv"),
              encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["expected_discrepancy_paise"] = int(r["expected_discrepancy_paise"])
        r["counterparty_set"] = frozenset(
            x for x in r["counterparty_ids"].split(";") if x)
    return rows


def _loop_of(record_type: str) -> str:
    if record_type in (TS.RT_PURCHASE, TS.RT_2B_LINE):
        return "itc"
    if record_type in (TS.RT_TDS_EVENT, TS.RT_26AS_ENTRY):
        return "tds"
    return "obligation"


def _assignments(decisions: list[TS.TaxDecision]
                 ) -> dict[tuple[str, str], TS.TaxDecision]:
    """Map record -> deciding decision. For filed records the deciding
    decision is the one carrying it in filed_ids (a duplicate's own verdict
    IS the duplicate decision); for book records, the primary (non-duplicate)
    decision wins."""
    out: dict[tuple[str, str], TS.TaxDecision] = {}
    for d in decisions:
        for fid in d.filed_ids:
            out.setdefault(("filed", fid), d)
        if d.status in _DUP_STATUSES:
            continue
        for bid in d.book_ids:
            out.setdefault(("book", bid), d)
    return out


def _counterparties(d: TS.TaxDecision, record_type: str) -> frozenset[str]:
    if record_type in _FILED_SIDE:
        return frozenset(d.book_ids)
    return frozenset(d.filed_ids)


def _claim_paise(d: TS.TaxDecision) -> int:
    """Rupee value a CLAIMABLE_NOW decision books as input credit: the
    conservative lower of the two sides."""
    sides = [x for x in (d.books_paise, d.filed_paise) if x is not None]
    return min(sides) if sides else 0


def grade_tax(decisions: list[TS.TaxDecision], golden: list[dict]) -> dict:
    assign = _assignments(decisions)
    n_correct = 0
    tp = fp = fn = 0
    per_loop: dict[str, list[bool]] = {}
    per_scenario: dict[str, list[bool]] = {}
    rows = []

    # Money truth from the golden side.
    golden_by_key = {(g["record_type"], g["record_id"]): g for g in golden}
    at_risk_itc_total = sum(-g["expected_discrepancy_paise"] for g in golden
                            if g["expected_disposition"] == TS.ITC_MISSING_IN_2B
                            and g["record_type"] == TS.RT_PURCHASE)
    at_risk_tds_total = sum(-g["expected_discrepancy_paise"] for g in golden
                            if g["expected_disposition"] == TS.TDS_MISSING_IN_26AS
                            and g["record_type"] == TS.RT_TDS_EVENT)
    shortfall_total = sum(-g["expected_discrepancy_paise"] for g in golden
                          if g["expected_disposition"] == TS.PAID_SHORT)

    for g in golden:
        side = "filed" if g["record_type"] in _FILED_SIDE else "book"
        d = assign.get((side, g["record_id"]))
        status = d.status if d else UNREPORTED
        expected = g["expected_disposition"]
        golden_set = g["counterparty_set"]

        if expected in TS.TAX_MATCHED_FAMILY:
            correct = (d is not None and status == expected
                       and _counterparties(d, g["record_type"]) == golden_set)
            if correct:
                tp += 1
            else:
                fn += 1
                if d is not None and status in TS.TAX_MATCHED_FAMILY:
                    fp += 1     # wrongly linked or wrongly classified match
        else:
            correct = (d is not None and status == expected
                       and (not golden_set
                            or _counterparties(d, g["record_type"])
                            == golden_set))
            if d is not None and status in TS.TAX_MATCHED_FAMILY:
                fp += 1         # matched something that must not be matched
        n_correct += int(correct)
        per_loop.setdefault(_loop_of(g["record_type"]), []).append(correct)
        per_scenario.setdefault(g["scenario_tag"], []).append(correct)
        rows.append({"record_type": g["record_type"],
                     "record_id": g["record_id"],
                     "scenario": g["scenario_tag"], "expected": expected,
                     "got": status, "correct": correct})

    # Money metrics from the decision side, judged against golden truth.
    claimed = false_claim = 0
    at_risk_itc_found = at_risk_tds_found = shortfall_found = 0
    for d in decisions:
        if d.loop == "itc" and d.status in TS.CLAIMABLE_NOW:
            amt = _claim_paise(d)
            claimed += amt
            ok = bool(d.book_ids) and bool(d.filed_ids)
            for rid in d.book_ids:
                gg = golden_by_key.get((TS.RT_PURCHASE, rid))
                if gg is None or gg["expected_disposition"] not in (
                        TS.CLAIMABLE_NOW | {TS.ITC_DEFERRED_NEXT_PERIOD}):
                    ok = False
                elif gg["counterparty_set"] != frozenset(d.filed_ids):
                    ok = False
            if not ok:
                false_claim += amt
        elif d.status == TS.ITC_MISSING_IN_2B:
            gg = golden_by_key.get((TS.RT_PURCHASE, d.book_ids[0])) \
                if d.book_ids else None
            if gg is not None and gg["expected_disposition"] == d.status:
                at_risk_itc_found += -(d.discrepancy_paise or 0)
        elif d.status == TS.TDS_MISSING_IN_26AS:
            gg = golden_by_key.get((TS.RT_TDS_EVENT, d.book_ids[0])) \
                if d.book_ids else None
            if gg is not None and gg["expected_disposition"] == d.status:
                at_risk_tds_found += -(d.discrepancy_paise or 0)
        elif d.status == TS.PAID_SHORT:
            gg = golden_by_key.get((TS.RT_OBLIGATION_PERIOD, d.book_ids[0])) \
                if d.book_ids else None
            if gg is not None and gg["expected_disposition"] == d.status:
                shortfall_found += -(d.discrepancy_paise or 0)

    n = len(golden)
    n_matchable = sum(1 for g in golden
                      if g["expected_disposition"] in TS.TAX_MATCHED_FAMILY)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / n_matchable if n_matchable else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision not in (None, 0) and recall not in (None, 0) else 0.0)

    return {
        "golden_rows": n,
        "matchable_rows": n_matchable,
        "true_positives": tp, "false_positives": fp, "false_negatives": fn,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4),
        "disposition_accuracy": round(n_correct / n, 4) if n else None,
        "accuracy_by_loop": {
            k: {"correct": sum(vv), "total": len(vv),
                "accuracy": round(sum(vv) / len(vv), 4)}
            for k, vv in sorted(per_loop.items())},
        "per_scenario": {
            k: {"correct": sum(vv), "total": len(vv),
                "accuracy": round(sum(vv) / len(vv), 4)}
            for k, vv in sorted(per_scenario.items())},
        "money": {
            "claimed_itc_paise": claimed,
            "false_claim_paise": false_claim,
            "itc_at_risk_paise": at_risk_itc_total,
            "itc_at_risk_identified_paise": at_risk_itc_found,
            "tds_at_risk_paise": at_risk_tds_total,
            "tds_at_risk_identified_paise": at_risk_tds_found,
            "obligation_shortfall_paise": shortfall_total,
            "obligation_shortfall_detected_paise": shortfall_found,
        },
        "row_details": rows,
    }


def run_strategy(name: str, data_dir: str) -> dict:
    strategy = ALL_STRATEGIES[name]
    inp = build_tax_input(data_dir)
    t0 = time.perf_counter()
    decisions = strategy(inp)
    elapsed = time.perf_counter() - t0
    golden = load_golden_tax(data_dir)
    result = grade_tax(decisions, golden)
    violations = verify_tax(data_dir, decisions)
    n_records = (len(inp.purchases) + len(inp.gstr2b) + len(inp.tds_events)
                 + len(inp.form26as) + len(inp.periods))
    result.update({
        "records_processed": n_records,
        "elapsed_seconds": round(elapsed, 4),
        "throughput_records_per_sec": round(n_records / elapsed, 2)
        if elapsed > 0 else None,
        "verify_violations": violations[:25],
        "verify_violation_count": len(violations),
    })
    return result


def _aggregate(per_seed: list[dict], key: str) -> dict:
    vals = [r[key] for r in per_seed if r.get(key) is not None]
    if not vals:
        return {"mean": None, "min": None, "max": None}
    return {"mean": round(sum(vals) / len(vals), 4),
            "min": round(min(vals), 4), "max": round(max(vals), 4)}


AGG_KEYS = ["precision", "recall", "f1", "disposition_accuracy",
            "verify_violation_count", "throughput_records_per_sec"]
MONEY_KEYS = ["claimed_itc_paise", "false_claim_paise", "itc_at_risk_paise",
              "itc_at_risk_identified_paise", "tds_at_risk_paise",
              "tds_at_risk_identified_paise", "obligation_shortfall_paise",
              "obligation_shortfall_detected_paise"]


def run_benchmark(seeds: list[int], days: int = 180,
                  strategies: list[str] | None = None,
                  regenerate: bool = True) -> dict:
    strategies = strategies or list(ALL_STRATEGIES)
    per_strategy: dict[str, dict] = {}
    for seed in seeds:
        data_dir = seed_dir(seed, days)
        if regenerate or not os.path.exists(
                os.path.join(data_dir, "golden_tax.csv")):
            generate(seed, data_dir, days)
    for name in strategies:
        per_seed = {}
        for seed in seeds:
            r = run_strategy(name, seed_dir(seed, days))
            r.pop("row_details", None)
            per_seed[str(seed)] = r
        rows = list(per_seed.values())
        agg = {k: _aggregate(rows, k) for k in AGG_KEYS}
        agg["golden_rows_total"] = sum(r["golden_rows"] for r in rows)
        agg["money_totals"] = {
            k: sum(r["money"][k] for r in rows) for k in MONEY_KEYS}
        agg["accuracy_by_loop"] = {
            loop: {"correct": sum(r["accuracy_by_loop"][loop]["correct"]
                                  for r in rows if loop in r["accuracy_by_loop"]),
                   "total": sum(r["accuracy_by_loop"][loop]["total"]
                                for r in rows if loop in r["accuracy_by_loop"])}
            for loop in ("itc", "tds", "obligation")}
        per_strategy[name] = {"per_seed": per_seed, "aggregate": agg}
    return {"seeds": seeds, "days": days, "strategies": per_strategy}


def _rs(paise: int) -> str:
    return f"Rs {paise // 100:,}"


def write_markdown_report(results: dict, path: str) -> None:
    lines = ["# Tax-matching benchmark", ""]
    lines.append(f"Seeds {results['seeds']} on {results['days']}-day worlds. "
                 "Graded per golden record: the deciding decision must carry "
                 "the expected disposition AND the expected counterparty "
                 "set. Verification is independent (own parsers, own rule "
                 "implementations) and hard-fails the engine on any "
                 "violation; baseline violations are reported as evidence.")
    lines.append("")
    lines.append("| strategy | disposition acc | ITC loop | TDS loop | "
                 "compliance | precision | recall | F1 | false ITC claims | "
                 "verify violations |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, block in results["strategies"].items():
        agg = block["aggregate"]
        loops = agg["accuracy_by_loop"]

        def loop_cell(loop: str) -> str:
            t = loops[loop]
            return (f"{t['correct']}/{t['total']}") if t["total"] else "-"

        lines.append(
            f"| {name} "
            f"| {agg['disposition_accuracy']['mean']:.1%} "
            f"| {loop_cell('itc')} | {loop_cell('tds')} "
            f"| {loop_cell('obligation')} "
            f"| {agg['precision']['mean']:.1%} "
            f"| {agg['recall']['mean']:.1%} | {agg['f1']['mean']:.3f} "
            f"| {_rs(agg['money_totals']['false_claim_paise'])} "
            f"| {int(agg['verify_violation_count']['mean'] * len(results['seeds']))} |")
    lines.append("")

    lines.append("## Money on the table (totals across seeds)")
    lines.append("")
    lines.append("| metric | " + " | ".join(results["strategies"]) + " |")
    lines.append("|---|" + "---|" * len(results["strategies"]))
    money_rows = [
        ("input credit claimed", "claimed_itc_paise"),
        ("**claimed falsely** (blocked/unknown/mispaired)", "false_claim_paise"),
        ("ITC at risk (vendor never filed) — identified",
         "itc_at_risk_identified_paise"),
        ("TDS credits at risk — identified", "tds_at_risk_identified_paise"),
        ("short-paid liability — detected",
         "obligation_shortfall_detected_paise"),
    ]
    truth = next(iter(results["strategies"].values()))["aggregate"]["money_totals"]
    for label, key in money_rows:
        cells = []
        for name in results["strategies"]:
            val = results["strategies"][name]["aggregate"]["money_totals"][key]
            if key == "itc_at_risk_identified_paise":
                cells.append(f"{_rs(val)} of {_rs(truth['itc_at_risk_paise'])}")
            elif key == "tds_at_risk_identified_paise":
                cells.append(f"{_rs(val)} of {_rs(truth['tds_at_risk_paise'])}")
            elif key == "obligation_shortfall_detected_paise":
                cells.append(f"{_rs(val)} of "
                             f"{_rs(truth['obligation_shortfall_paise'])}")
            else:
                cells.append(_rs(val))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Per-scenario disposition accuracy (all seeds pooled)")
    lines.append("")
    strategies = list(results["strategies"])
    lines.append("| scenario | " + " | ".join(strategies) + " |")
    lines.append("|---|" + "---|" * len(strategies))
    scenarios: dict[str, dict[str, tuple[int, int]]] = {}
    for name in strategies:
        for seed_result in results["strategies"][name]["per_seed"].values():
            for sc, t in seed_result["per_scenario"].items():
                cur = scenarios.setdefault(sc, {}).setdefault(name, (0, 0))
                scenarios[sc][name] = (cur[0] + t["correct"],
                                       cur[1] + t["total"])
    for sc in sorted(scenarios):
        cells = []
        for name in strategies:
            c, t = scenarios[sc].get(name, (0, 0))
            cells.append(f"{c}/{t}" if t else "-")
        lines.append(f"| {sc} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("Ground truth is minted at generation time "
                 "(`golden_tax.csv`); the engine derives the books side "
                 "blind from the statement, settlements, payments and the "
                 "Stage 1 reconciliation output, and a behavioral test pins "
                 "its decisions byte-identical with the answer keys deleted "
                 "or corrupted.")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", default="42,43,44,45,46")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--strategies", default=None)
    ap.add_argument("--report-dir", default="reports")
    ap.add_argument("--no-regenerate", action="store_true")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    strategies = args.strategies.split(",") if args.strategies else None
    results = run_benchmark(seeds, args.days, strategies,
                            regenerate=not args.no_regenerate)

    os.makedirs(args.report_dir, exist_ok=True)
    with open(os.path.join(args.report_dir, "tax_benchmark.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    write_markdown_report(results,
                          os.path.join(args.report_dir, "tax_benchmark.md"))

    print(f"seeds: {seeds} ({args.days}-day worlds)")
    header = (f"{'strategy':<12} {'disp.acc':>9} {'precision':>10} "
              f"{'recall':>7} {'false claims':>13} {'verify':>7}")
    print(header)
    print("-" * len(header))
    engine_violations = 0
    for name, block in results["strategies"].items():
        agg = block["aggregate"]
        total_viol = sum(r["verify_violation_count"]
                         for r in block["per_seed"].values())
        if name == "tax_engine":
            engine_violations = total_viol
        print(f"{name:<12} {agg['disposition_accuracy']['mean']:>9.4f} "
              f"{agg['precision']['mean']:>10.4f} "
              f"{agg['recall']['mean']:>7.4f} "
              f"{_rs(agg['money_totals']['false_claim_paise']):>13} "
              f"{total_viol:>7}")
    print(f"results written to {os.path.join(args.report_dir, 'tax_benchmark.json')}")
    if engine_violations:
        raise SystemExit(
            f"FAIL: independent verification found {engine_violations} "
            f"violations in tax_engine output")


if __name__ == "__main__":
    main()
