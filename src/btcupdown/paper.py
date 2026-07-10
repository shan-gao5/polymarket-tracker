"""A deterministic paper trading engine for the BTC 15-minute Up/Down series.

``PaperAccount`` simulates a cash account trading binary outcome tokens
against real order-book snapshots, with no network access and no wallet.
It models the mechanics that matter for this market:

- Marketable orders fill by walking the book level by level, so simulated
  entry prices include price impact, not just the top of book.
- Fill-and-kill semantics: an order fills as much as the book allows within
  its price limit and the remainder is dropped, mirroring how the live
  executor submits FAK orders.
- Binary settlement: when a window resolves, winning shares redeem at 1.00
  collateral each and losing shares expire worthless.

All money and share quantities are ``Decimal``. Shares are quantized to six
decimal places, matching the on-chain resolution of Polymarket's collateral
and outcome tokens.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from btcupdown.types import Book, Market, Outcome

Side = Literal["buy", "sell"]

SHARE_PRECISION = Decimal("0.000001")
WINNING_PAYOUT_PER_SHARE = Decimal("1")


class PaperTradingError(RuntimeError):
    """Base class for every paper trading failure."""


class MarketClosedError(PaperTradingError):
    """Raised when trading is attempted on a closed or expired window."""


class InsufficientCashError(PaperTradingError):
    """Raised when a buy would spend more collateral than the account holds."""


class InsufficientSharesError(PaperTradingError):
    """Raised when a sell exceeds the account's position in that outcome."""


class NoLiquidityError(PaperTradingError):
    """Raised when the book offers nothing within the order's price limit."""


class BookMismatchError(PaperTradingError):
    """Raised when the supplied book is for a different token than the order."""


@dataclass(frozen=True, slots=True)
class PaperFill:
    """One simulated fill at a single book level."""

    price: Decimal
    shares: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.shares


@dataclass(frozen=True, slots=True)
class PaperTrade:
    """One executed simulated order and its fills."""

    market_slug: str
    outcome: Outcome
    side: Side
    fills: tuple[PaperFill, ...]
    ts: datetime
    realized_pnl: Decimal = Decimal("0")
    """For sells: proceeds minus the average-cost basis of the shares sold."""

    @property
    def shares(self) -> Decimal:
        return sum((fill.shares for fill in self.fills), start=Decimal("0"))

    @property
    def notional(self) -> Decimal:
        """Collateral spent (buys) or received (sells)."""
        return sum((fill.notional for fill in self.fills), start=Decimal("0"))

    @property
    def average_price(self) -> Decimal:
        return self.notional / self.shares


@dataclass(slots=True)
class Position:
    """Open shares in one outcome of one market, tracked at average cost."""

    market_slug: str
    outcome: Outcome
    shares: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")

    @property
    def average_price(self) -> Decimal:
        return self.cost / self.shares


@dataclass(frozen=True, slots=True)
class Settlement:
    """The cash resolution of every open position in one market."""

    market_slug: str
    winning_outcome: Outcome
    winning_shares: Decimal
    losing_shares: Decimal
    payout: Decimal
    cost_basis: Decimal
    pnl: Decimal
    ts: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _quantize_shares(shares: Decimal) -> Decimal:
    return shares.quantize(SHARE_PRECISION, rounding=ROUND_DOWN)


class PaperAccount:
    """A simulated collateral account holding cash and outcome-token positions."""

    def __init__(self, starting_cash: Decimal = Decimal("1000")) -> None:
        if starting_cash <= 0:
            raise ValueError("starting cash must be positive")
        self.starting_cash = starting_cash
        self._cash = starting_cash
        self._positions: dict[tuple[str, Outcome], Position] = {}
        self.trades: list[PaperTrade] = []
        self.settlements: list[Settlement] = []
        self._realized_pnl = Decimal("0")

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def realized_pnl(self) -> Decimal:
        """Cumulative P&L from completed sells and settlements."""
        return self._realized_pnl

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def position(self, market_slug: str, outcome: Outcome) -> Position | None:
        return self._positions.get((market_slug, outcome))

    def equity(self, marks: Mapping[tuple[str, Outcome], Decimal] | None = None) -> Decimal:
        """Cash plus the marked value of every open position.

        ``marks`` maps ``(market_slug, outcome)`` to a valuation price,
        typically the current best bid (the conservative, liquidation-value
        mark). Every open position must have a mark.
        """
        marks = marks or {}
        value = self._cash
        for key, position in self._positions.items():
            if key not in marks:
                raise KeyError(f"no mark supplied for open position {key[0]}/{key[1]}")
            value += position.shares * marks[key]
        return value

    def unrealized_pnl(self, marks: Mapping[tuple[str, Outcome], Decimal]) -> Decimal:
        """Marked value of open positions minus their cost basis."""
        pnl = Decimal("0")
        for key, position in self._positions.items():
            if key not in marks:
                raise KeyError(f"no mark supplied for open position {key[0]}/{key[1]}")
            pnl += position.shares * marks[key] - position.cost
        return pnl

    def _check_tradable(self, market: Market, outcome: Outcome, book: Book, now: datetime) -> None:
        if market.closed or now >= market.window_end:
            raise MarketClosedError(f"market {market.slug} is closed")
        if book.token_id != market.token_id(outcome):
            raise BookMismatchError(
                f"book is for token {book.token_id}, not the {outcome} outcome of {market.slug}"
            )

    def buy(
        self,
        market: Market,
        outcome: Outcome,
        spend: Decimal,
        book: Book,
        *,
        max_price: Decimal | None = None,
        now: datetime | None = None,
    ) -> PaperTrade:
        """Buy up to ``spend`` collateral of an outcome, walking the asks.

        Fill-and-kill: fills as much of ``spend`` as the book allows at or
        below ``max_price`` and drops the rest. Raises ``NoLiquidityError``
        if nothing fills at all.
        """
        now = now or _utc_now()
        self._check_tradable(market, outcome, book, now)
        if spend <= 0:
            raise ValueError("spend must be positive")
        if spend > self._cash:
            raise InsufficientCashError(
                f"order spends {spend} but the account holds {self._cash}"
            )

        remaining = spend
        fills: list[PaperFill] = []
        for level in book.asks:
            if max_price is not None and level.price > max_price:
                break
            shares = _quantize_shares(min(level.size, remaining / level.price))
            if shares <= 0:
                break
            fill = PaperFill(price=level.price, shares=shares)
            fills.append(fill)
            remaining -= fill.notional
        if not fills:
            raise NoLiquidityError(
                f"no asks available at or below {max_price} for {market.slug}/{outcome}"
            )

        trade = PaperTrade(
            market_slug=market.slug,
            outcome=outcome,
            side="buy",
            fills=tuple(fills),
            ts=now,
        )
        self._cash -= trade.notional
        key = (market.slug, outcome)
        position = self._positions.setdefault(
            key, Position(market_slug=market.slug, outcome=outcome)
        )
        position.shares += trade.shares
        position.cost += trade.notional
        self.trades.append(trade)
        return trade

    def sell(
        self,
        market: Market,
        outcome: Outcome,
        shares: Decimal,
        book: Book,
        *,
        min_price: Decimal | None = None,
        now: datetime | None = None,
    ) -> PaperTrade:
        """Sell up to ``shares`` of an outcome, walking the bids.

        Fill-and-kill: fills as much as the book bids for at or above
        ``min_price`` and drops the rest. Realized P&L is booked against
        the position's average cost.
        """
        now = now or _utc_now()
        self._check_tradable(market, outcome, book, now)
        if shares <= 0:
            raise ValueError("shares must be positive")
        key = (market.slug, outcome)
        position = self._positions.get(key)
        if position is None or position.shares < shares:
            held = position.shares if position else Decimal("0")
            raise InsufficientSharesError(
                f"order sells {shares} shares of {market.slug}/{outcome} but the account holds {held}"
            )

        remaining = shares
        fills: list[PaperFill] = []
        for level in book.bids:
            if min_price is not None and level.price < min_price:
                break
            fill_shares = _quantize_shares(min(level.size, remaining))
            if fill_shares <= 0:
                break
            fills.append(PaperFill(price=level.price, shares=fill_shares))
            remaining -= fill_shares
            if remaining <= 0:
                break
        if not fills:
            raise NoLiquidityError(
                f"no bids available at or above {min_price} for {market.slug}/{outcome}"
            )

        sold = sum((fill.shares for fill in fills), start=Decimal("0"))
        proceeds = sum((fill.notional for fill in fills), start=Decimal("0"))
        cost_removed = position.cost * sold / position.shares
        realized = proceeds - cost_removed

        trade = PaperTrade(
            market_slug=market.slug,
            outcome=outcome,
            side="sell",
            fills=tuple(fills),
            ts=now,
            realized_pnl=realized,
        )
        self._cash += proceeds
        self._realized_pnl += realized
        position.shares -= sold
        position.cost -= cost_removed
        if position.shares <= 0:
            del self._positions[key]
        self.trades.append(trade)
        return trade

    def settle(self, market: Market, *, now: datetime | None = None) -> Settlement | None:
        """Resolve every open position in a market against its final outcome.

        Winning shares redeem at 1.00 collateral each; losing shares expire
        worthless. Returns ``None`` when the account holds no position in
        the market. Requires ``market.outcome`` to be set.
        """
        if market.outcome is None:
            raise ValueError(f"market {market.slug} is not resolved yet")
        now = now or _utc_now()

        winning = self._positions.pop((market.slug, market.outcome), None)
        losing_outcome: Outcome = "down" if market.outcome == "up" else "up"
        losing = self._positions.pop((market.slug, losing_outcome), None)
        if winning is None and losing is None:
            return None

        winning_shares = winning.shares if winning else Decimal("0")
        losing_shares = losing.shares if losing else Decimal("0")
        cost_basis = (winning.cost if winning else Decimal("0")) + (
            losing.cost if losing else Decimal("0")
        )
        payout = winning_shares * WINNING_PAYOUT_PER_SHARE
        pnl = payout - cost_basis

        settlement = Settlement(
            market_slug=market.slug,
            winning_outcome=market.outcome,
            winning_shares=winning_shares,
            losing_shares=losing_shares,
            payout=payout,
            cost_basis=cost_basis,
            pnl=pnl,
            ts=now,
        )
        self._cash += payout
        self._realized_pnl += pnl
        self.settlements.append(settlement)
        return settlement
