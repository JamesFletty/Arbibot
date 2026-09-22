from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from arbibot.ingestion.interfaces import PredictionMarketDataClient
from arbibot.ingestion.polymarket_ws import PolymarketMarketDataClient
from arbibot.storage.event_store import EventStore
from arbibot.storage.sqlite_store import SQLiteEventStore


class PolyRecordStopReason(StrEnum):
    MAX_EVENTS_REACHED = "MAX_EVENTS_REACHED"
    DURATION_REACHED = "DURATION_REACHED"
    INTERRUPTED = "INTERRUPTED"
    CLIENT_STOPPED = "CLIENT_STOPPED"
    ERROR = "ERROR"
    DRY_RUN = "DRY_RUN"


@dataclass(frozen=True, slots=True)
class PolyRecordSummary:
    source: str
    token_ids: list[str]
    store_path: str
    events_recorded: int
    started_at_ms: int
    ended_at_ms: int
    duration_ms: int
    stopped_reason: PolyRecordStopReason
    errors_count: int


def _now_ms() -> int:
    return int(time.time() * 1000)


async def record_polymarket_events(
    client: PredictionMarketDataClient,
    store: EventStore,
    *,
    token_ids: list[str],
    store_path: str,
    max_events: int | None = None,
    duration_seconds: int | None = None,
) -> PolyRecordSummary:
    if max_events is not None and max_events < 0:
        raise ValueError("max_events must be >= 0")
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")

    started_at_ms = _now_ms()
    deadline_ms = None if duration_seconds is None else started_at_ms + duration_seconds * 1000
    events_recorded = 0
    errors_count = 0
    reason = PolyRecordStopReason.CLIENT_STOPPED

    await client.start()
    try:
        async for event in client.events():
            if deadline_ms is not None and _now_ms() >= deadline_ms:
                reason = PolyRecordStopReason.DURATION_REACHED
                break
            store.append(event)
            events_recorded += 1
            if max_events is not None and events_recorded >= max_events:
                reason = PolyRecordStopReason.MAX_EVENTS_REACHED
                break
        else:
            reason = PolyRecordStopReason.CLIENT_STOPPED
    except (asyncio.CancelledError, KeyboardInterrupt):
        reason = PolyRecordStopReason.INTERRUPTED
    except Exception:
        errors_count += 1
        reason = PolyRecordStopReason.ERROR
    finally:
        await client.stop()

    ended_at_ms = _now_ms()
    return PolyRecordSummary(
        source=client.source,
        token_ids=token_ids,
        store_path=store_path,
        events_recorded=events_recorded,
        started_at_ms=started_at_ms,
        ended_at_ms=ended_at_ms,
        duration_ms=max(0, ended_at_ms - started_at_ms),
        stopped_reason=reason,
        errors_count=errors_count,
    )


def run_record_polymarket(
    *,
    store_path: str,
    token_ids: list[str],
    outcome: str | None,
    duration_seconds: int | None,
    max_events: int | None,
    as_json: bool,
    dry_run: bool,
) -> int:
    normalized = [token.strip() for token in token_ids if token.strip()]
    if not normalized:
        print("record-polymarket validation failed: at least one --token-id is required")
        return 1
    if outcome is not None and len(normalized) != 1:
        print("record-polymarket validation failed: --outcome requires exactly one token")
        return 1

    if dry_run:
        now = _now_ms()
        summary = PolyRecordSummary(
            source="polymarket",
            token_ids=normalized,
            store_path=store_path,
            events_recorded=0,
            started_at_ms=now,
            ended_at_ms=now,
            duration_ms=0,
            stopped_reason=PolyRecordStopReason.DRY_RUN,
            errors_count=0,
        )
        _print_summary(summary, as_json=as_json)
        return 0

    outcome_by_token = {normalized[0]: outcome} if outcome is not None else None
    store = SQLiteEventStore(Path(store_path))
    client = PolymarketMarketDataClient(normalized, outcome_by_token=outcome_by_token)
    try:
        summary = asyncio.run(
            record_polymarket_events(
                client,
                store,
                token_ids=normalized,
                store_path=store_path,
                max_events=max_events,
                duration_seconds=duration_seconds,
            )
        )
    except KeyboardInterrupt:
        now = _now_ms()
        summary = PolyRecordSummary(
            source="polymarket",
            token_ids=normalized,
            store_path=store_path,
            events_recorded=0,
            started_at_ms=now,
            ended_at_ms=now,
            duration_ms=0,
            stopped_reason=PolyRecordStopReason.INTERRUPTED,
            errors_count=0,
        )
    finally:
        store.close()

    _print_summary(summary, as_json=as_json)
    return 0 if summary.stopped_reason is not PolyRecordStopReason.ERROR else 2


def _print_summary(summary: PolyRecordSummary, *, as_json: bool) -> None:
    payload = asdict(summary)
    payload["stopped_reason"] = summary.stopped_reason.value
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    print(
        "Recorded Polymarket events "
        f"events_recorded={summary.events_recorded} "
        f"tokens={','.join(summary.token_ids)} "
        f"duration_ms={summary.duration_ms} "
        f"stopped_reason={summary.stopped_reason.value} "
        f"errors_count={summary.errors_count}"
    )
