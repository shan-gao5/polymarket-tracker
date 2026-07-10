"""A focused Python API for Polymarket's 15-minute BTC Up/Down markets.

This package deliberately exposes only what this one market series needs:
deterministic window math (``btcupdown.windows``), normalized market and
order-book types (``btcupdown.types``), and a pure paper trading engine
(``btcupdown.paper``). It does not re-export the general Polymarket SDK.
"""

from btcupdown.paper import (
    BookMismatchError,
    InsufficientCashError,
    InsufficientSharesError,
    MarketClosedError,
    NoLiquidityError,
    PaperAccount,
    PaperFill,
    PaperTrade,
    PaperTradingError,
    Position,
    Settlement,
)
from btcupdown.types import OUTCOMES, Book, BookLevel, Market, Outcome, Quote, other_outcome
from btcupdown.windows import (
    SLUG_PREFIX,
    WINDOW_SECONDS,
    epoch_for_slug,
    slug_for,
    window_bounds,
    window_start_for,
)

__all__ = [
    "OUTCOMES",
    "SLUG_PREFIX",
    "WINDOW_SECONDS",
    "Book",
    "BookLevel",
    "BookMismatchError",
    "InsufficientCashError",
    "InsufficientSharesError",
    "Market",
    "MarketClosedError",
    "NoLiquidityError",
    "Outcome",
    "PaperAccount",
    "PaperFill",
    "PaperTrade",
    "PaperTradingError",
    "Position",
    "Quote",
    "Settlement",
    "epoch_for_slug",
    "other_outcome",
    "slug_for",
    "window_bounds",
    "window_start_for",
]
