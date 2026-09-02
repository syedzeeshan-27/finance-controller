"""Stage 3 generator invariants: the tax world and its minted ground truth."""

from collections import Counter
from datetime import timedelta

import pytest

from recon import generate as G
from recon import schemas as S
from recon.normalize import (
    paise_from_optional_rupee_str, parse_bank_date, parse_iso_date,
)
from tax import registry as TR
from tax import rules as TX
from tax import schemas as TS

SEED = 7
DAYS = 180


@pytest.fixture(scope="module")
def world():
    return G.build_world(SEED, days=DAYS)


@pytest.fixture(scope="module")
def golden_by_id(world):
    out = {}
    for g in world["golden_tax"]:
        key = (g["record_type"], g["record_id"])
        assert key not in out, f"duplicate golden row {key}"
        out[key] = g
    return out


@pytest.fixture(scope="module")
def debit_rows(world):
    return [r for r in world["statement"]
            if paise_from_optional_rupee_str(r["debit_amount"]) > 0]


def test_tax_outputs_deterministic(world):
    again = G.build_world(SEED, days=DAYS)
    assert world["gstr2b"] == again["gstr2b"]
    assert world["form26as"] == again["form26as"]
    assert world["golden_tax"] == again["golden_tax"]


def test_golden_tax_covers_every_record_exactly_once(world, golden_by_id,
                                                     debit_rows):
    # Every classified vendor debit is a purchase golden row.
    classified = [r for r in debit_rows
                  if TR.classify_debit(r["narration"]) is not None]
    assert classified
    for r in classified:
        assert (TS.RT_PURCHASE, r["txn_id"]) in golden_by_id
    # Every settlement month has a fee-invoice purchase row.
    periods = {TX.period_of(s.created_at.date()) for s in world["settlements"]}
    for period in periods:
        assert (TS.RT_PURCHASE, TX.fee_purchase_id(period)) in golden_by_id
    # Every 2B line and 26AS entry is covered.
    for ln in world["gstr2b"]:
        assert (TS.RT_2B_LINE, ln["line_id"]) in golden_by_id
    for en in world["form26as"]:
        assert (TS.RT_26AS_ENTRY, en["entry_id"]) in golden_by_id
    # Every TDS-branch discrepancy settlement is a tds_event row.
    tds_setl = [s for s in world["settlements"] if s.tds_deducted_paise > 0]
    assert len(tds_setl) >= 2
    for s in tds_setl:
        assert (TS.RT_TDS_EVENT, s.settlement_id) in golden_by_id
    # Every in-world month has one gst: and one tds: period row.
    months = 6
    period_rows = [g for g in world["golden_tax"]
                   if g["record_type"] == TS.RT_OBLIGATION_PERIOD]
    assert len(period_rows) == 2 * months
    # And nothing else exists in the golden file.
    assert len(world["golden_tax"]) == (
        len(classified) + len(periods) + len(world["gstr2b"])
        + len(world["form26as"]) + len(tds_setl) + 2 * months)


def test_clean_lines_equal_books_derivation(world, golden_by_id, debit_rows):
    """For every defect-free debit-backed line, the filed amounts must equal
    the published inclusive-split of the actual bank debit, and the invoice
    number must carry the narration's reference — re-derived here from the
    statement alone."""
    by_txn = {r["txn_id"]: r for r in debit_rows}
    checked = 0
    for ln in world["gstr2b"]:
        g = golden_by_id[(TS.RT_2B_LINE, ln["line_id"])]
        if g["scenario_tag"] not in (G._TAX_SC_CLEAN, G._TAX_SC_BLOCKED):
            continue
        row = by_txn[g["counterparty_ids"]]
        total = paise_from_optional_rupee_str(row["debit_amount"])
        assert ln["taxable_value_paise"] == TX.taxable_from_inclusive(total)
        assert ln["gst_paise"] == TX.gst_from_inclusive(total)
        assert ln["invoice_no"].endswith("/" + TX.invoice_ref(row["narration"]))
        assert ln["period"] == TX.period_of(parse_bank_date(row["value_date"]))
        vendor = TR.classify_debit(row["narration"])
        assert ln["gstin"] == vendor.gstin
        assert ln["tax_head"] == TX.head_for(vendor.state)
        checked += 1
    assert checked >= 50


def test_fee_invoice_lines_equal_settlement_sums(world, golden_by_id):
    sums = {}
    for s in world["settlements"]:
        agg = sums.setdefault(TX.period_of(s.created_at.date()), [0, 0])
        agg[0] += s.fees_paise
        agg[1] += s.tax_paise
    fee_lines = [ln for ln in world["gstr2b"]
                 if ln["gstin"] == TR.RAZORPAY.gstin]
    assert len(fee_lines) == len(sums)
    for ln in fee_lines:
        fees, tax = sums[ln["period"]]
        assert ln["taxable_value_paise"] == fees
        assert ln["gst_paise"] == tax
        g = golden_by_id[(TS.RT_2B_LINE, ln["line_id"])]
        assert g["expected_disposition"] == TS.ITC_MATCHED
        assert g["counterparty_ids"] == TX.fee_purchase_id(ln["period"])


def test_quota_minimums_and_manifest_counts(world):
    sc = world["manifest"]["tax_scenarios"]
    for tag in (G._TAX_SC_MISMATCH, G._TAX_SC_MISSING, G._TAX_SC_LATE,
                G._TAX_SC_DUP, G._TAX_SC_HEAD, G._TAX_SC_TYPO):
        assert sc.get(tag, 0) >= 2, tag
    assert sc.get(G._TAX_SC_UNKNOWN, 0) >= 3
    assert sc.get(G._TAX_SC_BLOCKED, 0) >= 10
    assert sc.get(G._TAX_SC_NO_ITC, 0) >= 10
    assert world["manifest"]["counts"]["golden_tax_rows"] == len(world["golden_tax"])
    assert world["manifest"]["counts"]["gstr2b_lines"] == len(world["gstr2b"])
    assert world["manifest"]["counts"]["form26as_entries"] == len(world["form26as"])


def test_non_filing_vendors_never_appear_in_2b(world, golden_by_id, debit_rows):
    filed_gstins = {ln["gstin"] for ln in world["gstr2b"]}
    for v in TR.ALL_VENDORS:
        if not v.files_2b:
            assert v.gstin == "" or v.gstin not in filed_gstins
    # ... and their purchases are golden no_itc_applicable.
    checked = 0
    for r in debit_rows:
        v = TR.classify_debit(r["narration"])
        if v is None or v.files_2b:
            continue
        g = golden_by_id[(TS.RT_PURCHASE, r["txn_id"])]
        assert g["expected_disposition"] == TS.NO_ITC_APPLICABLE
        assert g["counterparty_ids"] == ""
        checked += 1
    assert checked >= 10   # lic + indianoil + freelance spends exist


def test_unknown_and_typo_lines_are_separated(world, golden_by_id, debit_rows):
    """F6/F7: a typo'd invoice number sits exactly 1 digit from its own
    purchase ref and >=2 from every other ref of that vendor; unknown-line
    amounts sit >= Rs 10 from every same-vendor-period purchase."""
    refs_by_vendor = {}
    purchases = {}   # (vendor_key, period) -> [gst...]
    for r in debit_rows:
        v = TR.classify_debit(r["narration"])
        if v is None:
            continue
        ref = TX.invoice_ref(r["narration"])
        refs_by_vendor.setdefault(v.key, []).append(ref)
        total = paise_from_optional_rupee_str(r["debit_amount"])
        period = TX.period_of(parse_bank_date(r["value_date"]))
        purchases.setdefault((v.key, period), []).append(
            TX.gst_from_inclusive(total))

    def hamming(a, b):
        return sum(x != y for x, y in zip(a, b)) if len(a) == len(b) else 99

    vendors_by_gstin = {v.gstin: v for v in TR.ALL_VENDORS if v.gstin}
    n_typo = n_unknown = 0
    for ln in world["gstr2b"]:
        g = golden_by_id[(TS.RT_2B_LINE, ln["line_id"])]
        ref = ln["invoice_no"].split("/")[-1]
        vendor = vendors_by_gstin[ln["gstin"]]
        if g["scenario_tag"] == G._TAX_SC_TYPO:
            dists = sorted(hamming(ref, o) for o in refs_by_vendor[vendor.key])
            assert dists[0] == 1            # its own purchase, one digit off
            assert len(dists) == 1 or dists[1] >= 2
            n_typo += 1
        elif g["scenario_tag"] == G._TAX_SC_UNKNOWN:
            assert all(hamming(ref, o) >= 2
                       for o in refs_by_vendor[vendor.key])
            near = purchases.get((vendor.key, ln["period"]), [])
            assert all(abs(ln["gst_paise"] - x) >= G.MIN_SEPARATION_PAISE
                       for x in near)
            n_unknown += 1
    assert n_typo >= 2 and n_unknown >= 3


def test_obligation_defects_stay_inside_detection_gates(world, tmp_path_factory):
    """The short/late GST months must not break Stage 2's recurring
    detection — run the real detector on the defective world."""
    from forecast.recurring import detect
    from forecast.slicing import build_input, load_world
    out = str(tmp_path_factory.mktemp("taxworld"))
    G.write_world(world, out)
    loaded = load_world(out)
    # The defects must not break detection at the cutoffs the backtest
    # GRADES detection at (the last rolling origin) nor at world end. Early
    # cutoffs are out of scope: growth legitimately pushes GST's amount
    # spread past the variable gate there with or without Stage 3's defects.
    for cutoff_idx in (160, DAYS - 1):
        inp = build_input(loaded, G.START_DATE + timedelta(days=cutoff_idx))
        keys = {d.key: d for d in detect(inp)}
        gst_keys = [k for k in keys if "GST PAYMENT" in k]
        tds_keys = [k for k in keys if "TDS PAYMENT" in k]
        payroll_keys = [k for k in keys if "STAFF PAYROLL" in k]
        assert len(gst_keys) == 1, cutoff_idx
        assert keys[gst_keys[0]].period == "monthly"
        assert len(tds_keys) == 1, cutoff_idx
        assert keys[tds_keys[0]].period == "monthly"
        assert len(payroll_keys) == 1, cutoff_idx


def test_tds_deposit_recomputable_from_statement(world, debit_rows):
    """From month 1 on, the TDS deposit equals 10% of the previous month's
    ACTUAL posted payroll debit — verified from the statement alone."""
    payroll = {}
    tds = {}
    for r in debit_rows:
        d = parse_bank_date(r["value_date"])
        amt = paise_from_optional_rupee_str(r["debit_amount"])
        if "STAFF PAYROLL" in r["narration"]:
            payroll[TX.period_of(d)] = amt
        elif "TDS PAYMENT-CBDT" in r["narration"]:
            tds[TX.period_of(d)] = amt
    assert len(tds) == 6
    checked = 0
    for period, amount in tds.items():
        prev = f"{period[:4]}-{int(period[5:]) - 1:02d}"
        if prev in payroll:
            assert amount == TX.tds_deposit(payroll[prev]), period
            checked += 1
    assert checked == 5    # months 1..5


def test_period_goldens_shape(world):
    rows = {g["record_id"]: g for g in world["golden_tax"]
            if g["record_type"] == TS.RT_OBLIGATION_PERIOD}
    statuses = Counter(g["expected_disposition"] for g in rows.values())
    assert statuses[TS.PAID_SHORT] == 1
    assert statuses[TS.PAID_LATE] == 1
    assert statuses[TS.UNVERIFIABLE_PRIOR_PERIOD] == 1
    assert statuses[TS.PAID_ON_TIME] == 9      # 12 periods - 3 above
    assert rows["tds:2025-04"]["expected_disposition"] == TS.UNVERIFIABLE_PRIOR_PERIOD
    short = [g for g in rows.values()
             if g["expected_disposition"] == TS.PAID_SHORT][0]
    assert short["expected_discrepancy_paise"] < 0
    assert short["record_id"].startswith("gst:")
    # every decided period names the bank debit it graded
    for g in rows.values():
        if g["expected_disposition"] != TS.NOT_PAID:
            assert g["counterparty_ids"].startswith("BANK")


def test_blocked_lines_filed_but_never_claimable(world, golden_by_id):
    blocked_gstins = {v.gstin for v in TR.DEBIT_VENDORS
                      if v.files_2b and not v.itc_eligible}
    n = 0
    for ln in world["gstr2b"]:
        if ln["gstin"] in blocked_gstins:
            g = golden_by_id[(TS.RT_2B_LINE, ln["line_id"])]
            assert g["expected_disposition"] == TS.BLOCKED_CREDIT_NO_ITC
            assert g["counterparty_ids"].startswith("BANK")
            n += 1
    assert n >= 10
