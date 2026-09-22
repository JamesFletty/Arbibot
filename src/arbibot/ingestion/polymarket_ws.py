"""Polymarket public market WebSocket normalization and client adapter."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import websockets

from arbibot.core.errors import EventValidationError
from arbibot.core.events import BaseEvent, BookLevel, MarketSide, PolyBookDelta, PolyBookSnapshot
from arbibot.core.time import now_monotonic_ns, now_wall_ms


class PolymarketPayloadError(EventValidationError):
    """Raised when a Polymarket market-channel payload cannot be normalized."""


@dataclass(frozen=True, slots=True)
class PolymarketClientConfig:
    url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    reconnect_initial_delay_ms: int = 250
    reconnect_max_delay_ms: int = 5_000
    heartbeat_interval_seconds: float = 10.0
    ignore_unsupported_events: bool = True


def _positive_int_from_text(value: object, field: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise PolymarketPayloadError(f"Invalid integer field: {field}") from exc
    if parsed <= 0:
        raise PolymarketPayloadError(f"Field must be positive: {field}")
    return parsed


def _positive_float_from_text(value: object, field: str) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise PolymarketPayloadError(f"Invalid numeric field: {field}") from exc
    if parsed <= 0:
        raise PolymarketPayloadError(f"Field must be > 0: {field}")
    return parsed


def _non_negative_float_from_text(value: object, field: str) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise PolymarketPayloadError(f"Invalid numeric field: {field}") from exc
    if parsed < 0:
        raise PolymarketPayloadError(f"Field must be >= 0: {field}")
    return parsed


def _levels(value: object, field: str) -> list[BookLevel]:
    if not isinstance(value, list):
        raise PolymarketPayloadError(f"{field} must be a list")
    result: list[BookLevel] = []
    for item in value:
        if not isinstance(item, dict):
            raise PolymarketPayloadError(f"{field} entries must be objects")
        result.append(
            BookLevel(
                price=_positive_float_from_text(item.get("price"), f"{field}.price"),
                size=_non_negative_float_from_text(item.get("size"), f"{field}.size"),
            )
        )
    return result


def parse_polymarket_payload(
    payload: dict[str, Any],
    *,
    recv_wall_ts_ms: int,
    recv_monotonic_ns: int,
    outcome_by_token: dict[str, str] | None = None,
) -> list[PolyBookSnapshot | PolyBookDelta]:
    event_type = payload.get("event_type")
    timestamp = _positive_int_from_text(payload.get("timestamp"), "timestamp")
    outcomes = outcome_by_token or {}

    if event_type == "book":
        token_id = str(payload.get("asset_id") or "")
        market_id = str(payload.get("market") or "")
        if not token_id or not market_id:
            raise PolymarketPayloadError("book requires asset_id and market")
        sequence = str(payload.get("hash") or timestamp)
        event = PolyBookSnapshot(
            event_id=f"polymarket-book-{token_id}-{sequence}",
            source="polymarket",
            source_ts_ms=timestamp,
            recv_wall_ts_ms=recv_wall_ts_ms,
            recv_monotonic_ns=recv_monotonic_ns,
            sequence_id=sequence,
            market_id=market_id,
            outcome=outcomes.get(token_id, "UNKNOWN"),
            token_id=token_id,
            bids=_levels(payload.get("bids"), "bids"),
            asks=_levels(payload.get("asks"), "asks"),
        )
        return [event]

    if event_type == "price_change":
        market_id = str(payload.get("market") or "")
        changes = payload.get("price_changes")
        if not market_id or not isinstance(changes, list):
            raise PolymarketPayloadError("price_change requires market and price_changes")
        events: list[PolyBookDelta] = []
        for index, raw_change in enumerate(changes):
            if not isinstance(raw_change, dict):
                raise PolymarketPayloadError("price_changes entries must be objects")
            token_id = str(raw_change.get("asset_id") or "")
            side_raw = str(raw_change.get("side") or "").upper()
            if not token_id or side_raw not in {"BUY", "SELL"}:
                raise PolymarketPayloadError("price change requires asset_id and BUY/SELL side")
            price = _positive_float_from_text(raw_change.get("price"), "price")
            size = _non_negative_float_from_text(raw_change.get("size"), "size")
            change_hash = str(raw_change.get("hash") or f"{timestamp}-{index}")
            events.append(
                PolyBookDelta(
                    event_id=(
                        f"polymarket-price_change-{token_id}-{timestamp}-{index}-{change_hash}"
                    ),
                    source="polymarket",
                    source_ts_ms=timestamp,
                    recv_wall_ts_ms=recv_wall_ts_ms,
                    recv_monotonic_ns=recv_monotonic_ns,
                    sequence_id=change_hash,
                    market_id=market_id,
                    outcome=outcomes.get(token_id, "UNKNOWN"),
                    token_id=token_id,
                    side=MarketSide.BUY if side_raw == "BUY" else MarketSide.SELL,
                    price=price,
                    size=size,
                )
            )
        return events

    return []


class PolymarketMarketDataClient:
    source = "polymarket"

    def __init__(
        self,
        token_ids: list[str],
        *,
        outcome_by_token: dict[str, str] | None = None,
        config: PolymarketClientConfig | None = None,
    ) -> None:
        normalized = [token.strip() for token in token_ids if token.strip()]
        if not normalized:
            raise ValueError("token_ids must contain at least one token")
        self.token_ids = normalized
        self.outcome_by_token = outcome_by_token or {}
        self.config = config or PolymarketClientConfig()
        self.market_id = "dynamic"
        self.outcome = "multiple" if len(normalized) > 1 else self.outcome_by_token.get(
            normalized[0], "UNKNOWN"
        )
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        self._stop_event.clear()

    async def stop(self) -> None:
        self._stop_event.set()

    def events(self) -> AsyncIterator[BaseEvent]:
        return self._events_impl()

    async def _events_impl(self) -> AsyncIterator[BaseEvent]:
        delay_ms = self.config.reconnect_initial_delay_ms
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.config.url) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "assets_ids": self.token_ids,
                                "type": "market",
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    delay_ms = self.config.reconnect_initial_delay_ms
                    last_ping = time.monotonic()
                    while not self._stop_event.is_set():
                        now = time.monotonic()
                        if now - last_ping >= self.config.heartbeat_interval_seconds:
                            await ws.send("PING")
                            last_ping = now
                        try:
                            raw_message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except TimeoutError:
                            continue
                        if raw_message == "PONG":
                            continue
                        payload = json.loads(raw_message)
                        payloads = payload if isinstance(payload, list) else [payload]
                        for item in payloads:
                            if not isinstance(item, dict):
                                raise PolymarketPayloadError(
                                    "WebSocket message must decode to object or object list"
                                )
                            parsed = parse_polymarket_payload(
                                item,
                                recv_wall_ts_ms=now_wall_ms(),
                                recv_monotonic_ns=now_monotonic_ns(),
                                outcome_by_token=self.outcome_by_token,
                            )
                            if not parsed and not self.config.ignore_unsupported_events:
                                raise PolymarketPayloadError("Unsupported Polymarket event type")
                            for event in parsed:
                                if event.token_id in self.token_ids:
                                    yield event
            except PolymarketPayloadError:
                raise
            except Exception:
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(delay_ms / 1000.0)
                delay_ms = min(delay_ms * 2, self.config.reconnect_max_delay_ms)
