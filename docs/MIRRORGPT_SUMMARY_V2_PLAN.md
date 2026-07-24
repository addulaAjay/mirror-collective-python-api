# MirrorGPT Summary V2 — confidence-tagged themes + nudge

Status: **in progress**
Branch: `feat/mirrorgpt-summary-v2`

Upgrade the conversation summarizer to the V2 prompt: `key_themes` carry a
`confidence` level, and each summary produces a `nudge` signal that feeds the
Soul Ping re-engagement path. This is "Option A" (full adoption) from the
summarizer prompt review.

## Design decisions (locked)

1. **`key_themes`**: `List[str]` → `List[KeyTheme]` where
   `KeyTheme = {theme: str, confidence: "high"|"medium"|"low"}`.
   Persisted in DynamoDB as a list of maps (schemaless — no migration).
2. **`nudge`**: two flat scalar fields on `Conversation` —
   `nudge_eligible: bool`, `nudge_reason: str`.
3. **Backward compatibility**: a `_normalize_themes()` helper coerces legacy
   `List[str]` records into `KeyTheme` (`confidence="low"`) on read, so old and
   new records both work everywhere themes are consumed.
4. **Nudge → Soul Ping = flavor, NOT a hard gate.** The re-engagement path must
   still send regardless of eligibility (protects against the prior
   "everyone skipped → silent outage" incident). If `nudge_eligible`, prefer a
   reason-grounded nudge built from `nudge_reason`; otherwise fall back to the
   existing rotating copy. `eligible=false` must never suppress a ping.

## Slice 1 — schema + parser + model + persistence + back-compat ✅ DONE

Foundation. Nudge captured but dormant; no behavior change downstream.

- [x] Add `KeyTheme` dataclass + `normalize_key_themes()` / `key_themes_to_items()` helpers (`models/conversation.py`)
- [x] `SummaryResult`: `key_themes: List[KeyTheme]`, add `nudge_eligible`, `nudge_reason`
- [x] `Conversation` model: add `nudge_eligible`, `nudge_reason`; normalize themes in `from_dynamodb_item`
- [x] `_parse_response`: accept object themes (clamp bad confidence → "low", drop malformed entries), parse `nudge` (tolerate missing), keep fence recovery
- [x] `_persist` + `dynamodb_service.update_conversation_summary`: write themes-as-maps + `nudge_eligible`/`nudge_reason`
- [x] Object-safe theme rendering in `soul_ping_service` (pulled forward from Slice 3 — the type change would otherwise break it now)
- [x] Tests: object parse, confidence clamp, legacy string back-compat, nudge present/absent, malformed-theme drop, model round-trip
- [x] Full suite green (all pass) + commit

Notes: `nudge_eligible`/`nudge_reason` are not DynamoDB reserved words (compound
underscore names), so no `ExpressionAttributeNames` aliasing was needed — matches
the existing unaliased `summary`/`key_themes`/`open_threads` writes.

## Slice 2 — reliability + prompt finalize ✅ DONE

- [x] `openai_service.send_with_overrides_async`: optional `response_format` param (splatted via `create_kwargs`)
- [x] Summarizer passes `{"type": "json_object"}`; bump `MIRRORGPT_SUMMARY_MAX_TOKENS` 400 → 500
- [x] Swap in V2 `SUMMARIZER_SYSTEM_PROMPT`; neutralized the skeleton `eligible` to `false`; softened the "high" confidence rubric to "explicitly supported throughout the current conversation" (no cross-conversation context is fed to the summarizer)
- [x] Tests: response_format passthrough; V2 prompt anchors (confidence + nudge); existing safety/anti-oracle anchors still pass
- [x] Full suite green + mypy clean + commit

## Slice 3 — Soul Ping wiring ✅ DONE

- [x] `soul_ping_service`: render themes via `t.theme` (object-safe — done in Slice 1)
- [x] `_recent_nudge_reason(user_id)` surfaces the recent conversation's `nudge_reason` when `nudge_eligible`
- [x] `build_reengagement_ping(..., reason=)` → reason-grounded Reflection Nudge ("A thread to pick up", systemic-leaning) when eligible; rotating generic copy otherwise
- [x] `maybe_send_for_user` re-engagement branch fetches the reason and passes it — always sends (flavor, never suppress)
- [x] Tests: reason-grounded build; `_recent_nudge_reason` eligible/not; full-path reason-grounded nudge; **not-eligible → still sends (anti-outage regression)**
- [x] Full suite green + mypy clean + commit

## Status: COMPLETE ✅

All three slices implemented, tested, and committed on `feat/mirrorgpt-summary-v2`.
End-to-end: the V2 prompt emits confidence-tagged themes + a nudge; the parser +
model + DynamoDB persist them (back-compatible with legacy string themes); and
the Soul Ping re-engagement path turns an eligible nudge into a grounded
Reflection Nudge without ever suppressing a ping.

## Risks / notes

- Main risk is back-compat; mitigated by `_normalize_themes` + tolerant parsing.
- No data migration (Dynamo schemaless); records upgrade lazily on next summary.
- `nudge` / `eligible` may be DynamoDB reserved words → use `ExpressionAttributeNames` aliases in the update expression.
- Anti-outage property protected by "flavor not gate" + a dedicated regression test.
