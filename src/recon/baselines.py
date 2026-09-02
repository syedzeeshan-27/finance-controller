"""Baseline matcher, graded by the exact same benchmark as the engine.

- naive: what a first-pass script does — amount within Rs 1, date within 3
  days, first come first served. The benchmark's leak canary: if this scores
  close to the engine, the world got too easy.

It consumes the same loaders/normalizer as the engine, so parsing is never
the differentiator.
"""

from __future__ import annotations

from recon import schemas as S
from recon.normalize import parse_bank_date, parse_iso_date


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


STRATEGIES = {
    "naive": naive_reconcile,
}
