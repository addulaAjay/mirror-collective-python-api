# Scalability Runbook

This document captures the audit, plan, and execution of the May 2026
scalability initiative for the Mirror Collective backend. It is intended
to be read in three modes:

- **As a reviewer**, to understand why any individual PR exists and what
  it changes.
- **As an operator**, to know what env vars and AWS quotas must be in
  place before deploy, and how to verify or roll back.
- **As a future engineer**, to understand the load-bearing assumptions
  behind the current architecture and what the next wave of work would
  look like.

---

## 1. Why this initiative

A scaling review run in May 2026 found the backend's hard ceiling was
**~10 concurrent users**, not the thousands the product roadmap was
about to ask for. The ceiling was set in three places, all
infrastructure-config rather than code:

| Resource | Status before | Effect |
|---|---|---|
| Lambda concurrent executions (account quota) | **10** | Request #11 onward gets HTTP 429 from API Gateway. |
| DynamoDB throughput on all 19 tables | `ProvisionedThroughput: 1/1` | First burst above ~20 concurrent writes throttles. Silently. |
| SES sending quota | sandbox: 1 email/sec, 200/day, verified recipients only | Signup confirmations, password resets, echo invites blocked at small scale. |

On top of that, the code had layered amplifiers that would make even a
generous infrastructure budget collapse under load: Cognito's
account-wide 120 RPS `GetUser` quota was called on every authenticated
request, sync `boto3` calls inside `async def` routes blocked the event
loop, OpenAI calls held a thread-pool worker for the full call
duration, and Echo Vault list endpoints did N+1 queries with no
pagination.

The initiative shipped 14 PRs across three waves (Tier 0 → Tier 1 →
Wave 1) plus a security follow-up for Apple IAP, taking the user-facing
ceiling from ~10 concurrent users to a realistic 5–10k after AWS-side
quota raises.

---

## 2. Scaling ceiling — before vs after

Numbers assume the AWS quotas listed in §3 are raised. Without those,
the code wins don't fully land.

| Path | Before | After Wave 1 | Bottleneck after |
|---|---|---|---|
| Cold start (API Lambda) | ~3.5–5 s @ 512 MB | ~700 ms – 1 s @ 1024 MB | Mangum init + FastAPI import graph |
| Concurrent users (hard floor) | 10 (Lambda quota) | 5,000+ (after quota raise) | Lambda quota or DDB GSI hot partitions |
| Authenticated request | 1 Cognito `GetUser` + 1 DDB read | 0 Cognito calls (ID-token claims) or cached profile | Cognito for first-call-per-user after 5 min |
| `GET /api/echoes` (50 echoes, 30 recipients) | 1 Query + 30 GetItems (~6 s cold) | 1 Query + 1 BatchGetItem (~120 ms warm) | DDB GSI page latency |
| `GET /api/echoes/inbox` (50 recipients) | 51 sequential Queries (~10 s) | ~1 fan-out latency via `asyncio.gather` | Recipient row count vs page size |
| `POST /api/mirrorgpt/chat` thread occupancy | 1 thread held ~1–4 s/call | 0 threads held (AsyncOpenAI) | OpenAI RPM tier |
| Per-Lambda DDB clients | 10+ duplicated | 1 long-lived resource | aioboto3 max_pool_connections=50 |
| OpenAI cost per chat | gpt-4o @ 1000 max_tokens | gpt-4o-mini @ 450 max_tokens | OpenAI account budget |
| Receipt validation | Deprecated `verifyReceipt` endpoint, **unverified JWS** | App Store Server API + Apple Root CA G3 chain verification | Apple/Google API quotas |

---

## 3. AWS-side quotas to raise (operator action)

These cannot be raised from code. Open AWS Support cases:

1. **Lambda concurrent executions:** 10 → **5,000** (us-east-1).
   Service Quotas console → AWS Lambda → "Concurrent executions" →
   Request increase. Typical turnaround: a few hours to a day.
2. **SES production access:** request from the SES console (Account
   dashboard → "Request production access"). Verify the sending domain
   via DKIM. Default after approval: 14 emails/sec, 50k/day.
3. **(Optional) OpenAI org tier:** the chat path now defaults to
   `gpt-4o-mini`, which fits inside Tier 1 budgets (500 RPM). If/when
   chat traffic exceeds Tier 1, request a tier upgrade via the OpenAI
   dashboard.

---

## 4. Architecture changes (the 14 PRs)

Grouped by concern. Each row is a merged PR or open PR with a brief
note about its load-bearing role.

### 4.1 Already merged into `main`

| PR | What it does | Why it matters |
|---|---|---|
| `chore/serverless-memory-1024` (#48) | Source aligns with deployed memory (1024 MB) | Prevents next deploy from reverting to 512 MB |
| `security/cognito-jwt-authorizer` (earlier) | Adds Cognito JWT authorizer + JWKS verification | Closed an auth bypass — production API previously accepted unsigned forged tokens |
| `security/scrub-pii-logs-and-tighten-dev-mode` (#50) | Removes 5 per-request PII logs; gates dev-mode bypass on `ENVIRONMENT` | PII leak to CloudWatch + auth bypass risk if `DEBUG=true` leaked to prod |
| `perf/cognito-service-singleton` (#51) | `@lru_cache` factory for CognitoService | Was being built per request via `Depends(lambda: CognitoService())` — ~150 ms per warm request |
| `perf/lambda-init-cleanup` (#52) | `Mangum(app, lifespan="off")`; drops in-process APScheduler; registers QuotaEnforcementMiddleware once | ~350–600 ms cold-start; removes dead-on-Lambda scheduler |
| `perf/service-singletons` (#53) | Singletons for DDB/Echo/StorageQuota; updates 14 call sites | Was instantiating ~10 boto3 clients per cold start |
| `infra/ddb-on-demand-and-retain` (#54) | All 19 DDB tables → `BillingMode: PAY_PER_REQUEST` + `DeletionPolicy: Retain` + Streams on 4 tables | Removes the 1-RCU/1-WCU throttle; protects against stack-delete data loss |
| `infra/replace-in-memory-rate-limiter-with-apigw-throttling` (#56) | Deletes per-container rate limiter; adds API Gateway stage throttling (1000 RPS sustained, 5000 burst) | In-memory limiter was per-container = no real protection |

### 4.2 Open PRs (review + merge)

Stack order matters where noted. Each PR's `git diff origin/main` was
re-validated post-rebase to confirm only the intentional changes
remain.

| PR | Stacks on | What | Notes |
|---|---|---|---|
| `perf/openai-defaults-and-timeout` | main | gpt-4o → gpt-4o-mini; `max_tokens` 1000 → 450; 20 s timeout; `max_retries=1` | 60–80% reduction in chat OpenAI cost. Env vars `OPENAI_MODEL` / `OPENAI_MAX_TOKENS` override defaults. |
| `perf/drop-cognito-getuser-per-request` | main | Skips Cognito `GetUser` when JWT carries claims; 5-min in-process TTL cache on fallback | Single largest hard-quota cliff fix. `COGNITO_PROFILE_CACHE_TTL_SECONDS` (default 300) controls TTL. |
| `perf/apple-app-store-server-api-migration` | main | Drops deprecated `verifyReceipt`; uses App Store Server API; reuses aiohttp; non-blocking Google `.execute()` | Apple is turning off `verifyReceipt`. Env vars: `APPLE_APP_STORE_KEY_ID`, `APPLE_APP_STORE_ISSUER_ID`, `APPLE_APP_STORE_BUNDLE_ID`, and `APPLE_APP_STORE_PRIVATE_KEY` (PEM string) or `APPLE_APP_STORE_PRIVATE_KEY_PATH`. |
| `feat/apple-jws-signature-verification` | `perf/apple-app-store-server-api-migration` | Adds `app-store-server-library` SDK + Apple Root CA G3 PEM; verifies signedTransactionInfo x5c chain | Closes the entitlement-fraud gap. New env var: `APPLE_APP_STORE_APP_APPLE_ID` (numeric App ID from App Store Connect, required for production). |
| `perf/asyncopenai-and-concurrency-semaphore` | `perf/openai-defaults-and-timeout` | Switches to `AsyncOpenAI`; adds `asyncio.Semaphore` capped at `OPENAI_MAX_INFLIGHT` (default 16) | No more thread-pool occupancy. Semaphore protects only the `create()` call, not the full stream iteration. |
| `perf/async-wrap-sync-boto3` | `perf/service-singletons` (merged) | Wraps sync boto3 in `asyncio.to_thread` for Cognito/SES/SNS/StorageQuota; sets `Config(max_pool_connections=50, retries=adaptive)` | Sync calls in `async def` were blocking the event loop. SNS sync methods now emit `DeprecationWarning`. |
| `perf/ddb-long-lived-resource-and-quiz-cache` | `perf/service-singletons` (merged) | One long-lived aioboto3 resource per container (was per-call); 5-min cache for `get_quiz_questions` scan | Quiz scan was firing on every request. Resource entered once via `AsyncExitStack`. |
| `perf/echo-n-plus-1-pagination-long-lived` | `perf/service-singletons` (merged) | `get_received_echoes` parallel `asyncio.gather`; `get_user_echoes` BatchGetItem; opaque base64 cursor pagination on `/echoes`, `/echoes/inbox`, `/recipients`, `/guardians` | Power-user inbox went from ~10 s to ~one round-trip. Response envelope gains `next_cursor`. |

### 4.3 Merge order suggestion

The dependency graph:

```
main
├── perf/openai-defaults-and-timeout
│   └── perf/asyncopenai-and-concurrency-semaphore
├── perf/drop-cognito-getuser-per-request
├── perf/apple-app-store-server-api-migration
│   └── feat/apple-jws-signature-verification
├── perf/async-wrap-sync-boto3        ┐
├── perf/ddb-long-lived-resource-and-quiz-cache  ├── all base on already-merged perf/service-singletons
└── perf/echo-n-plus-1-pagination-long-lived     ┘
```

Merge any stack-leaf to main; the parent stack is already on main. The
only ordering constraint is **inside** the two stacks:
`perf/openai-defaults-and-timeout` before
`perf/asyncopenai-and-concurrency-semaphore`, and
`perf/apple-app-store-server-api-migration` before
`feat/apple-jws-signature-verification`.

---

## 5. Environment variables required for deploy

New or renamed env vars that must be in the secret store (Parameter
Store / Secrets Manager) per stage before merge:

### Required for `perf/apple-app-store-server-api-migration`
- `APPLE_APP_STORE_KEY_ID` — App Store Connect API key ID (10 chars,
  e.g. `ABC1234567`)
- `APPLE_APP_STORE_ISSUER_ID` — UUID from App Store Connect
- `APPLE_APP_STORE_BUNDLE_ID` — e.g. `com.mirrorcollective.app`
- `APPLE_APP_STORE_PRIVATE_KEY` (PEM string) **OR**
  `APPLE_APP_STORE_PRIVATE_KEY_PATH` (filesystem path inside Lambda)

### Additional for `feat/apple-jws-signature-verification`
- `APPLE_APP_STORE_APP_APPLE_ID` — numeric App ID from App Store
  Connect (not the bundle ID; this is Apple's internal identifier).
  REQUIRED in production. Sandbox tolerates `0`.

### Optional tuning knobs (already have safe defaults)
- `COGNITO_PROFILE_CACHE_TTL_SECONDS` (default `300`) — TTL for
  cached profile after a Cognito `GetUser` fallback.
- `OPENAI_MAX_INFLIGHT` (default `16`) — per-container semaphore cap
  on concurrent OpenAI calls.
- `OPENAI_MODEL` (default `gpt-4o-mini`) — chat model. Set to
  `gpt-4o` to revert to the larger model without code changes.
- `OPENAI_MAX_TOKENS` (default `450`) — chat response cap.
- `QUIZ_QUESTIONS_CACHE_TTL_SECONDS` (default `300`) — TTL for the
  static quiz-questions cache.
- `LEGACY_APPLE_VERIFYRECEIPT_ENABLED` (default `false`) — emergency
  rollback flag. Routes Apple validation through the deprecated
  `verifyReceipt` endpoint. Apple's deprecated endpoint still works
  (and verifies signatures on their side), so this is a safe fallback
  if the modern path has issues.

### Legacy env vars now safe to remove from the secret store
- `RATE_LIMIT_WINDOW_SECONDS`, `RATE_LIMIT_MAX_REQUESTS` (Wave 1A-D
  deleted the in-memory limiter; API Gateway stage throttling
  replaces them).
- `AWS_SNS_INTERVAL` (the in-Lambda APScheduler that consumed this
  was removed in `perf/lambda-init-cleanup`).

---

## 6. Deployment runbook

### 6.1 Pre-deploy checks

```bash
# 1. Confirm all required env vars are populated for the target stage.
serverless print --stage production-v2 | grep -E "APPLE_APP_STORE_|COGNITO_"

# 2. Confirm Lambda concurrent-execution quota is raised.
aws service-quotas get-service-quota \
  --service-code lambda \
  --quota-code L-B99A9384 \
  --region us-east-1 \
  --query 'Quota.Value'

# 3. Confirm SES is out of sandbox.
aws ses get-account-sending-enabled --region us-east-1
aws ses get-send-quota --region us-east-1

# 4. Confirm DDB tables are on-demand + Retain.
for t in $(aws dynamodb list-tables --region us-east-1 \
  --query "TableNames[?contains(@, 'mirror-collective-python-api')]" \
  --output text); do
  aws dynamodb describe-table --table-name "$t" --region us-east-1 \
    --query "Table.BillingModeSummary.BillingMode" --output text
done | sort -u  # should print only PAY_PER_REQUEST
```

### 6.2 Deploy order

Each merge is a separate deploy. Don't batch — single-variable changes
make rollback simple.

1. **Tier 0 + already-merged work** is in main. No action.
2. **`perf/openai-defaults-and-timeout`** — smallest, safest.
3. **`perf/asyncopenai-and-concurrency-semaphore`** — stacks on #2.
4. **`perf/drop-cognito-getuser-per-request`** — isolated to auth.
5. **`perf/async-wrap-sync-boto3`** — touches 4 service files.
6. **`perf/ddb-long-lived-resource-and-quiz-cache`** — DynamoDB
   internals.
7. **`perf/echo-n-plus-1-pagination-long-lived`** — biggest diff;
   most-reviewed.
8. **`perf/apple-app-store-server-api-migration`** — needs the env
   vars in §5 before deploy.
9. **`feat/apple-jws-signature-verification`** — needs
   `APPLE_APP_STORE_APP_APPLE_ID`.

### 6.3 Post-deploy verification (per PR)

After each `serverless deploy`, watch CloudWatch for 5 minutes:

```bash
# Lambda errors + throttles
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Errors \
  --dimensions Name=FunctionName,Value=mirror-collective-python-api-production-v2-api \
  --start-time $(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Sum \
  --region us-east-1

# DynamoDB throttle events on any table
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name UserErrors \
  --start-time $(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Sum \
  --region us-east-1
```

### 6.4 PR-specific smoke tests

| PR | Smoke test |
|---|---|
| `perf/openai-defaults-and-timeout` | Send one `/mirrorgpt/chat` request; verify reply quality acceptable on `gpt-4o-mini`. |
| `perf/asyncopenai-and-concurrency-semaphore` | Fire 5 concurrent `/chat` requests; verify all complete (no `RuntimeWarning: coroutine was never awaited` in logs). |
| `perf/drop-cognito-getuser-per-request` | Login → 5 authenticated requests; CloudWatch should show 1 Cognito `GetUser` call, not 5. |
| `perf/async-wrap-sync-boto3` | Trigger an action that uses each of: Cognito (refresh token), SES (forgot password), SNS (push token register), S3 (echo upload). Verify no `RuntimeWarning: coroutine was never awaited`. |
| `perf/ddb-long-lived-resource-and-quiz-cache` | Hit `/quiz/questions` twice in 5 minutes; first call should be a Scan in DDB metrics, second should be served from cache (no Scan). |
| `perf/echo-n-plus-1-pagination-long-lived` | Create 60+ echoes for a test user. `GET /api/echoes?limit=50` should return 50 + `next_cursor`. Pass the cursor as `?cursor=...` → next page. |
| `perf/apple-app-store-server-api-migration` | Validate one TestFlight purchase. Logs should show "Apple App Store Server API" path, not legacy `verifyReceipt`. |
| `feat/apple-jws-signature-verification` | Same TestFlight purchase. Logs should show no `JWSVerificationError`; one successful chain verification per validation. |

### 6.5 Rollback

Every PR is independently revertible (no DB migrations). Rollback
strategy:

1. **Code rollback:** `git revert <merge-commit>` → `serverless
   deploy`. Lambda swaps the alias within seconds.
2. **Apple IAP-specific:** set `LEGACY_APPLE_VERIFYRECEIPT_ENABLED=true`
   and redeploy. Routes through Apple's still-live deprecated endpoint
   (full server-side verification). No code revert needed.
3. **OpenAI model regression:** set `OPENAI_MODEL=gpt-4o` and
   `OPENAI_MAX_TOKENS=1000` in the deploy env. Takes effect on next
   cold start; no redeploy required.
4. **Cognito cache invalidation:** set
   `COGNITO_PROFILE_CACHE_TTL_SECONDS=1` (effectively disables the
   cache).

---

## 7. Operational considerations

### 7.1 What "cold start" looks like now

| Phase | Time | Notes |
|---|---|---|
| Lambda init (Mangum + FastAPI app construction) | ~200–400 ms | Was ~600–1000 ms before lifespan=off |
| First boto3/aioboto3 client init (cached after) | ~80 ms | Was paid per-request |
| First Cognito JWT verify (JWKS fetch + parse) | ~30 ms | Cached for container lifetime |
| First Apple JWS verification (SDK init + root cert parse) | ~50 ms | Cached for container lifetime |
| **Total cold-start floor** | **~700 ms – 1 s** at 1024 MB | Was ~3.5–5 s |

### 7.2 What the warm path costs now

For a typical `POST /api/mirrorgpt/chat` request (the heaviest hot path):

| Component | Cost |
|---|---|
| API Gateway JWT authorizer | ~10 ms (outside Lambda, billed separately) |
| Mangum + middleware stack | ~5 ms |
| `get_current_user` (JWKS-cached) | ~2 ms |
| `get_user_with_profile` (profile-cached) | ~0 ms cache hit / ~80 ms Cognito miss |
| DDB ops (10–14 per chat turn) | ~30 ms via long-lived aioboto3 + adaptive retries |
| OpenAI `chat.completions.create` (gpt-4o-mini, 450 max_tokens) | 1.5–4 s wall-clock |
| Conversation summarizer (async, post-response) | 0 ms blocking (fired with `asyncio.create_task`) |
| **TTFB total** | **~1.5–4 s** dominated by OpenAI |

### 7.3 Known not-yet-addressed bottlenecks

These were identified in the scaling audit but deferred to Wave 2 or 3:

| Item | Wave | Estimated effort | Why deferred |
|---|---|---|---|
| Chat response streaming via Lambda Function URL | 3 | 2–3 days | Architectural change (chat must move off API Gateway HTTP API) |
| Hot-partition sharding on `recipient-echoes-index` and `user-conversations-index` | 3 | 1 week (schema change + backfill) | Not hit yet; flag if any user has >1k inbound echoes or open conversations |
| Write coalescing in `mirror_orchestrator` (single `TransactWriteItems` for the dual user+assistant message save) | 3 | 1 day | Currently 6–8 WCU per chat turn — fine at projected scale |
| ElastiCache (Valkey) for hot reads (user profile, subscription tier, archetype profile) | 2 | 1 week | Per-container caches cover most of the win for now |
| CloudFront in front of echo media S3 bucket | 2 | 2 days | Mobile clients download echoes 1× per view; S3 bandwidth fine until 10k+ DAU |
| SQS for SES/SNS/summary refresh (move async work out of request path) | 2 | 1 week | Inline async fire-and-forget tasks work at current scale |
| DDB Streams consumers for fan-out (back-linking, archetype evolution) | 2 | 1 week | Streams are already enabled on 4 tables; no consumers yet — events drop into the void per spec, no cost |
| Idempotency keys on `POST /echoes`, `/recipients`, `/guardians`, `/verify-purchase` | 2 | 2 days | Mobile retry storms haven't been observed |

### 7.4 Things to watch in CloudWatch post-deploy

| Metric | Alarm threshold | What it tells you |
|---|---|---|
| `AWS/Lambda Throttles` (api function) | > 0 per 5-min window | Concurrent-execution quota too low; raise via Support case |
| `AWS/Lambda Duration` p99 (api function) | > 25 s | Approaching the 30 s timeout; investigate slow path |
| `AWS/Lambda Init Duration` p95 | > 1.5 s | Cold-start regression; check recent deploys |
| `AWS/DynamoDB UserErrors` (any table) | > 0 | Throttle or validation error; check `TableName` dimension |
| `AWS/ApiGateway 5XXError` rate | > 1% | Backend errors; check Lambda Errors metric and log group |
| `AWS/ApiGateway IntegrationLatency` p95 | > 5 s | Slow Lambda response; check chat path specifically |
| OpenAI 429 rate (logged from `OpenAIService`) | > 0 sustained | Approaching rate limit; either raise OpenAI tier or lower `OPENAI_MAX_INFLIGHT` |
| Apple `JWSVerificationError` rate | > 0 | Either a misconfigured `APPLE_APP_STORE_BUNDLE_ID`/`APP_APPLE_ID`, or a real attack — investigate immediately |

---

## 8. Wave 2 roadmap (not yet started)

When traffic justifies it, Wave 2 adds caching + async layers. Sequenced
to land independently:

1. **ElastiCache Serverless (Valkey)** — wrap user profile, subscription
   tier, archetype profile, quiz questions reads. Replaces per-container
   caches with cross-container cache; protects against the cold-container
   penalty on first request after a deploy.

2. **CloudFront + OAC in front of the echo-media S3 bucket** — sign
   CloudFront URLs instead of S3 presigned URLs. Cuts S3 egress and
   improves global latency for echo playback.

3. **SQS queues for SES/SNS/summary-refresh/fanout** — moves email
   sending, push notifications, summary regeneration, and DDB-Streams
   fan-out off the user-facing request path. Caps user-facing latency
   regardless of downstream third-party slowness.

4. **DDB Streams consumer Lambdas** — the 4 streams added in
   `infra/ddb-on-demand-and-retain` (`users`, `echoes`, `recipients`,
   `archetype-profiles`) currently have no consumers; events drop into
   the void (free). Consumers handle recipient back-linking on signup,
   archetype evolution, and mirror-moment dispatch — all of which are
   today synchronous in the user request.

5. **Idempotency-Key middleware + DDB table** — wired to the four
   mutating POSTs. Mobile retry over flaky networks creates duplicates
   today; an `Idempotency-Key` header + conditional `put_item` with TTL
   prevents them.

## 9. Wave 3 roadmap

Architectural splits, sequenced for when scale requires:

1. **Chat streaming Lambda + Function URL** — carve
   `/api/mirrorgpt/chat`, `/greeting`, `/signals`, `/insights` into its
   own Lambda deployed as a Function URL with `RESPONSE_STREAM` invoke
   mode. The existing API stays on API Gateway. Drops chat TTFB from
   ~1.5–4 s to ~400 ms.
2. **Hot-partition sharding** — add a synthetic suffix on the GSI hash
   key for top-1% recipients/users on `recipient-echoes-index` and
   `user-conversations-index`. Pre-empts the single-partition throttle
   ceiling at ~3,000 RCU + 1,000 WCU.
3. **Write coalescing in `mirror_orchestrator`** — replace the dual
   `put_message` + `update_conversation` pattern with a single
   `TransactWriteItems`. Halves WCU per chat turn.
4. **CloudWatch dashboards + alarms** — the metrics in §7.4 wired to
   `serverless.yml` resources so they survive redeploys.

---

## 10. Appendix — verification commands used during this initiative

These were the load-bearing CLI calls used to ground the audit in real
data. Keep them around for future capacity reviews:

```bash
# Account-wide Lambda concurrency quota
aws lambda get-account-settings --region us-east-1

# DDB capacity per table
aws dynamodb describe-table --table-name <table> --region us-east-1 \
  --query 'Table.{Name:TableName,Billing:BillingModeSummary.BillingMode,RCU:ProvisionedThroughput.ReadCapacityUnits,WCU:ProvisionedThroughput.WriteCapacityUnits,Items:ItemCount}'

# API Gateway authorizer state (security audit)
aws apigatewayv2 get-authorizers --api-id <api-id> --region us-east-1
aws apigatewayv2 get-routes --api-id <api-id> --region us-east-1 \
  --query "Items[?AuthorizationType!='NONE'].RouteKey"

# SES quotas
aws ses get-send-quota --region us-east-1

# CloudWatch metric pull for a specific function/metric
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --dimensions Name=FunctionName,Value=<function-name> \
  --start-time $(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Sum \
  --region us-east-1
```

---

## 11. References

- AWS Lambda quotas:
  https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html
- DynamoDB capacity modes:
  https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadWriteCapacityMode.html
- AWS API Gateway HTTP API JWT authorizers:
  https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html
- Apple App Store Server API:
  https://developer.apple.com/documentation/appstoreserverapi
- App Store Server Library (Python):
  https://github.com/apple/app-store-server-library-python
- OpenAI rate limits:
  https://platform.openai.com/docs/guides/rate-limits
- Mangum (Lambda → ASGI adapter):
  https://mangum.fastapiexpert.com/
