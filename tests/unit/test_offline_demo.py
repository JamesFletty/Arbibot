import json
from pathlib import Path

from arbibot.apps.cli import main
from arbibot.apps.offline_demo import (
    BENCHMARK_EXPIRY_TS_MS,
    IMPULSE_SOURCE_TS_MS,
    OfflineDemoError,
    build_flat_session,
    build_impulse_session,
    evaluate_store,
    insert_malformed_event,
    run_offline_demo,
    write_events,
)
from arbibot.opportunity.edge import OutcomeSide
from arbibot.research.event_store_bridge import BridgeConfig, extract_repricing_cases
from arbibot.storage.sqlite_store import SQLiteEventStore


def test_impulse_session_is_deterministic() -> None:
    first = [event.model_dump() for event in build_impulse_session()]
    second = [event.model_dump() for event in build_impulse_session()]
    assert first == second
    assert {event["event_id"] for event in first} == {
        "demo-spot-1000",
        "demo-spot-1500",
        "demo-book-1500",
        "demo-spot-1600",
        "demo-book-1950",
        "demo-spot-2000",
        "demo-book-2050",
        "demo-book-2100",
        "demo-book-2250",
        "demo-book-2500",
        "demo-spot-4000",
    }


def test_offline_demo_success_replays_and_measures_lag(tmp_path: Path) -> None:
    report = run_offline_demo(
        tmp_path / "demo.sqlite3",
        "configs/paper.yaml",
    )
    assert report.data_origin == "synthetic"
    assert report.config_valid is True
    assert report.live_trading_enabled is False
    assert report.total_events == 11
    assert report.malformed_events == 0
    assert report.decisions_total == 5
    assert report.cases_emitted == 1
    assert report.quotes_emitted >= 2
    assert report.decisions_trade == 1
    assert report.paper_fills == 1
    assert report.observed_reprices == 1
    assert report.p50_delay_ms is not None
    assert 49 < report.p50_delay_ms < 51
    assert report.stale_book_decisions >= 1
    assert report.fresh_book_decisions >= 1
    assert report.benchmark_mode == "fair_probability_edge"


def test_offline_demo_flags_stale_book_without_hiding_fresh_decisions(tmp_path: Path) -> None:
    report = run_offline_demo(tmp_path / "stale.sqlite3", "configs/default.yaml")
    assert report.stale_book_decisions >= 1
    assert "STALE_BOOK" in report.no_trade_reasons
    assert report.fresh_book_decisions >= 1
    assert report.stale_spot_decisions == 0


def test_malformed_event_is_counted_and_does_not_abort(tmp_path: Path) -> None:
    store = tmp_path / "malformed.sqlite3"
    write_events(store, build_impulse_session())
    insert_malformed_event(store)
    report = evaluate_store(store, config_path="configs/paper.yaml", scenario="malformed")
    assert report.malformed_events == 1
    assert report.bridge_malformed_events == 1
    assert report.cases_emitted == 1
    assert report.decisions_total == 5


def test_flat_session_is_a_zero_case_boundary(tmp_path: Path) -> None:
    store = tmp_path / "flat.sqlite3"
    write_events(store, build_flat_session())
    report = evaluate_store(store, config_path="configs/paper.yaml", scenario="flat")
    assert report.cases_emitted == 0
    assert report.paper_fills == 0
    assert report.observed_reprices == 0
    assert report.p50_delay_ms is None
    assert report.decisions_total > 0
    assert report.stale_book_decisions >= 1


def test_empty_store_boundary(tmp_path: Path) -> None:
    store = tmp_path / "empty.sqlite3"
    write_events(store, [])
    report = evaluate_store(store, config_path="configs/paper.yaml", scenario="empty")
    assert report.total_events == 0
    assert report.decisions_total == 0
    assert report.cases_emitted == 0
    assert report.malformed_events == 0
    assert report.quotes_emitted == 0


def test_expiry_boundary_sets_zero_time_to_expiry(tmp_path: Path) -> None:
    store = tmp_path / "expiry.sqlite3"
    write_events(store, build_impulse_session())
    db = SQLiteEventStore(store)
    try:
        result = extract_repricing_cases(
            db,
            BridgeConfig(
                token_id="token-up",
                min_source_move_bps_100ms=5.0,
                market_expiry_ts_ms=IMPULSE_SOURCE_TS_MS,
                threshold_price=100.0,
                outcome_side=OutcomeSide.UP,
            ),
        )
    finally:
        db.close()
    assert result.summary.cases_emitted == 1
    assert result.cases[0].state.time_to_expiry_ms == 0
    assert BENCHMARK_EXPIRY_TS_MS > IMPULSE_SOURCE_TS_MS


def test_existing_store_is_an_explicit_failure(tmp_path: Path) -> None:
    store = tmp_path / "exists.sqlite3"
    write_events(store, build_impulse_session())
    try:
        write_events(store, build_impulse_session())
    except OfflineDemoError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("expected existing store to fail")


def test_demo_cli_json_and_refuses_existing_store(tmp_path: Path, capsys) -> None:
    store = tmp_path / "cli.sqlite3"
    rc = main(
        [
            "demo",
            "--store",
            str(store),
            "--config",
            "configs/paper.yaml",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["data_origin"] == "synthetic"
    assert payload["live_trading_enabled"] is False
    assert payload["cases_emitted"] == 1

    rc_again = main(["demo", "--store", str(store), "--config", "configs/paper.yaml"])
    err = capsys.readouterr().out
    assert rc_again == 1
    assert "already exists" in err


def test_demo_cli_missing_config(tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "demo",
            "--store",
            str(tmp_path / "missing-config.sqlite3"),
            "--config",
            "configs/missing.yaml",
        ]
    )
    assert rc == 1
    assert "failed" in capsys.readouterr().out.lower()
