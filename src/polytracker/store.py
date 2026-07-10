"""SQLite-backed datastore for market ticks, BTC spot ticks, and resolved outcomes."""

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
