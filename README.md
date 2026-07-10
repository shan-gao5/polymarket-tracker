# Polymarket Tracker

A live tracker and data pipeline for Polymarket's recurring 15-minute Bitcoin Up/Down markets, built on the official `polymarket-client` Python SDK.
The backtest engine that was originally planned as part of this project is currently skipped; see Status below.

Each market resolves **Up** if the BTC price at the end of its 15-minute window is greater than or equal to the price at the start, else **Down**.
The resolution source is Chainlink's BTC/USD Data Stream, and Polymarket's own public realtime feed happens to stream that exact value for free (`CryptoPricesSpec(topic="prices.crypto.chainlink")`), so no paid Chainlink credentials are needed anywhere in this project.

No API keys or accounts are required for anything built so far.
The whole pipeline runs against Polymarket's unauthenticated `AsyncPublicClient`.

## Status

| Phase | What it does | Status |
|---|---|---|
| 1. Market discovery | Finds the current/past/next 15-min BTC market by computing its slug directly (`btc-updown-15m-{window_start_epoch}`), no search/pagination needed | Done |
| 2. Data pipeline | Realtime ingestion of order-book ticks + Chainlink BTC price into SQLite; backfill of resolved outcomes and price history for past windows | Done |
| 3. Live tracker | FastAPI + WebSocket dashboard: countdown, implied probability, order book, BTC price delta | Done |
| 4. Backtest engine | Event-driven replay of stored ticks against pluggable strategies, with PnL/Sharpe/win-rate/drawdown metrics | **Skipped for now**, by request |
| 5. Live execution (optional) | Placing real orders via `AsyncSecureClient`, gated behind a private key and funded wallet | Not started, and not a default next step |

Phase 4 was deliberately deferred rather than dropped.
The data pipeline (Phase 2) keeps accumulating real ticks and resolved outcomes into `data/polytracker.db` any time the tracker or ingestion worker runs, so backtest data collection is not blocked on the engine existing.
Pick Phase 4 back up whenever - the store schema (`MarketTick`, `ChainlinkTick`, `ResolvedMarket`) was designed for it already.

## Layout

```
src/polytracker/
  discovery.py         # find MarketWindow objects for any 15-min window
  store.py             # SQLModel tables: MarketTick, ChainlinkTick, ResolvedMarket
  ingest.py            # realtime ingestion worker + in-memory LiveOrderBook
  backfill.py          # historical outcome + price history backfill
  tracker/
    app.py             # FastAPI + WebSocket live dashboard
    static/index.html  # dashboard frontend (vanilla HTML/CSS/JS, no build step)
tests/                 # all tests run end-to-end against the real live API, no mocks
```

There is no `backtest/` package yet - that is Phase 4, currently skipped.

## Running things

```bash
uv sync
uv run pytest tests/ -v
```

Tests hit the real Polymarket API (market discovery, a short live ingestion window, and backfill of real historical markets), so they need network access and take roughly 20 seconds.

To run the live dashboard:

```bash
uv run uvicorn polytracker.tracker.app:app --reload
```

Then open `http://127.0.0.1:8000`.
Every run also writes real ticks to `data/polytracker.db` (gitignored), which doubles as backtest seed data for whenever Phase 4 happens.
Set `POLYTRACKER_DB_PATH` to point it at a different SQLite file.

## Notes for contributors

See `DESIGN.md` for the full technical design doc: architecture, module-by-module reference, data model, verified `polymarket-client` API behavior, bugs found by testing live, and extension points for Phase 4/5.
It is written to be loaded whole into an agent's context with no other history needed.

See `AGENTS.md` for general engineering guidelines.
See the `polymarket-client` skill at `.claude/skills/polymarket-client/SKILL.md` for the load-on-demand version of the API gotchas (also included inline in `DESIGN.md`).
