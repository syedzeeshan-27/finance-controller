"""Baseline forecasters the real one must beat — measured, not asserted.

All three share the forecaster signature and output shape so the backtest
grades every strategy identically.
"""

from __future__ import annotations

from datetime import timedelta

from forecast.schemas import DayForecast, ForecastInput, ForecastResult
from forecast.slicing import daily_net_series


def _result(name: str, inp: ForecastInput, horizon: int,
            nets: list[int]) -> ForecastResult:
    res = ForecastResult(strategy=name, cutoff=inp.cutoff.isoformat(),
                         horizon=horizon,
                         opening_balance_paise=inp.opening_balance_paise)
    balance = inp.opening_balance_paise
    for h in range(1, horizon + 1):
        net = nets[h - 1]
        balance += net
        res.days.append(DayForecast(
            date=(inp.cutoff + timedelta(days=h)).isoformat(),
            net=net, balance=balance))
    worst = min(res.days, key=lambda d: d.balance)
    res.min_balance = {"date": worst.date, "paise": worst.balance}
    return res


def zero_net(inp: ForecastInput, horizon: int = 14) -> ForecastResult:
    """Balance stays flat: the do-nothing forecast."""
    return _result("zero_net", inp, horizon, [0] * horizon)


def trailing_mean_28(inp: ForecastInput, horizon: int = 14) -> ForecastResult:
    """Every future day gets the mean daily net of the last 28 observed days."""
    series = daily_net_series(inp)
    tail = series[-28:]
    mean = sum(v for _, v in tail) // len(tail) if tail else 0
    return _result("trailing_mean_28", inp, horizon, [mean] * horizon)


def naive_weekday(inp: ForecastInput, horizon: int = 14) -> ForecastResult:
    """Tomorrow = same weekday last week: cycle the last 7 observed days."""
    series = daily_net_series(inp)
    last7 = [v for _, v in series[-7:]]
    if len(last7) < 7:
        last7 = (last7 + [0] * 7)[:7]
    nets = [last7[(h - 1) % 7] for h in range(1, horizon + 1)]
    return _result("naive_weekday", inp, horizon, nets)


BASELINES = {
    "zero_net": zero_net,
    "trailing_mean_28": trailing_mean_28,
    "naive_weekday": naive_weekday,
}
