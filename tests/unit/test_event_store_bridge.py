from pathlib import Path

import pytest

from arbibot.core.events import BookLevel, PolyBookDelta, PolyBookSnapshot, SpotTick
from arbibot.opportunity.edge import OutcomeSide
from arbibot.research.event_store_bridge import BridgeConfig, extract_repricing_cases
from arbibot.storage.sqlite_store import SQLiteEventStore


def _spot(event_id: str, ts: int, price: float) -> SpotTick:
    return SpotTick(
        event_id=event_id,
        source="binance",
        source_ts_ms=ts,
        recv_wall_ts_ms=ts + 5,
        recv_monotonic_ns=ts * 1_000_000,
        symbol="BTCUSDT",
        price=price,
        size=1.0,
        stream_event_type="trade",
    )


def _snapshot(event_id: str, ts: int, bid: float, ask: float) -> PolyBookSnapshot:
    return PolyBookSnapshot(
        event_id=event_id,
        source="polymarket",
        source_ts_ms=ts,
        recv_wall_ts_ms=ts + 7,
        recv_monotonic_ns=ts * 1_000_000 + 1,
        market_id="m1",
        outcome="UP",
        token_id="token-up",
        bids=[BookLevel(price=bid, size=100.0)],
        asks=[BookLevel(price=ask, size=100.0)],
    )


def _snapshot_with_clock_skew(
    event_id: str,
    source_ts_ms: int,
    recv_monotonic_ns: int,
    bid: float,
    ask: float,
) -> PolyBookSnapshot:
    return PolyBookSnapshot(
        event_id=event_id,
        source="polymarket",
        source_ts_ms=source_ts_ms,
        recv_wall_ts_ms=source_ts_ms + 7,
        recv_monotonic_ns=recv_monotonic_ns,
        market_id="m1",
        outcome="UP",
        token_id="token-up",
        bids=[BookLevel(price=bid, size=100.0)],
        asks=[BookLevel(price=ask, size=100.0)],
    )


def _delta(event_id: str, ts: int, price: float, size: float) -> PolyBookDelta:
    return PolyBookDelta(
        event_id=event_id,
        source="polymarket",
        source_ts_ms=ts,
        recv_wall_ts_ms=ts + 7,
        recv_monotonic_ns=ts * 1_000_000 + 1,
        market_id="m1",
        outcome="UP",
        token_id="token-up",
        book_side="ASK",
        price=price,
        size=size,
    )


def _seed_bridge_events(store: SQLiteEventStore) -> None:
    store.append_many(
        [
            _spot("s0", 1_000, 100.0),
            _spot("s1", 1_500, 100.0),
            _snapshot("p0", 1_500, 0.49, 0.50),
            _spot("s2", 2_000, 100.2),
            _snapshot("p1", 2_050, 0.491, 0.501),
            _snapshot("p2", 2_100, 0.492, 0.502),
            _snapshot("p3", 2_250, 0.493, 0.503),
            _snapshot("p4", 2_500, 0.494, 0.504),
        ]
    )


def test_extract_fair_edge_case_from_persisted_events(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite3"
    store = SQLiteEventStore(db)
    try:
        _seed_bridge_events(store)
        result = extract_repricing_cases(
            store,
            BridgeConfig(
                token_id="token-up",
                min_source_move_bps_100ms=5.0,
                market_expiry_ts_ms=10_000,
                threshold_price=100.0,
                outcome_side=OutcomeSide.UP,
                fee_cost_bps=2.0,
            ),
        )
    finally:
        store.close()

    assert result.summary.spot_ticks_seen == 3
    assert result.summary.cases_emitted == 1
    case = result.cases[0]
    assert case.state.source_move_bps_100ms > 0
    assert case.state.destination_book_age_ms == 500
    assert case.state.time_to_expiry_ms == 8_000
    assert case.state.edge_basis == "fair_probability"
    assert case.state.expected_cost_bps == 2.0
    assert case.state.estimated_edge_bps > 0
    assert len(case.future_quotes) == 4
    assert 50 < case.future_quotes[0].offset_ms < 50.001


def test_bridge_lag_only_mode_does_not_claim_executable_edge(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite3"
    store = SQLiteEventStore(db)
    try:
        _seed_bridge_events(store)
        result = extract_repricing_cases(
            store,
            BridgeConfig(token_id="token-up", min_source_move_bps_100ms=5.0),
        )
    finally:
        store.close()

    assert result.summary.cases_emitted == 1
    assert result.cases[0].state.edge_basis == "lag_proxy"
    assert result.cases[0].state.has_executable_edge_basis is False


def test_bridge_requires_complete_fair_edge_inputs() -> None:
    with pytest.raises(ValueError, match="fair edge requires"):
        BridgeConfig(market_expiry_ts_ms=10_000)


def test_bridge_uses_monotonic_time_despite_exchange_clock_skew(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite3"
    store = SQLiteEventStore(db)
    try:
        store.append_many(
            [
                _spot("s0", 1_000, 100.0),
                _spot("s1", 1_500, 100.0),
                _snapshot_with_clock_skew("p0", 50_000, 1_500_000_001, 0.49, 0.50),
                _spot("s2", 2_000, 100.2),
                _snapshot_with_clock_skew("p1", 70_000, 2_050_000_001, 0.491, 0.501),
                _snapshot_with_clock_skew("p2", 80_000, 2_100_000_001, 0.492, 0.502),
                _snapshot_with_clock_skew("p3", 90_000, 2_250_000_001, 0.493, 0.503),
                _snapshot_with_clock_skew("p4", 100_000, 2_500_000_001, 0.494, 0.504),
            ]
        )
        result = extract_repricing_cases(
            store,
            BridgeConfig(token_id="token-up", min_source_move_bps_100ms=5.0),
        )
    finally:
        store.close()

    assert result.summary.cases_emitted == 1
    case = result.cases[0]
    assert case.state.destination_book_age_ms == 500
    assert 50 < case.future_quotes[0].offset_ms < 50.001


def test_bridge_skips_delta_without_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite3"
    store = SQLiteEventStore(db)
    try:
        store.append_many(
            [
                _spot("s0", 1_000, 100.0),
                _spot("s1", 1_500, 100.0),
                _delta("d0", 1_500, 0.50, 100.0),
                _spot("s2", 2_000, 100.2),
            ]
        )
        result = extract_repricing_cases(
            store,
            BridgeConfig(token_id="token-up", min_source_move_bps_100ms=5.0),
        )
    finally:
        store.close()

    assert result.summary.cases_emitted == 0
    assert result.summary.book_events_seen == 1
