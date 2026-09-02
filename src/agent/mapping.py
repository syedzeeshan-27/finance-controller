"""The StatementMapping — the ONLY artifact the intake agent influences.

The agent proposes one of these; `agent.validator` then proves it correct
arithmetically or rejects it with machine-readable reasons. The mapping is
pure structure (where things are), never content (what matched what).

Row model: every grid row belongs to exactly one of
    {header_row} | noise_rows | a period's opening_row / closing_row /
    transaction_rows
Periods carry their own opening/closing pseudo-rows because real exports
concatenate several statement windows into one sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COLUMN_ROLES = ("date", "narration", "debit", "credit", "balance")
OPTIONAL_COLUMN_ROLES = ("drcr_indicator", "chq_ref")


@dataclass
class Period:
    opening_row: int
    closing_row: int
    transaction_rows: list[int]


@dataclass
class StatementMapping:
    header_row: int
    columns: dict            # role -> column index (optional roles: None)
    date_format: str         # strptime, e.g. "%d-%m-%Y"
    periods: list[Period]
    noise_rows: list[int]
    reference_recipe: dict   # {"kind": "delimited"|"none", "delimiter": str,
                             #  "index": int, "token_regex": str}
    reversal_pairs: list = field(default_factory=list)  # [[row, row], ...]

    @classmethod
    def from_dict(cls, d: dict) -> "StatementMapping":
        return cls(
            header_row=d["header_row"],
            columns=dict(d["columns"]),
            date_format=d["date_format"],
            periods=[Period(p["opening_row"], p["closing_row"],
                            list(p["transaction_rows"]))
                     for p in d["periods"]],
            noise_rows=list(d["noise_rows"]),
            reference_recipe=dict(d["reference_recipe"]),
            reversal_pairs=[list(p) for p in d.get("reversal_pairs", [])],
        )

    def to_dict(self) -> dict:
        return {
            "header_row": self.header_row,
            "columns": self.columns,
            "date_format": self.date_format,
            "periods": [{"opening_row": p.opening_row,
                         "closing_row": p.closing_row,
                         "transaction_rows": p.transaction_rows}
                        for p in self.periods],
            "noise_rows": self.noise_rows,
            "reference_recipe": self.reference_recipe,
            "reversal_pairs": self.reversal_pairs,
        }


def extract_reference(narration: str, recipe: dict) -> str:
    """Apply the reference recipe to one narration. Returns "" when the
    narration doesn't fit the recipe — absence is normal, never an error."""
    import re
    if recipe.get("kind") != "delimited":
        return ""
    parts = narration.split(recipe["delimiter"])
    idx = recipe["index"]
    if idx >= len(parts):
        return ""
    token = parts[idx].strip()
    return token if re.fullmatch(recipe["token_regex"], token) else ""


_INT = {"type": "integer"}
_INT_ARRAY = {"type": "array", "items": _INT}

MAPPING_SCHEMA = {
    "type": "object",
    "properties": {
        "header_row": _INT,
        "columns": {
            "type": "object",
            "properties": {
                "date": _INT, "narration": _INT, "debit": _INT,
                "credit": _INT, "balance": _INT,
                "drcr_indicator": {"type": ["integer", "null"]},
                "chq_ref": {"type": ["integer", "null"]},
            },
            "required": ["date", "narration", "debit", "credit", "balance",
                         "drcr_indicator", "chq_ref"],
            "additionalProperties": False,
        },
        "date_format": {"type": "string"},
        "periods": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"opening_row": _INT, "closing_row": _INT,
                               "transaction_rows": _INT_ARRAY},
                "required": ["opening_row", "closing_row",
                             "transaction_rows"],
                "additionalProperties": False,
            },
        },
        "noise_rows": _INT_ARRAY,
        "reference_recipe": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["delimited", "none"]},
                "delimiter": {"type": "string"},
                "index": _INT,
                "token_regex": {"type": "string"},
            },
            "required": ["kind", "delimiter", "index", "token_regex"],
            "additionalProperties": False,
        },
        # pair shape (exactly two row indices) is enforced by the validator,
        # not the schema: the API rejects minItems/maxItems beyond 0/1
        "reversal_pairs": {"type": "array",
                           "items": {"type": "array", "items": _INT}},
    },
    "required": ["header_row", "columns", "date_format", "periods",
                 "noise_rows", "reference_recipe", "reversal_pairs"],
    "additionalProperties": False,
}
