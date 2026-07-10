"""Backfill historical 15-minute BTC markets: resolved outcomes and price history.

Ground-truth Up/Down labels come straight from Polymarket's own resolved
outcome (see discovery.MarketWindow.outcome) - no independent price feed
computation needed. get_price_history() fills in the price series for
windows we didn't capture with the realtime ingester.
"""

from __future__ import annotations

import logging

import polymarket
from sqlmodel import Session, select

from polytracker.discovery import (
    WINDOW_SECONDS,
    MarketNotFoundError,
    get_market_for_window,
)
from polytracker.store import MarketTick, ResolvedMarket

logger = logging.getLogger(__name__)


async def backfill_window(
    client: polymarket.AsyncPublicClient,
    session: Session,
    window_start_epoch: int,
    *,
    fetch_price_history: bool = True,
) -> ResolvedMarket | None:
    """Backfill one window: resolved outcome, and optionally its price history.

    Returns None if the window has no market yet or isn't resolved yet.
    """
    try:
        market = await get_market_for_window(client, window_start_epoch)
    except MarketNotFoundError:
        return None
    if not market.closed or market.outcome is None:
        return None

    existing = session.get(ResolvedMarket, market.slug)
    if existing is None:
        resolved = ResolvedMarket(
            slug=market.slug,
            condition_id=market.condition_id,
            up_token_id=market.up_token_id,
            down_token_id=market.down_token_id,
            window_start=market.window_start,
            window_end=market.window_end,
            outcome=market.outcome,
        )
        session.add(resolved)
        session.commit()
        session.refresh(resolved)
    else:
        resolved = existing

    if fetch_price_history:
        existing_ticks = session.exec(
            select(MarketTick.id).where(MarketTick.market_slug == market.slug).limit(1)
        ).first()
        if existing_ticks is None:
            await _backfill_price_history(client, session, market)

    return resolved


async def _backfill_price_history(client, session: Session, market) -> None:
    start_ts = int(market.window_start.timestamp())
    end_ts = int(market.window_end.timestamp())
    for token_id, side in ((market.up_token_id, "up"), (market.down_token_id, "down")):
        history = await client.get_price_history(
            token_id=token_id, start_ts=start_ts, end_ts=end_ts
        )
        for point in history:
            session.add(
                MarketTick(
                    ts=point.t if hasattr(point.t, "timestamp") else _epoch_to_dt(point.t),
                    market_slug=market.slug,
                    token_id=token_id,
                    side=side,
                    midpoint=float(point.p),
                )
            )
    session.commit()


def _epoch_to_dt(epoch: int):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)


async def backfill_range(
    client: polymarket.AsyncPublicClient,
    session: Session,
    start_window_epoch: int,
    end_window_epoch: int,
    *,
    fetch_price_history: bool = True,
) -> list[ResolvedMarket]:
    """Backfill every 15-minute window in [start_window_epoch, end_window_epoch)."""
    results: list[ResolvedMarket] = []
    for ws in range(start_window_epoch, end_window_epoch, WINDOW_SECONDS):
        resolved = await backfill_window(
            client, session, ws, fetch_price_history=fetch_price_history
        )
        if resolved is not None:
            results.append(resolved)
    return results
