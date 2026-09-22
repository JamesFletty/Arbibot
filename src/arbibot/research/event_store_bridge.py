from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError

from arbibot.core.events import PolyBookDelta, PolyBookSnapshot, SpotTick
from arbibot.market.book import LocalOrderBook
from arbibot.research.jev_repricing import RepricingState
from arbibot.research.repricing_benchmark import DEFAULT_HORIZONS_MS, FutureQuote, RepricingCase
from arbibot.storage.event_store import EventStore


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    symbol: str = "BTCUSDT"
    token_id: str | None = None
    market_expiry_ts_ms: int | None = None
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    fee_cost_bps: float = 0.0
    extra_cost_bps: float = 0.0
    min_source_move_bps_100ms: float = 0.0

    def __post_init__(self) -> None:
        if any(h <= 0 for h in self.horizons_ms):
            raise ValueError("horizons_ms must contain only positive values")
        if self.fee_cost_bps < 0 or self.extra_cost_bps < 0:
            raise ValueError("cost inputs must be non-negative")
        if self.min_source_move_bps_100ms < 0:
            raise ValueError("min_source_move_bps_100ms must be non-negative")


@dataclass(frozen=True, slots=True)
class BookQuote:
    token_id: str
    source_ts_ms: int
    recv_wall_ts_ms: int
    best_bid: float
    best_ask: float
    ask_depth: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> float:
        if self.mid <= 0:
            return float("inf")
        return ((self.best_ask - self.best_bid) / self.mid) * 10_000


@dataclass(frozen=True, slots=True)
class BridgeSummary:
    spot_ticks_seen: int
    book_events_seen: int
    quotes_emitted: int
    cases_emitted: int
    malformed_events: int
    skipped_no_history: int
    skipped_no_book: int
    skipped_no_future_quote: int


@dataclass(frozen=True, slots=True)
class BridgeResult:
    cases: tuple[RepricingCase, ...]
    summary: BridgeSummary


def _bps_return(new: float, old: float) -> float:
    if old <= 0:
        raise ValueError("reference price must be positive")
    return ((new - old) / old) * 10_000


def _prior_spot_price(
    timestamps: list[int],
    prices: list[float],
    cutoff_ms: int,
) -> float | None:
    idx = bisect_right(timestamps, cutoff_ms) - 1
    if idx < 0:
        return None
    return prices[idx]


def _quote_at_or_before(quotes: list[BookQuote], timestamps: list[int], ts_ms: int) -> BookQuote | None:
    idx = bisect_right(timestamps, ts_ms) - 1
    if idx < 0:
        return None
    return quotes[idx]


def _future_quotes(
    quotes: list[BookQuote],
    timestamps: list[int],
    ts_ms: int,
    horizons_ms: tuple[int, ...],
) -> tuple[FutureQuote, ...]:
    result: list[FutureQuote] = []
    seen_indices: set[int] = set()
    for horizon in horizons_ms:
        idx = bisect_left(timestamps, ts_ms + horizon)
        if idx >= len(quotes) or idx in seen_indices:
            continue
        seen_indices.add(idx)
        quote = quotes[idx]
        result.append(
            FutureQuote(
                offset_ms=quote.source_ts_ms - ts_ms,
                best_bid=quote.best_bid,
                best_ask=quote.best_ask,
            )
        )
    return tuple(sorted(result, key=lambda q: q.offset_ms))


def extract_repricing_cases(store: EventStore, config: BridgeConfig) -> BridgeResult:
    """Convert persisted spot/book events into replayable repricing benchmark cases.

    `estimated_edge_bps` is intentionally a research proxy: the absolute difference between the
    100 ms source move and the destination market's 100 ms move. It is not a fair-value estimate
    or expected PnL. Costs include the observed prediction-market spread plus configured fees and
    extra execution cost. This keeps the bridge useful for lag measurement without pretending the
    source move maps one-for-one to prediction-market probability.
    """

    spot_ticks: list[SpotTick] = []
    books: dict[str, LocalOrderBook] = {}
    quotes_by_token: dict[str, list[BookQuote]] = {}
    malformed_events = 0
    book_events_seen = 0

    for stored in store.iter_events(event_types=["SpotTick", "PolyBookSnapshot", "PolyBookDelta"]):
        try:
            payload = json.loads(stored.payload_json)
            if stored.event_type == "SpotTick":
                tick = SpotTick.model_validate(payload)
                if tick.symbol == config.symbol and tick.price > 0:
                    spot_ticks.append(tick)
                continue

            event: PolyBookSnapshot | PolyBookDelta
            if stored.event_type == "PolyBookSnapshot":
                event = PolyBookSnapshot.model_validate(payload)
            else:
                event = PolyBookDelta.model_validate(payload)
            book_events_seen += 1
            token_id = event.token_id or ""
            if not token_id or (config.token_id is not None and token_id != config.token_id):
                continue
            book = books.get(token_id)
            if book is None:
                if not isinstance(event, PolyBookSnapshot):
                    continue
                book = LocalOrderBook(token_id)
                books[token_id] = book
            if isinstance(event, PolyBookSnapshot):
                book.apply_snapshot(event)
            else:
                book.apply_delta(event)
            bid = book.best_bid()
            ask = book.best_ask()
            if bid is None or ask is None or book.is_crossed():
                continue
            quotes_by_token.setdefault(token_id, []).append(
                BookQuote(
                    token_id=token_id,
                    source_ts_ms=event.source_ts_ms,
                    recv_wall_ts_ms=event.recv_wall_ts_ms,
                    best_bid=float(bid.price),
                    best_ask=float(ask.price),
                    ask_depth=float(book.depth("ask", levels=3)),
                )
            )
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            malformed_events += 1

    spot_ticks.sort(key=lambda tick: (tick.source_ts_ms, tick.recv_wall_ts_ms))
    spot_ts = [tick.source_ts_ms for tick in spot_ticks]
    spot_prices = [tick.price for tick in spot_ticks]
    for token_quotes in quotes_by_token.values():
        token_quotes.sort(key=lambda quote: (quote.source_ts_ms, quote.recv_wall_ts_ms))

    cases: list[RepricingCase] = []
    skipped_no_history = 0
    skipped_no_book = 0
    skipped_no_future_quote = 0

    for idx, tick in enumerate(spot_ticks):
        prior_100 = _prior_spot_price(spot_ts, spot_prices, tick.source_ts_ms - 100)
        prior_500 = _prior_spot_price(spot_ts, spot_prices, tick.source_ts_ms - 500)
        if prior_100 is None or prior_500 is None:
            skipped_no_history += 1
            continue
        source_100 = _bps_return(tick.price, prior_100)
        source_500 = _bps_return(tick.price, prior_500)
        if abs(source_100) < config.min_source_move_bps_100ms:
            continue

        candidate_tokens = [config.token_id] if config.token_id is not None else list(quotes_by_token)
        for token_id in candidate_tokens:
            if token_id is None:
                continue
            quotes = quotes_by_token.get(token_id, [])
            if not quotes:
                skipped_no_book += 1
                continue
            quote_ts = [quote.source_ts_ms for quote in quotes]
            current_quote = _quote_at_or_before(quotes, quote_ts, tick.source_ts_ms)
            prior_quote = _quote_at_or_before(quotes, quote_ts, tick.source_ts_ms - 100)
            if current_quote is None:
                skipped_no_book += 1
                continue
            destination_100 = 0.0
            if prior_quote is not None:
                destination_100 = _bps_return(current_quote.mid, prior_quote.mid)

            future = _future_quotes(quotes, quote_ts, tick.source_ts_ms, config.horizons_ms)
            if not future:
                skipped_no_future_quote += 1
                continue

            lag_gap_bps = abs(source_100 - destination_100)
            expected_cost_bps = current_quote.spread_bps + config.fee_cost_bps + config.extra_cost_bps
            expiry_ms = (
                max(config.market_expiry_ts_ms - tick.source_ts_ms, 0)
                if config.market_expiry_ts_ms is not None
                else 2**31 - 1
            )
            state = RepricingState(
                source_move_bps_100ms=source_100,
                source_move_bps_500ms=source_500,
                destination_move_bps_100ms=destination_100,
                destination_best_bid=current_quote.best_bid,
                destination_best_ask=current_quote.best_ask,
                destination_depth_ask=current_quote.ask_depth,
                destination_book_age_ms=max(tick.source_ts_ms - current_quote.source_ts_ms, 0),
                source_event_age_ms=max(tick.recv_wall_ts_ms - tick.source_ts_ms, 0),
                estimated_edge_bps=lag_gap_bps,
                expected_cost_bps=expected_cost_bps,
                time_to_expiry_ms=expiry_ms,
            )
            cases.append(
                RepricingCase(
                    case_id=f"{tick.event_id}:{token_id}:{idx}",
                    state=state,
                    current_mid=current_quote.mid,
                    future_quotes=future,
                )
            )

    return BridgeResult(
        cases=tuple(cases),
        summary=BridgeSummary(
            spot_ticks_seen=len(spot_ticks),
            book_events_seen=book_events_seen,
            quotes_emitted=sum(len(quotes) for quotes in quotes_by_token.values()),
            cases_emitted=len(cases),
            malformed_events=malformed_events,
            skipped_no_history=skipped_no_history,
            skipped_no_book=skipped_no_book,
            skipped_no_future_quote=skipped_no_future_quote,
        ),
    )
