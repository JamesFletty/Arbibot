# Arbibot

Local-first research platform for measuring repricing lag between Binance BTCUSDT and Polymarket BTC UP/DOWN 5-minute markets.

The question is not whether the system can predict Bitcoin. The question is whether a spot move shows up late in the Polymarket book, and whether that lag still exists after spread, fees, slippage, latency, depth, and explicit no-trade gates. Live venue orders are not part of this tree. Default configs set `live_trading_enabled: false`.

## Status

Phases 0–15 in `docs/DEVELOPMENT_PLAN.md` are implemented: schemas, SQLite event store, ingestion adapters, candles, book builder, features, fair-price proxy, edge gates, paper execution, replay, risk, observability, a disabled live execution boundary, graph fusion, and TradingView/CryptoQuant adapters.

Phase 16 exists as a guarded mock pilot. It does not submit orders to Polymarket. Phase 17, passive maker orders, is not started.

## Setup

Python 3.11 or newer.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Quality gates, also run by `.github/workflows/ci.yml` and `scripts/run_tests.sh`:

```bash
ruff check .
mypy src
pytest
```

The optional Jev extra is only for the research benchmark arm:

```bash
pip install -e '.[jev]'
export TYPESAFE_API_KEY='...'
```

Deterministic tests and the offline demo do not need that key. Never commit `.env` or a filled-in copy of `.env.example`.

## CLI

The install creates an `arbibot` console script (`arbibot.apps.cli:main`).

### Validate a config

```bash
arbibot validate-config --config configs/paper.yaml
arbibot validate-config --config configs/default.yaml --json
```

### Local status

`status` evaluates the in-process health monitor from the config file. It does not open a market-data socket and it does not place orders.

```bash
arbibot status --config configs/paper.yaml
arbibot status --config configs/default.yaml --json
```

### Offline demo

`demo` writes a synthetic Binance/Polymarket session, replays it with paper execution, and runs the repricing benchmark. No live feed is contacted. An existing store is left untouched unless `--force` is set.

```bash
arbibot demo --store data/offline_demo.sqlite3 --config configs/paper.yaml
arbibot demo --store data/offline_demo.sqlite3 --config configs/paper.yaml --json --force
```

The JSON report includes `data_origin: synthetic`, decision counts, no-trade reasons, paper fills, and the synthetic first-reprice delay. On this fixture the impulse clears the paper gate once, and a later tick is refused with `STALE_BOOK`. Those outcomes are simulated on constructed quotes. They are not a venue order and they are not a market measurement. `health_can_submit_orders` uses the same zero-age local monitor as `status`; `live_trading_enabled` stays false.

### Replay and paper

`replay` reads a SQLite store. Adding `--evaluate-opportunities` requires a threshold, seconds to expiry, and an outcome side. `--paper-execute` requires opportunity evaluation. `paper` is that pair of flags.

```bash
arbibot replay --store data/offline_demo.sqlite3 --json

arbibot replay \
  --store data/offline_demo.sqlite3 \
  --evaluate-opportunities \
  --paper-execute \
  --threshold-price 100 \
  --seconds-to-expiry 300 \
  --target-size 1 \
  --outcome-side UP \
  --json

arbibot paper \
  --store data/offline_demo.sqlite3 \
  --threshold-price 100 \
  --seconds-to-expiry 300 \
  --outcome-side UP \
  --json
```

### Record public market data

These commands open read-only public websockets when they are not dry-run. They do not place orders. Recording without `--duration-seconds` or `--max-events` runs until interrupted.

```bash
arbibot record-binance \
  --store data/binance.sqlite3 \
  --symbol BTCUSDT \
  --streams aggTrade,trade \
  --duration-seconds 30 \
  --dry-run \
  --json

arbibot record-polymarket \
  --store data/polymarket.sqlite3 \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --outcome UP \
  --duration-seconds 30 \
  --dry-run \
  --json

arbibot record-session \
  --store data/events.sqlite3 \
  --symbol BTCUSDT \
  --binance-stream aggTrade \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --outcome UP \
  --duration-seconds 120 \
  --json
```

`--binance-stream` and `--token-id` may be repeated. SQLite files under `data/` are gitignored.

### Repricing benchmark

The benchmark reads a store already on disk. See `docs/REAL_DATA_REPRICING_BENCHMARK.md` for the real-data runbook. This repository does not ship a captured session, so there is no measured public-market result yet.

```bash
arbibot benchmark-repricing \
  --store data/events.sqlite3 \
  --symbol BTCUSDT \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --min-source-move-bps-100ms 5 \
  --json
```

Fair-probability mode adds `--market-expiry-ts-ms`, `--threshold-price`, and `--outcome-side`. `--jev` is optional and stays off unless the Jev extra and API key are configured.

## Layout

- `src/arbibot/core` — events, time, config, errors
- `src/arbibot/storage` — append-only SQLite event store
- `src/arbibot/ingestion` — Binance, Polymarket, TradingView, and CryptoQuant adapters
- `src/arbibot/market` — candles and local book
- `src/arbibot/features`, `model`, `opportunity` — features, fair-price proxy, edge, and gates
- `src/arbibot/execution` — paper engine and disabled live boundary
- `src/arbibot/replay` — deterministic replay
- `src/arbibot/risk`, `ops` — limits, kill switch, health, metrics, status
- `src/arbibot/research` — repricing bridge and benchmark; not on the decision hot path
- `src/arbibot/live` — guarded mock pilot
- `configs/` — paper and live-disabled YAML
- `docs/` — design, plan, tools, and the real-data benchmark runbook

## Safety

Paper mode is the default. A decision that cannot name its trade or no-trade reasons is not acceptable. Stale spot data, stale book data, wide spreads, thin depth, and the risk engine are explicit vetoes.

The live execution client raises while `live_trading_enabled` is false. Do not add venue order placement from the demo or the benchmark.

## Docs

- `docs/SYSTEM_DESIGN.md`
- `docs/DEVELOPMENT_PLAN.md`
- `docs/TOOLS_AND_SKILLS.md`
- `docs/JEV_REPRICING_BENCHMARK.md`
- `docs/REAL_DATA_REPRICING_BENCHMARK.md`
- `AGENTS.md`

## License

MIT. See `LICENSE`.
