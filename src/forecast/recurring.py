"""Layer (b): recurring-obligation detection and projection.

Deterministic detection of scheduled outflows (payroll, rent, GST, ...) from
narration-template keys and gap analysis — no ML, every accepted key passes
explicit periodicity, anchor-consistency and amount-stability gates, so a
rejected key falls through to the statistical layer instead of being guessed.

Amounts follow "known money first": where the books determine the next
instance (GST = the compliance loop's published 3%-of-prior-month-gross rule)
it is computed from the merchant's own captured payments; every other key is
projected from its own history.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from recon.normalize import parse_bank_date, parse_iso_date, roll_off_sunday
from forecast.schemas import ForecastInput
from tax import rules as TX     # cycle-free: tax.rules imports only recon.normalize

_DIGITS = re.compile(r"\d{4,}")

MIN_OCCURRENCES = 3
MONTHLY_GAP = (27, 34)
WEEKLY_GAP = (6, 8)
QUARTERLY_GAP = (85, 97)
ANCHOR_TOLERANCE_DAYS = 3
ANCHOR_CONSISTENCY = 0.8       # share of occurrences near the anchor
STABLE_BP = 1_000              # <= 10% deviation from median: stable
# Sales-linked obligations (GST = % of prior-month sales) legitimately swing
# well past 35% of the median when the business is growing and the first
# month runs low; 50% keeps them while random-noise templates (uniform draws
# over wide ranges) still fail both this and the gap-consistency gate.
VARIABLE_BP = 5_000            # <= 50%: variable; beyond: rejected
LIVENESS_PERIODS_X10 = 15      # last hit within 1.5 periods of cutoff


def template_key(narration: str) -> str:
    """Collapse the random digit runs so recurring rows share one key."""
    return _DIGITS.sub("#", narration.upper()).strip()


@dataclass
class Detected:
    key: str
    period: str                     # monthly | weekly | quarterly
    anchor: int                     # day-of-month (monthly/quarterly) or weekday
    amount_class: str               # fixed | stable | variable
    occurrences: list[tuple[date, int]] = field(default_factory=list)
    txn_ids: list[str] = field(default_factory=list)
    rule_amount_paise: int | None = None   # next amount the books determine

    @property
    def projected_amount(self) -> int:
        if self.rule_amount_paise is not None:
            return self.rule_amount_paise
        last3 = [a for _, a in self.occurrences[-3:]]
        if len(last3) == 3:
            d1, d2 = last3[1] - last3[0], last3[2] - last3[1]
            if (d1 > 0 and d2 > 0) or (d1 < 0 and d2 < 0):   # strictly monotonic
                return last3[2] + int(statistics.median([d1, d2]))
        return int(statistics.median(last3))


def _period_days(period: str) -> int:
    return {"weekly": 7, "monthly": 30, "quarterly": 91}[period]


def _classify_amounts(amounts: list[int]) -> str | None:
    med = int(statistics.median(amounts))
    if med <= 0:
        return None
    worst_bp = max(abs(a - med) * 10_000 // med for a in amounts)
    if worst_bp == 0:
        return "fixed"
    if worst_bp <= STABLE_BP:
        return "stable"
    if worst_bp <= VARIABLE_BP:
        return "variable"
    return None


def _gap_class(gaps: list[int]) -> str | None:
    med = statistics.median(gaps)
    for period, (lo, hi) in (("weekly", WEEKLY_GAP), ("monthly", MONTHLY_GAP),
                             ("quarterly", QUARTERLY_GAP)):
        if lo <= med <= hi:
            # gaps must be consistent, not just centred: random noise streams
            # can produce a plausible MEDIAN gap but never consistent gaps
            tol = 2 if period == "weekly" else 4
            if all(abs(g - med) <= tol for g in gaps):
                return period
    return None


# Known money first. GST is the one obligation whose level tracks sales
# rather than a contract, so extrapolating its history guesses where the
# compliance loop's published rule (3% of the previous month's captured
# gross, tax/rules.py) simply computes. Same recogniser as tax/books.py.
_GST_KEY_MARK = "GST PAYMENT-CBIC"


def _books_rule_amount(key: str, last_date: date,
                       inp: ForecastInput) -> int | None:
    """The next GST instance from the merchant's own captured payments, using
    the compliance loop's month convention (month 0 of the data = the first
    30 days' gross). None when the basis month has no captured payments —
    the caller then falls back to history."""
    if _GST_KEY_MARK not in key:
        return None
    captured = [(parse_iso_date(p["created_at"]), p["amount_paise"])
                for p in inp.payments if p["status"] == "captured"]
    if not captured:
        return None
    start = min(d for d, _ in captured)
    if inp.first_statement_date is not None:
        start = min(start, inp.first_statement_date)

    def month_index(d: date) -> int:
        return (d.year - start.year) * 12 + (d.month - start.month)

    ny, nm = last_date.year, last_date.month + 1        # next instance
    ny, nm = ny + (nm - 1) // 12, (nm - 1) % 12 + 1
    basis = month_index(date(ny, nm, 1)) - 1            # previous month
    if basis < 0:
        return None
    if basis == 0:
        gross = sum(a for d, a in captured if (d - start).days < 30)
    else:
        gross = sum(a for d, a in captured if month_index(d) == basis)
    return TX.gst_liability(gross) if gross > 0 else None


def detect(inp: ForecastInput) -> list[Detected]:
    occurrences: dict[str, list[tuple[date, int, str]]] = {}
    for r in inp.bank_rows:
        if r.debit_paise <= 0:
            continue
        key = template_key(r.narration)
        occurrences.setdefault(key, []).append(
            (parse_bank_date(r.value_date), r.debit_paise, r.txn_id))

    detected: list[Detected] = []
    for key, occ in sorted(occurrences.items()):
        occ.sort()
        dates = [d for d, _, _ in occ]
        amounts = [a for _, a, _ in occ]

        period = None
        if len(occ) >= MIN_OCCURRENCES:
            gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
            period = _gap_class(gaps)
        elif (len(occ) == 2 and amounts[0] == amounts[1]
                and QUARTERLY_GAP[0] <= (dates[1] - dates[0]).days <= QUARTERLY_GAP[1]):
            period = "quarterly"   # e.g. insurance: only 2 hits fit in-world
        if period is None:
            continue

        amount_class = _classify_amounts(amounts)
        if amount_class is None:
            continue

        if period in ("monthly", "quarterly"):
            anchor = int(statistics.median(d.day for d in dates))
            near = sum(1 for d in dates
                       if min(abs(d.day - anchor), 31 - abs(d.day - anchor))
                       <= ANCHOR_TOLERANCE_DAYS)
            if near < ANCHOR_CONSISTENCY * len(dates):
                continue
        else:
            anchor = statistics.mode(d.weekday() for d in dates)

        lively = (inp.cutoff - dates[-1]).days * 10 \
            <= _period_days(period) * LIVENESS_PERIODS_X10
        if not lively:
            continue

        detected.append(Detected(
            key=key, period=period, anchor=anchor, amount_class=amount_class,
            occurrences=[(d, a) for d, a, _ in occ],
            txn_ids=[t for _, _, t in occ],
            rule_amount_paise=_books_rule_amount(key, dates[-1], inp)))
    return detected


def _days_in_month(year: int, month: int) -> int:
    nxt = date(year, month, 28) + timedelta(days=4)
    return (nxt.replace(day=1) - date(year, month, 1)).days


def project(detected: list[Detected], cutoff: date, horizon: int) -> list[dict]:
    """Future obligation instances inside (cutoff, cutoff + horizon]."""
    end = cutoff + timedelta(days=horizon)
    out: list[dict] = []
    for det in detected:
        # "rule" = amount computed from the books, not extrapolated
        basis = "rule" if det.rule_amount_paise is not None else det.amount_class
        if det.period == "weekly":
            d = cutoff + timedelta(days=1)
            while d <= end:
                if d.weekday() == det.anchor:
                    out.append({"key": det.key, "due_date": d.isoformat(),
                                "amount_paise": det.projected_amount,
                                "basis": basis, "period": det.period})
                d += timedelta(days=1)
            continue
        step_months = 1 if det.period == "monthly" else 3
        last_date = det.occurrences[-1][0]
        y, m = last_date.year, last_date.month
        for _ in range(6):   # more instances than any 14-day horizon can hold
            m += step_months
            y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
            due = roll_off_sunday(date(y, m, min(det.anchor, _days_in_month(y, m))))
            if cutoff < due <= end:
                out.append({"key": det.key, "due_date": due.isoformat(),
                            "amount_paise": det.projected_amount,
                            "basis": basis, "period": det.period})
    return sorted(out, key=lambda o: (o["due_date"], o["key"]))
