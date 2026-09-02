"""Shapes for the unified daily close.

An ExceptionItem is one entry in the single queue, whatever loop it came
from; a DailyClose is the whole close: cash position, per-loop summaries,
the forecast digest, the queue, and the embedded decision lists that make
the close independently verifiable after the fact.

Determinism contract: a DailyClose contains no wall-clock timestamps and
every list is sorted by a published key, so two runs over the same world
serialise byte-identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# Where a queue item came from. One value per decision surface.
SOURCE_RECON_A = "recon_a"
SOURCE_RECON_B = "recon_b"
SOURCE_TAX_ITC = "tax_itc"
SOURCE_TAX_TDS = "tax_tds"
SOURCE_TAX_OBLIGATION = "tax_obligation"
SOURCE_FORECAST = "forecast"
SOURCES = (
    SOURCE_RECON_A, SOURCE_RECON_B, SOURCE_TAX_ITC, SOURCE_TAX_TDS,
    SOURCE_TAX_OBLIGATION, SOURCE_FORECAST,
)


@dataclass
class ExceptionItem:
    """One entry in the unified queue.

    money_at_risk_paise is always >= 0: the magnitude a human should weigh,
    not a signed discrepancy (each underlying decision keeps its own signed
    view). candidates are normalised to {"id", "amount_paise", "note"}
    whatever shape the source engine used.
    """
    source: str                            # one of SOURCES
    status: str                            # underlying decision status (or synthetic)
    severity: int                          # 1 (act today) | 2 (chase) | 3 (review)
    money_at_risk_paise: int
    record_ids: list[str]
    title: str
    detail: str = ""
    suggested_action: str = ""
    evidence: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    due_date: str | None = None            # ISO, when a date drives the action
    days_overdue: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyClose:
    """The whole close for one world at its final statement date.

    The full decision lists are embedded on purpose: they are what lets
    verify_close re-check the aggregation (and re-run the three stage
    verifiers) from the serialised close alone. Reports and dashboards
    render summaries; graders and verifiers read the embedded decisions.
    """
    close_date: str                        # ISO; max value_date on the statement
    world: str                             # data-dir basename, a display label
    cash: dict                             # balance_paise, statement_rows, first_statement_date, history_days
    recon_summary: dict
    leg_b_summary: dict
    tax_summary: dict | None               # None when the world predates the tax stage
    forecast: dict                         # full ForecastResult.to_dict()
    queue: list[ExceptionItem]
    verify_panel: dict                     # {"leg_a": n, "tax": n|None, "forecast": n, "samples": [...]}
    counts: dict                           # {"queue_total", "by_severity", "by_source"}
    decisions_a: list[dict]
    decisions_b: list[dict]
    tax_decisions: list[dict] | None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
