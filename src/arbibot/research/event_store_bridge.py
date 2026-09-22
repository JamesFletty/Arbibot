from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from decimal import Decimal
from math import log, sqrt

from pydantic import ValidationError

from arbibot.core.events import PolyBookDelta, PolyBookSnapshot, SpotTick
from arbibot.market.book import LocalOrderBook
from arbibot.model.fair_price import FairPriceInput, FairPriceModel
from arbibot.opportunity.edge import OutcomeSide
from arbibot.research.jev_repricing import RepricingState
from arbibot.research.repricing_benchmark import DEFAULT_HORIZONS_MS, FutureQuote, RepricingCase
from arbibot.storage.event_store import EventStore

_NS_PER_MS = 1_000_000


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    symbol: str = "BTCUSDT"
    token_id: str | None = None
    market_expiry_ts_ms: int | None = None
    threshold_price: float | None = None
    outcome_side: OutcomeSide | None = None
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
        fair_fields = (
            self.market_expiry_ts_ms is not None,
            self.threshold_price is not None,
            self.outcome_side is not None,
        )
        if any(fair_fields) and not all(fair_fields):
            raise ValueError(
                "fair edge requires market_expiry_ts_ms, threshold_price, and outcome_side"
            )
        if self.threshold_price is not None and self.threshold_price <= 0:
            raise ValueError("threshold_price must be > 0")

    @property
    def has_fair_edge_inputs(self) -> bool:
        return (
            self.market_expiry_ts_ms is not None
            and self.threshold_price is not None
            and self.outcome_side is not None
        )


@dataclass(frozen=True, slots=True)
class BookQuote:
    token_id: str
    source_ts_ms: int
    recv_wall_ts_ms: int
    recv_monotonic_ns: int
    best_bid: float
    best_ask: float
    ask_depth: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2


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
    recv_times_ns: list[int],
    prices: list[float],
    cutoff_ns: int,
) -> float | None:
    idx = bisect_right(recv_times_ns, cutoff_ns) - 1
    if idx < 0:
        return None
    return prices[idx]


def _realized_volatility(
    recv_times_ns: list[int],
    prices: list[float],
    current_index: int,
    window_ms: int,
) -> Decimal | None:
    cutoff_ns = recv_times_ns[current_index] - window_ms * _NS_PER_MS
    start = bisect_left(recv_times_ns, cutoff_ns, 0, current_index + 1)
    window_prices = prices[start : current_index + 1]
    if len(window_prices) < 3:
        return None
    returns = [
        log(current / previous)
        for previous, current in zip(window_prices, window_prices[1:], strict=False)
        if previous > 0 and current > 0
    ]
    if len(returns) < 2:
        return None
    average = sum(returns) / len(returns)
    variance = sum((value - average) ** 2 for value in returns) / len(returns)
    return Decimal(str(sqrt(variance)))


def _quote_at_or_before(
    quotes: list[BookQuote],
    recv_times_ns: list[int],
    recv_monotonic_ns: int,
) -> BookQuote | None:
    idx = bisect_right(recv_times_ns, recv_monotonic_ns) - 1
    if idx < 0:
        return None
    return quotes[idx]


def _future_quotes(
    quotes: list[BookQuote],
    recv_times_ns: list[int],
    recv_monotonic_ns: int,
    horizons_ms: tuple[int, ...],
) -> tuple[FutureQuote, ...]:
    result: list[FutureQuote] = []
    seen_indices: set[int] = set()
    for horizon_ms in horizons_ms:
        target_ns = recv_monotonic_ns + horizon_ms * _NS_PER_MS
        idx = bisect_left(recv_times_ns, target_ns)
        if idx >= len(quotes) or idx in seen_indices:
            continue
        seen_indices.add(idx)
        quote = quotes[idx]
        result.append(
            FutureQuote(
                offset_ms=(quote.recv_monotonic_ns - recv_monotonic_ns) / _NS_PER_MS,
                best_bid=quote.best_bid,
                best_ask=quote.best_ask,
            )
        )
    return tuple(sorted(result, key=lambda q: q.offset_ms))


def _edge_inputs(
    *,
    config: BridgeConfig,
    tick: SpotTick,
    current_quote: BookQuote,
    realized_volatility: Decimal | None,
) -> tuple[float, float, str, int]:
    if not config.has_fair_edge_inputs:
        return 0.0, 0.0, "lag_proxy", 2**31 - 1

    assert config.market_expiry_ts_ms is not None
    assert config.threshold_price is not None
    assert config.outcome_side is not None
    expiry_ms = max(config.market_expiry_ts_ms - tick.source_ts_ms, 0)
    if expiry_ms <= 0:
        return 0.0, config.fee_cost_bps + config.extra_cost_bps, "fair_probability", 0

    fair = FairPriceModel().estimate(
        FairPriceInput(
            spot_price=Decimal(str(tick.price)),
            threshold_price=Decimal(str(config.threshold_price)),
            seconds_to_expiry=Decimal(expiry_ms) / Decimal("1000"),
            realized_volatility=realized_volatility,
            momentum=None,
        )
    )
    fair_probability = (
        fair.fair_up_probability
        if config.outcome_side is OutcomeSide.UP
        else fair.fair_down_probability
    )
    gross_edge_bps = (float(fair_probability) - current_quote.best_ask) * 10_000
    expected_cost_bps = config.fee_cost_bps + config.extra_cost_bps
    return gross_edge_bps, expected_cost_bps, "fair_probability", expiry_ms


def extract_repricing_cases(store: EventStore, config: BridgeConfig) -> BridgeResult:
    """Convert persisted spot/book events into replayable repricing benchmark cases.

    Cross-venue sequencing uses the local monotonic receive clock. Exchange source timestamps are
    not comparable across venues and are not used to infer reaction latency.

    Without threshold/expiry/outcome inputs, cases are lag-measurement only and carry
    ``edge_basis='lag_proxy'``. With all three inputs, executable edge is expressed consistently as
    absolute fair-probability basis points: ``(fair_probability - executable_ask) * 10_000``.
    Configured fee and extra costs use the same absolute probability-bps basis. The ask is already
    the executable price, so spread is not subtracted a second time.
    """

    spot_ticks: list[SpotTick] = []
    books: dict[str, LocalOrderBook] = {}
    quotes_by_token: dict[str, list[BookQuote]] = {}
    malformed_events = 0
    book_events_seen = 0

    event_types = ["SpotTick", "PolyBookSnapshot", "PolyBookDelta"]
    for stored in store.iter_events(event_types=event_types):
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
                    recv_monotonic_ns=event.recv_monotonic_ns,
                    best_bid=float(bid.price),
                    best_ask=float(ask.price),
                    ask_depth=float(book.depth("ask", levels=3)),
                )
            )
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            malformed_events += 1

    spot_ticks.sort(key=lambda tick: tick.recv_monotonic_ns)
    spot_recv_ns = [tick.recv_monotonic_ns for tick in spot_ticks]
    spot_prices = [tick.price for tick in spot_ticks]
    for token_quotes in quotes_by_token.values():
        token_quotes.sort(key=lambda quote: quote.recv_monotonic_ns)

    cases: list[RepricingCase] = []
    skipped_no_history = 0
    skipped_no_book = 0
    skipped_no_future_quote = 0

    for idx, tick in enumerate(spot_ticks):
        prior_100 = _prior_spot_price(
            spot_recv_ns,
            spot_prices,
            tick.recv_monotonic_ns - 100 * _NS_PER_MS,
        )
        prior_500 = _prior_spot_price(
            spot_recv_ns,
            spot_prices,
            tick.recv_monotonic_ns - 500 * _NS_PER_MS,
        )
        if prior_100 is None or prior_500 is None:
            skipped_no_history += 1
            continue
        source_100 = _bps_return(tick.price, prior_100)
        source_500 = _bps_return(tick.price, prior_500)
        if abs(source_100) < config.min_source_move_bps_100ms:
            continue

        volatility = _realized_volatility(spot_recv_ns, spot_prices, idx, 30_000)
        if volatility is None:
            volatility = _realized_volatility(spot_recv_ns, spot_prices, idx, 5_000)

        candidate_tokens = (
            [config.token_id]
            if config.token_id is not None
            else list(quotes_by_token)
        )
        for token_id in candidate_tokens:
            if token_id is None:
                continue
            quotes = quotes_by_token.get(token_id, [])
            if not quotes:
                skipped_no_book += 1
                continue
            quote_recv_ns = [quote.recv_monotonic_ns for quote in quotes]
            current_quote = _quote_at_or_before(
                quotes,
                quote_recv_ns,
                tick.recv_monotonic_ns,
            )
            prior_quote = _quote_at_or_before(
                quotes,
                quote_recv_ns,
                tick.recv_monotonic_ns - 100 * _NS_PER_MS,
            )
            if current_quote is None:
                skipped_no_book += 1
                continue
            destination_100 = 0.0
            if prior_quote is not None:
                destination_100 = _bps_return(current_quote.mid, prior_quote.mid)

            future = _future_quotes(
                quotes,
                quote_recv_ns,
                tick.recv_monotonic_ns,
                config.horizons_ms,
            )
            if not future:
                skipped_no_future_quote += 1
                continue

            estimated_edge_bps, expected_cost_bps, edge_basis, expiry_ms = _edge_inputs(
                config=config,
                tick=tick,
                current_quote=current_quote,
                realized_volatility=volatility,
            )
            if edge_basis == "lag_proxy":
                estimated_edge_bps = abs(source_100 - destination_100)

            state = RepricingState(
                source_move_bps_100ms=source_100,
                source_move_bps_500ms=source_500,
                destination_move_bps_100ms=destination_100,
                destination_best_bid=current_quote.best_bid,
                destination_best_ask=current_quote.best_ask,
                destination_depth_ask=current_quote.ask_depth,
                destination_book_age_ms=max(
                    int((tick.recv_monotonic_ns - current_quote.recv_monotonic_ns) / _NS_PER_MS),
                    0,
                ),
                source_event_age_ms=0,
                estimated_edge_bps=estimated_edge_bps,
                expected_cost_bps=expected_cost_bps,
                time_to_expiry_ms=expiry_ms,
                edge_basis=edge_basis,
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
