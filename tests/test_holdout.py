"""Held-out world invariants: if these hold, the holdout goldens are defensible."""

import csv
import os
import subprocess
import sys
from datetime import time, timedelta

import pytest

from recon import generate as G
from recon import holdout as H
from recon import io_load
from recon import schemas as S
from recon.normalize import (
    paise_from_optional_rupee_str, paise_from_rupee_str, parse_bank_date, roll_off_sunday,
)

ALL_SEEDS = H.DEV_SEEDS + H.HELDOUT_SEEDS
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def worlds():
    return {seed: H.build_holdout_world(seed) for seed in ALL_SEEDS}


@pytest.fixture(scope="module")
def written(worlds, tmp_path_factory):
    base = tmp_path_factory.mktemp("holdout")
    out = {}
    for seed, w in worlds.items():
        d = str(base / str(seed))
        H.write_holdout_world(w, d)
        out[seed] = d
    return out


def _stmt(world):
    return {r["txn_id"]: r for r in world["statement"]}


def _credit_paise(row):
    return paise_from_optional_rupee_str(row["credit_amount"])


def _case_rows(world):
    return [r for r in world["golden_a"] if r["scenario_tag"] in H.CASE_TAGS]


# --- interface / determinism ---------------------------------------------------------

def test_interface_constants():
    assert H.HOLDOUT_VERSION == "1.1.0"
    assert H.DEV_SEEDS == (1000, 1006)
    assert H.HELDOUT_SEEDS == (1001, 1002, 1003, 1004, 1005)
    assert set(H.CASE_TAGS) == set(H.TAG_FAMILY)
    assert H.FILLER_TAG not in H.CASE_TAGS and H.NOISE_TAG not in H.CASE_TAGS
    assert all(H.family_of(s) == "A" for s in H.DEV_SEEDS)
    assert all(H.family_of(s) == "B" for s in H.HELDOUT_SEEDS)


def test_manifest(worlds):
    for seed, w in worlds.items():
        m = w["manifest"]
        assert m["holdout_version"] == H.HOLDOUT_VERSION and m["seed"] == seed
        assert m["family"] == H.family_of(seed)
        assert m["role"] == ("dev" if seed in H.DEV_SEEDS else "heldout")
        assert m["counts"]["settlements"] == len(w["settlements"])
        assert m["counts"]["bank_rows"] == len(w["statement"])
        assert m["counts"]["golden_leg_a_rows"] == len(w["golden_a"])
        assert m["counts"]["case_rows"] == len(_case_rows(w))
        assert sum(m["case_row_scenarios"].values()) == m["counts"]["case_rows"]


def test_determinism_in_process(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    H.write_holdout_world(H.build_holdout_world(1001), a)
    H.write_holdout_world(H.build_holdout_world(1001), b)
    assert G._dir_digest(a) == G._dir_digest(b)


def test_determinism_across_processes(tmp_path):
    digests = []
    for hashseed in ("0", "4242"):
        out = str(tmp_path / f"h{hashseed}")
        env = dict(os.environ, PYTHONHASHSEED=hashseed, PYTHONUTF8="1",
                   PYTHONPATH=os.path.join(ROOT, "src"))
        subprocess.run([sys.executable, "-m", "recon.holdout", "--seed", "1003", "--out", out],
                       cwd=ROOT, env=env, check=True, capture_output=True)
        digests.append(G._dir_digest(out))
    local = str(tmp_path / "local")
    H.write_holdout_world(H.build_holdout_world(1003), local)
    assert digests[0] == digests[1] == G._dir_digest(local)


def test_files_use_lf_only(written):
    for d in written.values():
        for name in sorted(os.listdir(d)):
            with open(os.path.join(d, name), "rb") as f:
                assert b"\r" not in f.read(), name


# --- completeness / loading ----------------------------------------------------------

def test_golden_leg_a_complete(worlds):
    for w in worlds.values():
        keys = [(r["record_type"], r["record_id"]) for r in w["golden_a"]]
        assert len(keys) == len(set(keys)), "duplicate golden rows"
        credits = [r["txn_id"] for r in w["statement"] if r["credit_amount"]]
        debits = {r["txn_id"] for r in w["statement"] if r["debit_amount"]}
        setl_ids = {s.settlement_id for s in w["settlements"]}
        assert sorted(k[1] for k in keys if k[0] == S.RT_SETTLEMENT) == sorted(setl_ids)
        assert sorted(k[1] for k in keys if k[0] == S.RT_BANK_CREDIT) == sorted(credits)
        assert not debits & {k[1] for k in keys}
        credit_set = set(credits)
        for r in w["golden_a"]:
            cps = [x for x in r["counterparty_ids"].split(S.COUNTERPARTY_SEP) if x]
            assert r["expected_disposition"] in S.LEG_A_STATUSES
            pool = credit_set if r["record_type"] == S.RT_SETTLEMENT else setl_ids
            assert all(x in pool for x in cps), r


def test_worlds_load_and_balance_chain(written):
    for d in written.values():
        setls = io_load.load_settlements(d)
        rows = io_load.load_bank_rows(d)
        io_load.load_payments(d)
        io_load.load_orders(d)
        ga = io_load.load_golden(d, "A")
        io_load.load_golden(d, "B")
        assert setls and rows and ga
        bal = G.OPENING_BALANCE_PAISE
        prev = None
        for i, r in enumerate(rows, start=1):
            assert r.txn_id == f"BANK{i:06d}"
            assert (r.credit_paise > 0) != (r.debit_paise > 0)
            bal += r.credit_paise - r.debit_paise
            assert r.balance_paise == bal
            day = parse_bank_date(r.value_date)
            assert prev is None or day >= prev, "statement not sorted by date"
            prev = day


def test_settlement_merchants_cover_every_settlement(written, worlds):
    for seed, d in written.items():
        with open(os.path.join(d, "settlement_merchants.csv"), encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert list(rows[0]) == H.MERCHANT_COLUMNS
        ids = [r["settlement_id"] for r in rows]
        assert sorted(ids) == sorted(s.settlement_id for s in worlds[seed]["settlements"])
        assert 2 <= len({r["merchant_name"] for r in rows}) <= 4


# --- ground truth -----------------------------------------------------------------

def _groups(world):
    """Matched-family groups: frozenset of settlement ids -> credit txn ids."""
    groups = {}
    for r in world["golden_a"]:
        if r["record_type"] == S.RT_BANK_CREDIT and r["expected_disposition"] in S.MATCHED_FAMILY:
            key = frozenset(r["counterparty_ids"].split(S.COUNTERPARTY_SEP))
            groups.setdefault(key, []).append(r)
    return groups


def test_matched_groups_close_and_state_the_figure(worlds):
    for seed, w in worlds.items():
        setl = {s.settlement_id: s for s in w["settlements"]}
        stmt = _stmt(w)
        gold = {(r["record_type"], r["record_id"]): r for r in w["golden_a"]}
        for sids, credit_rows in _groups(w).items():
            nets = sum(setl[i].amount_paise for i in sids)
            received = sum(_credit_paise(stmt[c["record_id"]]) for c in credit_rows)
            discs = {c["expected_discrepancy_paise"] for c in credit_rows}
            discs |= {gold[(S.RT_SETTLEMENT, i)]["expected_discrepancy_paise"] for i in sids}
            assert len(discs) == 1, (seed, sids)
            disc = discs.pop()
            assert nets + disc == received, (seed, sids)
            for i in sids:   # every settlement of the group points back at exactly these credits
                g = gold[(S.RT_SETTLEMENT, i)]
                assert g["expected_disposition"] in S.MATCHED_FAMILY
                assert sorted(g["counterparty_ids"].split(S.COUNTERPARTY_SEP)) == \
                    sorted(c["record_id"] for c in credit_rows)
            for c in credit_rows:  # window rule
                vd = parse_bank_date(stmt[c["record_id"]]["value_date"])
                for i in sids:
                    assert 0 <= (vd - setl[i].created_at.date()).days <= H.WINDOW_DAYS
            if disc:
                figures = {H.fig("A", abs(disc)), H.fig("B", abs(disc))}
                narrs = " | ".join(stmt[c["record_id"]]["narration"] for c in credit_rows)
                assert any(f in narrs for f in figures), (seed, narrs, disc)


# --- no structural tells ------------------------------------------------------------

def test_no_structural_tells(worlds):
    for w in worlds.values():
        for s in w["settlements"]:
            assert s.created_at.time() == time(1, 15)
            assert s.settled_at == roll_off_sunday(s.created_at.date() + timedelta(days=1))
            assert s.settlement_id.startswith("setl_") and len(s.settlement_id) == 19
            assert s.utr[:4] in G.BANK_PREFIXES and len(s.utr) == 16 and s.utr[4:].isdigit()
        case_ids = {r["record_id"] for r in _case_rows(w) if r["record_type"] == S.RT_SETTLEMENT}
        order = [s.settlement_id for s in w["settlements"]]
        # case settlements are interleaved with filler, not a contiguous block
        pos = [i for i, sid in enumerate(order) if sid in case_ids]
        assert pos and pos[-1] - pos[0] + 1 > len(pos)


def _case_settlement_ids(world):
    return {r["record_id"] for r in _case_rows(world) if r["record_type"] == S.RT_SETTLEMENT}


def test_cases_occur_among_late_settlements(worlds):
    """(a) A late created_at must not mark a settlement as filler."""
    late_total = late_case = all_total = all_case = 0
    for seed, w in worlds.items():
        case_ids = _case_settlement_ids(w)
        last = max(s.created_at.date() for s in w["settlements"])
        late = [s for s in w["settlements"] if (last - s.created_at.date()).days < 15]
        assert any(s.settlement_id in case_ids for s in late), seed
        late_total += len(late)
        late_case += sum(s.settlement_id in case_ids for s in late)
        all_total += len(w["settlements"])
        all_case += len(case_ids)
    # pooled over all seeds, the late case share tracks the overall case share
    assert abs(late_case / late_total - all_case / all_total) <= 0.15


def test_statement_covers_every_settlement_window(worlds):
    for w in worlds.values():
        last = _last_statement_day(w)
        for s in w["settlements"]:
            assert s.created_at.date() + timedelta(days=H.WINDOW_DAYS) <= last


# --- case families ------------------------------------------------------------------

def _rows(world, tag, rt=None):
    return [r for r in world["golden_a"]
            if r["scenario_tag"] == tag and (rt is None or r["record_type"] == rt)]


def _text(row):
    return (row["narration"] + " | " + row["ref_no"]).upper()


def _no_real_utr(world, row):
    return not any(s.utr in _text(row) for s in world["settlements"])


def test_references_without_utr(worlds):
    for seed, w in worlds.items():
        fam, stmt = H.family_of(seed), _stmt(w)
        setl = {s.settlement_id: s for s in w["settlements"]}
        merchants = {m["settlement_id"]: m["merchant_name"] for m in w["settlement_merchants"]}
        for r in _rows(w, H.T_REF_SID, S.RT_BANK_CREDIT):
            row, sid = stmt[r["record_id"]], r["counterparty_ids"]
            assert r["expected_disposition"] == S.MATCHED and _no_real_utr(w, row)
            assert H.sid_text(fam, sid) in row["narration"]
            # exactly one settlement token (case-insensitive) is printed
            assert [x for x in setl if x[5:].upper() in _text(row)] == [sid]
        for r in _rows(w, H.T_REF_MERCHANT, S.RT_BANK_CREDIT):
            row, sid = stmt[r["record_id"]], r["counterparty_ids"]
            assert r["expected_disposition"] == S.MATCHED and _no_real_utr(w, row)
            assert H.merchant_text(fam, merchants[sid]) in row["narration"]
            amount = _credit_paise(row)
            assert [s.settlement_id for s in w["settlements"] if s.amount_paise == amount] == [sid]


def _closest_token(narration, utr):
    import re
    toks = [t for t in re.split(r"[-*/ ]+", narration) if t]
    return min(toks, key=lambda t: H.levenshtein(t, utr))


def test_damaged_utr_points_to_one_settlement(worlds):
    for seed, w in worlds.items():
        stmt = _stmt(w)
        setl = {s.settlement_id: s for s in w["settlements"]}
        rows = _rows(w, H.T_UTR_DAMAGED, S.RT_BANK_CREDIT)
        assert rows
        for r in rows:
            row, s = stmt[r["record_id"]], setl[r["counterparty_ids"]]
            assert s.utr not in _text(row)
            tok = _closest_token(row["narration"], s.utr)
            d = H.levenshtein(tok, s.utr)
            assert 2 <= d <= 3, (seed, tok, s.utr)
            assert all(H.levenshtein(tok, x.utr) >= d + 3 for x in w["settlements"] if x is not s)
            assert _credit_paise(row) == s.amount_paise


def _window(world, value_date):
    return [s for s in world["settlements"]
            if 0 <= (value_date - s.created_at.date()).days <= H.WINDOW_DAYS]


def _subsets_summing(world, amount, value_date, max_k):
    import itertools
    nets = [s.amount_paise for s in _window(world, value_date)]
    return sum(1 for k in range(1, max_k + 1)
               for c in itertools.combinations(nets, k) if sum(c) == amount)


def test_merged_three_plus_unique_subset(worlds):
    for seed, w in worlds.items():
        stmt = _stmt(w)
        rows = _rows(w, H.T_MERGED, S.RT_BANK_CREDIT)
        assert len(rows) == 2
        for r in rows:
            row = stmt[r["record_id"]]
            members = r["counterparty_ids"].split(S.COUNTERPARTY_SEP)
            assert r["expected_disposition"] == S.MATCHED_MERGED and 3 <= len(members) <= 4
            amount = _credit_paise(row)
            vd = parse_bank_date(row["value_date"])
            assert _subsets_summing(w, amount, vd, 5) == 1
            assert all(abs(s.amount_paise - amount) >= G.MIN_SEPARATION_PAISE
                       for s in w["settlements"])


def test_stated_deductions_are_explicit(worlds):
    tags = (H.T_DED_STATED, H.T_REFUND, H.T_CHARGEBACK)
    for seed, w in worlds.items():
        stmt = _stmt(w)
        for tag in tags:
            rows = _rows(w, tag, S.RT_BANK_CREDIT)
            assert rows, (seed, tag)
            for r in rows:
                assert r["expected_disposition"] == S.MATCHED_WITH_DISCREPANCY
                assert r["expected_discrepancy_paise"] < 0
                row = stmt[r["record_id"]]
                # the short credit itself matches no settlement (or subset) exactly
                vd = parse_bank_date(row["value_date"])
                assert _subsets_summing(w, _credit_paise(row), vd, 3) == 0
        # both the UTR and the no-UTR flavour of family 1 exist in every world
        ded = [stmt[r["record_id"]] for r in _rows(w, H.T_DED_STATED, S.RT_BANK_CREDIT)]
        assert any(_no_real_utr(w, row) for row in ded)
        assert not all(_no_real_utr(w, row) for row in ded)


def _evidence(world, row, s):
    merchants = {m["settlement_id"]: m["merchant_name"] for m in world["settlement_merchants"]}
    return H.evidence_for(row["narration"] + " | " + row["ref_no"], s.settlement_id, s.utr,
                          merchants[s.settlement_id])


def test_no_clue_twins_are_indistinguishable(worlds):
    """'Distinguishes' = H.evidence_for differs between the twins: a settlement id
    token, any 4+ character UTR fragment, or a merchant name the twins do not share."""
    for seed, w in worlds.items():
        stmt, gold = _stmt(w), {r["record_id"]: r for r in w["golden_a"]}
        setl = {s.settlement_id: s for s in w["settlements"]}
        rows = _rows(w, H.T_TWINS_NOCLUE, S.RT_BANK_CREDIT)
        assert len(rows) == 2
        for r in rows:
            row = stmt[r["record_id"]]
            a, b = (setl[i] for i in r["counterparty_ids"].split(S.COUNTERPARTY_SEP))
            assert r["expected_disposition"] == S.AMBIGUOUS_ABSTAIN and "truth:" in r["notes"]
            assert a.amount_paise == b.amount_paise == _credit_paise(row)
            ea, eb = _evidence(w, row, a), _evidence(w, row, b)
            assert ea == eb and not {"sid", "utr"} & set(ea), (seed, row["narration"])
            vd = parse_bank_date(row["value_date"])
            for s in (a, b):
                g = gold[s.settlement_id]
                assert g["expected_disposition"] == S.AMBIGUOUS_ABSTAIN
                assert g["counterparty_ids"] == r["record_id"] and "truth:" in g["notes"]
                assert 0 <= (vd - s.created_at.date()).days <= H.WINDOW_DAYS
                assert s.settled_at <= vd       # both twins were due by the credit date
            assert _subsets_summing(w, a.amount_paise, vd, 3) == 2
        # one of the two pairs prints the merchant both twins share
        assert any(_evidence(w, stmt[r["record_id"]], setl[r["counterparty_ids"].split(";")[0]])
                   == ["merchant"] for r in rows)


def test_clue_twins_clue_points_to_exactly_one(worlds):
    for seed, w in worlds.items():
        stmt, gold = _stmt(w), {r["record_id"]: r for r in w["golden_a"]}
        twins = [s for s in w["settlements"]
                 if gold[s.settlement_id]["scenario_tag"] == H.T_TWINS_CLUE]
        assert len(twins) == 4
        rows = _rows(w, H.T_TWINS_CLUE, S.RT_BANK_CREDIT)
        assert len(rows) == 3
        for r in rows:
            row = stmt[r["record_id"]]
            paid = next(s for s in twins if s.settlement_id == r["counterparty_ids"])
            other = next(s for s in twins if s is not paid and s.amount_paise == paid.amount_paise)
            assert r["expected_disposition"] == S.MATCHED
            assert _credit_paise(row) == paid.amount_paise
            assert _evidence(w, row, paid) and not _evidence(w, row, other), row["narration"]
        unpaid = [s for s in twins if gold[s.settlement_id]["expected_disposition"]
                  == S.EXCEPTION_MISSING_BANK]
        assert len(unpaid) == 1 and gold[unpaid[0].settlement_id]["counterparty_ids"] == ""


def _last_statement_day(world):
    return max(parse_bank_date(r["value_date"]) for r in world["statement"])


def test_never_paid_settlements_have_no_defensible_credit(worlds):
    for seed, w in worlds.items():
        stmt, last = _stmt(w), _last_statement_day(w)
        setl = {s.settlement_id: s for s in w["settlements"]}
        credits = [(parse_bank_date(r["value_date"]), _credit_paise(r))
                   for r in w["statement"] if r["credit_amount"]]
        missing = [r for r in w["golden_a"] if r["record_type"] == S.RT_SETTLEMENT
                   and r["expected_disposition"] == S.EXCEPTION_MISSING_BANK]
        assert {r["scenario_tag"] for r in missing} == {H.T_NEVER_PAID, H.T_TWINS_CLUE,
                                                        H.T_DED_UNSTATED}
        for r in missing:
            s = setl[r["record_id"]]
            assert r["counterparty_ids"] == ""
            # the whole 0..10 day window lies inside the statement
            assert s.created_at.date() + timedelta(days=H.WINDOW_DAYS) <= last
            if r["scenario_tag"] != H.T_TWINS_CLUE:   # a clue twin's twin credit is named
                assert not any(a == s.amount_paise and 0 <= (d - s.created_at.date()).days
                               <= H.WINDOW_DAYS for d, a in credits)


def test_orphan_credits_belong_to_no_settlement(worlds):
    import re
    for seed, w in worlds.items():
        stmt = _stmt(w)
        tags = (H.T_ORPHAN, H.T_NEAR_NET, H.T_DED_UNSTATED)
        for tag in tags:
            rows = _rows(w, tag, S.RT_BANK_CREDIT)
            assert rows, (seed, tag)
            for r in rows:
                row = stmt[r["record_id"]]
                assert r["expected_disposition"] == S.EXCEPTION_MISSING_SETTLEMENT
                assert r["counterparty_ids"] == "" and r["expected_discrepancy_paise"] == 0
                amount, vd = _credit_paise(row), parse_bank_date(row["value_date"])
                assert _subsets_summing(w, amount, vd, 3) == 0
                for tok in re.findall(r"[A-Z]{4}\d{12}", _text(row)):
                    assert all(H.levenshtein(tok, s.utr) >= 6 for s in w["settlements"])
                assert not any(s.utr in _text(row) for s in w["settlements"])
                if tag == H.T_ORPHAN:
                    assert re.search(r"[A-Z]{4}\d{12}", row["narration"])
                    assert all(abs(s.amount_paise - amount) >= 5_000 for s in w["settlements"])
                else:   # tempting: one net is close, every other net is >= Rs 50 away
                    dist = sorted(abs(s.amount_paise - amount) for s in w["settlements"])
                    assert 0 < dist[0] <= max(H._CHARGE_TABLE) and dist[1] >= 5_000
                    assert "truth" in r["notes"] or tag == H.T_NEAR_NET


def test_outward_returns_mirror_an_earlier_debit(worlds):
    for seed, w in worlds.items():
        stmt = _stmt(w)
        rows = _rows(w, H.T_RETURN, S.RT_BANK_CREDIT)
        assert len(rows) == 2
        debits = [r for r in w["statement"] if r["debit_amount"]]
        for r in rows:
            row = stmt[r["record_id"]]
            assert r["expected_disposition"] == S.NON_SETTLEMENT_CREDIT and r["counterparty_ids"] == ""
            amount, vd = _credit_paise(row), parse_bank_date(row["value_date"])
            twin = [d for d in debits if row["ref_no"] in d["narration"]
                    and paise_from_rupee_str(d["debit_amount"]) == amount]
            assert len(twin) == 1
            assert 1 <= (vd - parse_bank_date(twin[0]["value_date"])).days <= 4
            assert _subsets_summing(w, amount, vd, 3) == 0
            assert all(H.levenshtein(row["ref_no"], s.utr) >= 6 for s in w["settlements"])


# --- template families ------------------------------------------------------------

def _literal(template):
    import re
    return re.sub(r"\{\w+\}", "{}", template)


def test_template_families_are_disjoint_statically():
    a = {_literal(t) for ts in H._TPL["A"].values() for t in ts}
    a |= {_literal(t) for t in H._FILLER_TPL["A"]}
    b = {_literal(t) for ts in H._TPL["B"].values() for t in ts}
    b |= {_literal(t) for t in H._FILLER_TPL["B"]}
    assert set(H._TPL["A"]) == set(H._TPL["B"]), "same case kinds in both families"
    assert not a & b
    generator = {_literal(t) for t in G._NARRATION_TEMPLATES} | {G._NO_UTR_TEMPLATE}
    assert not b & generator
    for t in b:   # the brief's family-1 example wordings are reserved for family A
        assert "CHGS" not in t.upper() and "194O" not in t.upper() and "TDS" not in t.upper()


def _skeleton(narration):
    import re
    t = narration.upper()
    for name in H._MERCHANT_POOL:
        for rendering in H.merchant_renderings(name):
            t = t.replace(rendering, "<M>")
    for vendor in H._VENDORS:
        t = t.replace(vendor, "<V>")
    t = re.sub(r"[A-Z0-9_]*\d[A-Z0-9_]*", "#", t)    # ids, UTRs, figures, counts
    return re.sub(r"\s+", " ", t).strip()


def test_rendered_case_narrations_share_no_skeleton(worlds):
    skel = {"A": set(), "B": set()}
    for seed, w in worlds.items():
        stmt = _stmt(w)
        for r in _case_rows(w):
            if r["record_type"] == S.RT_BANK_CREDIT:
                skel[H.family_of(seed)].add(_skeleton(stmt[r["record_id"]]["narration"]))
    assert skel["A"] and skel["B"]
    assert not skel["A"] & skel["B"], sorted(skel["A"] & skel["B"])


# --- volume / coverage ---------------------------------------------------------------

def test_heldout_volume_and_family_coverage(worlds):
    total, families = 0, set()
    for seed in H.HELDOUT_SEEDS:
        rows = _case_rows(worlds[seed])
        assert 30 <= len(rows) <= 50, (seed, len(rows))
        total += len(rows)
        families |= {H.TAG_FAMILY[r["scenario_tag"]] for r in rows}
    assert total >= 150
    assert families == set(range(1, 10))
    for seed in H.DEV_SEEDS:     # dev seeds carry every family too
        assert {H.TAG_FAMILY[r["scenario_tag"]] for r in _case_rows(worlds[seed])} == \
            set(range(1, 10))


def test_merchant_names_only_where_a_case_prints_them(worlds):
    allowed = {H.T_REF_MERCHANT, H.T_TWINS_CLUE, H.T_TWINS_NOCLUE}
    for seed, w in worlds.items():
        tag = {r["record_id"]: r["scenario_tag"] for r in w["golden_a"]}
        for row in w["statement"]:
            text = _text(row)
            named = [n for n in H._MERCHANT_POOL
                     if any(x in text for x in H.merchant_renderings(n))]
            if named:
                assert row["credit_amount"] and tag[row["txn_id"]] in allowed, row["narration"]


# --- hygiene --------------------------------------------------------------------------

def test_tags_known_and_filler_clean(worlds):
    for seed, w in worlds.items():
        stmt = _stmt(w)
        setl = {s.settlement_id: s for s in w["settlements"]}
        for r in w["golden_a"]:
            tag = r["scenario_tag"]
            assert tag in H.CASE_TAGS or tag in (H.FILLER_TAG, H.NOISE_TAG), tag
            if tag == H.FILLER_TAG and r["record_type"] == S.RT_BANK_CREDIT:
                s, row = setl[r["counterparty_ids"]], stmt[r["record_id"]]
                assert r["expected_disposition"] == S.MATCHED
                assert s.utr in row["narration"] and _credit_paise(row) == s.amount_paise
            if tag == H.NOISE_TAG:
                assert r["expected_disposition"] == S.NON_SETTLEMENT_CREDIT


def test_real_utr_only_in_credits_linked_to_its_settlement(worlds):
    for seed, w in worlds.items():
        gold = {r["record_id"]: r for r in w["golden_a"] if r["record_type"] == S.RT_BANK_CREDIT}
        for row in w["statement"]:
            if not row["credit_amount"]:
                continue
            linked = gold[row["txn_id"]]["counterparty_ids"].split(S.COUNTERPARTY_SEP)
            for s in w["settlements"]:
                if s.utr in _text(row):
                    assert s.settlement_id in linked, (seed, row["narration"])


def _link_delays(world):
    """(is_case, days after settled_at) for every golden settlement -> credit link."""
    stmt, setl = _stmt(world), {s.settlement_id: s for s in world["settlements"]}
    out = []
    for r in world["golden_a"]:
        if r["record_type"] == S.RT_SETTLEMENT and r["counterparty_ids"]:
            s = setl[r["record_id"]]
            for t in r["counterparty_ids"].split(S.COUNTERPARTY_SEP):
                vd = parse_bank_date(stmt[t]["value_date"])
                out.append((r["scenario_tag"] in H.CASE_TAGS, (vd - s.settled_at).days))
    return out


def _late_share(delays):
    return sum(d > 0 for d in delays) / len(delays)


def test_credit_delays_do_not_reveal_cases(worlds):
    """(b) Case and filler credits land after settled_at equally often."""
    pooled = {True: [], False: []}
    credit_late = {True: [], False: []}
    for seed, w in worlds.items():
        links = _link_delays(w)
        per = {cls: [d for c, d in links if c == cls] for cls in (True, False)}
        for cls, delays in per.items():
            assert delays and min(delays) >= 0
            pooled[cls] += delays
        assert _late_share(per[False]) >= 0.10, "filler credits are delayed too"
        assert abs(_late_share(per[True]) - _late_share(per[False])) <= 0.15, seed
        # per credit: late when it lands after the settled_at of every linked settlement
        setl = {s.settlement_id: s for s in w["settlements"]}
        stmt = _stmt(w)
        for r in w["golden_a"]:
            if r["record_type"] == S.RT_BANK_CREDIT and r["counterparty_ids"]:
                due = max(setl[i].settled_at for i in r["counterparty_ids"].split(";"))
                vd = parse_bank_date(stmt[r["record_id"]]["value_date"])
                credit_late[r["scenario_tag"] in H.CASE_TAGS].append((vd - due).days)
    assert abs(_late_share(pooled[True]) - _late_share(pooled[False])) <= 0.10
    assert abs(_late_share(credit_late[True]) - _late_share(credit_late[False])) <= 0.10
    # delays are drawn from the same range for both classes
    assert set(pooled[True]) <= set(pooled[False])
