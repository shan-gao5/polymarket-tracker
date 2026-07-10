"""Normalized market and order-book types for the BTC 15-minute Up/Down series.

These types are deliberately independent of the underlying Polymarket SDK so
that everything built on them (the paper trading engine, strategies, tests)
stays pure and deterministic. Adapters convert SDK objects into these types
at the edge.

Ordering convention: unlike the raw CLOB payloads (which sort both sides so
the best level is *last*), ``Book`` always stores both sides best-first:
``bids`` descending by price, ``asks`` ascending by price. The constructor
normalizes whatever order it is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from btcupdown.windows import epoch_for_slug

Outcome = Literal["up", "down"]
OUTCOMES: tuple[Outcome, Outcome] = ("up", "down")


def other_outcome(outcome: Outcome) -> Outcome:
    return "down" if outcome == "up" else "up"


@dataclass(frozen=True, slots=True)
class Market:
    """One 15-minute BTC Up/Down market, resolved to its trading identifiers."""

    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    window_start: datetime
    window_end: datetime
    closed: bool = False
    outcome: Outcome | None = None
    """Winning outcome once resolved, else None."""

    def token_id(self, outcome: Outcome) -> str:
        return self.up_token_id if outcome == "up" else self.down_token_id

    def outcome_for_token(self, token_id: str) -> Outcome:
        if token_id == self.up_token_id:
            return "up"
        if token_id == self.down_token_id:
            return "down"
        raise ValueError(f"token {token_id} does not belong to market {self.slug}")

    @property
    def window_start_epoch(self) -> int:
        return epoch_for_slug(self.slug)

    def seconds_remaining(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (self.window_end - now).total_seconds())


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One price level of an order book side."""

    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class Book:
    """An order-book snapshot for one outcome token, both sides best-first."""

    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "bids",
            tuple(sorted((l for l in self.bids if l.size > 0), key=lambda l: l.price, reverse=True)),
        )
        object.__setattr__(
            self,
            "asks",
            tuple(sorted((l for l in self.asks if l.size > 0), key=lambda l: l.price)),
        )

    @classmethod
    def from_clob(cls, book) -> "Book":
        """Build a normalized Book from a Polymarket SDK ``OrderBook``.

        Duck-typed on purpose: accepts any object exposing ``token_id``,
        ``timestamp``, and ``bids``/``asks`` sequences of levels with
        ``price`` and ``size`` attributes, so this module never has to
        import the SDK.
        """
        return cls(
            token_id=str(book.token_id),
            bids=tuple(BookLevel(price=Decimal(l.price), size=Decimal(l.size)) for l in book.bids),
            asks=tuple(BookLevel(price=Decimal(l.price), size=Decimal(l.size)) for l in book.asks),
            timestamp=book.timestamp,
        )

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book prices for one outcome token."""

    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    timestamp: datetime | None = None

    @property
    def midpoint(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2
