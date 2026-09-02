"""Layer (c): statistical residual — transparent weekday arithmetic, no ML.

Covers what the other layers deliberately do not: sales that have not been
captured yet, unscheduled variable spend, and small non-settlement income.
Everything is a trimmed mean over trailing same-weekday history with an
explicit, clamped trend ratio; every number is reproducible by hand.
"""

from __future__ import annotations

from datetime import date, timedelta

from recon import schemas as RS
from recon.normalize import parse_bank_date, parse_iso_date, roll_off_sunday
from forecast.schemas import ForecastInput

MIN_HISTORY_DAYS = 56
TRIMMED_WINDOW = 8            # trailing same-weekdays considered
TREND_CLAMP_X1000 = (800, 1250)


def _trimmed6_mean(values: list[int]) -> int:
    """Drop min and max of (up to) the last 8, integer mean of the rest."""
    if not values:
        return 0
    tail = values[-TRIMMED_WINDOW:]
    if len(tail) > 2:
        tail = sorted(tail)[1:-1]
    return sum(tail) // len(tail)


def capture_net_by_day(inp: ForecastInput) -> dict[date, int]:
    out: dict[date, int] = {}
    for p in inp.payments:
        if p["status"] != "captured":
            continue
        d = parse_iso_date(p["created_at"])
        out[d] = out.get(d, 0) + (p["amount_paise"] - p["fee_paise"] - p["tax_paise"])
    return out


def _weekday_history(series: dict[date, int], cutoff: date,
                     first: date) -> dict[int, list[int]]:
    by_weekday: dict[int, list[int]] = {i: [] for i in range(7)}
    d = first
    while d <= cutoff:
        by_weekday[d.weekday()].append(series.get(d, 0))
        d += timedelta(days=1)
    return by_weekday


def trend_ratio_x1000(series: dict[date, int], cutoff: date) -> int:
    last28 = sum(series.get(cutoff - timedelta(days=i), 0) for i in range(28))
    prior28 = sum(series.get(cutoff - timedelta(days=i), 0) for i in range(28, 56))
    if prior28 <= 0:
        return 1000
    ratio = last28 * 1000 // prior28
    lo, hi = TREND_CLAMP_X1000
    return max(lo, min(hi, ratio))


def project_sales(inp: ForecastInput, horizon: int,
                  include_known_capture_days: bool) -> dict[date, int]:
    """Estimated settlement credits from sales, placed at capture+2 (rolled).

    Capture days <= cutoff are normally the pipeline layer's job (it knows the
    real amounts); with `include_known_capture_days` (the no-pipeline
    ablation) they are estimated statistically instead, so the ablation
    measures the value of knowing vs estimating — not the absence of a layer.
    """
    end = inp.cutoff + timedelta(days=horizon)
    captures = capture_net_by_day(inp)
    if inp.first_statement_date is None:
        return {}
    by_weekday = _weekday_history(captures, inp.cutoff, inp.first_statement_date)
    ratio = trend_ratio_x1000(captures, inp.cutoff)

    out: dict[date, int] = {}
    capture_day = inp.cutoff - timedelta(days=4)
    while capture_day <= end:
        capture_day += timedelta(days=1)
        posted = roll_off_sunday(capture_day + timedelta(days=2))
        if not (inp.cutoff < posted <= end):
            continue
        if capture_day <= inp.cutoff and not include_known_capture_days:
            continue   # pipeline layer carries these with exact amounts
        est = _trimmed6_mean(by_weekday[capture_day.weekday()]) * ratio // 1000
        if est > 0:
            out[posted] = out.get(posted, 0) + est
    return out


def project_variable_spend(inp: ForecastInput, horizon: int,
                           recurring_txn_ids: set[str]) -> dict[date, int]:
    """Per-weekday trimmed mean of debit totals EXCLUDING detected recurring
    rows (the double-count guard). Spend is treated as stationary."""
    if inp.first_statement_date is None:
        return {}
    daily: dict[date, int] = {}
    for r in inp.bank_rows:
        if r.debit_paise > 0 and r.txn_id not in recurring_txn_ids:
            d = parse_bank_date(r.value_date)
            daily[d] = daily.get(d, 0) + r.debit_paise
    by_weekday = _weekday_history(daily, inp.cutoff, inp.first_statement_date)

    out: dict[date, int] = {}
    for h in range(1, horizon + 1):
        d = inp.cutoff + timedelta(days=h)
        est = _trimmed6_mean(by_weekday[d.weekday()])
        if est > 0:
            out[d] = est
    return out


def project_other_income(inp: ForecastInput, horizon: int,
                         decisions: list[RS.Decision]) -> dict[date, int]:
    """Non-settlement credits (classified by the recon engine) as a flat
    trailing-56-day daily mean — small by construction, but not ignored."""
    other_ids = {tid for d in decisions if d.status == RS.NON_SETTLEMENT_CREDIT
                 for tid in d.bank_txn_ids}
    window_start = inp.cutoff - timedelta(days=55)
    total = sum(r.credit_paise for r in inp.bank_rows
                if r.txn_id in other_ids
                and parse_bank_date(r.value_date) >= window_start)
    per_day = total // 56
    if per_day <= 0:
        return {}
    return {inp.cutoff + timedelta(days=h): per_day for h in range(1, horizon + 1)}
