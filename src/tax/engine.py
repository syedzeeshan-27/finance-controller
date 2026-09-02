"""The tax-matching engine: three deterministic, evidence-scored loops.

Loop 1 (input GST): merchant-books purchases vs GSTR-2B lines, matched by
invoice identity first, amounts as corroboration, singleton pairing last —
never by "close enough". Loop 2 (TDS credits): deductions observed by the
Stage 1 reconciliation vs Form 26AS entries. Loop 3 (compliance): recomputed
statutory liabilities vs the actual payment debits.

Later passes see only records earlier passes left unclaimed, mirroring the
reconciliation engine. Residuals are honest exceptions with candidates and
per-candidate rejection reasons, never forced matches.
"""

from __future__ import annotations

from tax import registry as TR
from tax import rules as TX
from tax import schemas as TS
from tax.books import PeriodCheck, Purchase, TdsEvent
from tax.io_tax import TaxInput


def _line_ref(line: dict) -> str | None:
    _, _, ref = line["invoice_no"].rpartition("/")
    return ref if ref.isdigit() else None


def _line_vendor_key(line: dict) -> str | None:
    v = TR.VENDORS_BY_GSTIN.get(line["gstin"])
    return v.key if v else None


def _hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


class _Loop1State:
    def __init__(self, purchases: list[Purchase], lines: list[dict]):
        self.purchases = purchases
        self.lines = lines
        self.claimed_p: set[str] = set()
        self.claimed_l: set[str] = set()
        self.matched_line_of: dict[str, str] = {}   # line_id -> purchase_id
        self.decisions: list[TS.TaxDecision] = []

    def open_purchases(self) -> list[Purchase]:
        return [p for p in self.purchases
                if p.purchase_id not in self.claimed_p]

    def open_lines(self) -> list[dict]:
        return [ln for ln in self.lines
                if ln["line_id"] not in self.claimed_l]

    def emit(self, d: TS.TaxDecision) -> None:
        self.decisions.append(d)
        self.claimed_p.update(d.book_ids)
        self.claimed_l.update(d.filed_ids)


def _decide_pair(st: _Loop1State, p: Purchase, line: dict, pass_name: str,
                 evidence: list[dict], base_conf: str) -> None:
    """Identity is established; classify the pairing and emit."""
    ev = list(evidence)
    if not p.itc_eligible:
        st.emit(TS.TaxDecision(
            loop="itc", kind="match", status=TS.BLOCKED_CREDIT_NO_ITC,
            book_ids=[p.purchase_id], filed_ids=[line["line_id"]],
            confidence=base_conf, books_paise=p.gst_paise,
            filed_paise=line["gst_paise"], discrepancy_paise=0,
            evidence=ev + [{"rule": "registry_blocked_credit",
                            "detail": p.no_itc_reason}],
            pass_name=pass_name,
            explanation="vendor filed this line, but Section 17(5) blocks the "
                        "credit — do not claim"))
        st.matched_line_of[line["line_id"]] = p.purchase_id
        return

    amounts_equal = (line["taxable_value_paise"] == p.taxable_paise
                     and line["gst_paise"] == p.gst_paise)
    disc = line["gst_paise"] - p.gst_paise
    if not amounts_equal:
        label = ("vendor_filed_short" if disc < 0 else "vendor_filed_excess"
                 if disc > 0 else "taxable_value_differs")
        st.emit(TS.TaxDecision(
            loop="itc", kind="match", status=TS.ITC_AMOUNT_MISMATCH,
            book_ids=[p.purchase_id], filed_ids=[line["line_id"]],
            confidence=TS.CONF_HIGH, books_paise=p.gst_paise,
            filed_paise=line["gst_paise"], discrepancy_paise=disc,
            discrepancy_breakdown=[{"label": label, "amount_paise": disc}],
            evidence=ev + [{"rule": "amounts_differ",
                            "detail": f"books gst {p.gst_paise}, "
                                      f"filed {line['gst_paise']}"}],
            pass_name=pass_name,
            explanation="invoice identity is certain but the vendor filed a "
                        "different amount; claim the lower figure and chase "
                        "the difference"))
        st.matched_line_of[line["line_id"]] = p.purchase_id
        return

    if line["tax_head"] != p.expected_head:
        st.emit(TS.TaxDecision(
            loop="itc", kind="match", status=TS.ITC_HEAD_MISMATCH,
            book_ids=[p.purchase_id], filed_ids=[line["line_id"]],
            confidence=TS.CONF_HIGH, books_paise=p.gst_paise,
            filed_paise=line["gst_paise"], discrepancy_paise=0,
            evidence=ev + [{"rule": "tax_head_differs",
                            "detail": f"expected {p.expected_head}, "
                                      f"filed {line['tax_head']}"}],
            pass_name=pass_name,
            explanation="amount is right but the vendor filed the wrong tax "
                        "head; credit needs a head correction, not a chase"))
        st.matched_line_of[line["line_id"]] = p.purchase_id
        return

    if line["period"] == TX.next_period(p.period):
        st.emit(TS.TaxDecision(
            loop="itc", kind="match", status=TS.ITC_DEFERRED_NEXT_PERIOD,
            book_ids=[p.purchase_id], filed_ids=[line["line_id"]],
            confidence=TS.CONF_HIGH, books_paise=p.gst_paise,
            filed_paise=line["gst_paise"], discrepancy_paise=0,
            evidence=ev + [{"rule": "filed_next_period",
                            "detail": f"purchase {p.period}, "
                                      f"filed {line['period']}"}],
            pass_name=pass_name,
            explanation="vendor filed one period late; the credit is safe "
                        "but claimable only next period"))
        st.matched_line_of[line["line_id"]] = p.purchase_id
        return

    conf = base_conf if line["period"] == p.period else TS.CONF_NEEDS_REVIEW
    st.emit(TS.TaxDecision(
        loop="itc", kind="match", status=TS.ITC_MATCHED,
        book_ids=[p.purchase_id], filed_ids=[line["line_id"]],
        confidence=conf, books_paise=p.gst_paise,
        filed_paise=line["gst_paise"], discrepancy_paise=0,
        evidence=ev + [{"rule": "amount_exact",
                        "detail": f"taxable and gst equal to the paisa "
                                  f"({p.gst_paise})"}],
        pass_name=pass_name,
        explanation="filed line matches the books entry exactly"))
    st.matched_line_of[line["line_id"]] = p.purchase_id


def _line_signature(line: dict) -> tuple:
    return (line["period"], line["invoice_no"], line["taxable_value_paise"],
            line["gst_paise"], line["tax_head"])


def _pass1_invoice_exact(st: _Loop1State) -> None:
    groups: dict[tuple[str, str], tuple[list[Purchase], list[dict]]] = {}
    for p in st.open_purchases():
        if p.ref is not None:
            groups.setdefault((p.vendor_key, p.ref), ([], []))[0].append(p)
    for ln in st.open_lines():
        key = (_line_vendor_key(ln), _line_ref(ln))
        if key in groups:
            groups[key][1].append(ln)

    ev = [{"rule": "invoice_exact",
           "detail": "invoice reference identical on both sides"}]
    for (vkey, ref), (ps, lns) in sorted(groups.items()):
        if not lns:
            continue
        # Verbatim re-filings collapse onto their earliest copy; the copies
        # become duplicate verdicts once the original is decided.
        lns.sort(key=lambda ln: ln["line_id"])
        reps: list[dict] = []
        dups: list[tuple[dict, dict]] = []
        seen: dict[tuple, dict] = {}
        for ln in lns:
            sig = _line_signature(ln)
            if sig in seen:
                dups.append((ln, seen[sig]))
            else:
                seen[sig] = ln
                reps.append(ln)

        if len(ps) == 1 and len(reps) == 1:
            _decide_pair(st, ps[0], reps[0], "invoice_exact", ev,
                         TS.CONF_EXACT)
        else:
            # A natural (vendor, ref) collision: identity alone is ambiguous,
            # so a pairing needs exact-amount corroboration unique on both
            # sides. Anything else stays open for the residual pass.
            by_amt_p: dict[tuple[int, int], list[Purchase]] = {}
            for p in ps:
                by_amt_p.setdefault((p.taxable_paise, p.gst_paise),
                                    []).append(p)
            by_amt_l: dict[tuple[int, int], list[dict]] = {}
            for ln in reps:
                by_amt_l.setdefault((ln["taxable_value_paise"],
                                     ln["gst_paise"]), []).append(ln)
            for amt, cand_p in sorted(by_amt_p.items()):
                cand_l = by_amt_l.get(amt, [])
                if len(cand_p) == 1 and len(cand_l) == 1:
                    _decide_pair(
                        st, cand_p[0], cand_l[0], "invoice_exact", ev + [
                            {"rule": "collision_amount_corroborated",
                             "detail": f"reference {ref} is shared; exact "
                                       f"amount is unique on both sides"}],
                        TS.CONF_HIGH)

        for ln, rep in dups:
            pid = st.matched_line_of.get(rep["line_id"])
            if pid is None:
                continue                     # residual pass will surface it
            st.emit(TS.TaxDecision(
                loop="itc", kind="exception", status=TS.DUPLICATE_2B_LINE,
                book_ids=[pid], filed_ids=[ln["line_id"]],
                confidence=TS.CONF_HIGH, books_paise=0,
                filed_paise=ln["gst_paise"],
                discrepancy_paise=0,
                evidence=[{"rule": "verbatim_refiling",
                           "detail": f"identical to {rep['line_id']}, "
                                     f"already matched"}],
                pass_name="invoice_exact",
                explanation="the same invoice appears twice in GSTR-2B; "
                            "claim it once"))


def _pass2_invoice_fuzzy(st: _Loop1State) -> None:
    """A one-digit-corrupted reference matches ONLY with exact amounts and a
    clean head — a damaged reference alone never matches."""
    open_l = [ln for ln in st.open_lines() if _line_ref(ln) is not None]
    open_p = [p for p in st.open_purchases() if p.ref is not None]
    edges: dict[str, list[Purchase]] = {}
    redges: dict[str, list[dict]] = {}
    for ln in open_l:
        vkey, ref = _line_vendor_key(ln), _line_ref(ln)
        for p in open_p:
            if (p.vendor_key == vkey and _hamming(p.ref, ref) == 1
                    and p.taxable_paise == ln["taxable_value_paise"]
                    and p.gst_paise == ln["gst_paise"]
                    and p.expected_head == ln["tax_head"]):
                edges.setdefault(ln["line_id"], []).append(p)
                redges.setdefault(p.purchase_id, []).append(ln)
    for line_id, cand in sorted(edges.items()):
        if len(cand) != 1 or len(redges[cand[0].purchase_id]) != 1:
            continue                          # ambiguous either way: abstain
        p = cand[0]
        if p.purchase_id in st.claimed_p or line_id in st.claimed_l:
            continue
        ln = next(x for x in open_l if x["line_id"] == line_id)
        _decide_pair(st, p, ln, "invoice_fuzzy", [
            {"rule": "invoice_fuzzy_amount_exact",
             "detail": f"reference one digit off ({p.ref} vs "
                       f"{_line_ref(ln)}); taxable+gst equal to the paisa"}],
            TS.CONF_HIGH)


def _pass3_vendor_period_singleton(st: _Loop1State) -> None:
    """Exactly one open books entry and one open filed line for a (vendor,
    period), amounts equal to the paisa: pair them. This is how the monthly
    fee invoice (which carries no books-side reference) matches. Unequal
    singletons are NOT paired — that shape is indistinguishable from a
    missing filing plus an unknown invoice."""
    by_vp_p: dict[tuple[str, str], list[Purchase]] = {}
    for p in st.open_purchases():
        if p.files_2b and p.itc_eligible:
            by_vp_p.setdefault((p.vendor_key, p.period), []).append(p)
    by_vp_l: dict[tuple[str, str], list[dict]] = {}
    for ln in st.open_lines():
        by_vp_l.setdefault((_line_vendor_key(ln), ln["period"]),
                           []).append(ln)
    for key, ps in sorted(by_vp_p.items()):
        lns = by_vp_l.get(key, [])
        if len(ps) != 1 or len(lns) != 1:
            continue
        p, ln = ps[0], lns[0]
        if (p.taxable_paise == ln["taxable_value_paise"]
                and p.gst_paise == ln["gst_paise"]):
            _decide_pair(st, p, ln, "vendor_period_singleton", [
                {"rule": "vendor_period_singleton",
                 "detail": f"only {p.vendor_key} entry in {p.period} on both "
                           f"sides; amounts equal to the paisa"}],
                TS.CONF_MEDIUM)


def _nearest_candidates(gst: int, pool: list[tuple[str, int]],
                        limit: int = 3) -> list[dict]:
    ranked = sorted(pool, key=lambda t: (abs(t[1] - gst), t[0]))[:limit]
    return [{"id": rid, "gst_paise": amt,
             "rejected_because": f"gst differs by {abs(amt - gst)} paise "
                                 "and no invoice reference links them"}
            for rid, amt in ranked]


def _pass4_residuals(st: _Loop1State) -> None:
    open_lines = st.open_lines()
    open_purchases = st.open_purchases()
    line_pool = [(ln["line_id"], ln["gst_paise"]) for ln in open_lines]
    purchase_pool = [(p.purchase_id, p.gst_paise) for p in open_purchases]

    for p in open_purchases:
        if not p.files_2b:
            st.emit(TS.TaxDecision(
                loop="itc", kind="info", status=TS.NO_ITC_APPLICABLE,
                book_ids=[p.purchase_id], filed_ids=[],
                books_paise=0, filed_paise=0, discrepancy_paise=0,
                evidence=[{"rule": "registry_no_itc",
                           "detail": p.no_itc_reason}],
                pass_name="residual",
                explanation="no input credit exists for this spend; nothing "
                            "to chase in GSTR-2B"))
        elif not p.itc_eligible:
            st.emit(TS.TaxDecision(
                loop="itc", kind="info", status=TS.BLOCKED_CREDIT_NO_ITC,
                book_ids=[p.purchase_id], filed_ids=[],
                books_paise=p.gst_paise, filed_paise=0, discrepancy_paise=0,
                evidence=[{"rule": "registry_blocked_credit",
                           "detail": p.no_itc_reason}],
                pass_name="residual",
                explanation="Section 17(5) blocks this credit regardless of "
                            "the vendor's filing"))
        else:
            st.emit(TS.TaxDecision(
                loop="itc", kind="exception", status=TS.ITC_MISSING_IN_2B,
                book_ids=[p.purchase_id], filed_ids=[],
                books_paise=p.gst_paise, filed_paise=0,
                discrepancy_paise=-p.gst_paise,
                discrepancy_breakdown=[{"label": "itc_at_risk",
                                        "amount_paise": -p.gst_paise}],
                evidence=[{"rule": "no_filed_line",
                           "detail": f"no GSTR-2B line for {p.vendor_key} "
                                     f"matches this purchase"}],
                candidates=_nearest_candidates(p.gst_paise, line_pool),
                pass_name="residual",
                explanation="the vendor has not filed this invoice; the "
                            "input credit is at risk until they do"))

    for ln in open_lines:
        if ln["line_id"] in st.claimed_l:
            continue
        st.emit(TS.TaxDecision(
            loop="itc", kind="exception", status=TS.UNKNOWN_INVOICE_IN_2B,
            book_ids=[], filed_ids=[ln["line_id"]],
            books_paise=0, filed_paise=ln["gst_paise"], discrepancy_paise=0,
            evidence=[{"rule": "no_books_entry",
                       "detail": f"no purchase behind {ln['invoice_no']}"}],
            candidates=_nearest_candidates(ln["gst_paise"], purchase_pool),
            pass_name="residual",
            explanation="an invoice was filed against us that the books "
                        "don't know; do NOT claim it — investigate"))


def _loop2_tds(events: list[TdsEvent],
               entries: list[dict]) -> list[TS.TaxDecision]:
    decisions: list[TS.TaxDecision] = []
    claimed_e: set[str] = set()
    claimed_f: set[str] = set()
    matched_entry_of: dict[str, str] = {}

    # Verbatim duplicate entries collapse onto their earliest copy.
    reps: list[dict] = []
    dups: list[tuple[dict, dict]] = []
    seen: dict[tuple, dict] = {}
    for en in sorted(entries, key=lambda e: e["entry_id"]):
        sig = (en["credit_date"], en["amount_paid_paise"], en["tds_paise"],
               en["fy_quarter"])
        if sig in seen:
            dups.append((en, seen[sig]))
        else:
            seen[sig] = en
            reps.append(en)

    by_amt_e: dict[int, list[TdsEvent]] = {}
    for e in events:
        by_amt_e.setdefault(e.amount_paid_paise, []).append(e)
    by_amt_f: dict[int, list[dict]] = {}
    for en in reps:
        by_amt_f.setdefault(en["amount_paid_paise"], []).append(en)

    for amount, evs in sorted(by_amt_e.items()):
        ens = by_amt_f.get(amount, [])
        if len(evs) != 1 or len(ens) != 1:
            continue                # ambiguity: leave both to the residuals
        e, en = evs[0], ens[0]
        ev = [{"rule": "amount_paid_exact",
               "detail": f"credited amount {amount} unique on both sides"}]
        if en["tds_paise"] != e.tds_paise:
            disc = en["tds_paise"] - e.tds_paise
            decisions.append(TS.TaxDecision(
                loop="tds", kind="match", status=TS.TDS_AMOUNT_MISMATCH,
                book_ids=[e.settlement_id], filed_ids=[en["entry_id"]],
                confidence=TS.CONF_HIGH, books_paise=e.tds_paise,
                filed_paise=en["tds_paise"], discrepancy_paise=disc,
                discrepancy_breakdown=[{"label": "deposited_differs",
                                        "amount_paise": disc}],
                evidence=ev + [{"rule": "tds_differs",
                                "detail": f"observed {e.tds_paise}, "
                                          f"filed {en['tds_paise']}"}],
                pass_name="tds_exact",
                explanation="the deduction is ours but the deposited amount "
                            "differs; reconcile with the deductor"))
        elif en["fy_quarter"] != TX.fy_quarter(e.credit_date):
            decisions.append(TS.TaxDecision(
                loop="tds", kind="match", status=TS.TDS_WRONG_QUARTER,
                book_ids=[e.settlement_id], filed_ids=[en["entry_id"]],
                confidence=TS.CONF_HIGH, books_paise=e.tds_paise,
                filed_paise=en["tds_paise"], discrepancy_paise=0,
                evidence=ev + [{"rule": "quarter_differs",
                                "detail": f"deducted in "
                                          f"{TX.fy_quarter(e.credit_date)}, "
                                          f"filed {en['fy_quarter']}"}],
                pass_name="tds_exact",
                explanation="credit exists but sits in the wrong quarter; "
                            "the return needs a correction"))
        else:
            decisions.append(TS.TaxDecision(
                loop="tds", kind="match", status=TS.TDS_CREDIT_MATCHED,
                book_ids=[e.settlement_id], filed_ids=[en["entry_id"]],
                confidence=TS.CONF_EXACT, books_paise=e.tds_paise,
                filed_paise=en["tds_paise"], discrepancy_paise=0,
                evidence=ev + [{"rule": "tds_exact",
                                "detail": f"tds {e.tds_paise} equal"}],
                pass_name="tds_exact",
                explanation="deduction and 26AS credit agree exactly"))
        claimed_e.add(e.settlement_id)
        claimed_f.add(en["entry_id"])
        matched_entry_of[en["entry_id"]] = e.settlement_id

    for en, rep in dups:
        sid = matched_entry_of.get(rep["entry_id"])
        if sid is None:
            continue
        decisions.append(TS.TaxDecision(
            loop="tds", kind="exception", status=TS.TDS_DUPLICATE_26AS,
            book_ids=[sid], filed_ids=[en["entry_id"]],
            confidence=TS.CONF_HIGH, books_paise=0,
            filed_paise=en["tds_paise"], discrepancy_paise=0,
            evidence=[{"rule": "verbatim_refiling",
                       "detail": f"identical to {rep['entry_id']}, already "
                                 f"matched"}],
            pass_name="tds_exact",
            explanation="the same deduction is reported twice in 26AS; "
                        "claim the credit once"))
        claimed_f.add(en["entry_id"])

    for e in events:
        if e.settlement_id in claimed_e:
            continue
        decisions.append(TS.TaxDecision(
            loop="tds", kind="exception", status=TS.TDS_MISSING_IN_26AS,
            book_ids=[e.settlement_id], filed_ids=[],
            books_paise=e.tds_paise, filed_paise=0,
            discrepancy_paise=-e.tds_paise,
            discrepancy_breakdown=[{"label": "tds_credit_at_risk",
                                    "amount_paise": -e.tds_paise}],
            evidence=[{"rule": "no_26as_entry",
                       "detail": f"deduction of {e.tds_paise} observed at "
                                 f"credit on {e.credit_date.isoformat()} "
                                 f"has no 26AS entry"}],
            pass_name="tds_residual",
            explanation="tax was withheld from our settlement but never "
                        "shows in 26AS; follow up with the deductor"))
    for en in entries:
        if en["entry_id"] in claimed_f:
            continue
        decisions.append(TS.TaxDecision(
            loop="tds", kind="exception", status=TS.TDS_UNKNOWN_ENTRY,
            book_ids=[], filed_ids=[en["entry_id"]],
            books_paise=0, filed_paise=en["tds_paise"], discrepancy_paise=0,
            evidence=[{"rule": "no_observed_deduction",
                       "detail": "no reconciled credit carries this "
                                 "deduction"}],
            pass_name="tds_residual",
            explanation="26AS reports a credit we never observed; verify "
                        "before claiming"))
    return decisions


def _loop3_compliance(periods: list[PeriodCheck]) -> list[TS.TaxDecision]:
    decisions: list[TS.TaxDecision] = []
    for pc in periods:
        rid = f"{pc.kind}:{pc.period}"
        base = dict(loop="obligation", book_ids=[rid],
                    filed_ids=[pc.paid_txn_id] if pc.paid_txn_id else [],
                    books_paise=pc.expected_paise, filed_paise=pc.paid_paise,
                    pass_name="compliance")
        if pc.expected_paise is None:
            decisions.append(TS.TaxDecision(
                kind="info", status=TS.UNVERIFIABLE_PRIOR_PERIOD,
                discrepancy_paise=0,
                evidence=[{"rule": "basis_precedes_statement",
                           "detail": "the liability basis month is outside "
                                     "the statement window"}],
                explanation="cannot verify this deposit: its basis precedes "
                            "the available history — flagged, not guessed",
                **base))
        elif pc.paid_paise is None:
            decisions.append(TS.TaxDecision(
                kind="exception", status=TS.NOT_PAID,
                discrepancy_paise=-pc.expected_paise,
                discrepancy_breakdown=[{"label": "unpaid_liability",
                                        "amount_paise": -pc.expected_paise}],
                evidence=[{"rule": "no_payment_debit",
                           "detail": f"no {pc.kind} payment found in "
                                     f"{pc.period}"}],
                explanation="the recomputed liability was never paid",
                **base))
        elif pc.paid_paise != pc.expected_paise:
            disc = pc.paid_paise - pc.expected_paise
            decisions.append(TS.TaxDecision(
                kind="exception", status=TS.PAID_SHORT,
                discrepancy_paise=disc,
                discrepancy_breakdown=[{
                    "label": "short_paid" if disc < 0 else "over_paid",
                    "amount_paise": disc}],
                evidence=[{"rule": "liability_recomputed",
                           "detail": f"expected {pc.expected_paise}, "
                                     f"paid {pc.paid_paise}"}],
                explanation="the payment does not cover the recomputed "
                            "liability",
                **base))
        elif pc.paid_date > TX.statutory_deadline(pc.due_date):
            days = (pc.paid_date - TX.statutory_deadline(pc.due_date)).days
            decisions.append(TS.TaxDecision(
                kind="exception", status=TS.PAID_LATE,
                discrepancy_paise=0,
                evidence=[{"rule": "paid_after_deadline",
                           "detail": f"posted {pc.paid_date.isoformat()}, "
                                     f"{days}d past the deadline"}],
                explanation="right amount, late payment — interest exposure",
                **base))
        else:
            decisions.append(TS.TaxDecision(
                kind="info", status=TS.PAID_ON_TIME,
                discrepancy_paise=0,
                evidence=[{"rule": "liability_recomputed",
                           "detail": f"paid {pc.paid_paise} on "
                                     f"{pc.paid_date.isoformat()}, on time"}],
                explanation="liability recomputed from first principles and "
                            "paid in full, on time",
                **base))
    return decisions


def reconcile_tax(inp: TaxInput) -> list[TS.TaxDecision]:
    st = _Loop1State(inp.purchases, inp.gstr2b)
    _pass1_invoice_exact(st)
    _pass2_invoice_fuzzy(st)
    _pass3_vendor_period_singleton(st)
    _pass4_residuals(st)
    decisions = list(st.decisions)
    decisions += _loop2_tds(inp.tds_events, inp.form26as)
    decisions += _loop3_compliance(inp.periods)
    return decisions
