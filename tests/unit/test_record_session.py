from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest

from arbibot.apps.commands.record_session import (
    SessionStopReason,
    record_session_events,
)
from arbibot.core.events import BaseEvent, BookLevel, PolyBookSnapshot, SpotTick
from arbibot.storage.event_store import EventStore, StoredEvent


class _MemoryStore(EventStore):
    def __init__(self) -> None:
        self.events: list[BaseEvent] = []

    def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    def append_many(self, events: Sequence[BaseEvent]) -> None:
        self.events.extend(events)

    def get_event(self, event_id: str) -> StoredEvent | None:
        del event_id
        return None

    def iter_events(
        self,
        start_ts_ms: int | None = None,
        end_ts_ms: int | None = None,
        event_types: Sequence[str] | None = None,
    ) -> Iterator[StoredEvent]:
        del start_ts_ms, end_ts_ms, event_types
        return iter(())


class _FakeClient:
    def __init__(self, source: str, events: list[BaseEvent], error: Exception | None = None) -> None:
        self.source = source
        self._events = events
        self._error = error
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def events(self) -> AsyncIterator[BaseEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[BaseEvent]:
        for event in self._events:
            yield event
        if self._error is not None:
            raise self._error
        while True:
            await asyncio.sleep(3600)


def _spot(index: int) -> SpotTick:
    ts = 1_000 + index
    return SpotTick(
        event_id=f"spot-{index}",
        source="binance",
        source_ts_ms=ts,
        recv_wall_ts_ms=ts + 2,
        recv_monotonic_ns=ts * 1_000_000,
        symbol="BTCUSDT",
        price=100.0 + index,
        size=1.0,
        stream_event_type="aggTrade",
    )


def _book(index: int) -> PolyBookSnapshot:
    ts = 2_000 + index
    return PolyBookSnapshot(
        event_id=f"book-{index}",
        source="polymarket",
        source_ts_ms=ts,
        recv_wall_ts_ms=ts + 3,
        recv_monotonic_ns=ts * 1_000_000,
        market_id="market-1",
        outcome="UP",
        token_id="token-up",
        bids=[BookLevel(price=0.49, size=20.0)],
        asks=[BookLevel(price=0.51, size=20.0)],
    )


@pytest.mark.asyncio
async def test_record_session_enforces_total_max_events() -> None:
    store = _MemoryStore()
    binance = _FakeClient("binance", [_spot(1), _spot(2), _spot(3)])
    polymarket = _FakeClient("polymarket", [_book(1), _book(2), _book(3)])

    summary = await record_session_events(
        binance_client=binance,
        polymarket_client=polymarket,
        store=store,
        store_path="memory",
        symbol="BTCUSDT",
        binance_streams=["aggTrade"],
        token_ids=["token-up"],
        max_events=4,
    )

    assert summary.stopped_reason is SessionStopReason.MAX_EVENTS_REACHED
    assert summary.events_recorded == 4
    assert summary.binance_events_recorded + summary.polymarket_events_recorded == 4
    assert len(store.events) == 4
    assert binance.started and binance.stopped
    assert polymarket.started and polymarket.stopped


@pytest.mark.asyncio
async def test_record_session_reports_source_failure() -> None:
    store = _MemoryStore()
    binance = _FakeClient("binance", [_spot(1)], error=RuntimeError("source failed"))
    polymarket = _FakeClient("polymarket", [_book(1)])

    summary = await record_session_events(
        binance_client=binance,
        polymarket_client=polymarket,
        store=store,
        store_path="memory",
        symbol="BTCUSDT",
        binance_streams=["aggTrade"],
        token_ids=["token-up"],
        max_events=10,
    )

    assert summary.stopped_reason is SessionStopReason.ERROR
    assert summary.stopped_source == "binance"
    assert summary.error_type == "RuntimeError"
    assert summary.error_message == "source failed"
    assert summary.failed_stage == "binance.events"


@pytest.mark.asyncio
async def test_record_session_zero_max_events_writes_nothing() -> None:
    store = _MemoryStore()
    summary = await record_session_events(
        binance_client=_FakeClient("binance", [_spot(1)]),
        polymarket_client=_FakeClient("polymarket", [_book(1)]),
        store=store,
        store_path="memory",
        symbol="BTCUSDT",
        binance_streams=["aggTrade"],
        token_ids=["token-up"],
        max_events=0,
    )

    assert summary.stopped_reason is SessionStopReason.MAX_EVENTS_REACHED
    assert summary.events_recorded == 0
    assert store.events == []
