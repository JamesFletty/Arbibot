from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean, median

from arbibot.research.jev_repricing import (
    DeterministicLagGate,
    JevGateResult,
    LagMeasurementGate,
    RepricingJudge,
    RepricingState,
)

DEFAULT_HORIZONS_MS = (50, 100, 250, 500)


@dataclass(frozen=True, slots=True)
class FutureQuote:
    offset_ms: float
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

    def first_reprice_delay_ms(self, min_reprice_bps: float = 1.0) -> float | None:
        if min_reprice_bps < 0:
            raise ValueError("min_reprice_bps must be non-negative")
        source_move = self.state.source_move_bps_100ms
        for quote in sorted(self.future_quotes, key=lambda q: q.offset_ms):
            move_bps = ((quote.mid - self.current_mid) / self.current_mid) * 10_000
            if abs(move_bps) >= min_reprice_bps and _same_direction(source_move, move_bps):
                return quote.offset_ms
        return None


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
    first_reprice_delay_ms: float | None
    labels: tuple[HorizonLabel, ...]


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    candidates: int
    latency_eligible_candidates: int
    latency_disqualified_candidates: int
    labeled: int
    repricing_hits: int
    precision: float | None
    mean_surviving_edge_bps: float | None
    mean_model_latency_ms: float | None


@dataclass(frozen=True, slots=True)
class ReactionDelayMetrics:
    candidates: int
    observed_reprices: int
    censored_no_reprice: int
    mean_delay_ms: float | None
    p50_delay_ms: float | None
    p90_delay_ms: float | None
    p95_delay_ms: float | None
    mean_model_latency_ms: float | None
    model_beats_reprice_count: int | None
    model_beats_reprice_rate: float | None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    deterministic: dict[int, ArmMetrics]
    jev: dict[int, ArmMetrics] | None
    deterministic_reaction: ReactionDelayMetrics
    jev_reaction: ReactionDelayMetrics | None


def _same_direction(source_move_bps: float, destination_move_bps: float) -> bool:
    return source_move_bps != 0 and source_move_bps * destination_move_bps > 0


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    if not 0 <= percentile <= 1:
        raise ValueError("percentile must be in [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = percentile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


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
        edge_survived = None
        if case.state.has_executable_edge_basis:
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
    deterministic_gate: LagMeasurementGate | DeterministicLagGate | None = None,
    jev_judge: RepricingJudge | None = None,
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS,
    min_reprice_bps: float = 1.0,
) -> list[CaseEvaluation]:
    gate = deterministic_gate or LagMeasurementGate()
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
                first_reprice_delay_ms=case.first_reprice_delay_ms(min_reprice_bps),
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
    candidates: list[CaseEvaluation] = []
    eligible: list[CaseEvaluation] = []
    latencies: list[float] = []

    for evaluation in evaluations:
        matches_deterministic = arm == "deterministic" and evaluation.deterministic_candidate
        matches_jev = arm == "jev" and evaluation.jev_candidate is True
        if not (matches_deterministic or matches_jev):
            continue
        candidates.append(evaluation)
        if evaluation.jev_model_latency_ms is not None:
            latencies.append(evaluation.jev_model_latency_ms)
        if arm == "jev":
            latency = evaluation.jev_model_latency_ms
            if latency is None or latency >= horizon_ms:
                continue
        eligible.append(evaluation)

    labels: list[HorizonLabel] = []
    for evaluation in eligible:
        label = next(label for label in evaluation.labels if label.horizon_ms == horizon_ms)
        if label.available:
            labels.append(label)

    hits = sum(label.repriced_with_source is True for label in labels)
    surviving_edges = [
        label.edge_survived_bps
        for label in labels
        if label.edge_survived_bps is not None
    ]
    return ArmMetrics(
        candidates=len(candidates),
        latency_eligible_candidates=len(eligible),
        latency_disqualified_candidates=len(candidates) - len(eligible),
        labeled=len(labels),
        repricing_hits=hits,
        precision=(hits / len(labels)) if labels else None,
        mean_surviving_edge_bps=(mean(surviving_edges) if surviving_edges else None),
        mean_model_latency_ms=(mean(latencies) if latencies else None),
    )


def _reaction_metrics(
    evaluations: Iterable[CaseEvaluation],
    *,
    arm: str,
) -> ReactionDelayMetrics:
    selected = [
        row
        for row in evaluations
        if (arm == "deterministic" and row.deterministic_candidate)
        or (arm == "jev" and row.jev_candidate is True)
    ]
    delays = [
        row.first_reprice_delay_ms
        for row in selected
        if row.first_reprice_delay_ms is not None
    ]
    latencies = [
        row.jev_model_latency_ms
        for row in selected
        if row.jev_model_latency_ms is not None
    ]

    beats: list[bool] = []
    if arm == "jev":
        for row in selected:
            if row.first_reprice_delay_ms is None or row.jev_model_latency_ms is None:
                continue
            beats.append(row.jev_model_latency_ms < row.first_reprice_delay_ms)

    return ReactionDelayMetrics(
        candidates=len(selected),
        observed_reprices=len(delays),
        censored_no_reprice=len(selected) - len(delays),
        mean_delay_ms=(mean(delays) if delays else None),
        p50_delay_ms=(median(delays) if delays else None),
        p90_delay_ms=_percentile(delays, 0.90),
        p95_delay_ms=_percentile(delays, 0.95),
        mean_model_latency_ms=(mean(latencies) if latencies else None),
        model_beats_reprice_count=(sum(beats) if arm == "jev" else None),
        model_beats_reprice_rate=(sum(beats) / len(beats) if beats else None),
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
    jev_reaction = None
    if include_jev:
        jev = {
            horizon: _metrics_for_arm(rows, horizon_ms=horizon, arm="jev")
            for horizon in horizons_ms
        }
        jev_reaction = _reaction_metrics(rows, arm="jev")
    return BenchmarkReport(
        deterministic=deterministic,
        jev=jev,
        deterministic_reaction=_reaction_metrics(rows, arm="deterministic"),
        jev_reaction=jev_reaction,
    )
