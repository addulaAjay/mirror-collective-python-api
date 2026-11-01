"""Server-driven feature flags.

Why the server and not the app: TestFlight and the App Store ship the identical
binary, so a value compiled into the bundle cannot be on for testers and off
for everyone else. (The app's build-time env flags don't work at all today —
``react-native-dotenv`` is a dependency but was never added to
``babel.config.js``, so ``process.env.*`` is never substituted.)

Flags are returned in the ``features`` object of ``GET /api/subscriptions/
status``, which the client already caches in ``SubscriptionContext``. Flipping
a flag is a Lambda env change that takes effect on the next cold start — no app
release.

Everything here FAILS CLOSED. A feature that is not ready leaking to production
users is worse than a tester waiting for a redeploy.
"""

import os
from typing import FrozenSet, Optional


def _parse_ids(raw: Optional[str]) -> FrozenSet[str]:
    """``"a, b, ,c,"`` -> ``{"a", "b", "c"}``.

    Env vars get hand-edited in the AWS console, so stray whitespace and empty
    segments are expected. Blanks are dropped deliberately: a set containing
    ``""`` would match any caller whose user id failed to resolve.
    """
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


# Global switch. Set REFLECTION_ROOM_ENABLED=true to show Reflection Room to
# every user — this is the production launch lever.
REFLECTION_ROOM_ENABLED = (
    os.getenv("REFLECTION_ROOM_ENABLED", "false").lower() == "true"
)

# Per-user allowlist, e.g. REFLECTION_ROOM_TESTER_IDS="cognito-sub-1,sub-2".
# Lets testers exercise the feature in production while it stays hidden from
# everyone else.
REFLECTION_ROOM_TESTER_IDS = _parse_ids(os.getenv("REFLECTION_ROOM_TESTER_IDS"))


def reflection_room_enabled_for(user_id: Optional[str]) -> bool:
    """Should this user see Reflection Room?

    Global switch first, then the tester allowlist. Read at call time rather
    than bound into a constant, so a Lambda env change needs only a cold
    start.
    """
    if REFLECTION_ROOM_ENABLED:
        return True
    if not user_id:
        return False
    return user_id in REFLECTION_ROOM_TESTER_IDS
