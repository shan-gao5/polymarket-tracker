"""Find Polymarket's recurring 15-minute Bitcoin Up/Down markets.

Markets in this series follow a predictable slug: ``btc-updown-15m-{epoch}``,
where ``epoch`` is the UTC unix timestamp of the window's start, aligned to
900-second (15-minute) boundaries. This lets us compute the current or any
past/future window's slug directly instead of paginating search results.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import polymarket

WINDOW_SECONDS = 15 * 60
SLUG_PREFIX = "btc-updown-15m"


class MarketNotFoundError(RuntimeError):
    """Raised when no market exists yet for a requested window."""


@dataclass(frozen=True, slots=True)
class MarketWindow:
    """A single 15-minute BTC Up/Down market, resolved to its trading ids."""

    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    window_start: datetime
    window_end: datetime
    closed: bool
    outcome: str | None
    """"up" or "down" once resolved, else None."""


def window_start_for(ts: float) -> int:
    """Align a unix timestamp down to the start of its 15-minute window."""
    epoch = int(ts)
    return epoch - (epoch % WINDOW_SECONDS)


def slug_for_window(window_start_epoch: int) -> str:
    return f"{SLUG_PREFIX}-{window_start_epoch}"


async def get_market_for_window(
    client: polymarket.AsyncPublicClient, window_start_epoch: int
) -> MarketWindow:
    """Fetch the market for a specific 15-minute window by its start time."""
    slug = slug_for_window(window_start_epoch)
    try:
        event = await client.get_event(slug=slug)
    except polymarket.errors.RequestRejectedError as exc:
        raise MarketNotFoundError(f"no market found for window {slug}") from exc
    if not event.markets:
        raise MarketNotFoundError(f"event {slug} has no markets")
    market = event.markets[0]
    window_start = event.schedule.start_time or datetime.fromtimestamp(
        window_start_epoch, tz=timezone.utc
    )
    outcome = None
    if market.state.closed:
        if market.outcomes.yes.price == 1:
            outcome = "up"
        elif market.outcomes.no.price == 1:
            outcome = "down"
    return MarketWindow(
        slug=market.slug,
        condition_id=market.condition_id,
        up_token_id=market.outcomes.yes.token_id,
        down_token_id=market.outcomes.no.token_id,
        window_start=window_start,
        window_end=window_start + timedelta(seconds=WINDOW_SECONDS),
        closed=market.state.closed,
        outcome=outcome,
    )


async def get_current_market(
    client: polymarket.AsyncPublicClient, *, now: float | None = None
) -> MarketWindow:
    """Fetch the market for the window containing ``now`` (default: wall clock)."""
    return await get_market_for_window(client, window_start_for(now or time.time()))


async def get_next_market(
    client: polymarket.AsyncPublicClient, current: MarketWindow
) -> MarketWindow:
    """Fetch the market for the window immediately after ``current``."""
    next_start = int(current.window_start.timestamp()) + WINDOW_SECONDS
    return await get_market_for_window(client, next_start)
