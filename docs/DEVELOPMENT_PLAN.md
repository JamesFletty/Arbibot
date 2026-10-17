# Arbibot Development Plan for Codex

## Implementation status

Checked against the tree on 2026-10-17. Phases 0–15 are implemented and covered by unit tests. Phase 16 is implemented as a guarded mock pilot: it cannot submit a venue order, and live trading stays disabled unless an explicit unlock config is constructed in-process. Phase 17 is not started.

| Phase | Task | Status |
| --- | --- | --- |
| 0 | Scaffold | done |
| 1 | Event store | done |
| 2 | Ingestion interfaces | done |
| 3 | Binance ingestor | done |
| 4 | Candle builder | done |
| 5 | Polymarket book builder | done |
| 6 | Feature engine | done |
| 7 | Fair-price model | done |
| 8 | Edge and trade gates | done |
| 9 | Paper execution | done |
| 10 | Replay engine | done |
| 11 | Risk engine | done |
| 12 | Observability | done |
| 13 | Live interface disabled | done |
| 14 | Graph fusion | done |
| 15 | External adapters | done |
| 16 | Live pilot | done as a guarded mock; no venue order placement |
| 17 | Passive orders | not started |

## Development Principle

Build the research and measurement system first. Live trading comes later, if the replay and paper results justify it.

Codex should work one phase at a time. Do not ask it to build the whole bot in one run.

## Phase 0 — Scaffold

Status: done.

Goal: create the project skeleton, schemas, config, time utilities, and tests.

Build:

- Python package under `src/arbibot`
- typed event schemas
- YAML config loader
- time utilities
- custom error types
- smoke tests

Acceptance:

- tests pass
- events serialize/deserialize
- malformed timestamps fail loudly
- no external API clients yet

## Phase 1 — Event Store

Status: done.

Goal: append-only local persistence.

Build:

- `storage/event_store.py` interface
- `storage/sqlite_store.py` implementation
- SQLite WAL mode
- deterministic replay ordering
- idempotency by event ID

Acceptance:

- append and replay work
- duplicate event behavior is explicit
- corrupted records do not crash the full replay stream

## Phase 2 — Ingestion Interfaces

Status: done.

Goal: define vendor-neutral contracts before touching live APIs.

Build:

- `SpotMarketDataClient`
- `PredictionMarketDataClient`
- `ExternalSignalClient`
- mock deterministic clients

Acceptance:

- consumers depend only on internal event models
- no vendor payload leaks outside adapters

## Phase 3 — Binance Ingestor

Status: done.

Goal: normalize BTCUSDT spot stream data.

Build:

- Binance WebSocket adapter
- fixture-driven parsing first
- reconnect handling
- stale-stream health events
- timestamp capture immediately on receipt

Acceptance:

- fixture payloads normalize correctly
- reconnect state is explicit
- handler does not do heavy blocking work

## Phase 4 — Candle Builder

Status: done.

Goal: deterministic 5-minute bars from ticks.

Build:

- source timestamp bucket logic
- OHLCV
- trade count
- finalized bar emission
- out-of-order tolerance

Acceptance:

- exact boundary timestamps are correct
- late ticks handled deterministically

## Phase 5 — Polymarket Book Builder

Status: done.

Goal: reconstruct local UP/DOWN order books.

Build:

- book snapshot handling
- price-level delta handling
- level deletion on size zero
- tick-size change handling
- weighted executable price calculation
- spread/depth/imbalance metrics

Acceptance:

- snapshots and deltas reconstruct correctly
- empty/crossed books are flagged
- weighted execution price is tested

## Phase 6 — Feature Engine

Status: done.

Goal: compute features for opportunity detection.

Build spot features:

- short-window returns
- realized volatility
- momentum slope
- acceleration
- distance to threshold

Build book features:

- best bid/ask
- spread
- depth
- imbalance
- stale age
- weighted executable price

Acceptance:

- deterministic fixtures produce deterministic features
- stale flags are explicit

## Phase 7 — Fair-Price Model

Status: done.

Goal: estimate UP/DOWN probability.

Build:

- simple lognormal proxy model
- inputs: spot, threshold, time to expiry, volatility, shrunk momentum
- probability clamps
- invalid input handling

Acceptance:

- monotonic behavior around threshold
- no NaNs or divide-by-zero
- near-expiry behavior tested

## Phase 8 — Edge and Trade Gates

Status: done.

Goal: produce auditable trade/no-trade decisions.

Build:

- gross edge calculation
- fee estimate
- slippage estimate
- spread cost
- latency risk
- model-error buffer
- net edge
- hard gates

Hard gates:

- stale Binance data
- stale Polymarket data
- low liquidity
- wide spread
- conflicting signal
- market too close to expiry
- daily loss cap
- unknown order state
- edge below threshold

Acceptance:

- every no-trade decision has a reason
- no edge calculation assumes top-of-book fill for larger size

## Phase 9 — Paper Execution

Status: done.

Goal: simulate orders pessimistically.

Build:

- paper FAK/FOK
- passive order simulation
- visible-depth fills
- future trade-through requirement for passive fills
- order events

Acceptance:

- full fill, partial fill, no fill, stale book, insufficient depth tested
- paper fills are pessimistic by default

## Phase 10 — Replay Engine

Status: done.

Goal: replay persisted events deterministically.

Build:

- event stream replay
- state reconstruction
- latency injection
- simulated decision/execution path
- replay summary report

Summary metrics:

- trades
- fills
- rejects
- gross PnL
- net PnL
- max drawdown
- average edge
- realized edge
- skipped-by-reason counts

Acceptance:

- same input log creates same output
- latency injection changes outcomes where expected

## Phase 11 — Risk Engine

Status: done.

Goal: independent veto layer.

Build:

- 0.5% risk per trade
- 2% daily loss cap
- -0.4% hard stop
- max exposure
- max open orders
- max daily traded notional
- kill switch

Acceptance:

- strong edge cannot bypass risk
- unknown order state blocks trading
- risk state is serializable

## Phase 12 — Observability

Status: done.

Goal: make failures diagnosable.

Build:

- structured JSON logs
- metrics registry
- health state generator
- local CLI status command

Acceptance:

- failed/no-trade states can be diagnosed from logs and metrics

## Phase 13 — Live Interface Disabled

Status: done.

Goal: create live execution boundary without enabling trading.

Build:

- `submit_order`
- `cancel_order`
- `cancel_all`
- `get_open_orders`
- `get_order_status`
- mock implementation
- `LIVE_TRADING_ENABLED=false` guard

Acceptance:

- live submit raises unless explicitly enabled
- no accidental live path exists

## Phase 14 — Graph Fusion

Status: done.

Goal: add deterministic graph signal scoring.

Build:

- config-driven nodes and edges
- bull score
- bear score
- convergence
- conflict
- confidence
- stale node decay

Acceptance:

- bull convergence tested
- bear convergence tested
- conflict suppression tested
- stale decay tested

## Phase 15 — External Adapters

Status: done.

Goal: add secondary context inputs.

Build:

- TradingView webhook adapter
- CryptoQuant polling interface
- TTL handling
- slow-regime flag for CryptoQuant

Acceptance:

- external signals cannot trigger trades alone
- stale/malformed payloads are tested

## Phase 16 — Live Pilot

Status: done as a guarded mock pilot. `LivePilotRunner` and `MockLiveExecutionClient` enforce unlock, size, health, and risk checks. They do not place venue orders. Live trading remains disabled by default.

Goal: tiny controlled live mode.

Build only after Phases 0-15 pass.

Requirements:

- `LIVE_TRADING_ENABLED=true`
- `PILOT_MODE=true`
- max order size configured
- startup confirmation
- one active market
- one active order
- FAK/FOK only
- disable on unknown order state
- disable on disconnect

Acceptance:

- pilot cannot exceed max order size
- degraded states disable trading

## Phase 17 — Passive Orders

Status: not started.

Goal: add short-lived maker logic only if justified.

Build:

- max live time
- cancel on signal decay
- cancel on stale book
- cancel near expiry
- one replacement max
- pessimistic queue estimate

Acceptance:

- no quote chasing
- no order spam
- adverse-selection fill test exists

## First Codex Prompt

```text
Implement Phase 0 only for Arbibot.

Create core event schemas, config loader, time utilities, error types, package scaffold, and tests.

Do not implement Binance, Polymarket, TradingView, CryptoQuant, execution, or live trading.

Every event must include event_id, source, source_ts_ms, recv_wall_ts_ms, recv_monotonic_ns, and optional sequence_id.

Run tests, lint, and type checks before finishing.
```

## Rule

If Codex tries to build live trading before replay and paper execution exist, stop it.
