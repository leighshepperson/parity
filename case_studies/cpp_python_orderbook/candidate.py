"""Correct and deliberately flawed Python ports of the order-book contract."""

from __future__ import annotations

import pyarrow as pa

OUTPUT_SCHEMA = pa.schema(
    [
        ("trade_sequence", pa.int64()),
        ("instrument", pa.string()),
        ("maker_order_id", pa.int64()),
        ("taker_order_id", pa.int64()),
        ("buyer_order_id", pa.int64()),
        ("seller_order_id", pa.int64()),
        ("price_ticks", pa.int64()),
        ("quantity_lots", pa.int64()),
        ("quantity_units", pa.int64()),
    ]
)


class InvalidCancel(Exception):
    """The contract rejects cancellation of an order that is no longer resting."""


InvalidCancel.__module__ = "legacy.exchange"


def _crosses(resting: dict[str, object], side: str, price_ticks: int) -> bool:
    if side == "B":
        return resting["side"] == "S" and int(resting["price_ticks"]) <= price_ticks
    return resting["side"] == "B" and int(resting["price_ticks"]) >= price_ticks


def _match(events: pa.Table, instruments: pa.Table, *, naive: bool) -> pa.Table:
    lot_sizes = {str(row["instrument"]): int(row["lot_size"]) for row in instruments.to_pylist()}
    book: list[dict[str, object]] = []
    resting_by_id: dict[int, dict[str, object]] = {}
    fills: list[dict[str, object]] = []

    for event in events.to_pylist():
        order_id = int(event["order_id"])
        if event["event_type"] == "CANCEL":
            resting = resting_by_id.get(order_id)
            if resting is None or not bool(resting["active"]):
                raise InvalidCancel("cannot cancel an inactive order")
            resting["active"] = False
            continue

        instrument = str(event["instrument"])
        side = str(event["side"])
        price_ticks = int(event["price_ticks"])
        incoming: dict[str, object] = {
            "order_id": order_id,
            "instrument": instrument,
            "side": side,
            "price_ticks": price_ticks,
            "remaining_lots": int(event["quantity_lots"]),
            "sequence": int(event["sequence"]),
            "active": True,
        }

        while int(incoming["remaining_lots"]) > 0:
            makers = [
                resting
                for resting in book
                if bool(resting["active"])
                and resting["instrument"] == instrument
                and _crosses(resting, side, price_ticks)
            ]
            if not makers:
                break
            if naive:
                maker = min(makers, key=lambda row: int(row["sequence"]))
            elif side == "B":
                maker = min(
                    makers,
                    key=lambda row: (int(row["price_ticks"]), int(row["sequence"])),
                )
            else:
                maker = min(
                    makers,
                    key=lambda row: (-int(row["price_ticks"]), int(row["sequence"])),
                )

            matched_lots = min(
                int(incoming["remaining_lots"]),
                int(maker["remaining_lots"]),
            )
            buyer_id = order_id if side == "B" else int(maker["order_id"])
            seller_id = order_id if side == "S" else int(maker["order_id"])
            fills.append(
                {
                    "trade_sequence": int(event["sequence"]),
                    "instrument": instrument,
                    "maker_order_id": int(maker["order_id"]),
                    "taker_order_id": order_id,
                    "buyer_order_id": buyer_id,
                    "seller_order_id": seller_id,
                    "price_ticks": int(maker["price_ticks"]),
                    "quantity_lots": matched_lots,
                    "quantity_units": (
                        matched_lots if naive else matched_lots * lot_sizes[instrument]
                    ),
                }
            )
            incoming["remaining_lots"] = int(incoming["remaining_lots"]) - matched_lots
            maker["remaining_lots"] = int(maker["remaining_lots"]) - matched_lots
            if int(maker["remaining_lots"]) == 0:
                maker["active"] = False
            if naive:
                break

        if int(incoming["remaining_lots"]) > 0:
            book.append(incoming)
            resting_by_id[order_id] = incoming

    return pa.Table.from_pylist(fills, schema=OUTPUT_SCHEMA)


def correct_port(events: pa.Table, instruments: pa.Table) -> pa.Table:
    """Preserve price-time priority, residual fills and instrument lot sizes."""

    return _match(events, instruments, naive=False)


def naive_port(events: pa.Table, instruments: pa.Table) -> pa.Table:
    """Inject three plausible rewrite defects for Parity to discover."""

    return _match(events, instruments, naive=True)
