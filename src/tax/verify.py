"""Independent verification of tax decisions. NO shared code with the engine:
raw CSVs are re-read with this module's own parsers, and every published rule
(inclusive split, liabilities, heads, deadlines, vendor knowledge) is
re-implemented here from its published definition, so the engine and its
checker can only agree if both are right.

Any violation hard-fails the benchmark run.
"""

from __future__ import annotations

import csv
import os
from datetime import date, timedelta

from tax import schemas as TS

# --- Own domain knowledge (re-typed from the published conventions) -----------

_MERCHANT_STATE = "29"
_GST_DUE_DAY, _TDS_DUE_DAY = 20, 7

# marker -> (eligible_now, files_2b). Independent copy of the vendor master.
_VENDOR_POSTURE = {
    "AMAZON WEB SERVICES": (True, True),
    "BHARTI AIRTEL": (True, True),
    "URBAN LADDER RENT": (True, True),
    "COURIER DTDC": (True, True),
    "VISTAPRINT": (True, True),
    "BIG BAZAAR": (True, True),
    "BLUEDART EXPRESS": (True, True),
    "SWIGGY INSTAMART": (False, True),      # Sec 17(5) blocked
    "MAKEMYTRIP": (False, True),            # Sec 17(5) blocked
    "LIC PREMIUM": (False, False),
    "INDIAN OIL": (False, False),
    "FREELANCE DESIGN": (False, False),
}
_OBLIGATION_MARKERS = ("STAFF PAYROLL", "GST PAYMENT-CBIC", "TDS PAYMENT-CBDT")


def _own_paise(s: str) -> int:
    s = s.strip().replace(",", "")
    if not s:
        return 0
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    return sign * (int(whole or "0") * 100 + int(frac or "0"))


def _own_split_heads(gst: int, head: str) -> tuple[int, int, int]:
    """Re-typed head split -> (igst, cgst, sgst). IGST never splits;
    intra-state halves with the odd paisa on SGST. Must agree with
    tax.rules.split_heads — enforced by test, never by import."""
    if head == "igst":
        return gst, 0, 0
    half = gst >> 1
    return 0, half, gst - half


def _own_split_gst(total: int) -> int:
    return total - (total * 100 + 59) // 118


def _own_marker(narration: str) -> str | None:
    up = narration.upper()
    for m in _OBLIGATION_MARKERS:
        if m in up:
            return None
    for m in _VENDOR_POSTURE:
        if m in up:
            return m
    return None


def _own_roll(d: date) -> date:
    return d + timedelta(days=1) if d.weekday() == 6 else d


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


_DUP_STATUSES = {TS.DUPLICATE_2B_LINE, TS.TDS_DUPLICATE_26AS}
_LINKED_L1 = TS.CLAIMABLE_NOW | {TS.ITC_DEFERRED_NEXT_PERIOD,
                                 TS.BLOCKED_CREDIT_NO_ITC}


def verify_tax(data_dir: str, decisions: list) -> list[str]:
    v: list[str] = []
    rows = [d.to_dict() if hasattr(d, "to_dict") else dict(d)
            for d in decisions]

    bank = _read(os.path.join(data_dir, "bank_statement.csv"))
    gstr2b = _read(os.path.join(data_dir, "gstr2b.csv"))
    form26as = _read(os.path.join(data_dir, "form26as.csv"))
    settlements = _read(os.path.join(data_dir, "settlements.csv"))
    payments = _read(os.path.join(data_dir, "payments.csv"))

    debit_by_txn = {r["txn_id"]: r for r in bank
                    if _own_paise(r["debit_amount"]) > 0}
    line_by_id = {r["line_id"]: r for r in gstr2b}
    entry_by_id = {r["entry_id"]: r for r in form26as}
    setl_ids = {r["settlement_id"] for r in settlements}

    # ---- 1. Partition: every filed record decided exactly once ---------------
    filed_seen: dict[str, int] = {}
    book_seen_primary: dict[str, int] = {}
    for i, d in enumerate(rows):
        for fid in d["filed_ids"]:
            if d["loop"] in ("itc", "tds"):
                filed_seen[fid] = filed_seen.get(fid, 0) + 1
        if d["status"] in _DUP_STATUSES:
            continue
        for bid in d["book_ids"]:
            book_seen_primary[bid] = book_seen_primary.get(bid, 0) + 1
    for lid in line_by_id:
        if filed_seen.get(lid, 0) != 1:
            v.append(f"2B line {lid} decided {filed_seen.get(lid, 0)} times")
    for eid in entry_by_id:
        if filed_seen.get(eid, 0) != 1:
            v.append(f"26AS entry {eid} decided {filed_seen.get(eid, 0)} times")
    for txn, r in debit_by_txn.items():
        if _own_marker(r["narration"]) is not None:
            n = book_seen_primary.get(txn, 0)
            if n != 1:
                v.append(f"vendor debit {txn} has {n} primary decisions")

    # loop-2 book ids must be real settlements
    for d in rows:
        if d["loop"] == "tds":
            for bid in d["book_ids"]:
                if bid not in setl_ids:
                    v.append(f"tds decision references unknown settlement {bid}")

    # ---- 2. Arithmetic -------------------------------------------------------
    fee_tax_by_period: dict[str, int] = {}
    for s in settlements:
        period = s["created_at"][:7]
        fee_tax_by_period[period] = (fee_tax_by_period.get(period, 0)
                                     + int(s["tax_paise"]))

    for i, d in enumerate(rows):
        breakdown = d.get("discrepancy_breakdown") or []
        if breakdown:
            total = sum(b.get("amount_paise", 0) for b in breakdown)
            if total != d.get("discrepancy_paise"):
                v.append(f"decision {i} ({d['status']}): breakdown sums to "
                         f"{total}, discrepancy says {d['discrepancy_paise']}")
        if (d["loop"] == "itc" and d["status"] in _LINKED_L1
                | {TS.ITC_MISSING_IN_2B}):
            for bid in d["book_ids"]:
                if bid in debit_by_txn:
                    own = _own_split_gst(
                        _own_paise(debit_by_txn[bid]["debit_amount"]))
                    if d.get("books_paise") != own:
                        v.append(f"decision {i}: books gst "
                                 f"{d.get('books_paise')} != {own} "
                                 f"re-derived from debit {bid}")
                elif bid.startswith("RZPFEE-"):
                    own = fee_tax_by_period.get(bid[len("RZPFEE-"):], -1)
                    if d.get("books_paise") != own:
                        v.append(f"decision {i}: fee-invoice gst "
                                 f"{d.get('books_paise')} != {own} "
                                 f"re-summed from settlements")
        if (d["status"] in (TS.ITC_AMOUNT_MISMATCH, TS.TDS_AMOUNT_MISMATCH)
                and d.get("books_paise") is not None
                and d.get("filed_paise") is not None):
            if d["discrepancy_paise"] != d["filed_paise"] - d["books_paise"]:
                v.append(f"decision {i}: discrepancy != filed - books")

    # ---- 3. Claim safety -----------------------------------------------------
    for i, d in enumerate(rows):
        if d["loop"] != "itc":
            continue
        if (d["status"] in TS.CLAIMABLE_NOW | {TS.ITC_DEFERRED_NEXT_PERIOD}
                and (not d["book_ids"] or not d["filed_ids"])):
            v.append(f"decision {i} ({d['status']}): linked verdict without "
                     f"both sides")
        if d["status"] in TS.CLAIMABLE_NOW:
            for fid in d["filed_ids"]:
                line = line_by_id.get(fid)
                if line is None:
                    v.append(f"decision {i} claims unknown line {fid}")
                    continue
            for bid in d["book_ids"]:
                row = debit_by_txn.get(bid)
                if row is None:
                    if not bid.startswith("RZPFEE-"):
                        v.append(f"decision {i} claims unknown purchase {bid}")
                    continue
                marker = _own_marker(row["narration"])
                posture = _VENDOR_POSTURE.get(marker or "", (False, False))
                if not posture[0]:
                    v.append(f"decision {i} claims credit on ineligible "
                             f"spend {bid} ({marker})")

    # ---- 4. Obligations: recompute everything ourselves ----------------------
    stmt_dates = sorted(date(int(r["value_date"][6:]), int(r["value_date"][3:5]),
                             int(r["value_date"][:2])) for r in bank)
    pay_dates = sorted(date(int(p["created_at"][:4]), int(p["created_at"][5:7]),
                            int(p["created_at"][8:10])) for p in payments)
    start = min(stmt_dates[0], pay_dates[0])

    gross: dict[int, int] = {}
    first30 = 0
    for p in payments:
        if p["status"] != "captured":
            continue
        d0 = date(int(p["created_at"][:4]), int(p["created_at"][5:7]),
                  int(p["created_at"][8:10]))
        m = (d0.year - start.year) * 12 + d0.month - start.month
        gross[m] = gross.get(m, 0) + int(p["amount_paise"])
        if (d0 - start).days < 30:
            first30 += int(p["amount_paise"])

    payroll: dict[int, int] = {}
    paid: dict[tuple[str, int], tuple[int, date, str]] = {}
    horizon = start
    for r in bank:
        amt = _own_paise(r["debit_amount"])
        if amt <= 0:
            continue
        up = r["narration"].upper()
        d0 = date(int(r["value_date"][6:]), int(r["value_date"][3:5]),
                  int(r["value_date"][:2]))
        horizon = max(horizon, d0)
        m = (d0.year - start.year) * 12 + d0.month - start.month
        if "STAFF PAYROLL" in up and m not in payroll:
            payroll[m] = amt
        elif "GST PAYMENT-CBIC" in up and ("gst", m) not in paid:
            paid[("gst", m)] = (amt, d0, r["txn_id"])
        elif "TDS PAYMENT-CBDT" in up and ("tds", m) not in paid:
            paid[("tds", m)] = (amt, d0, r["txn_id"])

    def own_expected(kind: str, m: int) -> int | None:
        if kind == "gst":
            g = first30 if m == 0 else gross.get(m - 1, 0)
            return (g * 3 // 100) // 1_000 * 1_000
        if m == 0 or (m - 1) not in payroll:
            return None
        return (payroll[m - 1] // 10) // 100 * 100

    # A period is gradeable only when its deadline sits inside the observed
    # DEBIT horizon (straggler credits past it never open a phantom period).
    n_months = ((horizon.year - start.year) * 12
                + horizon.month - start.month) + 1
    obligation_decisions = {d["book_ids"][0]: d for d in rows
                            if d["loop"] == "obligation" and d["book_ids"]}
    gradeable: set[str] = set()
    for m in range(n_months):
        y = start.year + (start.month - 1 + m) // 12
        mo = (start.month - 1 + m) % 12 + 1
        period = f"{y:04d}-{mo:02d}"
        for kind, due_day in (("gst", _GST_DUE_DAY), ("tds", _TDS_DUE_DAY)):
            if _own_roll(date(y, mo, due_day)) > horizon:
                continue
            gradeable.add(f"{kind}:{period}")
    for rid in sorted(set(obligation_decisions) - gradeable):
        v.append(f"decision for {rid}, whose deadline is outside the "
                 f"observed window")
    for m in range(n_months):
        y = start.year + (start.month - 1 + m) // 12
        mo = (start.month - 1 + m) % 12 + 1
        period = f"{y:04d}-{mo:02d}"
        for kind, due_day in (("gst", _GST_DUE_DAY), ("tds", _TDS_DUE_DAY)):
            if f"{kind}:{period}" not in gradeable:
                continue
            d = obligation_decisions.get(f"{kind}:{period}")
            if d is None:
                v.append(f"no decision for period {kind}:{period}")
                continue
            expected = own_expected(kind, m)
            got = paid.get((kind, m))
            if expected is None:
                want = TS.UNVERIFIABLE_PRIOR_PERIOD
            elif got is None:
                want = TS.NOT_PAID
            elif got[0] != expected:
                want = TS.PAID_SHORT
            elif got[1] > _own_roll(date(y, mo, due_day)):
                want = TS.PAID_LATE
            else:
                want = TS.PAID_ON_TIME
            if d["status"] != want:
                v.append(f"{kind}:{period}: engine says {d['status']}, "
                         f"independent recompute says {want}")

    # ---- 5. Evidence audit ---------------------------------------------------
    for i, d in enumerate(rows):
        rules = {e.get("rule") for e in d.get("evidence", [])}
        conf = d.get("confidence")
        if conf == TS.CONF_EXACT and not ({"invoice_exact", "amount_paid_exact"}
                                          & rules):
            v.append(f"decision {i}: confidence 'exact' without identity "
                     f"evidence ({sorted(rules)})")
        if conf == TS.CONF_MEDIUM and "vendor_period_singleton" not in rules:
            v.append(f"decision {i}: confidence 'medium' without singleton "
                     f"evidence")
    return v
