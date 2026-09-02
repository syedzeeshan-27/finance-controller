"""Deterministic parsing and money arithmetic. No floats, anywhere.

This module is the single source of truth for:
  - rupee-string <-> integer-paise conversion (bank CSVs carry decimal strings;
    everything in memory is integer paise, so "amounts equal" is an exact
    integer predicate — the property the near-collision scenarios test)
  - GST-on-fee rounding (one rule, shared by the generator and the engine's
    discrepancy decomposition, so the two can never disagree by a paisa)
  - date/timestamp parsing for the two formats in the data
  - UTR canonicalisation and token extraction from bank narrations
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

GST_RATE_PCT = 18  # GST on PSP fees, integer percent

_UTR_RE = re.compile(r"^[A-Z]{4}\d{12}$")   # bank code prefix + 12 digits = 16 chars
_ALNUM_RUN_RE = re.compile(r"[A-Z0-9]+")


# --- Money -------------------------------------------------------------------

def paise_from_rupee_str(s: str) -> int:
    """Parse a rupee decimal string ('1,52,340.50', '4500', '-2950.00') to
    integer paise without ever constructing a float."""
    s = s.strip().replace('"', "").replace(",", "").replace("Rs", "").strip()
    if not s:
        raise ValueError("empty amount string")
    sign = 1
    if s.startswith("-"):
        sign, s = -1, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not s:
        raise ValueError("sign with no digits")
    whole, dot, frac = s.partition(".")
    if not whole.isdigit() or (dot and not (frac.isdigit() and len(frac) <= 2)):
        raise ValueError(f"unparseable rupee amount: {s!r}")
    frac = (frac + "00")[:2] if dot else "00"
    return sign * (int(whole) * 100 + int(frac))


def paise_from_optional_rupee_str(s: str) -> int:
    """Bank rows leave one of debit/credit empty; empty means zero."""
    return paise_from_rupee_str(s) if s and s.strip() else 0


def rupee_str_from_paise(paise: int, indian_grouping: bool = False) -> str:
    """Format integer paise as a rupee decimal string. With indian_grouping,
    uses lakh/crore comma placement ('1,52,340.50') as Indian bank CSVs do."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    whole, frac = divmod(paise, 100)
    w = str(whole)
    if indian_grouping and len(w) > 3:
        head, tail = w[:-3], w[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        w = ",".join(groups + [tail])
    return f"{sign}{w}.{frac:02d}"


def gst_on_fee(fee_paise: int) -> int:
    """18% GST on a PSP fee, integer paise, round half up. The ONLY place this
    is computed; per-payment values are summed for batch totals (recomputing
    18% at batch level can differ by paise and would manufacture false
    discrepancies)."""
    return (fee_paise * GST_RATE_PCT + 50) // 100


# --- Dates -------------------------------------------------------------------

def parse_bank_date(s: str) -> date:
    """DD/MM/YYYY (Indian bank statement convention)."""
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def bank_date_str(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def parse_iso_ts(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%dT%H:%M:%S")


def parse_iso_date(s: str) -> date:
    return parse_iso_ts(s).date() if "T" in s else date.fromisoformat(s.strip())


def roll_off_sunday(d: date) -> date:
    """Bank credits don't post on Sundays; roll to Monday."""
    return d + timedelta(days=1) if d.weekday() == 6 else d


# --- UTR / narration ----------------------------------------------------------

def canonical(s: str) -> str:
    """Uppercase and strip every non-alphanumeric character."""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def is_utr_shaped(token: str) -> bool:
    return bool(_UTR_RE.match(token))


def extract_tokens(*texts: str, min_len: int = 10) -> tuple[list[str], str]:
    """From narration/ref_no strings, return (discrete alnum runs of at least
    min_len chars, the full canonical strip of all texts joined).

    The runs catch clean and truncated references; the full strip catches
    separator-mangled ones ('UTIB-2509 8765 43210' still contains the UTR as a
    substring once stripped). Order is deterministic; duplicates removed."""
    runs: list[str] = []
    seen = set()
    for text in texts:
        for run in _ALNUM_RUN_RE.findall(text.upper()):
            if len(run) >= min_len and run not in seen:
                seen.add(run)
                runs.append(run)
    full = "".join(canonical(t) for t in texts)
    return runs, full


def hamming_le_1(a: str, b: str) -> bool:
    """True if equal length and at most one substituted character."""
    if len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b) if x != y) <= 1


def overlap_suffix_prefix(token: str, utr: str, min_overlap: int = 10) -> bool:
    """True if token is a contiguous fragment of the UTR (prefix, suffix, or
    interior substring) with at least min_overlap characters."""
    return len(token) >= min_overlap and token in utr
