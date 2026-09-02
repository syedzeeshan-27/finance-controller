"""Raw statement exports as a plain grid of strings.

One published coercion rule set (pinned by tests) so every downstream layer
— the agent's peek tools, the validator, apply — sees identical cells:

- None / empty        -> ""
- int, integral float -> str(int(v))         (10.0 -> "10")
- fractional float    -> f"{v:.2f}"          (7881.25 -> "7881.25")
- datetime/date       -> "%d-%m-%Y"
- anything else       -> str(v).strip()
"""

from __future__ import annotations

import csv
import datetime as _dt
import os


def _coerce(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else f"{v:.2f}"
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.strftime("%d-%m-%Y")
    return str(v).strip()


def load_grid(path: str) -> list[list[str]]:
    """Load an .xlsx (first sheet) or .csv export as rows of string cells."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        grid = [[_coerce(c) for c in row]
                for row in ws.iter_rows(values_only=True)]
        wb.close()
    elif ext == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            grid = [[_coerce(c) for c in row] for row in csv.reader(f)]
    else:
        raise ValueError(f"unsupported statement format: {ext!r} "
                         "(expected .xlsx or .csv)")
    width = max((len(r) for r in grid), default=0)
    return [r + [""] * (width - len(r)) for r in grid]
