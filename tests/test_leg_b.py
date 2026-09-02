"""Leg B rules and the journey composition on generated worlds + micro-fixtures."""

from recon import io_load, schemas as S
from recon.benchmark import grade_leg_b
from recon.engine import reconcile_leg_a
from recon.generate import generate
from recon.journey import build_journeys
from recon.leg_b import reconcile_leg_b


def order(oid="order_1", amount=100_000, status="paid"):
    return {"order_id": oid, "receipt": f"R-{oid}", "amount_paise": amount,
            "status": status, "created_at": "2025-04-02T10:00:00"}


def payment(pid="pay_1", oid="order_1", amount=100_000, status="captured",
            created="2025-04-02T10:05:00", settlement_id="setl_X"):
    return {"payment_id": pid, "order_id": oid, "method": "upi",
            "amount_paise": amount, "fee_paise": 30, "tax_paise": 5,
            "status": status, "created_at": created,
            "settlement_id": settlement_id}


def by_record(decisions, record_id):
    return next(d for d in decisions if d.record_id == record_id)


class TestLegBRules:
    def test_clean_paid(self):
        ds = reconcile_leg_b([payment()], [order()])
        assert by_record(ds, "pay_1").status == S.PAYMENT_APPLIED
        o = by_record(ds, "order_1")
        assert o.status == S.ORDER_PAID and o.counterparty_ids == ["pay_1"]

    def test_duplicate_capture_is_the_later_one(self):
        p1 = payment("pay_1", created="2025-04-02T10:05:00")
        p2 = payment("pay_2", created="2025-04-02T11:00:00")
        ds = reconcile_leg_b([p2, p1], [order()])  # input order shuffled
        assert by_record(ds, "pay_1").status == S.PAYMENT_APPLIED
        d2 = by_record(ds, "pay_2")
        assert d2.status == S.EXCEPTION_DUPLICATE_PAYMENT
        assert d2.candidates[0]["record_id"] == "pay_1"

    def test_orphan_payment(self):
        ds = reconcile_leg_b([payment(oid="order_ghost")], [order()])
        assert by_record(ds, "pay_1").status == S.EXCEPTION_PAYMENT_NO_ORDER

    def test_amount_mismatch_both_sides(self):
        ds = reconcile_leg_b([payment(amount=90_000)], [order()])
        assert by_record(ds, "pay_1").status == S.EXCEPTION_AMOUNT_MISMATCH
        assert by_record(ds, "order_1").status == S.EXCEPTION_AMOUNT_MISMATCH

    def test_refund_flow(self):
        ds = reconcile_leg_b([payment(status="refunded")], [order()])
        assert by_record(ds, "pay_1").status == S.PAYMENT_REFUNDED
        assert by_record(ds, "order_1").status == S.ORDER_REFUNDED

    def test_unpaid_and_cancelled(self):
        ds = reconcile_leg_b([], [order("order_1", status="created"),
                                  order("order_2", status="cancelled")])
        assert by_record(ds, "order_1").status == S.EXCEPTION_UNPAID_ORDER
        assert by_record(ds, "order_2").status == S.ORDER_CANCELLED

    def test_failed_then_captured(self):
        pf = payment("pay_f", status="failed", created="2025-04-02T09:00:00")
        pc = payment("pay_c", created="2025-04-02T10:00:00")
        ds = reconcile_leg_b([pf, pc], [order()])
        assert by_record(ds, "pay_f").status == S.PAYMENT_FAILED
        assert by_record(ds, "pay_c").status == S.PAYMENT_APPLIED
        assert by_record(ds, "order_1").counterparty_ids == ["pay_c"]


class TestOnGeneratedWorld:
    def test_full_leg_b_accuracy(self, tmp_path):
        data_dir = str(tmp_path / "w")
        generate(13, data_dir)
        payments = io_load.load_payments(data_dir)
        orders = io_load.load_orders(data_dir)
        graded = grade_leg_b(reconcile_leg_b(payments, orders),
                             io_load.load_golden(data_dir, "B"))
        assert graded["disposition_accuracy"] == 1.0

    def test_journeys_compose(self, tmp_path):
        data_dir = str(tmp_path / "w")
        generate(13, data_dir)
        payments = io_load.load_payments(data_dir)
        orders = io_load.load_orders(data_dir)
        da = reconcile_leg_a(io_load.load_settlements(data_dir),
                             io_load.load_bank_rows(data_dir))
        db = reconcile_leg_b(payments, orders)
        journeys = build_journeys(orders, payments, da, db)
        assert len(journeys) == len(orders)
        # every paid order reaches a settlement verdict
        paid = [j for j in journeys if j["order_status"] == S.ORDER_PAID]
        assert paid and all(j["settlement_status"] for j in paid)
        # missing-bank settlements surface as broken journeys
        breaks = {j["first_break"] for j in journeys}
        assert "settlement never hit the bank" in breaks
        # clean journeys exist and carry bank txn ids
        clean = [j for j in paid if not j["first_break"]]
        assert clean and all(j["bank_txn_ids"] for j in clean)
