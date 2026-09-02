"""Explanations are presentation-only: they must never alter a decision."""

from recon import io_load, schemas as S
from recon.engine import reconcile_leg_a
from recon.explain import attach_explanations
from recon.generate import generate


def _decisions(data_dir):
    return reconcile_leg_a(io_load.load_settlements(data_dir),
                           io_load.load_bank_rows(data_dir))


def test_explanations_do_not_change_gradable_content(tmp_path):
    data_dir = str(tmp_path / "w")
    generate(17, data_dir)

    plain = _decisions(data_dir)
    explained = _decisions(data_dir)
    mode = attach_explanations(explained, use_llm=True)  # mock mode -> templates

    assert S.decisions_grading_view(plain) == S.decisions_grading_view(explained)
    assert mode == "templates"  # no key configured in tests
    assert all(d.explanation for d in explained)


def test_every_status_renders(tmp_path):
    data_dir = str(tmp_path / "w")
    generate(17, data_dir)
    ds = _decisions(data_dir)
    attach_explanations(ds)
    seen = {d.status for d in ds}
    # the generated world exercises the full leg A vocabulary
    assert seen >= {S.MATCHED, S.MATCHED_SPLIT, S.MATCHED_MERGED,
                    S.MATCHED_WITH_DISCREPANCY, S.DUPLICATE_CREDIT,
                    S.AMBIGUOUS_ABSTAIN, S.EXCEPTION_MISSING_BANK,
                    S.EXCEPTION_MISSING_SETTLEMENT, S.NON_SETTLEMENT_CREDIT,
                    S.OUT_OF_SCOPE}
    for d in ds:
        assert isinstance(d.explanation, str) and len(d.explanation) > 10
