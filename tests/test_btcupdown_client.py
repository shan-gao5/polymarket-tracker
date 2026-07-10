"""Tests for the btcupdown async client facade.

Most tests run against a fake SDK client so they are deterministic and
offline; they construct real SDK event/model objects where the facade's
behavior depends on them (stream event dispatch, book normalization).
A small number of live smoke tests at the bottom mirror the repo's
convention of verifying against the real API.
"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import polymarket
import pytest
from polymarket.models.clob.market_events import (
    MarketBestBidAskEvent,
    MarketBookEvent,
    MarketLastTradePriceEvent,
)
from polymarket.models.rtds_events import CryptoPricesChainlinkEvent

from btcupdown import (
    Book,
    BookLevel,
    BookUpdate,
    BtcPriceUpdate,
    BtcUpDownClient,
    Market,
    MarketNotFoundError,
    QuoteUpdate,
    TradeUpdate,
)

WINDOW_EPOCH = 1771868700
SLUG = f"btc-updown-15m-{WINDOW_EPOCH}"
UP_TOKEN = "111"
DOWN_TOKEN = "222"


def make_market(**overrides) -> Market:
    defaults = dict(
        slug=SLUG,
        condition_id="0xabc",
        up_token_id=UP_TOKEN,
        down_token_id=DOWN_TOKEN,
        window_start=datetime.fromtimestamp(WINDOW_EPOCH, tz=timezone.utc),
        window_end=datetime.fromtimestamp(WINDOW_EPOCH + 900, tz=timezone.utc),
    )
    defaults.update(overrides)
    return Market(**defaults)


def sdk_event(*, closed=False, up_price=None, down_price=None):
    return SimpleNamespace(
        markets=[
            SimpleNamespace(
                condition_id="0xabc",
                state=SimpleNamespace(closed=closed),
                outcomes=SimpleNamespace(
                    yes=SimpleNamespace(token_id=UP_TOKEN, price=up_price),
                    no=SimpleNamespace(token_id=DOWN_TOKEN, price=down_price),
                ),
            )
        ],
        schedule=SimpleNamespace(
            start_time=datetime.fromtimestamp(WINDOW_EPOCH, tz=timezone.utc)
        ),
    )


def sdk_book(token_id: str):
    """A raw CLOB-style book: both sides sorted best-*last*."""
    return SimpleNamespace(
        token_id=token_id,
        timestamp=datetime.fromtimestamp(WINDOW_EPOCH + 60, tz=timezone.utc),
        bids=[
            SimpleNamespace(price="0.40", size="10"),
            SimpleNamespace(price="0.45", size="5"),
        ],
        asks=[
            SimpleNamespace(price="0.55", size="8"),
            SimpleNamespace(price="0.50", size="3"),
        ],
    )


class FakeHandle:
    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def close(self):
        self.closed = True


class FakeSdk:
    """Duck-typed stand-in for polymarket.AsyncPublicClient."""

    def __init__(self, *, events=None, stream_events=None):
        self.events = events or {}
        self.stream_events = stream_events or []
        self.subscribed_specs = None
        self.handle = None

    async def get_event(self, *, slug):
        if slug not in self.events:
            raise polymarket.errors.RequestRejectedError(f"not found: {slug}", status=404)
        return self.events[slug]

    async def get_order_book(self, *, token_id):
        return sdk_book(token_id)

    async def get_order_books(self, *, token_ids):
        return tuple(sdk_book(t) for t in token_ids)

    async def get_price_history(self, *, token_id, start_ts=None, end_ts=None, **kw):
        return (
            SimpleNamespace(t=start_ts, p=Decimal("0.50")),
            SimpleNamespace(t=start_ts + 60, p=Decimal("0.62")),
        )

    async def subscribe(self, specs):
        self.subscribed_specs = specs
        self.handle = FakeHandle(self.stream_events)
        return self.handle


# -- market discovery -------------------------------------------------------


async def test_get_market_open_window():
    sdk = FakeSdk(events={SLUG: sdk_event()})
    async with BtcUpDownClient(sdk) as client:
        market = await client.get_market(WINDOW_EPOCH)
    assert market.slug == SLUG
    assert market.up_token_id == UP_TOKEN
    assert market.down_token_id == DOWN_TOKEN
    assert market.closed is False
    assert market.outcome is None
    assert (market.window_end - market.window_start).total_seconds() == 900


async def test_get_market_resolved_window():
    sdk = FakeSdk(events={SLUG: sdk_event(closed=True, up_price=1, down_price=0)})
    async with BtcUpDownClient(sdk) as client:
        market = await client.get_market(WINDOW_EPOCH)
    assert market.closed is True
    assert market.outcome == "up"


async def test_get_market_not_found():
    sdk = FakeSdk()
    async with BtcUpDownClient(sdk) as client:
        with pytest.raises(MarketNotFoundError):
            await client.get_market(WINDOW_EPOCH)


async def test_get_current_next_previous_market():
    slugs = {
        f"btc-updown-15m-{WINDOW_EPOCH + delta}": sdk_event()
        for delta in (-900, 0, 900)
    }
    sdk = FakeSdk(events=slugs)
    async with BtcUpDownClient(sdk) as client:
        current = await client.get_current_market(now=WINDOW_EPOCH + 30)
        assert current.slug == SLUG
        nxt = await client.get_next_market(current)
        assert nxt.window_start == current.window_end
        prev = await client.get_previous_market(current)
        assert prev.window_end == current.window_start


async def test_client_requires_context_manager():
    client = BtcUpDownClient()
    with pytest.raises(RuntimeError, match="context manager"):
        client._client


# -- market data ------------------------------------------------------------


async def test_get_book_normalizes_best_first():
    sdk = FakeSdk()
    async with BtcUpDownClient(sdk) as client:
        book = await client.get_book(make_market(), "up")
    assert book.token_id == UP_TOKEN
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.50")


async def test_get_books_returns_both_outcomes():
    sdk = FakeSdk()
    async with BtcUpDownClient(sdk) as client:
        books = await client.get_books(make_market())
    assert set(books) == {"up", "down"}
    assert books["up"].token_id == UP_TOKEN
    assert books["down"].token_id == DOWN_TOKEN


async def test_get_quote():
    sdk = FakeSdk()
    async with BtcUpDownClient(sdk) as client:
        quote = await client.get_quote(make_market(), "down")
    assert quote.token_id == DOWN_TOKEN
    assert quote.best_bid == Decimal("0.45")
    assert quote.best_ask == Decimal("0.50")
    assert quote.midpoint == Decimal("0.475")


async def test_get_price_history():
    sdk = FakeSdk()
    async with BtcUpDownClient(sdk) as client:
        points = await client.get_price_history(make_market(), "up")
    assert len(points) == 2
    assert points[0].timestamp == datetime.fromtimestamp(WINDOW_EPOCH, tz=timezone.utc)
    assert points[1].price == Decimal("0.62")


# -- realtime ---------------------------------------------------------------


def stream_fixture_events():
    ts = datetime.fromtimestamp(WINDOW_EPOCH + 10, tz=timezone.utc)
    return [
        MarketBookEvent(
            type="book",
            payload=dict(
                market="0xabc",
                token_id=UP_TOKEN,
                bids=[dict(price="0.40", size="10"), dict(price="0.45", size="5")],
                asks=[dict(price="0.55", size="8"), dict(price="0.50", size="3")],
                timestamp=ts,
            ),
        ),
        MarketBestBidAskEvent(
            type="best_bid_ask",
            payload=dict(
                market="0xabc",
                token_id=DOWN_TOKEN,
                best_bid="0.50",
                best_ask="0.56",
                timestamp=ts,
            ),
        ),
        MarketLastTradePriceEvent(
            type="last_trade_price",
            payload=dict(
                market="0xabc",
                token_id=UP_TOKEN,
                price="0.47",
                size="12",
                side="BUY",
                timestamp=ts,
            ),
        ),
        CryptoPricesChainlinkEvent(
            type="update",
            timestamp=ts,
            payload=dict(symbol="btc/usd", timestamp=WINDOW_EPOCH + 10, value="97123.45"),
        ),
        CryptoPricesChainlinkEvent(
            type="update",
            timestamp=ts,
            payload=dict(symbol="eth/usd", timestamp=WINDOW_EPOCH + 10, value="3500"),
        ),
    ]


async def test_stream_normalizes_events():
    sdk = FakeSdk(stream_events=stream_fixture_events())
    async with BtcUpDownClient(sdk) as client:
        received = [event async for event in client.stream(make_market())]

    assert [type(e) for e in received] == [
        BookUpdate,
        QuoteUpdate,
        TradeUpdate,
        BtcPriceUpdate,
    ]

    book_update = received[0]
    assert book_update.outcome == "up"
    assert isinstance(book_update.book, Book)
    assert book_update.book.best_bid == Decimal("0.45")
    assert book_update.book.best_ask == Decimal("0.50")

    quote_update = received[1]
    assert quote_update.outcome == "down"
    assert quote_update.quote.best_bid == Decimal("0.50")
    assert quote_update.quote.best_ask == Decimal("0.56")

    trade = received[2]
    assert trade.outcome == "up"
    assert trade.price == Decimal("0.47")
    assert trade.side == "BUY"

    btc = received[3]
    assert btc.price == Decimal("97123.45")

    assert sdk.handle.closed is True


async def test_stream_without_btc_price_subscribes_market_only():
    sdk = FakeSdk(stream_events=[])
    async with BtcUpDownClient(sdk) as client:
        _ = [e async for e in client.stream(make_market(), include_btc_price=False)]
    assert len(sdk.subscribed_specs) == 1


async def test_stream_closes_handle_when_consumer_bails():
    sdk = FakeSdk(stream_events=stream_fixture_events())
    async with BtcUpDownClient(sdk) as client:
        stream = client.stream(make_market())
        async for _ in stream:
            break
        await stream.aclose()
    assert sdk.handle.closed is True


# -- live smoke tests -------------------------------------------------------


async def test_live_current_market_and_books():
    async with BtcUpDownClient() as client:
        market = await client.get_current_market()
        books = await client.get_books(market)

    assert market.slug.startswith("btc-updown-15m-")
    assert market.closed is False
    assert set(books) == {"up", "down"}
    for book in books.values():
        if book.bids and book.asks:
            assert book.best_bid < book.best_ask


async def test_live_resolved_market_price_history():
    async with BtcUpDownClient() as client:
        market = await client.get_market(WINDOW_EPOCH)
        points = await client.get_price_history(market, "up")

    assert market.closed is True
    assert market.outcome in ("up", "down")
    assert len(points) > 0
    assert all(Decimal("0") <= p.price <= Decimal("1") for p in points)
