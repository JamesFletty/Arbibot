from __future__ import annotations

from arbibot.research.jev_repricing import (
    DeterministicLagGate,
    JevGateResult,
    LatencyBudget,
    RepricingState,
)


def _state(**updates: float | int) -> RepricingState:
    values: dict[str, float | int] = {
        "source_move_bps_100ms": 12.0,
        "source_move_bps_500ms": 18.0,
        "destination_move_bps_100ms": 2.0,
        "destination_best_bid": 0.52,
        "destination_best_ask": 0.53,
        "destination_depth_ask": 100.0,
        "destination_book_age_ms": 35,
        "source_event_age_ms": 8,
        "estimated_edge_bps": 30.0,
        "expected_cost_bps": 8.0,
        "time_to_expiry_ms": 120_000,
    }
    values.update(updates)
    return RepricingState(**values)  # type: ignore[arg-type]


def test_deterministic_gate_accepts_clean_lag_candidate() -> None:
    result = DeterministicLagGate().evaluate(_state())
    assert result.trade_candidate is True
    assert result.reasons == ()


def test_deterministic_gate_reports_all_failed_reasons() -> None:
    result = DeterministicLagGate().evaluate(
        _state(
            source_move_bps_100ms=1.0,
            estimated_edge_bps=9.0,
            expected_cost_bps=8.0,
            destination_book_age_ms=251,
            source_event_age_ms=101,
            destination_depth_ask=5.0,
            time_to_expiry_ms=14_999,
        )
    )
    assert result.trade_candidate is False
    assert result.reasons == (
        "SOURCE_MOVE_TOO_SMALL",
        "NET_EDGE_TOO_SMALL",
        "DESTINATION_BOOK_STALE",
        "SOURCE_EVENT_STALE",
        "INSUFFICIENT_ASK_DEPTH",
        "TOO_CLOSE_TO_EXPIRY",
    )


def test_latency_budget_requires_strict_headroom() -> None:
    budget = LatencyBudget(
        data_age_ms=8,
        feature_compute_ms=2,
        decision_ms=40,
        order_submit_ms=10,
        exchange_ack_ms=10,
    )
    assert budget.total_ms == 70
    assert budget.survives(100, reserve_ms=20) is True
    assert budget.survives(90, reserve_ms=20) is False


def test_jev_gate_thresholds_are_policy_not_model_output() -> None:
    strong = JevGateResult(
        genuine_lag_probability=0.90,
        adverse_selection_probability=0.10,
        executable_probability=0.85,
        model_latency_ms=80.0,
    )
    weak = JevGateResult(
        genuine_lag_probability=0.74,
        adverse_selection_probability=0.10,
        executable_probability=0.85,
        model_latency_ms=80.0,
    )
    assert strong.passes() is True
    assert weak.passes() is False
