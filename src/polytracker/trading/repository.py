"""Persistence and reconciliation helpers for execution records."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from polytracker.store import PaperTradingRun, TradeFill, TradeIntent
from polytracker.trading.domain import ExecutionResult, Fill, PreparedOrder


class DuplicateIntentError(RuntimeError):
    """Raised when an intent id has already been claimed."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def claim_intent(session: Session, order: PreparedOrder) -> TradeIntent:
    if session.get(TradeIntent, order.intent_id) is not None:
        raise DuplicateIntentError(f"intent {order.intent_id} already exists")
    now = utc_now()
    row = TradeIntent(
        id=order.intent_id,
        created_at=now,
        updated_at=now,
        mode=order.mode,
        market_slug=order.market.slug,
        condition_id=order.market.condition_id,
        token_id=order.token_id,
        outcome=order.outcome,
        side="BUY",
        requested_amount=str(order.amount),
        max_spend=str(order.max_spend),
        max_price=str(order.max_price),
        status="prepared",
    )
    session.add(row)
    session.commit()
    return row


def mark_submitting(session: Session, intent: TradeIntent) -> None:
    intent.status = "submitting"
    intent.updated_at = utc_now()
    session.add(intent)
    session.commit()


def mark_unknown(session: Session, intent: TradeIntent, error: BaseException) -> None:
    intent.status = "unknown"
    intent.error_code = type(error).__name__
    intent.error_message = str(error)
    intent.updated_at = utc_now()
    session.add(intent)
    session.commit()


def _add_fill(session: Session, intent_id: str, fill: Fill) -> None:
    existing = session.exec(
        select(TradeFill).where(
            TradeFill.intent_id == intent_id,
            TradeFill.trade_id == fill.trade_id,
        )
    ).first()
    if existing is not None:
        existing.price = str(fill.price)
        existing.size = str(fill.size)
        existing.status = fill.status
        existing.transaction_hash = fill.transaction_hash
        existing.matched_at = fill.matched_at
        session.add(existing)
        return
    session.add(
        TradeFill(
            intent_id=intent_id,
            trade_id=fill.trade_id,
            price=str(fill.price),
            size=str(fill.size),
            status=fill.status,
            transaction_hash=fill.transaction_hash,
            matched_at=fill.matched_at,
        )
    )


def record_result(
    session: Session,
    intent: TradeIntent,
    result: ExecutionResult,
    *,
    reconciled: bool = False,
) -> None:
    intent.order_id = result.order_id
    intent.remote_status = result.status
    intent.making_amount = str(result.making_amount)
    intent.taking_amount = str(result.taking_amount)
    average = result.average_fill_price
    intent.average_fill_price = str(average) if average is not None else None
    intent.error_code = result.error_code
    intent.error_message = result.error_message
    if not result.accepted:
        intent.status = "rejected"
    elif result.taking_amount > 0 or result.status == "matched":
        intent.status = "matched"
    else:
        intent.status = result.status
    intent.updated_at = utc_now()
    if reconciled:
        intent.reconciled_at = intent.updated_at
    session.add(intent)
    for fill in result.fills:
        _add_fill(session, intent.id, fill)
    session.commit()


def create_paper_run(session: Session, order: PreparedOrder) -> PaperTradingRun:
    run = PaperTradingRun(
        id=f"paper-run:{order.market.slug}",
        market_slug=order.market.slug,
        started_at=utc_now(),
        window_start=order.market.window_start,
        window_end=order.market.window_end,
        status="running",
        intent_id=order.intent_id,
    )
    if session.get(PaperTradingRun, run.id) is not None:
        raise DuplicateIntentError(f"paper run for {order.market.slug} already exists")
    session.add(run)
    session.commit()
    return run


def complete_paper_run(session: Session, run: PaperTradingRun) -> None:
    run.status = "passed"
    run.completed_at = utc_now()
    session.add(run)
    session.commit()


def fail_paper_run(session: Session, run: PaperTradingRun, error: BaseException) -> None:
    run.status = "failed"
    run.completed_at = utc_now()
    run.error_message = str(error)
    session.add(run)
    session.commit()


def has_passed_paper_gate(session: Session) -> bool:
    return session.exec(
        select(PaperTradingRun).where(PaperTradingRun.status == "passed")
    ).first() is not None


def has_live_intent_for_market(session: Session, market_slug: str) -> bool:
    return session.exec(
        select(TradeIntent).where(
            TradeIntent.mode == "live",
            TradeIntent.market_slug == market_slug,
        )
    ).first() is not None


def live_intents(session: Session) -> tuple[TradeIntent, ...]:
    return tuple(
        session.exec(select(TradeIntent).where(TradeIntent.mode == "live")).all()
    )
