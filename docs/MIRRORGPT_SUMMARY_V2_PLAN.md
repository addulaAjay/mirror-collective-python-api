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

## Slice 1 — schema + parser + model + persistence + back-compat

Foundation. Nudge captured but dormant; no behavior change downstream.

- [ ] Add `KeyTheme` dataclass + `_normalize_themes()` helper
- [ ] `SummaryResult`: `key_themes: List[KeyTheme]`, add `nudge_eligible`, `nudge_reason`
- [ ] `Conversation` model: add `nudge_eligible`, `nudge_reason`; normalize themes in `from_dynamodb_item`
- [ ] `_parse_response`: accept object themes (clamp bad confidence → "low", drop malformed entries), parse `nudge` (tolerate missing), keep fence recovery
- [ ] `_persist` + `dynamodb_service.update_conversation_summary`: write themes-as-maps + `nudge_eligible`/`nudge_reason` (reserved-word aliases via `ExpressionAttributeNames`)
- [ ] Tests: object parse, confidence clamp, legacy string back-compat, nudge present/absent, malformed-theme drop, model round-trip
- [ ] Full suite green + commit

## Slice 2 — reliability + prompt finalize

- [ ] `openai_service.send_with_overrides_async`: optional `response_format` param
- [ ] Summarizer passes `{"type": "json_object"}`; bump `MIRRORGPT_SUMMARY_MAX_TOKENS` 400 → 500
- [ ] Swap in V2 `SUMMARIZER_SYSTEM_PROMPT`; neutralize the `"eligible": true` skeleton example; soften the "high" confidence rubric (drop the "recent summarized context" clause the summarizer isn't fed)
- [ ] Tests: response_format passed through; prompt smoke checks
- [ ] Full suite green + commit

## Slice 3 — Soul Ping wiring

- [ ] `soul_ping_service`: render themes via `t.theme` (object-safe)
- [ ] `_build_context` / recent-convo path: surface `nudge_eligible` / `nudge_reason`
- [ ] `maybe_send_for_user` / `build_reengagement_ping`: eligible → reason-grounded nudge; else existing rotating copy (never suppress)
- [ ] Tests: object-theme rendering; eligible→reason-grounded; **not-eligible→still sends (anti-outage regression)**
- [ ] Full suite green + commit

## Risks / notes

- Main risk is back-compat; mitigated by `_normalize_themes` + tolerant parsing.
- No data migration (Dynamo schemaless); records upgrade lazily on next summary.
- `nudge` / `eligible` may be DynamoDB reserved words → use `ExpressionAttributeNames` aliases in the update expression.
- Anti-outage property protected by "flavor not gate" + a dedicated regression test.
