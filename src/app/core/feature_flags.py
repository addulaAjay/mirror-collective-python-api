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


# Internal test accounts — the profiles belonging to addula.ajay@gmail.com and
# mailtoajay44@gmail.com. Several each, from duplicate signups during testing,
# so all are listed and whichever one is signed in works.
#
# These live in code rather than the Lambda environment because there is no
# room: the production deploy on 2026-11-08 failed with "environment variables
# exceeded the 4KB limit. Measured size: 4311 bytes", and without this list the
# environment sits at roughly 3998 — about 98 bytes of headroom. The 22
# DYNAMODB_* table-name variables account for ~1843 bytes of that; deriving
# them in code would free most of it and is the real fix.
#
# They are internal test-account ids: not secrets, and not environment-specific
# config, so a constant is an honest home for them. FEATURE_TESTER_IDS still
# overrides when set, for the day there is space again.
_DEFAULT_TESTER_IDS = frozenset(
    {
        "84289458-e081-70f8-a76f-859cc488fdde",  # addula.ajay, core_plus
        "54f8a4b8-f061-7060-5a4c-2edca101decd",  # addula.ajay
        "045854c8-b031-7020-ce2a-0df085220040",  # addula.ajay
        "0428f478-e051-7025-1202-d3ab0994a2a4",  # mailtoajay44, trial
        "d4980488-00e1-70a6-bfb0-903e7886b1b7",  # mailtoajay44
        "c4f85448-d0b1-70e3-39c0-0ecf857c5201",  # mailtoajay44
        "b40894c8-f001-7038-8de0-acbe62b269cd",  # mailtoajay44
        "44986408-4041-7096-c5e6-db447f7876e6",  # mailtoajay44
    }
)


def _tester_ids(feature: str) -> FrozenSet[str]:
    """Allowlist for ``feature``, falling back to the shared list.

    ``<FEATURE>_TESTER_IDS`` wins when set and non-empty; otherwise
    ``FEATURE_TESTER_IDS`` applies. An empty per-feature value falls through
    rather than meaning "nobody", so clearing one in the AWS console does not
    silently lock testers out while the shared list is populated.

    One shared list exists because two 8-UUID lists in provider.environment
    pushed UpdateFunctionConfiguration past its 5120-byte request limit and
    failed the production deploy on 2026-11-08 (HTTP 413). Only the shared
    one is declared in serverless.yml, and only on the api function.

    Read at call time so a Lambda env change needs only a cold start.
    """
    specific = _parse_ids(os.getenv(f"{feature}_TESTER_IDS"))
    if specific:
        return specific
    shared = _parse_ids(os.getenv("FEATURE_TESTER_IDS"))
    return shared if shared else _DEFAULT_TESTER_IDS


# Global switch for the Echo Inbox — the in-app view of echoes sent to you.
# The feature is complete (recipient-to-account linking, the
# recipient-user-id GSI query, EchoInboxScreen, its route) but its only entry
# point was compiled out with a literal `{false && ...}` pending a product
# decision. This replaces that with something flippable without a release.
ECHO_INBOX_ENABLED = os.getenv("ECHO_INBOX_ENABLED", "false").lower() == "true"


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
    return _enabled_for(
        user_id, REFLECTION_ROOM_ENABLED, _tester_ids("REFLECTION_ROOM")
    )


def echo_inbox_enabled_for(user_id: Optional[str]) -> bool:
    """Should this user see the Echo Inbox entry point?

    Note this gates *visibility of the entry point only*. Echoes are already
    delivered to every recipient by email, whose CTA opens a server-rendered
    web viewer (``GET /share/echo/{id}``) and needs no app. The inbox is a
    second channel, not the only one — so turning this off strands nobody.

    There is no push notification for a received echo, so a user with the
    inbox enabled has no in-app signal that something arrived.
    """
    return _enabled_for(user_id, ECHO_INBOX_ENABLED, _tester_ids("ECHO_INBOX"))
