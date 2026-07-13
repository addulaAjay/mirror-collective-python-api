# MirrorGPT Memory — Implementation Plan (Recall Fix + Life Anchors + Memory Preflight)

**Status:** Proposed
**Scope:** `mirror_collective_python_api` (FastAPI + DynamoDB on Lambda)
**Driver:** Beta feedback — MirrorGPT does not consistently recall the last 1–2 chats; testers want durable, intentional memory ("Life Anchors").

> Product principle: *Mirror does not need to remember everything. It needs to remember what changes the meaning of everything else.*

---

## 1. How memory works today (as-built)

Per response, the LLM receives **only**:

```
[ static system prompt ]  +  [ last 10 turns of the CURRENT conversation ]  +  [ current user message ]
```

Key facts (with source):
- History is fetched **by `conversation_id` partition key** — it cannot span conversations. A new session = a new conversation = **empty history**. (`dynamodb_service.get_recent_messages`; `mirror_orchestrator._get_conversation_history`, limit=10)
- The **only** cross-session bridge is a single "continuity carrier" (prior conversation's summary), injected **only on the first turn of a new conversation**. (`mirror_orchestrator.process_mirror_chat` ~L269–276, `_load_prior_continuity_carrier` ~L476–554)
- The system prompt is **static** and, by explicit design, does **not** inject cross-session signals (archetype, patterns). (`_build_system_prompt` ~L199–210)

Durable structured memory that already exists but is **NOT wired into chat**:
- **Recent Reflection Summary** — `conversation_summarizer` produces `{summary, key_themes, open_threads}`, stored on the Conversation item. Used only as the cold-start carrier.
- **Echo Map / Pattern Memory** — `mc_echo_loop_state`: 6 loops (`pressure, overwhelm, grief, self_silencing, agency, transition`) with `tone_state`, `intensity_score/label`, `last_seen`, trend. Fully built; read only by Reflection Room / snapshot endpoints. **Chat never reads it.**
- Archetype profile + mirror-moments (`significance_score`) — computed, persisted, never injected.

---

## 2. Why last-chat recall is inconsistent (root cause)

Not one bug — a **fragile 5-condition chain**. A user recalls their last chat only if ALL hold:

1. In-context history never spans conversations → recall depends entirely on the carrier summary.
2. The carrier fires **only on a new conversation's first turn** (dropped turn 2+; never re-injected).
3. The prior conversation had **≥4 messages** (`MIRRORGPT_SUMMARY_FIRST_AT=4`) — short chats never summarize.
4. The summary **actually got written** — but it's a **fire-and-forget** task Lambda doesn't guarantee completes (`asyncio.create_task` post-response).
5. The summary sits on the **immediately** most-recent prior conversation (no fall-through to older summarized ones).

Two concrete bugs make #3/#4 worse:
- **Non-atomic `message_count`** — concurrent user+assistant writes do read-modify-write with `SET message_count = <in-memory>` (not DynamoDB `ADD`), so a 2-message turn often advances the count by 1 → the ≥4 threshold is reached late or never. (`dynamodb_service.update_conversation` ~L566; concurrent save via `asyncio.gather` in `mirrorgpt_routes`)
- **Best-effort summary write** — acknowledged in-code as "not guaranteed to complete on Lambda before freeze."

**Highest-impact levers:** (a) does the **client reuse `conversation_id`** (if it starts fresh each session, in-context history is always empty); (b) does the **summary reliably get written**.

### 2a. CONFIRMED (2026-07-12 trace): the client starts fresh every session — a client/server contract mismatch

End-to-end trace of both repos settles lever (a): **the server is built for resume; the RN client never wired it up and actively wipes the id.** This is the primary cause of the beta complaint.

**Server side — resume is ready and offered:**
- `/session/greeting` returns `"conversation_id": continuity.get("resume_conversation_id")` (the user's most-recent prior conversation) plus `has_prior_context`. In-code comment: *"the client should echo conversation_id back on /chat so the same thread continues."* (`src/app/api/mirrorgpt_routes.py:1200–1204`, resume id computed ~L335)
- `/chat` with a null `conversation_id` **creates a brand-new conversation** (`mirrorgpt_routes.py:400–416`); `get_recent_messages` queries strictly by `conversation_id` PK (`dynamodb_service.py:904–906`) → new conversation = empty history.

**Client side — drops the resume id and wipes stored state:**
1. `SessionGreetingResponse` type declares only `greeting_message, session_id, timestamp, user_archetype, archetype_confidence` — **no `conversation_id` field**, so the server's resume id is structurally invisible to the client. (`mirror_collective_app/.../src/types/api.ts:125–131`)
2. Every chat-screen mount runs `initializeSession()` → `SessionManager.generateNewSession()` → `AsyncStorage.removeItem(CONVERSATION_STORAGE_KEY)` — **wipes** any stored id. (`src/hooks/useChat/useChat.ts:56` → `src/services/sessionManager.ts:22`)
3. First `sendMessage` reads `getConversationId()` (now `null`) and sends `conversation_id: null`. (`useChat.ts:105,122`)
4. → server creates a fresh conversation → **in-context history is empty at the start of every real session** (app cold-start or screen unmount/remount).

Net: the *only* surviving cross-session bridge is the server-side continuity carrier (the fragile 5-condition summary hand-off above) — which is exactly why recall works for *some* users *sometimes*. Fixing the client contract restores deterministic single-thread continuity and takes the carrier off the critical path.

---

## 3. Target architecture — Memory Preflight + 4 tiers

```
User message  ─►  Memory Preflight (≈500–1,500 tokens)  ─►  MirrorGPT reflection
                         │
                         ├─ Tier 1  Session context (current turns)          [EXISTS]
                         ├─ Tier 2  Recent Reflection Summary                [EXISTS, under-used]
                         ├─ Tier 3  Pattern Memory / Echo Map loops+trends   [STORE EXISTS, not wired]
                         └─ Tier 4  Life Anchors (user-declared, permissioned)[NEW]
```

Status vs. proposal:

| Tier | Status | Work |
|------|--------|------|
| 1 Session | ✅ Working | — |
| 2 Recent Summary | ✅ Built, narrow | Make reliable + use in-session (Phase 0/1) |
| 3 Pattern / Echo Map | ⚠️ Stored, not wired | Retrieve + inject (Phase 1) |
| 4 Life Anchors | ❌ Missing | Build (Phase 2) |
| Memory Preflight | ⚠️ Insertion point exists | `process_mirror_chat` `asyncio.gather` |

The preflight assembles a compact structured packet and injects it via the **existing "background system message" vector** (same technique as the continuity carrier) — no rewrite of the prompt policy required, and it keeps token cost bounded.

---

## 4. Phased plan

### Phase 0 — Recall reliability (days; no new product surface)
Directly fixes the beta complaint. Ship first.

- **0.1 Atomic message count.** Replace `SET message_count = :v` with DynamoDB `ADD message_count :inc` (or stop storing a counter and derive it). Removes the undercount that starves summaries.
- **0.2 Reliable summary write.** Stop relying on fire-and-forget. Options (pick one): `await` the summary within the request when the threshold is crossed; OR summarize on session end / greeting open (lazy-on-read already exists — extend it); OR a DynamoDB Streams/queue trigger. Guarantee at-least-once.
- **0.3 Loosen the summary threshold.** Lower `MIRRORGPT_SUMMARY_FIRST_AT` (e.g. 4 → 2) or summarize any conversation with ≥1 user turn on session close, so short prior chats are recalled.
- **0.4 Carrier robustness.** (a) Re-inject the carrier beyond turn 1 (keep it in context for the whole new conversation, not just the first turn); (b) fall through to the most-recent **summarized** conversation if the immediately-prior one has no summary.
- **0.5 Client `conversation_id` reuse — CONFIRMED as the #1 fix (see §2a).** Root cause is a client/server contract mismatch, not a server gap: the server already offers a resume id via `/session/greeting`; the RN client can't see it (type omits the field) and wipes any stored id on mount. **Fix is client-side, no server change required:**
  1. Extend `SessionGreetingResponse` with `conversation_id?: string | null` and `has_prior_context?: boolean` (`src/types/api.ts`).
  2. In `initializeSession` (`src/hooks/useChat/useChat.ts`): **stop wiping** the conversation id on mount, and when the greeting response carries a `conversation_id`, store it via `SessionManager.setConversationId(...)` so the first `/chat` message echoes it back and the prior thread continues. (Adjust `generateNewSession()` so it no longer removes `CONVERSATION_STORAGE_KEY`, or split "new session id" from "clear conversation".)
  3. Add client tests: greeting with a resume id → next `/chat` sends that id; greeting without one → sends `null` (new conversation).

  **Product semantics decided:** *continue the most-recent thread* on re-entry (full in-context recall of that chat). This restores single-thread continuity immediately. Recall across **multiple distinct** prior chats ("last 1–2 chats") is delivered by the Memory Preflight recent-summary + patterns legs (Phase 1) — the two are complementary, not alternatives.

  **Server-side backstop (optional, defense-in-depth):** if `/chat` receives a null `conversation_id`, it *could* resume the user's most-recent open conversation instead of always creating a new one — hardens recall against any future client that forgets to echo the id. Lower priority than the client fix; gate behind a flag if added.

**Exit criteria:** a scripted 2-session test (chat → new session → reference prior topic) recalls reliably across Lambda cold starts, for both short and long prior chats.

### Phase 1 — Memory Preflight: wire Tier 3 (Echo Map) + Tier 2 (recent summary) into chat
Uses memory you already store. Scope grounded in a full code trace (2026-07-12).

#### As-built facts (confirmed)
- **Echo Map store:** `EchoLoopStateRepo` (`src/app/repositories/echo_loop_state_repo.py`), table `mc_echo_loop_state` (PK `user_id`, SK `loop_id`). ≤6 loops: `pressure, overwhelm, grief, self_silencing, agency, transition`. Per loop: `tone_state` (rising|steady|softening), `intensity_score` [0–1], `intensity_label` (High|Medium|Low), `last_seen`, `recently_changed`. Read via `query_by_user(user_id) -> List[EchoLoopState]`.
- **Tone guidance is free:** the tone library (`src/app/services/echo/tone_library_loader.py` → `lookup(loop_id, tone_state)`) already yields a reflection line per (loop, tone), e.g. grief+rising → *"Grief is surfacing. It's asking for presence, not resolution."* Reuse as `tone_guidance` — no new copy.
- **Chat reads none of it today** (`mirror_orchestrator` / `mirrorgpt_routes` don't touch loop state). This is the gap.
- **Injection mechanics:** the final LLM call is assembled in `ResponseGenerator.generate_enhanced_response` as `messages = [ChatMessage("system", system_prompt)] + history + [ChatMessage("user", user_message)]` (`mirror_orchestrator.py` ~L177–180). `_build_system_prompt` is **static** and deliberately injects no cross-session signals. The continuity carrier is injected by **prepending to `history`** only when history is empty (~L269–276).

#### Key design decision — read `query_by_user`, NOT `build_snapshot`
`build_snapshot()` (`services/echo/snapshot_service.py`) is the existing read path but **enforces an active reflection session and raises `SessionExpired`/`NotFoundError`** (tied to the daily quiz). Coupling chat to that would drop all pattern context for anyone chatting without a fresh reflection session — or throw in the hot path. Phase 1 therefore reads `loop_state_repo.query_by_user(user_id)` directly, filters `intensity_score > 0`, sorts desc, takes top-N, and enriches with the tone library itself. Chat stays decoupled from reflection-session lifecycle.

#### 1.1 Preflight fetch leg (patterns + recent summary)
Add legs to the existing `asyncio.gather` in `process_mirror_chat` (`mirror_orchestrator.py` ~L241), `return_exceptions=True`, degrade-to-empty per the existing per-leg pattern:
- **Tier 3:** `EchoLoopStateRepo().query_by_user(user_id)`.
- **Tier 2:** the recent reflection summary. Reuse the continuity data already computed for the carrier / greeting (`get_recent_conversations` most-recent summarized). Injected on **every** turn (the carrier only fires turn-1 of an empty conversation), so resumed/continued conversations also carry cross-session summary context.

Runs **in parallel** with the profile/signals/history fetches → **~0 added wall-clock**. Gated by the flag: when off, the legs are skipped entirely (no fetch, no cost).

#### 1.2 Render the packet
One bounded background `ChatMessage(role="system", …)` combining:
- **Active patterns** (top-N, N≈3): `loop — tone/trend, intensity_label, "age" (last_seen)` + the tone-library guidance line.
- **Recent summary** (Tier 2): the one-line recent reflection summary + top open thread.
- Framing header matching the carrier's: *"background only — do NOT quote, reflect as context not identity; obey anti-oracle/safety rules."*

Cap the whole packet (~400–600 tokens; hard char cap like history's per-turn 2000). Estimated ≈500–600 tokens for all 6 loops enriched, so top-3 stays well under budget.

#### 1.3 Inject on every turn
After the existing carrier logic, prepend the packet to `history`: `if packet: history = [packet] + history`. Final order → `[system_prompt, pattern+summary packet, carrier?/history…, user]`. No signature changes to `generate_enhanced_response` (packet rides the existing `history` vector).

#### Feature flag
`MIRRORGPT_PREFLIGHT_PATTERNS` (default `false`), read per convention (`os.getenv(..., "false").lower() == "true"`). Ship dark; enable per cohort.

#### Files to touch
- `src/app/services/mirror_orchestrator.py` — flag read (`__init__`), gather leg(s), `_load_pattern_preflight(user_id)` builder (fetch + render), prepend-to-history.
- Reuse (no change): `EchoLoopStateRepo`, `tone_library_loader`, the recent-summary path.
- Tests: fetch degrades to empty on repo error; render caps at top-N and includes tone guidance + recent summary; packet present-on-every-turn when flag on / absent when off; `process_mirror_chat` integration; token-size assertion.

#### Boundaries / non-goals
- Loops are written only by **quiz + practice completion** — Phase 1 is **read-only** injection; chat does not update loop state (a possible future enhancement, explicitly out of scope).
- Packet reflects reflection-room state (patterns) + recent-conversation summary — both already-stored memory; no new store.

**Exit criteria:** chat responses demonstrably reference active patterns and recent context; added tokens < ~600/turn; one extra (parallel) DynamoDB query; feature-flagged (`MIRRORGPT_PREFLIGHT_PATTERNS`); no p50 latency change with flag off; suite green.

### Phase 2 — Life Anchors (Tier 4) — the new feature
Build as an owned, user-scoped, **permissioned** entity. Scope grounded in a full code trace (2026-07-12). Conventions confirmed against `user_personalization` (permissioned template), `_RepoBase`, `me_routes.py`, and the `FakeAioSession`/`FakeTable` test shim. Stacks on the Phase 1 preflight for injection.

#### 🔑 Key design decision — detection must NOT put an LLM call in the chat hot path
The obvious implementation (run a `gpt-4o-mini` classifier synchronously inside `process_mirror_chat` before returning) adds ~1–2s to every qualifying reply — a latency regression we've avoided throughout Phase 0/1. Instead:
- **Gate = cheap heuristic in the hot path, reusing signals the orchestrator ALREADY computes** — `change_analysis.mirror_moment_triggered`, high `signal_1_emotional_resonance` / `significance_score`, plus a small keyword set (loss, died, divorce, diagnosis, sober, "remember this"). Zero new latency, zero new model call. When it trips, the chat response carries an immediate `memory_prompt`.
- **LLM only AFTER the user opts in.** When the user taps "Remember," the confirm endpoint runs the cheap `gpt-4o-mini` pass (reusing the `conversation_summarizer` call pattern — `send_with_overrides_async`, temp ~0.1, bounded `max_tokens`, JSON parse) to structure the anchor (`anchor_type`, `relationship`, `tone_guidance`). This is off the chat hot path; the user has already committed. **No anchor is ever stored without explicit confirmation.**

This keeps the headline feature perf-neutral for chat while still using an LLM where it adds value (structuring a confirmed anchor).

**2.1 Data model** — `src/app/models/life_anchor.py` (new):

```jsonc
{
  "user_id": "…",              // PK
  "anchor_id": "…",            // SK
  "anchor_type": "loss|birth|divorce|diagnosis|sobriety|anniversary|transition|custom",
  "title": "User's wife passed away",
  "description": "…",
  "relationship": "wife",       // optional
  "date": "optional",
  "emotional_weight": "sacred|high|medium",
  "reflection_use": "always_consider|when_relevant|never",
  "status": "active|paused",
  "scopes": {                   // where it may be used
    "mirrorgpt": true,
    "echo_map": false,
    "echo_vault": false,
    "legacy_capsule": false
  },
  "created_from": "mirrorgpt|manual",
  "user_confirmed": true,
  "tone_guidance": ["Do not say time heals everything."],  // optional do_not_use
  "created_at": "…", "updated_at": "…"
}
```

Ship as three stacked sub-PRs so each is reviewable and independently valuable:

#### Phase 2A — Entity + CRUD + infra (no chat coupling)
The permissioned store and user-facing management endpoints. Follows the confirmed conventions exactly:
- `src/app/models/life_anchor.py` — `@dataclass LifeAnchor` with `to_dynamodb_item`/`from_dynamodb_item` (pattern: `models/user_personalization.py`, `models/echo_loop_state.py`). Nested `scopes`/`tone_guidance` handled in `from_dynamodb_item` like `UserFlags`.
- `src/app/repositories/life_anchor_repo.py` — subclass `_RepoBase` (`repositories/_base.py`); `self.table_name = os.getenv("DYNAMODB_LIFE_ANCHORS_TABLE", "mc_life_anchors-development")`; `query_by_user`, `get`, `upsert`, `delete`, `list_active_for_user` (GSI `status-index`); injectable `session` for tests. Serialize via `_serializers.to_ddb`/`from_ddb`.
- `src/app/api/life_anchors_routes.py` — `APIRouter(tags=["Life Anchors"])`; `get_life_anchor_repo()` `Depends` factory; routes `GET/POST /api/me/life-anchors`, `PUT/DELETE /api/me/life-anchors/{anchor_id}`, `POST .../{id}/pause`; `get_current_user` dep + `_user_id_or_401`; `success/data/message` envelope; Pydantic request/response models with `Field` constraints. Register in `src/app/handler.py` via `app.include_router(life_anchors_router, prefix="/api")`.
- **Infra:** `serverless.yml` — `LifeAnchorsTable` (PK `user_id` / SK `anchor_id`; GSIs `status-index`, `created-at-index`; `BillingMode: PAY_PER_REQUEST`; `DeletionPolicy: Retain` + `UpdateReplacePolicy: Retain`), `provider.environment.DYNAMODB_LIFE_ANCHORS_TABLE`, and an IAM `Fn::GetAtt` grant incl. `index/*` (mirror `MirrorMomentsTable`). Add a table block to `scripts/create_reflection_room_tables.py` and the env var to `.env.example`.
- **Tests:** `tests/test_life_anchor_repo.py` (repo via `FakeAioSession`/`FakeTable` — `FakeTable(primary_key=["user_id","anchor_id"], indexes={"status-index":["user_id","status"]})`), `tests/test_life_anchors_routes.py` (`TestClient` + `app.dependency_overrides` for `get_current_user` and `get_life_anchor_repo`).

**Exit 2A:** full CRUD + pause/delete over the real conventions; ownership enforced by `user_id` PK; anchors round-trip through the fake DDB; suite green.

#### Phase 2B — Detection → confirm flow (perf-neutral)
- `src/app/api/models.py` — add `memory_prompt: Optional[Dict[str, Any]] = None` to `MirrorGPTChatData` (rides the same flow as the existing `suggested_practice`).
- **Heuristic gate** (new small module, e.g. `src/app/services/life_anchor_detector.py`): pure function over the orchestrator `result` (`mirror_moment_triggered`, emotional-resonance/significance, keyword set). Returns an optional `memory_prompt` dict `{prompt, candidate_text, anchor_type_guess, emotional_weight_guess}`. Wired in `mirror_orchestrator.process_mirror_chat` (or the route) with **no LLM call** — pure signal reuse. Surfaced via `result["memory_prompt"]` → `MirrorGPTChatData`.
- **Confirm endpoint** `POST /api/me/life-anchors/confirm` — accepts the candidate + user choice (Remember / Save-to-Echo-Vault / Not-now / Never); on Remember, runs the `gpt-4o-mini` structuring pass (reuse `conversation_summarizer` call/parse pattern) to fill `anchor_type`/`relationship`/`tone_guidance`, then `upsert`. "Never" records a suppression so we don't re-prompt the same candidate. **Storage only on explicit confirm.**

**Exit 2B:** high-emotional-weight turns surface a `memory_prompt` with **zero added chat latency** (assert no model call on the chat path); confirm writes a structured anchor; decline writes nothing.

#### Phase 2C — Inject anchors into the preflight (reuses Phase 1)
- Extend the Phase 1 preflight: add a Life Anchors fetch to `_load_preflight_data` (`list_active_for_user`, filtered `status=="active"` and `scopes.mirrorgpt==True`) and render `always_consider` + relevant `when_relevant` anchors into the existing packet, including `tone_guidance`/`do_not_use`. Same bounded-token cap and background-only framing. Gated by `MIRRORGPT_LIFE_ANCHORS` (separate flag from patterns).
- Ordering in the packet: Life Anchors first (highest-priority durable context), then patterns, then recent summary.

**Exit 2C:** an anchor created in session A is referenced (with care + tone guidance) in session B even when only the last 3 chats are in context; `status==paused`/deleted anchors never surface next turn; sacred anchors always considered.

#### Permissions / safety (spans 2A–2C)
Per-anchor `status` (active|paused) + `scopes` + `reflection_use` + `emotional_weight`; user can edit/pause/**hard-delete** (or TTL). Ownership = `user_id` PK from the Cognito claim, same as all user-scoped reads. Injection is gated by per-anchor permission — the guardrail that makes cross-session injection safe. Consider `ENABLE_MESSAGE_ENCRYPTION` for sensitive anchor content.

#### Client-UI boundary (out of backend scope)
Backend contract: chat response `memory_prompt` (the ask) + the `/api/me/life-anchors` CRUD + `/confirm` endpoints. The RN app renders the Remember/Not-now/Never prompt and a Life Anchors management screen. Coordinate the `memory_prompt` shape with the app team (mirrors how the client already reads `suggested_practice`).

---

## 5. Memory Preflight packet (target shape)

Assembled in `process_mirror_chat`, ~500–1,500 tokens, injected as a background system message:

```jsonc
{
  "life_anchors": [ { "anchor_type": "loss", "title": "User's wife passed away",
                      "emotional_weight": "sacred", "reflection_use": "always_consider" } ],
  "active_patterns": [ { "loop": "grief", "trend": "rising", "strength": "high", "last_seen": "2 days ago" } ],
  "recent_summary": "User has been processing loneliness and fear of moving forward…",
  "tone_guidance": "Respond gently. Reflect grief as context, not identity.",
  "do_not_use": [ "Do not say time heals everything." ]
}
```

---

## 6. Cost, safety, rollout

- **Cost:** the preflight is *cheaper* than expanding raw history — a bounded structured packet replaces multi-thousand-token transcripts. Cap each leg; log packet token size.
- **Policy change:** injecting cross-session context is a deliberate reversal of the current "current-session-only" prompt constraint. It MUST be gated by per-anchor permission/`status` — this is the guardrail that makes it safe.
- **Flags:** `MIRRORGPT_PREFLIGHT_PATTERNS`, `MIRRORGPT_LIFE_ANCHORS` — ship dark, enable per-cohort.
- **Privacy:** anchors can hold sensitive content; consider the existing `ENABLE_MESSAGE_ENCRYPTION` hook. Honor `status==paused` and delete immediately (hard delete or TTL).
- **Testing:** unit (repo/routes via fake DynamoDB), integration (2-session recall script), safety (paused/deleted anchors never surface; sacred anchors always considered).

---

## 7. Sequencing recommendation

1. **Phase 0** — fixes the actual beta bug; low risk; no schema change. Ship immediately.
2. **Phase 1** — wire Echo Map into chat; "feels like it remembers me" using existing data; ~1 small PR.
3. **Phase 2** — Life Anchors; the headline feature; schema + endpoints + detection + client UI.

Phase 0 and Phase 1 together deliver the "reliably references recent history + patterns" ask; Phase 2 delivers "intentional durable memory."
