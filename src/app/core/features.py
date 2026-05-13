"""Feature-flag catalog (pricing spec 2026-05-12 §6, §12).

Single source of truth for "which subscription tier unlocks which
feature". Routes opt into a feature gate via `require_feature(...)`
from `core.entitlement`; the gate composes a status check (is the
subscription currently live?) with a tier→feature lookup against the
map below.

Design intent (spec §12):
  - Launch ships with BASIC_ACCESS only.
  - Future Plus-tier features are pre-declared HERE — not retrofitted
    into routes at Plus launch. Adding `Feature.X` reservation today
    lets the route gate land as `Depends(require_feature(Feature.X))`
    on day one; flipping `X` from {} to {"plus"} below is a
    single-line spec update when the tier ships.
  - Storage add-on is NOT a feature flag. It's a quota number set on
    the user profile (`storage_add_on_active` + the +100 GB math in
    StorageQuotaService). Mixing it into this enum would re-couple
    the two axes the previous refactor just decoupled.

The set in FEATURE_TIER_MAP is the list of `subscription_tier` values
that grant the feature. `"trial"` appears alongside `"basic"` for
BASIC_ACCESS so a user mid-trial keeps full Basic access until their
status flips to `trial_expired` (the status check in
`require_feature` handles that transition independently).
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet


class Feature(str, Enum):
    """Every gated capability in the product.

    Values are the wire strings used in 402 detail payloads (so the
    client can route to the right paywall by name) and in any future
    analytics tagging. The string-Enum keeps the type expressive while
    serialising cleanly via FastAPI.
    """

    # ----- Live at launch -----
    BASIC_ACCESS = "basic_access"
    """Mirror Basic: MirrorGPT, Echo Vault (50 GB), Echo Map.

    Granted by tier ∈ {trial, basic} and (future) plus. Most existing
    routes gate on this.
    """

    # ----- Reserved for future Plus tier (V2 / V3 / V4 per spec §3) -----
    PLUS_ACCESS = "plus_access"
    """Umbrella for the Plus tier itself — gate on this when you want
    to ask 'is this user on Plus' without naming a specific feature."""

    REFLECTION_ROOM_ACCESS = "reflection_room_access"
    """Reflection Room (V2)."""

    ECHO_SIGNATURE_ACCESS = "echo_signature_access"
    """Echo Signature (V2)."""

    ECHO_MAP_ACCESS = "echo_map_access"
    """Echo Map — per spec §10 this will move to Plus when V2 ships.
    Today the Echo Map routes are gated on BASIC_ACCESS; flipping this
    feature's tier set to {plus} and re-pointing those routes is the
    migration when the time comes."""

    MIRROR_MOMENT_ACCESS = "mirror_moment_access"
    """Mirror Moment (V2)."""

    MIRROR_PLEDGE_ACCESS = "mirror_pledge_access"
    """Mirror Pledge (V2)."""

    CODE_LIBRARY_ACCESS = "code_library_access"
    """Code Library (V3)."""

    MEMORY_TIMELINE_ACCESS = "memory_timeline_access"
    """Memory Timeline (V4)."""

    ROLE_PATTERN_TIMELINE_ACCESS = "role_pattern_timeline_access"
    """Role & Pattern Timeline (V3)."""


# Subscription tier values that grant each feature. Keep in sync with
# UserProfile.subscription_tier — currently {free, trial, basic} with
# `plus` reserved for the future tier launch.
#
# `frozenset` not `set` so the map is immutable at module import — a
# typo in a route gate fails fast on KeyError rather than silently
# narrowing some user's access.
FEATURE_TIER_MAP: Dict[Feature, FrozenSet[str]] = {
    Feature.BASIC_ACCESS: frozenset({"trial", "basic", "plus"}),
    # Plus-only features. Empty at launch ({}) would also work, but
    # naming "plus" here documents the intent and means flipping the
    # feature live at V2 is a no-op (just add the tier to a UserProfile).
    Feature.PLUS_ACCESS: frozenset({"plus"}),
    Feature.REFLECTION_ROOM_ACCESS: frozenset({"plus"}),
    Feature.ECHO_SIGNATURE_ACCESS: frozenset({"plus"}),
    Feature.ECHO_MAP_ACCESS: frozenset({"trial", "basic", "plus"}),
    # ^ Echo Map ships with Basic at launch (spec §10 calls it out as
    # an exception that moves to Plus in V2). When that move happens,
    # change this line to frozenset({"plus"}) and the route's gate
    # automatically tightens.
    Feature.MIRROR_MOMENT_ACCESS: frozenset({"plus"}),
    Feature.MIRROR_PLEDGE_ACCESS: frozenset({"plus"}),
    Feature.CODE_LIBRARY_ACCESS: frozenset({"plus"}),
    Feature.MEMORY_TIMELINE_ACCESS: frozenset({"plus"}),
    Feature.ROLE_PATTERN_TIMELINE_ACCESS: frozenset({"plus"}),
}


def tier_grants(feature: Feature, tier: str) -> bool:
    """Pure-function predicate: does this subscription_tier value grant
    access to this feature?

    Used by the FastAPI gate and by callers that want to make
    feature-availability decisions without raising HTTP exceptions
    (e.g., the `/subscription/status` features block).
    """
    return tier in FEATURE_TIER_MAP[feature]


__all__ = ["Feature", "FEATURE_TIER_MAP", "tier_grants"]
