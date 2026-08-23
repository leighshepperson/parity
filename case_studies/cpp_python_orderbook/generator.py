"""Stateful order streams and the five retained migration regressions."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pyarrow as pa
from hypothesis import strategies as st

from parity import Invocation

Event = tuple[str, int, str, str, int, int]
Bundle = Invocation

EVENT_SCHEMA = pa.schema(
    [
        ("sequence", pa.int64()),
        ("event_type", pa.string()),
        ("order_id", pa.int64()),
        ("instrument", pa.string()),
        ("side", pa.string()),
        ("price_ticks", pa.int64()),
        ("quantity_lots", pa.int64()),
    ]
)
INSTRUMENT_SCHEMA = pa.schema(
    [
        ("instrument", pa.string()),
        ("lot_size", pa.int64()),
    ]
)

LOT_SIZE: tuple[Event, ...] = (
    ("NEW", 1, "BETA", "B", 95, 1),
    ("NEW", 2, "BETA", "S", 95, 1),
)
REFERENCE_REJECTS: tuple[Event, ...] = (
    ("NEW", 1, "ALPHA", "B", 95, 1),
    ("CANCEL", 1, "-", "-", 0, 0),
    ("NEW", 2, "ALPHA", "S", 95, 1),
    ("NEW", 3, "BETA", "B", 95, 1),
    ("NEW", 4, "ALPHA", "S", 95, 1),
    ("NEW", 5, "ALPHA", "B", 95, 2),
    ("CANCEL", 4, "-", "-", 0, 0),
)
PARTIAL_FILL: tuple[Event, ...] = (
    ("NEW", 1, "ALPHA", "B", 95, 1),
    ("CANCEL", 1, "-", "-", 0, 0),
    ("NEW", 2, "ALPHA", "B", 95, 1),
    ("NEW", 3, "ALPHA", "B", 95, 1),
    ("CANCEL", 2, "-", "-", 0, 0),
    ("CANCEL", 3, "-", "-", 0, 0),
    ("NEW", 4, "ALPHA", "B", 95, 1),
    ("NEW", 5, "BETA", "S", 95, 1),
    ("NEW", 6, "BETA", "S", 95, 1),
    ("NEW", 7, "BETA", "B", 95, 2),
)
CANDIDATE_REJECTS: tuple[Event, ...] = (
    ("NEW", 1, "ALPHA", "S", 96, 1),
    ("NEW", 2, "ALPHA", "B", 95, 1),
    ("CANCEL", 2, "-", "-", 0, 0),
    ("NEW", 3, "ALPHA", "B", 95, 1),
    ("NEW", 4, "ALPHA", "S", 95, 2),
    ("NEW", 5, "ALPHA", "B", 96, 1),
    ("CANCEL", 1, "-", "-", 0, 0),
)
PRICE_PRIORITY: tuple[Event, ...] = (
    ("NEW", 1, "BETA", "S", 96, 1),
    ("NEW", 2, "BETA", "S", 95, 1),
    ("NEW", 3, "BETA", "B", 96, 1),
)
REGRESSIONS = (
    LOT_SIZE,
    REFERENCE_REJECTS,
    PARTIAL_FILL,
    CANDIDATE_REJECTS,
    PRICE_PRIORITY,
)


def _tables(events: Sequence[Event]) -> Bundle:
    event_rows = [
        {
            "sequence": sequence,
            "event_type": event_type,
            "order_id": order_id,
            "instrument": instrument,
            "side": side,
            "price_ticks": price_ticks,
            "quantity_lots": quantity_lots,
        }
        for sequence, (
            event_type,
            order_id,
            instrument,
            side,
            price_ticks,
            quantity_lots,
        ) in enumerate(events, start=1)
    ]
    instruments = [
        {"instrument": "ALPHA", "lot_size": 1},
        {"instrument": "BETA", "lot_size": 10},
    ]
    return Invocation(
        kwargs={
            "events": pa.Table.from_pylist(event_rows, schema=EVENT_SCHEMA),
            "instruments": pa.Table.from_pylist(instruments, schema=INSTRUMENT_SCHEMA),
        }
    )


def _random_bundle(raw_events: Sequence[tuple[int, int, int, int, int, int]]) -> Bundle:
    events: list[Event] = []
    next_order_id = 1
    for action, instrument_index, side_index, price, quantity, cancel_selector in raw_events:
        if action == 0 and next_order_id > 1:
            events.append(("CANCEL", 1 + cancel_selector % (next_order_id - 1), "-", "-", 0, 0))
            continue
        events.append(
            (
                "NEW",
                next_order_id,
                ("ALPHA", "BETA")[instrument_index],
                ("B", "S")[side_index],
                price,
                quantity,
            )
        )
        next_order_id += 1
    return _tables(events)


def order_streams():
    """Return a shrinkable strategy spanning reviewed regressions and random streams."""

    event = st.tuples(
        st.integers(min_value=0, max_value=4),
        st.integers(min_value=0, max_value=1),
        st.integers(min_value=0, max_value=1),
        st.integers(min_value=95, max_value=105),
        st.integers(min_value=1, max_value=12),
        st.integers(min_value=0, max_value=63),
    )
    random_streams = st.lists(event, min_size=1, max_size=28).map(_random_bundle)
    reviewed_streams = st.sampled_from(REGRESSIONS).map(_tables)
    return st.one_of(reviewed_streams, random_streams)


def _one(events: Sequence[Event]) -> Iterable[Bundle]:
    return (_tables(events),)


def regression_lot_size() -> Iterable[Bundle]:
    return _one(LOT_SIZE)


def regression_reference_rejects() -> Iterable[Bundle]:
    return _one(REFERENCE_REJECTS)


def regression_partial_fill() -> Iterable[Bundle]:
    return _one(PARTIAL_FILL)


def regression_candidate_rejects() -> Iterable[Bundle]:
    return _one(CANDIDATE_REJECTS)


def regression_price_priority() -> Iterable[Bundle]:
    return _one(PRICE_PRIORITY)
