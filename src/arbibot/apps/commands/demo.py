from __future__ import annotations

import json

from arbibot.apps.offline_demo import OfflineDemoError, run_offline_demo
from arbibot.core.errors import ConfigError


def run_demo(store_path: str, config_path: str, *, force: bool, as_json: bool) -> int:
    try:
        report = run_offline_demo(store_path, config_path, force=force)
    except (OfflineDemoError, ConfigError) as exc:
        print(f"Offline demo failed: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Offline demo runtime failed: {exc}")
        return 2

    payload = report.to_dict()
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    print(
        "Offline demo completed "
        f"origin={report.data_origin} "
        f"events={report.total_events} "
        f"decisions={report.decisions_total} "
        f"trade={report.decisions_trade} "
        f"no_trade={report.decisions_no_trade} "
        f"stale_book_decisions={report.stale_book_decisions} "
        f"malformed_events={report.malformed_events} "
        f"paper_fills={report.paper_fills} "
        f"cases={report.cases_emitted} "
        f"observed_reprices={report.observed_reprices} "
        f"p50_delay_ms={report.p50_delay_ms} "
        f"live_trading_enabled={report.live_trading_enabled}"
    )
    if report.no_trade_reasons:
        reasons = ", ".join(
            f"{reason}={count}" for reason, count in sorted(report.no_trade_reasons.items())
        )
        print(f"no-trade reasons: {reasons}")
    print(f"store={report.store_path}")
    return 0
