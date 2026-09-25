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

Scheduled debits: besides random spend, the statement carries recurring
business debits (payroll, rent, AWS, telecom, insurance, GST and TDS
payments, two of the GST payments perturbed). They are out of scope for
leg A (debits are never reconciled) and exist so the statement reads like
a real current account. They are kept draw-for-draw identical to the
generator the benchmark numbers were produced with, so committed worlds
keep their bytes. (The forecast and tax loops that once graded them were
cut from this repo; they live on the `full-scope` branch.)

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
        # kinds.
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
# _build_obligations) or random noise, never both.
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

# --- Scheduled recurring debits ------------------------------------------------
# Emitted by _build_obligations from the ":obligations" RNG stream. Amount
# rules reference world state deterministically (prev-month gross, payroll
# base). The three helpers below are verbatim copies of the arithmetic the
# removed tax package used, so every debit keeps its exact amount.

_GST_LIABILITY_RATE_PCT = 3            # of prev-month gross captured
_TDS_DEPOSIT_RATE_PCT = 10             # of prev-month posted payroll


def _gst_liability(prev_month_gross_paise: int) -> int:
    """3% of the previous month's gross captured payments, floored to Rs 10."""
    return (prev_month_gross_paise * _GST_LIABILITY_RATE_PCT // 100) // 1_000 * 1_000


def _tds_deposit(prev_month_payroll_paise: int) -> int:
    """10% of the previous month's actual posted payroll, floored to Rs 1."""
    return (prev_month_payroll_paise // _TDS_DEPOSIT_RATE_PCT) // 100 * 100


def _period_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"

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
    month-0 fallback)."""
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
             _tds_deposit(tds_base))
        emit("telecom", f"UPI-BHARTI AIRTEL-{n()}@icici", date(year, month, 12),
             TELECOM_PAISE)
        if month in (4, 7, 10, 1):
            emit("insurance", f"ACH-D-LIC PREMIUM-{n()}", date(year, month, 15),
                 LIC_PREMIUM_PAISE)
        prev_gross = gross_by_month.get(m_idx - 1, first30)
        emit("gst", f"GST PAYMENT-CBIC-{n()}", date(year, month, 20),
             _gst_liability(prev_gross))

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


# --- Perturbed GST payments -----------------------------------------------------
# Two GST payment debits are perturbed on their own RNG stream (one short-paid,
# one posted two days late). Kept only so the statement stays byte-identical.

def _month_idx(d: date) -> int:
    return (d.year - START_DATE.year) * 12 + d.month - START_DATE.month


def _inject_obligation_defects(seed: int, obligations: list[GenDebit],
                               days: int) -> dict:
    """Mutate the GST payment debits in place: one month short-paid (~4%,
    re-floored to Rs 10) and one month posted 2 days late. Months >= 1 only,
    so the documented month-0 fallback case stays clean. The late month is
    chosen so the GST series keeps a regular monthly rhythm (gates below),
    deterministically advancing to the next eligible month otherwise. The
    selection logic is kept exactly as it was so worlds keep their bytes."""
    rng = Random(f"{seed}:tax:obligations")
    world_end = START_DATE + timedelta(days=days - 1)
    gst = sorted((d for d in obligations if d.obligation_key == "gst"),
                 key=lambda d: d.due_date)
    eligible = [d for d in gst if _month_idx(d.due_date) >= 1]

    def _one_prefix_ok(dates: list[date]) -> bool:
        # Monthly-rhythm gates: median gap in [27, 34], every gap within +-4
        # of the median, >=80% of days within +-3 of the median anchor.
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        med = statistics.median(gaps)
        if not (27 <= med <= 34 and all(abs(g - med) <= 4 for g in gaps)):
            return False
        anchor = int(statistics.median(d.day for d in dates))
        near = sum(1 for d in dates
                   if min(abs(d.day - anchor), 31 - abs(d.day - anchor)) <= 3)
        return near >= 0.8 * len(dates)

    def gates_ok(dates: list[date]) -> bool:
        # Every PREFIX of the series must clear the gates, not just the
        # full world.
        return all(_one_prefix_ok(dates[:k])
                   for k in range(3, len(dates) + 1))

    defects: dict[str, dict | None] = {"gst_short": None, "gst_late": None}
    order = list(eligible)
    rng.shuffle(order)

    # The late defect has the harder constraint (the gates), so it
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
        defects["gst_late"] = {"period": _period_of(cand.due_date)}
        break
    assert late is not None or len(eligible) < 2, \
        "no GST month can be delayed within the detection gates"

    short = next((d for d in order
                  if d is not late and d.amount_paise >= 100_000), None)
    if short is not None:
        new_amount = short.amount_paise * 96 // 100 // 1_000 * 1_000
        defects["gst_short"] = {
            "period": _period_of(short.due_date),
            "shortfall_paise": new_amount - short.amount_paise,
        }
        short.amount_paise = new_amount
    return defects


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
    _inject_obligation_defects(seed, obligations, days)
    debits.extend(obligations)
    statement = _assemble_statement(credits, debits)

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
            "scheduled_debits": len(obligations),
            "golden_leg_a_rows": len(golden_a),
            "golden_leg_b_rows": len(golden_b),
        },
        "settlement_scenarios": dict(sorted(scenario_counts.items())),
        "bank_credit_scenarios": dict(sorted(credit_scenarios.items())),
        "conventions": {
            "amounts": "integer paise in PSP/order files; rupee decimal strings in bank statement",
            "gst_on_fee": "18%, per-payment integer round-half-up, summed per batch",
            "min_amount_separation_paise": MIN_SEPARATION_PAISE,
        },
    }

    return {
        "orders": orders, "payments": payments, "settlements": settlements,
        "statement": statement, "golden_a": golden_a, "golden_b": golden_b,
        "manifest": manifest,
    }


def write_world(world: dict, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    def write_csv(name: str, columns: list[str], rows: list[dict]) -> None:
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
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

    write_csv("golden_leg_a.csv", S.GOLDEN_COLUMNS, world["golden_a"])
    write_csv("golden_leg_b.csv", S.GOLDEN_COLUMNS, world["golden_b"])

    with open(os.path.join(out_dir, "golden_manifest.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(world["manifest"], f, indent=2)


def seed_dir(seed: int, days: int = DAYS) -> str:
    """Canonical on-disk location for a generated world. 90-day worlds keep
    the original layout; other lengths get a d<days> suffix."""
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
