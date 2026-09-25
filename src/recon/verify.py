"""Independent invariant checker for reconciliation output.

Deliberately shares NO code with the engine or the normalizer: it re-reads the
raw CSVs with its own tiny parsers and checks the engine's claims against
them. If the engine and this module disagree, the benchmark fails loudly.

Checked invariants:
  1. Partition — every settlement and every bank credit is referenced by
     exactly one decision; nothing is claimed twice; nothing is dropped.
  2. Arithmetic — for every matched-family decision: expected equals the sum
     of the linked settlements' amounts (from the raw CSV), received equals
     the sum of the linked credits' amounts, received - expected equals the
     reported discrepancy, and any breakdown sums to the discrepancy.
  3. Evidence audit — confidence tiers must be backed by the evidence class
     that defines them; no matched-family decision may have empty evidence.
  4. Exception completeness — ambiguous/exception decisions must present
     candidates (or an explicit no-candidates reason).
  5. Input sanity — the bank statement's running balance is continuous.

Usage:
    violations = verify_leg_a(data_dir, decisions_as_dicts)   # [] == clean
    python -m recon.verify data/seeds/42 out/decisions.json
"""

from __future__ import annotations

import csv
import json
import os
import sys

_MATCHED_FAMILY = {"matched", "matched_split", "matched_merged",
                   "matched_with_discrepancy"}
_NEEDS_CANDIDATES = {"ambiguous_abstain", "duplicate_credit",
                     "exception_missing_bank", "exception_missing_settlement"}

_TIER_EVIDENCE = {
    "exact": {"utr_exact"},
    "high": {"utr_fuzzy", "decomposed_discrepancy", "sum_exact_parts",
             "merge_residual_exact"},
    "medium": {"amount_exact_unique", "amount_within_rs1_and_3_days",
               "amount_exact_first_come"},
    "needs_review": set(),   # any non-empty evidence
}


def _own_date_ordinal(s: str) -> int | None:
    """Independent DD/MM/YYYY -> day ordinal (proleptic, own arithmetic)."""
    parts = s.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        day, month, year = (int(p) for p in parts)
        import datetime as _dt
        return _dt.date(year, month, day).toordinal()
    except ValueError:
        return None


def _own_paise(s: str) -> int:
    """Independent rupee-string parser (int arithmetic, no shared code)."""
    s = s.strip().strip('"').replace(",", "")
    if not s:
        return 0
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    value = int(whole) * 100 + int(frac)
    return -value if neg else value


def _read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def verify_leg_a(data_dir: str, decisions: list[dict]) -> list[str]:
    violations: list[str] = []

    settlements = _read_csv(os.path.join(data_dir, "settlements.csv"))
    bank = _read_csv(os.path.join(data_dir, "bank_statement.csv"))
    setl_amount = {r["settlement_id"]: int(r["amount_paise"]) for r in settlements}
    credit_amount = {r["txn_id"]: _own_paise(r["credit_amount"])
                     for r in bank if r["credit_amount"].strip()}

    # 5. input sanity: balance continuity (catches generator corruption early).
    # Real statement exports may concatenate disjoint periods (e.g. May and
    # July): a discontinuity is tolerated ONLY across a value-date gap wider
    # than 7 days — a period boundary, not a tampered row. Generated worlds
    # post rows near-daily, so corruption there still has no place to hide.
    prev_balance = None
    prev_ordinal = None
    for r in bank:
        credit = _own_paise(r["credit_amount"])
        debit = _own_paise(r["debit_amount"])
        balance = _own_paise(r["balance"])
        ordinal = _own_date_ordinal(r["value_date"])
        if prev_balance is not None and prev_balance + credit - debit != balance:
            gap = (ordinal - prev_ordinal
                   if None not in (ordinal, prev_ordinal) else 0)
            if gap <= 7:
                violations.append(
                    f"balance discontinuity at {r['txn_id']}: "
                    f"{prev_balance} + {credit} - {debit} != {balance}")
        prev_balance = balance
        prev_ordinal = ordinal

    # 1. partition
    seen_s: dict[str, int] = {}
    seen_c: dict[str, int] = {}
    for i, d in enumerate(decisions):
        for sid in d.get("settlement_ids", []):
            if sid not in setl_amount:
                violations.append(f"decision {i} references unknown settlement {sid}")
            seen_s[sid] = seen_s.get(sid, 0) + 1
        for tid in d.get("bank_txn_ids", []):
            if tid not in credit_amount:
                # decisions may reference debit rows only as out_of_scope
                if d.get("status") != "out_of_scope":
                    violations.append(
                        f"decision {i} references {tid} which is not a credit row")
                continue
            seen_c[tid] = seen_c.get(tid, 0) + 1
    for sid, n in seen_s.items():
        if n > 1:
            violations.append(f"settlement {sid} claimed by {n} decisions")
    for tid, n in seen_c.items():
        if n > 1:
            violations.append(f"bank credit {tid} claimed by {n} decisions")
    for sid in setl_amount:
        if sid not in seen_s:
            violations.append(f"settlement {sid} not covered by any decision")
    for tid in credit_amount:
        if tid not in seen_c:
            violations.append(f"bank credit {tid} not covered by any decision")

    # 2-4. per-decision checks
    for i, d in enumerate(decisions):
        status = d.get("status", "")
        if status in _MATCHED_FAMILY:
            exp = sum(setl_amount.get(s, 0) for s in d.get("settlement_ids", []))
            rec = sum(credit_amount.get(t, 0) for t in d.get("bank_txn_ids", []))
            if d.get("expected_paise") != exp:
                violations.append(
                    f"decision {i} ({status}): expected_paise {d.get('expected_paise')} "
                    f"!= sum of settlement amounts {exp}")
            if d.get("received_paise") != rec:
                violations.append(
                    f"decision {i} ({status}): received_paise {d.get('received_paise')} "
                    f"!= sum of credit amounts {rec}")
            if d.get("discrepancy_paise") != rec - exp:
                violations.append(
                    f"decision {i} ({status}): discrepancy {d.get('discrepancy_paise')} "
                    f"!= received - expected = {rec - exp}")
            breakdown = d.get("discrepancy_breakdown") or []
            if breakdown:
                total = sum(b.get("amount_paise", 0) for b in breakdown)
                if total != d.get("discrepancy_paise"):
                    violations.append(
                        f"decision {i} ({status}): breakdown sums to {total}, "
                        f"discrepancy is {d.get('discrepancy_paise')}")
            evidence_rules = {e.get("rule") for e in d.get("evidence") or []}
            if not evidence_rules:
                violations.append(f"decision {i} ({status}): matched with no evidence")
            tier = d.get("confidence", "")
            required = _TIER_EVIDENCE.get(tier)
            if required is None:
                violations.append(f"decision {i} ({status}): unknown confidence {tier!r}")
            elif required and not (evidence_rules & required):
                violations.append(
                    f"decision {i} ({status}): confidence '{tier}' lacks any of "
                    f"the qualifying evidence rules {sorted(required)}")
            if tier == "exact" and d.get("discrepancy_paise") != 0:
                violations.append(
                    f"decision {i}: confidence 'exact' with nonzero discrepancy")
        elif status in _NEEDS_CANDIDATES:
            if not d.get("candidates"):
                violations.append(
                    f"decision {i} ({status}): exception without candidates/reasons")

    return violations


def main() -> None:
    data_dir, decisions_path = sys.argv[1], sys.argv[2]
    with open(decisions_path, encoding="utf-8") as f:
        payload = json.load(f)
    decisions = payload["decisions"] if isinstance(payload, dict) else payload
    violations = verify_leg_a(data_dir, decisions)
    if violations:
        print(f"{len(violations)} violation(s):")
        for v in violations:
            print(f"  - {v}")
        raise SystemExit(1)
    print("verify: OK — all invariants hold")


if __name__ == "__main__":
    main()
