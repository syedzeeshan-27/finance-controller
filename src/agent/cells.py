"""Parsers for raw statement cells (messy, bank-flavoured).

Deliberately separate from recon.normalize: the canonical parsers stay
untouched and strict, while these accept what real exports contain —
thousands separators in any grouping, a trailing "Cr"/"Dr" qualifier,
parentheses negatives. Everything returns integer paise.
"""

from __future__ import annotations

import datetime as _dt
import re

_AMOUNT_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")


def parse_amount_cell(cell: str) -> tuple[int, str | None]:
    """-> (paise, qualifier) where qualifier is "DR", "CR" or None.

    Raises ValueError on anything that is not a clean amount."""
    s = cell.strip()
    qualifier = None
    m = re.search(r"\b(cr|dr)\.?$", s, re.IGNORECASE)
    if m:
        qualifier = m.group(1).upper()
        s = s[:m.start()].strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    s = s.replace(",", "").replace("₹", "").replace("INR", "").strip()
    if not _AMOUNT_RE.fullmatch(s):
        raise ValueError(f"not an amount: {cell!r}")
    if s.startswith("-"):
        negative, s = True, s[1:]
    whole, _, frac = s.partition(".")
    paise = int(whole) * 100 + int(frac.ljust(2, "0")[:2] or "0")
    return (-paise if negative else paise), qualifier


def parse_date_cell(cell: str, date_format: str) -> _dt.date:
    return _dt.datetime.strptime(cell.strip(), date_format).date()
