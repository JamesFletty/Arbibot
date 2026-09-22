# Jev repricing-lag benchmark

This experiment tests one question: does a Jev judgment improve selection of short-lived BTC -> Polymarket repricing opportunities enough to pay for the latency of the model call?

It does **not** put Jev in the live execution path. Arbibot's deterministic rules, risk limits, order sizing, price limits, stale-data checks, and kill switches remain code-owned.

## Experimental arms

For every replayable candidate state, evaluate the same observation with:

1. **Deterministic baseline** — `DeterministicLagGate` only.
2. **Deterministic + Jev** — deterministic gate first, then one Jev request containing three independent Noul questions.

The Jev questions estimate:

- probability the destination quote is genuinely lagging the source move;
- probability the apparent edge is adverse selection / stale / transient;
- probability the edge remains executable given spread, depth, data ages, edge and expiry.

Thresholds remain deterministic policy in `JevGateResult.passes()`.

## Latency accounting

Measure all components rather than treating inference speed as the whole latency budget:

`data age + feature compute + decision + order submit + exchange ack`

`JevGateResult.model_latency_ms` is measured around the complete SDK call with `time.perf_counter_ns()`. A Jev-assisted candidate only has a speed edge if the resulting total remains strictly below the empirically observed repricing window with a safety reserve.

## Labels

For each candidate time `t0`, derive destination repricing labels from future persisted events at multiple horizons, initially:

- 50 ms
- 100 ms
- 250 ms
- 500 ms

The primary label should answer whether the destination best executable price moved in the source direction by a configured material amount before the horizon. Do not label from final market outcome; this experiment is about repricing arrival, not predicting BTC settlement.

## Metrics

Compare arms on:

- candidate precision by horizon;
- false-positive rate;
- median and tail decision latency (p50/p95/p99);
- executable edge remaining at simulated order-arrival time;
- fill probability / paper fills once order-book replay is available;
- net edge after spread, fees, slippage, queue uncertainty and latency;
- opportunity loss caused by Jev latency.

The important result is not Jev classification accuracy in isolation. The pass condition is improved **net executable edge** after charging the Jev arm for its measured latency.

## Running Jev research calls

Install the optional dependency:

```bash
pip install -e '.[jev]'
export TYPESAFE_API_KEY='...'
```

The integration uses TypeSafe's official Python SDK and defaults to `jev-latest`. As of September 22, 2026, the official package is `typesafe-sdk` and the SDK exposes `TypeSafeClient.system_one(...)` with typed `Noul` questions.

No key is required for deterministic unit tests.

## Next integration point

Once synchronized Binance + Polymarket events are present on the branch being used for experiments, add a replay researcher that materializes `RepricingState` at each source impulse and computes future-horizon labels from destination-book events. Keep that researcher outside `ReplayEngine` until the experiment shows an actual executable advantage.
