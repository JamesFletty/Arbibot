from arbibot.research.jev_repricing import JevGateResult, RepricingState
from arbibot.research.repricing_benchmark import (
    FutureQuote,
    RepricingCase,
    build_report,
    evaluate_cases,
    label_case,
)


def _state() -> RepricingState:
    return RepricingState(
        source_move_bps_100ms=12.0,
        source_move_bps_500ms=20.0,
        destination_move_bps_100ms=0.0,
        destination_best_bid=0.49,
        destination_best_ask=0.50,
        destination_depth_ask=100.0,
        destination_book_age_ms=20,
        source_event_age_ms=10,
        estimated_edge_bps=30.0,
        expected_cost_bps=5.0,
        time_to_expiry_ms=120_000,
    )


def _case() -> RepricingCase:
    return RepricingCase(
        case_id="c1",
        state=_state(),
        current_mid=0.495,
        future_quotes=(
            FutureQuote(offset_ms=20, best_bid=0.49, best_ask=0.50),
            FutureQuote(offset_ms=37, best_bid=0.4905, best_ask=0.5005),
            FutureQuote(offset_ms=50, best_bid=0.491, best_ask=0.501),
            FutureQuote(offset_ms=100, best_bid=0.492, best_ask=0.502),
            FutureQuote(offset_ms=250, best_bid=0.493, best_ask=0.503),
            FutureQuote(offset_ms=500, best_bid=0.494, best_ask=0.504),
        ),
    )


def test_label_case_marks_same_direction_repricing() -> None:
    labels = label_case(_case(), horizons_ms=(50, 100))
    assert len(labels) == 2
    assert labels[0].available is True
    assert labels[0].repriced_with_source is True
    assert labels[0].destination_move_bps is not None
    assert labels[0].destination_move_bps > 0


def test_label_case_marks_unavailable_horizon() -> None:
    labels = label_case(_case(), horizons_ms=(1000,))
    assert labels[0].available is False
    assert labels[0].repriced_with_source is None


def test_first_reprice_delay_uses_first_qualifying_quote() -> None:
    assert _case().first_reprice_delay_ms() == 37


class _PassingJudge:
    def judge(self, state: RepricingState) -> JevGateResult:
        assert state.net_edge_bps == 25.0
        return JevGateResult(
            genuine_lag_probability=0.9,
            adverse_selection_probability=0.1,
            executable_probability=0.9,
            model_latency_ms=17.0,
        )


class _SlowPassingJudge:
    def judge(self, state: RepricingState) -> JevGateResult:
        return JevGateResult(
            genuine_lag_probability=0.9,
            adverse_selection_probability=0.1,
            executable_probability=0.9,
            model_latency_ms=125.0,
        )


def test_evaluate_cases_runs_jev_only_after_deterministic_gate() -> None:
    evaluations = evaluate_cases([_case()], jev_judge=_PassingJudge(), horizons_ms=(100,))
    assert evaluations[0].deterministic_candidate is True
    assert evaluations[0].jev_candidate is True
    assert evaluations[0].jev_model_latency_ms == 17.0
    assert evaluations[0].first_reprice_delay_ms == 37


def test_build_report_compares_deterministic_and_jev_arms() -> None:
    evaluations = evaluate_cases([_case()], jev_judge=_PassingJudge(), horizons_ms=(100,))
    report = build_report(evaluations, horizons_ms=(100,), include_jev=True)
    assert report.deterministic[100].candidates == 1
    assert report.deterministic[100].repricing_hits == 1
    assert report.deterministic_reaction.observed_reprices == 1
    assert report.deterministic_reaction.p50_delay_ms == 37
    assert report.jev is not None
    assert report.jev[100].candidates == 1
    assert report.jev[100].latency_eligible_candidates == 1
    assert report.jev[100].latency_disqualified_candidates == 0
    assert report.jev[100].mean_model_latency_ms == 17.0
    assert report.jev_reaction is not None
    assert report.jev_reaction.model_beats_reprice_count == 1
    assert report.jev_reaction.model_beats_reprice_rate == 1.0


def test_jev_candidate_is_disqualified_when_model_is_slower_than_horizon() -> None:
    evaluations = evaluate_cases([_case()], jev_judge=_SlowPassingJudge(), horizons_ms=(100,))
    report = build_report(evaluations, horizons_ms=(100,), include_jev=True)
    assert report.jev is not None
    assert report.jev[100].candidates == 1
    assert report.jev[100].latency_eligible_candidates == 0
    assert report.jev[100].latency_disqualified_candidates == 1
    assert report.jev[100].labeled == 0
    assert report.jev[100].precision is None
    assert report.jev_reaction is not None
    assert report.jev_reaction.model_beats_reprice_count == 0
    assert report.jev_reaction.model_beats_reprice_rate == 0.0
