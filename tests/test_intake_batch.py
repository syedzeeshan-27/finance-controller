"""Batch intake: folder walk, bank detection, attempt counting (1/2/3/failed),
golden role grading, and the report, with a scripted agent."""

import json
import os

import pytest

from agent import intake_batch as B
from agent.gemini import GeminiLiveTransport

CSV = ("Bank: HDFC BANK LTD,,,,,\n"
       "Date,Narration,Ref,Debit,Credit,Balance\n"
       ",Opening Balance,,,,1000.00\n"
       "01/05/2025,UPI/123456789012/SHOP,,100.00,,900.00\n"
       "02/05/2025,NEFT CR RAZORPAY,,,500.00,1400.00\n"
       ",Closing Balance,,,,1400.00\n")

GOOD = {"header_row": 1,
        "columns": {"date": 0, "narration": 1, "debit": 3, "credit": 4,
                    "balance": 5, "drcr_indicator": None, "chq_ref": 2},
        "date_format": "%d/%m/%Y",
        "periods": [{"opening_row": 2, "closing_row": 5, "transaction_rows": [3, 4]}],
        "noise_rows": [0],
        "reference_recipe": {"kind": "none", "delimiter": "", "index": 0,
                             "token_regex": ""},
        "reversal_pairs": []}
SWAPPED = json.loads(json.dumps(GOOD))
SWAPPED["columns"].update({"debit": 4, "credit": 3})


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "stmts"
    (d / "world").mkdir(parents=True)
    (d / "golden").mkdir()
    (d / "hdfc_1.csv").write_text(CSV, encoding="utf-8")
    (d / "world" / "bank_statement.csv").write_text("ignored", encoding="utf-8")
    (d / "golden" / "hdfc_1.json").write_text(json.dumps(
        {"header_row": 1, "columns": dict(GOOD["columns"], amount=None),
         "periods": [{"opening_row": 2, "closing_row": 5, "transaction_rows": [3, 4]}]}),
        encoding="utf-8")
    return str(d)


def _reply(mapping):
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "submit_mapping", "args": {"mapping": mapping}}}]},
        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 120}}


def _factory(mappings):
    replies = [_reply(m) for m in mappings]

    def make(path, task=""):
        return GeminiLiveTransport(path, task=task, model_name="gemini-3.5-flash-lite",
                                   api_key="k", rpm=0, post=lambda *a: replies.pop(0),
                                   sleep=lambda s: None)
    return make


def test_folder_walk_skips_world_and_golden(folder):
    files = [os.path.basename(p) for p in B.statement_files(folder)]
    assert files == ["hdfc_1.csv"]


def test_bank_detection():
    assert B.detect_bank([["Bank: HDFC BANK LTD"]], "x.csv") == "HDFC Bank"
    assert B.detect_bank([["IFSC: UTIB0001234"]], "x.csv") == "Axis Bank"
    assert B.detect_bank([["nothing"]], "kotak_2.xlsx") == "Kotak Mahindra Bank"
    assert B.detect_bank([["nothing"]], "statement.xlsx") == "unknown"


def test_bank_detection_ignores_counterparty_banks_in_narrations():
    grid = [["KOTAK MAHINDRA BANK LIMITED"], ["Date", "Description"],
            ["01-03-25", "NEFT CR-UTIB0RZP001-RAZORPAY SOFTWARE PVT LTD"],
            ["02-03-25", "IMPS from AXIS BANK customer"]]
    assert B.detect_bank(grid, "x.xlsx") == "Kotak Mahindra Bank"


def test_layout_limits_come_from_the_golden_file():
    assert B.layout_limits({"columns": {"amount": None},
                            "periods": [{"opening_row": 3, "closing_row": 9}]}) == []
    assert B.layout_limits({"columns": {"amount": 3},
                            "periods": [{"opening_row": None, "closing_row": 9}]}) == [
        "one amount column with a Dr/Cr marker", "opening balance only in a summary line"]


@pytest.mark.parametrize("mappings,result,roles", [
    ([GOOD], "attempt 1", "all correct"),
    ([SWAPPED, GOOD], "attempt 2", "all correct"),
    ([SWAPPED, SWAPPED, SWAPPED], "failed", ""),
])
def test_attempts_and_roles(folder, mappings, result, roles):
    results, stopped = B.run_batch([], [folder], transport_factory=_factory(mappings))
    assert not stopped
    [r] = results
    assert r.bank == "HDFC Bank" and r.synthetic
    assert r.result == result
    assert r.roles == roles
    assert r.rows == 6 and r.layout == "expressible"
    if result == "failed":
        assert "E_BALANCE_CHAIN_BREAK" in r.reason and "3 attempt" in r.reason
    else:
        assert r.transactions == 2


def test_provider_failure_is_not_run_and_leaves_no_recording(folder):
    from agent.gemini import ProviderUnavailable

    def make(path, task=""):
        def post(*a):
            raise ProviderUnavailable("Gemini unreachable: timed out")
        return GeminiLiveTransport(path, task=task, model_name="gemini-3.5-flash-lite",
                                   api_key="k", rpm=0, post=post, sleep=lambda s: None)
    results, stopped = B.run_batch([folder], [], transport_factory=make)
    assert stopped == "provider"
    assert results[0].result == "not run" and "unreachable" in results[0].reason
    assert not os.path.exists(B._transcript_path(folder, B.statement_files(folder)[0]))


def test_offline_replay_and_report(folder):
    B.run_batch([folder], [], transport_factory=_factory([GOOD]))
    again, _ = B.run_batch([folder], [], offline=True)
    assert again[0].result == "attempt 1" and again[0].api_calls == 1
    md = B.render(again, "", [folder], [])
    assert "passed on attempt 1 / 2 / 3: 1 / 0 / 0" in md
    assert "Synthetic look-alikes" not in md


def test_role_grading_catches_swapped_narration():
    from agent.mapping import StatementMapping
    m = StatementMapping.from_dict(GOOD)
    golden = {"header_row": 1, "columns": dict(GOOD["columns"], narration=2),
              "periods": [{"transaction_rows": [3, 4]}]}
    assert B.grade_roles(m, golden) == ["narration column 1 (golden 2)"]
