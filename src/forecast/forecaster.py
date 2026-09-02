"""The composed cash forecaster: three deterministic layers, one balance path.

  layer a (pipeline)  — money already captured: KNOWN inflows, not estimates
  layer b (recurring) — detected scheduled obligations, projected forward
  layer c (residual)  — transparent weekday statistics for everything else

Every layer's contribution stays separable in the output (that is what makes
the backtest's ablation table possible), and the composition is verified by
forecast/verify.py invariants on every run.
"""

from __future__ import annotations

from datetime import timedelta

from forecast import bands as B
from forecast import pipeline as P
from forecast import recurring as R
from forecast import residual as C
from forecast.schemas import DayForecast, ForecastInput, ForecastResult


def forecast(inp: ForecastInput, horizon: int = 14, *,
             enable_pipeline: bool = True,
             enable_recurring: bool = True,
             enable_residual: bool = True,
             threshold_paise: int | None = None,
             with_bands: bool = True,
             strategy_name: str = "forecaster",
             decisions=None) -> ForecastResult:
    res = ForecastResult(strategy=strategy_name, cutoff=inp.cutoff.isoformat(),
                         horizon=horizon,
                         opening_balance_paise=inp.opening_balance_paise,
                         threshold_paise=threshold_paise)

    # `decisions` is the controller's seam: valid only when the caller's leg A
    # run saw exactly this input's records (the close checks that). Band
    # calibration below never forwards it — sub-sliced history must re-recon.
    if decisions is None:
        decisions = P.run_recon(inp)

    known = {"by_date": {}, "in_flight": [], "attention": []}
    if enable_pipeline:
        known = P.known_inflows(inp, horizon, decisions)
        res.in_flight = known["in_flight"]
        res.attention = known["attention"]

    recurring_by_date: dict = {}
    consumed_txn_ids: set[str] = set()
    if enable_recurring:
        detected = R.detect(inp)
        for det in detected:
            consumed_txn_ids.update(det.txn_ids)
        res.obligations = R.project(detected, inp.cutoff, horizon)
        for o in res.obligations:
            recurring_by_date[o["due_date"]] = (
                recurring_by_date.get(o["due_date"], 0) + o["amount_paise"])

    sales: dict = {}
    spend: dict = {}
    other: dict = {}
    if enable_residual:
        if inp.history_days >= C.MIN_HISTORY_DAYS:
            sales = C.project_sales(inp, horizon,
                                    include_known_capture_days=not enable_pipeline)
            spend = C.project_variable_spend(inp, horizon, consumed_txn_ids)
            other = C.project_other_income(inp, horizon, decisions)
        else:
            res.warnings.append(
                f"insufficient history ({inp.history_days} < "
                f"{C.MIN_HISTORY_DAYS} days): statistical layer disabled")

    balance = inp.opening_balance_paise
    for h in range(1, horizon + 1):
        d = inp.cutoff + timedelta(days=h)
        iso = d.isoformat()
        day = DayForecast(
            date=iso,
            known_inflows=known["by_date"].get(d, 0),
            projected_sales_inflows=sales.get(d, 0),
            other_income=other.get(d, 0),
            recurring_outflows=recurring_by_date.get(iso, 0),
            variable_outflows=spend.get(d, 0),
        )
        day.net = (day.known_inflows + day.projected_sales_inflows
                   + day.other_income - day.recurring_outflows
                   - day.variable_outflows)
        balance += day.net
        day.balance = balance
        res.days.append(day)

    if with_bands:
        def run_internal(sub: ForecastInput, hz: int) -> ForecastResult:
            return forecast(sub, hz, enable_pipeline=enable_pipeline,
                            enable_recurring=enable_recurring,
                            enable_residual=enable_residual,
                            with_bands=False, strategy_name="internal")

        offsets, meta = B.compute_band_offsets(inp, horizon, run_internal)
        if offsets is None:
            res.warnings.append(
                f"history {inp.history_days} < {B.MIN_HISTORY_FOR_BANDS} days: "
                "bands unavailable")
        else:
            for day, (lo, hi) in zip(res.days, offsets):
                day.lo80 = day.balance + lo
                day.hi80 = day.balance + hi
            res.calibration = meta

    worst = min(res.days, key=lambda x: x.balance)
    res.min_balance = {"date": worst.date, "paise": worst.balance,
                       "lo80": worst.lo80, "hi80": worst.hi80}
    if threshold_paise is not None:
        below = [x.date for x in res.days if x.balance < threshold_paise]
        res.first_below_threshold = below[0] if below else None
    return res
