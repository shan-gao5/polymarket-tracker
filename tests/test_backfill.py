"""End-to-end backfill tests against real, already-resolved BTC markets."""

import polymarket
import pytest
from sqlmodel import select

from polytracker.backfill import backfill_range, backfill_window
from polytracker.store import MarketTick, ResolvedMarket, get_session, make_engine

KNOWN_RESOLVED_WINDOW = 1771868700  # btc-updown-15m-1771868700, confirmed closed


@pytest.mark.asyncio
async def test_backfill_window_stores_outcome_and_price_history(tmp_path):
    engine = make_engine(tmp_path / "backfill.db")
    session = get_session(engine)

    async with polymarket.AsyncPublicClient() as client:
        resolved = await backfill_window(client, session, KNOWN_RESOLVED_WINDOW)

    assert resolved is not None
    assert resolved.slug == "btc-updown-15m-1771868700"
    assert resolved.outcome in ("up", "down")

    stored = session.get(ResolvedMarket, resolved.slug)
    assert stored is not None
    assert stored.outcome == resolved.outcome

    ticks = session.exec(
        select(MarketTick).where(MarketTick.market_slug == resolved.slug)
    ).all()
    assert len(ticks) > 0
    assert {t.side for t in ticks} == {"up", "down"}
    assert all(t.midpoint is not None for t in ticks)

    session.close()


@pytest.mark.asyncio
async def test_backfill_window_is_idempotent(tmp_path):
    engine = make_engine(tmp_path / "backfill_idempotent.db")
    session = get_session(engine)

    async with polymarket.AsyncPublicClient() as client:
        await backfill_window(client, session, KNOWN_RESOLVED_WINDOW)
        await backfill_window(client, session, KNOWN_RESOLVED_WINDOW)

    resolved_rows = session.exec(select(ResolvedMarket)).all()
    ticks = session.exec(select(MarketTick)).all()
    assert len(resolved_rows) == 1
    # second call must not duplicate price history
    assert len(ticks) == 30  # 15 points x 2 sides

    session.close()


@pytest.mark.asyncio
async def test_backfill_range_covers_multiple_windows(tmp_path):
    engine = make_engine(tmp_path / "backfill_range.db")
    session = get_session(engine)

    async with polymarket.AsyncPublicClient() as client:
        results = await backfill_range(
            client, session, KNOWN_RESOLVED_WINDOW, KNOWN_RESOLVED_WINDOW + 3 * 900
        )

    assert len(results) == 3
    assert {r.slug for r in results} == {
        "btc-updown-15m-1771868700",
        "btc-updown-15m-1771869600",
        "btc-updown-15m-1771870500",
    }

    session.close()
