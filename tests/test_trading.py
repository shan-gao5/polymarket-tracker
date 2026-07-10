"""Deterministic safety tests for paper and live execution infrastructure."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import polymarket
import pytest
from polymarket.models.clob.order_book import OrderBook
from sqlmodel import select

from polytracker.discovery import MarketWindow
from polytracker.store import TradeFill, TradeIntent, get_session, make_engine
from polytracker.trading.config import AuthConfig, ConfigurationError, RiskConfig
from polytracker.trading.domain import RiskViolation, prepare_buy_order
from polytracker.trading.execution import PaperExecutor
from polytracker.trading.repository import DuplicateIntentError, has_live_intent_for_market
from polytracker.trading.service import execute_prepared, wait_for_user_event


def make_market(now: datetime) -> MarketWindow:
    return MarketWindow(
        slug="btc-updown-15m-1234567800",
        condition_id="0xcondition",
        up_token_id="up-token",
        down_token_id="down-token",
        window_start=now - timedelta(seconds=10),
        window_end=now + timedelta(minutes=10),
        closed=False,
        outcome=None,
    )


def make_book(
    now: datetime,
    *,
    token_id: str = "up-token",
    min_order_size: str = "5",
    asks: list[dict[str, str]] | None = None,
) -> OrderBook:
    return OrderBook.model_validate(
        {
            "market": "0xcondition",
            "asset_id": token_id,
            "timestamp": now,
            "bids": [{"price": "0.49", "size": "20"}],
            "asks": asks
            or [
                {"price": "0.60", "size": "20"},
                {"price": "0.50", "size": "20"},
            ],
            "min_order_size": min_order_size,
            "tick_size": "0.01",
            "neg_risk": False,
            "last_trade_price": "0.50",
            "hash": "book-hash",
        }
    )


def test_auth_config_requires_paired_relayer_credentials(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", "0x" + "22" * 20)
    monkeypatch.setenv("POLYMARKET_RELAYER_API_KEY", "relayer-secret")

    with pytest.raises(ConfigurationError, match="must be provided together"):
        AuthConfig.from_env()


def test_auth_config_repr_redacts_private_key(monkeypatch):
    private_key = "0x" + "11" * 32
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", private_key)
    monkeypatch.setenv("POLYMARKET_WALLET_ADDRESS", "0x" + "22" * 20)

    config = AuthConfig.from_env()

    assert private_key not in repr(config)


def test_auth_config_accepts_exported_key_aliases(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_WALLET_ADDRESS", raising=False)
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("ACCOUNT_ADDRESS", "0x" + "22" * 20)

    config = AuthConfig.from_env()

    assert config.wallet_address == "0x" + "22" * 20


def test_risk_config_rejects_live_cap_above_five(monkeypatch):
    monkeypatch.setenv("POLYTRACKER_MAX_LIVE_SPEND", "5.01")

    with pytest.raises(ConfigurationError, match="cannot exceed 5"):
        RiskConfig.from_env()


def test_prepare_buy_order_uses_minimum_size_and_one_tick_guard():
    now = datetime.now(timezone.utc)

    order = prepare_buy_order(
        mode="live",
        market=make_market(now),
        outcome="up",
        book=make_book(now),
        risk=RiskConfig(),
        now=now,
    )

    assert order.amount == Decimal("2.55")
    assert order.max_spend == Decimal("5")
    assert order.max_price == Decimal("0.51")
    assert order.intent_id == "live:btc-updown-15m-1234567800:buy-test"


def test_prepare_buy_order_rejects_stale_book():
    now = datetime.now(timezone.utc)

    with pytest.raises(RiskViolation, match="order book age"):
        prepare_buy_order(
            mode="live",
            market=make_market(now),
            outcome="up",
            book=make_book(now - timedelta(seconds=6)),
            risk=RiskConfig(max_book_age_seconds=5),
            now=now,
        )


def test_prepare_buy_order_rejects_expiry_cutoff():
    now = datetime.now(timezone.utc)
    market = make_market(now)
    market = MarketWindow(
        slug=market.slug,
        condition_id=market.condition_id,
        up_token_id=market.up_token_id,
        down_token_id=market.down_token_id,
        window_start=market.window_start,
        window_end=now + timedelta(seconds=119),
        closed=False,
        outcome=None,
    )

    with pytest.raises(RiskViolation, match="requires at least"):
        prepare_buy_order(
            mode="live",
            market=market,
            outcome="up",
            book=make_book(now),
            risk=RiskConfig(),
            now=now,
        )


def test_prepare_buy_order_rejects_minimum_above_cap():
    now = datetime.now(timezone.utc)

    with pytest.raises(RiskViolation, match="above the 5 cap"):
        prepare_buy_order(
            mode="live",
            market=make_market(now),
            outcome="up",
            book=make_book(now, min_order_size="10"),
            risk=RiskConfig(),
            now=now,
        )


async def test_paper_execution_persists_intent_and_fills(tmp_path):
    now = datetime.now(timezone.utc)
    engine = make_engine(tmp_path / "trading.db")
    session = get_session(engine)
    order = prepare_buy_order(
        mode="paper",
        market=make_market(now),
        outcome="up",
        book=make_book(now),
        risk=RiskConfig(),
        now=now,
    )

    intent, result = await execute_prepared(
        session=session,
        executor=PaperExecutor(),
        order=order,
        book=make_book(now),
    )

    assert result.accepted
    assert result.status == "matched"
    assert result.making_amount == Decimal("2.55")
    assert result.taking_amount == Decimal("5.1")
    assert intent.status == "matched"
    assert session.exec(select(TradeFill).where(TradeFill.intent_id == intent.id)).all()
    session.close()


async def test_duplicate_intent_cannot_execute_twice(tmp_path):
    now = datetime.now(timezone.utc)
    engine = make_engine(tmp_path / "duplicate.db")
    session = get_session(engine)
    book = make_book(now)
    order = prepare_buy_order(
        mode="live",
        market=make_market(now),
        outcome="up",
        book=book,
        risk=RiskConfig(),
        now=now,
    )

    await execute_prepared(
        session=session,
        executor=PaperExecutor(),
        order=order,
        book=book,
    )

    with pytest.raises(DuplicateIntentError):
        await execute_prepared(
            session=session,
            executor=PaperExecutor(),
            order=order,
            book=book,
        )
    assert has_live_intent_for_market(session, order.market.slug)
    session.close()


async def test_ambiguous_submission_is_not_retried(tmp_path):
    class AmbiguousExecutor:
        calls = 0

        async def execute(self, order, book):
            self.calls += 1
            raise TimeoutError("response lost after submission")

    now = datetime.now(timezone.utc)
    engine = make_engine(tmp_path / "ambiguous.db")
    session = get_session(engine)
    book = make_book(now)
    order = prepare_buy_order(
        mode="live",
        market=make_market(now),
        outcome="down",
        book=make_book(now, token_id="down-token"),
        risk=RiskConfig(),
        now=now,
    )
    executor = AmbiguousExecutor()

    with pytest.raises(TimeoutError):
        await execute_prepared(
            session=session,
            executor=executor,
            order=order,
            book=make_book(now, token_id="down-token"),
        )
    persisted = session.get(TradeIntent, order.intent_id)
    assert persisted is not None
    assert persisted.status == "unknown"

    with pytest.raises(DuplicateIntentError):
        await execute_prepared(
            session=session,
            executor=executor,
            order=order,
            book=book,
        )
    assert executor.calls == 1
    session.close()


async def test_closed_user_stream_is_non_fatal():
    class ClosedStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    assert await wait_for_user_event(ClosedStream(), token_id="token") is None


async def test_authenticated_wallet_readiness_opt_in():
    if os.environ.get("POLYTRACKER_RUN_AUTH_TESTS", "").lower() != "true":
        pytest.skip("set POLYTRACKER_RUN_AUTH_TESTS=true to run authenticated reads")

    auth = AuthConfig.from_env()
    async with await polymarket.AsyncSecureClient.create(**auth.secure_client_kwargs()) as client:
        collateral = await client.get_balance_allowance(asset_type="COLLATERAL")
        open_orders = await client.list_open_orders().first_page()
        assert client.wallet_type == "POLY_PROXY"

    assert collateral.balance >= 0
    assert isinstance(open_orders.items, tuple)
