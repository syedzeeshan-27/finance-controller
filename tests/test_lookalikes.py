"""Synthetic bank-statement look-alikes (data/lookalikes/): the committed files
match a fresh generation, every row is classified once, every balance chain
closes, and the corpus has the promised shape. Adapted from the tests the
look-alike author wrote in its sandbox (docs/provenance/lookalike_author_brief.md);
the balance check keeps the author's own cell parser, independent of the intake
validator."""

import glob
import importlib.util
import json
import os

import pytest

from agent.rawgrid import load_grid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOKALIKES_DIR = os.path.join(_ROOT, "data", "lookalikes")
GOLDEN_DIR = os.path.join(LOOKALIKES_DIR, "golden")


def _load_generator():
    path = os.path.join(_ROOT, "scripts", "make_lookalikes.py")
    spec = importlib.util.spec_from_file_location("make_lookalikes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def goldens():
    return sorted(glob.glob(os.path.join(GOLDEN_DIR, "*.json")))


def load_golden(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _files(root):
    out = set()
    for d, _dirs, files in os.walk(root):
        for name in files:
            out.add(os.path.relpath(os.path.join(d, name), root).replace(os.sep, "/"))
    return out


def parse_paise(s):
    """Independent parser: text cell -> signed integer paise, or None if blank."""
    s = s.strip()
    if s == "":
        return None
    for suf in (" Cr", " CR", " cr", " Dr", " DR", " dr", "Cr", "Dr", "CR", "DR"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    s = s.replace(",", "")
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if "." in s:
        whole, frac = s.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    val = int(whole) * 100 + int(frac)
    return -val if neg else val


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("lookalikes"))
    _load_generator().generate_all(d)
    return d


def test_committed_files_match_a_fresh_generation(fresh):
    assert _files(fresh) == _files(LOOKALIKES_DIR)
    for rel in sorted(_files(fresh)):
        a, b = os.path.join(fresh, rel), os.path.join(LOOKALIKES_DIR, rel)
        if rel.endswith(".xlsx"):
            # compare content: deflate output may differ across zlib builds
            assert load_grid(a) == load_grid(b), rel
        else:
            with open(a, "rb") as fa, open(b, "rb") as fb:
                assert fa.read() == fb.read(), rel


def test_regeneration_is_byte_identical(fresh, tmp_path):
    again = str(tmp_path / "again")
    _load_generator().generate_all(again)
    assert _files(again) == _files(fresh)
    for rel in _files(fresh):
        with open(os.path.join(fresh, rel), "rb") as fa, \
                open(os.path.join(again, rel), "rb") as fb:
            assert fa.read() == fb.read(), f"{rel} differs across two runs"


@pytest.mark.parametrize("golden_path", goldens())
def test_every_row_classified_exactly_once(golden_path):
    g = load_golden(golden_path)
    grid = load_grid(os.path.join(LOOKALIKES_DIR, g["file"]))
    classified = list(g["repeated_header_rows"]) + list(g["noise_rows"]) + [g["header_row"]]
    for period in g["periods"]:
        if period["opening_row"] is not None:
            classified.append(period["opening_row"])
        if period["closing_row"] is not None:
            classified.append(period["closing_row"])
        classified += period["transaction_rows"]
    assert len(classified) == len(set(classified)), "some row classified more than once"
    assert set(classified) == set(range(len(grid))), "rows left unclassified or out of range"


@pytest.mark.parametrize("golden_path", goldens())
def test_balance_chain_and_counts(golden_path):
    g = load_golden(golden_path)
    grid = load_grid(os.path.join(LOOKALIKES_DIR, g["file"]))
    cols = g["columns"]
    total_txns = total_credits = total_debits = 0
    for period in g["periods"]:
        running = period["opening_balance_paise"]
        for r in period["transaction_rows"]:
            row = grid[r]
            if cols["amount"] is not None:
                amt = parse_paise(row[cols["amount"]])
                is_credit = row[cols["drcr_indicator"]].strip().lower().startswith("c")
                signed = amt if is_credit else -amt
            else:
                credit = parse_paise(row[cols["credit"]]) if cols["credit"] is not None else None
                debit = parse_paise(row[cols["debit"]]) if cols["debit"] is not None else None
                signed = (credit or 0) - (debit or 0)
            running += signed
            if signed > 0:
                total_credits += signed
            else:
                total_debits += -signed
            total_txns += 1
            bal_cell = parse_paise(row[cols["balance"]])
            assert bal_cell == running, f"{g['file']} row {r}: balance {bal_cell} != running {running}"
        assert running == period["closing_balance_paise"], (
            f"{g['file']}: closing {period['closing_balance_paise']} != computed {running}")
        if period["opening_row"] is not None:
            assert parse_paise(grid[period["opening_row"]][cols["balance"]]) == \
                period["opening_balance_paise"]
        if period["closing_row"] is not None:
            assert parse_paise(grid[period["closing_row"]][cols["balance"]]) == \
                period["closing_balance_paise"]
    assert total_txns == g["transactions"]
    assert total_credits == g["credits_paise"]
    assert total_debits == g["debits_paise"]


def test_corpus_shape():
    gs = [load_golden(p) for p in goldens()]
    assert len(gs) == 15
    by_bank = {}
    for g in gs:
        by_bank.setdefault(g["bank"], []).append(g)
    assert sorted(by_bank) == ["Axis Bank", "HDFC Bank", "ICICI Bank",
                               "Kotak Mahindra Bank", "State Bank of India"]
    assert all(len(files) == 3 for files in by_bank.values())
    assert sum(g["file"].endswith(".xlsx") for g in gs) >= 9
    assert sum(g["file"].endswith(".csv") for g in gs) >= 3
    layouts = [tuple(load_grid(os.path.join(LOOKALIKES_DIR, g["file"]))[g["header_row"]])
               for g in gs]
    assert len(layouts) == len(set(layouts)), "two files share the exact same header layout"


def test_labelled_synthetic():
    with open(os.path.join(LOOKALIKES_DIR, "README.md"), encoding="utf-8") as f:
        assert "synthetic look-alikes, not real statements" in f.read()
