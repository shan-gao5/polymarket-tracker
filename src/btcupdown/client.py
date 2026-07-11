"""Async client facade for the BTC 15-minute Up/Down market series.

This is the only module in ``btcupdown`` that talks to the Polymarket SDK.
It exposes a small, purpose-built surface - fetch a window's market, read
its order books and quotes, pull price history, and stream normalized
realtime events - and converts every SDK object into the pure types from
``btcupdown.types`` at the edge, so nothing built on top of this client
ever touches the SDK directly.

Usage::

    async with BtcUpDownClient() as client:
        market = await client.get_current_market()
        books = await client.get_books(market)
        async for event in client.stream(market):
            ...
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import TracebackType
from typing import Any

from btcupdown.types import Book, Market, Outcome, Quote
from btcupdown.windows import WINDOW_SECONDS, slug_for, window_bounds, window_start_for

CHAINLINK_BTC_SYMBOL = "btc/usd"
"""Symbol of the Chainlink BTC/USD feed these markets resolve against."""


class MarketNotFoundError(RuntimeError):
    """Raised when no market exists (yet) for a requested window."""


# ---------------------------------------------------------------------------
# Normalized realtime events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookUpdate:
    """A full order-book snapshot for one outcome token."""

    outcome: Outcome
    book: Book


@dataclass(frozen=True, slots=True)
class QuoteUpdate:
    """A top-of-book change for one outcome token."""

    outcome: Outcome
    quote: Quote


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    """A trade print for one outcome token."""

    outcome: Outcome
    token_id: str
    price: Decimal
    size: Decimal | None
    side: str
    timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class BtcPriceUpdate:
    """A Chainlink BTC/USD tick - the value these markets resolve against."""

    price: Decimal
    timestamp: datetime | None


StreamEvent = BookUpdate | QuoteUpdate | TradeUpdate | BtcPriceUpdate


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One point of a token's traded-price history."""

    timestamp: datetime
    price: Decimal


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class BtcUpDownClient:
    """Read-only async client for the ``btc-updown-15m`` market series.

    Wraps ``polymarket.AsyncPublicClient`` but never leaks SDK types.
    Everything here works unauthenticated - no API keys or wallets needed.

    An already-constructed SDK client can be injected (for tests or to
    share one connection); otherwise one is created on ``__aenter__`` and
    closed on ``__aexit__``.
    """

    def __init__(self, sdk_client: Any | None = None) -> None:
        self._sdk = sdk_client
        self._owns_sdk = sdk_client is None

    async def __aenter__(self) -> "BtcUpDownClient":
        if self._sdk is None:
            import polymarket

            self._sdk = polymarket.AsyncPublicClient()
            await self._sdk.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_sdk and self._sdk is not None:
            await self._sdk.__aexit__(exc_type, exc, tb)
            self._sdk = None

    @property
    def _client(self) -> Any:
        if self._sdk is None:
            raise RuntimeError("BtcUpDownClient must be used as an async context manager")
        return self._sdk

    # -- market discovery ---------------------------------------------------

    async def get_market(self, window_start_epoch: int) -> Market:
        """Fetch the market for the window starting at ``window_start_epoch``.

        The slug is computed locally (the series schedule is deterministic),
        so this is a single API call. Raises :class:`MarketNotFoundError` if
        Polymarket has not created the window's market yet.
        """
        import polymarket.errors

        slug = slug_for(window_start_epoch)
        try:
            event = await self._client.get_event(slug=slug)
        except polymarket.errors.RequestRejectedError as exc:
            raise MarketNotFoundError(f"no market found for window {slug}") from exc
        if not event.markets:
            raise MarketNotFoundError(f"event {slug} has no markets")
        sdk_market = event.markets[0]
        window_start, window_end = window_bounds(window_start_epoch)
        outcome: Outcome | None = None
        if sdk_market.state.closed:
            if sdk_market.outcomes.yes.price == 1:
                outcome = "up"
            elif sdk_market.outcomes.no.price == 1:
                outcome = "down"
        return Market(
            slug=slug,
            condition_id=sdk_market.condition_id,
            up_token_id=sdk_market.outcomes.yes.token_id,
            down_token_id=sdk_market.outcomes.no.token_id,
            window_start=window_start,
            window_end=window_end,
            closed=sdk_market.state.closed,
            outcome=outcome,
        )

    async def get_current_market(self, *, now: float | datetime | None = None) -> Market:
        """Fetch the market for the window containing ``now`` (default: wall clock)."""
        return await self.get_market(window_start_for(now if now is not None else time.time()))

    async def get_next_market(self, market: Market) -> Market:
        """Fetch the market for the window immediately after ``market``."""
        return await self.get_market(market.window_start_epoch + WINDOW_SECONDS)

    async def get_previous_market(self, market: Market) -> Market:
        """Fetch the market for the window immediately before ``market``."""
        return await self.get_market(market.window_start_epoch - WINDOW_SECONDS)

    # -- market data --------------------------------------------------------

    async def get_book(self, market: Market, outcome: Outcome) -> Book:
        """Fetch one outcome's order book, normalized best-first."""
        raw = await self._client.get_order_book(token_id=market.token_id(outcome))
        return Book.from_clob(raw)

    async def get_books(self, market: Market) -> dict[Outcome, Book]:
        """Fetch both outcomes' order books in one API call."""
        raws = await self._client.get_order_books(
            token_ids=[market.up_token_id, market.down_token_id]
        )
        books = {market.outcome_for_token(str(raw.token_id)): Book.from_clob(raw) for raw in raws}
        missing = {"up", "down"} - books.keys()
        if missing:
            raise ValueError(f"order books missing for outcomes {sorted(missing)}")
        return books

    async def get_quote(self, market: Market, outcome: Outcome) -> Quote:
        """Fetch one outcome's top-of-book quote (derived from its full book)."""
        book = await self.get_book(market, outcome)
        return Quote(
            token_id=book.token_id,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            timestamp=book.timestamp,
        )

    async def get_price_history(
        self, market: Market, outcome: Outcome
    ) -> tuple[PricePoint, ...]:
        """Fetch one outcome's traded-price history over the market's window."""
        points = await self._client.get_price_history(
            token_id=market.token_id(outcome),
            start_ts=market.window_start_epoch,
            end_ts=market.window_start_epoch + WINDOW_SECONDS,
        )
        return tuple(
            PricePoint(
                timestamp=datetime.fromtimestamp(point.t, tz=timezone.utc),
                price=Decimal(point.p),
            )
            for point in points
        )

    # -- realtime -----------------------------------------------------------

    async def stream(
        self, market: Market, *, include_btc_price: bool = True
    ) -> AsyncIterator[StreamEvent]:
        """Stream normalized realtime events for one market window.

        Yields :class:`BookUpdate`, :class:`QuoteUpdate`, and
        :class:`TradeUpdate` for both outcome tokens, plus
        :class:`BtcPriceUpdate` Chainlink ticks unless ``include_btc_price``
        is False. Runs until cancelled or the connection closes; the caller
        decides when the window is over (e.g. via ``market.seconds_remaining``).
        """
        from polymarket.models.clob.market_events import (
            MarketBestBidAskEvent,
            MarketBookEvent,
            MarketLastTradePriceEvent,
        )
        from polymarket.models.rtds_events import CryptoPricesChainlinkEvent
        from polymarket.streams import CryptoPricesSpec, MarketSpec

        specs: list[Any] = [
            MarketSpec(
                token_ids=[market.up_token_id, market.down_token_id],
                custom_feature_enabled=True,
            )
        ]
        if include_btc_price:
            specs.append(
                CryptoPricesSpec(
                    topic="prices.crypto.chainlink", symbols=[CHAINLINK_BTC_SYMBOL]
                )
            )

        handle = await self._client.subscribe(specs)
        try:
            async for event in handle:
                if isinstance(event, MarketBookEvent):
                    p = event.payload
                    yield BookUpdate(
                        outcome=market.outcome_for_token(str(p.token_id)),
                        book=Book.from_clob(p),
                    )
                elif isinstance(event, MarketBestBidAskEvent):
                    p = event.payload
                    yield QuoteUpdate(
                        outcome=market.outcome_for_token(str(p.token_id)),
                        quote=Quote(
                            token_id=str(p.token_id),
                            best_bid=p.best_bid,
                            best_ask=p.best_ask,
                            timestamp=p.timestamp,
                        ),
                    )
                elif isinstance(event, MarketLastTradePriceEvent):
                    p = event.payload
                    yield TradeUpdate(
                        outcome=market.outcome_for_token(str(p.token_id)),
                        token_id=str(p.token_id),
                        price=p.price,
                        size=p.size,
                        side=p.side,
                        timestamp=p.timestamp,
                    )
                elif isinstance(event, CryptoPricesChainlinkEvent):
                    p = event.payload
                    if p.symbol == CHAINLINK_BTC_SYMBOL:
                        yield BtcPriceUpdate(price=p.value, timestamp=event.timestamp)
        finally:
            await handle.close()
