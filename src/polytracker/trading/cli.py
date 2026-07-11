"""Operational CLI for paper validation and deliberately gated live trading."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import polymarket
from polymarket.streams import UserSpec

from polytracker.discovery import MarketWindow, get_current_market
from polytracker.store import DEFAULT_DB_PATH, get_session, make_engine
from polytracker.trading.config import (
    AuthConfig,
    ConfigurationError,
    RiskConfig,
    live_trading_enabled,
)
from polytracker.trading.domain import RiskViolation, TradeOutcome, prepare_buy_order
from polytracker.trading.execution import LiveExecutor, PaperExecutor
from polytracker.trading.geo import check_geoblock
from polytracker.trading.repository import (
    DuplicateIntentError,
    has_live_intent_for_market,
    has_passed_paper_gate,
    live_intents,
)
from polytracker.trading.service import (
    execute_prepared,
    reconcile_intent,
    run_paper_gate,
    wait_for_user_event,
)

COLLATERAL_BASE_UNITS = Decimal("1000000")


def _db_path() -> Path:
    return Path(os.environ.get("POLYTRACKER_DB_PATH", DEFAULT_DB_PATH))


def _json(value: dict) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _required_allowance(client: polymarket.AsyncSecureClient, allowances: dict[str, int]) -> int:
    expected = client.environment.standard_exchange.lower()
    return next((value for address, value in allowances.items() if address.lower() == expected), 0)


async def _doctor() -> int:
    geo = await check_geoblock()
    auth = AuthConfig.from_env()
    async with await polymarket.AsyncSecureClient.create(**auth.secure_client_kwargs()) as client:
        market = await get_current_market(client)
        collateral = await client.get_balance_allowance(asset_type="COLLATERAL")
        open_orders = await client.list_open_orders().first_page()
        closed_only = await client.get_closed_only_mode()
        required_allowance = _required_allowance(client, collateral.allowances)
        ready = (
            not geo.blocked
            and not closed_only
            and client.wallet_type == "POLY_PROXY"
            and collateral.balance > 0
            and required_allowance > 0
        )
        _json(
            {
                "geoblock": {
                    "blocked": geo.blocked,
                    "country": geo.country,
                    "region": geo.region,
                },
                "account": {
                    "signer": client.signer,
                    "wallet": client.wallet,
                    "wallet_type": client.wallet_type,
                    "closed_only": closed_only,
                },
                "collateral": {
                    "balance": str(Decimal(collateral.balance) / COLLATERAL_BASE_UNITS),
                    "required_exchange_allowance": required_allowance,
                },
                "current_market": {
                    "slug": market.slug,
                    "condition_id": market.condition_id,
                    "seconds_remaining": max(
                        0, (market.window_end - datetime.now(timezone.utc)).total_seconds()
                    ),
                },
                "account_open_orders_first_page": len(open_orders.items),
                "relayer_credentials_configured": auth.relayer_api_key is not None,
                "ready": ready,
            }
        )
    return 0 if ready else 2


async def _setup() -> int:
    auth = AuthConfig.from_env(require_relayer=True)
    geo = await check_geoblock()
    if geo.blocked:
        raise RuntimeError(f"trading is blocked in {geo.country}/{geo.region}")
    async with await polymarket.AsyncSecureClient.create(**auth.secure_client_kwargs()) as client:
        await client.setup_trading_approvals()
        collateral = await client.get_balance_allowance(asset_type="COLLATERAL")
        _json(
            {
                "wallet": client.wallet,
                "wallet_type": client.wallet_type,
                "approvals": "ready",
                "required_exchange_allowance": _required_allowance(client, collateral.allowances),
            }
        )
    return 0


async def _paper_test(outcome: TradeOutcome) -> int:
    risk = RiskConfig.from_env()
    engine = make_engine(_db_path())
    session = get_session(engine)
    try:
        async with polymarket.AsyncPublicClient() as client:
            run, intent, result = await run_paper_gate(
                client=client,
                session=session,
                executor=PaperExecutor(),
                outcome=outcome,
                risk=risk,
            )
        _json(
            {
                "paper_gate": run.status,
                "market_slug": run.market_slug,
                "intent_id": intent.id,
                "spent": result.making_amount,
                "shares": result.taking_amount,
                "average_fill_price": result.average_fill_price,
            }
        )
        return 0
    finally:
        session.close()


def _check_live_preconditions(session, market: MarketWindow, confirmation: str) -> None:
    if not live_trading_enabled():
        raise RiskViolation("set POLYTRACKER_LIVE_ENABLED=true to enable live-test")
    if confirmation != market.slug:
        raise RiskViolation("--confirm must exactly match the current market slug")
    if not has_passed_paper_gate(session):
        raise RiskViolation("a completed paper-test is required before live-test")
    if has_live_intent_for_market(session, market.slug):
        raise RiskViolation("a live intent already exists for this market")


async def _live_test(outcome: TradeOutcome, confirmation: str) -> int:
    geo = await check_geoblock()
    if geo.blocked:
        raise RiskViolation(f"trading is blocked in {geo.country}/{geo.region}")
    auth = AuthConfig.from_env()
    risk = RiskConfig.from_env()
    engine = make_engine(_db_path())
    session = get_session(engine)
    try:
        async with await polymarket.AsyncSecureClient.create(**auth.secure_client_kwargs()) as client:
            if client.wallet_type != "POLY_PROXY":
                raise RiskViolation(
                    f"expected the Google-linked POLY_PROXY wallet, got {client.wallet_type}"
                )
            market = await get_current_market(client)
            _check_live_preconditions(session, market, confirmation)
            token_id = market.up_token_id if outcome == "up" else market.down_token_id
            book = await client.get_order_book(token_id=token_id)
            order = prepare_buy_order(
                mode="live",
                market=market,
                outcome=outcome,
                book=book,
                risk=risk,
            )
            collateral = await client.get_balance_allowance(asset_type="COLLATERAL")
            required_base_units = int(order.max_spend * COLLATERAL_BASE_UNITS)
            if collateral.balance < required_base_units:
                raise RiskViolation(
                    f"collateral balance is below the {order.max_spend} maximum spend"
                )
            if _required_allowance(client, collateral.allowances) < required_base_units:
                raise RiskViolation("required exchange allowance is missing; run setup")

            user_stream = await client.subscribe(UserSpec(markets=[market.condition_id]))
            event_task = asyncio.create_task(
                wait_for_user_event(user_stream, token_id=order.token_id)
            )
            try:
                intent, result = await execute_prepared(
                    session=session,
                    executor=LiveExecutor(client),
                    order=order,
                    book=book,
                )
                if result.accepted:
                    user_event = await event_task
                else:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    user_event = None
            finally:
                await user_stream.close()
                if not event_task.done():
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)

            if result.accepted:
                await asyncio.sleep(1)
                reconciled = await reconcile_intent(client, session, intent)
            else:
                reconciled = None
            _json(
                {
                    "accepted": result.accepted,
                    "status": result.status,
                    "order_id": result.order_id,
                    "market_slug": market.slug,
                    "outcome": outcome,
                    "requested_amount": order.amount,
                    "max_spend": order.max_spend,
                    "max_price": order.max_price,
                    "user_stream_event": user_event,
                    "reconciled_status": reconciled.status if reconciled else None,
                }
            )
            return 0 if result.accepted else 3
    finally:
        session.close()


async def _reconcile() -> int:
    auth = AuthConfig.from_env()
    engine = make_engine(_db_path())
    session = get_session(engine)
    try:
        results = []
        async with await polymarket.AsyncSecureClient.create(**auth.secure_client_kwargs()) as client:
            for intent in live_intents(session):
                result = await reconcile_intent(client, session, intent)
                results.append(
                    {
                        "intent_id": intent.id,
                        "order_id": intent.order_id,
                        "status": intent.status,
                        "remote_result": result.status if result else None,
                    }
                )
        _json({"reconciled": results})
        return 0
    finally:
        session.close()


async def _redeem(condition_id: str, confirmation: str) -> int:
    if confirmation != condition_id:
        raise RiskViolation("--confirm must exactly match --condition-id")
    auth = AuthConfig.from_env(require_relayer=True)
    async with await polymarket.AsyncSecureClient.create(**auth.secure_client_kwargs()) as client:
        handle = await client.redeem_positions(condition_id=condition_id)
        outcome = await handle.wait()
        _json(
            {
                "condition_id": condition_id,
                "transaction_id": outcome.transaction_id,
                "transaction_hash": outcome.transaction_hash,
            }
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m polytracker.trading")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="run authenticated read-only readiness checks")
    subparsers.add_parser("setup", help="create missing trading approvals")

    paper = subparsers.add_parser("paper-test", help="validate one complete market in paper mode")
    paper.add_argument("--windows", type=int, choices=[1], default=1)
    paper.add_argument("--outcome", choices=["up", "down"], required=True)

    live = subparsers.add_parser("live-test", help="place one protected live FAK buy")
    live.add_argument("--outcome", choices=["up", "down"], required=True)
    live.add_argument("--max-spend", type=Decimal, default=Decimal("5"))
    live.add_argument("--confirm", required=True)

    subparsers.add_parser("reconcile", help="reconcile all locally tracked live orders")
    redeem = subparsers.add_parser("redeem", help="redeem one resolved condition")
    redeem.add_argument("--condition-id", required=True)
    redeem.add_argument("--confirm", required=True)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        return await _doctor()
    if args.command == "setup":
        return await _setup()
    if args.command == "paper-test":
        return await _paper_test(args.outcome)
    if args.command == "live-test":
        if args.max_spend != Decimal("5"):
            raise RiskViolation("the first live test requires --max-spend 5")
        return await _live_test(args.outcome, args.confirm)
    if args.command == "reconcile":
        return await _reconcile()
    if args.command == "redeem":
        return await _redeem(args.condition_id, args.confirm)
    raise AssertionError(f"unknown command {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (
        ConfigurationError,
        DuplicateIntentError,
        OSError,
        RiskViolation,
        RuntimeError,
        polymarket.PolymarketError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
