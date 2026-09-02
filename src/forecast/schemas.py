"""Record shapes for the forecasting pipeline. All money is integer paise."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date

from recon import schemas as RS


@dataclass
class ForecastInput:
    """Everything a forecaster is allowed to know: the world strictly up to
    and including the cutoff date. Built ONLY by slicing.build_input."""
    cutoff: date
    opening_balance_paise: int          # statement balance at the cutoff
    first_statement_date: date | None   # for history-length checks
    bank_rows: list[RS.BankRow] = field(default_factory=list)
    settlements: list[RS.Settlement] = field(default_factory=list)
    payments: list[dict] = field(default_factory=list)

    @property
    def history_days(self) -> int:
        if self.first_statement_date is None:
            return 0
        return (self.cutoff - self.first_statement_date).days + 1


@dataclass
class DayForecast:
    date: str                        # ISO
    known_inflows: int = 0           # layer a: in-flight settlement money
    projected_sales_inflows: int = 0  # layer c, credit side
    other_income: int = 0            # layer c, non-settlement credits
    recurring_outflows: int = 0      # layer b (positive number)
    variable_outflows: int = 0       # layer c, debit side (positive number)
    net: int = 0
    balance: int = 0
    lo80: int | None = None
    hi80: int | None = None


@dataclass
class ForecastResult:
    strategy: str
    cutoff: str                      # ISO
    horizon: int
    opening_balance_paise: int
    days: list[DayForecast] = field(default_factory=list)
    obligations: list[dict] = field(default_factory=list)   # projected recurring
    in_flight: list[dict] = field(default_factory=list)     # known settlement money
    attention: list[dict] = field(default_factory=list)     # overdue settlements
    min_balance: dict = field(default_factory=dict)         # {date, paise}
    first_below_threshold: str | None = None
    threshold_paise: int | None = None
    calibration: dict | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
