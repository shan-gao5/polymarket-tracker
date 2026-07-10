# Polymarket Tracker

A live tracker and data pipeline for Polymarket's recurring 15-minute Bitcoin Up/Down markets, built on the official `polymarket-client` Python SDK.
The backtest engine that was originally planned as part of this project is currently skipped; see Status below.

Each market resolves **Up** if the BTC price at the end of its 15-minute window is greater than or equal to the price at the start, else **Down**.
The resolution source is Chainlink's BTC/USD Data Stream, and Polymarket's own public realtime feed happens to stream that exact value for free (`CryptoPricesSpec(topic="prices.crypto.chainlink")`), so no paid Chainlink credentials are needed anywhere in this project.

Market discovery, ingestion, backfill, paper testing, and the dashboard require no API keys or account.
Live execution uses `AsyncSecureClient` with an exported signer key and the public Polymarket profile wallet address.

## Status

| Phase | What it does | Status |
|---|---|---|
| 1. Market discovery | Finds the current/past/next 15-min BTC market by computing its slug directly (`btc-updown-15m-{window_start_epoch}`), no search/pagination needed | Done |
| 2. Data pipeline | Realtime ingestion of order-book ticks + Chainlink BTC price into SQLite; backfill of resolved outcomes and price history for past windows | Done |
| 3. Live tracker | FastAPI + WebSocket dashboard: countdown, implied probability, order book, BTC price delta | Done |
| 4. Backtest engine | Event-driven replay of stored ticks against pluggable strategies, with PnL/Sharpe/win-rate/drawdown metrics | **Skipped for now**, by request |
| 5. Live execution foundation | Authenticated diagnostics, paper validation, protected live test order, persistence, reconciliation, and redemption via `AsyncSecureClient` | Done; no automated strategy yet |

Phase 4 was deliberately deferred rather than dropped.
The data pipeline (Phase 2) keeps accumulating real ticks and resolved outcomes into `data/polytracker.db` any time the tracker or ingestion worker runs, so backtest data collection is not blocked on the engine existing.
Pick Phase 4 back up whenever - the store schema (`MarketTick`, `ChainlinkTick`, `ResolvedMarket`) was designed for it already.

## Layout

```
src/polytracker/
  discovery.py         # find MarketWindow objects for any 15-min window
  store.py             # market data plus paper/live execution audit tables
  ingest.py            # realtime ingestion worker + in-memory LiveOrderBook
  backfill.py          # historical outcome + price history backfill
  tracker/
    app.py             # FastAPI + WebSocket live dashboard
    static/index.html  # dashboard frontend (vanilla HTML/CSS/JS, no build step)
  trading/             # risk checks, paper/live executors, reconciliation, and CLI
tests/                 # deterministic execution tests plus real public-API tests
```

There is no `backtest/` package yet - that is Phase 4, currently skipped.

## Running things

```bash
uv sync
uv run pytest tests/ -v
```

Tests hit the real Polymarket API (market discovery, a short live ingestion window, and backfill of real historical markets), so they need network access and take roughly 20 seconds.
The trading safety tests are deterministic and never place, cancel, or redeem real orders.

To run the live dashboard:

```bash
uv run uvicorn polytracker.tracker.app:app --reload
```

Then open `http://127.0.0.1:8000`.
Every run also writes real ticks to `data/polytracker.db` (gitignored), which doubles as backtest seed data for whenever Phase 4 happens.
Set `POLYTRACKER_DB_PATH` to point it at a different SQLite file.

## Trading foundation

Copy `.env.example` to `.env` and populate the exported Google/Magic signer and profile wallet address.
The implementation accepts either `WALLET_PRIVATE_KEY` and `ACCOUNT_ADDRESS`, or the canonical aliases `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_WALLET_ADDRESS`.
The `.env` file is gitignored, and secret values are never written to SQLite or log output.

Run authenticated read-only diagnostics first:

```bash
uv run python -m polytracker.trading doctor
```

The command verifies geographic eligibility, signer-to-wallet ownership, wallet type, account mode, pUSD balance, current exchange allowance, open orders, and current BTC market discovery.
It exits nonzero if live trading is not ready.
Do not attempt to bypass a geographic restriction.

If the account is missing current approvals, add the relayer key pair from Polymarket Settings and run:

```bash
uv run python -m polytracker.trading setup
```

Before live testing, complete one full market in paper mode:

```bash
uv run python -m polytracker.trading paper-test --windows 1 --outcome up
```

The paper run uses the real order book, simulates a protected FAK fill, observes the full market window and rollover, and persists the passed gate.

The live command is deliberately narrow and disabled by default.
It places at most one FAK buy per market, calculates the smallest valid order, and enforces a hard $5 maximum:

```bash
POLYTRACKER_LIVE_ENABLED=true uv run python -m polytracker.trading live-test \
  --outcome up \
  --max-spend 5 \
  --confirm btc-updown-15m-START_EPOCH
```

The confirmation must exactly match the current market slug.
The command refuses stale books, insufficient time, insufficient funds or allowances, duplicate intents, blocked regions, and any spend above the fixed limit.
Use `reconcile` after an interruption and `redeem --condition-id ID --confirm ID` after resolution.

## Notes for contributors

See `DESIGN.md` for the full technical design doc: architecture, module-by-module reference, data model, verified `polymarket-client` API behavior, bugs found by testing live, and extension points for Phase 4/5.
It is written to be loaded whole into an agent's context with no other history needed.

See `AGENTS.md` for general engineering guidelines.
See the `polymarket-client` skill at `.claude/skills/polymarket-client/SKILL.md` for the load-on-demand version of the API gotchas (also included inline in `DESIGN.md`).
