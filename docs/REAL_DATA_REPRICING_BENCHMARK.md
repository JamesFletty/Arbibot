# Real-data repricing benchmark

This is the runbook for measuring Binance BTCUSDT versus Polymarket CLOB repricing lag on a persisted session. It is not a result. This repository does not currently contain a captured public session or a measured real-market delay distribution.

## Why this change does not include a measured result

`benchmark-repricing` reads a local SQLite event store. It does not download market data. A real-data result needs one synchronized store that already contains Binance spot ticks and Polymarket book snapshots or deltas for the same interval.

Producing that store means opening the public market-data sockets already coded in the adapters:

- Binance combined stream: `wss://stream.binance.com:9443/stream`
- Polymarket public market channel: `wss://ws-subscriptions-clob.polymarket.com/ws/market`

Those sockets are read-only market data. They do not place orders. Recording still needs the operator to supply the active BTC UP/DOWN 5-minute token id. This repository has no market-discovery client, and a token id is not stable across 5-minute windows. Inventing a discovery endpoint is out of scope, so a live capture was not run here.

Until a store is recorded locally, use the offline demo for a synthetic walkthrough:

```bash
arbibot demo --store data/offline_demo.sqlite3 --config configs/paper.yaml
```

That demo writes no network traffic. Its delay number is synthetic.

## Record a public read-only session

Install the package, keep live trading disabled, and pass a token id you have already identified for the current 5-minute market.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

arbibot record-session \
  --store data/events.sqlite3 \
  --symbol BTCUSDT \
  --binance-stream aggTrade \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --outcome UP \
  --duration-seconds 120 \
  --json
```

`--dry-run` validates the arguments and writes nothing. It does not open a socket.

```bash
arbibot record-session \
  --store data/events.sqlite3 \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --outcome UP \
  --dry-run \
  --json
```

Separate recorders exist when only one venue is needed. A Binance-only store cannot emit repricing cases, because the bridge requires both spot history and a destination book.

```bash
arbibot record-binance \
  --store data/binance.sqlite3 \
  --symbol BTCUSDT \
  --streams aggTrade,trade \
  --duration-seconds 30 \
  --dry-run

arbibot record-polymarket \
  --store data/polymarket.sqlite3 \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --outcome UP \
  --duration-seconds 30 \
  --dry-run
```

Do not commit `*.sqlite3` files. They are gitignored.

## Run the benchmark

Lag measurement does not claim an executable edge. Omit the fair-value inputs for that mode.

```bash
arbibot benchmark-repricing \
  --store data/events.sqlite3 \
  --symbol BTCUSDT \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --min-source-move-bps-100ms 5 \
  --json
```

Fair-probability edge mode needs an expiry timestamp in milliseconds, the market threshold, and the outcome side. Fee and extra cost are absolute probability basis points, the same units as `(fair_probability - executable_ask) * 10000`.

```bash
arbibot benchmark-repricing \
  --store data/events.sqlite3 \
  --symbol BTCUSDT \
  --token-id "$POLYMARKET_TOKEN_ID" \
  --market-expiry-ts-ms "$MARKET_EXPIRY_TS_MS" \
  --threshold-price "$THRESHOLD_PRICE" \
  --outcome-side UP \
  --fee-cost-bps 0 \
  --extra-cost-bps 0 \
  --min-source-move-bps-100ms 5 \
  --json
```

`--jev` adds the optional TypeSafe call and requires the fair-value inputs plus `pip install -e '.[jev]'` and `TYPESAFE_API_KEY`. Leave it off for a deterministic local report.

The text report prints:

- `mode`, either `lag_measurement` or `fair_probability_edge`
- cases and quotes emitted by the event-store bridge
- observed versus censored first-reprice delays, with p50, p90, and p95
- per-horizon candidate count, precision, and surviving edge

Cross-venue order uses each event's local monotonic receive time. Exchange source timestamps are stored and are not treated as a shared clock.

## What a later result write-up needs

A measured write-up should attach the command, the session start and end, the token id and expiry, the bridge summary, and the reaction-delay lines above. It should also say whether the run was lag-only or fair-edge, and it should not treat a synthetic `arbibot demo` delay as market evidence.
