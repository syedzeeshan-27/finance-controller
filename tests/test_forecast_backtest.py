"""Backtest math on hand-computed fixtures, the leakage guard, and verify."""

import csv
import os
import shutil

from recon.generate import generate, START_DATE
from forecast.backtest import _pool_seed, actuals_for, grade_origin
from forecast.forecaster import forecast
from forecast.slicing import build_input, load_world
from forecast.verify import verify_result

from datetime import date, timedelta


def _result(nets, opening=1_000_000, lo_hi=None):
    days = []
    balance = opening
    for h, net in enumerate(nets, start=1):
        balance += net
        d = {"date": f"2025-07-{15 + h:02d}",
             "known_inflows": 0,
             "projected_sales_inflows": max(net, 0),
             "other_income": 0,
             "recurring_outflows": 0,
             "variable_outflows": max(-net, 0),
             "net": net, "balance": balance,
             "lo80": balance + lo_hi[0] if lo_hi else None,
             "hi80": balance + lo_hi[1] if lo_hi else None}
        days.append(d)
    worst = min(days, key=lambda d: d["balance"])
    return {"strategy": "t", "cutoff": "2025-07-15", "horizon": len(nets),
            "opening_balance_paise": opening, "days": days,
            "min_balance": {"date": worst["date"], "paise": worst["balance"]}}


class TestGradeOrigin:
    def test_hand_computed_wape_bias_mae(self):
        result = _result([100, -50, 200])
        actual = {"opening": 1_000_000, "nets": [80, -60, 150],
                  "balances": [1_000_080, 1_000_020, 1_000_170]}
        g = grade_origin(result, actual, {"X": 10_000})
        # |20| + |10| + |50| = 80 over |80|+|60|+|150| = 290
        assert g["wape"] == 80 / 290
        # (20 + 10 + 50) / 290
        assert g["bias"] == 80 / 290
        # balance errors: |1000100-1000080|=20, |1000050-1000020|=30, |1000250-1000170|=80
        assert g["balance_mae_paise"] == (20 + 30 + 80) // 3
        assert g["abs_balance_err_by_h"] == [20, 30, 80]

    def test_coverage_counts_banded_days_only(self):
        result = _result([0, 0], lo_hi=(-25, 25))
        actual = {"opening": 1_000_000, "nets": [10, 100],
                  "balances": [1_000_010, 1_000_110]}
        g = grade_origin(result, actual, {})
        assert g["band_total"] == 2
        assert g["band_covered"] == 1   # day 2 actual escapes the band

    def test_threshold_crossing_states(self):
        # forecast dips below (predicted), actual does not -> false alarm
        result = _result([-300])
        actual = {"opening": 1_000_000, "nets": [-100], "balances": [999_900]}
        g = grade_origin(result, actual, {"X": 200})   # threshold 999_800
        assert g["crossings"]["X"] == {"hit": 0, "miss": 0, "false_alarm": 1,
                                       "correct_negative": 0}
        # both dip -> hit
        actual2 = {"opening": 1_000_000, "nets": [-500], "balances": [999_500]}
        g2 = grade_origin(result, actual2, {"X": 200})
        assert g2["crossings"]["X"]["hit"] == 1

    def test_origin_audit_fields(self):
        result = _result([100, -50, 200])
        actual = {"opening": 1_000_000, "nets": [80, -60, 150],
                  "balances": [1_000_080, 1_000_020, 1_000_170]}
        g = grade_origin(result, actual, {"X": 10_000},
                         cutoff=date(2025, 7, 15))
        assert g["cutoff"] == "2025-07-15"
        assert g["opening_paise"] == 1_000_000
        # forecast path 1000100, 1000050, 1000250 vs truth 1000080, 1000020, 1000170
        assert (g["pred_min_paise"], g["true_min_paise"]) == (1_000_050, 1_000_020)
        assert (g["pred_last_paise"], g["true_last_paise"]) == (1_000_250, 1_000_170)
        assert g["signed_last_err_paise"] == 80
        # the positional form (no cutoff) keeps working and leaves it unset
        assert grade_origin(result, actual, {"X": 10_000})["cutoff"] is None


class TestPooling:
    def test_origin_rows_reconcile_with_pooled_alerts(self):
        """Every origin is exactly one outcome per depth, the per-origin rows
        say which, and hits + misses is the number of true events."""
        offsets = {"X": 200}                          # threshold = 999_800
        cases = [([-300], [-100], date(2025, 7, 15)),   # forecast dips, truth not
                 ([-300], [-500], date(2025, 7, 24)),   # both dip
                 ([-100], [-500], date(2025, 8, 2)),    # truth dips, forecast not
                 ([-100], [-100], date(2025, 8, 11))]   # neither
        grades = []
        for nets, a_nets, cutoff in cases:
            actual = {"opening": 1_000_000, "nets": a_nets,
                      "balances": [1_000_000 + a_nets[0]]}
            grades.append(grade_origin(_result(nets), actual, offsets,
                                       cutoff=cutoff))
        pooled = _pool_seed(grades)
        c = pooled["crossings"]["X"]
        assert c == {"hit": 1, "miss": 1, "false_alarm": 1, "correct_negative": 1}
        assert c["hit"] + c["miss"] == 2 and sum(c.values()) == len(cases)
        rows = pooled["origin_rows"]
        assert [r["alerts"]["X"] for r in rows] == [
            "false_alarm", "hit", "miss", "correct_negative"]
        assert [r["cutoff"] for r in rows] == [
            "2025-07-15", "2025-07-24", "2025-08-02", "2025-08-11"]
        assert pooled["opening_mean_paise"] == 1_000_000
        assert rows[1]["true_min_paise"] == 999_500
        assert rows[1]["pred_min_paise"] == 999_700


class TestVerify:
    def test_clean_passes_and_corruption_caught(self):
        r = _result([100, -50])
        assert verify_result(r) == []
        r["days"][1]["balance"] += 1
        assert any("balance" in v for v in verify_result(r))

    def test_component_sum_checked(self):
        r = _result([100])
        r["days"][0]["known_inflows"] = 999
        assert any("component sum" in v for v in verify_result(r))

    def test_band_monotonicity_checked(self):
        r = _result([0, 0], lo_hi=(-10, 10))
        r["days"][1]["lo80"] = r["days"][1]["balance"] - 2
        r["days"][1]["hi80"] = r["days"][1]["balance"] + 2
        assert any("narrower" in v for v in verify_result(r))


class TestLeakage:
    def test_forecast_blind_to_post_cutoff_rows(self, tmp_path):
        """The forecaster's output must be identical whether post-cutoff data
        exists on disk, is deleted, or is mutated."""
        original = str(tmp_path / "orig")
        generate(21, original, days=140)
        cutoff = START_DATE + timedelta(days=110)
        cutoff_iso = cutoff.isoformat()
        cutoff_bank = cutoff.strftime("%d/%m/%Y")

        def bank_date_le(s):
            d, m, y = s.split("/")
            return f"{y}-{m}-{d}" <= cutoff_iso

        truncated = str(tmp_path / "trunc")
        shutil.copytree(original, truncated)
        # drop every post-cutoff row from the three time-series files
        for name, keep in [
            ("bank_statement.csv", lambda r: bank_date_le(r["value_date"])),
            ("settlements.csv", lambda r: r["created_at"][:10] <= cutoff_iso),
            ("payments.csv", lambda r: r["created_at"][:10] <= cutoff_iso),
        ]:
            path = os.path.join(truncated, name)
            with open(path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                cols, rows = reader.fieldnames, [r for r in reader if keep(r)]
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)

        mutated = str(tmp_path / "mut")
        shutil.copytree(original, mutated)
        path = os.path.join(mutated, "bank_statement.csv")
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            cols, rows = reader.fieldnames, list(reader)
        for r in rows:
            if not bank_date_le(r["value_date"]):
                r["credit_amount"], r["debit_amount"] = "", "9,99,999.99"
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

        results = []
        for d in (original, truncated, mutated):
            inp = build_input(load_world(d), cutoff)
            results.append(forecast(inp, 14).to_dict())
        assert results[0] == results[1] == results[2]


class TestActuals:
    def test_actuals_match_statement_running_balance(self, tmp_path):
        d = str(tmp_path / "w")
        generate(23, d, days=120)
        world = load_world(d)
        cutoff = START_DATE + timedelta(days=100)
        a = actuals_for(world, cutoff, 14)
        # balance path must agree with the statement's own running balance
        from recon.normalize import parse_bank_date
        by_day = {}
        for r in world["bank_rows"]:
            by_day[parse_bank_date(r.value_date)] = r.balance_paise
        for h in range(1, 15):
            day = cutoff + timedelta(days=h)
            if day in by_day:
                assert a["balances"][h - 1] == by_day[day]
