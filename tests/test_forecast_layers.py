"""Forecaster layer tests: pipeline, recurring detection, residual stats, bands."""

from datetime import date

import pytest

from recon import schemas as RS
from forecast import residual as C
from forecast.forecaster import forecast
from forecast.pipeline import known_inflows, run_recon
from forecast.recurring import Detected, detect, project, template_key
from forecast.schemas import ForecastInput
from tax import rules as TX

CUTOFF = date(2025, 7, 15)


def bank_row(txn_id, value_date, credit=0, debit=0, narration="X", balance=0):
    d = value_date.strftime("%d/%m/%Y")
    return RS.BankRow(txn_id=txn_id, txn_date=d, value_date=d,
                      narration=narration, ref_no="", debit_paise=debit,
                      credit_paise=credit, balance_paise=balance)


def settlement(sid, amount, created, settled, utr="UTIB250987654321"):
    return RS.Settlement(settlement_id=sid, amount_paise=amount, fees_paise=0,
                         tax_paise=0, utr=utr, payment_count=1,
                         status="processed",
                         created_at=f"{created.isoformat()}T01:15:00",
                         settled_at=settled.isoformat())


def inp_with(bank_rows=(), settlements=(), payments=(), cutoff=CUTOFF):
    rows = list(bank_rows)
    return ForecastInput(
        cutoff=cutoff, opening_balance_paise=rows[-1].balance_paise if rows else 0,
        first_statement_date=date(2025, 4, 2) if rows else None,
        bank_rows=rows, settlements=list(settlements), payments=list(payments))


class TestPipeline:
    def test_unpaid_future_settlement_is_known_inflow(self):
        s = settlement("setl_A", 1_000_000, date(2025, 7, 14), date(2025, 7, 16))
        inp = inp_with(settlements=[s])
        k = known_inflows(inp, 14, run_recon(inp))
        assert k["by_date"][date(2025, 7, 16)] == 1_000_000
        assert k["in_flight"][0]["kind"] == "settlement"
        assert not k["attention"]

    def test_expected_date_rolls_off_sunday(self):
        # 2025-07-20 is a Sunday -> credit expected Monday the 21st
        s = settlement("setl_A", 500_000, date(2025, 7, 17), date(2025, 7, 20))
        inp = inp_with(settlements=[s])
        k = known_inflows(inp, 14, run_recon(inp))
        assert k["by_date"] == {date(2025, 7, 21): 500_000}

    def test_overdue_settlement_goes_to_attention_not_path(self):
        s = settlement("setl_A", 750_000, date(2025, 7, 1), date(2025, 7, 3))
        inp = inp_with(settlements=[s])
        k = known_inflows(inp, 14, run_recon(inp))
        assert k["by_date"] == {}
        assert k["attention"][0]["id"] == "setl_A"
        assert k["attention"][0]["days_overdue"] == 12

    def test_already_paid_settlement_excluded(self):
        s = settlement("setl_A", 900_000, date(2025, 7, 10), date(2025, 7, 12))
        paid = bank_row("BANK000001", date(2025, 7, 12), credit=900_000,
                        narration=f"NEFT-{s.utr}-RAZORPAY SOFTWARE-SETTLEMENT")
        inp = inp_with(bank_rows=[paid], settlements=[s])
        k = known_inflows(inp, 14, run_recon(inp))
        assert k["by_date"] == {} and not k["attention"]

    def test_unsettled_captures_follow_t_plus_2(self):
        pays = [
            {"payment_id": "pay_1", "order_id": "o1", "method": "upi",
             "amount_paise": 100_000, "fee_paise": 300, "tax_paise": 54,
             "status": "captured", "created_at": "2025-07-15T10:00:00",
             "settlement_id": "setl_future"},
            {"payment_id": "pay_2", "order_id": "o2", "method": "upi",
             "amount_paise": 50_000, "fee_paise": 150, "tax_paise": 27,
             "status": "captured", "created_at": "2025-07-15T11:00:00",
             "settlement_id": "setl_future"},
            {"payment_id": "pay_3", "order_id": "o3", "method": "upi",
             "amount_paise": 77_000, "fee_paise": 0, "tax_paise": 0,
             "status": "failed", "created_at": "2025-07-15T12:00:00",
             "settlement_id": ""},
        ]
        inp = inp_with(payments=pays)
        k = known_inflows(inp, 14, run_recon(inp))
        expected_net = (100_000 - 300 - 54) + (50_000 - 150 - 27)
        assert k["by_date"] == {date(2025, 7, 17): expected_net}
        assert k["in_flight"][0]["kind"] == "unsettled_batch"


class TestRecurringDetection:
    def test_template_key(self):
        assert template_key("SAL-NEFT-STAFF PAYROLL-48213") == "SAL-NEFT-STAFF PAYROLL-#"
        assert template_key("NEFT DR-AWS INDIA PL-INV73104") == "NEFT DR-AWS INDIA PL-INV#"
        assert template_key("UPI-BHARTI AIRTEL-84120@icici") == "UPI-BHARTI AIRTEL-#@ICICI"
        assert template_key("POS 4287XXXXXX BIG BAZAAR-40213") == "POS #XXXXXX BIG BAZAAR-#"

    def _monthly_rows(self, amounts, days=(1, 1, 2, 1)):
        months = [4, 5, 6, 7]
        return [bank_row(f"B{i}", date(2025, months[i], days[i]),
                         debit=amounts[i], narration=f"SAL-NEFT-STAFF PAYROLL-{10000+i}")
                for i in range(len(amounts))]

    def test_monthly_fixed_with_jitter_detected(self):
        inp = inp_with(bank_rows=self._monthly_rows([500_000] * 4))
        det = detect(inp)
        assert len(det) == 1
        assert det[0].period == "monthly" and det[0].anchor == 1
        assert det[0].amount_class == "fixed"

    def test_two_hit_quarterly_equal_amounts(self):
        rows = [bank_row("B1", date(2025, 4, 15), debit=3_600_000,
                         narration="ACH-D-LIC PREMIUM-11111"),
                bank_row("B2", date(2025, 7, 15), debit=3_600_000,
                         narration="ACH-D-LIC PREMIUM-22222")]
        det = detect(inp_with(bank_rows=rows))
        assert len(det) == 1 and det[0].period == "quarterly"

    def test_volatile_amounts_rejected(self):
        inp = inp_with(bank_rows=self._monthly_rows(
            [100_000, 900_000, 150_000, 800_000]))
        assert detect(inp) == []

    def test_inconsistent_gaps_rejected(self):
        rows = [bank_row(f"B{i}", d, debit=200_000,
                         narration=f"UPI-SWIGGY INSTAMART-{30000+i}@ybl")
                for i, d in enumerate([date(2025, 5, 1), date(2025, 5, 3),
                                       date(2025, 6, 2), date(2025, 7, 14)])]
        assert detect(inp_with(bank_rows=rows)) == []

    def test_lapsed_key_not_detected(self):
        rows = [bank_row(f"B{i}", date(2025, m, 1), debit=500_000,
                         narration=f"SAL-NEFT-STAFF PAYROLL-{10000+i}")
                for i, m in enumerate([2, 3, 4])]
        # last hit 2025-04-01, cutoff 2025-07-15: 105 days > 1.5 months
        assert detect(inp_with(bank_rows=rows)) == []

    def test_drift_projection(self):
        det = Detected(key="K", period="monthly", anchor=1, amount_class="stable",
                       occurrences=[(date(2025, 5, 1), 100), (date(2025, 6, 1), 110),
                                    (date(2025, 7, 1), 120)])
        assert det.projected_amount == 130   # strictly monotonic -> extrapolate

    def test_month_length_clamp_and_sunday_roll(self):
        det = Detected(key="K", period="monthly", anchor=31, amount_class="fixed",
                       occurrences=[(date(2025, 5, 31), 100)])
        out = project([det], date(2025, 6, 25), 10)
        # June has 30 days; 2025-06-30 is a Monday -> due 30 June
        assert out == [{"key": "K", "due_date": "2025-06-30", "amount_paise": 100,
                        "basis": "fixed", "period": "monthly"}]


class TestBooksRule:
    """Known money first: the GST obligation's next amount comes from the
    merchant's own captured payments (the compliance loop's rule), not from
    extrapolating history."""

    def _gst_rows(self):
        return [bank_row(f"G{i}", date(2025, m, 20), debit=amt,
                         narration=f"GST PAYMENT-CBIC-{40000 + i}",
                         balance=5_000_000)
                for i, (m, amt) in enumerate([(4, 41_000), (5, 44_000),
                                              (6, 39_000)])]

    def test_gst_next_amount_comes_from_captured_gross(self):
        payments = [{"payment_id": f"pay_{i}", "status": "captured",
                     "created_at": f"2025-06-{d:02d}T10:00:00",
                     "amount_paise": a}
                    for i, (d, a) in enumerate([(3, 400_000), (17, 350_000),
                                                (28, 250_000)])]
        payments.append({"payment_id": "pay_failed", "status": "failed",
                         "created_at": "2025-06-09T10:00:00",
                         "amount_paise": 999_999})      # never counts
        det = detect(inp_with(bank_rows=self._gst_rows(), payments=payments))
        assert len(det) == 1
        assert det[0].rule_amount_paise == TX.gst_liability(1_000_000) == 30_000
        assert det[0].projected_amount == 30_000
        out = project(det, CUTOFF, 14)
        # 2025-07-20 is a Sunday -> due Monday the 21st
        assert out == [{"key": det[0].key, "due_date": "2025-07-21",
                        "amount_paise": 30_000, "basis": "rule",
                        "period": "monthly"}]

    def test_without_payments_history_is_used(self):
        det = detect(inp_with(bank_rows=self._gst_rows()))
        assert len(det) == 1 and det[0].rule_amount_paise is None
        assert det[0].projected_amount == 41_000      # median of the last 3
        assert project(det, CUTOFF, 14)[0]["basis"] == "stable"

    def test_rule_applies_only_to_gst_keys(self):
        rows = [bank_row(f"R{i}", date(2025, m, 5), debit=5_500_000,
                         narration=f"NEFT DR-URBAN LADDER RENT-{70000 + i}")
                for i, m in enumerate([4, 5, 6])]
        payments = [{"payment_id": "p", "status": "captured",
                     "created_at": "2025-06-10T10:00:00",
                     "amount_paise": 1_000_000}]
        det = detect(inp_with(bank_rows=rows, payments=payments))
        assert len(det) == 1 and det[0].rule_amount_paise is None


class TestResidual:
    def test_trimmed6_mean(self):
        assert C._trimmed6_mean([1, 2, 3, 4, 5, 6, 7, 8]) == 27 // 6
        assert C._trimmed6_mean([5]) == 5
        assert C._trimmed6_mean([]) == 0

    def test_trend_clamps(self):
        series_up = {date(2025, 7, 15) - __import__("datetime").timedelta(days=i):
                     (1_000 if i < 28 else 100) for i in range(56)}
        assert C.trend_ratio_x1000(series_up, CUTOFF) == 1250
        series_down = {date(2025, 7, 15) - __import__("datetime").timedelta(days=i):
                       (100 if i < 28 else 1_000) for i in range(56)}
        assert C.trend_ratio_x1000(series_down, CUTOFF) == 800

    def test_variable_spend_excludes_recurring_rows(self):
        from datetime import timedelta
        rows = []
        # populate six of the trailing Tuesdays so the trimmed mean is nonzero
        for i in range(6):
            tuesday = date(2025, 7, 15) - timedelta(days=7 * (i + 1))
            rows.append(bank_row(f"B{i}", tuesday, debit=100_000, narration="N-1"))
            rows.append(bank_row(f"R{i}", tuesday, debit=900_000, narration="R-1"))
        inp = inp_with(bank_rows=rows)
        recurring_ids = {f"R{i}" for i in range(6)}
        with_excl = C.project_variable_spend(inp, 7, recurring_ids)
        without = C.project_variable_spend(inp, 7, set())
        assert sum(with_excl.values()) < sum(without.values())
        assert sum(with_excl.values()) > 0


@pytest.fixture(scope="module")
def world_dir(tmp_path_factory):
    from recon.generate import generate
    d = str(tmp_path_factory.mktemp("w"))
    generate(9, d, days=180)
    return d


class TestComposedOnGeneratedWorld:

    def test_bands_present_and_monotone(self, world_dir):
        from datetime import timedelta
        from recon.generate import START_DATE
        from forecast.slicing import build_input, load_world
        inp = build_input(load_world(world_dir), START_DATE + timedelta(days=120))
        res = forecast(inp, 14)
        widths = [d.hi80 - d.lo80 for d in res.days]
        assert all(w >= 0 for w in widths)
        assert widths == sorted(widths)     # monotone envelope
        assert res.calibration is not None
        assert not res.warnings

    def test_insufficient_history_degrades_honestly(self, world_dir):
        from datetime import timedelta
        from recon.generate import START_DATE
        from forecast.slicing import build_input, load_world
        inp = build_input(load_world(world_dir), START_DATE + timedelta(days=70))
        res = forecast(inp, 14)
        assert any("bands unavailable" in w for w in res.warnings)
        assert all(d.lo80 is None for d in res.days)

    def test_deterministic(self, world_dir):
        from datetime import timedelta
        from recon.generate import START_DATE
        from forecast.slicing import build_input, load_world
        inp = build_input(load_world(world_dir), START_DATE + timedelta(days=120))
        assert forecast(inp, 14).to_dict() == forecast(inp, 14).to_dict()
