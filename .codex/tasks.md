# Codex Task Queue

Work through these tasks in order. Do not skip ahead.

Status on 2026-10-17: tasks 000–016 are implemented. Task 016 is a guarded mock pilot and does not place venue orders. Task 017 is not started. See `docs/DEVELOPMENT_PLAN.md` for the phase acceptance notes.

## Task 000 — Scaffold

Status: done.

Build core schemas, config loader, time utilities, error types, and tests.

## Task 001 — Event Store

Status: done.

Implement append-only SQLite WAL event storage with deterministic replay ordering.

## Task 002 — Ingestion Interfaces

Status: done.

Define vendor-neutral market-data client interfaces and deterministic mock clients.

## Task 003 — Binance Ingestor

Status: done.

Implement Binance WebSocket normalization from fixture payloads first, then live stream support.

## Task 004 — Candle Builder

Status: done.

Build deterministic 5-minute bars from source timestamps.

## Task 005 — Polymarket Book Builder

Status: done.

Implement local book reconstruction from snapshots and deltas.

## Task 006 — Feature Engine

Status: done.

Compute spot, momentum, volatility, liquidity, and stale-data features.

## Task 007 — Fair-Price Model

Status: done.

Implement simple lognormal proxy model for UP/DOWN probability.

## Task 008 — Edge and Gates

Status: done.

Compute net edge and emit auditable trade/no-trade decisions.

## Task 009 — Paper Execution

Status: done.

Simulate pessimistic FAK/FOK and passive fills.

## Task 010 — Replay Engine

Status: done.

Replay persisted events deterministically with latency injection.

## Task 011 — Risk Engine

Status: done.

Implement position sizing, exposure caps, daily stop, hard stop, and kill switch.

## Task 012 — Observability

Status: done.

Add structured logs, metrics, health state, and local status output.

## Task 013 — Live Interface Disabled

Status: done.

Create live execution interface with hard disabled-by-default guard.

## Task 014 — Graph Fusion

Status: done.

Implement deterministic graph signal fusion.

## Task 015 — External Adapters

Status: done.

Add TradingView webhook and CryptoQuant slow-regime adapters.

## Task 016 — Live Pilot

Status: done as a guarded mock. Venue order placement is not implemented.

Implement tiny live pilot mode with explicit unlock requirements.

## Task 017 — Passive Orders

Status: not started.

Add short-lived passive maker logic only after taker path validation.
