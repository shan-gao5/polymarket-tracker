"""A focused Python API for Polymarket's 15-minute BTC Up/Down markets.

This package deliberately exposes only what this one market series needs:
deterministic window math (``btcupdown.windows``), normalized market and
order-book types (``btcupdown.types``), a pure paper trading engine
(``btcupdown.paper``), and a read-only async client facade
(``btcupdown.client``). It does not re-export the general Polymarket SDK.
"""

from btcupdown.client import (
    CHAINLINK_BTC_SYMBOL,
    BookUpdate,
    BtcPriceUpdate,
    BtcUpDownClient,
    MarketNotFoundError,
    PricePoint,
    QuoteUpdate,
    StreamEvent,
    TradeUpdate,
)
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
    "CHAINLINK_BTC_SYMBOL",
    "OUTCOMES",
    "SLUG_PREFIX",
    "WINDOW_SECONDS",
    "Book",
    "BookLevel",
    "BookMismatchError",
    "BookUpdate",
    "BtcPriceUpdate",
    "BtcUpDownClient",
    "InsufficientCashError",
    "InsufficientSharesError",
    "Market",
    "MarketClosedError",
    "MarketNotFoundError",
    "NoLiquidityError",
    "Outcome",
    "PaperAccount",
    "PaperFill",
    "PaperTrade",
    "PaperTradingError",
    "Position",
    "PricePoint",
    "Quote",
    "QuoteUpdate",
    "Settlement",
    "StreamEvent",
    "TradeUpdate",
    "epoch_for_slug",
    "other_outcome",
    "slug_for",
    "window_bounds",
    "window_start_for",
]
