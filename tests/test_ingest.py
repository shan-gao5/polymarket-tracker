"""End-to-end test: ingest real ticks from the live current market for a short window."""

import polymarket
import pytest
from sqlmodel import select

from polytracker.discovery import get_current_market
from polytracker.ingest import LiveOrderBook, ingest_window
from polytracker.store import ChainlinkTick, MarketTick, get_session, make_engine


@pytest.mark.asyncio
async def test_ingest_window_writes_real_ticks(tmp_path):
    engine = make_engine(tmp_path / "ingest.db")
    session = get_session(engine)
    live_book = LiveOrderBook()

    async with polymarket.AsyncPublicClient() as client:
        market = await get_current_market(client)
        await ingest_window(client, market, session, live_book=live_book, stop_after=15)

    market_ticks = session.exec(select(MarketTick)).all()
    btc_ticks = session.exec(select(ChainlinkTick)).all()

    assert len(market_ticks) > 0
    assert len(btc_ticks) > 0
    assert all(t.market_slug == market.slug for t in market_ticks)
    assert all(t.side in ("up", "down") for t in market_ticks)
    assert all(t.price > 0 for t in btc_ticks)

    assert live_book.btc_price is not None and live_book.btc_price > 0
    assert market.up_token_id in live_book.books
    assert market.down_token_id in live_book.books

    session.close()
