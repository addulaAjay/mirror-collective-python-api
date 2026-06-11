# Push Notifications (APNs / SNS) — environment setup

Soul Pings and all iOS push are delivered through **AWS SNS platform
applications** (one per APNs environment). SNS platform apps hold your APNs
signing key and have **no CloudFormation resource**, so they're created once per
environment by a script and their ARNs are stored in **SSM Parameter Store**.
`serverless.yml` then resolves the ARN per stage at deploy time:

```yaml
SNS_IOS_APP_ARN: ${ssm:/mirror-collective/${self:provider.stage}/sns/ios-app-arn, ''}
SNS_ANDROID_APP_ARN: ${ssm:/mirror-collective/${self:provider.stage}/sns/android-app-arn, ''}
```

(The empty-string fallback means a deploy still succeeds before the param exists
— push just won't deliver until it's set.)

## The matching rule (get this wrong = silent non-delivery)

A device's APNs token is bound to the APNs **environment** baked into the build,
which must match the SNS platform app the backend registers it against:

| App build | `aps-environment` | APNs env | Deploy stage | SNS platform | API host |
|---|---|---|---|---|---|
| Debug (Xcode/Metro) | `development` | sandbox | `staging` | `APNS_SANDBOX` | staging gateway |
| TestFlight / App Store | `production` | production | `prod*` | `APNS` | prod gateway (`ct3onxgeol…`) |

- iOS entitlement today: `aps-environment = development` (dev builds → sandbox).
  Release builds get `production` injected from the provisioning profile.
- The RN app pins **Release/TestFlight → production API** (`src/constants/config/config.ts`),
  so a production token is never registered against staging.

## One-time setup per environment

Token-based APNs auth (a non-expiring `.p8` key from the Apple Developer portal →
Keys → create a key with **Apple Push Notifications service (APNs)** enabled).

```bash
export APNS_KEY_ID=ABC123DEFG          # the key's Key ID
export APNS_TEAM_ID=TEAMID1234         # Apple Developer Team ID
export APNS_BUNDLE_ID=com.themirrorcollective.mirror   # the iOS bundle id

# staging → APNS_SANDBOX
python scripts/setup_apns_platform_app.py --stage staging --p8 ./AuthKey_ABC123DEFG.p8

# production → APNS
python scripts/setup_apns_platform_app.py --stage prod --p8 ./AuthKey_ABC123DEFG.p8
```

The script creates the platform app with the right `APNS`/`APNS_SANDBOX` type and
writes the ARN to `/mirror-collective/<stage>/sns/ios-app-arn`. Re-running updates
the credentials + SSM value in place. Requires AWS creds with
`sns:CreatePlatformApplication` + `ssm:PutParameter`.

Then deploy as usual — `SNS_IOS_APP_ARN` is wired automatically:

```bash
serverless deploy --stage staging
serverless deploy --stage prod
```

## Verify
- `aws ssm get-parameter --name /mirror-collective/staging/sns/ios-app-arn`
- After a deploy, the `api` + `soulPingDispatch` Lambdas show `SNS_IOS_APP_ARN`
  populated in their env.
- On a **real device** (the simulator can't get an APNs token): log in → confirm
  `POST /api/register-device` creates an SNS endpoint → `POST /api/soul-pings/test`
  → push arrives → tap opens the Soul Ping screen.

## Android (parked)
Focus is iOS-only right now. The `SNS_ANDROID_APP_ARN` env + SSM path
(`/mirror-collective/<stage>/sns/android-app-arn`) are wired the same way, but
creating the FCM platform app + a branded notification channel (Notifee) is
deferred. The script currently sets up iOS only.
