"""Baseline matchers, graded by the exact same benchmark as the engine.

- naive: what a first-pass script does — amount within Rs 1, date within 3
  days, first come first served. The benchmark's leak canary: if this scores
  close to the engine, the world got too easy.
- utr_then_amount: what a competent script does — the settlement's UTR
  verbatim in the narration or reference first, then exact-paise amount
  within 10 days of the settlement's creation, first come first served. No
  fuzzy UTRs, no merges, no deduction rules, no abstention on ties. A UTR
  match whose amounts differ is left for review, as the engine does.

It consumes the same loaders/normalizer as the engine, so parsing is never
the differentiator.
"""

from __future__ import annotations

from recon import schemas as S
from recon.normalize import parse_bank_date, parse_iso_date

UTR_BASELINE_WINDOW_DAYS = 10


def _credits_and_debits(bank_rows: list[S.BankRow]):
    credits = [r for r in bank_rows if r.credit_paise > 0]
    debits = [r for r in bank_rows if r.credit_paise <= 0]
    return credits, debits


def _emit_debits(decisions: list[S.Decision], debits: list[S.BankRow]) -> None:
    for row in debits:
        decisions.append(S.Decision(
            leg="A", kind="out_of_scope", status=S.OUT_OF_SCOPE,
            bank_txn_ids=[row.txn_id], pass_name="scope"))


def naive_reconcile(settlements: list[S.Settlement],
                    bank_rows: list[S.BankRow]) -> list[S.Decision]:
    """Amount within Rs 1, value date within 3 days of expected settlement,
    first come first served."""
    TOLERANCE = 100     # paise
    DAY_WINDOW = 3
    credits, debits = _credits_and_debits(bank_rows)
    open_setls = {s.settlement_id: (s, parse_iso_date(s.settled_at)) for s in settlements}
    decisions: list[S.Decision] = []

    for row in credits:
        vdate = parse_bank_date(row.value_date)
        best = None
        for sid, (s, sdate) in open_setls.items():
            if abs(row.credit_paise - s.amount_paise) <= TOLERANCE \
                    and abs((vdate - sdate).days) <= DAY_WINDOW:
                gap = abs(row.credit_paise - s.amount_paise)
                if best is None or gap < best[1]:
                    best = (sid, gap)
        if best is not None:
            sid = best[0]
            s = open_setls.pop(sid)[0]
            decisions.append(S.Decision(
                leg="A", kind="match", status=S.MATCHED,
                settlement_ids=[sid], bank_txn_ids=[row.txn_id],
                confidence=S.CONF_MEDIUM,
                expected_paise=s.amount_paise, received_paise=row.credit_paise,
                discrepancy_paise=row.credit_paise - s.amount_paise,
                evidence=[{"rule": "amount_within_rs1_and_3_days",
                           "detail": "naive tolerance match"}],
                pass_name="naive"))
        else:
            decisions.append(S.Decision(
                leg="A", kind="exception", status=S.EXCEPTION_MISSING_SETTLEMENT,
                bank_txn_ids=[row.txn_id], received_paise=row.credit_paise,
                evidence=[{"rule": "no_candidate", "detail": "naive found nothing"}],
                candidates=[{"record_id": "", "amount_paise": 0,
                             "reason": "naive matcher keeps no candidate detail"}],
                pass_name="naive"))

    for sid, (s, _) in open_setls.items():
        decisions.append(S.Decision(
            leg="A", kind="exception", status=S.EXCEPTION_MISSING_BANK,
            settlement_ids=[sid], expected_paise=s.amount_paise,
            evidence=[{"rule": "no_candidate", "detail": "naive found nothing"}],
            candidates=[{"record_id": "", "amount_paise": 0,
                         "reason": "naive matcher keeps no candidate detail"}],
            pass_name="naive"))
    _emit_debits(decisions, debits)
    return decisions


def utr_then_amount(settlements: list[S.Settlement],
                    bank_rows: list[S.BankRow]) -> list[S.Decision]:
    """Exact UTR first, then exact amount in the window, first come first served."""
    credits, debits = _credits_and_debits(bank_rows)
    open_setls = {s.settlement_id: s for s in settlements}
    created = {s.settlement_id: parse_iso_date(s.created_at) for s in settlements}
    decisions: list[S.Decision] = []
    unmatched: list[S.BankRow] = []

    for row in credits:                                   # pass 1: exact UTR
        text = f"{row.narration} {row.ref_no}".upper()
        hit = next((s for s in open_setls.values()
                    if s.utr and s.utr.upper() in text), None)
        if hit is None:
            unmatched.append(row)
            continue
        open_setls.pop(hit.settlement_id)
        disc = row.credit_paise - hit.amount_paise
        decisions.append(S.Decision(
            leg="A", kind="match",
            status=S.MATCHED if disc == 0 else S.MATCHED_WITH_DISCREPANCY,
            settlement_ids=[hit.settlement_id], bank_txn_ids=[row.txn_id],
            confidence=S.CONF_EXACT if disc == 0 else S.CONF_NEEDS_REVIEW,
            expected_paise=hit.amount_paise, received_paise=row.credit_paise,
            discrepancy_paise=disc,
            discrepancy_breakdown=([] if disc == 0 else
                                   [{"label": "unexplained", "amount_paise": disc}]),
            evidence=[{"rule": "utr_exact",
                       "detail": f"UTR {hit.utr} in narration/ref"}],
            pass_name="utr_then_amount"))

    for row in unmatched:                                 # pass 2: exact amount
        vdate = parse_bank_date(row.value_date)
        cands = sorted((s for s in open_setls.values()
                        if s.amount_paise == row.credit_paise
                        and 0 <= (vdate - created[s.settlement_id]).days
                        <= UTR_BASELINE_WINDOW_DAYS),
                       key=lambda s: (created[s.settlement_id], s.settlement_id))
        if cands:
            s = open_setls.pop(cands[0].settlement_id)
            decisions.append(S.Decision(
                leg="A", kind="match", status=S.MATCHED,
                settlement_ids=[s.settlement_id], bank_txn_ids=[row.txn_id],
                confidence=S.CONF_MEDIUM,
                expected_paise=s.amount_paise, received_paise=row.credit_paise,
                discrepancy_paise=0,
                evidence=[{"rule": "amount_exact_first_come",
                           "detail": "exact amount in the window; first "
                                     "candidate taken, ties not checked"}],
                pass_name="utr_then_amount"))
        else:
            decisions.append(S.Decision(
                leg="A", kind="exception", status=S.EXCEPTION_MISSING_SETTLEMENT,
                bank_txn_ids=[row.txn_id], received_paise=row.credit_paise,
                evidence=[{"rule": "no_candidate", "detail": "no UTR, no exact amount"}],
                candidates=[{"record_id": "", "amount_paise": 0,
                             "reason": "baseline keeps no candidate detail"}],
                pass_name="utr_then_amount"))

    for sid, s in open_setls.items():
        decisions.append(S.Decision(
            leg="A", kind="exception", status=S.EXCEPTION_MISSING_BANK,
            settlement_ids=[sid], expected_paise=s.amount_paise,
            evidence=[{"rule": "no_candidate", "detail": "no UTR, no exact amount"}],
            candidates=[{"record_id": "", "amount_paise": 0,
                         "reason": "baseline keeps no candidate detail"}],
            pass_name="utr_then_amount"))
    _emit_debits(decisions, debits)
    return decisions


STRATEGIES = {
    "naive": naive_reconcile,
    "utr_then_amount": utr_then_amount,
}
