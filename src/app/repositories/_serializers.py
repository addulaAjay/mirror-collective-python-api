"""Shared float ↔ Decimal helpers for DynamoDB.

DynamoDB rejects native ``float`` (boto raises ``TypeError: Float types are
not supported``) and returns numbers as ``Decimal`` on read. Every Reflection
Room repo passes data through these helpers so domain models can hold native
floats while persistence stays DDB-correct.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def to_ddb(value: Any) -> Any:
    """Recursively convert a Python value to a DynamoDB-safe shape.

    Floats become ``Decimal`` (string-converted to avoid binary-fraction drift),
    dicts and lists recurse, everything else passes through unchanged.
    """
    if isinstance(value, bool):
        # bool is a subclass of int; preserve as-is so it serializes to BOOL.
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_ddb(v) for v in value]
    if isinstance(value, tuple):
        return [to_ddb(v) for v in value]
    return value


def from_ddb(value: Any) -> Any:
    """Recursively convert a DynamoDB-shaped value back to native Python.

    ``Decimal`` becomes ``int`` if integral, else ``float``. Dicts and lists
    recurse, everything else passes through unchanged.
    """
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: from_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_ddb(v) for v in value]
    return value
