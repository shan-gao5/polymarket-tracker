"""Deterministic tests for the btcupdown paper trading engine."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from btcupdown import (
    Book,
    BookLevel,
    BookMismatchError,
    InsufficientCashError,
    InsufficientSharesError,
    Market,
    MarketClosedError,
    NoLiquidityError,
    PaperAccount,
    slug_for,
    window_bounds,
)

ALIGNED_EPOCH = 1_750_000_500
START, END = window_bounds(ALIGNED_EPOCH)
MID_WINDOW = START + timedelta(minutes=5)
SLUG = slug_for(ALIGNED_EPOCH)


def make_market(**overrides) -> Market:
    fields = dict(
        slug=SLUG,
        condition_id="0xcondition",
        up_token_id="up-token",
        down_token_id="down-token",
        window_start=START,
        window_end=END,
    )
    fields.update(overrides)
    return Market(**fields)


def make_book(token_id: str = "up-token", *, bids=None, asks=None) -> Book:
    return Book(
        token_id=token_id,
        bids=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in (bids or [("0.45", "100")])),
        asks=tuple(BookLevel(Decimal(p), Decimal(s)) for p, s in (asks or [("0.50", "100")])),
    )


def test_buy_fills_at_ask_and_updates_cash_and_position():
    account = PaperAccount(starting_cash=Decimal("100"))
    trade = account.buy(make_market(), "up", Decimal("10"), make_book(), now=MID_WINDOW)

    assert trade.side == "buy"
    assert trade.shares == Decimal("20")
    assert trade.notional == Decimal("10.00")
    assert trade.average_price == Decimal("0.50")
    assert account.cash == Decimal("90.00")
    position = account.position(SLUG, "up")
    assert position.shares == Decimal("20")
    assert position.cost == Decimal("10.00")
    assert position.average_price == Decimal("0.50")


def test_buy_walks_multiple_levels_with_price_impact():
    book = make_book(asks=[("0.50", "10"), ("0.60", "100")])
    account = PaperAccount(starting_cash=Decimal("100"))
    trade = account.buy(make_market(), "up", Decimal("11"), book, now=MID_WINDOW)

    # 10 shares at 0.50 (5.00), then 10 shares at 0.60 (6.00).
    assert [(f.price, f.shares) for f in trade.fills] == [
        (Decimal("0.50"), Decimal("10")),
        (Decimal("0.60"), Decimal("10")),
    ]
    assert trade.notional == Decimal("11.00")
    assert trade.average_price == Decimal("0.55")


def test_buy_respects_max_price_limit():
    book = make_book(asks=[("0.50", "1"), ("0.99", "100")])
    account = PaperAccount(starting_cash=Decimal("100"))
    trade = account.buy(
        make_market(), "up", Decimal("10"), book, max_price=Decimal("0.60"), now=MID_WINDOW
    )
    assert trade.shares == Decimal("1")
    assert trade.notional == Decimal("0.50")


def test_buy_is_fill_and_kill_when_book_is_shallow():
    book = make_book(asks=[("0.50", "10")])
    account = PaperAccount(starting_cash=Decimal("100"))
    trade = account.buy(make_market(), "up", Decimal("50"), book, now=MID_WINDOW)
    assert trade.shares == Decimal("10")
    assert account.cash == Decimal("95.00")


def test_buy_skips_dust_level_and_fills_deeper_liquidity():
    book = make_book(asks=[("0.50", "0.0000005"), ("0.60", "100")])
    account = PaperAccount(starting_cash=Decimal("100"))
    trade = account.buy(make_market(), "up", Decimal("6"), book, now=MID_WINDOW)
    assert [(f.price, f.shares) for f in trade.fills] == [
        (Decimal("0.60"), Decimal("10")),
    ]


def test_buy_stops_when_remaining_spend_is_below_share_precision():
    book = make_book(asks=[("0.50", "100"), ("0.60", "100")])
    account = PaperAccount(starting_cash=Decimal("100"))
    with pytest.raises(NoLiquidityError):
        account.buy(make_market(), "up", Decimal("0.0000001"), book, now=MID_WINDOW)


def test_buy_rejects_when_nothing_within_limit():
    account = PaperAccount(starting_cash=Decimal("100"))
    with pytest.raises(NoLiquidityError):
        account.buy(
            make_market(),
            "up",
            Decimal("10"),
            make_book(asks=[("0.80", "100")]),
            max_price=Decimal("0.60"),
            now=MID_WINDOW,
        )


def test_buy_requires_cash():
    account = PaperAccount(starting_cash=Decimal("5"))
    with pytest.raises(InsufficientCashError):
        account.buy(make_market(), "up", Decimal("10"), make_book(), now=MID_WINDOW)


def test_buy_rejects_closed_and_expired_markets():
    account = PaperAccount(starting_cash=Decimal("100"))
    with pytest.raises(MarketClosedError):
        account.buy(make_market(closed=True), "up", Decimal("10"), make_book(), now=MID_WINDOW)
    with pytest.raises(MarketClosedError):
        account.buy(make_market(), "up", Decimal("10"), make_book(), now=END)


def test_buy_rejects_book_for_wrong_token():
    account = PaperAccount(starting_cash=Decimal("100"))
    with pytest.raises(BookMismatchError):
        account.buy(
            make_market(), "down", Decimal("10"), make_book("up-token"), now=MID_WINDOW
        )


def test_sell_books_realized_pnl_against_average_cost():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)  # 20 shares at 0.50

    trade = account.sell(
        market, "up", Decimal("10"), make_book(bids=[("0.60", "50")]), now=MID_WINDOW
    )
    assert trade.notional == Decimal("6.00")
    assert trade.realized_pnl == Decimal("1.00")  # sold cost basis 5.00 for 6.00
    assert account.cash == Decimal("96.00")
    assert account.realized_pnl == Decimal("1.00")
    position = account.position(SLUG, "up")
    assert position.shares == Decimal("10")
    assert position.cost == Decimal("5.00")


def test_selling_entire_position_removes_it():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)
    account.sell(market, "up", Decimal("20"), make_book(bids=[("0.55", "50")]), now=MID_WINDOW)
    assert account.position(SLUG, "up") is None
    assert account.positions == ()


def test_sell_requires_shares():
    account = PaperAccount(starting_cash=Decimal("100"))
    with pytest.raises(InsufficientSharesError):
        account.sell(make_market(), "up", Decimal("1"), make_book(), now=MID_WINDOW)


def test_sell_is_fill_and_kill_when_bids_are_shallow():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)  # 20 shares
    trade = account.sell(
        market, "up", Decimal("20"), make_book(bids=[("0.45", "5")]), now=MID_WINDOW
    )
    assert trade.shares == Decimal("5")
    assert account.position(SLUG, "up").shares == Decimal("15")


def test_sell_skips_dust_level_and_fills_deeper_liquidity():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)  # 20 shares
    trade = account.sell(
        market,
        "up",
        Decimal("20"),
        make_book(bids=[("0.55", "0.0000005"), ("0.45", "50")]),
        now=MID_WINDOW,
    )
    assert [(f.price, f.shares) for f in trade.fills] == [
        (Decimal("0.45"), Decimal("20")),
    ]


def test_sell_stops_when_remaining_shares_are_below_precision():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)  # 20 shares
    trade = account.sell(
        market,
        "up",
        Decimal("20"),
        make_book(bids=[("0.55", "20"), ("0.45", "50")]),
        now=MID_WINDOW,
    )
    assert [(f.price, f.shares) for f in trade.fills] == [
        (Decimal("0.55"), Decimal("20")),
    ]
    assert account.position(SLUG, "up") is None


def test_sell_rejects_when_no_bid_within_limit():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)
    with pytest.raises(NoLiquidityError):
        account.sell(
            market,
            "up",
            Decimal("5"),
            make_book(bids=[("0.30", "100")]),
            min_price=Decimal("0.40"),
            now=MID_WINDOW,
        )


def test_winning_settlement_pays_one_per_share():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)  # 20 shares, cost 10

    resolved = make_market(closed=True, outcome="up")
    settlement = account.settle(resolved, now=END)
    assert settlement.payout == Decimal("20")
    assert settlement.pnl == Decimal("10.00")
    assert account.cash == Decimal("110.00")
    assert account.realized_pnl == Decimal("10.00")
    assert account.positions == ()


def test_losing_settlement_expires_worthless():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)

    settlement = account.settle(make_market(closed=True, outcome="down"), now=END)
    assert settlement.payout == Decimal("0")
    assert settlement.pnl == Decimal("-10.00")
    assert account.cash == Decimal("90.00")


def test_settlement_nets_both_sides():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book("up-token"), now=MID_WINDOW)  # 20 shares
    account.buy(
        market,
        "down",
        Decimal("9"),
        make_book("down-token", asks=[("0.45", "100")]),
        now=MID_WINDOW,
    )  # 20 shares

    settlement = account.settle(make_market(closed=True, outcome="up"), now=END)
    assert settlement.winning_shares == Decimal("20")
    assert settlement.losing_shares == Decimal("20")
    assert settlement.cost_basis == Decimal("19.00")
    assert settlement.pnl == Decimal("1.00")
    assert account.cash == Decimal("101.00")


def test_settle_without_position_returns_none():
    account = PaperAccount(starting_cash=Decimal("100"))
    assert account.settle(make_market(closed=True, outcome="up")) is None
    assert account.settlements == []


def test_settle_requires_resolved_market():
    account = PaperAccount(starting_cash=Decimal("100"))
    with pytest.raises(ValueError, match="not resolved"):
        account.settle(make_market())


def test_equity_and_unrealized_pnl_use_marks():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)  # 20 shares at 0.50

    marks = {(SLUG, "up"): Decimal("0.55")}
    assert account.equity(marks) == Decimal("101.00")
    assert account.unrealized_pnl(marks) == Decimal("1.00")
    with pytest.raises(KeyError, match="no mark supplied"):
        account.equity({})


def test_equity_with_no_positions_needs_no_marks():
    account = PaperAccount(starting_cash=Decimal("250"))
    assert account.equity() == Decimal("250")


def test_full_round_trip_conserves_value():
    account = PaperAccount(starting_cash=Decimal("100"))
    market = make_market()
    account.buy(market, "up", Decimal("10"), make_book(), now=MID_WINDOW)
    account.sell(market, "up", Decimal("5"), make_book(bids=[("0.52", "50")]), now=MID_WINDOW)
    account.settle(make_market(closed=True, outcome="up"), now=END)

    # cash change must equal cumulative realized pnl once everything is flat
    assert account.positions == ()
    assert account.cash - account.starting_cash == account.realized_pnl
    assert len(account.trades) == 2
    assert len(account.settlements) == 1
