"""Realtime ingestion worker: streams market + Chainlink BTC ticks into the datastore.

Runs one 15-minute window at a time, then rolls over to the next market
automatically. Also exposes an in-memory live order book snapshot for
consumers (the dashboard) that don't need every book delta persisted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import polymarket
from polymarket.models.clob.market_events import (
    MarketBestBidAskEvent,
    MarketBookEvent,
    MarketLastTradePriceEvent,
)
from polymarket.models.rtds_events import CryptoPricesChainlinkEvent
from sqlmodel import Session

from polytracker.discovery import MarketWindow, get_current_market, get_next_market
from polytracker.store import ChainlinkTick, MarketTick, Outcome

logger = logging.getLogger(__name__)

CHAINLINK_BTC_SYMBOL = "btc/usd"


@dataclass
class LiveOrderBook:
    """Latest full book per token id, kept in memory for the live dashboard."""

    books: dict[str, MarketBookEvent] = field(default_factory=dict)
    best: dict[str, MarketBestBidAskEvent] = field(default_factory=dict)
    last_trade: dict[str, MarketLastTradePriceEvent] = field(default_factory=dict)
    btc_price: float | None = None
    btc_price_ts: float | None = None


def _side_for_token(market: MarketWindow, token_id: str) -> Outcome:
    if token_id == market.up_token_id:
        return "up"
    if token_id == market.down_token_id:
        return "down"
    raise ValueError(f"token_id {token_id} does not belong to market {market.slug}")


async def ingest_window(
    client: polymarket.AsyncPublicClient,
    market: MarketWindow,
    session: Session,
    *,
    live_book: LiveOrderBook | None = None,
    stop_after: float | None = None,
    on_btc_tick: Callable[[float, datetime], None] | None = None,
) -> None:
    """Stream one market window's ticks into ``session`` until it closes.

    ``stop_after`` (seconds) is a test hook to bound how long we listen.
    ``on_btc_tick`` is called with (price, timestamp) for every Chainlink
    BTC/USD update, letting callers (e.g. the live dashboard) capture the
    window's opening price without adding dashboard-specific state here.
    """
    from polymarket.streams import CryptoPricesSpec, MarketSpec

    handle = await client.subscribe(
        [
            MarketSpec(
                token_ids=[market.up_token_id, market.down_token_id],
                custom_feature_enabled=True,
            ),
            CryptoPricesSpec(topic="prices.crypto.chainlink", symbols=[CHAINLINK_BTC_SYMBOL]),
        ]
    )

    async def _consume() -> None:
        async for event in handle:
            if isinstance(event, MarketBestBidAskEvent):
                p = event.payload
                side = _side_for_token(market, p.token_id)
                mid = (
                    float(p.best_bid + p.best_ask) / 2
                    if p.best_bid is not None and p.best_ask is not None
                    else None
                )
                session.add(
                    MarketTick(
                        ts=p.timestamp,
                        market_slug=market.slug,
                        token_id=p.token_id,
                        side=side,
                        best_bid=float(p.best_bid) if p.best_bid is not None else None,
                        best_ask=float(p.best_ask) if p.best_ask is not None else None,
                        midpoint=mid,
                    )
                )
                session.commit()
                if live_book is not None:
                    live_book.best[p.token_id] = event
            elif isinstance(event, MarketLastTradePriceEvent):
                p = event.payload
                side = _side_for_token(market, p.token_id)
                session.add(
                    MarketTick(
                        ts=p.timestamp,
                        market_slug=market.slug,
                        token_id=p.token_id,
                        side=side,
                        last_trade_price=float(p.price),
                    )
                )
                session.commit()
                if live_book is not None:
                    live_book.last_trade[p.token_id] = event
            elif isinstance(event, MarketBookEvent):
                if live_book is not None:
                    live_book.books[event.payload.token_id] = event
            elif isinstance(event, CryptoPricesChainlinkEvent):
                p = event.payload
                if p.symbol == CHAINLINK_BTC_SYMBOL:
                    session.add(ChainlinkTick(ts=event.timestamp, symbol=p.symbol, price=float(p.value)))
                    session.commit()
                    price = float(p.value)
                    if live_book is not None:
                        live_book.btc_price = price
                        live_book.btc_price_ts = event.timestamp.timestamp()
                    if on_btc_tick is not None:
                        on_btc_tick(price, event.timestamp)

    try:
        if stop_after is not None:
            await asyncio.wait_for(_consume(), timeout=stop_after)
        else:
            await _consume()
    except TimeoutError:
        pass
    finally:
        await handle.close()


async def run_forever(
    client: polymarket.AsyncPublicClient,
    session: Session,
    *,
    live_book: LiveOrderBook | None = None,
) -> None:
    """Ingest the current window, then roll over to each next window forever."""
    market = await get_current_market(client)
    while True:
        logger.info("ingesting window %s", market.slug)
        window_seconds = (market.window_end - market.window_start).total_seconds()
        await ingest_window(
            client,
            market,
            session,
            live_book=live_book,
            stop_after=window_seconds + 5,
        )
        market = await get_next_market(client, market)
