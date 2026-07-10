"""Paper and live executors sharing the same prepared-order contract."""

from __future__ import annotations

from decimal import Decimal

import polymarket
from polymarket.models.clob.order_book import OrderBook

from polytracker.trading.domain import ExecutionResult, Fill, PreparedOrder


class PaperExecutor:
    """Simulate a FAK buy against a real order-book snapshot."""

    async def execute(self, order: PreparedOrder, book: OrderBook) -> ExecutionResult:
        remaining = order.amount
        spent = Decimal("0")
        shares = Decimal("0")
        fills: list[Fill] = []

        for index, level in enumerate(reversed(book.asks)):
            if level.price > order.max_price or remaining <= 0:
                continue
            level_spend = level.price * level.size
            fill_spend = min(remaining, level_spend)
            fill_size = fill_spend / level.price
            if fill_size <= 0:
                continue
            spent += fill_spend
            shares += fill_size
            remaining -= fill_spend
            fills.append(
                Fill(
                    trade_id=f"paper:{order.intent_id}:{index}",
                    price=level.price,
                    size=fill_size,
                    status="CONFIRMED",
                )
            )

        if shares <= 0:
            return ExecutionResult(
                accepted=False,
                status="rejected",
                error_code="fak_not_filled",
                error_message="no asks were available within the maximum price",
            )
        return ExecutionResult(
            accepted=True,
            status="matched",
            order_id=f"paper:{order.intent_id}",
            making_amount=spent,
            taking_amount=shares,
            fills=tuple(fills),
        )


class LiveExecutor:
    """Submit one protected FAK buy through the unified secure SDK."""

    def __init__(self, client: polymarket.AsyncSecureClient) -> None:
        self.client = client

    async def execute(self, order: PreparedOrder, _book: OrderBook) -> ExecutionResult:
        response = await self.client.place_market_order(
            token_id=order.token_id,
            side="BUY",
            amount=order.amount,
            max_spend=order.max_spend,
            max_price=order.max_price,
            order_type="FAK",
        )
        if not response.ok:
            return ExecutionResult(
                accepted=False,
                status="rejected",
                error_code=response.code,
                error_message=response.message,
            )
        return ExecutionResult(
            accepted=True,
            status=response.status,
            order_id=response.order_id,
            making_amount=response.making_amount,
            taking_amount=response.taking_amount,
        )
