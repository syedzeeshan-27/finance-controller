"""The deterministic gate on every match the resolver agent proposes.

The agent proposes; this module disposes. It shares no code with the agent
(`agent.resolve`) or the engine: it imports only the standard library, reads
the raw world files with its own parsers, and never sees a golden file. A
proposal that fails any rule is rejected and its item stays in the human
queue. The agent never sees the verdict and never gets a retry.

Rules for a `match` proposal (codes in brackets). The brief's minimum set:

  R1 [shape]       verdict/ids/deduction/evidence well-formed, ids unique
  R2 [unknown_id]  every id exists; bank rows are credits
  R3 [item]        the queue item's own records are part of the proposal
  R4 [claimed]     no record is already claimed (engine auto-matches)
  R5 [many_to_many] one credit <-> N settlements, or one settlement <-> N credits
  R6 [arithmetic]  d = sum(settlement nets) - sum(credits) equals the stated
                   deduction_paise exactly, and d >= 0
  R7 [evidence]    when d > 0: the quoted evidence is a verbatim substring
                   (whitespace-collapsed) of a proposed credit's narration or
                   reference, and a money figure of that narration (read in
                   the full narration's context, grammar below) equal to d lies
                   inside the quoted span; when d = 0 the evidence is ignored
  R8 [window]      every settlement/credit pair: 0 <= value date - settlement
                   created date <= 10 days

Stricter rules (on by default; `strict=False` scores the brief-minimum set):

  R9  [provenance]    each proposed credit carries a PSP marker, a UTR-shaped
                      token, or an identifier of a proposed settlement
  R10 [contradiction] a proposed credit names a settlement outside the
                      proposal: its settlement id, its exact UTR or a 10+
                      character fragment of it, a merchant name no proposed
                      settlement has, or a well-formed UTR 3+ edits away from
                      every proposed UTR
  R11 [rival]         an unclaimed settlement with the same net (or an unclaimed
                      credit with the same amount) also fits the window, and the
                      credit does not carry an identifier that separates the
                      proposal from that rival
  R12 [conflict]      (batch) two otherwise-accepted proposals share a record:
                      both are rejected

Money-figure grammar (R7), fixed before any agent run: ASCII digits; optional
currency prefix INR / Rs / Rs. / the rupee sign, adjacent or one space away;
digit groups separated by commas (Indian or Western grouping) or none; at most
two decimals; optional suffix "/-", "Dr" or "Cr". A figure is NOT a money
figure when it is glued to letters or digits (refs, UTRs, "194O"), followed
by "%", part of a date (d/m/y, d-m-y, d.m.y, next to a month name) or a time
(hh:mm). A deduction built from several figures ("CHG 25.00 GST 4.50") is
rejected: the brief requires the deduction figure itself to appear verbatim.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date

WINDOW_DAYS = 10
PSP_MARKERS = ("RAZORPAY", "RZPAY", "RZP")
VERDICTS = ("match", "exception", "abstain")
MIN_UTR_FRAGMENT = 10
MIN_PROVENANCE_FRAGMENT = 8
FOREIGN_UTR_MIN_EDITS = 3

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
           "OCT", "NOV", "DEC")
_UTR_TOKEN_RE = re.compile(r"[A-Z]{4}[0-9]{12}")
_ALNUM_RUN_RE = re.compile(r"[A-Z0-9]+")
_NUMBER_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?", re.ASCII)
_GROUPED_RE = re.compile(r"[0-9]{1,3}(?:,[0-9]{2,3})+(?:\.[0-9]{1,2})?")
_PLAIN_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_SETTLEMENT_ID_RE = re.compile(r"setl_[A-Za-z0-9]+")


# --- own parsers ---------------------------------------------------------------

def _paise(s: str) -> int:
    s = (s or "").strip().replace('"', "").replace(",", "")
    if not s:
        return 0
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    whole, _, frac = s.partition(".")
    return sign * (int(whole or "0") * 100 + int((frac + "00")[:2]))


def _bank_date(ddmmyyyy: str) -> date:
    dd, mm, yyyy = ddmmyyyy.strip().split("/")
    return date(int(yyyy), int(mm), int(dd))


def _iso_date(s: str) -> date:
    y, m, d = s.strip()[:10].split("-")
    return date(int(y), int(m), int(d))


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def collapse_ws(s: str) -> str:
    """Whitespace runs (incl. non-breaking spaces) -> one space, trimmed."""
    return " ".join((s or "").split())


def _canon(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _edits(a: str, b: str) -> int:
    """Levenshtein distance (tiny strings only)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# --- the world, as the verifier sees it -----------------------------------------

@dataclass
class WorldView:
    settlements: dict[str, dict]      # id -> {net, utr, created}
    credits: dict[str, dict]          # txn_id -> {amount, value_date, narration, ref}
    debits: frozenset[str]
    merchants: dict[str, str]         # settlement id -> merchant name (may be empty)


def load_world_view(world_dir: str) -> WorldView:
    settlements = {}
    for r in _read(os.path.join(world_dir, "settlements.csv")):
        settlements[r["settlement_id"]] = {
            "net": int(r["amount_paise"]), "utr": r["utr"].strip().upper(),
            "created": _iso_date(r["created_at"])}
    credits, debits = {}, set()
    for r in _read(os.path.join(world_dir, "bank_statement.csv")):
        credit = _paise(r["credit_amount"])
        if credit > 0:
            credits[r["txn_id"]] = {
                "amount": credit, "value_date": _bank_date(r["value_date"]),
                "narration": r["narration"], "ref": r["ref_no"]}
        else:
            debits.add(r["txn_id"])
    merchants = {}
    sidecar = os.path.join(world_dir, "settlement_merchants.csv")
    if os.path.exists(sidecar):
        for r in _read(sidecar):
            merchants[r["settlement_id"]] = r["merchant_name"].strip()
    return WorldView(settlements, credits, frozenset(debits), merchants)


# --- money figures ----------------------------------------------------------------

def _glued(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _is_date_or_time_context(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 2):start]
    after = text[end:end + 2]
    for sep in "/-.:":
        if len(after) == 2 and after[0] == sep and after[1].isdigit():
            return True
        if len(before) == 2 and before[1] == sep and before[0].isdigit():
            return True
    next_word = text[end:].lstrip(" -").upper()
    prev_word = text[:start].rstrip(" -").upper()
    for mon in _MONTHS:
        if next_word.startswith(mon) and not _glued(next_word[3:4]):
            return True
        if prev_word.endswith(mon) and not _glued(prev_word[-4:-3]):
            return True
    return False


def _has_currency_prefix(text: str, start: int) -> bool:
    """A currency prefix (INR, Rs, Rs., the rupee sign) directly before the
    number, or one space before it, and not itself glued to a word."""
    k = start - 1 if start > 0 and text[start - 1] == " " else start
    head = text[:k].upper()
    for pre in ("INR", "RS.", "RS", "₹"):
        if head.endswith(pre):
            p0 = k - len(pre)
            return p0 == 0 or not _glued(text[p0 - 1])
    return False


def money_figures(text: str) -> list[int]:
    """Every money figure in `text`, in paise, per the published grammar."""
    return [value for value, _, _ in money_figure_spans(text)]


def money_figure_spans(text: str) -> list[tuple[int, int, int]]:
    """(paise, start, end) for every money figure in `text`."""
    out: list[tuple[int, int, int]] = []
    for m in _NUMBER_RE.finditer(text):
        tok = m.group().rstrip(",")
        start, end = m.start(), m.start() + len(tok)
        # left: nothing glued on, unless it is a currency prefix
        if start > 0 and _glued(text[start - 1]) and not _has_currency_prefix(text, start):
            continue
        # right: no percent, nothing glued on except a Dr/Cr suffix
        right = text[end:]
        if right[:1] == "%":
            continue
        if right[:1] and _glued(right[0]):
            if not (right[:2].upper() in ("DR", "CR") and not _glued(right[2:3] or " ")):
                continue
        if _is_date_or_time_context(text, start, end):
            continue
        if "," in tok:
            if not _GROUPED_RE.fullmatch(tok):
                continue
        elif not _PLAIN_RE.fullmatch(tok):
            continue
        out.append((_paise(tok), start, end))
    return out


def figure_in_quote(source: str, quote: str, paise: int) -> bool:
    """True when `quote` occurs in `source` (both whitespace-collapsed) and a
    money figure OF THE SOURCE equal to `paise` lies inside that occurrence.
    Figures are read in the context of the whole narration, so quoting a
    slice of a reference ("0001234" out of "UTIB0001234") manufactures
    nothing."""
    src, q = collapse_ws(source), collapse_ws(quote)
    if not q:
        return False
    spans = money_figure_spans(src)
    i = src.find(q)
    while i >= 0:
        if any(v == paise and s >= i and e <= i + len(q) for v, s, e in spans):
            return True
        i = src.find(q, i + 1)
    return False


# --- identifiers ----------------------------------------------------------------------

def _utr_fragments(utr: str, n: int) -> set[str]:
    return {utr[i:i + n] for i in range(0, len(utr) - n + 1)} if len(utr) >= n else set()


def _mentions_settlement_id(blob_upper: str, sid: str) -> bool:
    return sid.upper() in blob_upper


def _carries_identifier(view: WorldView, blob: str, sid: str,
                        min_fragment: int) -> bool:
    """Does the credit text carry an identifier of settlement `sid`?"""
    s = view.settlements[sid]
    up = blob.upper()
    canon = _canon(blob)
    if _mentions_settlement_id(up, sid):
        return True
    if s["utr"] and s["utr"] in canon:
        return True
    if any(f in canon for f in _utr_fragments(s["utr"], min_fragment)):
        return True
    name = view.merchants.get(sid, "")
    return bool(name) and collapse_ws(name).upper() in collapse_ws(up)


# --- verdicts --------------------------------------------------------------------------

@dataclass
class Verdict:
    accepted: bool
    codes: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    status: str = ""                  # derived status when accepted as a match
    deduction_paise: int | None = None

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "codes": self.codes,
                "details": self.details, "status": self.status,
                "deduction_paise": self.deduction_paise}


def _fail(v: Verdict, code: str, detail: str) -> None:
    v.accepted = False
    if code not in v.codes:
        v.codes.append(code)
    v.details.append(f"{code}: {detail}")


def _in_window(view: WorldView, sid: str, tid: str) -> bool:
    delta = (view.credits[tid]["value_date"]
             - view.settlements[sid]["created"]).days
    return 0 <= delta <= WINDOW_DAYS


def verify_proposal(view: WorldView, proposal: dict, item_records: list[str],
                    claimed: set[str] | frozenset[str],
                    strict: bool = True) -> Verdict:
    v = Verdict(accepted=True)
    verdict = proposal.get("verdict")
    if verdict not in VERDICTS:
        _fail(v, "shape", f"verdict {verdict!r} not one of {VERDICTS}")
        return v
    if verdict != "match":
        v.accepted = False          # not a resolution: the item stays with a human
        v.codes.append("no_match_proposed")
        return v

    sids = proposal.get("settlement_ids")
    tids = proposal.get("bank_row_ids")
    ded = proposal.get("deduction_paise")
    evidence = proposal.get("deduction_evidence")
    if (not isinstance(sids, list) or not sids
            or not all(isinstance(x, str) for x in sids)
            or not isinstance(tids, list) or not tids
            or not all(isinstance(x, str) for x in tids)
            or isinstance(ded, bool) or not isinstance(ded, int)
            or not isinstance(evidence, str)
            or not isinstance(proposal.get("reason"), str)):
        _fail(v, "shape", "ids must be non-empty string lists, deduction an "
                          "integer, evidence and reason strings")
        return v
    if len(set(sids)) != len(sids) or len(set(tids)) != len(tids):
        _fail(v, "shape", "duplicate ids in the proposal")
        return v

    # R2 existence
    for sid in sids:
        if sid not in view.settlements:
            _fail(v, "unknown_id", f"no settlement {sid}")
    for tid in tids:
        if tid not in view.credits:
            _fail(v, "unknown_id", f"{tid} is not a bank credit")
    if not v.accepted:
        return v

    # R3 the item's own records
    missing = [r for r in item_records if r not in set(sids) | set(tids)]
    if missing:
        _fail(v, "item", f"proposal leaves out the queue item's records {missing}")

    # R4 claimed
    taken = sorted((set(sids) | set(tids)) & set(claimed))
    if taken:
        _fail(v, "claimed", f"already claimed: {taken}")

    # R5 one-to-many
    if len(sids) > 1 and len(tids) > 1:
        _fail(v, "many_to_many", "only 1 credit <-> N settlements or "
                                 "1 settlement <-> N credits is verifiable")

    # R6 arithmetic
    d = (sum(view.settlements[s]["net"] for s in sids)
         - sum(view.credits[t]["amount"] for t in tids))
    v.deduction_paise = d
    if d != ded:
        _fail(v, "arithmetic", f"settlements minus credits is {d} paise but the "
                               f"proposal states a deduction of {ded}")
    if d < 0:
        _fail(v, "arithmetic", f"credits exceed the settlements by {-d} paise")

    # R7 evidence
    if d > 0:
        quote = collapse_ws(evidence)
        if not quote:
            _fail(v, "evidence", "a deduction needs a quoted narration span")
        else:
            sources = [view.credits[t][k] for t in tids for k in ("narration", "ref")]
            if not any(quote in collapse_ws(src) for src in sources):
                _fail(v, "evidence", "quoted span is not verbatim in any "
                                     "proposed credit's narration or reference")
            elif not any(figure_in_quote(src, quote, d) for src in sources):
                _fail(v, "evidence", f"the deduction {d} paise does not appear "
                                     "as a money figure in the quoted span")

    # R8 window
    for sid in sids:
        for tid in tids:
            if not _in_window(view, sid, tid):
                _fail(v, "window", f"{tid} is outside 0..{WINDOW_DAYS} days "
                                   f"of {sid}'s creation")

    if strict and v.accepted:
        _strict_rules(view, v, sids, tids, claimed)

    if v.accepted:
        if len(sids) > 1:
            v.status = "matched_merged"
        elif len(tids) > 1:
            v.status = "matched_split"
        else:
            v.status = "matched_with_discrepancy" if d else "matched"
    return v


def _strict_rules(view: WorldView, v: Verdict, sids: list[str],
                  tids: list[str], claimed) -> None:
    proposed = set(sids)
    names_all = {collapse_ws(n).upper() for n in view.merchants.values() if n}
    proposed_names = {collapse_ws(view.merchants.get(s, "")).upper() for s in sids}
    proposed_utrs = [view.settlements[s]["utr"] for s in sids]

    for tid in tids:
        c = view.credits[tid]
        blob = f"{c['narration']} {c['ref']}"
        up = collapse_ws(blob.upper())
        canon = _canon(blob)
        tokens = _ALNUM_RUN_RE.findall(blob.upper())

        # R9 provenance
        marker = any(m in up for m in PSP_MARKERS)
        utr_shaped = any(_UTR_TOKEN_RE.fullmatch(t) for t in tokens)
        identified = any(_carries_identifier(view, blob, s, MIN_PROVENANCE_FRAGMENT)
                         for s in sids)
        if not (marker or utr_shaped or identified):
            _fail(v, "provenance", f"{tid} carries no PSP marker, UTR or "
                                   "identifier of a proposed settlement")

        # R10 contradiction
        for other in _SETTLEMENT_ID_RE.findall(blob):
            match = next((s for s in view.settlements
                          if s.upper() == other.upper()), None)
            if match is not None and match not in proposed:
                _fail(v, "contradiction", f"{tid} names settlement {match}")
        for sid, s in view.settlements.items():
            if sid in proposed or not s["utr"]:
                continue
            if s["utr"] in canon:
                _fail(v, "contradiction", f"{tid} carries the UTR of {sid}")
                continue
            frags = [f for f in _utr_fragments(s["utr"], MIN_UTR_FRAGMENT)
                     if f in canon]
            if frags and not any(f in u for f in frags for u in proposed_utrs):
                _fail(v, "contradiction", f"{tid} carries a fragment of {sid}'s UTR")
        for name in sorted(names_all):
            if name and name in up and name not in proposed_names:
                _fail(v, "contradiction", f"{tid} names merchant {name!r}, "
                                          "which no proposed settlement has")
        for t in tokens:
            if _UTR_TOKEN_RE.fullmatch(t) and proposed_utrs and all(
                    _edits(t, u) >= FOREIGN_UTR_MIN_EDITS for u in proposed_utrs):
                _fail(v, "contradiction", f"{tid} carries UTR-shaped token {t} "
                                          "far from every proposed UTR")

    # R11 rivals, both directions
    claimed = set(claimed)
    for sid in sids:
        net = view.settlements[sid]["net"]
        rivals = [r for r, s in view.settlements.items()
                  if r not in proposed and r not in claimed and s["net"] == net
                  and all(_in_window(view, r, t) for t in tids)]
        for r in sorted(rivals):
            if not _separated(view, tids, sid, r):
                _fail(v, "rival", f"{r} has the same net as {sid} and fits the "
                                  "window; nothing in the credit separates them")
    for tid in tids:
        amount = view.credits[tid]["amount"]
        rivals = [r for r, c in view.credits.items()
                  if r not in tids and r not in claimed and c["amount"] == amount
                  and all(_in_window(view, s, r) for s in sids)]
        for r in sorted(rivals):
            mine = any(_carries_identifier(
                view, f"{view.credits[tid]['narration']} {view.credits[tid]['ref']}",
                s, MIN_UTR_FRAGMENT) for s in sids)
            theirs = any(_carries_identifier(
                view, f"{view.credits[r]['narration']} {view.credits[r]['ref']}",
                s, MIN_UTR_FRAGMENT) for s in sids)
            if not mine or theirs:
                _fail(v, "rival", f"credit {r} has the same amount as {tid} and "
                                  "fits the window; nothing separates them")


def _separated(view: WorldView, tids: list[str], sid: str, rival: str) -> bool:
    """True when some proposed credit identifies `sid` and not `rival`."""
    for tid in tids:
        c = view.credits[tid]
        blob = f"{c['narration']} {c['ref']}"
        up = collapse_ws(blob.upper())
        canon = _canon(blob)
        s, r = view.settlements[sid], view.settlements[rival]
        if _mentions_settlement_id(up, sid):
            return True
        if s["utr"] and s["utr"] in canon:
            return True
        own = {f for f in _utr_fragments(s["utr"], MIN_UTR_FRAGMENT) if f in canon}
        if own and not any(f in r["utr"] for f in own):
            return True
        name_s = view.merchants.get(sid, "").upper()
        name_r = view.merchants.get(rival, "").upper()
        if name_s and name_s != name_r and name_s in up:
            return True
    return False


def verify_batch(view: WorldView, proposals: list[tuple[list[str], dict]],
                 claimed, strict: bool = True) -> list[Verdict]:
    """Verify every (item_records, proposal) pair, then reject any accepted
    proposals that share a record (R12)."""
    verdicts = [verify_proposal(view, p, items, claimed, strict=strict)
                for items, p in proposals]
    owners: dict[str, list[int]] = {}
    for i, (verdict, (_, p)) in enumerate(zip(verdicts, proposals)):
        if not verdict.accepted:
            continue
        for rec in list(p["settlement_ids"]) + list(p["bank_row_ids"]):
            owners.setdefault(rec, []).append(i)
    for rec, idxs in sorted(owners.items()):
        if len(idxs) > 1:
            for i in idxs:
                verdicts[i].status = ""
                _fail(verdicts[i], "conflict",
                      f"{rec} is also claimed by another accepted proposal")
    return verdicts
