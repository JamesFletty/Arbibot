from __future__ import annotations

import json
from dataclasses import asdict

from arbibot.opportunity.edge import OutcomeSide
from arbibot.research.event_store_bridge import BridgeConfig, extract_repricing_cases
from arbibot.research.jev_repricing import DeterministicLagGate, JevRepricingGate
from arbibot.research.repricing_benchmark import build_report, evaluate_cases
from arbibot.storage.sqlite_store import SQLiteEventStore


def run_benchmark_repricing(
    *,
    store_path: str,
    symbol: str,
    token_id: str | None,
    market_expiry_ts_ms: int | None,
    threshold_price: float | None,
    outcome_side: str | None,
    fee_cost_bps: float,
    extra_cost_bps: float,
    min_source_move_bps_100ms: float,
    use_jev: bool,
    jev_model: str,
    as_json: bool,
) -> int:
    try:
        parsed_outcome = OutcomeSide(outcome_side) if outcome_side is not None else None
        config = BridgeConfig(
            symbol=symbol,
            token_id=token_id,
            market_expiry_ts_ms=market_expiry_ts_ms,
            threshold_price=threshold_price,
            outcome_side=parsed_outcome,
            fee_cost_bps=fee_cost_bps,
            extra_cost_bps=extra_cost_bps,
            min_source_move_bps_100ms=min_source_move_bps_100ms,
        )
        if use_jev and not config.has_fair_edge_inputs:
            raise ValueError(
                "--jev requires --market-expiry-ts-ms, --threshold-price, and --outcome-side"
            )
        store = SQLiteEventStore(store_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Benchmark setup failed: {exc}")
        return 1

    try:
        bridge = extract_repricing_cases(store, config)
        judge = JevRepricingGate(model=jev_model) if use_jev else None
        gate = DeterministicLagGate() if config.has_fair_edge_inputs else None
        evaluations = evaluate_cases(
            bridge.cases,
            deterministic_gate=gate,
            jev_judge=judge,
        )
        report = build_report(evaluations, include_jev=use_jev)
    except Exception as exc:  # noqa: BLE001
        print(f"Benchmark runtime failed: {exc}")
        return 2
    finally:
        store.close()

    payload = {
        "mode": "fair_probability_edge" if config.has_fair_edge_inputs else "lag_measurement",
        "bridge": asdict(bridge.summary),
        "report": asdict(report),
    }
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "Repricing benchmark completed "
            f"mode={payload['mode']} "
            f"cases={bridge.summary.cases_emitted} "
            f"quotes={bridge.summary.quotes_emitted} "
            f"jev={'on' if use_jev else 'off'}"
        )
        for horizon, metrics in report.deterministic.items():
            print(
                f"deterministic horizon={horizon}ms candidates={metrics.candidates} "
                f"precision={metrics.precision} "
                f"surviving_edge_bps={metrics.mean_surviving_edge_bps}"
            )
        if report.jev is not None:
            for horizon, metrics in report.jev.items():
                print(
                    f"jev horizon={horizon}ms candidates={metrics.candidates} "
                    f"eligible={metrics.latency_eligible_candidates} "
                    f"latency_disqualified={metrics.latency_disqualified_candidates} "
                    f"precision={metrics.precision} "
                    f"model_latency_ms={metrics.mean_model_latency_ms}"
                )
    return 0
