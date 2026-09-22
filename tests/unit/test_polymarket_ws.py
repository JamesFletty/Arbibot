from arbibot.core.events import MarketSide, PolyBookDelta, PolyBookSnapshot
from arbibot.ingestion.polymarket_ws import parse_polymarket_payload


def test_parse_book_snapshot() -> None:
    events = parse_polymarket_payload(
        {
            "event_type": "book",
            "asset_id": "token-up",
            "market": "condition-1",
            "timestamp": "1000",
            "hash": "abc",
            "bids": [{"price": "0.49", "size": "12"}],
            "asks": [{"price": "0.51", "size": "10"}],
        },
        recv_wall_ts_ms=1005,
        recv_monotonic_ns=1_000_000,
        outcome_by_token={"token-up": "UP"},
    )
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, PolyBookSnapshot)
    assert event.token_id == "token-up"
    assert event.outcome == "UP"
    assert event.bids[0].price == 0.49
    assert event.asks[0].size == 10.0


def test_parse_price_change_expands_entries() -> None:
    events = parse_polymarket_payload(
        {
            "event_type": "price_change",
            "market": "condition-1",
            "timestamp": "1100",
            "price_changes": [
                {
                    "asset_id": "token-up",
                    "price": "0.50",
                    "size": "20",
                    "side": "BUY",
                    "hash": "h1",
                },
                {
                    "asset_id": "token-up",
                    "price": "0.52",
                    "size": "0",
                    "side": "SELL",
                    "hash": "h2",
                },
            ],
        },
        recv_wall_ts_ms=1105,
        recv_monotonic_ns=1_100_000,
    )
    assert len(events) == 2
    first, second = events
    assert isinstance(first, PolyBookDelta)
    assert first.side is MarketSide.BUY
    assert second.side is MarketSide.SELL
    assert second.size == 0.0


def test_unsupported_market_event_is_ignored() -> None:
    events = parse_polymarket_payload(
        {
            "event_type": "last_trade_price",
            "timestamp": "1200",
        },
        recv_wall_ts_ms=1205,
        recv_monotonic_ns=1_200_000,
    )
    assert events == []
