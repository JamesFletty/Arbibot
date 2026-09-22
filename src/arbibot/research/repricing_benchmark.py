from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Iterable

from arbibot.research.jev_repricing import (
    DeterministicLagGate,
    JevGateResult,
    RepricingJudge,
    RepricingState,
)

DEFAULT_HORIZONS_MS = (50, 100, 250, 500)


@dataclass(frozen=True, slots=True)
class FutureQuote:
    offset_ms: int
    best_bid: float
    best_ask: float

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2


@dataclass(frozen=True, slots=True)
class RepricingCase:
    case_id: str
    state: RepricingState
    current_mid: float
    future_quotes: tuple[FutureQuote, ...]

    def quote_at_or_after(self, horizon_ms: int) -> FutureQuote | None:
        eligible = [q for q in self.future_quotes if q.offset_ms >= horizon_ms]
        if not eligible:
            return None
        return min(eligible, key=lambda q: q.offset_ms)


@dataclass(frozen=True, slots=True)
class HorizonLabel:
    horizon_ms: int
    available: bool
    destination_move_bps: float | None
    repriced_with_source: bool | None
    edge_survived_bps: float | None


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    case_id: str
    deterministic_candidate: bool
    jev_candidate: bool | None
    jev_model_latency_ms: float | None
    labels: tuple[HorizonLabel, ...]


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    candidates: int
    labeled: int
    repricing_hits: int
    precision: float | None
    mean_surviving_edge_bps: float | None
    mean_model_latency_ms: float | None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    deterministic: dict[int, ArmMetrics]
    jev: dict[int, ArmMetrics] | None


def _same_direction(source_move_bps: float, destination_move_bps: float) -> bool:
    return source_move_bps != 0 and source_move_bps * destination_move_bps > 0


def label_case(
    case: RepricingCase,
    *,
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS,
    min_reprice_bps: float = 1.0,
) -> tuple[HorizonLabel, ...]:
    if case.current_mid <= 0:
        raise ValueError("current_mid must be positive")
    if min_reprice_bps < 0:
        raise ValueError("min_reprice_bps must be non-negative")

    labels: list[HorizonLabel] = []
    source_move = case.state.source_move_bps_100ms
    for horizon_ms in horizons_ms:
        if horizon_ms <= 0:
            raise ValueError("horizons must be positive")
        quote = case.quote_at_or_after(horizon_ms)
        if quote is None:
            labels.append(HorizonLabel(horizon_ms, False, None, None, None))
            continue
        move_bps = ((quote.mid - case.current_mid) / case.current_mid) * 10_000
        repriced = abs(move_bps) >= min_reprice_bps and _same_direction(source_move, move_bps)
        edge_survived = case.state.net_edge_bps - abs(move_bps)
        labels.append(
            HorizonLabel(
                horizon_ms=horizon_ms,
                available=True,
                destination_move_bps=move_bps,
                repriced_with_source=repriced,
                edge_survived_bps=edge_survived,
            )
        )
    return tuple(labels)


def evaluate_cases(
    cases: Iterable[RepricingCase],
    *,
    deterministic_gate: DeterministicLagGate | None = None,
    jev_judge: RepricingJudge | None = None,
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS,
    min_reprice_bps: float = 1.0,
) -> list[CaseEvaluation]:
    gate = deterministic_gate or DeterministicLagGate()
    evaluations: list[CaseEvaluation] = []
    for case in cases:
        deterministic = gate.evaluate(case.state)
        jev_result: JevGateResult | None = None
        if jev_judge is not None and deterministic.trade_candidate:
            jev_result = jev_judge.judge(case.state)
        evaluations.append(
            CaseEvaluation(
                case_id=case.case_id,
                deterministic_candidate=deterministic.trade_candidate,
                jev_candidate=jev_result.passes() if jev_result is not None else None,
                jev_model_latency_ms=(
                    jev_result.model_latency_ms if jev_result is not None else None
                ),
                labels=label_case(
                    case,
                    horizons_ms=horizons_ms,
                    min_reprice_bps=min_reprice_bps,
                ),
            )
        )
    return evaluations


def _metrics_for_arm(
    evaluations: Iterable[CaseEvaluation],
    *,
    horizon_ms: int,
    arm: str,
) -> ArmMetrics:
    selected: list[CaseEvaluation] = []
    for evaluation in evaluations:
        if arm == "deterministic" and evaluation.deterministic_candidate:
            selected.append(evaluation)
        elif arm == "jev" and evaluation.jev_candidate is True:
            selected.append(evaluation)

    labels: list[HorizonLabel] = []
    latencies: list[float] = []
    for evaluation in selected:
        label = next(label for label in evaluation.labels if label.horizon_ms == horizon_ms)
        if label.available:
            labels.append(label)
        if evaluation.jev_model_latency_ms is not None:
            latencies.append(evaluation.jev_model_latency_ms)

    hits = sum(label.repriced_with_source is True for label in labels)
    surviving_edges = [
        label.edge_survived_bps
        for label in labels
        if label.edge_survived_bps is not None
    ]
    return ArmMetrics(
        candidates=len(selected),
        labeled=len(labels),
        repricing_hits=hits,
        precision=(hits / len(labels)) if labels else None,
        mean_surviving_edge_bps=(mean(surviving_edges) if surviving_edges else None),
        mean_model_latency_ms=(mean(latencies) if latencies else None),
    )


def build_report(
    evaluations: Iterable[CaseEvaluation],
    *,
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS,
    include_jev: bool = False,
) -> BenchmarkReport:
    rows = list(evaluations)
    deterministic = {
        horizon: _metrics_for_arm(rows, horizon_ms=horizon, arm="deterministic")
        for horizon in horizons_ms
    }
    jev = None
    if include_jev:
        jev = {
            horizon: _metrics_for_arm(rows, horizon_ms=horizon, arm="jev")
            for horizon in horizons_ms
        }
    return BenchmarkReport(deterministic=deterministic, jev=jev)
