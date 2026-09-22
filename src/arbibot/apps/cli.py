from __future__ import annotations

import argparse

from arbibot.apps.commands import (
    run_benchmark_repricing,
    run_paper,
    run_record_binance,
    run_record_polymarket,
    run_replay,
    run_status,
    run_validate_config,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arbibot")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate-config")
    v.add_argument("--config", required=True)
    v.add_argument("--json", action="store_true")

    s = sub.add_parser("status")
    s.add_argument("--config", required=True)
    s.add_argument("--json", action="store_true")

    r = sub.add_parser("replay")
    r.add_argument("--store", required=True)
    r.add_argument("--config")
    r.add_argument("--evaluate-opportunities", action="store_true")
    r.add_argument("--paper-execute", action="store_true")
    r.add_argument("--threshold-price")
    r.add_argument("--seconds-to-expiry")
    r.add_argument("--target-size", default="1")
    r.add_argument("--outcome-side", choices=["UP", "DOWN"])
    r.add_argument("--json", action="store_true")

    pa = sub.add_parser("paper")
    pa.add_argument("--store", required=True)
    pa.add_argument("--config")
    pa.add_argument("--threshold-price", required=False)
    pa.add_argument("--seconds-to-expiry", required=False)
    pa.add_argument("--target-size", default="1")
    pa.add_argument("--outcome-side", choices=["UP", "DOWN"], required=False)
    pa.add_argument("--json", action="store_true")

    rb = sub.add_parser("record-binance")
    rb.add_argument("--store", default="data/events.sqlite3")
    rb.add_argument("--symbol", default="BTCUSDT")
    rb.add_argument("--streams", default="aggTrade,trade")
    rb.add_argument("--duration-seconds", type=int)
    rb.add_argument("--max-events", type=int)
    rb.add_argument("--config")
    rb.add_argument("--json", action="store_true")
    rb.add_argument("--dry-run", action="store_true")

    rp = sub.add_parser("record-polymarket")
    rp.add_argument("--store", default="data/events.sqlite3")
    rp.add_argument("--token-id", action="append", required=True)
    rp.add_argument("--outcome", choices=["UP", "DOWN"])
    rp.add_argument("--duration-seconds", type=int)
    rp.add_argument("--max-events", type=int)
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--dry-run", action="store_true")

    br = sub.add_parser("benchmark-repricing")
    br.add_argument("--store", required=True)
    br.add_argument("--symbol", default="BTCUSDT")
    br.add_argument("--token-id")
    br.add_argument("--market-expiry-ts-ms", type=int)
    br.add_argument("--fee-cost-bps", type=float, default=0.0)
    br.add_argument("--extra-cost-bps", type=float, default=0.0)
    br.add_argument("--min-source-move-bps-100ms", type=float, default=5.0)
    br.add_argument("--jev", action="store_true")
    br.add_argument("--jev-model", default="jev-latest")
    br.add_argument("--json", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-config":
        return run_validate_config(args.config, args.json)
    if args.command == "status":
        return run_status(args.config, args.json)
    if args.command == "replay":
        return run_replay(
            store_path=args.store,
            evaluate_opportunities=args.evaluate_opportunities,
            paper_execute=args.paper_execute,
            threshold_price=args.threshold_price,
            seconds_to_expiry=args.seconds_to_expiry,
            target_size=args.target_size,
            outcome_side=args.outcome_side,
            as_json=args.json,
        )
    if args.command == "paper":
        return run_paper(
            store_path=args.store,
            threshold_price=args.threshold_price,
            seconds_to_expiry=args.seconds_to_expiry,
            target_size=args.target_size,
            outcome_side=args.outcome_side,
            as_json=args.json,
        )
    if args.command == "record-binance":
        return run_record_binance(
            store_path=args.store,
            symbol=args.symbol,
            streams_csv=args.streams,
            duration_seconds=args.duration_seconds,
            max_events=args.max_events,
            as_json=args.json,
            dry_run=args.dry_run,
            config_path=args.config,
        )
    if args.command == "record-polymarket":
        return run_record_polymarket(
            store_path=args.store,
            token_ids=args.token_id,
            outcome=args.outcome,
            duration_seconds=args.duration_seconds,
            max_events=args.max_events,
            as_json=args.json,
            dry_run=args.dry_run,
        )
    if args.command == "benchmark-repricing":
        return run_benchmark_repricing(
            store_path=args.store,
            symbol=args.symbol,
            token_id=args.token_id,
            market_expiry_ts_ms=args.market_expiry_ts_ms,
            fee_cost_bps=args.fee_cost_bps,
            extra_cost_bps=args.extra_cost_bps,
            min_source_move_bps_100ms=args.min_source_move_bps_100ms,
            use_jev=args.jev,
            jev_model=args.jev_model,
            as_json=args.json,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
