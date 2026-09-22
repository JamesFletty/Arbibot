from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from arbibot.core.events import BaseEvent
from arbibot.ingestion.binance_ws import BinanceSpotMarketDataClient
from arbibot.ingestion.interfaces import EventStreamClient
from arbibot.ingestion.polymarket_ws import PolymarketMarketDataClient
from arbibot.storage.event_store import EventStore
from arbibot.storage.sqlite_store import SQLiteEventStore


class SessionStopReason(StrEnum):
    MAX_EVENTS_REACHED = "MAX_EVENTS_REACHED"
    DURATION_REACHED = "DURATION_REACHED"
    INTERRUPTED = "INTERRUPTED"
    CLIENT_STOPPED = "CLIENT_STOPPED"
    ERROR = "ERROR"
    DRY_RUN = "DRY_RUN"


@dataclass(frozen=True, slots=True)
class SessionRecordSummary:
    store_path: str
    symbol: str
    binance_streams: list[str]
    token_ids: list[str]
    events_recorded: int
    binance_events_recorded: int
    polymarket_events_recorded: int
    started_at_ms: int
    ended_at_ms: int
    duration_ms: int
    stopped_reason: SessionStopReason
    stopped_source: str | None
    error_type: str | None
    error_message: str | None
    failed_stage: str | None


@dataclass(frozen=True, slots=True)
class _QueuedEvent:
    source: str
    event: BaseEvent


@dataclass(frozen=True, slots=True)
class _ProducerStopped:
    source: str
    error: BaseException | None = None


QueueItem = _QueuedEvent | _ProducerStopped


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _pump_source(
    source: str,
    client: EventStreamClient,
    queue: asyncio.Queue[QueueItem],
) -> None:
    try:
        async for event in client.events():
            await queue.put(_QueuedEvent(source=source, event=event))
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        await queue.put(_ProducerStopped(source=source, error=exc))
        return
    await queue.put(_ProducerStopped(source=source))


async def record_session_events(
    *,
    binance_client: EventStreamClient,
    polymarket_client: EventStreamClient,
    store: EventStore,
    store_path: str,
    symbol: str,
    binance_streams: list[str],
    token_ids: list[str],
    max_events: int | None = None,
    duration_seconds: int | None = None,
) -> SessionRecordSummary:
    if max_events is not None and max_events < 0:
        raise ValueError("max_events must be >= 0")
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")

    started_at_ms = _now_ms()
    deadline_ms = None if duration_seconds is None else started_at_ms + duration_seconds * 1000
    queue: asyncio.Queue[QueueItem] = asyncio.Queue()
    total = 0
    binance_count = 0
    polymarket_count = 0
    reason = SessionStopReason.CLIENT_STOPPED
    stopped_source: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    failed_stage: str | None = None

    clients = (
        ("binance", binance_client),
        ("polymarket", polymarket_client),
    )
    started: list[EventStreamClient] = []
    tasks: list[asyncio.Task[None]] = []

    try:
        for source, client in clients:
            try:
                await client.start()
            except BaseException as exc:
                reason = SessionStopReason.ERROR
                stopped_source = source
                error_type = type(exc).__name__
                error_message = str(exc)
                failed_stage = f"{source}.start"
                return _summary(
                    store_path=store_path,
                    symbol=symbol,
                    binance_streams=binance_streams,
                    token_ids=token_ids,
                    total=total,
                    binance_count=binance_count,
                    polymarket_count=polymarket_count,
                    started_at_ms=started_at_ms,
                    reason=reason,
                    stopped_source=stopped_source,
                    error_type=error_type,
                    error_message=error_message,
                    failed_stage=failed_stage,
                )
            started.append(client)

        tasks = [
            asyncio.create_task(_pump_source(source, client, queue), name=f"record-{source}")
            for source, client in clients
        ]

        if max_events == 0:
            reason = SessionStopReason.MAX_EVENTS_REACHED
        else:
            while True:
                if deadline_ms is not None and _now_ms() >= deadline_ms:
                    reason = SessionStopReason.DURATION_REACHED
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    continue

                if isinstance(item, _ProducerStopped):
                    stopped_source = item.source
                    if item.error is None:
                        reason = SessionStopReason.CLIENT_STOPPED
                    else:
                        reason = SessionStopReason.ERROR
                        error_type = type(item.error).__name__
                        error_message = str(item.error)
                        failed_stage = f"{item.source}.events"
                    break

                try:
                    store.append(item.event)
                except BaseException as exc:
                    reason = SessionStopReason.ERROR
                    stopped_source = item.source
                    error_type = type(exc).__name__
                    error_message = str(exc)
                    failed_stage = "store.append"
                    break

                total += 1
                if item.source == "binance":
                    binance_count += 1
                elif item.source == "polymarket":
                    polymarket_count += 1

                if max_events is not None and total >= max_events:
                    reason = SessionStopReason.MAX_EVENTS_REACHED
                    break
    except (asyncio.CancelledError, KeyboardInterrupt):
        reason = SessionStopReason.INTERRUPTED
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for client in reversed(started):
            try:
                await client.stop()
            except BaseException as exc:
                if reason is not SessionStopReason.ERROR:
                    reason = SessionStopReason.ERROR
                    error_type = type(exc).__name__
                    error_message = str(exc)
                    failed_stage = "client.stop"

    return _summary(
        store_path=store_path,
        symbol=symbol,
        binance_streams=binance_streams,
        token_ids=token_ids,
        total=total,
        binance_count=binance_count,
        polymarket_count=polymarket_count,
        started_at_ms=started_at_ms,
        reason=reason,
        stopped_source=stopped_source,
        error_type=error_type,
        error_message=error_message,
        failed_stage=failed_stage,
    )


def _summary(
    *,
    store_path: str,
    symbol: str,
    binance_streams: list[str],
    token_ids: list[str],
    total: int,
    binance_count: int,
    polymarket_count: int,
    started_at_ms: int,
    reason: SessionStopReason,
    stopped_source: str | None,
    error_type: str | None,
    error_message: str | None,
    failed_stage: str | None,
) -> SessionRecordSummary:
    ended_at_ms = _now_ms()
    return SessionRecordSummary(
        store_path=store_path,
        symbol=symbol,
        binance_streams=binance_streams,
        token_ids=token_ids,
        events_recorded=total,
        binance_events_recorded=binance_count,
        polymarket_events_recorded=polymarket_count,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        duration_ms=max(0, ended_at_ms - started_at_ms),
        stopped_reason=reason,
        stopped_source=stopped_source,
        error_type=error_type,
        error_message=error_message,
        failed_stage=failed_stage,
    )


def run_record_session(
    *,
    store_path: str,
    symbol: str,
    binance_streams: list[str],
    token_ids: list[str],
    outcome: str | None,
    duration_seconds: int | None,
    max_events: int | None,
    as_json: bool,
    dry_run: bool,
) -> int:
    normalized_symbol = symbol.upper()
    normalized_tokens = [token.strip() for token in token_ids if token.strip()]
    normalized_streams = [stream.strip() for stream in binance_streams if stream.strip()]
    if not normalized_tokens:
        print("record-session validation failed: at least one --token-id is required")
        return 1
    if not normalized_streams:
        print("record-session validation failed: at least one Binance stream is required")
        return 1
    supported_streams = {"aggTrade", "trade", "bookTicker"}
    unknown = [stream for stream in normalized_streams if stream not in supported_streams]
    if unknown:
        print(f"record-session validation failed: unsupported Binance streams {unknown}")
        return 1
    if outcome is not None and len(normalized_tokens) != 1:
        print("record-session validation failed: --outcome requires exactly one token")
        return 1

    if dry_run:
        now = _now_ms()
        summary = SessionRecordSummary(
            store_path=store_path,
            symbol=normalized_symbol,
            binance_streams=normalized_streams,
            token_ids=normalized_tokens,
            events_recorded=0,
            binance_events_recorded=0,
            polymarket_events_recorded=0,
            started_at_ms=now,
            ended_at_ms=now,
            duration_ms=0,
            stopped_reason=SessionStopReason.DRY_RUN,
            stopped_source=None,
            error_type=None,
            error_message=None,
            failed_stage=None,
        )
        _print_summary(summary, as_json=as_json)
        return 0

    stream_names = [f"{normalized_symbol.lower()}@{stream}" for stream in normalized_streams]
    outcome_by_token = {normalized_tokens[0]: outcome} if outcome is not None else None
    binance = BinanceSpotMarketDataClient(symbol=normalized_symbol, streams=stream_names)
    polymarket = PolymarketMarketDataClient(
        normalized_tokens,
        outcome_by_token=outcome_by_token,
    )
    store = SQLiteEventStore(Path(store_path))
    try:
        summary = asyncio.run(
            record_session_events(
                binance_client=binance,
                polymarket_client=polymarket,
                store=store,
                store_path=store_path,
                symbol=normalized_symbol,
                binance_streams=normalized_streams,
                token_ids=normalized_tokens,
                max_events=max_events,
                duration_seconds=duration_seconds,
            )
        )
    except KeyboardInterrupt:
        now = _now_ms()
        summary = SessionRecordSummary(
            store_path=store_path,
            symbol=normalized_symbol,
            binance_streams=normalized_streams,
            token_ids=normalized_tokens,
            events_recorded=0,
            binance_events_recorded=0,
            polymarket_events_recorded=0,
            started_at_ms=now,
            ended_at_ms=now,
            duration_ms=0,
            stopped_reason=SessionStopReason.INTERRUPTED,
            stopped_source=None,
            error_type=None,
            error_message=None,
            failed_stage=None,
        )
    finally:
        store.close()

    _print_summary(summary, as_json=as_json)
    return 0 if summary.stopped_reason is not SessionStopReason.ERROR else 2


def _print_summary(summary: SessionRecordSummary, *, as_json: bool) -> None:
    payload = asdict(summary)
    payload["stopped_reason"] = summary.stopped_reason.value
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    print(
        "Recorded synchronized session "
        f"events={summary.events_recorded} "
        f"binance={summary.binance_events_recorded} "
        f"polymarket={summary.polymarket_events_recorded} "
        f"duration_ms={summary.duration_ms} "
        f"stopped_reason={summary.stopped_reason.value}"
    )
    if summary.error_type is not None:
        print(
            f"error_type={summary.error_type} "
            f"failed_stage={summary.failed_stage} "
            f"error_message={summary.error_message}"
        )
