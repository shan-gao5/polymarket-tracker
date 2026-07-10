"""SQLite-backed datastore for market data and trade execution records."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlmodel import Field, Session, SQLModel, create_engine

DEFAULT_DB_PATH = Path("data/polytracker.db")

# NOTE: table columns below use plain `str`, not this Literal, because SQLModel's
# get_sqlalchemy_type() crashes on `Literal[...]` field annotations (TypeError:
# issubclass() arg 1 must be a class). Use Outcome only for non-table type hints.
Outcome = Literal["up", "down"]


class MarketTick(SQLModel, table=True):
    """A best-bid/ask or last-trade update for one outcome token."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    market_slug: str = Field(index=True)
    token_id: str = Field(index=True)
    side: str
    best_bid: float | None = None
    best_ask: float | None = None
    midpoint: float | None = None
    last_trade_price: float | None = None


class ChainlinkTick(SQLModel, table=True):
    """A BTC/USD price update from Polymarket's public Chainlink feed."""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    symbol: str = Field(index=True, default="btc/usd")
    price: float


class ResolvedMarket(SQLModel, table=True):
    """A closed market's final ground-truth outcome, for backtest labeling."""

    slug: str = Field(primary_key=True)
    condition_id: str
    up_token_id: str
    down_token_id: str
    window_start: datetime = Field(index=True)
    window_end: datetime
    outcome: str


class PaperTradingRun(SQLModel, table=True):
    """A full-market paper validation run required before live testing."""

    id: str = Field(primary_key=True)
    market_slug: str = Field(index=True)
    started_at: datetime = Field(index=True)
    window_start: datetime
    window_end: datetime
    completed_at: datetime | None = None
    status: str = Field(index=True)
    intent_id: str | None = None
    error_message: str | None = None


class TradeIntent(SQLModel, table=True):
    """One idempotent paper or live order request and its latest state."""

    id: str = Field(primary_key=True)
    created_at: datetime = Field(index=True)
    updated_at: datetime
    mode: str = Field(index=True)
    market_slug: str = Field(index=True)
    condition_id: str
    token_id: str = Field(index=True)
    outcome: str
    side: str
    requested_amount: str
    max_spend: str
    max_price: str
    status: str = Field(index=True)
    order_id: str | None = Field(default=None, index=True)
    remote_status: str | None = None
    making_amount: str | None = None
    taking_amount: str | None = None
    average_fill_price: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    reconciled_at: datetime | None = None


class TradeFill(SQLModel, table=True):
    """A fill associated with a locally persisted trade intent."""

    id: int | None = Field(default=None, primary_key=True)
    intent_id: str = Field(index=True)
    trade_id: str = Field(index=True)
    price: str
    size: str
    status: str
    transaction_hash: str | None = None
    matched_at: datetime | None = Field(default=None, index=True)


def make_engine(db_path: Path | str = DEFAULT_DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


def get_session(engine) -> Session:
    # expire_on_commit=False: callers keep using returned rows right after
    # commit() (e.g. printing them, reading fields for a live dashboard).
    # With the default True, SQLAlchemy expires all attributes on commit and
    # silently re-queries on next access, which is surprising and slow for
    # our commit-per-tick ingestion pattern.
    return Session(engine, expire_on_commit=False)
