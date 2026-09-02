"""Independent invariant checks on forecast output.

Same philosophy as recon/verify.py: no shared logic with the forecaster —
this file recomputes everything from the raw fields of the result and fails
the backtest loudly on any inconsistency.
"""

from __future__ import annotations

from datetime import date, timedelta

_COMPONENT_FIELDS = ("known_inflows", "projected_sales_inflows", "other_income",
                     "recurring_outflows", "variable_outflows")


def verify_result(result: dict) -> list[str]:
    v: list[str] = []
    days = result.get("days", [])
    if len(days) != result.get("horizon"):
        v.append(f"{len(days)} day rows for horizon {result.get('horizon')}")

    cutoff = date.fromisoformat(result["cutoff"])
    balance = result["opening_balance_paise"]
    prev_width = None
    for h, d in enumerate(days, start=1):
        if d["date"] != (cutoff + timedelta(days=h)).isoformat():
            v.append(f"day {h}: date {d['date']} not cutoff+{h}")
        expected_net = (d["known_inflows"] + d["projected_sales_inflows"]
                        + d["other_income"] - d["recurring_outflows"]
                        - d["variable_outflows"])
        if d["net"] != expected_net:
            v.append(f"day {h}: net {d['net']} != component sum {expected_net}")
        balance += d["net"]
        if d["balance"] != balance:
            v.append(f"day {h}: balance {d['balance']} != running {balance}")
        for f in _COMPONENT_FIELDS + ("net", "balance"):
            if not isinstance(d[f], int):
                v.append(f"day {h}: {f} is not an integer")
        lo, hi = d.get("lo80"), d.get("hi80")
        if (lo is None) != (hi is None):
            v.append(f"day {h}: half-open band")
        if lo is not None:
            if lo > hi:
                v.append(f"day {h}: lo80 {lo} > hi80 {hi}")
            width = hi - lo
            if prev_width is not None and width < prev_width:
                v.append(f"day {h}: band width {width} narrower than day {h-1}")
            prev_width = width

    if days:
        worst = min(days, key=lambda d: d["balance"])
        mb = result.get("min_balance", {})
        if mb.get("paise") != worst["balance"]:
            v.append(f"min_balance {mb.get('paise')} != actual min {worst['balance']}")
        thr = result.get("threshold_paise")
        if thr is not None:
            below = [d["date"] for d in days if d["balance"] < thr]
            first = below[0] if below else None
            if result.get("first_below_threshold") != first:
                v.append("first_below_threshold inconsistent with day rows")
    return v
