"""Leg A reconciliation engine: settlements <-> bank credits.

Deterministic, multi-pass, evidence-scored. Design rules:

  - Identity evidence (UTR) outranks amount evidence; amount evidence is only
    ever EXACT integer-paise equality — no match in this engine rests on an
    amount tolerance, which is what defeats near-collision traps honestly.
  - Every decision carries the evidence that produced it; every exception
    carries the candidates that were considered and why each was rejected.
  - When candidates are indistinguishable the engine abstains. Forcing a
    plausible-but-unproven match is treated as worse than leaving work for a
    human.

Pass order (later passes only see records unclaimed by earlier ones):
  0. scope split + UTR token extraction + settlement-shaped classification
  1. exact UTR (verbatim run or separator-mangled substring); handles clean,
     delayed, decomposable-discrepancy, duplicate and split-by-shared-UTR cases
  2. fuzzy UTR (fragment >= 10 chars, or one-digit corruption) — only with
     exact amount corroboration
  3. merge resolution for UTR-certain credits whose amount exceeds the
     settlement net by exactly one other settlement's net
  4. exact-amount + window matching, only when unique in BOTH directions
  5. residuals: honest exceptions with ranked nearest-miss candidates
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from recon import schemas as S
from recon.normalize import (
    extract_tokens, hamming_le_1, is_utr_shaped, overlap_suffix_prefix,
    parse_bank_date, parse_iso_date,
)

WINDOW_DAYS = 10
BANK_CHARGE_PAISE = 2_950   # NEFT/RTGS charge incl. GST — known deduction

RAZORPAY_MARKERS = ("RAZORPAY", "RZPAY", "RZP")


# --- Working records ----------------------------------------------------------

@dataclass
class _Credit:
    row: S.BankRow
    value_date: date
    tokens: list[str]
    full_strip: str
    shaped: bool

    @property
    def txn_id(self) -> str:
        return self.row.txn_id

    @property
    def amount(self) -> int:
        return self.row.credit_paise


@dataclass
class _Setl:
    rec: S.Settlement
    base_date: date            # settlement batch date; window anchor

    @property
    def sid(self) -> str:
        return self.rec.settlement_id

    @property
    def amount(self) -> int:
        return self.rec.amount_paise

    @property
    def utr(self) -> str:
        return self.rec.utr


@dataclass
class _Pending:
    """UTR-certain credit whose amount is not yet explained."""
    setl: _Setl
    credit: _Credit
    evidence: list[dict]


def _in_window(setl: _Setl, credit: _Credit, extra: int = 0) -> bool:
    delta = (credit.value_date - setl.base_date).days
    return 0 <= delta <= WINDOW_DAYS + extra


def _decompose(expected: int, received: int) -> list[dict] | None:
    """Try to explain received - expected via known deduction rules."""
    disc = received - expected
    if disc == -BANK_CHARGE_PAISE:
        return [{"label": "bank_neft_rtgs_charge_incl_gst", "amount_paise": disc}]
    tds = (expected + 50) // 100  # 1% of net, round half up
    if disc == -tds:
        return [{"label": "tds_1pct_of_net", "amount_paise": disc}]
    return None


class _State:
    def __init__(self, settlements: list[_Setl], credits: list[_Credit]):
        self.settlements = settlements
        self.credits = credits
        self.claimed_s: set[str] = set()
        self.claimed_c: set[str] = set()
        self.decisions: list[S.Decision] = []
        self.pending: list[_Pending] = []

    def open_settlements(self) -> list[_Setl]:
        return [s for s in self.settlements if s.sid not in self.claimed_s]

    def open_credits(self, shaped_only: bool = False) -> list[_Credit]:
        return [c for c in self.credits if c.txn_id not in self.claimed_c
                and (c.shaped or not shaped_only)]

    def emit(self, d: S.Decision) -> None:
        self.decisions.append(d)
        self.claimed_s.update(d.settlement_ids)
        self.claimed_c.update(d.bank_txn_ids)


# --- Pass 0: scope + extraction -----------------------------------------------

def _prepare(settlements: list[S.Settlement], bank_rows: list[S.BankRow]
             ) -> tuple[list[_Setl], list[_Credit], list[S.BankRow]]:
    setls = [_Setl(rec=s, base_date=parse_iso_date(s.created_at)) for s in settlements]
    credits: list[_Credit] = []
    debits: list[S.BankRow] = []
    for row in bank_rows:
        if row.credit_paise <= 0:
            debits.append(row)
            continue
        tokens, full = extract_tokens(row.narration, row.ref_no)
        blob = (row.narration + " " + row.ref_no).upper()
        shaped = any(m in blob for m in RAZORPAY_MARKERS) or any(
            is_utr_shaped(t) for t in tokens)
        credits.append(_Credit(row=row, value_date=parse_bank_date(row.value_date),
                               tokens=tokens, full_strip=full, shaped=shaped))
    return setls, credits, debits


# --- Pass 1: exact UTR --------------------------------------------------------

def _exact_utr_refs(setls: list[_Setl], credits: list[_Credit]
                    ) -> dict[str, list[_Credit]]:
    """settlement_id -> credits whose narration/ref carries its UTR verbatim
    (as a discrete token, or embedded with separators in the full strip)."""
    refs: dict[str, list[_Credit]] = {}
    for s in setls:
        for c in credits:
            if s.utr in c.tokens or s.utr in c.full_strip:
                refs.setdefault(s.sid, []).append(c)
    return refs


def _pass1_exact_utr(st: _State) -> None:
    refs = _exact_utr_refs(st.open_settlements(), st.open_credits())
    by_id = {s.sid: s for s in st.settlements}
    for sid, cs in refs.items():
        s = by_id[sid]
        ev_base = [{"rule": "utr_exact",
                    "detail": f"UTR {s.utr} present in bank narration/ref"}]
        late = [c for c in cs if not _in_window(s, c)]
        late_ev = ([{"rule": "late_settlement",
                     "detail": "credit landed outside the usual T+2 window; "
                               "UTR identity overrides the window"}]
                   if late else [])

        if len(cs) == 1:
            c = cs[0]
            if c.amount == s.amount:
                st.emit(S.Decision(
                    leg="A", kind="match", status=S.MATCHED,
                    settlement_ids=[sid], bank_txn_ids=[c.txn_id],
                    confidence=S.CONF_EXACT,
                    expected_paise=s.amount, received_paise=c.amount,
                    discrepancy_paise=0,
                    evidence=ev_base + [{"rule": "amount_exact",
                                         "detail": f"{c.amount} == {s.amount}"}] + late_ev,
                    pass_name="pass1_exact_utr"))
            else:
                breakdown = _decompose(s.amount, c.amount)
                if breakdown is not None:
                    st.emit(S.Decision(
                        leg="A", kind="match", status=S.MATCHED_WITH_DISCREPANCY,
                        settlement_ids=[sid], bank_txn_ids=[c.txn_id],
                        confidence=S.CONF_HIGH,
                        expected_paise=s.amount, received_paise=c.amount,
                        discrepancy_paise=c.amount - s.amount,
                        discrepancy_breakdown=breakdown,
                        evidence=ev_base + [{"rule": "decomposed_discrepancy",
                                             "detail": breakdown[0]["label"]}] + late_ev,
                        pass_name="pass1_exact_utr"))
                else:
                    st.pending.append(_Pending(s, c, ev_base))
            continue

        # multiple credits reference this settlement's UTR
        cs_sorted = sorted(cs, key=lambda c: (c.value_date, c.txn_id))
        amounts = [c.amount for c in cs_sorted]
        if all(a == s.amount for a in amounts):
            first, rest = cs_sorted[0], cs_sorted[1:]
            st.emit(S.Decision(
                leg="A", kind="match", status=S.MATCHED,
                settlement_ids=[sid], bank_txn_ids=[first.txn_id],
                confidence=S.CONF_EXACT,
                expected_paise=s.amount, received_paise=first.amount,
                discrepancy_paise=0,
                evidence=ev_base + [
                    {"rule": "amount_exact", "detail": f"{first.amount} == {s.amount}"},
                    {"rule": "earliest_value_date",
                     "detail": f"{len(rest)} later credit(s) carry the same UTR "
                               "and amount; flagged as duplicates"}],
                pass_name="pass1_exact_utr"))
            for c in rest:
                st.emit(S.Decision(
                    leg="A", kind="exception", status=S.DUPLICATE_CREDIT,
                    bank_txn_ids=[c.txn_id],
                    received_paise=c.amount,
                    evidence=ev_base + [{"rule": "duplicate_posting",
                                         "detail": f"same UTR and amount as {first.txn_id}, "
                                                   f"posted {c.value_date.isoformat()}"}],
                    candidates=[{"record_id": sid,
                                 "amount_paise": s.amount,
                                 "reason": "settlement already fully covered by "
                                           f"{first.txn_id}; this posting repeats it"}],
                    pass_name="pass1_exact_utr"))
        elif sum(amounts) == s.amount:
            st.emit(S.Decision(
                leg="A", kind="match", status=S.MATCHED_SPLIT,
                settlement_ids=[sid], bank_txn_ids=[c.txn_id for c in cs_sorted],
                confidence=S.CONF_HIGH,
                expected_paise=s.amount, received_paise=sum(amounts),
                discrepancy_paise=0,
                evidence=ev_base + [{"rule": "sum_exact_parts",
                                     "detail": f"{len(cs_sorted)} credits share the UTR "
                                               f"and sum exactly to {s.amount}"}],
                pass_name="pass1_exact_utr"))
        else:
            for c in cs_sorted:
                st.emit(S.Decision(
                    leg="A", kind="exception", status=S.AMBIGUOUS_ABSTAIN,
                    bank_txn_ids=[c.txn_id],
                    received_paise=c.amount,
                    evidence=ev_base,
                    candidates=[{"record_id": sid, "amount_paise": s.amount,
                                 "reason": "multiple credits share this UTR but "
                                           "amounts neither equal nor sum to the net"}],
                    pass_name="pass1_exact_utr"))


# --- Pass 2: fuzzy UTR + exact amount ----------------------------------------

def _fuzzy_hit(utr: str, credit: _Credit) -> str | None:
    for t in credit.tokens:
        if t != utr and overlap_suffix_prefix(t, utr):
            return f"token {t} is a {len(t)}-char fragment of UTR {utr}"
        if is_utr_shaped(t) and t != utr and hamming_le_1(t, utr):
            return f"token {t} differs from UTR {utr} by one character"
    return None


def _pass2_fuzzy_utr(st: _State) -> None:
    for s in st.open_settlements():
        hits: list[tuple[_Credit, str]] = []
        for c in st.open_credits(shaped_only=True):
            why = _fuzzy_hit(s.utr, c)
            if why is not None:
                hits.append((c, why))
        exact_amount = [(c, why) for c, why in hits if c.amount == s.amount]
        if len(exact_amount) == 1:
            c, why = exact_amount[0]
            st.emit(S.Decision(
                leg="A", kind="match", status=S.MATCHED,
                settlement_ids=[s.sid], bank_txn_ids=[c.txn_id],
                confidence=S.CONF_HIGH,
                expected_paise=s.amount, received_paise=c.amount,
                discrepancy_paise=0,
                evidence=[{"rule": "utr_fuzzy", "detail": why},
                          {"rule": "amount_exact",
                           "detail": f"{c.amount} == {s.amount} corroborates the "
                                     "damaged reference"}],
                pass_name="pass2_fuzzy_utr"))
        # fuzzy without exact amount: never matched — deliberately.


# --- Pass 3: merge resolution -------------------------------------------------

def _pass3_merges(st: _State) -> None:
    still_pending: list[_Pending] = []
    for p in st.pending:
        if p.credit.txn_id in st.claimed_c or p.setl.sid in st.claimed_s:
            continue
        residual = p.credit.amount - p.setl.amount
        partners = [s for s in st.open_settlements()
                    if s.sid != p.setl.sid and s.amount == residual
                    and _in_window(s, p.credit, extra=3)]
        if residual > 0 and len(partners) == 1:
            partner = partners[0]
            st.emit(S.Decision(
                leg="A", kind="match", status=S.MATCHED_MERGED,
                settlement_ids=[p.setl.sid, partner.sid],
                bank_txn_ids=[p.credit.txn_id],
                confidence=S.CONF_HIGH,
                expected_paise=p.setl.amount + partner.amount,
                received_paise=p.credit.amount,
                discrepancy_paise=0,
                evidence=p.evidence + [
                    {"rule": "merge_residual_exact",
                     "detail": f"credit {p.credit.amount} = {p.setl.amount} "
                               f"({p.setl.sid}, UTR match) + {partner.amount} "
                               f"({partner.sid}, exact residual)"}],
                pass_name="pass3_merge"))
        elif residual > 0 and len(partners) > 1:
            st.emit(S.Decision(
                leg="A", kind="exception", status=S.AMBIGUOUS_ABSTAIN,
                bank_txn_ids=[p.credit.txn_id],
                received_paise=p.credit.amount,
                evidence=p.evidence,
                candidates=[{"record_id": x.sid, "amount_paise": x.amount,
                             "reason": "residual after the UTR-certain portion "
                                       "matches this settlement's net exactly"}
                            for x in [p.setl] + partners],
                pass_name="pass3_merge"))
        else:
            still_pending.append(p)
    st.pending = still_pending


# --- Pass 4: unique exact amount + window -------------------------------------

def _pass4_amount_unique(st: _State) -> None:
    """Exact-amount matching, safe only when unique in BOTH directions.

    Candidate edges (settlement <-> credit with equal paise inside the window)
    are computed once over the open records. A pair is matched only if each end
    has exactly one edge. EVERY other edge-participating record — including
    rivals whose own edge got consumed by a tie — abstains with its candidate
    set spelled out. Records without edges fall through to pass 5."""
    setls = st.open_settlements()
    credits = st.open_credits(shaped_only=True)
    s_cands: dict[str, list[_Credit]] = {}
    c_cands: dict[str, list[_Setl]] = {}
    for s in setls:
        for c in credits:
            if c.amount == s.amount and _in_window(s, c):
                s_cands.setdefault(s.sid, []).append(c)
                c_cands.setdefault(c.txn_id, []).append(s)

    for s in setls:
        cands = s_cands.get(s.sid, [])
        if len(cands) == 1 and len(c_cands[cands[0].txn_id]) == 1:
            c = cands[0]
            st.emit(S.Decision(
                leg="A", kind="match", status=S.MATCHED,
                settlement_ids=[s.sid], bank_txn_ids=[c.txn_id],
                confidence=S.CONF_MEDIUM,
                expected_paise=s.amount, received_paise=c.amount,
                discrepancy_paise=0,
                evidence=[
                    {"rule": "amount_exact_unique",
                     "detail": f"{c.amount} paise matches exactly one open "
                               "settlement and exactly one open credit"},
                    {"rule": "window",
                     "detail": f"value date {c.value_date.isoformat()} within "
                               f"{WINDOW_DAYS} days of batch {s.base_date.isoformat()}"}],
                pass_name="pass4_amount_unique"))

    # Everything still open that participates in an edge is part of a tie.
    for s in setls:
        if s.sid in st.claimed_s or s.sid not in s_cands:
            continue
        cands = s_cands[s.sid]
        rivals = sorted({x.sid for c in cands for x in c_cands[c.txn_id]} - {s.sid})
        st.emit(S.Decision(
            leg="A", kind="exception", status=S.AMBIGUOUS_ABSTAIN,
            settlement_ids=[s.sid],
            expected_paise=s.amount,
            evidence=[{"rule": "amount_tie",
                       "detail": "amount evidence alone cannot separate the "
                                 "candidates; no reference evidence available"}],
            candidates=(
                [{"record_id": c.txn_id, "amount_paise": c.amount,
                  "reason": f"credit on {c.value_date.isoformat()} matches the "
                            f"net exactly but {len(c_cands[c.txn_id])} settlements "
                            "claim the same amount"} for c in cands]
                + [{"record_id": rid, "amount_paise": s.amount,
                    "reason": "rival settlement with identical net and "
                              "overlapping window"} for rid in rivals]),
            pass_name="pass4_amount_unique"))
    for c in credits:
        if c.txn_id in st.claimed_c or c.txn_id not in c_cands:
            continue
        st.emit(S.Decision(
            leg="A", kind="exception", status=S.AMBIGUOUS_ABSTAIN,
            bank_txn_ids=[c.txn_id],
            received_paise=c.amount,
            evidence=[{"rule": "amount_tie",
                       "detail": "two or more settlements share this exact "
                                 "amount in the window; the narration carries "
                                 "no reference"}],
            candidates=[{"record_id": x.sid, "amount_paise": x.amount,
                         "reason": "identical net, overlapping window, no "
                                   "distinguishing evidence"}
                        for x in c_cands[c.txn_id]],
            pass_name="pass4_amount_unique"))


# --- Pass 5: residuals --------------------------------------------------------

def _nearest(amount: int, pool: list, key, n: int = 3) -> list:
    return sorted(pool, key=lambda x: abs(key(x) - amount))[:n]


def _pass5_residuals(st: _State, debits: list[S.BankRow]) -> None:
    # UTR-certain but unexplained amounts that no merge could resolve
    for p in st.pending:
        if p.credit.txn_id in st.claimed_c or p.setl.sid in st.claimed_s:
            continue
        disc = p.credit.amount - p.setl.amount
        st.emit(S.Decision(
            leg="A", kind="match", status=S.MATCHED_WITH_DISCREPANCY,
            settlement_ids=[p.setl.sid], bank_txn_ids=[p.credit.txn_id],
            confidence=S.CONF_NEEDS_REVIEW,
            expected_paise=p.setl.amount, received_paise=p.credit.amount,
            discrepancy_paise=disc,
            discrepancy_breakdown=[{"label": "unexplained", "amount_paise": disc}],
            evidence=p.evidence + [{"rule": "unexplained_residual",
                                    "detail": f"UTR identity is certain but {disc} "
                                              "paise cannot be decomposed"}],
            pass_name="pass5_residuals"))

    # Snapshot both sides BEFORE emitting residual exceptions: emitting marks
    # records claimed, and exceptions on one side must still be able to cite
    # nearest-miss candidates from the other.
    open_credits = st.open_credits(shaped_only=True)
    open_setls = st.open_settlements()
    for s in open_setls:
        near = _nearest(s.amount, [c for c in open_credits
                                   if c.txn_id not in st.claimed_c and _in_window(s, c)],
                        key=lambda c: c.amount)
        st.emit(S.Decision(
            leg="A", kind="exception", status=S.EXCEPTION_MISSING_BANK,
            settlement_ids=[s.sid],
            expected_paise=s.amount,
            evidence=[{"rule": "no_credit_found",
                       "detail": f"no bank credit carries UTR {s.utr}, and no "
                                 "amount-certain candidate exists in the window"}],
            candidates=[{"record_id": c.txn_id, "amount_paise": c.amount,
                         "reason": f"differs by {abs(c.amount - s.amount)} paise"
                                   + ("" if c.tokens else "; no reference in narration")}
                        for c in near] or
                       [{"record_id": "", "amount_paise": 0,
                         "reason": "no unclaimed settlement-shaped credit in window"}],
            pass_name="pass5_residuals"))

    for c in st.open_credits():
        if c.txn_id in st.claimed_c:
            continue
        if not c.shaped:
            st.emit(S.Decision(
                leg="A", kind="exception", status=S.NON_SETTLEMENT_CREDIT,
                bank_txn_ids=[c.txn_id],
                received_paise=c.amount,
                evidence=[{"rule": "not_settlement_shaped",
                           "detail": "narration carries no PSP marker and no "
                                     "UTR-shaped reference"}],
                pass_name="pass5_residuals"))
            continue
        near = _nearest(c.amount, open_setls, key=lambda s: s.amount)
        st.emit(S.Decision(
            leg="A", kind="exception", status=S.EXCEPTION_MISSING_SETTLEMENT,
            bank_txn_ids=[c.txn_id],
            received_paise=c.amount,
            evidence=[{"rule": "no_settlement_found",
                       "detail": "credit looks like a PSP settlement but its "
                                 "reference matches no known settlement"}],
            candidates=[{"record_id": s.sid, "amount_paise": s.amount,
                         "reason": f"differs by {abs(s.amount - c.amount)} paise; "
                                   "UTR does not match"}
                        for s in near] or
                       [{"record_id": "", "amount_paise": 0,
                         "reason": "no open settlement to compare against"}],
            pass_name="pass5_residuals"))

    for row in debits:
        st.emit(S.Decision(
            leg="A", kind="out_of_scope", status=S.OUT_OF_SCOPE,
            bank_txn_ids=[row.txn_id],
            evidence=[{"rule": "debit", "detail": "leg A reconciles credits only"}],
            pass_name="pass0_scope"))


# --- Entry point --------------------------------------------------------------

def reconcile_leg_a(settlements: list[S.Settlement],
                    bank_rows: list[S.BankRow]) -> list[S.Decision]:
    setls, credits, debits = _prepare(settlements, bank_rows)
    st = _State(setls, credits)
    _pass1_exact_utr(st)
    _pass2_fuzzy_utr(st)
    _pass3_merges(st)
    _pass4_amount_unique(st)
    _pass5_residuals(st, debits)
    return st.decisions
