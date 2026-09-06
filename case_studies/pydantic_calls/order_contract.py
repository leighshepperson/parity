"""A small order-validation boundary run unchanged against Pydantic 1 and 2."""

from pydantic import BaseModel


class Order(BaseModel):
    reference: int | str
    label: str
    quantity: int
    note: str | None


def validate_order(payload):
    order = Order(**payload)
    return order.model_dump() if hasattr(order, "model_dump") else order.dict()
