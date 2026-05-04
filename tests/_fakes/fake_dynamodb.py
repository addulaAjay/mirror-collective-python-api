"""In-memory shim of the slice of ``aioboto3`` the Reflection Room repos use.

Implements just enough surface to drive ``put_item``, ``get_item``, ``query``,
``update_item``, ``delete_item`` for unit testing repos without DynamoDB Local.

Does **not** replicate boto3 behavior in full — projection expressions, batch
ops, transactions, conditional writes, and consistent-read semantics are all
absent. Tests that need them should use DynamoDB Local instead.

Usage in a test::

    from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

    session = FakeAioSession({
        "mc_reflection_sessions-test": FakeTable(
            primary_key=["session_id"],
            indexes={"user_id-created_at-index": ["user_id", "created_at"]},
        ),
    })
    repo = ReflectionSessionRepo(session=session)
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Condition expression matcher — supports the subset boto's ConditionBase
# values from boto3.dynamodb.conditions emit.
# ---------------------------------------------------------------------------


def _condition_op_and_kvs(condition: Any) -> Tuple[str, List[Any]]:
    """Return (operator, [values]) for a boto ConditionBase.

    boto3.dynamodb.conditions.ConditionBase exposes ``.expression_operator``
    (e.g. ``"="``, ``"<="``, ``"AND"``) and stores operands on ``._values``
    (a tuple of Attr/Key objects + their argument). The leading underscore is
    boto3's choice; the layout has been stable for years.
    """
    op = getattr(condition, "expression_operator", None)
    values = list(getattr(condition, "_values", ()))
    return op, values


def _attr_name(operand: Any) -> Optional[str]:
    """Return the attribute name an Attr/Key operand refers to, if applicable."""
    return getattr(operand, "name", None)


def _eval_condition(condition: Any, item: Dict[str, Any]) -> bool:
    op, values = _condition_op_and_kvs(condition)
    if op is None:
        return True

    # Boolean composition.
    if op == "AND":
        return all(_eval_condition(v, item) for v in values)
    if op == "OR":
        return any(_eval_condition(v, item) for v in values)
    if op == "NOT":
        return not _eval_condition(values[0], item)

    # Comparator: values = [operand_attr, target]
    if not values:
        return False
    attr = _attr_name(values[0])
    if attr is None:
        return False
    target = values[1] if len(values) > 1 else None
    actual = item.get(attr)

    if op == "=":
        return actual == target
    if op == "<>":
        return actual != target
    if op == "<":
        return actual is not None and actual < target
    if op == "<=":
        return actual is not None and actual <= target
    if op == ">":
        return actual is not None and actual > target
    if op == ">=":
        return actual is not None and actual >= target
    if op == "BETWEEN":
        lo, hi = values[1], values[2]
        return actual is not None and lo <= actual <= hi
    if op == "begins_with":
        prefix = values[1]
        return isinstance(actual, str) and actual.startswith(prefix)

    raise NotImplementedError(f"FakeDynamoDB: unsupported operator {op}")


# ---------------------------------------------------------------------------
# Tables, resource, session
# ---------------------------------------------------------------------------


class FakeTable:
    """In-memory analogue of ``aioboto3.dynamodb.Table``.

    ``primary_key`` is the ordered list of key attribute names — 1 attr for a
    PK-only table, 2 for a PK+SK table. ``indexes`` is a mapping of GSI name
    to its key schema (same shape as the primary key list).
    """

    def __init__(
        self,
        primary_key: List[str],
        indexes: Optional[Dict[str, List[str]]] = None,
    ):
        if not (1 <= len(primary_key) <= 2):
            raise ValueError("primary_key must have 1 or 2 attributes")
        self.primary_key = list(primary_key)
        self.indexes: Dict[str, List[str]] = indexes or {}
        # Items keyed by tuple(primary_key_values).
        self._items: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    # -------------------- helpers --------------------

    def _key_tuple(self, item_or_key: Dict[str, Any]) -> Tuple[Any, ...]:
        try:
            return tuple(item_or_key[k] for k in self.primary_key)
        except KeyError as exc:
            raise KeyError(
                f"FakeTable: missing key attr {exc!s} (expected {self.primary_key})"
            ) from exc

    def _all_items(self) -> List[Dict[str, Any]]:
        return [copy.deepcopy(v) for v in self._items.values()]

    # -------------------- aioboto3-shaped methods --------------------

    async def put_item(self, *, Item: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        # DDB rejects native float; mirror that for parity with real boto.
        _reject_floats(Item)
        self._items[self._key_tuple(Item)] = copy.deepcopy(Item)
        return {}

    async def get_item(self, *, Key: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        item = self._items.get(self._key_tuple(Key))
        if item is None:
            return {}
        return {"Item": copy.deepcopy(item)}

    async def delete_item(self, *, Key: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        self._items.pop(self._key_tuple(Key), None)
        return {}

    async def query(
        self,
        *,
        KeyConditionExpression: Any,
        IndexName: Optional[str] = None,
        ScanIndexForward: bool = True,
        Limit: Optional[int] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        if IndexName is None:
            sort_attr = self.primary_key[1] if len(self.primary_key) > 1 else None
        else:
            schema = self.indexes.get(IndexName)
            if schema is None:
                raise KeyError(f"FakeTable: unknown index '{IndexName}'")
            sort_attr = schema[1] if len(schema) > 1 else None

        results = [
            item
            for item in self._all_items()
            if _eval_condition(KeyConditionExpression, item)
        ]
        if sort_attr is not None:
            results.sort(
                key=lambda i: i.get(sort_attr, ""), reverse=not ScanIndexForward
            )
        if Limit is not None:
            results = results[:Limit]
        return {"Items": results, "Count": len(results)}

    async def scan(self, **_: Any) -> Dict[str, Any]:
        items = self._all_items()
        return {"Items": items, "Count": len(items)}

    async def update_item(
        self,
        *,
        Key: Dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeValues: Optional[Dict[str, Any]] = None,
        ExpressionAttributeNames: Optional[Dict[str, str]] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        # Bare-bones SET handler: ``SET attr = :val[, ...]``.
        # Real DDB supports ADD / REMOVE / DELETE etc. — repos here only need SET.
        item = self._items.get(self._key_tuple(Key))
        if item is None:
            item = dict(Key)
        attr_names = ExpressionAttributeNames or {}
        attr_values = ExpressionAttributeValues or {}

        if not UpdateExpression.upper().startswith("SET "):
            raise NotImplementedError(
                "FakeDynamoDB.update_item supports only SET expressions"
            )
        body = UpdateExpression[4:]
        for clause in [c.strip() for c in body.split(",") if c.strip()]:
            name_part, value_part = (s.strip() for s in clause.split("=", 1))
            attr_name = attr_names.get(name_part, name_part)
            value = attr_values.get(value_part)
            item[attr_name] = value
        self._items[self._key_tuple(item)] = item
        return {"Attributes": copy.deepcopy(item)}


def _reject_floats(value: Any) -> None:
    """Walk a dict/list and raise if a native float is found.

    Mirrors boto3's behavior so tests catch missed Decimal conversions.
    """
    if isinstance(value, dict):
        for v in value.values():
            _reject_floats(v)
    elif isinstance(value, list):
        for v in value:
            _reject_floats(v)
    elif isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")


class _FakeDDBResource:
    def __init__(self, tables: Dict[str, FakeTable]):
        self._tables = tables

    async def Table(self, name: str) -> FakeTable:
        if name not in self._tables:
            raise KeyError(f"FakeDynamoDB: no such table '{name}'")
        return self._tables[name]


class _AsyncCM:
    def __init__(self, value: Any):
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeAioSession:
    """Drop-in shim for ``aioboto3.Session`` that returns a fake DDB resource."""

    def __init__(self, tables: Dict[str, FakeTable]):
        self._resource = _FakeDDBResource(tables)

    def resource(self, kind: str, **_: Any) -> _AsyncCM:
        if kind != "dynamodb":
            raise NotImplementedError(
                f"FakeAioSession only supports 'dynamodb', got {kind!r}"
            )
        return _AsyncCM(self._resource)
