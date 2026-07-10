---
name: polymarket-client
description: Reference notes and gotchas for the polymarket-client Python package (import name `polymarket`), verified against the live API rather than the prose docs. Load this before writing or debugging any code that imports polymarket, touches AsyncPublicClient/AsyncSecureClient, subscribes to realtime streams, or works with the btc-updown-15m market series or the SQLModel store in this repo.
metadata:
  category: reference
---

# polymarket-client API notes

The prose docs at docs.polymarket.com/dev-tooling/python are directionally right but wrong on several concrete details.
These notes were verified against the real installed package (`polymarket-client==0.1.0b16` on PyPI, requires Python >=3.11) by calling the live API, not by reading documentation.
Trust this file over the prose docs when they disagree.
If a new package version changes any of this, update this file rather than rediscovering it.

## Package basics

- Import name is `polymarket`, not `polymarket_client`.
  Install with `uv add polymarket-client`.
- `AsyncPublicClient` is a plain constructor and async context manager.
  Use `async with polymarket.AsyncPublicClient() as client:`.
- `AsyncSecureClient` must be created with the async factory so it can derive or validate credentials and resolve the selected wallet.
  Use `async with await polymarket.AsyncSecureClient.create(private_key=..., wallet=...) as client:`.
- `AsyncSecureClient.create()` derives CLOB API credentials when `credentials=` is omitted, classifies the wallet from the signer and supplied address, and validates that deterministic relationship.
  Passing the exported Google/Magic signer and its Polymarket profile address classifies as `POLY_PROXY`.
- `AsyncSecureClient.create()` accepts an optional `RelayerApiKey(key=..., address=...)` as `api_key=` for gasless wallet operations.
  Normal order placement does not require that relayer key when the wallet already has current approvals.
- High-level live methods include `place_limit_order()`, `place_market_order()`, `list_open_orders()`, `list_account_trades()`, `get_balance_allowance()`, `setup_trading_approvals()`, and `redeem_positions()`.
  `place_market_order(..., order_type="FAK")` returns an accepted or rejected discriminated response and must not be retried blindly after an ambiguous transport failure.
- `get_balance_allowance(asset_type="COLLATERAL")` reports pUSD balance and allowances in base units with six decimal places.
- The installed SDK exports only `PRODUCTION`; it does not provide a paper or testnet environment.
- Paginated methods (`search()`, `list_markets()`, `list_events()`, etc.) return an `AsyncPaginator` synchronously, not a coroutine.
  Get the first page with `await paginator.first_page()`, which returns `Page(items=...)`.
  Iterate all pages with `async for page in paginator`.
- `client.subscribe(spec)` is itself a coroutine.
  Await it to get a `SubscriptionHandle`, then `async for event in handle`.
  Passing the un-awaited coroutine straight to `async for` fails with a confusing `TypeError`.
- A single `subscribe()` call accepts a list of mixed spec types (e.g. `[MarketSpec(...), CryptoPricesSpec(...)]`) and multiplexes them onto one handle.
  No need for separate subscriptions per stream type.
- Confirmed live: `AsyncPublicClient` realtime subscriptions (`MarketSpec`, `CryptoPricesSpec`) work fully unauthenticated.
  `UserSpec` is the only subscription type that needs `AsyncSecureClient`.

## The btc-updown-15m market series

- BTC 15-minute Up/Down market slugs follow the pattern `btc-updown-15m-{epoch_seconds}`, where `epoch_seconds` is the UTC unix timestamp of the window start, aligned to 900-second boundaries (`ts - ts % 900`).
  Fetch the market with `client.get_event(slug=slug)` and take `.markets[0]`.
  There is no need to search or paginate to find the current market once you know this; see `polytracker.discovery`.
- A not-found slug raises `polymarket.errors.RequestRejectedError`, not `UnexpectedResponseError`.
- Outcome token ids are `market.outcomes.yes.token_id` (label "Up") and `market.outcomes.no.token_id` (label "Down") - a fixed two-field object, not a list to iterate.
- Resolved markets settle the winning outcome's `price` to `1` and the losing one to `0`.
  Use `market.outcomes.yes.price == 1` as the backtest ground-truth label instead of re-deriving the outcome from a price feed yourself.
- The resolution source for this series is Chainlink's BTC/USD Data Stream (`data.chain.link/streams/btc-usd`), confirmed in each market's `resolution.source` field and description text.
- `CryptoPricesSpec` supports `topic="prices.crypto.chainlink"` in addition to `"prices.crypto.binance"`, and it streams the literal BTC/USD Chainlink value these markets resolve against - for free, unauthenticated, via Polymarket's own feed.
  Symbols are lowercase with a slash, e.g. `"btc/usd"` (not `"BTC"` or `"BTCUSDT"`).
  This means paid Chainlink Data Streams credentials are never needed for this project.
- `MarketBookEvent` order book levels: `bids` are sorted ascending by price (best bid is the *last* element), `asks` are sorted descending by price (best ask is also the *last* element).
- `MarketBestBidAskEvent` only arrives when the `MarketSpec` subscription is created with `custom_feature_enabled=True`; otherwise you only get `MarketBookEvent` and `MarketPriceChangeEvent`.
- `get_price_history(token_id=..., start_ts=..., end_ts=...)` returns a tuple of `PriceHistoryPoint(t=<epoch int>, p=<Decimal>)` directly (already awaited, not paginated), roughly one point per minute for a 15-minute window.

## SQLModel / SQLAlchemy gotchas (src/polytracker/store.py)

- SQLModel table fields typed as `Literal["up", "down"]` crash at class-definition time (`TypeError: issubclass() arg 1 must be a class` inside `get_sqlalchemy_type`).
  Use plain `str` columns and keep the `Literal` only as a non-table type hint.
- SQLAlchemy's default `expire_on_commit=True` expires ORM object attributes after every `commit()`, so `repr(obj)` right after a commit prints as empty/blank until you touch an attribute (which silently triggers a re-query).
  We build sessions with `expire_on_commit=False` in `store.get_session()` to avoid this, since the ingestion pipeline commits per tick and the live tracker/backtest code hold onto rows after commit.
