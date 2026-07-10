"""Tests for the btcupdown package's window math and normalized types."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcupdown import (
    Book,
    BookLevel,
    Market,
    epoch_for_slug,
    other_outcome,
    slug_for,
    window_bounds,
    window_start_for,
)

ALIGNED_EPOCH = 1_750_000_500  # divisible by 900


def test_window_start_for_aligns_down():
    assert window_start_for(ALIGNED_EPOCH) == ALIGNED_EPOCH
    assert window_start_for(ALIGNED_EPOCH + 899.9) == ALIGNED_EPOCH
    assert window_start_for(ALIGNED_EPOCH + 900) == ALIGNED_EPOCH + 900


def test_window_start_for_accepts_datetimes():
    aware = datetime.fromtimestamp(ALIGNED_EPOCH + 60, tz=timezone.utc)
    naive = aware.replace(tzinfo=None)
    assert window_start_for(aware) == ALIGNED_EPOCH
    assert window_start_for(naive) == ALIGNED_EPOCH


def test_slug_round_trip():
    slug = slug_for(ALIGNED_EPOCH)
    assert slug == f"btc-updown-15m-{ALIGNED_EPOCH}"
    assert epoch_for_slug(slug) == ALIGNED_EPOCH


def test_slug_for_rejects_misaligned_epoch():
    with pytest.raises(ValueError, match="not aligned"):
        slug_for(ALIGNED_EPOCH + 1)


@pytest.mark.parametrize(
    "slug",
    ["eth-updown-15m-1750000500", "btc-updown-15m-notanumber", "btc-updown-15m-1750000501"],
)
def test_epoch_for_slug_rejects_foreign_slugs(slug):
    with pytest.raises(ValueError):
        epoch_for_slug(slug)


def test_window_bounds_span_15_minutes():
    start, end = window_bounds(ALIGNED_EPOCH)
    assert start == datetime.fromtimestamp(ALIGNED_EPOCH, tz=timezone.utc)
    assert end - start == timedelta(minutes=15)


def test_other_outcome():
    assert other_outcome("up") == "down"
    assert other_outcome("down") == "up"


def make_market(**overrides) -> Market:
    start, end = window_bounds(ALIGNED_EPOCH)
    fields = dict(
        slug=slug_for(ALIGNED_EPOCH),
        condition_id="0xcondition",
        up_token_id="up-token",
        down_token_id="down-token",
        window_start=start,
        window_end=end,
    )
    fields.update(overrides)
    return Market(**fields)


def test_market_token_helpers():
    market = make_market()
    assert market.token_id("up") == "up-token"
    assert market.token_id("down") == "down-token"
    assert market.outcome_for_token("up-token") == "up"
    assert market.outcome_for_token("down-token") == "down"
    with pytest.raises(ValueError, match="does not belong"):
        market.outcome_for_token("other-token")
    assert market.window_start_epoch == ALIGNED_EPOCH


def test_market_seconds_remaining_clamps_at_zero():
    market = make_market()
    assert market.seconds_remaining(now=market.window_start) == 900.0
    assert market.seconds_remaining(now=market.window_end + timedelta(minutes=1)) == 0.0


def test_book_normalizes_clob_ordering_and_drops_empty_levels():
    # Raw CLOB payloads sort both sides best-last; Book must flip to best-first.
    book = Book(
        token_id="up-token",
        bids=(
            BookLevel(Decimal("0.30"), Decimal("10")),
            BookLevel(Decimal("0.40"), Decimal("0")),
            BookLevel(Decimal("0.45"), Decimal("5")),
        ),
        asks=(
            BookLevel(Decimal("0.70"), Decimal("10")),
            BookLevel(Decimal("0.55"), Decimal("5")),
        ),
    )
    assert [level.price for level in book.bids] == [Decimal("0.45"), Decimal("0.30")]
    assert [level.price for level in book.asks] == [Decimal("0.55"), Decimal("0.70")]
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.55")
    assert book.midpoint == Decimal("0.50")
    assert book.spread == Decimal("0.10")


def test_empty_book_has_no_quotes():
    book = Book(token_id="up-token", bids=(), asks=())
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.midpoint is None
    assert book.spread is None


def test_book_from_clob_duck_types():
    class Level:
        def __init__(self, price, size):
            self.price = price
            self.size = size

    class ClobBook:
        token_id = "up-token"
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bids = [Level("0.40", "10"), Level("0.45", "5")]
        asks = [Level("0.70", "10"), Level("0.55", "5")]

    book = Book.from_clob(ClobBook())
    assert book.token_id == "up-token"
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.55")
    assert book.timestamp == ClobBook.timestamp
