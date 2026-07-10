"""End-to-end tests against the real Polymarket API (no auth required).

These hit the live network on purpose: the goal is to verify the slug-guessing
scheme actually finds real markets, not to mock around it.
"""

import time

import polymarket
import pytest

from polytracker.discovery import (
    MarketNotFoundError,
    get_current_market,
    get_market_for_window,
    get_next_market,
    slug_for_window,
    window_start_for,
)


def test_window_start_aligns_to_15_minutes():
    assert window_start_for(1771868711) == 1771868700
    assert window_start_for(1771868700) == 1771868700


def test_slug_for_window():
    assert slug_for_window(1771868700) == "btc-updown-15m-1771868700"


@pytest.mark.asyncio
async def test_get_current_market_live():
    async with polymarket.AsyncPublicClient() as client:
        market = await get_current_market(client)

    assert market.slug.startswith("btc-updown-15m-")
    assert market.up_token_id
    assert market.down_token_id
    assert market.up_token_id != market.down_token_id
    assert market.condition_id.startswith("0x")

    now = time.time()
    assert market.window_start.timestamp() <= now < market.window_end.timestamp()
    assert (market.window_end - market.window_start).total_seconds() == 900


@pytest.mark.asyncio
async def test_get_market_for_past_resolved_window_live():
    async with polymarket.AsyncPublicClient() as client:
        market = await get_market_for_window(client, 1771868700)

    assert market.slug == "btc-updown-15m-1771868700"
    assert market.closed is True
    assert market.outcome in ("up", "down")


@pytest.mark.asyncio
async def test_unknown_window_raises_not_found_live():
    async with polymarket.AsyncPublicClient() as client:
        with pytest.raises(MarketNotFoundError):
            await get_market_for_window(client, 1)


@pytest.mark.asyncio
async def test_get_next_market_live():
    async with polymarket.AsyncPublicClient() as client:
        current = await get_current_market(client)
        nxt = await get_next_market(client, current)

    assert nxt.window_start == current.window_end
