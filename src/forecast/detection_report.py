"""Detection-quality grading: did the recurring detector find the TRUE
schedule? Graded against golden_obligations.csv (minted at generation time),
strictly on the grader side — the forecaster never sees this file.
"""

from __future__ import annotations

import csv
import os
from datetime import date, timedelta

from recon.generate import seed_dir, START_DATE
from forecast.recurring import detect, template_key
from forecast.slicing import build_input, load_world

EXPECTED_PERIODS = {"payroll": "monthly", "rent": "monthly", "gst": "monthly",
                    "tds": "monthly", "aws": "monthly", "telecom": "monthly",
                    "insurance": "quarterly"}


def _golden(data_dir: str) -> list[dict]:
    with open(os.path.join(data_dir, "golden_obligations.csv"),
              encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["amount_paise"] = int(r["amount_paise"])
    return rows


def detection_quality(seeds: list[int], days: int, cutoff_day_idx: int) -> dict:
    cutoff = START_DATE + timedelta(days=cutoff_day_idx)
    per_key: dict[str, dict] = {
        k: {"key": k, "worlds": 0, "detected": 0, "period_ok": 0,
            "anchor_ok": 0, "amount_ok": 0}
        for k in EXPECTED_PERIODS}
    false_positive_keys = 0

    for seed in seeds:
        data_dir = seed_dir(seed, days)
        golden = _golden(data_dir)
        world = load_world(data_dir)
        detected = detect(build_input(world, cutoff))
        detected_by_tk = {d.key: d for d in detected}

        golden_by_key: dict[str, list[dict]] = {}
        for g in golden:
            golden_by_key.setdefault(g["obligation_key"], []).append(g)
        golden_template_keys = {template_key(g["narration"]) for g in golden}
        false_positive_keys += sum(1 for d in detected
                                   if d.key not in golden_template_keys)

        for key, instances in golden_by_key.items():
            stats = per_key[key]
            stats["worlds"] += 1
            tk = template_key(instances[0]["narration"])
            det = detected_by_tk.get(tk)
            if det is None:
                continue
            stats["detected"] += 1
            if det.period == EXPECTED_PERIODS[key]:
                stats["period_ok"] += 1
            true_anchor = int(date.fromisoformat(instances[0]["due_date"]).day)
            if abs(det.anchor - true_anchor) <= 2:
                stats["anchor_ok"] += 1
            future = [g for g in instances if g["due_date"] > cutoff.isoformat()]
            if future:
                next_actual = min(future, key=lambda g: g["due_date"])
                projected = det.projected_amount
                if abs(projected - next_actual["amount_paise"]) * 10 \
                        <= next_actual["amount_paise"]:
                    stats["amount_ok"] += 1
            else:
                stats["amount_ok"] += 1   # nothing left to project; vacuously fine

    return {"cutoff_day_idx": cutoff_day_idx,
            "per_key": sorted(per_key.values(), key=lambda r: r["key"]),
            "false_positive_keys": false_positive_keys}
