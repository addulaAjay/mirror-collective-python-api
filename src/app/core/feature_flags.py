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


# Global switch for the Echo Inbox — the in-app view of echoes sent to you.
# The feature is complete (recipient-to-account linking, the
# recipient-user-id GSI query, EchoInboxScreen, its route) but its only entry
# point was compiled out with a literal `{false && ...}` pending a product
# decision. This replaces that with something flippable without a release.
ECHO_INBOX_ENABLED = os.getenv("ECHO_INBOX_ENABLED", "false").lower() == "true"

# Per-user allowlist for the Echo Inbox. Separate from the Reflection Room
# list on purpose — the two roll out independently.
ECHO_INBOX_TESTER_IDS = _parse_ids(os.getenv("ECHO_INBOX_TESTER_IDS"))


def _enabled_for(
    user_id: Optional[str], globally_on: bool, tester_ids: FrozenSet[str]
) -> bool:
    """Global switch first, then the tester allowlist.

    Shared by every rollout flag here. Arguments are passed in rather than
    read from module state so callers stay patchable in tests, and so a
    Lambda env change takes effect on the next cold start rather than needing
    a deploy.
    """
    if globally_on:
        return True
    if not user_id:
        return False
    return user_id in tester_ids


def reflection_room_enabled_for(user_id: Optional[str]) -> bool:
    """Should this user see Reflection Room?"""
    return _enabled_for(user_id, REFLECTION_ROOM_ENABLED, REFLECTION_ROOM_TESTER_IDS)


def echo_inbox_enabled_for(user_id: Optional[str]) -> bool:
    """Should this user see the Echo Inbox entry point?

    Note this gates *visibility of the entry point only*. Echoes are already
    delivered to every recipient by email, whose CTA opens a server-rendered
    web viewer (``GET /share/echo/{id}``) and needs no app. The inbox is a
    second channel, not the only one — so turning this off strands nobody.

    There is no push notification for a received echo, so a user with the
    inbox enabled has no in-app signal that something arrived.
    """
    return _enabled_for(user_id, ECHO_INBOX_ENABLED, ECHO_INBOX_TESTER_IDS)
