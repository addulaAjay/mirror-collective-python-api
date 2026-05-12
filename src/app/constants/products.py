"""
Single source of truth for in-app purchase product SKUs.

Must match exactly:
  - App Store Connect product IDs
  - Google Play Console product IDs
  - Frontend catalog at MirrorCollectiveApp/src/constants/products.ts

Used by:
  - subscription_service receipt parsing — to recognise which product a
    transaction is for and map it to (kind, billing_period).
  - subscription_routes /verify-purchase — to whitelist incoming SKUs
    (defence in depth against a forged receipt that claims a product
    we don't actually sell).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict


class ProductKind(str, Enum):
    CORE = "core"
    STORAGE = "storage"


class BillingPeriod(str, Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


@dataclass(frozen=True)
class ProductDescriptor:
    key: str
    sku: str
    kind: ProductKind
    billing_period: BillingPeriod
    display_name: str


_CORE_MONTHLY = ProductDescriptor(
    key="CORE_MONTHLY",
    sku="com.themirrorcollective.mirror.core.monthly",
    kind=ProductKind.CORE,
    billing_period=BillingPeriod.MONTHLY,
    display_name="Mirror Core (Monthly)",
)
_CORE_YEARLY = ProductDescriptor(
    key="CORE_YEARLY",
    sku="com.themirrorcollective.mirror.core.yearly",
    kind=ProductKind.CORE,
    billing_period=BillingPeriod.YEARLY,
    display_name="Mirror Core (Yearly)",
)
_STORAGE_MONTHLY = ProductDescriptor(
    key="STORAGE_MONTHLY",
    sku="com.themirrorcollective.mirror.storage.monthly",
    kind=ProductKind.STORAGE,
    billing_period=BillingPeriod.MONTHLY,
    display_name="Echo Vault Storage (Monthly)",
)
_STORAGE_YEARLY = ProductDescriptor(
    key="STORAGE_YEARLY",
    sku="com.themirrorcollective.mirror.storage.yearly",
    kind=ProductKind.STORAGE,
    billing_period=BillingPeriod.YEARLY,
    display_name="Echo Vault Storage (Yearly)",
)

ALL_PRODUCTS: Dict[str, ProductDescriptor] = {
    p.sku: p for p in (_CORE_MONTHLY, _CORE_YEARLY, _STORAGE_MONTHLY, _STORAGE_YEARLY)
}

KNOWN_SKUS = frozenset(ALL_PRODUCTS.keys())


def is_known_sku(sku: str) -> bool:
    """Defence-in-depth: reject receipts claiming a product we don't sell."""
    return sku in KNOWN_SKUS


def descriptor_for_sku(sku: str) -> ProductDescriptor | None:
    return ALL_PRODUCTS.get(sku)
