"""Independent held-out leg-A worlds: reconciliation cases the base generator
never produces, with ground truth a careful human could defend.

Each world reuses the generator's clean building blocks (orders, payments,
daily settlements, noise, scheduled debits) under its own RNG namespace
("holdout<seed>", so a holdout world never coincides with
`generate.build_world(seed)`), carves labelled case groups out of the
settlements and writes golden rows at construction time.

Tags: every case row carries one of CASE_TAGS (families 1-9, see TAG_FAMILY).
Clean filler settlements and their credits carry `clean_exact_utr`
(generate.SC_CLEAN); unrelated non-Razorpay inflows from the generator's
noise carry `noise_credit` (generate.SC_NOISE_CREDIT). Neither is a case tag.

Ground truth policy: `expected_disposition` is what can be defended from
settlements.csv, bank_statement.csv and settlement_merchants.csv alone. Hidden
truth that no observable evidence supports goes in `notes` only.

Template families: "A" for DEV_SEEDS, "B" for HELDOUT_SEEDS (and any other
seed). They share no narration template.

No structural tells: the statement runs TAIL_DAYS past the last payment day, so
every settlement's 0..10 day window lies inside it and cases are drawn from all
created_at dates; filler and case credits share one posting-delay policy
(DELAY_SHARE of each class lands 1-3 days after settled_at; twins and merged
groups prefer members that share a settled_at).

CLI:
    python -m recon.holdout --seed N [--out DIR] [--verify-determinism]
    python -m recon.holdout --all [--out DIR] [--verify-determinism]
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import tempfile
from datetime import date, timedelta
from random import Random

from recon import generate as G
from recon import schemas as S
from recon.generate import GenCredit, GenSettlement
from recon.normalize import roll_off_sunday, rupee_str_from_paise

HOLDOUT_VERSION = "1.1.0"
DEV_SEEDS = (1000, 1006)
HELDOUT_SEEDS = (1001, 1002, 1003, 1004, 1005)
DEFAULT_DAYS = 60           # payment days; the statement runs TAIL_DAYS longer
WINDOW_DAYS = 10            # credit value date must be 0..10 days after created_at
# The last settlement is created the day after the last payment day; a 14-day
# statement tail puts its whole 0..10 day window (plus margin) inside the statement.
TAIL_DAYS = WINDOW_DAYS + 4
CLOSING_FEE_PAISE = 59_000  # A/C maintenance fee Rs 500 + 18% GST, the statement's last row
# Posting delays: the same share of filler and case credits lands 1-3 days after
# settled_at, so a delay never marks a case. Uniqueness audits run on a window
# widened by DELAY_SLACK days, so any later delay keeps every audit valid.
DELAY_SHARE = 0.25
DELAY_SLACK = 4             # 3 days of delay + 1 day of Sunday roll
FILLER_TAG = G.SC_CLEAN
NOISE_TAG = G.SC_NOISE_CREDIT
MERCHANT_COLUMNS = ["settlement_id", "merchant_name"]

# --- Case tags ------------------------------------------------------------------
# Clean filler rows carry FILLER_TAG ("clean_exact_utr"); generator noise credits
# carry NOISE_TAG ("noise_credit"). Only the tags below count as case rows.
T_DED_STATED = "deduction_stated"
T_REF_SID = "ref_settlement_id"
T_REF_MERCHANT = "ref_merchant_name"
T_UTR_DAMAGED = "utr_damaged_multi"
T_MERGED = "merged_3plus"
T_REFUND = "refund_netted"
T_CHARGEBACK = "chargeback_netted"
T_TWINS_CLUE = "twins_with_clue"
T_TWINS_NOCLUE = "twins_no_clue"
T_DED_UNSTATED = "deduction_unstated"
T_NEVER_PAID = "never_paid"
T_ORPHAN = "orphan_rzp_credit"
T_NEAR_NET = "orphan_near_net"
T_RETURN = "outward_neft_return"

CASE_TAGS: dict[str, str] = {
    T_DED_STATED: "family 1: credit short of the net by an arbitrary deduction "
                  "(charges/TDS/commission) stated as a figure in the narration",
    T_REF_SID: "family 2: no UTR; narration carries the settlement id (A verbatim, "
               "B upper-cased without prefix); amount exact",
    T_REF_MERCHANT: "family 2: no UTR; narration names the settlement's merchant brand; "
                    "amount exact and unique",
    T_UTR_DAMAGED: "family 3: UTR in the narration with 2-3 characters damaged "
                   "(A: digit swaps/substitutions, B: OCR look-alikes/dropped digits); amount exact",
    T_MERGED: "family 4: 3-4 settlements created within 3 days paid as one credit "
              "(exact sum, unique subset; one member's UTR or none)",
    T_REFUND: "family 5: customer refund netted off the credit, figure stated in the narration",
    T_CHARGEBACK: "family 5: chargeback recovery netted off the credit, figure stated "
                  "in the narration",
    T_TWINS_CLUE: "family 6: equal-net twins; the credit narration names one twin (merchant "
                  "brand, UTR tail or settlement id); the unnamed twin is paid by its own "
                  "named credit or never paid",
    T_TWINS_NOCLUE: "family 7: equal-net twins, one credit, nothing distinguishes them; "
                    "abstain on all three rows (payer recorded in notes)",
    T_DED_UNSTATED: "family 1: credit short of the net by a deduction NOT stated anywhere and "
                    "no UTR/id: not defensibly matchable, so exception on both rows "
                    "(truth in notes)",
    T_NEVER_PAID: "family 8: settlement that never reached the bank (no credit)",
    T_ORPHAN: "family 8: Razorpay-looking credit with a well-formed UTR that belongs to no "
              "settlement; amount unrelated to any net",
    T_NEAR_NET: "family 8: Razorpay-looking credit belonging to no settlement whose amount is "
                "within Rs 1-9 of a real net or equals a net minus an unstated plausible charge",
    T_RETURN: "family 9 (own): an outward NEFT to a vendor bounced back (account closed / "
              "invalid) and re-credited 1-3 days after the debit, same UTR and amount; "
              "a non-settlement credit",
}
TAG_FAMILY: dict[str, int] = {
    T_DED_STATED: 1, T_REF_SID: 2, T_REF_MERCHANT: 2, T_UTR_DAMAGED: 3, T_MERGED: 4,
    T_REFUND: 5, T_CHARGEBACK: 5, T_TWINS_CLUE: 6, T_TWINS_NOCLUE: 7, T_DED_UNSTATED: 1,
    T_NEVER_PAID: 8, T_ORPHAN: 8, T_NEAR_NET: 8, T_RETURN: 9,
}

# --- Narration templates ----------------------------------------------------------
# Filler: family A reads like generator worlds; family B uses another bank's style.
_FILLER_TPL = {
    "A": list(G._NARRATION_TEMPLATES),
    "B": [
        "NEFT INWARD {utr} RAZORPAY SW PVT LTD",
        "INW/NEFT/{utr}/RAZORPAY SOFTWARE PRIVATE LIMITED",
        "RTGS INWARD-{utr}-RZP SOFTWARE PVT LTD-PAYOUT",
    ],
}

_TPL: dict[str, dict[str, list[str]]] = {
    "A": {
        "ded_charge_utr": ["NEFT CR-{utr}-RAZORPAY SOFTWARE PVT LTD-SETL LESS CHGS {amt}",
                           "RTGS-{utr}-RAZORPAY SOFTWARE PL-LESS BANK CHGS {amt}"],
        "ded_charge_noutr": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT LESS CHGS {amt}"],
        "ded_tds_utr": ["NEFT CR-{utr}-RAZORPAY SOFTWARE PVT LTD-TDS 194O {amt}"],
        "ded_tds_noutr": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETL NET TDS 194O {amt}"],
        "ded_comm_utr": ["NEFT CR-{utr}-RAZORPAY SOFTWARE PVT LTD-LESS COMMN {amt}"],
        "ded_comm_noutr": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETL LESS COMMN {amt}"],
        "ref_sid": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-{sid}",
                    "RAZORPAY SOFTWARE PVT LTD SETTLEMENT {sid}"],
        "ref_merchant": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-{merchant}",
                         "RAZORPAY SETTLEMENT-{merchant}-NEFT"],
        "merged_utr": ["NEFT CR-{utr}-RAZORPAY SOFTWARE PVT LTD-{n} SETL CONSOLIDATED",
                       "RTGS-{utr}-RAZORPAY SOFTWARE PL-BULK SETL"],
        "merged_noutr": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-BULK SETTLEMENT {n} BATCHES",
                         "RAZORPAY SOFTWARE PVT LTD-CONSOLIDATED SETL"],
        "refund": ["NEFT CR-{utr}-RAZORPAY SOFTWARE PVT LTD-LESS REFUND {amt}",
                   "RTGS-{utr}-RAZORPAY SOFTWARE PL-REFUND ADJ {amt}"],
        "chargeback": ["NEFT CR-{utr}-RAZORPAY SOFTWARE PVT LTD-CB RECOVERY {amt}"],
        "utr_tail": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-UTR ENDING {tail}"],
        "plain": ["NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT PAYOUT",
                  "RAZORPAY SOFTWARE PVT LTD-NEFT SETL"],
        "return_debit": ["NEFT DR-{utr}-{vendor}"],
        "return_credit": ["NEFT RETURN-{utr}-{vendor}-ACCOUNT CLOSED",
                          "NEFT RTN-{utr}-{vendor}-INVALID A/C"],
    },
    "B": {
        "ded_charge_utr": ["NEFT INWARD {utr} RAZORPAY SW PVT LTD /BANK COMMISSION RS.{amt} RECOVERED",
                           "INW/NEFT/{utr}/RZP SOFTWARE/SVC CHG INR {amt} DEDUCTED"],
        "ded_charge_noutr": ["INWARD REMITTANCE RAZORPAY SW PVT LTD - HANDLING FEE INR {amt} DEDUCTED"],
        "ded_tds_utr": ["NEFT INWARD {utr} RAZORPAY SW PVT LTD /INCOME TAX WITHHELD U/S 194-O RS.{amt}"],
        "ded_tds_noutr": ["RZP SOFTWARE PAYOUT - WITHHOLDING TAX SEC 194-O INR {amt}"],
        "ded_comm_utr": ["INW/NEFT/{utr}/RZP SOFTWARE/PLATFORM FEE ADJ INR {amt}"],
        "ded_comm_noutr": ["RAZORPAY SW PVT LTD PAYOUT - PLATFORM FEE ADJ RS.{amt}"],
        "ref_sid": ["RZP SW PVT LTD STLMNT ID {sid}",
                    "INWARD NEFT RAZORPAY SW/REF {sid}"],
        "ref_merchant": ["RZP SW PVT LTD PAYOUT FOR M/S {merchant}",
                         "INW/NEFT/RAZORPAY SOFTWARE/A/C {merchant}"],
        "merged_utr": ["INWARD NEFT {utr} RZP SW PL CLUBBED PAYOUT X{n}",
                       "INW/RTGS/{utr}/RAZORPAY SOFTWARE/COMBINED STLMNT"],
        "merged_noutr": ["RAZORPAY SW PVT LTD COMBINED STLMNT ({n} NOS)",
                         "RZP SOFTWARE PAYOUT - MULTIPLE SETTLEMENTS CLUBBED"],
        "refund": ["NEFT INWARD {utr} RZP SW PL NET OF CUSTOMER REFUND INR {amt}",
                   "INW/NEFT/{utr}/RAZORPAY SOFTWARE/REFUNDS SETOFF RS.{amt}"],
        "chargeback": ["NEFT INWARD {utr} RAZORPAY SW PVT LTD /DISPUTE DEBIT ADJUSTED RS.{amt}"],
        "utr_tail": ["INWARD NEFT RZP SW PVT LTD REF ..{tail}"],
        "plain": ["INWARD NEFT RAZORPAY SW PVT LTD PAYOUT",
                  "RZP SOFTWARE PRIVATE LIMITED - STLMNT CR"],
        "return_debit": ["NEFT/{utr}/{vendor}/VENDOR PMT"],
        "return_credit": ["RTN:NEFT/{utr}/{vendor}/INCORRECT A/C NO",
                          "INW RETURN NEFT {utr} {vendor} BENEFICIARY A/C CLOSED"],
    },
}

# OCR look-alikes used by family B damage.
_OCR = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _damage(rng: Random, family: str, utr: str) -> str:
    """Damage 2-3 characters of the UTR's digit part."""
    digits = list(range(4, len(utr)))
    s = list(utr)
    if family == "A":
        if rng.random() < 0.5:                       # 2-3 digit substitutions
            for pos in rng.sample(digits, rng.randint(2, 3)):
                s[pos] = rng.choice([d for d in "0123456789" if d != utr[pos]])
        else:                                        # adjacent swap + one substitution
            i = rng.choice([p for p in digits[:-1] if utr[p] != utr[p + 1]])
            s[i], s[i + 1] = s[i + 1], s[i]
            j = rng.choice([p for p in digits if p not in (i, i + 1)])
            s[j] = rng.choice([d for d in "0123456789" if d != utr[j]])
        return "".join(s)
    ocr = [p for p in digits if utr[p] in _OCR]
    if len(ocr) >= 2 and rng.random() < 0.6:         # OCR look-alike letters
        for pos in rng.sample(ocr, min(len(ocr), rng.randint(2, 3))):
            s[pos] = _OCR[utr[pos]]
        return "".join(s)
    drop = rng.sample(digits, 2)                     # two dropped digits
    return "".join(ch for k, ch in enumerate(utr) if k not in drop)


def sid_text(family: str, settlement_id: str) -> str:
    """How a settlement id is printed: A verbatim, B upper-cased token without prefix."""
    return settlement_id if family == "A" else settlement_id[len("setl_"):].upper()


def merchant_text(family: str, name: str) -> str:
    return merchant_renderings(name)[0 if family == "A" else 1]


def evidence_for(text: str, settlement_id: str, utr: str, merchant_name: str) -> list[str]:
    """Observable evidence in a credit's narration/ref that names one settlement:
    "sid" (its id token, case-insensitive), "utr" (any 4+ character fragment of its
    UTR) or "merchant" (its merchant name in any rendering). Two twins are
    distinguished by a text iff their evidence lists differ; a merchant name shared
    by both twins therefore does not distinguish them."""
    t = text.upper()
    out = []
    if settlement_id[len("setl_"):].upper() in t:
        out.append("sid")
    if any(utr[i:i + 4].upper() in t for i in range(len(utr) - 3)):
        out.append("utr")
    if any(r in t for r in merchant_renderings(merchant_name)):
        out.append("merchant")
    return out


# Bank charges incl. 18% GST (Rs 5+0.90 ... Rs 125+22.50).
_CHARGE_TABLE = (590, 1_180, 1_770, 2_360, 2_950, 5_900, 11_800, 14_750)

_MERCHANT_POOL = ("Kesar Kitchen", "Nilgiri Naturals", "Tanvi Threads", "Mitti Crafts",
                  "Bodhi Wellness", "Pinecone Toys", "Saffron Lane", "Kora Home",
                  "Vayu Sports", "Jharokha Decor")
_MERCHANT_WEIGHTS = (5, 3, 2, 2)

_VENDORS = ("SHREE GANESH PACKAGING", "VARDHMAN CARTONS", "MAHALAXMI ROADWAYS",
            "SAI KRUPA LOGISTICS", "PRAGATI OFFSET PRINTERS")


def family_of(seed: int) -> str:
    return "A" if seed in DEV_SEEDS else "B"


def role_of(seed: int) -> str:
    if seed in DEV_SEEDS:
        return "dev"
    return "heldout" if seed in HELDOUT_SEEDS else "adhoc"


def fig(family: str, paise: int) -> str:
    """How a figure is printed in a narration: A plain '1234.50', B Indian-grouped."""
    return rupee_str_from_paise(paise, indian_grouping=(family == "B"))


def merchant_renderings(name: str) -> list[str]:
    """Every way a merchant name may be printed (A: spaced, B: compact)."""
    return [name.upper(), name.upper().replace(" ", "")]


def _created(s: GenSettlement) -> date:
    return s.created_at.date()


class _Builder:
    def __init__(self, seed: int, days: int):
        self.seed, self.days = seed, days
        self.family = family_of(seed)
        # Own namespace: never the same draws as generate.build_world(seed).
        self.key = f"holdout{seed}"
        self.ids = G.Ids(self.key)
        self.orders, self.payments = G._build_orders_and_payments(self.key, self.ids, days)
        self.settlements = G._build_settlements(self.key, self.ids, self.payments)
        self.world_end = G.START_DATE + timedelta(days=days - 1)
        self.stmt_days = days + TAIL_DAYS
        self.stmt_end = G.START_DATE + timedelta(days=self.stmt_days - 1)
        self.rng_plan = Random(f"{self.key}:plan")
        self.rng_bank = Random(f"{self.key}:bank")
        self.rng_amt = Random(f"{self.key}:amounts")
        self.credits: list[GenCredit] = []
        self.delayable: list[GenCredit] = []   # credits that pay (or truly pay) settlements
        self.debits: list = []
        self.late_notes: list = []      # (record, fn) resolved once txn_ids exist
        self.merchant = self._assign_merchants()
        # Every settlement's window lies inside the statement (TAIL_DAYS), so a case
        # can sit on any created_at date: the case share does not depend on it.
        self.pool = self.settlements[:]
        self.rng_plan.shuffle(self.pool)

    # -- helpers ---------------------------------------------------------------
    def _assign_merchants(self) -> dict[str, str]:
        rng = Random(f"{self.key}:merchants")
        n = rng.randint(2, 4)
        self.merchant_names = rng.sample(_MERCHANT_POOL, n)
        return {s.settlement_id: rng.choices(self.merchant_names,
                                             weights=_MERCHANT_WEIGHTS[:n])[0]
                for s in self.settlements}

    def evidence(self, text: str, s: GenSettlement) -> list[str]:
        return evidence_for(text, s.settlement_id, s.utr, self.merchant[s.settlement_id])

    def clean_ref(self, avoid: list[GenSettlement]) -> str:
        """A NEFTIN-style ref that carries no evidence for any settlement in avoid."""
        while True:
            ref = G._ref_for(self.rng_bank, None)
            if not any(self.evidence(ref, s) for s in avoid):
                return ref

    def tpl(self, kind: str) -> str:
        return self.rng_bank.choice(_TPL[self.family][kind])

    def take(self, pred=lambda s: True) -> GenSettlement:
        for i, s in enumerate(self.pool):
            if pred(s):
                return self.pool.pop(i)
        raise AssertionError("no eligible settlement left for a case")

    def window(self, vd: date) -> list[GenSettlement]:
        """Settlements whose 0..10 day window holds vd or any date up to DELAY_SLACK
        days later (a superset of the final window once a delay is applied)."""
        return [s for s in self.settlements
                if -DELAY_SLACK <= (vd - _created(s)).days <= WINDOW_DAYS]

    def exact_subsets(self, amount: int, vd: date, max_k: int) -> int:
        """How many subsets (size 1..max_k) of window settlements sum to amount."""
        nets = [s.amount_paise for s in self.window(vd)]
        return sum(1 for k in range(1, max_k + 1)
                   for combo in itertools.combinations(nets, k) if sum(combo) == amount)

    def amount_ok(self, amount: int, vd: date, near_ok=(), gap: int = G.MIN_SEPARATION_PAISE,
                  max_k: int = 3) -> bool:
        """No settlement outside near_ok within `gap`, and no exact subset match."""
        if any(abs(s.amount_paise - amount) < gap for s in self.settlements
               if s.settlement_id not in near_ok):
            return False
        return self.exact_subsets(amount, vd, max_k) == 0

    def emit(self, settlements: list[GenSettlement], *, amount: int, vd: date, narration: str,
             ref: str, disposition: str, tag: str, disc: int = 0, notes: str = "",
             link: bool = True, delayable: bool = True) -> GenCredit:
        c = GenCredit(value_date=vd, narration=narration, ref_no=ref, amount_paise=amount,
                      disposition=disposition, scenario=tag,
                      counterparties=list(settlements) if link else [],
                      expected_discrepancy_paise=disc, notes=notes)
        self.credits.append(c)
        if delayable:
            self.delayable.append(c)
        if link:
            for s in settlements:
                s.counterparties.append(c)
        return c

    def separate(self, twin_pairs: list[tuple[GenSettlement, GenSettlement]],
                 groups: list[list[GenSettlement]] = ()) -> None:
        """Force twins equal, keep every other pair of nets >= Rs 10 apart, keep each
        merged group's sum >= Rs 10 from every single net and explained by exactly
        one subset (<= 5) of its window, and keep each twin amount explained only by
        the two twins (subsets <= 3)."""
        rng = Random(f"{self.key}:nudge")
        partner: dict[str, GenSettlement] = {}
        for a, b in twin_pairs:
            lo, hi = sorted((a, b), key=lambda s: s.amount_paise)
            G._force_net(lo, hi.amount_paise)   # raise the smaller: payments stay positive
            partner[a.settlement_id], partner[b.settlement_id] = b, a

        def group_bad(g: list[GenSettlement]) -> bool:
            total = sum(m.amount_paise for m in g)
            return (any(abs(total - s.amount_paise) < G.MIN_SEPARATION_PAISE
                        for s in self.settlements)
                    or self.exact_subsets(total, max(m.settled_at for m in g), 5) != 1)

        def pair_bad(a: GenSettlement, b: GenSettlement) -> bool:
            return self.exact_subsets(a.amount_paise, max(a.settled_at, b.settled_at), 3) != 2

        for _ in range(max(80, 2 * len(self.settlements))):
            ordered = sorted(self.settlements, key=lambda s: (s.amount_paise, s.settlement_id))
            bad = next(((x, y) for x, y in zip(ordered, ordered[1:])
                        if y.amount_paise - x.amount_paise < G.MIN_SEPARATION_PAISE
                        and partner.get(x.settlement_id) is not y), None)
            delta = rng.randint(G.MIN_SEPARATION_PAISE, 3 * G.MIN_SEPARATION_PAISE)
            if bad is None:
                clash = next((g for g in groups if group_bad(g)), None)
                if clash is not None:
                    # a random member (never a twin): a member shared with a colliding
                    # subset would move both sums together and never break the tie
                    m = rng.choice(clash)
                    G._force_net(m, m.amount_paise + delta)
                    continue
                twin = next(((a, b) for a, b in twin_pairs if pair_bad(a, b)), None)
                if twin is None:
                    break
                target = twin[0].amount_paise + delta
                for s in twin:                      # move the pair together
                    G._force_net(s, target)
                continue
            x, y = bad
            if y.settlement_id not in partner:
                G._force_net(y, y.amount_paise + delta)
            elif x.settlement_id not in partner:
                G._force_net(x, x.amount_paise + delta)
            else:                                   # two twin pairs collide: move a pair
                target = y.amount_paise + delta
                G._force_net(y, target)
                G._force_net(partner[y.settlement_id], target)
        else:
            raise AssertionError("separation audit failed to converge")
        G._sync_orders_to_payments(self.orders, self.payments)

    # -- cases -----------------------------------------------------------------
    def plan(self) -> None:
        # 1. Cases that change nets (twins forced equal, merged sums audited) are
        #    chosen first, then the separation audit fixes every net for good.
        #    Groups come first: weekend batches sharing a settled_at are scarce.
        self.merged = [self.take_group((3,)),
                       self.take_group((4, 3) if self.rng_plan.random() < 0.5 else (3,))]
        self.twins_clue = [self.take_pair(), self.take_pair()]
        self.twins_noclue = [self.take_pair(), self.take_pair()]
        self.clue_kinds = [self.rng_plan.choice(("merchant", "utr_tail", "sid")),
                           self.rng_plan.choice(("merchant", "utr_tail"))]
        for (a, b), kind in zip(self.twins_clue, self.clue_kinds):
            if kind == "merchant" and self.merchant[a.settlement_id] == self.merchant[b.settlement_id]:
                self.merchant[b.settlement_id] = self.rng_plan.choice(
                    [n for n in self.merchant_names if n != self.merchant[a.settlement_id]])
        a, b = self.twins_noclue[0]        # this pair's credit prints their SHARED merchant
        self.merchant[b.settlement_id] = self.merchant[a.settlement_id]
        self.separate(self.twins_clue + self.twins_noclue, self.merged)
        # 2. Roles that only read nets, picked with amount-aware predicates.
        solo = lambda s: self.exact_subsets(s.amount_paise, s.settled_at, 3) == 1
        big = lambda s: s.amount_paise >= 300_000 and solo(s)   # deductions need room
        self.ded = [self.take(big) for _ in range(3)]
        self.ref_sid = self.take()
        self.ref_merchant = self.take()
        self.damaged = [self.take() for _ in range(2)]
        self.refund = self.take(big)
        self.chargeback = self.take(big)
        self.unstated = self.take(lambda s: big(s) and self.isolated(s))
        self.never_paid = [self.take(), self.take(self.isolated)]   # [1] gets a near-net decoy
        self.decoy_filler_target = self.take(self.isolated)         # stays filler, paid normally

    def isolated(self, s: GenSettlement) -> bool:
        """Every other net is >= Rs 50 + the largest plausible charge away, so a
        decoy/short credit near s is tempting for s and for nothing else."""
        return all(abs(x.amount_paise - s.amount_paise) >= 5_000 + max(_CHARGE_TABLE)
                   for x in self.settlements if x is not s)

    def take_pair(self, max_gap: int = 1) -> tuple[GenSettlement, GenSettlement]:
        """Two twin candidates. Preference: same settled_at (then one credit date is
        equally plausible for both and carries no extra delay), nets within a third
        of each other (forcing them equal stays a modest nudge); fallbacks relax
        the date to created <= max_gap days apart and the net ratio."""
        ladder = ((True, 4, 3), (True, 2, 1), (False, 4, 3), (False, 2, 1),
                  (True, 10**9, 1), (False, 10**9, 1))
        for same_day, num, den in ladder:
            for i, a in enumerate(self.pool):
                for b in self.pool[i + 1:]:
                    lo, hi = sorted((a.amount_paise, b.amount_paise))
                    dated = (a.settled_at == b.settled_at if same_day
                             else abs((_created(a) - _created(b)).days) <= max_gap)
                    if dated and hi * den <= lo * num:
                        self.pool = [s for s in self.pool if s is not a and s is not b]
                        x, y = sorted((a, b), key=lambda s: (s.created_at, s.settlement_id))
                        return x, y
        raise AssertionError("no twin pair available")

    def take_group(self, sizes: tuple[int, ...], span: int = 2) -> list[GenSettlement]:
        """Settlements paid out together: preferably a batch sharing one settled_at
        (weekend settlements all fall due on Monday), trying each size in turn;
        fallback: sizes[0] settlements created within `span` days."""
        same = lambda a, s: s.settled_at == a.settled_at
        close = lambda a, s: 0 <= (_created(s) - _created(a)).days <= span
        for fits, ks in ((same, sizes), (close, sizes[:1])):
            for k in ks:
                for anchor in self.pool:
                    near = [s for s in self.pool if s is not anchor and fits(anchor, s)]
                    if len(near) >= k - 1:
                        group = [anchor] + near[:k - 1]
                        self.pool = [s for s in self.pool if all(s is not g for g in group)]
                        return sorted(group, key=lambda s: (s.created_at, s.settlement_id))
        raise AssertionError("no settlement group available")

    def single(self, s: GenSettlement, tag: str, narration: str, notes: str) -> GenCredit:
        """One settlement paid in full by one credit that carries no UTR in ref_no."""
        s.scenario, s.notes = tag, notes
        return self.emit([s], amount=s.amount_paise, vd=s.settled_at, narration=narration,
                         ref=G._ref_for(self.rng_bank, None), disposition=S.MATCHED,
                         tag=tag, notes=notes)

    def case_references(self) -> None:
        s = self.ref_sid
        token = s.settlement_id[len("setl_"):].upper()
        assert sum(x.settlement_id[len("setl_"):].upper() == token
                   for x in self.settlements) == 1, "settlement token not case-insensitively unique"
        self.single(s, T_REF_SID,
                    self.tpl("ref_sid").format(sid=sid_text(self.family, s.settlement_id)),
                    "no UTR; narration names the settlement id")
        m = self.ref_merchant
        name = self.merchant[m.settlement_id]
        self.single(m, T_REF_MERCHANT,
                    self.tpl("ref_merchant").format(merchant=merchant_text(self.family, name)),
                    f"no UTR; narration names merchant '{name}'; amount exact and unique")

    def case_damaged_utr(self) -> None:
        for s in self.damaged:
            for _ in range(200):
                bad = _damage(self.rng_bank, self.family, s.utr)
                dist = levenshtein(bad, s.utr)
                # the true UTR must be the clear nearest one: every other UTR >= dist + 3
                if dist >= 2 and all(levenshtein(bad, x.utr) >= dist + 3
                                     for x in self.settlements if x is not s):
                    break
            else:
                raise AssertionError("could not damage a UTR unambiguously")
            narr = self.rng_bank.choice(_FILLER_TPL[self.family]).format(utr=bad)
            self.single(s, T_UTR_DAMAGED, narr,
                        f"UTR printed as {bad} (edit distance {dist}); amount exact")

    def draw_deduction(self, kind: str, net: int) -> int:
        r = self.rng_amt
        if kind == "charge":
            return r.choice(_CHARGE_TABLE) if r.random() < 0.5 else r.randint(1_000, 45_000)
        if kind == "tds":
            return max(100, (net * r.randint(5, 100) + 5_000) // 10_000)   # 0.05%-1%
        return r.randint(2_000, 90_000)                                     # commission

    def deduct(self, s: GenSettlement, tag: str, what: str, draw, kind: str,
               has_utr: bool) -> None:
        """Credit = net - d, with d printed in the narration (defensible match)."""
        vd = s.settled_at
        # credit + stated figure must be explained by this settlement alone
        assert self.exact_subsets(s.amount_paise, vd, 3) == 1, "net not isolated"
        for _ in range(200):
            d = draw()
            if 0 < d < s.amount_paise // 2 and self.amount_ok(s.amount_paise - d, vd,
                                                               near_ok={s.settlement_id}):
                break
        else:
            raise AssertionError(f"no clean {what} amount")
        narr = self.tpl(kind).format(utr=s.utr, amt=fig(self.family, d))
        notes = f"{what} of {d} paise netted off the credit; figure stated in the narration"
        s.scenario, s.disposition, s.notes = tag, S.MATCHED_WITH_DISCREPANCY, notes
        s.expected_discrepancy_paise = -d
        self.emit([s], amount=s.amount_paise - d, vd=vd, narration=narr,
                  ref=G._ref_for(self.rng_bank, s.utr if has_utr else None),
                  disposition=S.MATCHED_WITH_DISCREPANCY, tag=tag, disc=-d, notes=notes)

    def case_deduction_stated(self) -> None:
        kinds = ["charge", "tds", self.rng_plan.choice(["charge", "tds", "comm"])]
        with_utr = [True, self.rng_plan.random() < 0.5, False]
        for s, kind, has_utr in zip(self.ded, kinds, with_utr):
            self.deduct(s, T_DED_STATED, f"{kind} deduction",
                        lambda: self.draw_deduction(kind, s.amount_paise),
                        f"ded_{kind}_{'utr' if has_utr else 'noutr'}", has_utr)

    def case_netted(self) -> None:
        r = self.rng_amt

        def refund(net: int) -> int:           # partial refund, often whole rupees
            amt = r.randint(net * 3 // 100, net * 25 // 100)
            return max(100, amt // 100 * 100) if r.random() < 0.5 else amt

        s = self.refund
        self.deduct(s, T_REFUND, "customer refund", lambda: refund(s.amount_paise),
                    "refund", True)
        c = self.chargeback
        self.deduct(c, T_CHARGEBACK, "chargeback recovery",
                    lambda: r.randint(49_900, min(900_000, c.amount_paise // 3)),
                    "chargeback", True)

    def case_merged(self) -> None:
        for gi, group in enumerate(self.merged):
            vd = max(s.settled_at for s in group)
            amount = sum(s.amount_paise for s in group)
            assert self.exact_subsets(amount, vd, 5) == 1, "merged sum not uniquely explained"
            if gi == 0:
                carrier = self.rng_plan.choice(group)
                narr = self.tpl("merged_utr").format(utr=carrier.utr, n=len(group))
                ref = G._ref_for(self.rng_bank, carrier.utr)
                how = f"narration carries only the UTR of {carrier.settlement_id}"
            else:
                narr = self.tpl("merged_noutr").format(n=len(group))
                ref = G._ref_for(self.rng_bank, None)
                how = "no UTR; only the exact sum ties the members together"
            notes = f"{len(group)} settlements paid as one consolidated credit; {how}"
            for s in group:
                s.scenario, s.disposition, s.notes = T_MERGED, S.MATCHED_MERGED, notes
            self.emit(group, amount=amount, vd=vd, narration=narr, ref=ref,
                      disposition=S.MATCHED_MERGED, tag=T_MERGED, notes=notes)

    def utr_tail(self, s: GenSettlement) -> str:
        n = 6 if self.family == "A" else 5
        while sum(x.utr.endswith(s.utr[-n:]) for x in self.settlements) > 1:
            n += 1
        return s.utr[-n:]

    def clue_credit(self, s: GenSettlement, other: GenSettlement, kind: str,
                    vd: date) -> GenCredit:
        """Credit paying s whose narration names s and carries nothing about `other`."""
        for _ in range(50):
            if kind == "merchant":
                narr = self.tpl("ref_merchant").format(
                    merchant=merchant_text(self.family, self.merchant[s.settlement_id]))
            elif kind == "sid":
                narr = self.tpl("ref_sid").format(sid=sid_text(self.family, s.settlement_id))
            else:
                narr = self.tpl("utr_tail").format(tail=self.utr_tail(s))
            ref = self.clean_ref([other])
            if self.evidence(narr + " | " + ref, s) and not self.evidence(narr + " | " + ref, other):
                break
            kind = "sid"          # tail collided with the other twin's UTR: name the id
        else:
            raise AssertionError("no clean clue")
        assert self.exact_subsets(s.amount_paise, vd, 3) == 2, "twin amount not isolated"
        notes = (f"equal-net twin of {other.settlement_id}; the narration's {kind} clue "
                 f"names this settlement")
        s.scenario, s.disposition, s.notes = T_TWINS_CLUE, S.MATCHED, notes
        return self.emit([s], amount=s.amount_paise, vd=vd, narration=narr, ref=ref,
                         disposition=S.MATCHED, tag=T_TWINS_CLUE, notes=notes)

    def case_twins_clue(self) -> None:
        (a, b), (c, d) = self.twins_clue
        # One credit names the paid twin; its twin never gets a credit.
        paid, unpaid = (a, b) if self.rng_plan.random() < 0.5 else (b, a)
        self.clue_credit(paid, unpaid, self.clue_kinds[0], max(a.settled_at, b.settled_at))
        unpaid.scenario, unpaid.disposition = T_TWINS_CLUE, S.EXCEPTION_MISSING_BANK
        unpaid.notes = (f"equal-net twin of {paid.settlement_id}, whose credit names "
                        f"{paid.settlement_id}; no credit ever arrived for this one")
        # Both paid; each credit names its own twin.
        vd = max(c.settled_at, d.settled_at)
        self.clue_credit(c, d, self.clue_kinds[1], vd)
        self.clue_credit(d, c, self.clue_kinds[1], vd)

    def case_twins_noclue(self) -> None:
        for i, (a, b) in enumerate(self.twins_noclue):
            vd = max(a.settled_at, b.settled_at)
            paid = a if self.rng_plan.random() < 0.5 else b
            if i == 0:     # prints the merchant both twins share: still no clue
                narr = self.tpl("ref_merchant").format(
                    merchant=merchant_text(self.family, self.merchant[a.settlement_id]))
            else:
                narr = self.tpl("plain")
            ref = self.clean_ref([a, b])
            text = narr + " | " + ref
            assert self.evidence(text, a) == self.evidence(text, b)
            assert not {"sid", "utr"} & set(self.evidence(text, a))
            assert self.exact_subsets(a.amount_paise, vd, 3) == 2, "twin amount not isolated"
            credit = self.emit([a, b], amount=paid.amount_paise, vd=vd, narration=narr, ref=ref,
                               disposition=S.AMBIGUOUS_ABSTAIN, tag=T_TWINS_NOCLUE)
            for s in (a, b):
                s.scenario, s.disposition = T_TWINS_NOCLUE, S.AMBIGUOUS_ABSTAIN
            self.late_notes.append((credit, lambda p=paid, a=a, b=b: (
                f"twins {a.settlement_id}/{b.settlement_id} share this amount and window; "
                f"nothing observable separates them; truth: pays {p.settlement_id}")))
            for s in (a, b):
                self.late_notes.append((s, lambda s=s, p=paid, c=credit: (
                    "indistinguishable equal-net twin; truth: "
                    + (f"paid by {c.txn_id}" if s is p else "never paid"))))

    def fake_utr(self, prefix: str | None = None) -> str:
        """Well-formed UTR far (edit distance >= 6) from every settlement UTR."""
        while True:
            u = (prefix or self.rng_bank.choice(G.BANK_PREFIXES)) + "".join(
                self.rng_bank.choice("0123456789") for _ in range(12))
            if all(levenshtein(u, s.utr) >= 6 for s in self.settlements):
                return u

    def orphan(self, *, amount: int, vd: date, narration: str, ref: str, tag: str,
               notes: str, delayable: bool = False) -> GenCredit:
        return self.emit([], amount=amount, vd=vd, narration=narration, ref=ref,
                         disposition=S.EXCEPTION_MISSING_SETTLEMENT, tag=tag, notes=notes,
                         link=False, delayable=delayable)

    def plausible_shortfall(self, net: int) -> int:
        r = self.rng_amt
        return r.choice(_CHARGE_TABLE) if r.random() < 0.6 else max(100, (net * 10 + 5_000) // 10_000)

    def case_unstated(self) -> None:
        s = self.unstated
        vd = s.settled_at
        for _ in range(200):
            d = self.plausible_shortfall(s.amount_paise)
            if self.amount_ok(s.amount_paise - d, vd, near_ok={s.settlement_id}, gap=5_000):
                break
        else:
            raise AssertionError("no clean unstated shortfall")
        narr, ref = self.tpl("plain"), self.clean_ref([s])
        assert not self.evidence(narr + " | " + ref, s)
        c = self.orphan(amount=s.amount_paise - d, vd=vd, narration=narr, ref=ref,
                        tag=T_DED_UNSTATED, notes="", delayable=True)   # truly pays s
        s.scenario, s.disposition = T_DED_UNSTATED, S.EXCEPTION_MISSING_BANK
        c.notes = (f"amount differs from the nearest net and no figure/UTR explains it; "
                   f"truth: pays {s.settlement_id} short by {d} paise (unstated deduction)")
        self.late_notes.append((s, lambda c=c, d=d: (
            f"no defensible credit; truth: paid by {c.txn_id} short by {d} paise, "
            f"deduction never stated")))

    def case_negatives(self) -> None:
        for s in self.never_paid:
            s.scenario, s.disposition = T_NEVER_PAID, S.EXCEPTION_MISSING_BANK
            s.notes = "settlement processed by the PSP but no credit ever reached the bank"
        # Orphan: well-formed but foreign UTR, amount unrelated to every net.
        r = self.rng_amt
        while True:
            vd = roll_off_sunday(G.START_DATE + timedelta(days=r.randint(3, self.days - 3)))
            amount = r.randint(500_000, 6_000_000)
            if self.amount_ok(amount, vd, gap=5_000):
                break
        u = self.fake_utr()
        self.orphan(amount=amount, vd=vd, ref=G._ref_for(self.rng_bank, u),
                    narration=self.rng_bank.choice(_FILLER_TPL[self.family]).format(utr=u),
                    tag=T_ORPHAN, notes="UTR matches no settlement; no settlement nets to it")
        # Near-net decoys: one next to a never-paid net, one next to a paid filler net.
        for i, target in enumerate((self.never_paid[1], self.decoy_filler_target)):
            # dated like a settlement credit: on settled_at or 1-3 days later
            late = r.random() < DELAY_SHARE
            vd = roll_off_sunday(target.settled_at + timedelta(days=r.randint(1, 3) if late else 0))
            for _ in range(200):
                if r.random() < 0.5:
                    how = "net minus an unstated plausible charge"
                    amount = target.amount_paise - self.plausible_shortfall(target.amount_paise)
                else:
                    how = "within Rs 1-9 of the net"
                    amount = target.amount_paise + r.choice((-1, 1)) * r.randint(100, 900)
                if self.amount_ok(amount, vd, near_ok={target.settlement_id}, gap=5_000):
                    break
            else:
                raise AssertionError("no clean decoy amount")
            if i == 0:
                u = self.fake_utr()
                narr = self.rng_bank.choice(_FILLER_TPL[self.family]).format(utr=u)
                ref = G._ref_for(self.rng_bank, u)
            else:
                narr, ref = self.tpl("plain"), self.clean_ref([target])
            state = "never paid" if i == 0 else "paid by its own UTR credit"
            self.orphan(amount=amount, vd=vd, narration=narr, ref=ref, tag=T_NEAR_NET,
                        notes=f"decoy {how} of {target.settlement_id} ({state}); "
                              f"belongs to no settlement")

    def case_returns(self, count: int = 2) -> None:
        """Outward vendor NEFTs that bounce back: the debit and its return credit share
        UTR and amount, so the credit is provably not settlement money."""
        r = Random(f"{self.key}:returns")
        own_bank = r.choice(G.BANK_PREFIXES)       # outward UTRs carry the account's bank
        vendors = r.sample(_VENDORS, count)
        for vendor in vendors:
            while True:
                day = G.START_DATE + timedelta(days=r.randint(3, self.days - 6))
                if day.weekday() == 6:
                    continue
                back = roll_off_sunday(day + timedelta(days=r.randint(1, 3)))
                amount = r.randint(2_000, 60_000) * 100 + (r.randint(1, 99) if r.random() < 0.3 else 0)
                if self.amount_ok(amount, back, gap=5_000):
                    break
            u = self.fake_utr(own_bank)
            self.debits.append(G.GenDebit(
                value_date=day, narration=self.tpl("return_debit").format(utr=u, vendor=vendor),
                ref_no="", amount_paise=amount))
            self.emit([], amount=amount, vd=back,
                      narration=self.tpl("return_credit").format(utr=u, vendor=vendor),
                      ref=u, disposition=S.NON_SETTLEMENT_CREDIT, tag=T_RETURN, link=False,
                      delayable=False,
                      notes=f"return of the outward NEFT {u} to {vendor} debited on "
                            f"{day.isoformat()}; not settlement money")

    def filler(self) -> None:
        for s in self.settlements:
            if s.scenario == FILLER_TAG and not s.counterparties and s.disposition == S.MATCHED:
                narr = self.rng_bank.choice(_FILLER_TPL[self.family]).format(utr=s.utr)
                self.emit([s], amount=s.amount_paise, vd=s.settled_at, narration=narr,
                          ref=G._ref_for(self.rng_bank, s.utr), disposition=S.MATCHED,
                          tag=FILLER_TAG)

    def assign_delays(self) -> None:
        """Post the same share of filler and case credits 1-3 days after their base
        date (settled_at, or a group's common settled_at). The share is counted in
        settlement links per class, so a late credit never marks a case."""
        rng = Random(f"{self.key}:delays")
        filler = [c for c in self.delayable if c.scenario == FILLER_TAG]
        cases = [c for c in self.delayable if c.scenario != FILLER_TAG]
        for group in (filler, cases):
            weight = [max(1, len(c.counterparties)) for c in group]
            target = round(DELAY_SHARE * sum(weight))
            order = list(range(len(group)))
            rng.shuffle(order)
            done = 0
            for i in order:
                if done + weight[i] <= target:
                    c = group[i]
                    c.value_date = roll_off_sunday(c.value_date + timedelta(days=rng.randint(1, 3)))
                    done += weight[i]

    def build(self) -> dict:
        self.plan()
        self.case_deduction_stated()
        self.case_references()
        self.case_damaged_utr()
        self.case_merged()
        self.case_netted()
        self.case_twins_clue()
        self.case_twins_noclue()
        self.case_unstated()
        self.case_negatives()
        self.case_returns()
        self.filler()
        self.assign_delays()
        noise_credits, noise_debits = G._build_noise(self.key, self.settlements, self.stmt_days)
        self.credits.extend(noise_credits)
        self.debits.extend(noise_debits)
        self.debits.extend(G._build_obligations(self.key, self.payments, self.stmt_days))
        # A fixed closing row pins the statement's last date at (or just after) stmt_end.
        self.debits.append(G.GenDebit(value_date=roll_off_sunday(self.stmt_end),
                                      narration="A/C MAINTENANCE FEE INCL GST", ref_no="",
                                      amount_paise=CLOSING_FEE_PAISE))
        statement = G._assemble_statement(self.credits, self.debits)
        for rec, fn in self.late_notes:
            rec.notes = fn()
        return self.world(statement)

    def world(self, statement: list[dict]) -> dict:
        golden_a = [_golden_row(S.RT_SETTLEMENT, s.settlement_id, s.disposition,
                                [c.txn_id for c in s.counterparties], s.scenario,
                                s.expected_discrepancy_paise, s.notes)
                    for s in self.settlements]
        golden_a += [_golden_row(S.RT_BANK_CREDIT, c.txn_id, c.disposition,
                                 [x.settlement_id for x in c.counterparties], c.scenario,
                                 c.expected_discrepancy_paise, c.notes)
                     for c in sorted(self.credits, key=lambda c: c.txn_id)]
        golden_b = [_golden_row(S.RT_ORDER, o.order_id, o.legb_disposition,
                                o.legb_counterparties, o.legb_scenario, 0, o.legb_notes)
                    for o in self.orders]
        golden_b += [_golden_row(S.RT_PAYMENT, p.payment_id, p.legb_disposition,
                                 p.legb_counterparties, p.legb_scenario, 0, p.legb_notes)
                     for p in self.payments]
        merchants = [{"settlement_id": s.settlement_id, "merchant_name": self.merchant[s.settlement_id]}
                     for s in self.settlements]
        manifest = {
            "holdout_version": HOLDOUT_VERSION,
            "base_generator_version": G.GENERATOR_VERSION,
            "seed": self.seed, "family": self.family, "role": role_of(self.seed),
            "period": {"start": G.START_DATE.isoformat(), "days": self.days,
                       "statement_days": self.stmt_days},
            "counts": {
                "orders": len(self.orders), "payments": len(self.payments),
                "settlements": len(self.settlements), "bank_rows": len(statement),
                "bank_credits": len(self.credits), "bank_debits": len(self.debits),
                "golden_leg_a_rows": len(golden_a), "golden_leg_b_rows": len(golden_b),
                "case_rows": sum(1 for r in golden_a if r["scenario_tag"] in CASE_TAGS),
                "merchants": len({m["merchant_name"] for m in merchants}),
            },
            "settlement_scenarios": _count(s.scenario for s in self.settlements),
            "bank_credit_scenarios": _count(c.scenario for c in self.credits),
            "case_row_scenarios": _count(r["scenario_tag"] for r in golden_a
                                         if r["scenario_tag"] in CASE_TAGS),
            "conventions": {
                "filler_tag": FILLER_TAG, "noise_tag": NOISE_TAG,
                "match_window_days": WINDOW_DAYS,
                "min_amount_separation_paise": G.MIN_SEPARATION_PAISE,
                "discrepancy": "received - expected, paise; stated verbatim in the narration",
            },
        }
        return {"orders": self.orders, "payments": self.payments,
                "settlements": self.settlements, "statement": statement,
                "golden_a": golden_a, "golden_b": golden_b, "manifest": manifest,
                "settlement_merchants": merchants}


def _golden_row(rt, rid, disp, cps, tag, disc, notes) -> dict:
    return {"record_type": rt, "record_id": rid, "expected_disposition": disp,
            "counterparty_ids": S.COUNTERPARTY_SEP.join(cps), "scenario_tag": tag,
            "expected_discrepancy_paise": disc, "notes": notes}


def _count(tags) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tags:
        out[t] = out.get(t, 0) + 1
    return dict(sorted(out.items()))


# --- Public interface -------------------------------------------------------------

def build_holdout_world(seed: int, days: int = DEFAULT_DAYS) -> dict:
    return _Builder(seed, days).build()


def write_holdout_world(world: dict, out_dir: str) -> None:
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
    write_csv("settlement_merchants.csv", MERCHANT_COLUMNS, world["settlement_merchants"])
    with open(os.path.join(out_dir, "golden_manifest.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(world["manifest"], f, indent=2)
        f.write("\n")


def holdout_dir(seed: int) -> str:
    return os.path.join("data", "holdout", str(seed))


def generate_holdout(seed: int, out_dir: str | None = None, days: int = DEFAULT_DAYS) -> str:
    out = out_dir or holdout_dir(seed)
    write_holdout_world(build_holdout_world(seed, days), out)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="every dev and held-out seed")
    ap.add_argument("--out", default=None, help="output dir (with --all: parent dir)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--verify-determinism", action="store_true",
                    help="build twice into temp dirs and compare digests")
    args = ap.parse_args(argv)
    if args.all == (args.seed is not None):
        ap.error("give exactly one of --seed N or --all")
    seeds = list(DEV_SEEDS + HELDOUT_SEEDS) if args.all else [args.seed]
    for seed in seeds:
        if args.verify_determinism:
            with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
                write_holdout_world(build_holdout_world(seed, args.days), t1)
                write_holdout_world(build_holdout_world(seed, args.days), t2)
                d1, d2 = G._dir_digest(t1), G._dir_digest(t2)
            print(f"seed {seed}: {d1} {'==' if d1 == d2 else '!='} {d2}")
            if d1 != d2:
                raise SystemExit("NOT deterministic")
            continue
        if args.all:
            out = os.path.join(args.out or os.path.join("data", "holdout"), str(seed))
        else:
            out = args.out or holdout_dir(seed)
        generate_holdout(seed, out, args.days)
        with open(os.path.join(out, "golden_manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        print(f"seed {seed} family {m['family']} role {m['role']}: "
              f"{m['counts']['case_rows']} case rows -> {out}")


if __name__ == "__main__":
    main()
