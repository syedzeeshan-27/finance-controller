"""Tax-engine fixtures: every disposition, the honesty cases, and the
behavioral canary proving the engine never reads an answer key."""

import os
import shutil
from datetime import date

import pytest

from tax import rules as TX
from tax import schemas as TS
from tax.books import PeriodCheck, Purchase, TdsEvent
from tax.engine import reconcile_tax
from tax.io_tax import TaxInput


def purchase(pid="BANK000001", vendor="dtdc", period="2025-05", ref="48213",
             taxable=100_000, gst=18_000, head=TX.HEAD_CGST_SGST,
             eligible=True, files=True, reason=""):
    return Purchase(purchase_id=pid, vendor_key=vendor, period=period,
                    invoice_date=date(2025, 5, 10), ref=ref,
                    taxable_paise=taxable, gst_paise=gst, expected_head=head,
                    itc_eligible=eligible, files_2b=files,
                    no_itc_reason=reason)


def line(line_id="2B000001", period="2025-05", gstin="29AAACD8017H1ZN",
         invoice_no="DTDC/48213", taxable=100_000, gst=18_000,
         head=TX.HEAD_CGST_SGST):
    return {"line_id": line_id, "period": period, "gstin": gstin,
            "vendor_name": "DTDC EXPRESS LTD", "invoice_no": invoice_no,
            "invoice_date": "2025-05-10", "taxable_value_paise": taxable,
            "gst_paise": gst, "tax_head": head}


def run(purchases=(), lines=(), events=(), entries=(), periods=()):
    return reconcile_tax(TaxInput(
        purchases=list(purchases), gstr2b=list(lines),
        tds_events=list(events), form26as=list(entries),
        periods=list(periods)))


def only(decisions, status):
    out = [d for d in decisions if d.status == status]
    assert len(out) == 1, f"{status}: {[d.status for d in decisions]}"
    return out[0]


class TestLoop1Passes:
    def test_clean_pair_matches_exact(self):
        d = only(run([purchase()], [line()]), TS.ITC_MATCHED)
        assert d.confidence == TS.CONF_EXACT
        assert d.book_ids == ["BANK000001"] and d.filed_ids == ["2B000001"]
        assert d.pass_name == "invoice_exact"

    def test_amount_mismatch_flagged_with_breakdown(self):
        d = only(run([purchase()], [line(gst=17_000)]),
                 TS.ITC_AMOUNT_MISMATCH)
        assert d.discrepancy_paise == -1_000
        assert d.discrepancy_breakdown == [
            {"label": "vendor_filed_short", "amount_paise": -1_000}]
        assert d.confidence == TS.CONF_HIGH

    def test_head_mismatch(self):
        d = only(run([purchase()], [line(head=TX.HEAD_IGST)]),
                 TS.ITC_HEAD_MISMATCH)
        assert d.discrepancy_paise == 0

    def test_filed_late_becomes_deferred(self):
        d = only(run([purchase()], [line(period="2025-06")]),
                 TS.ITC_DEFERRED_NEXT_PERIOD)
        assert d.confidence == TS.CONF_HIGH

    def test_duplicate_line_claimed_once(self):
        dec = run([purchase()], [line(), line(line_id="2B000002")])
        m = only(dec, TS.ITC_MATCHED)
        dup = only(dec, TS.DUPLICATE_2B_LINE)
        assert m.filed_ids == ["2B000001"]          # earliest line wins
        assert dup.filed_ids == ["2B000002"]
        assert dup.book_ids == ["BANK000001"]       # counterparty preserved

    def test_typo_matches_only_with_exact_amounts(self):
        good = run([purchase()], [line(invoice_no="DTDC/48219")])
        d = only(good, TS.ITC_MATCHED)
        assert d.pass_name == "invoice_fuzzy"
        assert d.confidence == TS.CONF_HIGH
        # same typo but amounts differ: a damaged reference alone never matches
        bad = run([purchase()], [line(invoice_no="DTDC/48219", gst=17_000)])
        assert only(bad, TS.ITC_MISSING_IN_2B)
        assert only(bad, TS.UNKNOWN_INVOICE_IN_2B)

    def test_ref_collision_resolved_by_exact_amount(self):
        ps = [purchase(), purchase(pid="BANK000002", taxable=200_000,
                                   gst=36_000)]
        lns = [line(), line(line_id="2B000002", taxable=200_000, gst=36_000)]
        dec = run(ps, lns)
        matched = [d for d in dec if d.status == TS.ITC_MATCHED]
        assert len(matched) == 2
        pairs = {(d.book_ids[0], d.filed_ids[0]) for d in matched}
        assert pairs == {("BANK000001", "2B000001"),
                         ("BANK000002", "2B000002")}
        assert all(d.confidence == TS.CONF_HIGH for d in matched)

    def test_fee_invoice_matches_by_vendor_period_singleton(self):
        p = Purchase(purchase_id="RZPFEE-2025-05", vendor_key="razorpay",
                     period="2025-05", invoice_date=None, ref=None,
                     taxable_paise=555_000, gst_paise=99_900,
                     expected_head=TX.HEAD_CGST_SGST, itc_eligible=True,
                     files_2b=True)
        ln = line(gstin="29AAGCR4375J1ZU", invoice_no="RZP/202505",
                  taxable=555_000, gst=99_900)
        d = only(run([p], [ln]), TS.ITC_MATCHED)
        assert d.pass_name == "vendor_period_singleton"
        assert d.confidence == TS.CONF_MEDIUM

    def test_singleton_with_differing_amounts_is_not_forced(self):
        """One refless books entry vs one refless line with different money:
        indistinguishable from missing + unknown — the engine must refuse."""
        p = Purchase(purchase_id="RZPFEE-2025-05", vendor_key="razorpay",
                     period="2025-05", invoice_date=None, ref=None,
                     taxable_paise=555_000, gst_paise=99_900,
                     expected_head=TX.HEAD_CGST_SGST, itc_eligible=True,
                     files_2b=True)
        ln = line(gstin="29AAGCR4375J1ZU", invoice_no="RZP/202505",
                  taxable=500_000, gst=90_000)
        dec = run([p], [ln])
        missing = only(dec, TS.ITC_MISSING_IN_2B)
        unknown = only(dec, TS.UNKNOWN_INVOICE_IN_2B)
        assert missing.candidates and unknown.candidates

    def test_blocked_and_no_itc_paths(self):
        blocked_p = purchase(vendor="swiggy", eligible=False,
                             reason="blocked_17_5_food_and_beverages")
        blocked_ln = line(gstin="29AAFCB7707D1ZP", invoice_no="SWG/48213")
        exempt_p = purchase(pid="BANK000009", vendor="lic", ref="77777",
                            eligible=False, files=False, head="",
                            reason="exempt_life_insurance_premium")
        dec = run([blocked_p, exempt_p], [blocked_ln])
        b = only(dec, TS.BLOCKED_CREDIT_NO_ITC)
        assert b.filed_ids == ["2B000001"] and b.kind == "match"
        n = only(dec, TS.NO_ITC_APPLICABLE)
        assert n.book_ids == ["BANK000009"] and not n.filed_ids

    def test_missing_carries_at_risk_amount_and_candidates(self):
        d = only(run([purchase()], []), TS.ITC_MISSING_IN_2B)
        assert d.discrepancy_paise == -18_000
        assert d.discrepancy_breakdown[0]["label"] == "itc_at_risk"


class TestLoop2:
    def event(self, sid="setl_A", amount=2_866_540, tds=28_665,
              d=date(2025, 4, 16)):
        return TdsEvent(settlement_id=sid, txn_id="BANK000053",
                        credit_date=d, amount_paid_paise=amount,
                        tds_paise=tds)

    def entry(self, eid="26AS001", amount=2_866_540, tds=28_665,
              quarter="2025-26Q1", d="2025-04-16"):
        return {"entry_id": eid, "tan": "BLRR12345E",
                "deductor_name": "RAZORPAY SOFTWARE PVT LTD",
                "section": "194O", "fy_quarter": quarter, "credit_date": d,
                "amount_paid_paise": amount, "tds_paise": tds}

    def test_exact_match(self):
        d = only(run(events=[self.event()], entries=[self.entry()]),
                 TS.TDS_CREDIT_MATCHED)
        assert d.confidence == TS.CONF_EXACT

    def test_amount_mismatch(self):
        d = only(run(events=[self.event()], entries=[self.entry(tds=28_365)]),
                 TS.TDS_AMOUNT_MISMATCH)
        assert d.discrepancy_paise == -300

    def test_wrong_quarter_still_linked(self):
        d = only(run(events=[self.event()],
                     entries=[self.entry(quarter="2025-26Q2")]),
                 TS.TDS_WRONG_QUARTER)
        assert d.book_ids == ["setl_A"] and d.filed_ids == ["26AS001"]

    def test_duplicate_entry(self):
        dec = run(events=[self.event()],
                  entries=[self.entry(), self.entry(eid="26AS002")])
        assert only(dec, TS.TDS_CREDIT_MATCHED).filed_ids == ["26AS001"]
        dup = only(dec, TS.TDS_DUPLICATE_26AS)
        assert dup.filed_ids == ["26AS002"] and dup.book_ids == ["setl_A"]

    def test_missing_and_unknown(self):
        dec = run(events=[self.event()],
                  entries=[self.entry(eid="26AS009", amount=999_999,
                                      tds=10_000)])
        assert only(dec, TS.TDS_MISSING_IN_26AS).discrepancy_paise == -28_665
        assert only(dec, TS.TDS_UNKNOWN_ENTRY).filed_ids == ["26AS009"]


class TestLoop3:
    def check(self, kind="gst", period="2025-05", expected=4_126_000,
              paid=4_126_000, paid_date=date(2025, 5, 20),
              due=date(2025, 5, 20)):
        return PeriodCheck(kind=kind, period=period, due_date=due,
                           expected_paise=expected,
                           paid_txn_id="BANK000070" if paid is not None else "",
                           paid_paise=paid, paid_date=paid_date)

    def test_on_time(self):
        assert only(run(periods=[self.check()]), TS.PAID_ON_TIME)

    def test_short(self):
        d = only(run(periods=[self.check(paid=4_000_000)]), TS.PAID_SHORT)
        assert d.discrepancy_paise == -126_000

    def test_late(self):
        d = only(run(periods=[self.check(paid_date=date(2025, 5, 23))]),
                 TS.PAID_LATE)
        assert d.filed_ids == ["BANK000070"]

    def test_not_paid(self):
        d = only(run(periods=[self.check(paid=None, paid_date=None)]),
                 TS.NOT_PAID)
        assert d.discrepancy_paise == -4_126_000

    def test_unverifiable(self):
        d = only(run(periods=[self.check(kind="tds", expected=None)]),
                 TS.UNVERIFIABLE_PRIOR_PERIOD)
        assert d.kind == "info"

    def test_sunday_deadline_rolls(self):
        # due 2025-07-20 is a Sunday; paying on Monday the 21st is on time
        d = only(run(periods=[self.check(period="2025-07",
                                         due=date(2025, 7, 20),
                                         paid_date=date(2025, 7, 21))]),
                 TS.PAID_ON_TIME)
        assert d.status == TS.PAID_ON_TIME


class TestGoldenBlindBehavior:
    def test_decisions_identical_without_and_with_corrupted_goldens(
            self, tmp_path):
        """Delete the answer keys, then corrupt them — the engine's output
        must be byte-identical either way."""
        from recon.generate import generate
        from tax.io_tax import build_tax_input

        src = str(tmp_path / "w")
        generate(9, src, days=90)
        baseline = [d.to_dict() for d in
                    reconcile_tax(build_tax_input(src))]

        removed = str(tmp_path / "removed")
        shutil.copytree(src, removed)
        for name in os.listdir(removed):
            if name.startswith("golden_"):
                os.remove(os.path.join(removed, name))
        assert baseline == [d.to_dict() for d in
                            reconcile_tax(build_tax_input(removed))]

        corrupted = str(tmp_path / "corrupted")
        shutil.copytree(src, corrupted)
        for name in os.listdir(corrupted):
            if name.startswith("golden_") and name.endswith(".csv"):
                with open(os.path.join(corrupted, name), "a",
                          encoding="utf-8") as f:
                    f.write("tampered,tampered,tampered\n")
        assert baseline == [d.to_dict() for d in
                            reconcile_tax(build_tax_input(corrupted))]

    def test_deterministic(self, tmp_path):
        from recon.generate import generate
        from tax.io_tax import build_tax_input
        d = str(tmp_path / "w")
        generate(11, d, days=90)
        inp = build_tax_input(d)
        assert ([x.to_dict() for x in reconcile_tax(inp)]
                == [x.to_dict() for x in reconcile_tax(inp)])
