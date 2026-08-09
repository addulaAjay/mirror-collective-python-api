"""DynamoDB writes must convert Python floats to Decimal.

Regression: put_item stored items verbatim, so a subscription's float price_usd
(9.99) hit boto3 as a float -> "Float types are not supported. Use Decimal types
instead." — a valid, verified purchase then failed to persist.
"""

from decimal import Decimal

from src.app.services.dynamodb_service import _floats_to_decimal


def test_scalar_float_becomes_decimal():
    out = _floats_to_decimal(9.99)
    assert isinstance(out, Decimal) and out == Decimal("9.99")


def test_nested_structures_and_types_preserved():
    out = _floats_to_decimal(
        {
            "price_usd": 9.99,
            "count": 5,
            "name": "x",
            "flag": True,
            "none": None,
            "nested": {"quota_gb": 50.0},
            "list": [1.5, 2, "s"],
        }
    )
    assert isinstance(out["price_usd"], Decimal) and out["price_usd"] == Decimal("9.99")
    assert out["count"] == 5 and isinstance(out["count"], int)
    assert out["name"] == "x"
    assert out["flag"] is True  # bool is not float — must be untouched
    assert out["none"] is None
    assert isinstance(out["nested"]["quota_gb"], Decimal)
    assert isinstance(out["list"][0], Decimal) and out["list"][0] == Decimal("1.5")
    assert out["list"][1] == 2 and out["list"][2] == "s"
