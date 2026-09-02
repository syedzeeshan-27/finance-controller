"""The independent tax verifier: passes clean output, catches every class of
corruption it exists to catch."""

import copy

import pytest

from recon.generate import generate
from tax import schemas as TS
from tax.engine import reconcile_tax
from tax.io_tax import build_tax_input
from tax.verify import verify_tax


@pytest.fixture(scope="module")
def world_dir(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("w"))
    generate(9, d, days=90)
    return d


@pytest.fixture(scope="module")
def clean(world_dir):
    return reconcile_tax(build_tax_input(world_dir))


def test_clean_engine_output_verifies(world_dir, clean):
    assert verify_tax(world_dir, clean) == []


def _tampered(clean, mutate):
    decisions = copy.deepcopy(clean)
    mutate(decisions)
    return decisions


def test_catches_books_arithmetic_tamper(world_dir, clean):
    def mutate(ds):
        d = next(x for x in ds if x.status == TS.ITC_MATCHED
                 and x.book_ids[0].startswith("BANK"))
        d.books_paise += 100
    v = verify_tax(world_dir, _tampered(clean, mutate))
    assert any("books gst" in x for x in v)


def test_catches_double_claimed_line(world_dir, clean):
    def mutate(ds):
        d = next(x for x in ds if x.status == TS.ITC_MATCHED)
        dd = copy.deepcopy(d)
        ds.append(dd)
    v = verify_tax(world_dir, _tampered(clean, mutate))
    assert any("decided 2 times" in x for x in v)


def test_catches_claim_on_blocked_spend(world_dir, clean):
    def mutate(ds):
        d = next(x for x in ds if x.status == TS.BLOCKED_CREDIT_NO_ITC
                 and x.filed_ids)
        d.status = TS.ITC_MATCHED
    v = verify_tax(world_dir, _tampered(clean, mutate))
    assert any("ineligible" in x for x in v)


def test_catches_compliance_verdict_flip(world_dir, clean):
    def mutate(ds):
        d = next(x for x in ds if x.status == TS.PAID_ON_TIME)
        d.status = TS.PAID_LATE
    v = verify_tax(world_dir, _tampered(clean, mutate))
    assert any("independent recompute says" in x for x in v)


def test_catches_breakdown_sum_tamper(world_dir, clean):
    def mutate(ds):
        d = next(x for x in ds if x.discrepancy_breakdown)
        d.discrepancy_breakdown[0]["amount_paise"] += 1
    v = verify_tax(world_dir, _tampered(clean, mutate))
    assert any("breakdown sums" in x for x in v)


def test_catches_missing_period_decision(world_dir, clean):
    def mutate(ds):
        idx = next(i for i, x in enumerate(ds)
                   if x.loop == "obligation" and x.status == TS.PAID_ON_TIME)
        del ds[idx]
    v = verify_tax(world_dir, _tampered(clean, mutate))
    assert any("no decision for period" in x or "obligation decisions" in x
               for x in v)
