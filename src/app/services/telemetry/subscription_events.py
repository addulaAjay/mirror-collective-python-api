"""Subscription / trial telemetry events (pricing spec 2026-05-12 §5).

Five events the analytics stack needs from day one to compute trial
conversion funnels:

  - paywall_view   FE fires on StartFreeTrialScreen mount.
  - start_trial    BE fires on successful trial activation
                   (verify_and_activate_purchase, is_trial=True).
  - trial_convert  BE fires when a renewal moves status TRIAL -> ACTIVE
                   (first paid renewal after the 14-day trial).
  - trial_cancel   BE fires when the user disables auto-renew while still
                   in the trial window (cancel_subscription).
  - trial_expire   BE fires when the trial ends without conversion
                   (_handle_subscription_expired with prior status TRIAL).

All events share the same TelemetryEmitter Protocol as reflection_events
and route through the module-level default emitter. Swap to a Mixpanel /
Segment / Kinesis sink in one place when we wire analytics shipping.

The PII filter in StructuredLogEmitter is strict (int/float/bool + str
<=64 chars only). Subscription-id / product-id strings fit; transaction
metadata that's longer is dropped silently rather than leaked to logs.
"""

from __future__ import annotations

from typing import Any, Optional

from .reflection_events import (
    StructuredLogEmitter,
    TelemetryEmitter,
    get_default_emitter,
    hash_user_id,
)

# Event names per spec §5.
EVENT_PAYWALL_VIEW = "paywall_view"
EVENT_START_TRIAL = "start_trial"
EVENT_TRIAL_CONVERT = "trial_convert"
EVENT_TRIAL_CANCEL = "trial_cancel"
EVENT_TRIAL_EXPIRE = "trial_expire"


def emit_subscription_event(
    event_name: str,
    *,
    user_id: str,
    subscription_id: Optional[str] = None,
    product_id: Optional[str] = None,
    platform: Optional[str] = None,
    emitter: Optional[TelemetryEmitter] = None,
    **extra: Any,
) -> None:
    """Fire one of the five trial events.

    `user_id` is hashed before emission to match the privacy guarantees
    in reflection_events (no raw cognito sub in analytics output). Pass
    `emitter=` to override the default sink in tests; production code
    relies on the module-level default.

    Optional fields (`subscription_id`, `product_id`, `platform`) are
    forwarded to the emitter when present so analytics can break the
    funnel down by SKU / platform. `extra` is for one-off keys; the
    PII filter on StructuredLogEmitter prunes anything richer than a
    short scalar.

    Best-effort: any emitter exception is swallowed so a logger glitch
    never blocks the actual subscription flow.
    """
    target = emitter or get_default_emitter()
    fields: dict[str, Any] = {}
    if subscription_id:
        fields["subscription_id"] = subscription_id
    if product_id:
        fields["product_id"] = product_id
    if platform:
        fields["platform"] = platform
    fields.update(extra)

    try:
        target.emit(
            event_name,
            user_hash=hash_user_id(user_id),
            **fields,
        )
    except Exception:
        # Telemetry must not break the subscription lifecycle — silent
        # failure is the right call here. StructuredLogEmitter doesn't
        # raise in practice, but a future emitter could.
        pass


__all__ = [
    "EVENT_PAYWALL_VIEW",
    "EVENT_START_TRIAL",
    "EVENT_TRIAL_CONVERT",
    "EVENT_TRIAL_CANCEL",
    "EVENT_TRIAL_EXPIRE",
    "StructuredLogEmitter",
    "TelemetryEmitter",
    "emit_subscription_event",
    "get_default_emitter",
]
