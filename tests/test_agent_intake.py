"""Statement intake: the validator's proof, apply determinism, quarantine."""

import copy
import json
import os

import pytest

from agent.apply import apply_mapping, write_world
from agent.cells import parse_amount_cell
from agent.mapping import StatementMapping
from agent.rawgrid import _coerce, load_grid
from agent.validator import validate_mapping

_FIXTURE = os.path.join("data", "real", "statement_a", "statement.xlsx")
_MAPPING = os.path.join("data", "real", "statement_a", "world",
                        "mapping.json")


def _grid():
    return load_grid(_FIXTURE)


def _mapping_dict():
    with open(_MAPPING, encoding="utf-8") as f:
        return json.load(f)


def _codes(report):
    return {e["code"] for e in report.errors}


# --- raw cells ---------------------------------------------------------------

def test_rawgrid_coercion_rules():
    import datetime
    assert _coerce(None) == ""
    assert _coerce(10) == "10"
    assert _coerce(10.0) == "10"
    assert _coerce(7881.25) == "7881.25"
    assert _coerce(datetime.datetime(2026, 5, 1)) == "01-05-2026"
    assert _coerce(" x ") == "x"


def test_amount_cell_parses_bank_flavours():
    assert parse_amount_cell("1,23,456.78 Cr") == (12345678, "CR")
    assert parse_amount_cell("500.5 Dr") == (50050, "DR")
    assert parse_amount_cell("(500.00)") == (-50000, None)
    assert parse_amount_cell("10") == (1000, None)
    with pytest.raises(ValueError):
        parse_amount_cell("INT")
    with pytest.raises(ValueError):
        parse_amount_cell("")


# --- the validator's proof ---------------------------------------------------

def test_validator_accepts_committed_mapping():
    report = validate_mapping(_grid(), StatementMapping.from_dict(
        _mapping_dict()))
    assert report.accepted, report.errors
    assert all(p["chain_ok"] for p in report.stats["periods"])
    assert report.stats["transaction_rows"] == 48
    assert report.stats["tokens_extracted"] == 47   # the Int.Pd row has none


def test_validator_rejects_swapped_debit_credit_columns():
    d = _mapping_dict()
    d["columns"]["debit"], d["columns"]["credit"] = (
        d["columns"]["credit"], d["columns"]["debit"])
    report = validate_mapping(_grid(), StatementMapping.from_dict(d))
    assert not report.accepted
    assert "E_BALANCE_CHAIN_BREAK" in _codes(report)


def test_validator_rejects_wrong_balance_column():
    d = _mapping_dict()
    d["columns"]["balance"] = d["columns"]["chq_ref"]
    d["columns"]["chq_ref"] = None
    report = validate_mapping(_grid(), StatementMapping.from_dict(d))
    assert not report.accepted


def test_validator_rejects_hidden_transaction_row():
    d = _mapping_dict()
    row = d["periods"][0]["transaction_rows"].pop()   # hide the last txn
    d["noise_rows"].append(row)
    report = validate_mapping(_grid(), StatementMapping.from_dict(d))
    assert not report.accepted
    assert "E_CLOSING_MISMATCH" in _codes(report)
    assert "E_TRANSACTION_LIKE_NOISE" in _codes(report)


def test_validator_rejects_unclassified_and_double_classified_rows():
    d = _mapping_dict()
    dropped = d["noise_rows"].pop()
    report = validate_mapping(_grid(), StatementMapping.from_dict(d))
    assert "E_ROW_UNCLASSIFIED" in _codes(report)

    d = _mapping_dict()
    d["noise_rows"].append(d["periods"][0]["transaction_rows"][0])
    report = validate_mapping(_grid(), StatementMapping.from_dict(d))
    assert "E_ROW_DOUBLE_CLASSIFIED" in _codes(report)
    assert dropped is not None


def test_validator_rejects_broken_reversal_pair():
    d = _mapping_dict()
    a, b = d["reversal_pairs"][0]
    d["reversal_pairs"] = [[a, b + 1]]   # a different debit, not the reversal
    report = validate_mapping(_grid(), StatementMapping.from_dict(d))
    assert "E_REVERSAL_PAIR" in _codes(report)


def test_validator_reports_period_gap_as_warning_not_error():
    report = validate_mapping(_grid(), StatementMapping.from_dict(
        _mapping_dict()))
    assert any(w["code"] == "W_PERIOD_GAP" for w in report.warnings)


# --- apply + round trip ------------------------------------------------------

def test_apply_mapping_byte_deterministic(tmp_path):
    grid = _grid()
    mapping = StatementMapping.from_dict(_mapping_dict())
    rows_a = apply_mapping(grid, mapping)
    rows_b = apply_mapping(copy.deepcopy(grid), mapping)
    assert rows_a == rows_b
    for sub in ("a", "b"):
        write_world(str(tmp_path / sub), rows_a)
    bytes_a = (tmp_path / "a" / "bank_statement.csv").read_bytes()
    bytes_b = (tmp_path / "b" / "bank_statement.csv").read_bytes()
    assert bytes_a == bytes_b


def test_canonical_output_round_trips_io_load_and_reproves_chain(tmp_path):
    from agent.intake import intake
    result = intake(_FIXTURE, str(tmp_path / "world"),
                    mapping_path=_MAPPING, mode="hand-mapped")
    assert result.canonical_rows == 48
    from recon import io_load
    typed = io_load.load_bank_rows(str(tmp_path / "world"))
    assert len(typed) == 48
    assert typed[0].ref_no == "612124918501"


def test_intake_replay_reproduces_committed_world(tmp_path):
    """No API key, no network: the committed transcript replays the recorded
    live agent run byte-for-byte (request hashes checked) and must rebuild
    exactly the committed canonical statement."""
    from agent.intake import intake
    from agent.provider import ReplayTransport
    transcript = os.path.join("data", "agent_transcripts", "intake",
                              "statement_a.jsonl")
    result = intake(_FIXTURE, str(tmp_path / "world"),
                    transport=ReplayTransport(transcript), mode="replay")
    assert result.canonical_rows == 48
    committed = os.path.join("data", "real", "statement_a", "world",
                             "bank_statement.csv")
    with open(committed, "rb") as f:
        want = f.read()
    assert (tmp_path / "world" / "bank_statement.csv").read_bytes() == want


def test_committed_world_matches_committed_mapping(tmp_path):
    """The committed canonical world IS the committed mapping applied."""
    from agent.intake import intake
    intake(_FIXTURE, str(tmp_path / "world"), mapping_path=_MAPPING)
    committed = os.path.join("data", "real", "statement_a", "world",
                             "bank_statement.csv")
    with open(committed, "rb") as f:
        want = f.read()
    assert (tmp_path / "world" / "bank_statement.csv").read_bytes() == want


# --- quarantine: the agent proposes, it never decides ------------------------

_DECISION_PACKAGES = ("recon", "controller")


def test_no_decision_module_imports_agent():
    src = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src")
    offenders = []
    for pkg in _DECISION_PACKAGES:
        for root, _dirs, files in os.walk(os.path.join(src, pkg)):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                if "import agent" in text or "from agent" in text:
                    offenders.append(path)
    assert offenders == []


def test_seed_world_decisions_identical_with_agent_package_present(tmp_path):
    from recon import io_load, schemas as S
    from recon.engine import reconcile_leg_a
    from recon.generate import generate
    data_dir = str(tmp_path / "w")
    generate(17, data_dir)
    before = S.decisions_grading_view(reconcile_leg_a(
        io_load.load_settlements(data_dir), io_load.load_bank_rows(data_dir)))
    import agent.intake  # noqa: F401  — importing the agent layer changes nothing
    after = S.decisions_grading_view(reconcile_leg_a(
        io_load.load_settlements(data_dir), io_load.load_bank_rows(data_dir)))
    assert before == after
