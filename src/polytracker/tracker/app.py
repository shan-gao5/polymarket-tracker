"""Live dashboard: FastAPI + WebSocket view of the current BTC 15-min market.

Runs the same realtime ingestion pipeline as the backtest data pipeline
(polytracker.ingest), so every second the dashboard is open also feeds the
historical dataset used for backtesting.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import polymarket
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from polytracker.discovery import (
    MarketNotFoundError,
    MarketWindow,
    get_current_market,
    get_next_market,
)
from polytracker.ingest import LiveOrderBook, ingest_window
from polytracker.store import DEFAULT_DB_PATH, get_session, make_engine

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
BROADCAST_INTERVAL_SECONDS = 1.0


@dataclass
class TrackerState:
    live_book: LiveOrderBook = field(default_factory=LiveOrderBook)
    market: MarketWindow | None = None
    window_open_btc_price: float | None = None

    def set_market(self, market: MarketWindow) -> None:
        self.market = market
        self.window_open_btc_price = None

    def maybe_set_open_price(self, price: float, _ts: datetime) -> None:
        if self.window_open_btc_price is None:
            self.window_open_btc_price = price


class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active.discard(websocket)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


def _outcome_payload(state: TrackerState, token_id: str) -> dict:
    best = state.live_book.best.get(token_id)
    last = state.live_book.last_trade.get(token_id)
    best_bid = float(best.payload.best_bid) if best and best.payload.best_bid is not None else None
    best_ask = float(best.payload.best_ask) if best and best.payload.best_ask is not None else None
    midpoint = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    return {
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "midpoint": midpoint,
        "last_trade_price": float(last.payload.price) if last else None,
    }


def build_snapshot(state: TrackerState) -> dict:
    market = state.market
    if market is None:
        return {"status": "initializing"}

    now = datetime.now(timezone.utc)
    seconds_remaining = max(0.0, (market.window_end - now).total_seconds())
    btc_price = state.live_book.btc_price
    open_price = state.window_open_btc_price
    delta = btc_price - open_price if btc_price is not None and open_price is not None else None
    delta_pct = (delta / open_price * 100) if delta is not None and open_price else None

    return {
        "status": "closed" if seconds_remaining <= 0 else "live",
        "market_slug": market.slug,
        "window_start": market.window_start.isoformat(),
        "window_end": market.window_end.isoformat(),
        "seconds_remaining": seconds_remaining,
        "up": _outcome_payload(state, market.up_token_id),
        "down": _outcome_payload(state, market.down_token_id),
        "btc_price": btc_price,
        "btc_open_price": open_price,
        "btc_delta": delta,
        "btc_delta_pct": delta_pct,
    }


async def _get_current_with_retry(
    client: polymarket.AsyncPublicClient, *, retries: int = 5, delay: float = 2.0
) -> MarketWindow:
    for attempt in range(retries):
        try:
            return await get_current_market(client)
        except MarketNotFoundError:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _get_next_with_retry(
    client: polymarket.AsyncPublicClient,
    market: MarketWindow,
    *,
    retries: int = 10,
    delay: float = 2.0,
) -> MarketWindow:
    for attempt in range(retries):
        try:
            return await get_next_market(client, market)
        except MarketNotFoundError:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def pipeline_loop(client: polymarket.AsyncPublicClient, session, state: TrackerState) -> None:
    """Ingest the current window forever, updating ``state`` as windows roll over."""
    market = await _get_current_with_retry(client)
    state.set_market(market)
    while True:
        logger.info("tracker: ingesting window %s", market.slug)
        window_seconds = (market.window_end - market.window_start).total_seconds()
        await ingest_window(
            client,
            market,
            session,
            live_book=state.live_book,
            stop_after=window_seconds + 5,
            on_btc_tick=state.maybe_set_open_price,
        )
        market = await _get_next_with_retry(client, market)
        state.set_market(market)


async def broadcast_loop(state: TrackerState, manager: ConnectionManager) -> None:
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)
        await manager.broadcast(build_snapshot(state))


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = polymarket.AsyncPublicClient()
    await client.__aenter__()
    db_path = os.environ.get("POLYTRACKER_DB_PATH", DEFAULT_DB_PATH)
    engine = make_engine(db_path)
    session = get_session(engine)

    state = TrackerState()
    manager = ConnectionManager()
    app.state.tracker_state = state
    app.state.manager = manager

    pipeline_task = asyncio.create_task(pipeline_loop(client, session, state))
    broadcast_task = asyncio.create_task(broadcast_loop(state, manager))

    try:
        yield
    finally:
        pipeline_task.cancel()
        broadcast_task.cancel()
        for task in (pipeline_task, broadcast_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await client.close()
        session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state(request: Request) -> dict:
    return build_snapshot(request.app.state.tracker_state)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    manager: ConnectionManager = websocket.app.state.manager
    state: TrackerState = websocket.app.state.tracker_state
    await manager.connect(websocket)
    try:
        await websocket.send_json(build_snapshot(state))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
