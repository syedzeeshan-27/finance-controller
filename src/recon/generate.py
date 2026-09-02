"""Synthetic Razorpay-merchant world generator with scenario-tagged ground truth.

Builds, for one seed: a merchant order book, Razorpay payment records,
settlement records (UTR, T+N), and a bank statement whose credits carry the
settlement money through realistically messy narrations — plus golden files
that record, at mint time, what the correct reconciliation outcome is for
every settlement, bank credit, order and payment.

Design rules:
  - Ground truth is written the moment a record is constructed. No later
    labelling step, no LLM anywhere.
  - The world is built clean first; labelled scenario mutations are applied to
    chosen subsets. Every mutation writes its own golden rows.
  - Ambiguity is constructed, not accidental: a global amount-separation audit
    keeps all settlement nets at least MIN_SEPARATION apart EXCEPT the pairs a
    scenario deliberately places closer (twins: equal; near-collisions: Rs 1-9).
    So when the golden file says "unique amount", it is provably unique, and
    when it says "ambiguous", abstention is provably the only defensible call.
  - Bank narrations never embed generator-internal identifiers. The UTR is the
    only legitimate join key, present only where the scenario says so.

Stage 2 additions: the world has forecastable STRUCTURE — weekday/trend/
month-end payment volume and scheduled recurring obligations (payroll, rent,
GST, TDS, AWS, insurance, telecom) whose true schedule is minted into
golden_obligations.csv at generation time. The post-cutoff statement rows ARE
the forecast ground truth; the obligations golden exists only to grade
recurring-schedule DETECTION, never to feed a forecaster.

Stage 3 additions: the world has a TAX side — gstr2b.csv (what vendors filed,
with injected filing defects), form26as.csv (what the 194-O deductor filed,
from the TDS branch of the discrepancy scenario), two GST compliance defects
(one short-paid month, one late month sized inside Stage 2's detection
gates), and golden_tax.csv recording the expected disposition of every
purchase, 2B line, TDS event, 26AS entry and statutory payment period.

CLI:
    python -m recon.generate --seed 42 [--days 180] [--out DIR] [--verify-determinism]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from random import Random

from recon import schemas as S
from recon.normalize import (
    bank_date_str, gst_on_fee, roll_off_sunday, rupee_str_from_paise,
)
from tax import registry as TR
from tax import rules as TX
from tax import schemas as TS

GENERATOR_VERSION = "3.0.0"

START_DATE = date(2025, 4, 1)          # FY 2025-26 Q1
DAYS = 90                              # default world length; --days overrides
OPENING_BALANCE_PAISE = 120_000_000    # Rs 12,00,000.00

# --- Payment-volume structure (integer x1000 arithmetic; no floats) -----------
# Daily expected payment count = BASE * weekday * trend * month_end / 1e12,
# round half up, plus a +-2 draw from the dedicated ":volume" RNG stream.
# RNG discipline: each named substream is consumed by exactly ONE concern; new
# concerns get new stream names; no stream ever borrows draws from another.
_WEEKDAY_X1000 = [950, 900, 950, 1000, 1150, 1350, 700]   # Mon..Sun retail shape
_MONTH_END_X1000 = 1250        # last 3 calendar days of the month
BASE_DAILY_X1000 = 9500        # ~9.5 payments/day at trend=1, weekday=1


def _trend_x1000(day_idx: int) -> int:
    """Linear growth: +30% by day 180."""
    return 1000 + (5 * day_idx) // 3


def _month_end_x1000(d: date) -> int:
    next_month = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_in_month = (next_month - d.replace(day=1)).days
    return _MONTH_END_X1000 if d.day > days_in_month - 3 else 1000


def _expected_payment_count(d: date, day_idx: int) -> int:
    lam = (BASE_DAILY_X1000 * _WEEKDAY_X1000[d.weekday()]
           * _trend_x1000(day_idx) * _month_end_x1000(d))
    return (lam + 5 * 10**11) // 10**12   # round half up from x1e12

MIN_SEPARATION_PAISE = 1_000           # Rs 10: nets this close are "colliding"
SETTLEMENT_WINDOW_DAYS = 10

# Method economics: (weight, fee basis points)
METHODS = {
    "upi":        (55, 30),
    "card":       (30, 200),
    "netbanking": (10, 170),
    "wallet":     (5, 200),
}

BANK_PREFIXES = ["UTIB", "HDFC", "ICIC", "SBIN", "KKBK", "YESB", "IDFB"]

BANK_CHARGE_PAISE = 2_950              # NEFT/RTGS charge Rs 25 + 18% GST

# Scenario tags (leg A)
SC_CLEAN = "clean_exact_utr"
SC_TRUNCATED = "utr_truncated"
SC_MANGLED_SEP = "utr_mangled_separators"
SC_MANGLED_TYPO = "utr_mangled_typo"
SC_AMOUNT_ONLY = "utr_absent_amount_unique"
SC_TWINS = "ambiguous_twins"
SC_NEAR_COLLISION = "near_collision"
SC_MISSING_BANK = "missing_bank_credit"
SC_ORPHAN_CREDIT = "orphan_bank_credit"
SC_DUPLICATE = "duplicate_bank_credit"
SC_SPLIT = "split_settlement"
SC_MERGED = "merged_credits"
SC_DISCREPANCY = "matched_with_discrepancy"
SC_DELAYED = "delayed_settlement"
SC_NOISE_CREDIT = "noise_credit"

# Leg B scenario tags
SB_CLEAN = "order_paid_clean"
SB_UNPAID = "order_unpaid"
SB_CANCELLED = "order_cancelled"
SB_DUP_PAYMENT = "duplicate_payment"
SB_ORPHAN_PAYMENT = "payment_no_order"
SB_MISMATCH = "amount_mismatch"
SB_REFUNDED = "order_refunded"
SB_FAILED_RETRY = "failed_then_captured"


# --- Internal build records ---------------------------------------------------

@dataclass
class GenPayment:
    payment_id: str
    order_id: str
    method: str
    amount_paise: int
    fee_paise: int
    tax_paise: int
    status: str                  # captured | refunded | failed
    created_at: datetime
    settlement_id: str = ""
    legb_disposition: str = S.PAYMENT_APPLIED
    legb_counterparties: list[str] = field(default_factory=list)
    legb_scenario: str = SB_CLEAN
    legb_notes: str = ""

    @property
    def net_contribution(self) -> int:
        return self.amount_paise - self.fee_paise - self.tax_paise


@dataclass
class GenOrder:
    order_id: str
    receipt: str
    amount_paise: int
    status: str                  # created | paid | cancelled
    created_at: datetime
    legb_disposition: str = S.ORDER_PAID
    legb_counterparties: list[str] = field(default_factory=list)
    legb_scenario: str = SB_CLEAN
    legb_notes: str = ""


@dataclass
class GenSettlement:
    settlement_id: str
    utr: str
    created_at: datetime
    settled_at: date             # expected bank credit date
    members: list[GenPayment] = field(default_factory=list)
    scenario: str = SC_CLEAN
    disposition: str = S.MATCHED
    counterparties: list["GenCredit"] = field(default_factory=list)
    expected_discrepancy_paise: int = 0
    notes: str = ""
    tds_deducted_paise: int = 0  # >0 when the discrepancy scenario chose the
                                 # 1% TDS branch; feeds Form 26AS minting

    @property
    def amount_paise(self) -> int:
        return sum(p.net_contribution for p in self.members)

    @property
    def fees_paise(self) -> int:
        return sum(p.fee_paise for p in self.members)

    @property
    def tax_paise(self) -> int:
        return sum(p.tax_paise for p in self.members)


@dataclass
class GenCredit:
    """A bank credit row before txn_ids/balances are assigned."""
    value_date: date
    narration: str
    ref_no: str
    amount_paise: int
    disposition: str
    scenario: str
    counterparties: list[GenSettlement] = field(default_factory=list)
    expected_discrepancy_paise: int = 0
    notes: str = ""
    razorpay_shaped: bool = True
    txn_id: str = ""             # assigned after statement ordering


@dataclass
class GenDebit:
    value_date: date
    narration: str
    ref_no: str
    amount_paise: int
    obligation_key: str = ""     # non-empty for scheduled recurring obligations
    due_date: date | None = None
    txn_id: str = ""


# --- ID minting ---------------------------------------------------------------

class Ids:
    """Deterministic, readable synthetic identifiers."""

    def __init__(self, seed: int):
        self._rng = Random(f"{seed}:ids")
        self._minted: set[str] = set()
        self._utrs: list[str] = []

    def _token(self, length: int = 14) -> str:
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        while True:
            t = "".join(self._rng.choice(alphabet) for _ in range(length))
            if t not in self._minted:
                self._minted.add(t)
                return t

    def payment(self) -> str:
        return "pay_" + self._token()

    def order(self) -> str:
        return "order_" + self._token()

    def settlement(self) -> str:
        return "setl_" + self._token()

    def utr(self) -> str:
        while True:
            u = self._rng.choice(BANK_PREFIXES) + "".join(
                self._rng.choice("0123456789") for _ in range(12))
            # No two minted UTRs may sit within edit distance 1 of each other,
            # otherwise a one-digit narration typo could legitimately point at
            # two different settlements and the typo scenario's golden label
            # ("matched") would be unsound.
            if u in self._minted:
                continue
            if any(len(u) == len(v) and sum(a != b for a, b in zip(u, v)) <= 2
                   for v in self._utrs):
                continue
            self._minted.add(u)
            self._utrs.append(u)
            return u


# --- World construction -------------------------------------------------------

def _log_uniform_rupees(rng: Random, lo: int, hi: int) -> int:
    """Skewed-small rupee amounts, like real ticket sizes."""
    return int(round(math.exp(rng.uniform(math.log(lo), math.log(hi)))))


def _mint_payment(ids: Ids, rng: Random, order: GenOrder, day: date,
                  status: str = "captured", amount_paise: int | None = None) -> GenPayment:
    method = rng.choices(list(METHODS), weights=[w for w, _ in METHODS.values()])[0]
    amount = order.amount_paise if amount_paise is None else amount_paise
    fee = (amount * METHODS[method][1] + 5_000) // 10_000
    return GenPayment(
        payment_id=ids.payment(), order_id=order.order_id, method=method,
        amount_paise=amount, fee_paise=fee, tax_paise=gst_on_fee(fee),
        status=status,
        created_at=datetime.combine(day, time(rng.randint(7, 22), rng.randint(0, 59),
                                              rng.randint(0, 59))),
    )


def _build_orders_and_payments(seed: int, ids: Ids, days: int
                               ) -> tuple[list[GenOrder], list[GenPayment]]:
    rng = Random(f"{seed}:payments")
    rng_volume = Random(f"{seed}:volume")
    orders: list[GenOrder] = []
    payments: list[GenPayment] = []
    receipt_no = 0

    for day_idx in range(days):
        day = START_DATE + timedelta(days=day_idx)
        count = max(2, _expected_payment_count(day, day_idx)
                    + rng_volume.randint(-2, 2))
        for _ in range(count):
            receipt_no += 1
            rupees = _log_uniform_rupees(rng, 80, 30_000)
            amount = rupees * 100 + (rng.randint(1, 99) if rng.random() < 0.10 else 0)
            order = GenOrder(
                order_id=ids.order(), receipt=f"RCPT-2025-{receipt_no:05d}",
                amount_paise=amount, status="paid",
                created_at=datetime.combine(day, time(rng.randint(6, 22),
                                                      rng.randint(0, 59))),
            )
            roll = rng.random()
            if roll < 0.04:                                   # never paid
                order.status = "created"
                order.legb_disposition = S.EXCEPTION_UNPAID_ORDER
                order.legb_scenario = SB_UNPAID
                order.legb_notes = "no captured payment ever arrived for this order"
                orders.append(order)
                continue
            if roll < 0.06:                                   # cancelled
                order.status = "cancelled"
                order.legb_disposition = S.ORDER_CANCELLED
                order.legb_scenario = SB_CANCELLED
                orders.append(order)
                continue

            pay = _mint_payment(ids, rng, order, day)
            order.legb_counterparties = [pay.payment_id]
            pay.legb_counterparties = [order.order_id]
            orders.append(order)
            payments.append(pay)

            roll2 = rng.random()
            if roll2 < 0.05:                                  # failed attempt first
                failed = _mint_payment(ids, rng, order, day, status="failed")
                failed.legb_disposition = S.PAYMENT_FAILED
                failed.legb_counterparties = [order.order_id]
                failed.legb_scenario = SB_FAILED_RETRY
                failed.legb_notes = "failed attempt; a later capture succeeded"
                payments.append(failed)
            elif roll2 < 0.065:                               # double charge
                dup = _mint_payment(ids, rng, order, day)
                # the duplicate is the LATER capture — that ordering is what
                # lets a reconciler decide which one is the exception
                dup.created_at = pay.created_at + timedelta(minutes=rng.randint(2, 90))
                dup.legb_disposition = S.EXCEPTION_DUPLICATE_PAYMENT
                dup.legb_counterparties = [order.order_id]
                dup.legb_scenario = SB_DUP_PAYMENT
                dup.legb_notes = "second capture against an already-paid order"
                payments.append(dup)
                pay.legb_scenario = SB_DUP_PAYMENT
            elif roll2 < 0.085:                               # refunded before settlement
                pay.status = "refunded"
                pay.legb_disposition = S.PAYMENT_REFUNDED
                pay.legb_scenario = SB_REFUNDED
                order.legb_disposition = S.ORDER_REFUNDED
                order.legb_scenario = SB_REFUNDED
            elif roll2 < 0.10:                                # partial capture
                short = max(100, (pay.amount_paise * rng.randint(50, 90)) // 100)
                pay.amount_paise = short
                pay.fee_paise = (short * METHODS[pay.method][1] + 5_000) // 10_000
                pay.tax_paise = gst_on_fee(pay.fee_paise)
                pay.legb_disposition = S.EXCEPTION_AMOUNT_MISMATCH
                pay.legb_scenario = SB_MISMATCH
                pay.legb_notes = "captured amount differs from order amount"
                order.legb_disposition = S.EXCEPTION_AMOUNT_MISMATCH
                order.legb_scenario = SB_MISMATCH
                order.legb_notes = "payment captured for a different amount"

    # A few orphan payments: money captured against no known order.
    for _ in range(6 * days // DAYS):
        day = START_DATE + timedelta(days=rng.randint(0, days - 1))
        ghost = GenOrder(order_id="", receipt="", amount_paise=0, status="paid",
                         created_at=datetime.combine(day, time(12, 0)))
        rupees = _log_uniform_rupees(rng, 80, 8_000)
        ghost.amount_paise = rupees * 100
        orphan = _mint_payment(ids, rng, ghost, day)
        orphan.order_id = ""
        orphan.legb_disposition = S.EXCEPTION_PAYMENT_NO_ORDER
        orphan.legb_counterparties = []
        orphan.legb_scenario = SB_ORPHAN_PAYMENT
        orphan.legb_notes = "captured payment carries no order reference"
        payments.append(orphan)

    return orders, payments


def _build_settlements(seed: int, ids: Ids, payments: list[GenPayment]) -> list[GenSettlement]:
    """Daily batching of captured payments; ~30% of days split into two
    settlements by method group (instant settlement product behaviour)."""
    rng = Random(f"{seed}:settlements")
    by_day: dict[date, list[GenPayment]] = {}
    for p in payments:
        if p.status == "captured":
            by_day.setdefault(p.created_at.date(), []).append(p)

    settlements: list[GenSettlement] = []
    for day in sorted(by_day):
        members = by_day[day]
        groups: list[list[GenPayment]]
        upi = [p for p in members if p.method == "upi"]
        rest = [p for p in members if p.method != "upi"]
        if upi and rest and rng.random() < 0.30:
            groups = [upi, rest]
        else:
            groups = [members]
        for group in groups:
            if not group:
                continue
            setl = GenSettlement(
                settlement_id=ids.settlement(), utr=ids.utr(),
                created_at=datetime.combine(day + timedelta(days=1), time(1, 15)),
                settled_at=roll_off_sunday(day + timedelta(days=2)),
                members=group,
            )
            for p in group:
                p.settlement_id = setl.settlement_id
            settlements.append(setl)
    return settlements


def _force_net(setl: GenSettlement, target_paise: int) -> None:
    """Adjust member payment amounts so the settlement net hits target exactly,
    keeping per-payment fee/tax arithmetic consistent. Small adjustments move
    net one-for-one except at rare fee-rounding boundaries, so we correct
    residuals across members until exact."""
    # Only nudge payments that are leg-B-clean: touching one half of a
    # duplicate pair or a mismatch scenario would silently invalidate that
    # scenario's golden label.
    nudgeable = [p for p in setl.members if p.legb_scenario == SB_CLEAN] or setl.members
    for _ in range(12):
        residual = target_paise - setl.amount_paise
        if residual == 0:
            return
        member = max(nudgeable, key=lambda p: p.amount_paise)
        member.amount_paise += residual
        member.fee_paise = (member.amount_paise * METHODS[member.method][1] + 5_000) // 10_000
        member.tax_paise = gst_on_fee(member.fee_paise)
    if setl.amount_paise != target_paise:
        raise AssertionError(
            f"could not force {setl.settlement_id} net to {target_paise}")


def _sync_orders_to_payments(orders: list[GenOrder], payments: list[GenPayment]) -> None:
    """After nudging payment amounts, cleanly-paid orders must still equal
    their payment amount (mismatches are a labelled scenario, not drift)."""
    by_order: dict[str, GenOrder] = {o.order_id: o for o in orders if o.order_id}
    for p in payments:
        if (p.order_id and p.status == "captured"
                and p.legb_disposition == S.PAYMENT_APPLIED):
            o = by_order.get(p.order_id)
            if o is not None and o.legb_disposition == S.ORDER_PAID:
                o.amount_paise = p.amount_paise


@dataclass
class ScenarioPlan:
    singles: dict[str, list[GenSettlement]]
    twin_pairs: list[tuple[GenSettlement, GenSettlement]]
    collision_pairs: list[tuple[GenSettlement, GenSettlement]]
    merge_pairs: list[tuple[GenSettlement, GenSettlement]]


def _assign_scenarios(seed: int, settlements: list[GenSettlement]) -> ScenarioPlan:
    rng = Random(f"{seed}:scenarios")
    pool = settlements[:]
    rng.shuffle(pool)

    def take_pair(max_day_gap: int) -> tuple[GenSettlement, GenSettlement]:
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                gap = abs((pool[i].created_at.date() - pool[j].created_at.date()).days)
                if gap <= max_day_gap:
                    b = pool.pop(j)
                    a = pool.pop(i)
                    return (a, b) if a.created_at <= b.created_at else (b, a)
        raise AssertionError("no pair available within day gap")

    twin_pairs = [take_pair(2) for _ in range(2)]
    collision_pairs = [take_pair(3) for _ in range(3)]
    merge_pairs = [take_pair(1) for _ in range(2)]

    n = len(settlements)
    single_counts = {
        SC_TRUNCATED: max(2, round(n * 0.08)),
        SC_MANGLED_SEP: max(2, round(n * 0.04)),
        SC_MANGLED_TYPO: max(2, round(n * 0.04)),
        SC_AMOUNT_ONLY: max(2, round(n * 0.07)),
        SC_MISSING_BANK: max(2, round(n * 0.07)),
        SC_DUPLICATE: max(2, round(n * 0.04)),
        SC_SPLIT: max(2, round(n * 0.05)),
        SC_DISCREPANCY: max(2, round(n * 0.06)),
        SC_DELAYED: max(2, round(n * 0.04)),
    }
    singles: dict[str, list[GenSettlement]] = {}
    for tag, count in single_counts.items():
        singles[tag] = [pool.pop() for _ in range(min(count, len(pool)))]
    singles[SC_CLEAN] = pool[:]  # remainder
    return ScenarioPlan(singles, twin_pairs, collision_pairs, merge_pairs)


def _separation_audit(settlements: list[GenSettlement], plan: ScenarioPlan,
                      orders: list[GenOrder], payments: list[GenPayment]) -> None:
    """Enforce global amount separation so 'unique amount' and 'ambiguous' are
    provable properties, then apply the designed equalities/collisions.

    Order of operations matters: designed pairs are constructed first, then
    every OTHER pair of nets must sit at least MIN_SEPARATION apart; violators
    that are not part of a designed pair get nudged and we re-check."""
    rng_nudge = Random("nudge")  # deterministic; independent of seed on purpose

    # 1. Designed constructions.
    for a, b in plan.twin_pairs:
        _force_net(b, a.amount_paise)
        for s in (a, b):
            s.scenario = SC_TWINS
    for a, b in plan.collision_pairs:
        delta = rng_nudge.randint(100, 900)
        _force_net(b, a.amount_paise + delta)
        for s in (a, b):
            s.scenario = SC_NEAR_COLLISION
    for a, b in plan.merge_pairs:
        for s in (a, b):
            s.scenario = SC_MERGED

    designed = set()
    for a, b in plan.twin_pairs + plan.collision_pairs:
        designed.add(frozenset((a.settlement_id, b.settlement_id)))

    # 2. Global separation for everything else. Cap scales with world size:
    # bigger worlds have more accidental near-collisions to nudge apart.
    for _ in range(max(80, 2 * len(settlements))):
        violation = None
        ordered = sorted(settlements, key=lambda s: s.amount_paise)
        for x, y in zip(ordered, ordered[1:]):
            if frozenset((x.settlement_id, y.settlement_id)) in designed:
                continue
            if abs(x.amount_paise - y.amount_paise) < MIN_SEPARATION_PAISE:
                violation = (x, y)
                break
        if violation is None:
            break
        x, y = violation
        # Nudge whichever is not part of a designed pair.
        movable = y if all(y.settlement_id not in fs for fs in designed) else x
        _force_net(movable, movable.amount_paise
                   + rng_nudge.randint(MIN_SEPARATION_PAISE, 3 * MIN_SEPARATION_PAISE))
    else:
        raise AssertionError("separation audit failed to converge")

    _sync_orders_to_payments(orders, payments)

    # 3. Prove the properties we will claim in the goldens.
    nets = sorted(s.amount_paise for s in settlements)
    designed_deltas = {abs(a.amount_paise - b.amount_paise)
                       for a, b in plan.twin_pairs + plan.collision_pairs}
    for u, v in zip(nets, nets[1:]):
        if v - u < MIN_SEPARATION_PAISE and (v - u) not in designed_deltas:
            raise AssertionError(f"undesigned near-collision survived audit: {u} vs {v}")


# --- Bank statement construction ---------------------------------------------

_NARRATION_TEMPLATES = [
    "NEFT-{utr}-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT",
    "NEFT CR-{utr}-RAZORPAY SOFTWARE PVT LTD",
    "RTGS-{utr}-RAZORPAY SOFTWARE PL-SETL",
    "BY TRANSFER-NEFT*{utr}*RAZORPAY",
]
_NO_UTR_TEMPLATE = "NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT"


def _narration_for(rng: Random, utr: str) -> str:
    return rng.choice(_NARRATION_TEMPLATES).format(utr=utr)


def _mangle_separators(utr: str) -> str:
    return f"{utr[:4]}-{utr[4:8]} {utr[8:12]} {utr[12:]}"


def _typo(rng: Random, utr: str) -> str:
    pos = rng.randint(4, len(utr) - 1)
    old = utr[pos]
    new = rng.choice([d for d in "0123456789" if d != old])
    return utr[:pos] + new + utr[pos + 1:]


def _ref_for(rng: Random, utr: str | None) -> str:
    if utr is not None and rng.random() < 0.5:
        return utr
    return "NEFTIN" + "".join(rng.choice("0123456789") for _ in range(8))


def _build_bank_credits(seed: int, ids: Ids, plan: ScenarioPlan,
                        settlements: list[GenSettlement]) -> list[GenCredit]:
    rng = Random(f"{seed}:bank")
    credits: list[GenCredit] = []

    def emit(setl: GenSettlement, *, amount: int | None = None,
             value_date: date | None = None, narration: str | None = None,
             ref: str | None = None, disposition: str = S.MATCHED,
             scenario: str | None = None, expected_disc: int = 0,
             notes: str = "") -> GenCredit:
        c = GenCredit(
            value_date=roll_off_sunday(value_date or setl.settled_at),
            narration=narration if narration is not None else _narration_for(rng, setl.utr),
            ref_no=ref if ref is not None else _ref_for(rng, setl.utr),
            amount_paise=amount if amount is not None else setl.amount_paise,
            disposition=disposition,
            scenario=scenario or setl.scenario,
            counterparties=[setl],
            expected_discrepancy_paise=expected_disc,
            notes=notes,
        )
        credits.append(c)
        setl.counterparties.append(c)
        return c

    for setl in plan.singles[SC_CLEAN]:
        setl.scenario = SC_CLEAN
        emit(setl)

    for setl in plan.singles[SC_TRUNCATED]:
        setl.scenario = SC_TRUNCATED
        keep = rng.randint(10, 12)
        frag = setl.utr[-keep:]
        emit(setl, narration=f"NEFT CR {frag} RAZORPAY SOFTWARE", ref=_ref_for(rng, None),
             notes=f"narration carries only the last {keep} chars of the UTR")

    for setl in plan.singles[SC_MANGLED_SEP]:
        setl.scenario = SC_MANGLED_SEP
        emit(setl, narration=f"NEFT-{_mangle_separators(setl.utr)}-RAZORPAY SOFTWARE PVT LTD",
             ref=_ref_for(rng, None),
             notes="UTR broken up by separators in the narration")

    for setl in plan.singles[SC_MANGLED_TYPO]:
        setl.scenario = SC_MANGLED_TYPO
        emit(setl, narration=_narration_for(rng, _typo(rng, setl.utr)),
             ref=_ref_for(rng, None),
             notes="one digit of the UTR corrupted in the narration")

    for setl in plan.singles[SC_AMOUNT_ONLY]:
        setl.scenario = SC_AMOUNT_ONLY
        emit(setl, narration=_NO_UTR_TEMPLATE, ref=_ref_for(rng, None),
             notes="no UTR anywhere; amount is provably unique in the window")

    for setl in plan.singles[SC_MISSING_BANK]:
        setl.scenario = SC_MISSING_BANK
        setl.disposition = S.EXCEPTION_MISSING_BANK
        setl.notes = "settlement processed by PSP but no bank credit ever arrived"
        # no credit emitted

    for setl in plan.singles[SC_DUPLICATE]:
        setl.scenario = SC_DUPLICATE
        first = emit(setl, notes="original credit")
        dup = emit(setl, value_date=first.value_date + timedelta(days=1),
                   narration=first.narration, ref=first.ref_no,
                   disposition=S.DUPLICATE_CREDIT,
                   notes="bank posted the same settlement twice")
        # settlement matches only the first credit
        setl.counterparties = [first]

    for setl in plan.singles[SC_SPLIT]:
        setl.scenario = SC_SPLIT
        setl.disposition = S.MATCHED_SPLIT
        parts = 2 if rng.random() < 0.7 else 3
        amounts = []
        remaining = setl.amount_paise
        for i in range(parts - 1):
            share = remaining * rng.randint(35, 55) // 100
            amounts.append(share)
            remaining -= share
        amounts.append(remaining)
        part_credits = []
        for i, amt in enumerate(amounts, start=1):
            c = emit(setl, amount=amt,
                     narration=f"RTGS-{setl.utr}/{i}-RAZORPAY SOFTWARE PL-PART",
                     ref=setl.utr, disposition=S.MATCHED_SPLIT,
                     notes=f"part {i}/{parts} of a split settlement")
            part_credits.append(c)
        setl.counterparties = part_credits
        for c in part_credits:
            c.counterparties = [setl]

    for i, setl in enumerate(plan.singles[SC_DISCREPANCY]):
        setl.scenario = SC_DISCREPANCY
        setl.disposition = S.MATCHED_WITH_DISCREPANCY
        # Alternate deterministically so every world carries BOTH deduction
        # kinds — the TDS half also feeds Stage 3's Form 26AS loop, which
        # needs a guaranteed minimum of observed deductions per world.
        if i % 2 == 0:
            deduction = BANK_CHARGE_PAISE
            why = "bank NEFT/RTGS charge deducted at credit"
        else:
            deduction = (setl.amount_paise + 50) // 100  # 1% TDS-style, round half up
            why = "1% marketplace TDS-style deduction at credit"
            setl.tds_deducted_paise = deduction
        setl.expected_discrepancy_paise = -deduction
        emit(setl, amount=setl.amount_paise - deduction,
             disposition=S.MATCHED_WITH_DISCREPANCY, expected_disc=-deduction,
             notes=why)

    for setl in plan.singles[SC_DELAYED]:
        setl.scenario = SC_DELAYED
        emit(setl, value_date=setl.settled_at + timedelta(days=rng.randint(3, 5)),
             notes="credit landed T+5..T+7, well outside the usual window")

    # Twins: equal nets, one credit arrives, no UTR anywhere. Nobody can tell
    # which settlement got paid; abstention is the only defensible answer.
    for a, b in plan.twin_pairs:
        paid = rng.choice((a, b))
        credit = GenCredit(
            value_date=roll_off_sunday(max(a.settled_at, b.settled_at)),
            narration=_NO_UTR_TEMPLATE, ref_no=_ref_for(rng, None),
            amount_paise=paid.amount_paise,
            disposition=S.AMBIGUOUS_ABSTAIN, scenario=SC_TWINS,
            counterparties=[a, b],
            notes="two settlements share this exact amount and window; "
                  "no observable evidence distinguishes them",
        )
        credits.append(credit)
        for s in (a, b):
            s.disposition = S.AMBIGUOUS_ABSTAIN
            s.counterparties = [credit]
            s.notes = ("indistinguishable twin settlements; one of them was paid "
                       "by the single arriving credit, the other was not — "
                       "requires bank enquiry")

    # Near-collisions: nets Rs 1-9 apart. One clean UTR, one amount-only.
    for a, b in plan.collision_pairs:
        emit(a, notes="near-collision partner with clean UTR")
        emit(b, narration=_NO_UTR_TEMPLATE, ref=_ref_for(rng, None),
             notes="near-collision partner without UTR; exact-paise amount "
                   "equality is the only safe evidence")

    # Merged: two same/adjacent-day settlements paid as one credit carrying the
    # first settlement's UTR.
    for a, b in plan.merge_pairs:
        credit = GenCredit(
            value_date=roll_off_sunday(max(a.settled_at, b.settled_at)),
            narration=_narration_for(rng, a.utr), ref_no=a.utr,
            amount_paise=a.amount_paise + b.amount_paise,
            disposition=S.MATCHED_MERGED, scenario=SC_MERGED,
            counterparties=[a, b],
            notes="consolidated credit covering two settlements; narration "
                  "carries only the first UTR",
        )
        credits.append(credit)
        for s in (a, b):
            s.disposition = S.MATCHED_MERGED
            s.counterparties = [credit]

    # Orphan razorpay-shaped credits: tempting amounts near clean settlements.
    clean = plan.singles[SC_CLEAN]
    for i in range(5):
        target = clean[i % len(clean)]
        off = rng.choice((-1, 1)) * rng.randint(200, 5_000)
        credits.append(GenCredit(
            value_date=roll_off_sunday(target.settled_at + timedelta(days=rng.randint(0, 2))),
            narration=_narration_for(rng, ids.utr()),   # well-formed, nonexistent UTR
            ref_no=_ref_for(rng, None),
            amount_paise=target.amount_paise + off,
            disposition=S.EXCEPTION_MISSING_SETTLEMENT, scenario=SC_ORPHAN_CREDIT,
            counterparties=[],
            notes="razorpay-shaped credit whose UTR matches no settlement; "
                  "amount deliberately close to a real settlement",
        ))
    return credits


# One-off/variable spend only. A narration template is either scheduled (see
# _SCHEDULED_OBLIGATIONS) or random noise — NEVER both, or the recurring-
# detection ground truth would be poisoned.
_NOISE_DEBITS = [
    ("UPI-SWIGGY INSTAMART-{n}@ybl", (20_000, 250_000)),
    ("POS 4287XXXXXX INDIAN OIL-{n}", (150_000, 700_000)),
    ("IMPS-P2A-COURIER DTDC-{n}", (50_000, 300_000)),
    ("UPI-MAKEMYTRIP HOLIDAYS-{n}@okhdfc", (80_000, 600_000)),
    ("NEFT DR-VISTAPRINT MARKETING-{n}", (30_000, 250_000)),
    ("IMPS-P2A-FREELANCE DESIGN-{n}", (100_000, 500_000)),
    ("POS 4287XXXXXX BIG BAZAAR-{n}", (40_000, 300_000)),
    ("UPI-BLUEDART EXPRESS-{n}@paytm", (25_000, 200_000)),
]

# --- Scheduled recurring obligations (Stage 2 forecastable structure) ---------
# Emitted by _build_obligations from the ":obligations" RNG stream; every
# posted row is recorded in golden_obligations.csv at mint time. Amount rules
# reference world state deterministically (prev-month gross, payroll base).

PAYROLL_BASE_PAISE = 42_000_000        # month 0; grows +Rs 5,000/month
PAYROLL_GROWTH_PAISE = 500_000
RENT_PAISE = 5_500_000
AWS_BASE_PAISE = 1_800_000             # grows 1.5%/month
LIC_PREMIUM_PAISE = 3_600_000          # quarterly: Apr/Jul/Oct/Jan
TELECOM_PAISE = 249_900


def _payroll_base(month_idx: int) -> int:
    return PAYROLL_BASE_PAISE + month_idx * PAYROLL_GROWTH_PAISE


def _months_in_world(days: int) -> list[tuple[int, int, int]]:
    """(month_idx, year, month) for every month the world touches."""
    out = []
    d = START_DATE.replace(day=1)
    end = START_DATE + timedelta(days=days - 1)
    idx = 0
    while d <= end:
        out.append((idx, d.year, d.month))
        d = (d + timedelta(days=32)).replace(day=1)
        idx += 1
    return out


def _gst_gross_inputs(payments: list[GenPayment]) -> tuple[dict[int, int], int]:
    """Gross captured amount per month index, plus the days-0..29 gross that
    stands in for the missing month before the world (the documented GST
    month-0 fallback). Shared by the obligation schedule and the tax goldens
    so the two can never drift."""
    gross_by_month: dict[int, int] = {}
    first30 = 0
    for p in payments:
        if p.status != "captured":
            continue
        d = p.created_at.date()
        m_idx = (d.year - START_DATE.year) * 12 + d.month - START_DATE.month
        gross_by_month[m_idx] = gross_by_month.get(m_idx, 0) + p.amount_paise
        if (d - START_DATE).days < 30:
            first30 += p.amount_paise
    return gross_by_month, first30


def _build_obligations(seed: int, payments: list[GenPayment], days: int
                       ) -> list[GenDebit]:
    rng = Random(f"{seed}:obligations")
    world_end = START_DATE + timedelta(days=days - 1)
    gross_by_month, first30 = _gst_gross_inputs(payments)

    def n(digits: int = 5) -> str:
        return "".join(rng.choice("0123456789") for _ in range(digits))

    def jitter_pct(amount: int, bp: int) -> int:
        return amount + rng.randint(-amount * bp // 10_000, amount * bp // 10_000)

    debits: list[GenDebit] = []

    def emit(key: str, narration: str, due: date, amount: int,
             date_jitter: bool = False) -> None:
        if due > world_end:
            return
        posted = roll_off_sunday(due)
        if date_jitter and rng.random() < 0.25:
            posted = roll_off_sunday(posted + timedelta(days=1))
        if posted > world_end:
            return   # would post past the last statement day; drop deterministically
        debits.append(GenDebit(value_date=posted, narration=narration, ref_no="",
                               amount_paise=amount, obligation_key=key, due_date=due))

    payroll_actual: dict[int, int] = {}   # month -> posted payroll amount
    for m_idx, year, month in _months_in_world(days):
        # Locals keep the RNG draw order identical to the inline calls:
        # narration digits first, then the amount jitter, then emit's own
        # date-jitter draw.
        payroll_narr = f"SAL-NEFT-STAFF PAYROLL-{n()}"
        payroll_amt = jitter_pct(_payroll_base(m_idx), 80)
        payroll_actual[m_idx] = payroll_amt
        emit("payroll", payroll_narr, date(year, month, 1), payroll_amt,
             date_jitter=True)
        emit("rent", f"NEFT DR-URBAN LADDER RENT-{n()}", date(year, month, 1),
             RENT_PAISE)
        emit("aws", f"NEFT DR-AMAZON WEB SERVICES INDIA PL-INV{n()}",
             date(year, month, 5),
             jitter_pct(AWS_BASE_PAISE * (1000 + 15 * m_idx) // 1000, 500),
             date_jitter=True)
        # TDS deposit = 10% of the PREVIOUS month's actual posted payroll, so
        # a books-blind engine can recompute it from the statement alone.
        # Month 0's basis (the payroll before the world began) is unobservable
        # — its expected disposition is `unverifiable_prior_period`, and the
        # amount here intentionally uses the unjittered base as a stand-in.
        tds_base = (_payroll_base(0) if m_idx == 0
                    else payroll_actual[m_idx - 1])
        emit("tds", f"TDS PAYMENT-CBDT-{n()}", date(year, month, 7),
             TX.tds_deposit(tds_base))
        emit("telecom", f"UPI-BHARTI AIRTEL-{n()}@icici", date(year, month, 12),
             TELECOM_PAISE)
        if month in (4, 7, 10, 1):
            emit("insurance", f"ACH-D-LIC PREMIUM-{n()}", date(year, month, 15),
                 LIC_PREMIUM_PAISE)
        prev_gross = gross_by_month.get(m_idx - 1, first30)
        emit("gst", f"GST PAYMENT-CBIC-{n()}", date(year, month, 20),
             TX.gst_liability(prev_gross))

    return debits

_NOISE_CREDIT_NARRATIONS = [
    "NEFT CR-{bank} BANK-EXPORT INCENTIVE DGFT-{n}",
    "IMPS-P2A CR-KHATRI TRADERS-INV {n}",
    "NEFT CR-FLIPKART WHOLESALE REFUND-{n}",
    "BY CASH DEPOSIT-BRANCH {n}",
    "INT.PD:SB ACCOUNT-{n}",
]


def _build_noise(seed: int, settlements: list[GenSettlement], days: int
                 ) -> tuple[list[GenCredit], list[GenDebit]]:
    rng = Random(f"{seed}:noise")
    nets = sorted(s.amount_paise for s in settlements)

    def far_from_all_nets(amount: int) -> bool:
        import bisect
        i = bisect.bisect_left(nets, amount)
        for j in (i - 1, i):
            if 0 <= j < len(nets) and abs(nets[j] - amount) < MIN_SEPARATION_PAISE:
                return False
        return True

    debits: list[GenDebit] = []
    for day_idx in range(days):
        day = START_DATE + timedelta(days=day_idx)
        if day.weekday() == 6:
            continue
        for _ in range(rng.randint(0, 3)):
            tpl, (lo, hi) = rng.choice(_NOISE_DEBITS)
            debits.append(GenDebit(
                value_date=day,
                narration=tpl.format(n=rng.randint(10_000, 99_999)),
                ref_no="", amount_paise=rng.randint(lo, hi),
            ))

    credits: list[GenCredit] = []
    for _ in range(9 * days // DAYS):
        day = roll_off_sunday(START_DATE + timedelta(days=rng.randint(0, days - 1)))
        tpl = rng.choice(_NOISE_CREDIT_NARRATIONS)
        for _ in range(500):
            amount = rng.randint(20_000, 3_000_000)
            if far_from_all_nets(amount):
                break
        else:
            raise AssertionError("could not place noise credit away from settlement nets")
        credits.append(GenCredit(
            value_date=day,
            narration=tpl.format(bank=rng.choice(["HDFC", "ICICI", "AXIS"]),
                                 n=rng.randint(1_000, 99_999)),
            ref_no="", amount_paise=amount,
            disposition=S.NON_SETTLEMENT_CREDIT, scenario=SC_NOISE_CREDIT,
            counterparties=[], razorpay_shaped=False,
            notes="unrelated inflow; must not be claimed by reconciliation",
        ))
    return credits, debits


# --- Stage 3: tax world (GSTR-2B, Form 26AS, obligation compliance) -----------
# Everything here runs on NEW RNG streams (":tax:*") so the Stage 1/2 record
# streams keep their draw order. Intended world-byte changes are confined to
# the TDS-deposit amounts (months >= 1, now 10% of the actual posted payroll)
# and the two injected GST compliance defects.

_TAX_SC_CLEAN = "tax_clean"
_TAX_SC_FEE = "tax_fee_invoice"
_TAX_SC_MISMATCH = "tax_amount_mismatch"
_TAX_SC_HEAD = "tax_wrong_head"
_TAX_SC_TYPO = "tax_invoice_typo"
_TAX_SC_LATE = "tax_filed_late"
_TAX_SC_DUP = "tax_duplicate_2b"
_TAX_SC_MISSING = "tax_missing_2b"
_TAX_SC_UNKNOWN = "tax_unknown_2b"
_TAX_SC_BLOCKED = "tax_blocked_credit"
_TAX_SC_NO_ITC = "tax_no_itc"
_TAX_SC_26AS_CLEAN = "tax_26as_clean"
_TAX_SC_26AS_MISSING = "tax_26as_missing"
_TAX_SC_26AS_MISMATCH = "tax_26as_amount_mismatch"
_TAX_SC_26AS_QUARTER = "tax_26as_wrong_quarter"
_TAX_SC_26AS_DUP = "tax_26as_duplicate"
_TAX_SC_OBL_CLEAN = "tax_obligation_clean"
_TAX_SC_OBL_SHORT = "tax_obligation_short_paid"
_TAX_SC_OBL_LATE = "tax_obligation_late_paid"
_TAX_SC_OBL_MISSING = "tax_obligation_missing"
_TAX_SC_OBL_UNVERIFIABLE = "tax_obligation_unverifiable"

# Vendors whose spends are scheduled obligations; unknown-invoice defects are
# minted only against one-off vendors so the singleton-per-period structure of
# scheduled vendors (which the tax engine may pair on) is never polluted.
_SCHEDULED_VENDOR_KEYS = frozenset({"aws", "airtel", "landlord", "lic"})


def _month_idx(d: date) -> int:
    return (d.year - START_DATE.year) * 12 + d.month - START_DATE.month


def _hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


def _inject_obligation_defects(seed: int, obligations: list[GenDebit],
                               days: int) -> dict:
    """Mutate the GST payment debits in place: one month short-paid (~4%,
    re-floored to Rs 10) and one month posted 2 days late. Months >= 1 only,
    so the documented month-0 fallback case stays clean. The late month must
    keep Stage 2's recurring-detection gates (every gap within +-4 of the
    median gap, >= 80% of day-of-month anchors within +-3 of the median) —
    checked here at build time, deterministically advancing to the next
    eligible month if a candidate would sit outside the gates."""
    rng = Random(f"{seed}:tax:obligations")
    world_end = START_DATE + timedelta(days=days - 1)
    gst = sorted((d for d in obligations if d.obligation_key == "gst"),
                 key=lambda d: d.due_date)
    eligible = [d for d in gst if _month_idx(d.due_date) >= 1]

    def _one_prefix_ok(dates: list[date]) -> bool:
        # Exact replica of forecast.recurring's monthly gates (median gap in
        # [27, 34], every gap within +-4 of the true median, >=80% of days
        # within +-3 of the median anchor) — a test runs the real detector on
        # the mutated world at every backtest cutoff to pin the two together.
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        med = statistics.median(gaps)
        if not (27 <= med <= 34 and all(abs(g - med) <= 4 for g in gaps)):
            return False
        anchor = int(statistics.median(d.day for d in dates))
        near = sum(1 for d in dates
                   if min(abs(d.day - anchor), 31 - abs(d.day - anchor)) <= 3)
        return near >= 0.8 * len(dates)

    def gates_ok(dates: list[date]) -> bool:
        # Detection runs at rolling cutoffs, so every PREFIX of the series
        # must clear the gates, not just the full world.
        return all(_one_prefix_ok(dates[:k])
                   for k in range(3, len(dates) + 1))

    defects: dict[str, dict | None] = {"gst_short": None, "gst_late": None}
    order = list(eligible)
    rng.shuffle(order)

    # The late defect has the harder constraint (detection gates), so it
    # chooses its month first; the short defect takes any other month.
    late = None
    for cand in order:
        new_date = roll_off_sunday(cand.value_date + timedelta(days=2))
        if new_date > world_end:
            continue
        dates = sorted(new_date if d is cand else d.value_date for d in gst)
        if not gates_ok(dates):
            continue
        cand.value_date = new_date
        late = cand
        defects["gst_late"] = {"period": TX.period_of(cand.due_date)}
        break
    assert late is not None or len(eligible) < 2, \
        "no GST month can be delayed within the detection gates"

    short = next((d for d in order
                  if d is not late and d.amount_paise >= 100_000), None)
    if short is not None:
        new_amount = short.amount_paise * 96 // 100 // 1_000 * 1_000
        defects["gst_short"] = {
            "period": TX.period_of(short.due_date),
            "shortfall_paise": new_amount - short.amount_paise,
        }
        short.amount_paise = new_amount
    return defects


def _mint_gstr2b(seed: int, debits: list[GenDebit],
                 settlements: list[GenSettlement]
                 ) -> tuple[list[dict], list[dict], dict[str, int]]:
    """The vendor-filed side of loop 1. Returns (gstr2b rows, golden rows for
    purchases and 2B lines, scenario counts). The books-side purchase universe
    built here is world truth; `tax/books.py` re-derives the same set blind,
    from the CSVs and the vendor registry alone."""
    rng = Random(f"{seed}:tax:gstr2b")

    purchases: list[dict] = []
    for d in debits:
        v = TR.classify_debit(d.narration)
        if v is None:
            continue
        purchases.append({
            "purchase_id": d.txn_id, "vendor": v,
            "period": TX.period_of(d.value_date), "invoice_date": d.value_date,
            "ref": TX.invoice_ref(d.narration),
            "taxable": TX.taxable_from_inclusive(d.amount_paise),
            "gst": TX.gst_from_inclusive(d.amount_paise),
        })

    monthly: dict[str, dict[str, int]] = {}
    for s in settlements:
        agg = monthly.setdefault(TX.period_of(s.created_at.date()),
                                 {"fees": 0, "tax": 0})
        agg["fees"] += s.fees_paise
        agg["tax"] += s.tax_paise
    for period in sorted(monthly):
        y, m = int(period[:4]), int(period[5:7])
        month_end = ((date(y, m, 28) + timedelta(days=4)).replace(day=1)
                     - timedelta(days=1))
        purchases.append({
            "purchase_id": TX.fee_purchase_id(period), "vendor": TR.RAZORPAY,
            "period": period, "invoice_date": month_end, "ref": None,
            "taxable": monthly[period]["fees"], "gst": monthly[period]["tax"],
        })

    # Refs per vendor, for the collision audits. Natural (vendor, ref)
    # collisions are allowed in the world — the engine must corroborate by
    # amount — but colliding purchases never receive defect fates, and equal
    # (vendor, ref, gst) triples would be genuinely undecidable, so they fail
    # the build loudly instead of silently corrupting the ground truth.
    vendor_refs: dict[str, set[str]] = {}
    by_vendor_ref: dict[tuple[str, str], list[dict]] = {}
    for p in purchases:
        if p["ref"] is None:
            continue
        vendor_refs.setdefault(p["vendor"].key, set()).add(p["ref"])
        by_vendor_ref.setdefault((p["vendor"].key, p["ref"]), []).append(p)
    colliding_pids: set[str] = set()
    for group in by_vendor_ref.values():
        if len(group) > 1:
            amounts = [g["gst"] for g in group]
            assert len(set(amounts)) == len(amounts), \
                "identical (vendor, ref, gst) purchases are undecidable"
            colliding_pids.update(g["purchase_id"] for g in group)

    claimable = [p for p in purchases
                 if p["vendor"].files_2b and p["vendor"].itc_eligible
                 and p["ref"] is not None]
    last_period = max(p["period"] for p in purchases)

    def quota(pct: int, floor: int = 2) -> int:
        return max(floor, len(claimable) * pct // 100)

    buckets: list[list] = [   # [scenario, remaining, eligibility]
        [_TAX_SC_MISMATCH, quota(4), None],
        [_TAX_SC_MISSING, quota(4), None],
        [_TAX_SC_LATE, quota(3), lambda p: p["period"] < last_period],
        [_TAX_SC_DUP, quota(2), None],
        [_TAX_SC_HEAD, quota(2), None],
        [_TAX_SC_TYPO, quota(3), None],
    ]
    pool = [p for p in claimable if p["purchase_id"] not in colliding_pids]
    rng.shuffle(pool)
    fate: dict[str, str] = {}
    for p in pool:
        for bucket in buckets:
            if bucket[1] > 0 and (bucket[2] is None or bucket[2](p)):
                fate[p["purchase_id"]] = bucket[0]
                bucket[1] -= 1
                break

    def typo_ref(vendor_key: str, ref: str) -> str:
        """One-digit corruption that stays >= 2 away from every OTHER ref of
        the vendor, so fuzzy matching can never point at two purchases."""
        others = vendor_refs[vendor_key] - {ref}
        start_pos, start_digit = rng.randrange(len(ref)), rng.randrange(10)
        for dp in range(len(ref)):
            pos = (start_pos + dp) % len(ref)
            for dd in range(10):
                new = str((start_digit + dd) % 10)
                if new == ref[pos]:
                    continue
                cand = ref[:pos] + new + ref[pos + 1:]
                if all(_hamming(cand, o) >= 2 for o in others):
                    vendor_refs[vendor_key].add(cand)
                    return cand
        raise AssertionError(f"no safe typo mutation for {vendor_key}/{ref}")

    lines: list[dict] = []

    def add_line(vendor: TR.Vendor, period: str, invoice_no: str,
                 invoice_date: date, taxable: int, gst: int, head: str,
                 pid: str | None, sc: str, disc: int = 0,
                 disposition: str = "") -> dict:
        line = {
            "period": period, "gstin": vendor.gstin,
            "vendor_name": vendor.display_name, "invoice_no": invoice_no,
            "invoice_date": invoice_date.isoformat(),
            "taxable_value_paise": taxable, "gst_paise": gst, "tax_head": head,
            "_pid": pid, "_sc": sc, "_disc": disc, "_disp": disposition,
            "_idx": len(lines),
        }
        lines.append(line)
        return line

    purchase_golden: dict[str, dict] = {}   # purchase_id -> partial golden row

    def set_purchase(p: dict, disposition: str, sc: str, disc: int = 0,
                     notes: str = "") -> None:
        purchase_golden[p["purchase_id"]] = {
            "disposition": disposition, "scenario": sc, "disc": disc,
            "notes": notes, "lines": [],
        }

    for p in purchases:
        v = p["vendor"]
        head = TX.head_for(v.state) if v.files_2b else ""
        if not v.files_2b:
            set_purchase(p, TS.NO_ITC_APPLICABLE, _TAX_SC_NO_ITC,
                         notes=v.no_itc_reason)
            continue
        if v.key == "razorpay":
            inv_no = f"RZP/{p['period'][:4]}{p['period'][5:]}"
            set_purchase(p, TS.ITC_MATCHED, _TAX_SC_FEE,
                         notes="monthly consolidated PSP fee invoice")
            add_line(v, p["period"], inv_no, p["invoice_date"], p["taxable"],
                     p["gst"], head, p["purchase_id"], _TAX_SC_FEE,
                     disposition=TS.ITC_MATCHED)
            continue
        inv_no = f"{v.inv_prefix}/{p['ref']}"
        if not v.itc_eligible:   # Sec 17(5) blocked: filed, must not be claimed
            set_purchase(p, TS.BLOCKED_CREDIT_NO_ITC, _TAX_SC_BLOCKED,
                         notes=v.no_itc_reason)
            add_line(v, p["period"], inv_no, p["invoice_date"], p["taxable"],
                     p["gst"], head, p["purchase_id"], _TAX_SC_BLOCKED,
                     disposition=TS.BLOCKED_CREDIT_NO_ITC)
            continue
        sc = fate.get(p["purchase_id"], _TAX_SC_CLEAN)
        if sc == _TAX_SC_MISSING:
            set_purchase(p, TS.ITC_MISSING_IN_2B, sc, disc=-p["gst"],
                         notes="vendor never filed; input credit at risk")
        elif sc == _TAX_SC_MISMATCH:
            delta = rng.choice((-1, 1)) * rng.randint(3, 40) * 100
            if p["gst"] + delta <= 0:
                delta = abs(delta)
            set_purchase(p, TS.ITC_AMOUNT_MISMATCH, sc, disc=delta)
            add_line(v, p["period"], inv_no, p["invoice_date"], p["taxable"],
                     p["gst"] + delta, head, p["purchase_id"], sc, disc=delta,
                     disposition=TS.ITC_AMOUNT_MISMATCH)
        elif sc == _TAX_SC_HEAD:
            wrong = (TX.HEAD_IGST if head == TX.HEAD_CGST_SGST
                     else TX.HEAD_CGST_SGST)
            set_purchase(p, TS.ITC_HEAD_MISMATCH, sc)
            add_line(v, p["period"], inv_no, p["invoice_date"], p["taxable"],
                     p["gst"], wrong, p["purchase_id"], sc,
                     disposition=TS.ITC_HEAD_MISMATCH)
        elif sc == _TAX_SC_TYPO:
            mutated = f"{v.inv_prefix}/{typo_ref(v.key, p['ref'])}"
            set_purchase(p, TS.ITC_MATCHED, sc)
            add_line(v, p["period"], mutated, p["invoice_date"], p["taxable"],
                     p["gst"], head, p["purchase_id"], sc,
                     disposition=TS.ITC_MATCHED)
        elif sc == _TAX_SC_LATE:
            set_purchase(p, TS.ITC_DEFERRED_NEXT_PERIOD, sc,
                         notes="vendor filed in the following period")
            add_line(v, TX.next_period(p["period"]), inv_no, p["invoice_date"],
                     p["taxable"], p["gst"], head, p["purchase_id"], sc,
                     disposition=TS.ITC_DEFERRED_NEXT_PERIOD)
        elif sc == _TAX_SC_DUP:
            set_purchase(p, TS.ITC_MATCHED, sc)
            add_line(v, p["period"], inv_no, p["invoice_date"], p["taxable"],
                     p["gst"], head, p["purchase_id"], sc,
                     disposition=TS.ITC_MATCHED)
            add_line(v, p["period"], inv_no, p["invoice_date"], p["taxable"],
                     p["gst"], head, p["purchase_id"], sc,
                     disposition=TS.DUPLICATE_2B_LINE)
        else:
            set_purchase(p, TS.ITC_MATCHED, _TAX_SC_CLEAN)
            add_line(v, p["period"], inv_no, p["invoice_date"], p["taxable"],
                     p["gst"], head, p["purchase_id"], _TAX_SC_CLEAN,
                     disposition=TS.ITC_MATCHED)

    # Unknown invoices: filed lines with no purchase behind them. One-off
    # vendors only; refs >= 2 away from every real ref of the vendor; amounts
    # >= MIN_SEPARATION from every same-vendor-period purchase, so no honest
    # pass can be tricked into pairing them.
    oneoff = [v for v in TR.DEBIT_VENDORS
              if v.files_2b and v.itc_eligible
              and v.key not in _SCHEDULED_VENDOR_KEYS]
    periods = sorted({p["period"] for p in purchases})
    for _ in range(max(3, len(claimable) * 3 // 100)):
        v = oneoff[rng.randrange(len(oneoff))]
        period = periods[rng.randrange(len(periods))]
        for _attempt in range(500):
            ref = "".join(rng.choice("0123456789") for _ in range(5))
            if all(_hamming(ref, o) >= 2
                   for o in vendor_refs.get(v.key, set())):
                break
        else:
            raise AssertionError("could not mint a non-colliding unknown ref")
        vendor_refs.setdefault(v.key, set()).add(ref)
        near = [p["gst"] for p in purchases
                if p["vendor"].key == v.key and p["period"] == period]
        near += [ln["gst_paise"] for ln in lines
                 if ln["_sc"] == _TAX_SC_UNKNOWN and ln["gstin"] == v.gstin
                 and ln["period"] == period]
        for _attempt in range(500):
            total = rng.randint(30_000, 600_000)
            gst = TX.gst_from_inclusive(total)
            if all(abs(gst - x) >= MIN_SEPARATION_PAISE for x in near):
                break
        else:
            raise AssertionError("could not separate an unknown-line amount")
        y, m = int(period[:4]), int(period[5:7])
        add_line(v, period, f"{v.inv_prefix}/{ref}",
                 date(y, m, rng.randint(1, 28)),
                 TX.taxable_from_inclusive(total), gst, TX.head_for(v.state),
                 None, _TAX_SC_UNKNOWN, disposition=TS.UNKNOWN_INVOICE_IN_2B)

    # Assign line ids in a canonical order; wire matched line ids back onto
    # the purchase golden rows (duplicates link the purchase to the FIRST
    # line only, mirroring the recon duplicate-credit convention).
    lines.sort(key=lambda ln: (ln["period"], ln["gstin"], ln["invoice_no"],
                               ln["_idx"]))
    for i, ln in enumerate(lines, start=1):
        ln["line_id"] = f"2B{i:06d}"
        if ln["_pid"] is not None and ln["_disp"] != TS.DUPLICATE_2B_LINE:
            purchase_golden[ln["_pid"]]["lines"].append(ln["line_id"])

    golden: list[dict] = []
    counts: dict[str, int] = {}

    def count(sc: str) -> None:
        counts[sc] = counts.get(sc, 0) + 1

    for p in purchases:
        g = purchase_golden[p["purchase_id"]]
        golden.append({
            "record_type": TS.RT_PURCHASE, "record_id": p["purchase_id"],
            "expected_disposition": g["disposition"],
            "counterparty_ids": S.COUNTERPARTY_SEP.join(g["lines"]),
            "scenario_tag": g["scenario"],
            "expected_discrepancy_paise": g["disc"], "notes": g["notes"],
        })
        count(g["scenario"])
    for ln in lines:
        golden.append({
            "record_type": TS.RT_2B_LINE, "record_id": ln["line_id"],
            "expected_disposition": ln["_disp"],
            "counterparty_ids": ln["_pid"] or "",
            "scenario_tag": ln["_sc"],
            "expected_discrepancy_paise": ln["_disc"], "notes": "",
        })
        if ln["_pid"] is None:      # unknown lines have no purchase to count
            count(ln["_sc"])

    rows = [{k: ln[k] for k in TS.GSTR2B_COLUMNS} for ln in lines]
    return rows, golden, counts


def _mint_form26as(seed: int, settlements: list[GenSettlement], days: int
                   ) -> tuple[list[dict], list[dict], dict[str, int]]:
    """The deductor-filed side of loop 2, sourced from the settlements whose
    discrepancy scenario chose the 1%-TDS branch."""
    rng = Random(f"{seed}:tax:26as")
    events: list[dict] = []
    for s in settlements:
        if s.tds_deducted_paise <= 0 or not s.counterparties:
            continue
        credit = s.counterparties[0]
        events.append({
            "settlement_id": s.settlement_id, "date": credit.value_date,
            "amount_paid": s.amount_paise, "tds": s.tds_deducted_paise,
        })
    events.sort(key=lambda e: (e["date"], e["settlement_id"]))

    world_end = START_DATE + timedelta(days=days - 1)
    multi_quarter = TX.fy_quarter(START_DATE) != TX.fy_quarter(world_end)
    order = list(range(len(events)))
    rng.shuffle(order)
    defect_of: dict[int, str] = {}
    slots = iter(order)
    # Thresholds keep a clean majority on small worlds: a lone event is never
    # defected, and each further defect kind needs one more event in hand.
    for sc, cond in ((_TAX_SC_26AS_MISSING, len(events) >= 2),
                     (_TAX_SC_26AS_MISMATCH, len(events) >= 3),
                     (_TAX_SC_26AS_QUARTER, multi_quarter and len(events) >= 4),
                     (_TAX_SC_26AS_DUP, len(events) >= 5)):
        if not cond:
            continue
        try:
            defect_of[next(slots)] = sc
        except StopIteration:
            break

    entries: list[dict] = []
    golden: list[dict] = []
    counts: dict[str, int] = {}

    def add_entry(e: dict, tds: int, quarter: str, disposition: str,
                  sc: str, disc: int = 0) -> dict:
        entry = {
            "tan": TR.DEDUCTOR_TAN, "deductor_name": TR.DEDUCTOR_NAME,
            "section": TR.TDS_SECTION, "fy_quarter": quarter,
            "credit_date": e["date"].isoformat(),
            "amount_paid_paise": e["amount_paid"], "tds_paise": tds,
            "_sid": e["settlement_id"], "_disp": disposition, "_sc": sc,
            "_disc": disc, "_idx": len(entries),
        }
        entries.append(entry)
        return entry

    def event_row(e: dict, disposition: str, sc: str, entry_ids: list[str],
                  disc: int = 0, notes: str = "") -> dict:
        return {
            "record_type": TS.RT_TDS_EVENT, "record_id": e["settlement_id"],
            "expected_disposition": disposition,
            "counterparty_ids": S.COUNTERPARTY_SEP.join(entry_ids),
            "scenario_tag": sc, "expected_discrepancy_paise": disc,
            "notes": notes, "_entries": entry_ids,
        }

    event_goldens: list[tuple[dict, list[dict]]] = []  # (event row, its entries)
    for i, e in enumerate(events):
        sc = defect_of.get(i, _TAX_SC_26AS_CLEAN)
        counts[sc] = counts.get(sc, 0) + 1
        quarter = TX.fy_quarter(e["date"])
        if sc == _TAX_SC_26AS_MISSING:
            event_goldens.append((event_row(
                e, TS.TDS_MISSING_IN_26AS, sc, [], disc=-e["tds"],
                notes="deduction observed at credit but never filed"), []))
        elif sc == _TAX_SC_26AS_MISMATCH:
            delta = rng.choice((-1, 1)) * rng.randint(1, 9) * 100
            if e["tds"] + delta <= 0:
                delta = abs(delta)
            entry = add_entry(e, e["tds"] + delta, quarter,
                              TS.TDS_AMOUNT_MISMATCH, sc, disc=delta)
            event_goldens.append((event_row(
                e, TS.TDS_AMOUNT_MISMATCH, sc, [], disc=delta), [entry]))
        elif sc == _TAX_SC_26AS_QUARTER:
            wrong_q = TX.fy_quarter(e["date"] + timedelta(days=92))
            assert wrong_q != quarter
            entry = add_entry(e, e["tds"], wrong_q, TS.TDS_WRONG_QUARTER, sc)
            event_goldens.append((event_row(
                e, TS.TDS_WRONG_QUARTER, sc, []), [entry]))
        elif sc == _TAX_SC_26AS_DUP:
            first = add_entry(e, e["tds"], quarter, TS.TDS_CREDIT_MATCHED, sc)
            add_entry(e, e["tds"], quarter, TS.TDS_DUPLICATE_26AS, sc)
            event_goldens.append((event_row(
                e, TS.TDS_CREDIT_MATCHED, sc, []), [first]))
        else:
            entry = add_entry(e, e["tds"], quarter, TS.TDS_CREDIT_MATCHED, sc)
            event_goldens.append((event_row(
                e, TS.TDS_CREDIT_MATCHED, sc, []), [entry]))

    entries.sort(key=lambda en: (en["credit_date"], en["_idx"]))
    for i, en in enumerate(entries, start=1):
        en["entry_id"] = f"26AS{i:03d}"
    for row, matched_entries in event_goldens:
        ids = [en["entry_id"] for en in matched_entries]
        row["counterparty_ids"] = S.COUNTERPARTY_SEP.join(ids)
        row.pop("_entries", None)
        golden.append(row)
    for en in entries:
        golden.append({
            "record_type": TS.RT_26AS_ENTRY, "record_id": en["entry_id"],
            "expected_disposition": en["_disp"],
            "counterparty_ids": en["_sid"], "scenario_tag": en["_sc"],
            "expected_discrepancy_paise": en["_disc"], "notes": "",
        })

    rows = [{k: en[k] for k in TS.FORM26AS_COLUMNS} for en in entries]
    return rows, golden, counts


def _golden_tax_periods(payments: list[GenPayment], debits: list[GenDebit],
                        obligations: list[GenDebit], days: int
                        ) -> list[dict]:
    """Loop-3 ground truth: for every gradeable month, what the GST and TDS
    compliance verdicts should be, derived from the same published rules the
    engine recomputes with. No RNG — pure rule application. A period is
    gradeable only when its statutory deadline lies within the observed
    debit horizon (same rule the engine applies), so straggler credits past
    world end never create phantom not-yet-due periods."""
    gross_by_month, first30 = _gst_gross_inputs(payments)
    horizon = max(d.value_date for d in debits)
    by_key_month: dict[tuple[str, int], GenDebit] = {}
    for d in obligations:
        by_key_month[(d.obligation_key, _month_idx(d.due_date))] = d

    def row(kind: str, period: str, disposition: str, sc: str,
            txn_ids: list[str], disc: int, notes: str = "") -> dict:
        return {
            "record_type": TS.RT_OBLIGATION_PERIOD,
            "record_id": f"{kind}:{period}",
            "expected_disposition": disposition,
            "counterparty_ids": S.COUNTERPARTY_SEP.join(txn_ids),
            "scenario_tag": sc, "expected_discrepancy_paise": disc,
            "notes": notes,
        }

    golden: list[dict] = []
    for m_idx, year, month in _months_in_world(days):
        period = f"{year:04d}-{month:02d}"
        gst_due = TX.statutory_deadline(date(year, month, TX.GST_DUE_DAY))
        tds_due = TX.statutory_deadline(date(year, month, TX.TDS_DUE_DAY))

        liability = TX.gst_liability(first30 if m_idx == 0
                                     else gross_by_month.get(m_idx - 1, 0))
        d = by_key_month.get(("gst", m_idx))
        if gst_due > horizon:
            pass                          # not yet due inside the window
        elif d is None:
            golden.append(row("gst", period, TS.NOT_PAID, _TAX_SC_OBL_MISSING,
                              [], -liability))
        elif d.amount_paise != liability:
            golden.append(row("gst", period, TS.PAID_SHORT, _TAX_SC_OBL_SHORT,
                              [d.txn_id], d.amount_paise - liability))
        elif d.value_date > TX.statutory_deadline(d.due_date):
            golden.append(row("gst", period, TS.PAID_LATE, _TAX_SC_OBL_LATE,
                              [d.txn_id], 0,
                              notes=f"posted {d.value_date.isoformat()}, due "
                                    f"{d.due_date.isoformat()}"))
        else:
            golden.append(row("gst", period, TS.PAID_ON_TIME,
                              _TAX_SC_OBL_CLEAN, [d.txn_id], 0))

        d = by_key_month.get(("tds", m_idx))
        payroll_prev = by_key_month.get(("payroll", m_idx - 1))
        if tds_due > horizon:
            continue                      # not yet due inside the window
        if m_idx == 0 or payroll_prev is None:
            golden.append(row("tds", period, TS.UNVERIFIABLE_PRIOR_PERIOD,
                              _TAX_SC_OBL_UNVERIFIABLE,
                              [d.txn_id] if d else [], 0,
                              notes="basis month precedes the statement"))
            continue
        expected = TX.tds_deposit(payroll_prev.amount_paise)
        if d is None:
            golden.append(row("tds", period, TS.NOT_PAID, _TAX_SC_OBL_MISSING,
                              [], -expected))
        elif d.amount_paise != expected:
            golden.append(row("tds", period, TS.PAID_SHORT, _TAX_SC_OBL_SHORT,
                              [d.txn_id], d.amount_paise - expected))
        elif d.value_date > TX.statutory_deadline(d.due_date):
            golden.append(row("tds", period, TS.PAID_LATE, _TAX_SC_OBL_LATE,
                              [d.txn_id], 0))
        else:
            golden.append(row("tds", period, TS.PAID_ON_TIME,
                              _TAX_SC_OBL_CLEAN, [d.txn_id], 0))
    return golden


# --- Assembly, goldens, writers -----------------------------------------------

def _assemble_statement(credits: list[GenCredit], debits: list[GenDebit]) -> list[dict]:
    rows: list[tuple[date, int, GenCredit | GenDebit]] = []
    for c in credits:
        rows.append((c.value_date, 0, c))
    for d in debits:
        rows.append((d.value_date, 1, d))
    rows.sort(key=lambda r: (r[0], r[1], r[2].amount_paise, r[2].narration))

    out: list[dict] = []
    balance = OPENING_BALANCE_PAISE
    for i, (vdate, _, rec) in enumerate(rows, start=1):
        rec.txn_id = f"BANK{i:06d}"
        credit_amt = rec.amount_paise if isinstance(rec, GenCredit) else 0
        debit_amt = rec.amount_paise if isinstance(rec, GenDebit) else 0
        balance += credit_amt - debit_amt
        out.append({
            "txn_id": rec.txn_id,
            "txn_date": bank_date_str(vdate),
            "value_date": bank_date_str(vdate),
            "narration": rec.narration,
            "ref_no": rec.ref_no,
            "debit_amount": rupee_str_from_paise(debit_amt, indian_grouping=True) if debit_amt else "",
            "credit_amount": rupee_str_from_paise(credit_amt, indian_grouping=True) if credit_amt else "",
            "balance": rupee_str_from_paise(balance, indian_grouping=True),
        })
    return out


def build_world(seed: int, days: int = DAYS) -> dict:
    ids = Ids(seed)
    orders, payments = _build_orders_and_payments(seed, ids, days)
    settlements = _build_settlements(seed, ids, payments)
    plan = _assign_scenarios(seed, settlements)
    _separation_audit(settlements, plan, orders, payments)
    credits = _build_bank_credits(seed, ids, plan, settlements)
    noise_credits, debits = _build_noise(seed, settlements, days)
    credits.extend(noise_credits)
    obligations = _build_obligations(seed, payments, days)
    tax_defects = _inject_obligation_defects(seed, obligations, days)
    debits.extend(obligations)
    statement = _assemble_statement(credits, debits)

    # Tax minting runs after assembly: purchase ids are bank txn_ids.
    gstr2b_rows, golden_tax_itc, tax_counts = _mint_gstr2b(
        seed, debits, settlements)
    form26as_rows, golden_tax_26as, counts_26as = _mint_form26as(
        seed, settlements, days)
    golden_periods = _golden_tax_periods(payments, debits, obligations, days)
    golden_tax = golden_tax_itc + golden_tax_26as + golden_periods
    for sc, n_sc in counts_26as.items():
        tax_counts[sc] = tax_counts.get(sc, 0) + n_sc
    for g in golden_periods:
        tax_counts[g["scenario_tag"]] = tax_counts.get(g["scenario_tag"], 0) + 1

    golden_obligations = [{
        "obligation_key": d.obligation_key,
        "due_date": d.due_date.isoformat(),
        "posted_date": d.value_date.isoformat(),
        "amount_paise": d.amount_paise,
        "txn_id": d.txn_id,
        "narration": d.narration,
    } for d in obligations]

    golden_a: list[dict] = []
    for s in settlements:
        golden_a.append({
            "record_type": S.RT_SETTLEMENT,
            "record_id": s.settlement_id,
            "expected_disposition": s.disposition,
            "counterparty_ids": S.COUNTERPARTY_SEP.join(c.txn_id for c in s.counterparties),
            "scenario_tag": s.scenario,
            "expected_discrepancy_paise": s.expected_discrepancy_paise,
            "notes": s.notes,
        })
    for c in credits:
        golden_a.append({
            "record_type": S.RT_BANK_CREDIT,
            "record_id": c.txn_id,
            "expected_disposition": c.disposition,
            "counterparty_ids": S.COUNTERPARTY_SEP.join(x.settlement_id for x in c.counterparties),
            "scenario_tag": c.scenario,
            "expected_discrepancy_paise": c.expected_discrepancy_paise,
            "notes": c.notes,
        })

    golden_b: list[dict] = []
    for o in orders:
        golden_b.append({
            "record_type": S.RT_ORDER,
            "record_id": o.order_id,
            "expected_disposition": o.legb_disposition,
            "counterparty_ids": S.COUNTERPARTY_SEP.join(o.legb_counterparties),
            "scenario_tag": o.legb_scenario,
            "expected_discrepancy_paise": 0,
            "notes": o.legb_notes,
        })
    for p in payments:
        golden_b.append({
            "record_type": S.RT_PAYMENT,
            "record_id": p.payment_id,
            "expected_disposition": p.legb_disposition,
            "counterparty_ids": S.COUNTERPARTY_SEP.join(p.legb_counterparties),
            "scenario_tag": p.legb_scenario,
            "expected_discrepancy_paise": 0,
            "notes": p.legb_notes,
        })

    scenario_counts: dict[str, int] = {}
    for s in settlements:
        scenario_counts[s.scenario] = scenario_counts.get(s.scenario, 0) + 1
    credit_scenarios: dict[str, int] = {}
    for c in credits:
        credit_scenarios[c.scenario] = credit_scenarios.get(c.scenario, 0) + 1

    manifest = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "period": {"start": START_DATE.isoformat(), "days": days},
        "counts": {
            "orders": len(orders),
            "payments": len(payments),
            "settlements": len(settlements),
            "bank_rows": len(statement),
            "bank_credits": len(credits),
            "bank_debits": len(debits),
            "scheduled_obligations": len(obligations),
            "golden_leg_a_rows": len(golden_a),
            "golden_leg_b_rows": len(golden_b),
            "gstr2b_lines": len(gstr2b_rows),
            "form26as_entries": len(form26as_rows),
            "golden_tax_rows": len(golden_tax),
        },
        "settlement_scenarios": dict(sorted(scenario_counts.items())),
        "bank_credit_scenarios": dict(sorted(credit_scenarios.items())),
        "forecast_structure": {
            "volume_model": "weekday x trend x month-end multipliers (x1000 ints), +-2 count noise",
            "weekday_x1000": _WEEKDAY_X1000,
            "trend": "1000 + 5*day_idx//3 (x1000)",
            "month_end_x1000": _MONTH_END_X1000,
            "scheduled_obligation_keys": sorted({d.obligation_key for d in obligations}),
            "gst_month0_fallback": "no in-world previous month; uses gross of days 0-29",
        },
        "conventions": {
            "amounts": "integer paise in PSP/order files; rupee decimal strings in bank statement",
            "gst_on_fee": "18%, per-payment integer round-half-up, summed per batch",
            "min_amount_separation_paise": MIN_SEPARATION_PAISE,
        },
        "tax_scenarios": dict(sorted(tax_counts.items())),
        "tax_defects": tax_defects,
        "tax_conventions": {
            "merchant_gstin": TX.MERCHANT_GSTIN,
            "inclusive_split": "taxable = (total*100+59)//118; gst = total - taxable (pair rule)",
            "fee_invoice": "one consolidated 2B line per month; taxable/gst = settlement fee/tax sums by created_at month",
            "invoice_ref": "last run of >=5 consecutive digits in the narration",
            "gst_liability": "3% of prev-month gross captured, floored to Rs 10; month 0 uses gross of days 0-29",
            "tds_deposit": "10% of prev-month ACTUAL posted payroll, floored to Rs 1; month 0 unverifiable in-world",
        },
    }

    return {
        "orders": orders, "payments": payments, "settlements": settlements,
        "statement": statement, "golden_a": golden_a, "golden_b": golden_b,
        "golden_obligations": golden_obligations, "gstr2b": gstr2b_rows,
        "form26as": form26as_rows, "golden_tax": golden_tax,
        "manifest": manifest,
    }


def write_world(world: dict, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    def write_csv(name: str, columns: list[str], rows: list[dict]) -> None:
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(rows)

    write_csv("payments.csv", S.PAYMENTS_COLUMNS, [{
        "payment_id": p.payment_id, "order_id": p.order_id, "method": p.method,
        "amount_paise": p.amount_paise, "fee_paise": p.fee_paise,
        "tax_paise": p.tax_paise, "status": p.status,
        "created_at": p.created_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "settlement_id": p.settlement_id,
    } for p in world["payments"]])

    write_csv("settlements.csv", S.SETTLEMENTS_COLUMNS, [{
        "settlement_id": s.settlement_id, "amount_paise": s.amount_paise,
        "fees_paise": s.fees_paise, "tax_paise": s.tax_paise, "utr": s.utr,
        "payment_count": len(s.members), "status": "processed",
        "created_at": s.created_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "settled_at": s.settled_at.isoformat(),
    } for s in world["settlements"]])

    write_csv("bank_statement.csv", S.BANK_COLUMNS, world["statement"])

    write_csv("order_book.csv", S.ORDER_BOOK_COLUMNS, [{
        "order_id": o.order_id, "receipt": o.receipt,
        "amount_paise": o.amount_paise, "status": o.status,
        "created_at": o.created_at.strftime("%Y-%m-%dT%H:%M:%S"),
    } for o in world["orders"] if o.order_id])

    write_csv("gstr2b.csv", TS.GSTR2B_COLUMNS, world["gstr2b"])
    write_csv("form26as.csv", TS.FORM26AS_COLUMNS, world["form26as"])

    write_csv("golden_leg_a.csv", S.GOLDEN_COLUMNS, world["golden_a"])
    write_csv("golden_leg_b.csv", S.GOLDEN_COLUMNS, world["golden_b"])
    write_csv("golden_tax.csv", S.GOLDEN_COLUMNS, world["golden_tax"])
    write_csv("golden_obligations.csv",
              ["obligation_key", "due_date", "posted_date", "amount_paise",
               "txn_id", "narration"],
              world["golden_obligations"])

    with open(os.path.join(out_dir, "golden_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(world["manifest"], f, indent=2)


def seed_dir(seed: int, days: int = DAYS) -> str:
    """Canonical on-disk location for a generated world. 90-day worlds keep
    the original layout; other lengths get a d<days> suffix so recon and
    forecast worlds coexist."""
    name = str(seed) if days == DAYS else f"{seed}d{days}"
    return os.path.join("data", "seeds", name)


def generate(seed: int, out_dir: str | None = None, days: int = DAYS) -> str:
    out = out_dir or seed_dir(seed, days)
    write_world(build_world(seed, days), out)
    return out


def _dir_digest(path: str) -> str:
    h = hashlib.sha256()
    for name in sorted(os.listdir(path)):
        with open(os.path.join(path, name), "rb") as f:
            h.update(name.encode())
            h.update(f.read())
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--out", default=None)
    ap.add_argument("--verify-determinism", action="store_true",
                    help="generate twice into temp dirs and compare hashes")
    args = ap.parse_args()

    if args.verify_determinism:
        import tempfile
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            write_world(build_world(args.seed, args.days), t1)
            write_world(build_world(args.seed, args.days), t2)
            d1, d2 = _dir_digest(t1), _dir_digest(t2)
            print(f"run 1 digest: {d1}")
            print(f"run 2 digest: {d2}")
            if d1 != d2:
                raise SystemExit("NOT deterministic")
            print("deterministic: OK")
        return

    out = generate(args.seed, args.out, args.days)
    with open(os.path.join(out, "golden_manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    print(json.dumps(manifest, indent=2))
    print(f"world written to {out}")


if __name__ == "__main__":
    main()
