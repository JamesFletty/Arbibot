"""Offline end-to-end demo session.

Assumption: every event in this module is synthetic and constructed in-process.
The demo does not open Binance, Polymarket, or any other socket. Cross-venue
timing uses the same local monotonic clock as the repricing bridge.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from arbibot.core.config import load_config
from arbibot.core.events import BaseEvent, BookLevel, PolyBookSnapshot, SpotTick
from arbibot.opportunity.edge import OutcomeSide
from arbibot.ops.health import HealthMonitor
from arbibot.replay.engine import ReplayConfig, ReplayEngine
from arbibot.research.event_store_bridge import BridgeConfig, extract_repricing_cases
from arbibot.research.jev_repricing import DeterministicLagGate
from arbibot.research.repricing_benchmark import build_report, evaluate_cases
from arbibot.storage.sqlite_store import SQLiteEventStore

DEMO_SYMBOL = "BTCUSDT"
DEMO_TOKEN_ID = "token-up"
DEMO_MARKET_ID = "offline-demo"
DEMO_OUTCOME = "UP"
IMPULSE_SOURCE_TS_MS = 2_000
STALE_SOURCE_TS_MS = 4_000
BENCHMARK_EXPIRY_TS_MS = 62_000
BENCHMARK_THRESHOLD_PRICE = 100.0
BENCHMARK_MIN_MOVE_BPS = 5.0
PAPER_SECONDS_TO_EXPIRY = Decimal("300")
PAPER_TARGET_SIZE = Decimal("1")
DATA_ORIGIN = "synthetic"


class OfflineDemoError(Exception):
    """Raised when the offline demo cannot be prepared."""


@dataclass(frozen=True, slots=True)
class OfflineDemoReport:
    data_origin: str
    scenario: str
    store_path: str
    config_path: str
    config_valid: bool
    live_trading_enabled: bool
    health_status: str
    health_can_submit_orders: bool
    total_events: int
    deserialized_events: int
    malformed_events: int
    decisions_total: int
    decisions_trade: int
    decisions_no_trade: int
    no_trade_reasons: dict[str, int]
    stale_book_decisions: int
    stale_spot_decisions: int
    fresh_book_decisions: int
    paper_order_events: int
    paper_fills: int
    benchmark_mode: str
    cases_emitted: int
    quotes_emitted: int
    bridge_malformed_events: int
    observed_reprices: int
    p50_delay_ms: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "data_origin": self.data_origin,
            "scenario": self.scenario,
            "store_path": self.store_path,
            "config_path": self.config_path,
            "config_valid": self.config_valid,
            "live_trading_enabled": self.live_trading_enabled,
            "health_status": self.health_status,
            "health_can_submit_orders": self.health_can_submit_orders,
            "total_events": self.total_events,
            "deserialized_events": self.deserialized_events,
            "malformed_events": self.malformed_events,
            "decisions_total": self.decisions_total,
            "decisions_trade": self.decisions_trade,
            "decisions_no_trade": self.decisions_no_trade,
            "no_trade_reasons": dict(sorted(self.no_trade_reasons.items())),
            "stale_book_decisions": self.stale_book_decisions,
            "stale_spot_decisions": self.stale_spot_decisions,
            "fresh_book_decisions": self.fresh_book_decisions,
            "paper_order_events": self.paper_order_events,
            "paper_fills": self.paper_fills,
            "benchmark_mode": self.benchmark_mode,
            "cases_emitted": self.cases_emitted,
            "quotes_emitted": self.quotes_emitted,
            "bridge_malformed_events": self.bridge_malformed_events,
            "observed_reprices": self.observed_reprices,
            "p50_delay_ms": self.p50_delay_ms,
        }


def _spot(event_id: str, ts: int, price: float) -> SpotTick:
    return SpotTick(
        event_id=event_id,
        source="binance",
        source_ts_ms=ts,
        recv_wall_ts_ms=ts + 5,
        recv_monotonic_ns=ts * 1_000_000,
        symbol=DEMO_SYMBOL,
        price=price,
        size=1.0,
        stream_event_type="trade",
    )


def _book(event_id: str, ts: int, bid: float, ask: float) -> PolyBookSnapshot:
    return PolyBookSnapshot(
        event_id=event_id,
        source="polymarket",
        source_ts_ms=ts,
        recv_wall_ts_ms=ts + 7,
        recv_monotonic_ns=ts * 1_000_000 + 1,
        market_id=DEMO_MARKET_ID,
        outcome=DEMO_OUTCOME,
        token_id=DEMO_TOKEN_ID,
        bids=[BookLevel(price=bid, size=100.0)],
        asks=[BookLevel(price=ask, size=100.0)],
    )


def build_impulse_session() -> list[BaseEvent]:
    """Spot impulse, a lagged book that then reprices, and one stale follow-up tick."""

    return [
        _spot("demo-spot-1000", 1_000, 100.0),
        _spot("demo-spot-1500", 1_500, 100.0),
        _book("demo-book-1500", 1_500, 0.49, 0.51),
        _spot("demo-spot-1600", 1_600, 100.0),
        _book("demo-book-1950", 1_950, 0.49, 0.51),
        _spot("demo-spot-2000", IMPULSE_SOURCE_TS_MS, 100.2),
        _book("demo-book-2050", 2_050, 0.491, 0.511),
        _book("demo-book-2100", 2_100, 0.492, 0.512),
        _book("demo-book-2250", 2_250, 0.493, 0.513),
        _book("demo-book-2500", 2_500, 0.494, 0.514),
        _spot("demo-spot-4000", STALE_SOURCE_TS_MS, 100.2),
    ]


def build_flat_session() -> list[BaseEvent]:
    """Same clocks as the impulse session with no source move large enough to qualify."""

    return [
        _spot("flat-spot-1000", 1_000, 100.0),
        _spot("flat-spot-1500", 1_500, 100.0),
        _book("flat-book-1500", 1_500, 0.49, 0.51),
        _spot("flat-spot-1600", 1_600, 100.0),
        _book("flat-book-1950", 1_950, 0.49, 0.51),
        _spot("flat-spot-2000", IMPULSE_SOURCE_TS_MS, 100.0),
        _book("flat-book-2050", 2_050, 0.49, 0.51),
        _book("flat-book-2100", 2_100, 0.49, 0.51),
        _book("flat-book-2250", 2_250, 0.49, 0.51),
        _book("flat-book-2500", 2_500, 0.49, 0.51),
        _spot("flat-spot-4000", STALE_SOURCE_TS_MS, 100.0),
    ]


def _remove_store_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            candidate.unlink()


def write_events(
    store_path: str | Path,
    events: Sequence[BaseEvent],
    *,
    force: bool = False,
) -> None:
    path = Path(store_path)
    sidecars = [Path(f"{path}-wal"), Path(f"{path}-shm")]
    if path.exists() or any(candidate.exists() for candidate in sidecars):
        if not force:
            raise OfflineDemoError(f"store already exists: {path}")
        _remove_store_files(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteEventStore(path)
    try:
        if events:
            store.append_many(list(events))
    finally:
        store.close()


def insert_malformed_event(store_path: str | Path) -> None:
    """Append one SpotTick row whose payload cannot be deserialized."""

    path = Path(store_path)
    if not path.exists():
        raise OfflineDemoError(f"store does not exist: {path}")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO events (
                event_id,
                event_type,
                source,
                source_ts_ms,
                recv_wall_ts_ms,
                recv_monotonic_ns,
                inserted_at_wall_ts_ms,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "demo-malformed-spot",
                "SpotTick",
                "binance",
                1,
                1,
                1,
                1,
                "{",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def evaluate_store(
    store_path: str | Path,
    *,
    config_path: str | Path,
    scenario: str,
) -> OfflineDemoReport:
    path = Path(store_path)
    config_file = Path(config_path)
    if not path.exists():
        raise OfflineDemoError(f"store does not exist: {path}")

    config = load_config(config_file)
    health = HealthMonitor().evaluate(
        clock_offset_ms=0,
        binance_event_age_ms=0,
        polymarket_event_age_ms=0,
        order_api_latency_p95_ms=0,
        unknown_order_state=False,
        kill_switch_active=False,
        risk_engine_disabled=False,
        event_store_available=True,
        market_metadata_available=True,
    )

    replay_store = SQLiteEventStore(path)
    try:
        replay = ReplayEngine(
            replay_store,
            ReplayConfig(
                symbol=DEMO_SYMBOL,
                evaluate_opportunities=True,
                paper_execute=True,
                threshold_price=Decimal(str(BENCHMARK_THRESHOLD_PRICE)),
                seconds_to_expiry=PAPER_SECONDS_TO_EXPIRY,
                target_size=PAPER_TARGET_SIZE,
                outcome_side=OutcomeSide.UP,
            ),
        ).run()
    finally:
        replay_store.close()

    bridge_config = BridgeConfig(
        symbol=DEMO_SYMBOL,
        token_id=DEMO_TOKEN_ID,
        market_expiry_ts_ms=BENCHMARK_EXPIRY_TS_MS,
        threshold_price=BENCHMARK_THRESHOLD_PRICE,
        outcome_side=OutcomeSide.UP,
        min_source_move_bps_100ms=BENCHMARK_MIN_MOVE_BPS,
    )
    benchmark_store = SQLiteEventStore(path)
    try:
        bridge = extract_repricing_cases(benchmark_store, bridge_config)
        gate = DeterministicLagGate() if bridge_config.has_fair_edge_inputs else None
        evaluations = evaluate_cases(bridge.cases, deterministic_gate=gate)
        benchmark = build_report(evaluations, include_jev=False)
    finally:
        benchmark_store.close()

    reasons: Counter[str] = Counter()
    stale_book = 0
    stale_spot = 0
    fresh_book = 0
    for decision in replay.decisions:
        for reason in decision.reasons:
            reasons[reason] += 1
        if "STALE_BOOK" in decision.reasons:
            stale_book += 1
        elif "NO_ACTIVE_BOOK" not in decision.reasons:
            fresh_book += 1
        if "STALE_SPOT" in decision.reasons:
            stale_spot += 1

    mode = "fair_probability_edge" if bridge_config.has_fair_edge_inputs else "lag_measurement"
    return OfflineDemoReport(
        data_origin=DATA_ORIGIN,
        scenario=scenario,
        store_path=str(path),
        config_path=str(config_file),
        config_valid=True,
        live_trading_enabled=config.live_trading_enabled,
        health_status=str(health.status),
        health_can_submit_orders=health.can_submit_orders,
        total_events=replay.summary.total_events,
        deserialized_events=replay.summary.deserialized_events,
        malformed_events=replay.summary.malformed_events,
        decisions_total=replay.summary.decisions_total,
        decisions_trade=replay.summary.decisions_trade,
        decisions_no_trade=replay.summary.decisions_no_trade,
        no_trade_reasons=dict(reasons),
        stale_book_decisions=stale_book,
        stale_spot_decisions=stale_spot,
        fresh_book_decisions=fresh_book,
        paper_order_events=replay.summary.order_events_total,
        paper_fills=replay.summary.orders_filled,
        benchmark_mode=mode,
        cases_emitted=bridge.summary.cases_emitted,
        quotes_emitted=bridge.summary.quotes_emitted,
        bridge_malformed_events=bridge.summary.malformed_events,
        observed_reprices=benchmark.deterministic_reaction.observed_reprices,
        p50_delay_ms=benchmark.deterministic_reaction.p50_delay_ms,
    )


def run_offline_demo(
    store_path: str | Path,
    config_path: str | Path,
    *,
    force: bool = False,
) -> OfflineDemoReport:
    """Persist the synthetic impulse session and run paper replay plus the benchmark."""

    load_config(config_path)
    write_events(store_path, build_impulse_session(), force=force)
    return evaluate_store(store_path, config_path=config_path, scenario="impulse")
