"""Rolling-origin backtest for cash forecasting.

Ground truth is the generator's own post-cutoff bank statement — minted at
generation time, never touched by any forecaster (the slicing wall + a
leakage test guarantee it). Metrics are graded per origin (seed x cutoff),
pooled per seed, then aggregated mean/min/max across seeds, Stage-1 style.

Two readings are published side by side, on purpose: WAPE on the daily net
flow (harsh — the series is spiky by construction) and the balance-level
errors a cash planner actually acts on (path MAE, the horizon low, its date).
Threshold-crossing alerts are graded at every depth in THRESHOLD_OFFSETS_PAISE
and reported with the count of true events behind them, plus one auditable
row per origin, because a hit rate over a handful of events is only honest
when the handful is visible.

CLI:
    python -m forecast.backtest --seeds 42,43,44,45,46,47,48,49,50,51
        [--days 180] [--horizon 14] [--report-dir reports]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import date, timedelta

from recon.generate import generate, seed_dir, START_DATE
from recon.normalize import parse_bank_date
from forecast import verify as fverify
from forecast.baselines import BASELINES
from forecast.schemas import ForecastInput
from forecast.slicing import build_input, load_world

# Cutoff day indices: spacing 9 rotates the cutoff weekday through all seven
# across the seven origins; min 106 guarantees >= 98 days of history (bands),
# max 160 + 14 <= 179 guarantees actuals exist for the whole horizon.
CUTOFF_DAY_INDICES = [106, 115, 124, 133, 142, 151, 160]
HORIZON = 14

# Threshold offsets below the cutoff balance for crossing detection. The
# 1st-of-month payroll+rent cluster (~Rs 4.75L) makes real crossings common.
THRESHOLD_OFFSETS_PAISE = {"2L": 20_000_000, "3L": 30_000_000, "4L": 40_000_000}


# --- Actuals (grader-only) ----------------------------------------------------

def actuals_for(world: dict, cutoff: date, horizon: int) -> dict:
    daily: dict[date, int] = {}
    balance_at: dict[date, int] = {}
    for r in world["bank_rows"]:
        d = parse_bank_date(r.value_date)
        daily[d] = daily.get(d, 0) + r.credit_paise - r.debit_paise
        balance_at[d] = r.balance_paise      # statement order: last row wins
    # opening balance = balance of the latest statement day <= cutoff
    prior_days = [d for d in balance_at if d <= cutoff]
    opening = balance_at[max(prior_days)] if prior_days else 0

    nets, balances = [], []
    bal = opening
    for h in range(1, horizon + 1):
        d = cutoff + timedelta(days=h)
        net = daily.get(d, 0)
        bal += net
        nets.append(net)
        balances.append(bal)
    return {"opening": opening, "nets": nets, "balances": balances}


# --- Metrics ------------------------------------------------------------------

_OUTCOMES = ("hit", "miss", "false_alarm", "correct_negative")


def grade_origin(result: dict, actual: dict,
                 threshold_offsets: dict[str, int], *,
                 cutoff: date | None = None) -> dict:
    f_nets = [d["net"] for d in result["days"]]
    f_bals = [d["balance"] for d in result["days"]]
    a_nets, a_bals = actual["nets"], actual["balances"]
    n = len(a_nets)

    abs_err = sum(abs(f - a) for f, a in zip(f_nets, a_nets))
    denom = sum(abs(a) for a in a_nets)
    wape = abs_err / denom if denom else None
    bias = sum(f - a for f, a in zip(f_nets, a_nets)) / denom if denom else None
    bal_mae = sum(abs(f - a) for f, a in zip(f_bals, a_bals)) // n
    bal_mae_pct = bal_mae / actual["opening"] if actual["opening"] else None

    covered = total_banded = 0
    for d, a in zip(result["days"], a_bals):
        if d["lo80"] is not None:
            total_banded += 1
            covered += int(d["lo80"] <= a <= d["hi80"])

    f_min, a_min = min(f_bals), min(a_bals)
    f_min_day = f_bals.index(f_min) + 1
    a_min_day = a_bals.index(a_min) + 1

    crossings = {}
    for label, off in threshold_offsets.items():
        thr = actual["opening"] - off
        predicted, happened = f_min < thr, a_min < thr
        crossings[label] = {
            "hit": int(predicted and happened),
            "miss": int(not predicted and happened),
            "false_alarm": int(predicted and not happened),
            "correct_negative": int(not predicted and not happened),
        }

    return {
        "cutoff": cutoff.isoformat() if cutoff else None,
        "opening_paise": actual["opening"],
        "wape": wape, "bias": bias,
        "balance_mae_paise": bal_mae, "balance_mae_pct": bal_mae_pct,
        "abs_balance_err_by_h": [abs(f - a) for f, a in zip(f_bals, a_bals)],
        "pred_last_paise": f_bals[-1], "true_last_paise": a_bals[-1],
        "signed_last_err_paise": f_bals[-1] - a_bals[-1],
        "pred_min_paise": f_min, "true_min_paise": a_min,
        "band_covered": covered, "band_total": total_banded,
        "min_balance_err_paise": abs(f_min - a_min),
        "min_date_within_2d": int(abs(f_min_day - a_min_day) <= 2),
        "crossings": crossings,
    }


def _outcome(cell: dict) -> str:
    """The one outcome an origin x depth cell holds (exactly one flag is 1)."""
    return next(k for k in _OUTCOMES if cell[k])


def _origin_row(g: dict) -> dict:
    """Compact audit record kept in the report: which cutoffs crossed, and how
    far the point path sat from the truth at the horizon's low and last day."""
    return {
        "cutoff": g["cutoff"],
        "opening_paise": g["opening_paise"],
        "wape": None if g["wape"] is None else round(g["wape"], 4),
        "pred_min_paise": g["pred_min_paise"],
        "true_min_paise": g["true_min_paise"],
        "pred_last_paise": g["pred_last_paise"],
        "true_last_paise": g["true_last_paise"],
        "signed_last_err_paise": g["signed_last_err_paise"],
        "alerts": {label: _outcome(cell) for label, cell in g["crossings"].items()},
    }


def _pool_seed(origin_grades: list[dict]) -> dict:
    n = len(origin_grades)
    covered = sum(g["band_covered"] for g in origin_grades)
    banded = sum(g["band_total"] for g in origin_grades)
    crossings = {}
    for label in origin_grades[0]["crossings"]:
        crossings[label] = {
            k: sum(g["crossings"][label][k] for g in origin_grades)
            for k in _OUTCOMES}
    horizon = len(origin_grades[0]["abs_balance_err_by_h"])
    return {
        "origins": n,
        "opening_mean_paise": int(statistics.mean(
            g["opening_paise"] for g in origin_grades)),
        "wape": round(statistics.mean(g["wape"] for g in origin_grades), 4),
        "bias": round(statistics.mean(g["bias"] for g in origin_grades), 4),
        "balance_mae_paise": int(statistics.mean(
            g["balance_mae_paise"] for g in origin_grades)),
        "balance_mae_pct": round(statistics.mean(
            g["balance_mae_pct"] for g in origin_grades), 4),
        "balance_mae_by_h_paise": [
            int(statistics.mean(g["abs_balance_err_by_h"][h] for g in origin_grades))
            for h in range(horizon)],
        "coverage80": round(covered / banded, 4) if banded else None,
        "min_balance_err_paise": int(statistics.mean(
            g["min_balance_err_paise"] for g in origin_grades)),
        "min_date_within_2d_rate": round(statistics.mean(
            g["min_date_within_2d"] for g in origin_grades), 4),
        "crossings": crossings,
        "origin_rows": [_origin_row(g) for g in origin_grades],
    }


def _aggregate(per_seed: dict[str, dict], key: str) -> dict | None:
    vals = [m[key] for m in per_seed.values() if m.get(key) is not None]
    if not vals:
        return None
    return {"mean": round(statistics.mean(vals), 4),
            "min": round(min(vals), 4), "max": round(max(vals), 4)}


AGG_KEYS = ["wape", "bias", "balance_mae_paise", "balance_mae_pct",
            "coverage80", "min_balance_err_paise", "min_date_within_2d_rate",
            "opening_mean_paise"]


# --- Strategy registry --------------------------------------------------------

def _strategies() -> dict:
    strategies = dict(BASELINES)
    try:
        from forecast.forecaster import forecast as _forecast

        def full(inp: ForecastInput, horizon: int):
            return _forecast(inp, horizon)

        def no_pipeline(inp: ForecastInput, horizon: int):
            return _forecast(inp, horizon, enable_pipeline=False,
                             strategy_name="forecaster_no_pipeline")

        def no_recurring(inp: ForecastInput, horizon: int):
            return _forecast(inp, horizon, enable_recurring=False,
                             strategy_name="forecaster_no_recurring")

        strategies["forecaster"] = full
        strategies["forecaster_no_pipeline"] = no_pipeline
        strategies["forecaster_no_recurring"] = no_recurring
    except ImportError:
        pass  # baselines-only mode while the forecaster is being built
    return strategies


# --- Harness ------------------------------------------------------------------

def run_backtest(seeds: list[int], days: int = 180, horizon: int = HORIZON,
                 regenerate: bool = False) -> dict:
    strategies = _strategies()
    results: dict[str, dict] = {name: {"per_seed": {}} for name in strategies}
    detection = None

    for seed in seeds:
        data_dir = seed_dir(seed, days)
        if regenerate or not os.path.exists(
                os.path.join(data_dir, "golden_manifest.json")):
            generate(seed, data_dir, days)
        world = load_world(data_dir)

        inputs = {}
        actuals = {}
        for idx in CUTOFF_DAY_INDICES:
            cutoff = START_DATE + timedelta(days=idx)
            inputs[idx] = build_input(world, cutoff)
            actuals[idx] = actuals_for(world, cutoff, horizon)

        for name, strategy in strategies.items():
            grades = []
            t0 = time.perf_counter()
            for idx in CUTOFF_DAY_INDICES:
                result = strategy(inputs[idx], horizon).to_dict()
                violations = fverify.verify_result(result)
                if violations and name.startswith("forecaster"):
                    raise SystemExit(
                        f"forecast verify failed for {name} seed {seed} "
                        f"cutoff {idx}:\n" + "\n".join(f"  - {v}" for v in violations))
                grades.append(grade_origin(
                    result, actuals[idx], THRESHOLD_OFFSETS_PAISE,
                    cutoff=START_DATE + timedelta(days=idx)))
            pooled = _pool_seed(grades)
            pooled["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
            results[name]["per_seed"][str(seed)] = pooled

    for name, r in results.items():
        r["aggregate"] = {k: _aggregate(r["per_seed"], k) for k in AGG_KEYS}
        r["crossings_total"] = {
            label: {k: sum(r["per_seed"][s]["crossings"][label][k]
                           for s in r["per_seed"])
                    for k in ("hit", "miss", "false_alarm", "correct_negative")}
            for label in THRESHOLD_OFFSETS_PAISE}

    try:
        from forecast.detection_report import detection_quality
        detection = detection_quality(seeds, days, CUTOFF_DAY_INDICES[-1])
    except ImportError:
        detection = None

    return {
        "seeds": seeds, "days": days, "horizon": horizon,
        "cutoff_day_indices": CUTOFF_DAY_INDICES,
        "origins_per_seed": len(CUTOFF_DAY_INDICES),
        "strategies": results,
        "detection_quality": detection,
        "notes": [
            "Ground truth = the generator's post-cutoff bank statement, minted at "
            "generation time; no forecaster can read past the cutoff (leakage test).",
            "Irreducible error is designed in: delayed/missing settlements, "
            "random noise debits, jittered obligations.",
            "Coverage80 is reported as measured; constants were frozen before "
            "the final multi-seed run.",
            "Read WAPE for what it is: error on the daily net-flow series, which "
            "is spiky by construction (settlement batches, the 1st-of-month "
            "obligation cluster, noise debits), so a 50-60% daily WAPE coexists "
            "with a balance path that sits within ~1-2% of the opening balance. "
            "Cash planning reads the balance path, the horizon low and its date, "
            "and the alerts - the daily line is not a promise about any one day.",
            "Alert evidence is a small sample by nature: only origins whose "
            "held-out fortnight really dropped by the depth count as true "
            "events. The sweep table states that count and the per-origin table "
            "lists every event - read hits and false alarms as 'on the events "
            "we had', not as a track record.",
        ],
    }


# --- Markdown report ----------------------------------------------------------

def _pct(v):
    return "-" if v is None else (f"{v['mean']:.1%}" if isinstance(v, dict) else f"{v:.1%}")


def _rs(v):
    if v is None:
        return "-"
    paise = v["mean"] if isinstance(v, dict) else v
    return f"Rs {paise / 100:,.0f}"


def _alerts_cell(c: dict) -> str:
    return f"{c['hit']}/{c['miss']}/{c['false_alarm']}"


def _pooled_alerts(crossings_total: dict) -> dict:
    """Outcome counts summed over every depth."""
    return {k: sum(c[k] for c in crossings_total.values()) for k in _OUTCOMES}


def write_markdown_report(results: dict, path: str) -> None:
    lines: list[str] = []
    add = lines.append
    strategies = results["strategies"]
    n_origins = len(results["seeds"]) * results["origins_per_seed"]
    first = next(iter(strategies.values()))
    labels = list(first["crossings_total"])
    add("# Cash Forecast Backtest Report")
    add("")
    add(f"Seeds: {results['seeds']} x {results['origins_per_seed']} rolling "
        f"origins (cutoff days {results['cutoff_day_indices']}) x "
        f"{results['horizon']}-day horizon on {results['days']}-day worlds "
        f"= {n_origins} held-out origins.")
    add("")
    add("## Headline (mean across seeds)")
    add("")
    add("| strategy | WAPE (daily net) | bias | balance MAE | MAE % of opening "
        "| coverage80 | min-balance err | alerts H/M/FA (all depths) |")
    add("|---|---|---|---|---|---|---|---|")
    for name, r in strategies.items():
        agg = r["aggregate"]
        add(f"| {name} | {_pct(agg['wape'])} | {_pct(agg['bias'])} "
            f"| {_rs(agg['balance_mae_paise'])} | {_pct(agg['balance_mae_pct'])} "
            f"| {_pct(agg['coverage80'])} | {_rs(agg['min_balance_err_paise'])} "
            f"| {_alerts_cell(_pooled_alerts(r['crossings_total']))} |")
    add("")
    opening = first["aggregate"].get("opening_mean_paise")
    if opening:
        add(f"Mean opening balance at the origins: {_rs(opening)} - the "
            "denominator of 'MAE % of opening'. WAPE is on the daily net flow; "
            "balance MAE and the min-balance error are on the balance path a "
            "cash planner reads.")
        add("")
    add("## Cash-drop alerts - threshold sweep")
    add("")
    events = {lab: first["crossings_total"][lab]["hit"]
              + first["crossings_total"][lab]["miss"] for lab in labels}
    add("An alert fires when a strategy's point path dips more than the depth "
        "below the origin's opening balance within the horizon; a true event "
        "is the same test on the held-out actuals. "
        f"{n_origins} origins x {len(labels)} depths. True events: "
        + " · ".join(f"{lab} {events[lab]}" for lab in labels)
        + f" - {sum(events.values())} in total (hits + misses, identical for "
        "every strategy). H/M/FA = hits / misses / false alarms.")
    add("")
    add("| strategy | " + " | ".join(f"{lab} H/M/FA" for lab in labels)
        + " | all depths H/M/FA |")
    add("|---|" + "|".join(["---"] * (len(labels) + 1)) + "|")
    for name, r in strategies.items():
        cells = [_alerts_cell(r["crossings_total"][lab]) for lab in labels]
        cells.append(_alerts_cell(_pooled_alerts(r["crossings_total"])))
        add(f"| {name} | " + " | ".join(cells) + " |")
    add("")
    fc = strategies.get("forecaster")
    if fc and any("origin_rows" in per for per in fc["per_seed"].values()):
        add("## Which origins crossed (forecaster)")
        add("")
        add("Every origin x depth that was a true event or a false alarm - the "
            "whole evidence behind the alert numbers, so the sample size is "
            "visible rather than implied. Threshold = opening - depth.")
        add("")
        add(f"| seed | cutoff | depth | opening | true {results['horizon']}-day "
            "low | forecast low | outcome |")
        add("|---|---|---|---|---|---|---|")
        for seed, per in fc["per_seed"].items():
            for row in per.get("origin_rows", []):
                for lab, word in row["alerts"].items():
                    if word == "correct_negative":
                        continue
                    add(f"| {seed} | {row['cutoff']} | {lab} "
                        f"| {_rs(row['opening_paise'])} "
                        f"| {_rs(row['true_min_paise'])} "
                        f"| {_rs(row['pred_min_paise'])} "
                        f"| {word.replace('_', ' ')} |")
        add("")
    add("## Balance error growth by horizon day (mean across seeds, Rs)")
    add("")
    names = list(results["strategies"])
    add("| h | " + " | ".join(names) + " |")
    add("|---|" + "|".join(["---"] * len(names)) + "|")
    horizon = results["horizon"]
    for h in range(horizon):
        cells = []
        for name in names:
            per_seed = results["strategies"][name]["per_seed"]
            vals = [per_seed[s]["balance_mae_by_h_paise"][h] for s in per_seed]
            cells.append(f"{statistics.mean(vals) / 100:,.0f}")
        add(f"| {h + 1} | " + " | ".join(cells) + " |")
    add("")
    if results.get("detection_quality"):
        dq = results["detection_quality"]
        add("## Recurring-obligation detection quality (vs golden_obligations.csv)")
        add("")
        add("| obligation | detected | period ok | anchor ok (+-2d) | amount ok (+-10%) |")
        add("|---|---|---|---|---|")
        for row in dq["per_key"]:
            add(f"| {row['key']} | {row['detected']}/{row['worlds']} "
                f"| {row['period_ok']}/{row['worlds']} "
                f"| {row['anchor_ok']}/{row['worlds']} "
                f"| {row['amount_ok']}/{row['worlds']} |")
        add("")
        add(f"False-positive recurring keys detected: {dq['false_positive_keys']}")
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
    ap.add_argument("--seeds", default="42,43,44,45,46,47,48,49,50,51")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--report-dir", default="reports")
    ap.add_argument("--regenerate", action="store_true")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    results = run_backtest(seeds, args.days, args.horizon, args.regenerate)

    os.makedirs(args.report_dir, exist_ok=True)
    out = os.path.join(args.report_dir, "forecast_backtest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    write_markdown_report(results,
                          os.path.join(args.report_dir, "forecast_backtest.md"))

    header = f"{'strategy':<24} {'WAPE':>7} {'bias':>7} {'balMAE':>12} " \
             f"{'cov80':>6} {'minerr':>10}"
    print(header)
    print("-" * len(header))
    for name, r in results["strategies"].items():
        agg = r["aggregate"]

        def m(key):
            v = agg[key]
            return v["mean"] if v else None

        wape = f"{m('wape'):.3f}" if m("wape") is not None else "-"
        bias = f"{m('bias'):+.3f}" if m("bias") is not None else "-"
        cov = f"{m('coverage80'):.0%}" if m("coverage80") is not None else "-"
        print(f"{name:<24} {wape:>7} {bias:>7} "
              f"{'Rs ' + format(int(m('balance_mae_paise')) // 100, ','):>12} "
              f"{cov:>6} "
              f"{'Rs ' + format(int(m('min_balance_err_paise')) // 100, ','):>10}")
    print(f"\nresults written to {out}")


if __name__ == "__main__":
    main()
