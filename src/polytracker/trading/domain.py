"""Pure domain types and order-book calculations for execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Literal

from polymarket.models.clob.order_book import OrderBook

from polytracker.discovery import MarketWindow
from polytracker.trading.config import RiskConfig

TradingMode = Literal["paper", "live"]
TradeOutcome = Literal["up", "down"]


class RiskViolation(RuntimeError):
    """Raised before execution when an order would violate a hard limit."""


@dataclass(frozen=True, slots=True)
class PreparedOrder:
    intent_id: str
    mode: TradingMode
    market: MarketWindow
    outcome: TradeOutcome
    token_id: str
    amount: Decimal
    max_spend: Decimal
    max_price: Decimal


@dataclass(frozen=True, slots=True)
class Fill:
    trade_id: str
    price: Decimal
    size: Decimal
    status: str
    transaction_hash: str | None = None
    matched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    accepted: bool
    status: str
    order_id: str | None = None
    making_amount: Decimal = Decimal("0")
    taking_amount: Decimal = Decimal("0")
    error_code: str | None = None
    error_message: str | None = None
    fills: tuple[Fill, ...] = ()

    @property
    def average_fill_price(self) -> Decimal | None:
        if self.taking_amount <= 0:
            return None
        return self.making_amount / self.taking_amount


def token_for_outcome(market: MarketWindow, outcome: TradeOutcome) -> str:
    return market.up_token_id if outcome == "up" else market.down_token_id


def prepare_buy_order(
    *,
    mode: TradingMode,
    market: MarketWindow,
    outcome: TradeOutcome,
    book: OrderBook,
    risk: RiskConfig,
    now: datetime | None = None,
) -> PreparedOrder:
    """Build the smallest valid protected buy order for the current book."""
    now = now or datetime.now(timezone.utc)
    if market.closed:
        raise RiskViolation("market is closed")
    seconds_remaining = (market.window_end - now).total_seconds()
    if seconds_remaining < risk.min_seconds_remaining:
        raise RiskViolation(
            f"market has only {seconds_remaining:.1f}s remaining; "
            f"requires at least {risk.min_seconds_remaining:.1f}s"
        )
    if book.token_id != token_for_outcome(market, outcome):
        raise RiskViolation("order book token does not match the requested outcome")
    if book.timestamp is None:
        raise RiskViolation("order book has no timestamp")
    book_ts = book.timestamp
    if book_ts.tzinfo is None:
        book_ts = book_ts.replace(tzinfo=timezone.utc)
    age = (now - book_ts).total_seconds()
    if age < -2 or age > risk.max_book_age_seconds:
        raise RiskViolation(
            f"order book age is {age:.1f}s; maximum is {risk.max_book_age_seconds:.1f}s"
        )
    if not book.asks:
        raise RiskViolation("order book has no asks")

    best_ask = book.asks[-1].price
    max_price = min(best_ask + book.tick_size, Decimal("0.99"))
    if max_price < best_ask:
        raise RiskViolation("best ask is above the maximum valid outcome-token price")
    minimum_spend = book.min_order_size * max_price
    amount = minimum_spend.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if amount <= 0:
        raise RiskViolation("calculated order amount is not positive")
    if amount > risk.max_live_spend:
        raise RiskViolation(
            f"minimum valid protected order costs {amount}, above the {risk.max_live_spend} cap"
        )

    intent_id = f"{mode}:{market.slug}:buy-test"
    return PreparedOrder(
        intent_id=intent_id,
        mode=mode,
        market=market,
        outcome=outcome,
        token_id=token_for_outcome(market, outcome),
        amount=amount,
        max_spend=risk.max_live_spend,
        max_price=max_price,
    )
