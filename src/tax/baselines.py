"""The naive tax matcher — the benchmark's hardness canary.

Matches by amount proximity alone (within Re 1), first-come-first-served,
ignoring vendor identity, invoice references, tax heads, periods, quarters,
registry eligibility and statutory deadlines. This is the strategy every
"just compare the numbers" spreadsheet implements — the benchmark exists to
show exactly where it fails: it claims blocked and unknown credits, misses
every deferral and head error, and calls a short-paid liability compliant.
"""

from __future__ import annotations

from tax import schemas as TS
from tax.io_tax import TaxInput

_TOL = 100   # Re 1


def naive_tax(inp: TaxInput) -> list[TS.TaxDecision]:
    decisions: list[TS.TaxDecision] = []

    # Loop 1: claim the first purchase within Re 1 of each filed line.
    open_p = list(inp.purchases)
    for ln in inp.gstr2b:
        hit = next((p for p in open_p
                    if abs(p.gst_paise - ln["gst_paise"]) <= _TOL), None)
        if hit is None:
            decisions.append(TS.TaxDecision(
                loop="itc", kind="exception",
                status=TS.UNKNOWN_INVOICE_IN_2B,
                book_ids=[], filed_ids=[ln["line_id"]],
                books_paise=0, filed_paise=ln["gst_paise"],
                discrepancy_paise=0,
                evidence=[{"rule": "amount_tolerance",
                           "detail": "no purchase within Re 1"}],
                pass_name="naive"))
            continue
        open_p.remove(hit)
        decisions.append(TS.TaxDecision(
            loop="itc", kind="match", status=TS.ITC_MATCHED,
            book_ids=[hit.purchase_id], filed_ids=[ln["line_id"]],
            confidence=TS.CONF_HIGH,
            books_paise=hit.gst_paise, filed_paise=ln["gst_paise"],
            discrepancy_paise=ln["gst_paise"] - hit.gst_paise,
            evidence=[{"rule": "amount_tolerance",
                       "detail": "gst within Re 1"}],
            pass_name="naive"))
    for p in open_p:
        decisions.append(TS.TaxDecision(
            loop="itc", kind="exception", status=TS.ITC_MISSING_IN_2B,
            book_ids=[p.purchase_id], filed_ids=[],
            books_paise=p.gst_paise, filed_paise=0,
            discrepancy_paise=-p.gst_paise,
            evidence=[{"rule": "amount_tolerance",
                       "detail": "no filed line within Re 1"}],
            pass_name="naive"))

    # Loop 2: same idea against 26AS.
    open_e = list(inp.tds_events)
    for en in inp.form26as:
        hit = next((e for e in open_e
                    if abs(e.tds_paise - en["tds_paise"]) <= _TOL), None)
        if hit is None:
            decisions.append(TS.TaxDecision(
                loop="tds", kind="exception", status=TS.TDS_UNKNOWN_ENTRY,
                book_ids=[], filed_ids=[en["entry_id"]],
                books_paise=0, filed_paise=en["tds_paise"],
                discrepancy_paise=0, pass_name="naive"))
            continue
        open_e.remove(hit)
        decisions.append(TS.TaxDecision(
            loop="tds", kind="match", status=TS.TDS_CREDIT_MATCHED,
            book_ids=[hit.settlement_id], filed_ids=[en["entry_id"]],
            confidence=TS.CONF_HIGH,
            books_paise=hit.tds_paise, filed_paise=en["tds_paise"],
            discrepancy_paise=en["tds_paise"] - hit.tds_paise,
            pass_name="naive"))
    for e in open_e:
        decisions.append(TS.TaxDecision(
            loop="tds", kind="exception", status=TS.TDS_MISSING_IN_26AS,
            book_ids=[e.settlement_id], filed_ids=[],
            books_paise=e.tds_paise, filed_paise=0,
            discrepancy_paise=-e.tds_paise, pass_name="naive"))

    # Loop 3: a payment debit in the month means all is well.
    for pc in inp.periods:
        rid = f"{pc.kind}:{pc.period}"
        if pc.paid_paise is not None:
            decisions.append(TS.TaxDecision(
                loop="obligation", kind="info", status=TS.PAID_ON_TIME,
                book_ids=[rid], filed_ids=[pc.paid_txn_id],
                books_paise=pc.paid_paise, filed_paise=pc.paid_paise,
                discrepancy_paise=0,
                evidence=[{"rule": "debit_present",
                           "detail": "a payment debit exists this month"}],
                pass_name="naive"))
        else:
            decisions.append(TS.TaxDecision(
                loop="obligation", kind="exception", status=TS.NOT_PAID,
                book_ids=[rid], filed_ids=[],
                books_paise=None, filed_paise=None, discrepancy_paise=0,
                pass_name="naive"))
    return decisions


BASELINES = {"naive_tax": naive_tax}
