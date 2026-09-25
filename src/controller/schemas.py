"""Shapes for the unified daily close.

An ExceptionItem is one entry in the single queue, whichever leg it came
from; a DailyClose is the whole close: cash position, per-leg summaries,
the queue, and the embedded decision lists that make the close
independently verifiable after the fact.

Determinism contract: a DailyClose contains no wall-clock timestamps and
every list is sorted by a published key, so two runs over the same world
serialise byte-identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

# Where a queue item came from. One value per decision surface.
SOURCE_RECON_A = "recon_a"
SOURCE_RECON_B = "recon_b"
SOURCES = (SOURCE_RECON_A, SOURCE_RECON_B)


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
    due_date: str | None = None            # ISO; expected credit date of an overdue settlement
    days_overdue: int | None = None        # filled by controller.overdue

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyClose:
    """The whole close for one world at its final statement date.

    The full decision lists are embedded on purpose: they are what lets
    verify_close re-check the aggregation (and re-run the stage verifier)
    from the serialised close alone. Reports and dashboards
    render summaries; graders and verifiers read the embedded decisions.
    """
    close_date: str                        # ISO; max value_date on the statement
    world: str                             # data-dir basename, a display label
    cash: dict                             # balance_paise, statement_rows, first_statement_date, history_days
    recon_summary: dict
    leg_b_summary: dict
    queue: list[ExceptionItem]
    verify_panel: dict                     # {"leg_a": n, "samples": [...]}
    counts: dict                           # {"queue_total", "by_severity", "by_source"}
    decisions_a: list[dict]
    decisions_b: list[dict]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
