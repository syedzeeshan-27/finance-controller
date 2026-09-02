"""Self-calibrating empirical uncertainty bands.

The same forecaster is re-run at six internal historical cutoffs (T-7, T-14,
... T-42); its errors against the ALREADY-OBSERVED days become per-horizon-day
error quantiles. No parametric assumptions, no leakage (internal cutoffs only
look backwards), and realized coverage is measured by the backtest — never
asserted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Callable

from forecast.schemas import ForecastInput
from forecast.slicing import daily_net_series, reslice

INTERNAL_CUTOFFS = 6           # weekly spacing: T-7 .. T-42
MIN_HISTORY_FOR_BANDS = 98     # 56 (statistical layer) + 42 (internal span)


def compute_band_offsets(inp: ForecastInput, horizon: int,
                         run: Callable[[ForecastInput, int], object]
                         ) -> tuple[list[tuple[int, int]] | None, dict | None]:
    """Returns ([(lo_offset, hi_offset)] per horizon day, calibration meta)."""
    if inp.history_days < MIN_HISTORY_FOR_BANDS:
        return None, None

    observed = dict(daily_net_series(inp))
    errors_by_h: dict[int, list[int]] = {h: [] for h in range(1, horizon + 1)}

    for k in range(1, INTERNAL_CUTOFFS + 1):
        internal_cutoff = inp.cutoff - timedelta(days=7 * k)
        sub = reslice(inp, internal_cutoff)
        result = run(sub, horizon)
        forecast_cum = 0
        actual_cum = 0
        for h, day in enumerate(result.days, start=1):
            forecast_cum += day.net
            actual_cum += observed.get(internal_cutoff + timedelta(days=h), 0)
            errors_by_h[h].append(actual_cum - forecast_cum)

    offsets: list[tuple[int, int]] = []
    lo_prev, hi_prev = 0, 0
    for h in range(1, horizon + 1):
        pooled: list[int] = []
        for hh in (h - 1, h, h + 1):
            pooled.extend(errors_by_h.get(max(1, min(horizon, hh)), []))
        pooled.sort()
        lo, hi = pooled[1], pooled[-2]     # ~10th/90th pct of ~18 samples
        # monotone envelope: uncertainty never shrinks with horizon
        lo, hi = min(lo, lo_prev), max(hi, hi_prev)
        lo_prev, hi_prev = lo, hi
        offsets.append((lo, hi))

    meta = {"n_internal_cutoffs": INTERNAL_CUTOFFS,
            "method": "pooled order statistics of cumulative-net errors "
                      "(h-1..h+1), monotone envelope"}
    return offsets, meta
