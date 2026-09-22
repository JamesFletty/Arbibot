from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RepricingState:
    """Minimal, replayable state for source->destination repricing research."""

    source_move_bps_100ms: float
    source_move_bps_500ms: float
    destination_move_bps_100ms: float
    destination_best_bid: float
    destination_best_ask: float
    destination_depth_ask: float
    destination_book_age_ms: int
    source_event_age_ms: int
    estimated_edge_bps: float
    expected_cost_bps: float
    time_to_expiry_ms: int
    edge_basis: str = "fair_probability"

    @property
    def net_edge_bps(self) -> float:
        return self.estimated_edge_bps - self.expected_cost_bps

    @property
    def spread_bps(self) -> float:
        mid = (self.destination_best_bid + self.destination_best_ask) / 2
        if mid <= 0:
            return float("inf")
        return ((self.destination_best_ask - self.destination_best_bid) / mid) * 10_000

    @property
    def has_executable_edge_basis(self) -> bool:
        return self.edge_basis == "fair_probability"

    def as_model_state(self) -> dict[str, float | int | str]:
        return {
            "source_move_bps_100ms": self.source_move_bps_100ms,
            "source_move_bps_500ms": self.source_move_bps_500ms,
            "destination_move_bps_100ms": self.destination_move_bps_100ms,
            "destination_best_bid": self.destination_best_bid,
            "destination_best_ask": self.destination_best_ask,
            "destination_depth_ask": self.destination_depth_ask,
            "destination_book_age_ms": self.destination_book_age_ms,
            "source_event_age_ms": self.source_event_age_ms,
            "estimated_edge_bps": self.estimated_edge_bps,
            "expected_cost_bps": self.expected_cost_bps,
            "net_edge_bps": self.net_edge_bps,
            "spread_bps": self.spread_bps,
            "time_to_expiry_ms": self.time_to_expiry_ms,
            "edge_basis": self.edge_basis,
        }


@dataclass(frozen=True, slots=True)
class DeterministicGateResult:
    trade_candidate: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LagMeasurementGate:
    """Select clean lag observations without pretending a lag proxy is executable edge."""

    min_source_move_bps_100ms: float = 5.0
    max_book_age_ms: int = 250
    max_source_age_ms: int = 100
    min_ask_depth: float = 25.0
    min_time_to_expiry_ms: int = 15_000

    def evaluate(self, state: RepricingState) -> DeterministicGateResult:
        reasons: list[str] = []
        if abs(state.source_move_bps_100ms) < self.min_source_move_bps_100ms:
            reasons.append("SOURCE_MOVE_TOO_SMALL")
        if state.destination_book_age_ms > self.max_book_age_ms:
            reasons.append("DESTINATION_BOOK_STALE")
        if state.source_event_age_ms > self.max_source_age_ms:
            reasons.append("SOURCE_EVENT_STALE")
        if state.destination_depth_ask < self.min_ask_depth:
            reasons.append("INSUFFICIENT_ASK_DEPTH")
        if state.time_to_expiry_ms < self.min_time_to_expiry_ms:
            reasons.append("TOO_CLOSE_TO_EXPIRY")
        return DeterministicGateResult(not reasons, tuple(reasons))


@dataclass(frozen=True, slots=True)
class DeterministicLagGate:
    """Executable-edge gate; requires a fair-probability edge basis."""

    min_source_move_bps_100ms: float = 5.0
    min_net_edge_bps: float = 10.0
    max_book_age_ms: int = 250
    max_source_age_ms: int = 100
    min_ask_depth: float = 25.0
    min_time_to_expiry_ms: int = 15_000

    def evaluate(self, state: RepricingState) -> DeterministicGateResult:
        reasons: list[str] = []
        if not state.has_executable_edge_basis:
            reasons.append("EDGE_BASIS_NOT_EXECUTABLE")
        if abs(state.source_move_bps_100ms) < self.min_source_move_bps_100ms:
            reasons.append("SOURCE_MOVE_TOO_SMALL")
        if state.net_edge_bps < self.min_net_edge_bps:
            reasons.append("NET_EDGE_TOO_SMALL")
        if state.destination_book_age_ms > self.max_book_age_ms:
            reasons.append("DESTINATION_BOOK_STALE")
        if state.source_event_age_ms > self.max_source_age_ms:
            reasons.append("SOURCE_EVENT_STALE")
        if state.destination_depth_ask < self.min_ask_depth:
            reasons.append("INSUFFICIENT_ASK_DEPTH")
        if state.time_to_expiry_ms < self.min_time_to_expiry_ms:
            reasons.append("TOO_CLOSE_TO_EXPIRY")
        return DeterministicGateResult(not reasons, tuple(reasons))


@dataclass(frozen=True, slots=True)
class LatencyBudget:
    data_age_ms: float
    feature_compute_ms: float
    decision_ms: float
    order_submit_ms: float
    exchange_ack_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return (
            self.data_age_ms
            + self.feature_compute_ms
            + self.decision_ms
            + self.order_submit_ms
            + self.exchange_ack_ms
        )

    def survives(self, observed_repricing_window_ms: float, reserve_ms: float = 0.0) -> bool:
        if observed_repricing_window_ms < 0 or reserve_ms < 0:
            raise ValueError("latency window and reserve must be non-negative")
        return self.total_ms + reserve_ms < observed_repricing_window_ms


@dataclass(frozen=True, slots=True)
class JevGateResult:
    genuine_lag_probability: float
    adverse_selection_probability: float
    executable_probability: float
    model_latency_ms: float
    resolved_model: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "genuine_lag_probability",
            "adverse_selection_probability",
            "executable_probability",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.model_latency_ms < 0:
            raise ValueError("model_latency_ms must be non-negative")

    def passes(
        self,
        *,
        min_lag_probability: float = 0.75,
        max_adverse_selection_probability: float = 0.35,
        min_executable_probability: float = 0.70,
    ) -> bool:
        return (
            self.genuine_lag_probability >= min_lag_probability
            and self.adverse_selection_probability <= max_adverse_selection_probability
            and self.executable_probability >= min_executable_probability
        )


class RepricingJudge(Protocol):
    def judge(self, state: RepricingState) -> JevGateResult: ...


class JevRepricingGate:
    """Research-only Jev adapter for states with a real fair-probability edge basis."""

    def __init__(self, client: Any | None = None, *, model: str = "jev-latest") -> None:
        self._client = client
        self._model = model

    def judge(self, state: RepricingState) -> JevGateResult:
        if not state.has_executable_edge_basis:
            raise ValueError("Jev executable gating requires edge_basis='fair_probability'")

        client = self._client
        owns_client = client is None
        if client is None:
            try:
                from typesafe_sdk import Noul, TypeSafeClient  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "Jev research support requires the optional 'jev' dependency: "
                    "pip install 'arbibot[jev]'"
                ) from exc
            client = TypeSafeClient()
        else:
            try:
                from typesafe_sdk import Noul
            except ImportError as exc:
                raise RuntimeError(
                    "Jev research support requires the optional 'jev' dependency: "
                    "pip install 'arbibot[jev]'"
                ) from exc

        questions = {
            "genuine_lag": Noul(
                instructions=(
                    "Is the destination quote likely lagging the source move rather than already "
                    "reflecting it? Judge only from the supplied market state."
                )
            ),
            "adverse_selection": Noul(
                instructions=(
                    "Is this apparent edge likely caused by stale, transient, or adverse-selection "
                    "conditions that make immediate execution unattractive?"
                )
            ),
            "executable": Noul(
                instructions=(
                    "Given the supplied spread, depth, fair-probability edge, data ages, and expiry, "
                    "is the repricing edge likely executable before it disappears?"
                )
            ),
        }

        started = time.perf_counter_ns()
        try:
            response = client.system_one(
                state=state.as_model_state(),
                questions=questions,
                model=self._model,
            )
        finally:
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            if owns_client:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        answers = response.answers
        resolved_model = getattr(response, "model", None)
        return JevGateResult(
            genuine_lag_probability=float(answers["genuine_lag"].noul),
            adverse_selection_probability=float(answers["adverse_selection"].noul),
            executable_probability=float(answers["executable"].noul),
            model_latency_ms=elapsed_ms,
            resolved_model=str(resolved_model) if resolved_model is not None else None,
        )
