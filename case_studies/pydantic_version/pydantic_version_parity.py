"""Shared behavioural contract for the real Pydantic 1-to-2 campaign."""

from __future__ import annotations

from typing import Optional

import pydantic


def stable(frame):
    """A control whose explicit integer contract is stable in both releases."""

    class StableModel(pydantic.BaseModel):
        amount: int

    value = int(frame.column("value")[0].as_py())
    return {"amount": StableModel(amount=value).amount}


def migration_surface(frame):
    """Exercise independent historical behaviours selected by canonical input."""

    operation = str(frame.column("operation")[0].as_py())
    value = float(frame.column("value")[0].as_py())

    if operation == "integer-to-string":

        class TextModel(pydantic.BaseModel):
            text: str

        return {"operation": operation, "value": TextModel(text=int(value)).text}

    if operation == "fractional-to-integer":

        class IntegerModel(pydantic.BaseModel):
            number: int

        return {"operation": operation, "value": IntegerModel(number=value).number}

    if operation == "optional-default":

        class OptionalModel(pydantic.BaseModel):
            number: Optional[int]  # noqa: UP045 - this is the historical v1 contract

        return {"operation": operation, "value": OptionalModel().number}

    if operation == "model-dict-equality":

        class EqualityModel(pydantic.BaseModel):
            number: int

        model = EqualityModel(number=int(value))
        return {"operation": operation, "value": model == {"number": int(value)}}

    raise AssertionError("generator produced an unsupported operation")
