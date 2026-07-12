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
Build as an owned, user-scoped, **permissioned** entity following the modern repository pattern (`user_personalization` is the template).

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

**2.2 Files to add/edit** (mirrors the Reflection-Room convention):
- `src/app/models/life_anchor.py` — dataclass + `to_dynamodb_item`/`from_dynamodb_item`.
- `src/app/repositories/life_anchor_repo.py` — subclass `_RepoBase`; `os.getenv("DYNAMODB_LIFE_ANCHORS_TABLE", …)`; CRUD + `list_active_for_user(user_id)`; injectable `session` for tests.
- `src/app/api/life_anchor_routes.py` — CRUD (create / list / update / pause / delete), `get_current_user` dep, `success/data/message` envelope; register in `src/app/handler.py`.
- `scripts/create_reflection_room_tables.py` (or new script) — table config block (PK `user_id`, SK `anchor_id`; optional GSI by `status`).
- `serverless.yml` — env var (`provider.environment`), IAM `Fn::GetAtt` grant, and `LifeAnchorsTable` resource (`DeletionPolicy: Retain`).
- `.env.example` / `.env.*` — `DYNAMODB_LIFE_ANCHORS_TABLE`.
- `tests/` — repo + route tests using the in-memory `fake_dynamodb` shim.

**2.3 Permissions / user control** — reuse `user_personalization` privacy-flags mechanism + per-anchor `status`/`scopes`. User can edit, pause, delete; control where each anchor is used. Ownership = `user_id`-as-PK, same as all user-scoped reads (Cognito claim).

**2.4 Anchor creation (detection flow)** — after a response, run a cheap classifier (reuse `gpt-4o-mini`, like the summarizer) or rule/keyword heuristics on high emotional-weight turns. If a candidate is detected, the reply includes a memory prompt: *"This feels like more than a passing reflection — would you like The Mirror to remember this as a Life Anchor?"* Options: Remember / Save to Echo Vault / Not now / Never. On confirm → write the anchor. **No anchor is stored without explicit user confirmation.**

**2.5 Injection** — add a Life Anchors leg to the preflight `asyncio.gather`; render `always_consider` + relevant `when_relevant` anchors into the background system packet, filtered by `status==active` and `scopes.mirrorgpt==true`. Include `tone_guidance`/`do_not_use`.

**Exit criteria:** an anchor created in session A is referenced (with care + tone guidance) in session B even when only the last 3 chats are in context; user can pause/delete and the reference disappears next turn.

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
