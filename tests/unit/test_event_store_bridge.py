from pathlib import Path

from arbibot.core.events import BookLevel, PolyBookDelta, PolyBookSnapshot, SpotTick
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


def test_extract_repricing_cases_from_persisted_events(tmp_path: Path) -> None:
    db = tmp_path / "events.sqlite3"
    store = SQLiteEventStore(db)
    try:
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
        result = extract_repricing_cases(
            store,
            BridgeConfig(
                token_id="token-up",
                min_source_move_bps_100ms=5.0,
                market_expiry_ts_ms=10_000,
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
    assert len(case.future_quotes) == 4
    assert case.future_quotes[0].offset_ms == 50


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
