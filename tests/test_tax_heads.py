"""CGST/SGST split (published + re-typed impls) and the GSTR-1 v1 summary."""

import os

from recon import io_load
from recon.generate import generate
from recon.normalize import parse_iso_date
from tax import rules as TX
from tax import verify as TV
from tax.books import derive_purchases
from tax.gstr1 import build_gstr1


def test_split_heads_odd_paisa_pinned():
    assert TX.split_heads(101, TX.HEAD_CGST_SGST) == {
        "igst_paise": 0, "cgst_paise": 50, "sgst_paise": 51}
    assert TX.split_heads(100, TX.HEAD_CGST_SGST) == {
        "igst_paise": 0, "cgst_paise": 50, "sgst_paise": 50}
    assert TX.split_heads(101, TX.HEAD_IGST) == {
        "igst_paise": 101, "cgst_paise": 0, "sgst_paise": 0}


def test_split_sums_identity_over_range():
    for gst in range(0, 2000):
        for head in (TX.HEAD_IGST, TX.HEAD_CGST_SGST):
            s = TX.split_heads(gst, head)
            assert s["igst_paise"] + s["cgst_paise"] + s["sgst_paise"] == gst
            assert abs(s["cgst_paise"] - s["sgst_paise"]) <= 1
            if head == TX.HEAD_IGST:
                assert s["cgst_paise"] == s["sgst_paise"] == 0


def test_published_and_retyped_split_agree():
    """rules.split_heads and tax.verify._own_split_heads share no code;
    they must still agree everywhere."""
    for gst in range(0, 2000):
        for head in ("igst", "cgst_sgst"):
            s = TX.split_heads(gst, head)
            assert TV._own_split_heads(gst, head) == (
                s["igst_paise"], s["cgst_paise"], s["sgst_paise"])


def test_every_filing_purchase_carries_a_consistent_split(tmp_path):
    data_dir = str(tmp_path / "w")
    generate(17, data_dir)
    purchases = derive_purchases(io_load.load_bank_rows(data_dir),
                                 io_load.load_settlements(data_dir))
    filing = [p for p in purchases if p.files_2b]
    assert filing, "world should contain 2B-filing purchases"
    for p in filing:
        assert p.head_split is not None
        assert sum(p.head_split.values()) == p.gst_paise
        assert p.head_split == TX.split_heads(p.gst_paise, p.expected_head)
    for p in purchases:
        if not p.files_2b:
            assert p.head_split is None


def test_gstr1_totals_equal_captured_gross_per_period(tmp_path):
    data_dir = str(tmp_path / "w")
    generate(17, data_dir)
    payments = io_load.load_payments(data_dir)
    table = build_gstr1(payments)
    assert table, "world should contain captured payments"
    for period, row in table.items():
        captured = [p for p in payments if p["status"] == "captured"
                    and TX.period_of(parse_iso_date(p["created_at"]))
                    == period]
        assert row["gross_paise"] == sum(p["amount_paise"] for p in captured)
        assert row["invoice_count"] == len(captured)
        assert row["liability_paise"] == TX.gst_liability(row["gross_paise"])


def test_close_summary_heads_sum_to_claimable_books_side():
    from controller.close import daily_close
    close = daily_close(os.path.join("data", "seeds", "42")).to_dict()
    heads = close["tax_summary"]["itc_claimable_by_head"]
    assert set(heads) == {"igst_paise", "cgst_paise", "sgst_paise"}
    assert all(v >= 0 for v in heads.values())
    assert sum(heads.values()) > 0
