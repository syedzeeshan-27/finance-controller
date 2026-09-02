"""Razorpay official-format adapters: paise passthrough, io_load round-trip."""

import os

from ingest.razorpay_files import (load_fixture_dir, payments_from_report,
                                   settlements_from_api, write_csv)
from recon import io_load, schemas as S

_FIXTURES = os.path.join("data", "fixtures", "razorpay")


def _loaded():
    return load_fixture_dir(_FIXTURES)


def test_settlement_entity_maps_paise_passthrough():
    collection, report = _loaded()
    rows = settlements_from_api(collection, report)
    assert [r["settlement_id"] for r in rows] == ["setl_MnO5pQ6rS7tU8v",
                                                  "setl_NpQ7rS8tU9vW0x"]
    first = rows[0]
    assert first["amount_paise"] == 338071      # verbatim, never converted
    assert first["fees_paise"] == 10152
    assert first["tax_paise"] == 1827
    assert first["utr"] == "1775092500abx4kq"
    assert first["payment_count"] == 2          # joined from the report


def test_epoch_timestamps_become_iso():
    collection, report = _loaded()
    rows = settlements_from_api(collection, report)
    assert rows[0]["created_at"] == "2026-04-02T01:15:00"
    assert rows[0]["settled_at"] == "2026-04-02"
    payments = payments_from_report(report)
    assert payments[0]["created_at"] == "2026-04-01T12:00:00"


def test_report_rows_become_payments_refunds_excluded():
    _collection, report = _loaded()
    payments = payments_from_report(report)
    assert len(payments) == 3                   # the refund row is not a payment
    assert {p["payment_id"] for p in payments} == {
        "pay_LxA01aB2cD3eF4", "pay_LxB02bC3dE4fG5", "pay_LxC03cD4eF5gH6"}
    upi = payments[0]
    assert upi["amount_paise"] == 100000 and upi["fee_paise"] == 2900
    assert upi["status"] == "captured"
    assert upi["order_id"] == "order_LxOrd01aB2cD3e"


def test_adapter_output_loads_via_io_load(tmp_path):
    collection, report = _loaded()
    out = str(tmp_path)
    write_csv(os.path.join(out, "settlements.csv"), S.SETTLEMENTS_COLUMNS,
              settlements_from_api(collection, report))
    write_csv(os.path.join(out, "payments.csv"), S.PAYMENTS_COLUMNS,
              payments_from_report(report))
    settlements = io_load.load_settlements(out)
    payments = io_load.load_payments(out)
    assert len(settlements) == 2 and len(payments) == 3
    # settlement amount equals the report's credits minus debits
    by_id = {s.settlement_id: s for s in settlements}
    assert by_id["setl_NpQ7rS8tU9vW0x"].amount_paise == 488200 - 15000
    assert by_id["setl_MnO5pQ6rS7tU8v"].amount_paise == 96578 + 241493
