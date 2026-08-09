# Release & Environments

Two isolated AWS environments, one Serverless service (`mirror-collective-python-api`).

| Env | Serverless stage | Deploy trigger | Cognito pool | API URL |
|-----|------------------|----------------|--------------|---------|
| **staging** | `staging` | push to `develop` | staging pool (`STAGING_COGNITO_*`) | `<staging-id>.execute-api.us-east-1.amazonaws.com` |
| **production** | `production-v2` | push a `v*` tag **+ manual approval** | prod pool (`PROD_COGNITO_*`) | `https://ct3onxgeol.execute-api.us-east-1.amazonaws.com` |

Everything except Cognito/SES is created per-stage by CloudFormation (tables, buckets, IAM roles are suffixed with the stage name), so the two environments share **no** DynamoDB tables, S3 buckets, or SNS platform apps. Cognito user pools are external and separate per env.

---

## Day-to-day release flow

```
work on a feature branch
      │  PR → merged to
      ▼
   develop  ──(CI auto)──►  deploy to STAGING
      │                         │
      │                    validate on staging (smoke tests below)
      │
      ├─ merge develop → main   (main == validated code)
      ▼
   git tag v1.2.3 && git push origin v1.2.3
      │
      ▼
   CI builds, then PAUSES for approval on the `production` Environment
      │  (a required reviewer clicks "Approve and deploy")
      ▼
   deploy to PRODUCTION (stage production-v2)
```

Tag from a commit that has already been validated on staging (i.e. tag `main`
after `develop` has been deployed and smoke-tested).

### Cutting a production release

```bash
git checkout main && git pull
git tag v1.2.3            # semver; must start with "v"
git push origin v1.2.3    # triggers the tag-gated prod pipeline
```

Then open the GitHub Actions run and **approve** the `deploy-production` job.

### Rollback

Re-tag the last-good commit and push a new tag (e.g. `v1.2.4` pointing at the
previous release commit), then approve. Serverless/CloudFormation redeploys that
code. (There is no automatic rollback; forward-fix via a new tag.)

---

## One-time setup (AWS + GitHub console — required before the first staging deploy)

These cannot be done from the repo; do them once.

### 1. Create the staging Cognito user pool
Mirror the prod pool's config (sign-in with email, email verification, the same
app-client settings incl. a client secret, and any Lambda triggers). Capture:
- User Pool ID → `STAGING_COGNITO_USER_POOL_ID`
- App Client ID → `STAGING_COGNITO_CLIENT_ID`
- App Client secret → `STAGING_COGNITO_CLIENT_SECRET`

### 2. Create staging SNS/APNs platform apps (push)
```bash
# --stage staging maps to APNs SANDBOX (matches Debug/dev builds).
python scripts/setup_apns_platform_app.py --stage staging --p8 ./AuthKey_XXXXXXXXXX.p8
```
This writes the SSM params (`/mirror-collective/staging/sns/ios-app-arn`, …) the
deploy reads.

### 3. Add GitHub repo secrets (`STAGING_*`)
Settings → Secrets and variables → Actions. The deploy-staging job reads:
`STAGING_COGNITO_USER_POOL_ID`, `STAGING_COGNITO_CLIENT_ID`,
`STAGING_COGNITO_CLIENT_SECRET`, `STAGING_OPENAI_API_KEY`,
`STAGING_SES_SENDER_EMAIL`, `STAGING_SNS_TOPIC_ARN`, `STAGING_APP_URL`,
`STAGING_SHARE_TOKEN_SECRET`, `STAGING_SHARE_BASE_URL`,
`STAGING_APPLE_APP_STORE_KEY_ID`, `STAGING_APPLE_APP_STORE_ISSUER_ID`,
`STAGING_APPLE_APP_STORE_PRIVATE_KEY`, `STAGING_APPLE_APP_STORE_APP_APPLE_ID`.
(OpenAI / Apple keys may reuse the prod values; `SHARE_TOKEN_SECRET` must be a
new random secret; `SHARE_BASE_URL`/`APP_URL` point at the staging API URL.)

### 4. Create the `production` GitHub Environment (approval gate)
Settings → Environments → New environment → **production** → enable
**Required reviewers** (add yourself). The `deploy-production` job references
`environment: production`, so it will pause for approval before every prod deploy.

### 5. First staging deploy
Either push to `develop`, or run locally once to bootstrap the stack:
```bash
export STAGE=staging COGNITO_USER_POOL_ID=... COGNITO_CLIENT_ID=... ...
serverless deploy --stage staging --verbose
aws s3 sync emails/email-assets s3://mirror-collective-email-assets-staging/ \
  --cache-control "public, max-age=31536000"
```
Note the printed API Gateway URL — that is the staging base URL.

---

## App wiring

- **Production**: no change — the app hardcodes the prod URL as `PROD_HOST` and
  release/TestFlight builds always use it.
- **Staging**: dev builds point at staging via an env override:
  ```bash
  MIRROR_API_BASE_URL=https://<staging-id>.execute-api.us-east-1.amazonaws.com
  ```
  (see `src/constants/config/config.ts` — `HOST_OVERRIDE`). Release builds ignore
  this override, so a staging URL can never ship to the App Store.

---

## Smoke test (run on staging after each deploy)

1. Sign up → receive verification email → verify → log in.
2. MirrorGPT: send a message, get a reply.
3. Echo Vault: upload an echo (image/audio) → confirm it lands in the
   `-staging` bucket/table.
4. IAP (sandbox): start trial / purchase → verify activation.
5. Delete account → confirm you can no longer log in.
6. Confirm all writes landed in `*-staging` tables and **nothing** touched prod.
