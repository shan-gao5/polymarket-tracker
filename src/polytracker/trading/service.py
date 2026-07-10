"""Execution orchestration, paper gating, and remote reconciliation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

import polymarket
from polymarket.models.clob.order_book import OrderBook

from polytracker.discovery import (
    MarketNotFoundError,
    MarketWindow,
    get_current_market,
    get_next_market,
)
from polytracker.store import PaperTradingRun, TradeIntent
from polytracker.trading.config import RiskConfig
from polytracker.trading.domain import ExecutionResult, Fill, PreparedOrder, TradeOutcome, prepare_buy_order
from polytracker.trading.repository import (
    claim_intent,
    complete_paper_run,
    create_paper_run,
    fail_paper_run,
    mark_submitting,
    mark_unknown,
    record_result,
)


class Executor(Protocol):
    async def execute(self, order: PreparedOrder, book: OrderBook) -> ExecutionResult: ...


async def execute_prepared(
    *,
    session,
    executor: Executor,
    order: PreparedOrder,
    book: OrderBook,
) -> tuple[TradeIntent, ExecutionResult]:
    """Claim an intent, execute once, and persist every terminal transition."""
    intent = claim_intent(session, order)
    mark_submitting(session, intent)
    try:
        result = await executor.execute(order, book)
    except BaseException as error:
        mark_unknown(session, intent, error)
        raise
    record_result(session, intent, result)
    return intent, result


async def _next_market_with_retry(
    client: polymarket.AsyncPublicClient,
    current: MarketWindow,
    *,
    retries: int = 20,
    delay: float = 2.0,
) -> MarketWindow:
    for attempt in range(retries):
        try:
            return await get_next_market(client, current)
        except MarketNotFoundError:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _wait_until(target: datetime) -> None:
    while True:
        remaining = (target - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 30.0))


async def _select_full_paper_market(
    client: polymarket.AsyncPublicClient,
) -> MarketWindow:
    current = await get_current_market(client)
    elapsed = (datetime.now(timezone.utc) - current.window_start).total_seconds()
    if elapsed <= 10:
        return current
    await _wait_until(current.window_end)
    return await _next_market_with_retry(client, current)


async def run_paper_gate(
    *,
    client: polymarket.AsyncPublicClient,
    session,
    executor: Executor,
    outcome: TradeOutcome,
    risk: RiskConfig,
) -> tuple[PaperTradingRun, TradeIntent, ExecutionResult]:
    """Execute one paper intent and observe a complete market rollover."""
    market = await _select_full_paper_market(client)
    await _wait_until(market.window_start)
    book = await client.get_order_book(token_id=(market.up_token_id if outcome == "up" else market.down_token_id))
    order = prepare_buy_order(
        mode="paper",
        market=market,
        outcome=outcome,
        book=book,
        risk=risk,
    )
    run = create_paper_run(session, order)
    try:
        intent, result = await execute_prepared(
            session=session,
            executor=executor,
            order=order,
            book=book,
        )
        if not result.accepted:
            raise RuntimeError(result.error_message or "paper order was rejected")

        while datetime.now(timezone.utc) < market.window_end:
            await asyncio.sleep(
                min(30.0, max(0.1, (market.window_end - datetime.now(timezone.utc)).total_seconds()))
            )
            if datetime.now(timezone.utc) < market.window_end:
                await client.get_order_book(token_id=order.token_id)
        await _next_market_with_retry(client, market)
    except BaseException as error:
        fail_paper_run(session, run, error)
        raise
    complete_paper_run(session, run)
    return run, intent, result


def _trade_belongs_to_order(trade, order_id: str) -> bool:
    if trade.taker_order_id == order_id:
        return True
    return any(maker.order_id == order_id for maker in trade.maker_orders)


async def reconcile_intent(
    client: polymarket.AsyncSecureClient,
    session,
    intent: TradeIntent,
) -> ExecutionResult | None:
    """Refresh a known remote order without ever creating a replacement."""
    if not intent.order_id:
        intent.reconciled_at = datetime.now(timezone.utc)
        intent.updated_at = intent.reconciled_at
        session.add(intent)
        session.commit()
        return None

    trades_page = await client.list_account_trades(
        token_id=intent.token_id,
        market=intent.condition_id,
    ).first_page()
    matching = tuple(
        trade for trade in trades_page.items if _trade_belongs_to_order(trade, intent.order_id)
    )
    if matching:
        fills = tuple(
            Fill(
                trade_id=trade.id,
                price=trade.price,
                size=trade.size,
                status=trade.status,
                transaction_hash=trade.transaction_hash or None,
                matched_at=trade.matched_at,
            )
            for trade in matching
        )
        taking = sum((fill.size for fill in fills), start=Decimal("0"))
        making = sum((fill.price * fill.size for fill in fills), start=Decimal("0"))
        result = ExecutionResult(
            accepted=True,
            status="matched",
            order_id=intent.order_id,
            making_amount=making,
            taking_amount=taking,
            fills=fills,
        )
        record_result(session, intent, result, reconciled=True)
        return result

    open_page = await client.list_open_orders(id=intent.order_id).first_page()
    if open_page.items:
        order = open_page.items[0]
        result = ExecutionResult(
            accepted=True,
            status=order.status.lower(),
            order_id=intent.order_id,
            making_amount=order.price * order.size_matched,
            taking_amount=order.size_matched,
        )
        record_result(session, intent, result, reconciled=True)
        return result

    intent.reconciled_at = datetime.now(timezone.utc)
    intent.updated_at = intent.reconciled_at
    session.add(intent)
    session.commit()
    return None


async def wait_for_user_event(handle, *, token_id: str, timeout: float = 10.0) -> str | None:
    """Return the first matching user event type, or None on timeout."""

    async def _consume() -> str:
        async for event in handle:
            if str(event.payload.token_id) == token_id:
                return event.type
        raise RuntimeError("user event stream closed")

    try:
        return await asyncio.wait_for(_consume(), timeout=timeout)
    except (RuntimeError, TimeoutError):
        return None
