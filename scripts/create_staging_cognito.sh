#!/usr/bin/env bash
#
# Create the STAGING Cognito user pool + app client that the API needs, then
# print the values to store as GitHub Actions secrets.
#
# The API depends on (verified in src/app/services/cognito_service.py):
#   - email-based sign-up/sign-in         (UsernameAttributes = email)
#   - self sign-up with email code verify (AutoVerifiedAttributes = email)
#   - standard attrs: given_name, family_name, phone_number
#   - custom attrs:   custom:deleted_at, custom:account_status  (soft-delete)
#   - app client WITH a secret            (SECRET_HASH on admin/refresh auth)
#   - auth flows: ADMIN_NO_SRP_AUTH + REFRESH_TOKEN_AUTH
#
# This mirrors prod's *functional* requirements. Before running, diff against
# the real prod pool so anything custom (Lambda triggers, MFA, password policy,
# email/SES config) is matched:
#
#   aws cognito-idp describe-user-pool        --user-pool-id "$PROD_POOL_ID"
#   aws cognito-idp describe-user-pool-client --user-pool-id "$PROD_POOL_ID" \
#       --client-id "$PROD_CLIENT_ID"
#
# Requires: awscli v2 (configured), jq, and gh (optional, for pushing secrets).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
POOL_NAME="${POOL_NAME:-mirror-collective-staging}"
CLIENT_NAME="${CLIENT_NAME:-mirror-collective-staging-app}"

echo "Creating Cognito user pool '${POOL_NAME}' in ${REGION}..."

POOL_ID=$(aws cognito-idp create-user-pool \
  --region "$REGION" \
  --pool-name "$POOL_NAME" \
  --username-attributes email \
  --auto-verified-attributes email \
  --account-recovery-setting 'RecoveryMechanisms=[{Priority=1,Name=verified_email}]' \
  --admin-create-user-config 'AllowAdminCreateUserOnly=false' \
  --email-configuration 'EmailSendingAccount=COGNITO_DEFAULT' \
  --policies 'PasswordPolicy={MinimumLength=8,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=false}' \
  --schema \
    'Name=given_name,AttributeDataType=String,Mutable=true,Required=false' \
    'Name=family_name,AttributeDataType=String,Mutable=true,Required=false' \
    'Name=phone_number,AttributeDataType=String,Mutable=true,Required=false' \
    'Name=deleted_at,AttributeDataType=String,Mutable=true,Required=false' \
    'Name=account_status,AttributeDataType=String,Mutable=true,Required=false' \
  --query 'UserPool.Id' --output text)

echo "  -> Pool ID: ${POOL_ID}"

echo "Creating app client '${CLIENT_NAME}' (with secret)..."

CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --region "$REGION" \
  --user-pool-id "$POOL_ID" \
  --client-name "$CLIENT_NAME" \
  --generate-secret \
  --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH ALLOW_USER_PASSWORD_AUTH \
  --access-token-validity 1 \
  --id-token-validity 1 \
  --refresh-token-validity 30 \
  --token-validity-units 'AccessToken=hours,IdToken=hours,RefreshToken=days' \
  --prevent-user-existence-errors ENABLED \
  --query 'UserPoolClient.ClientId' --output text)

CLIENT_SECRET=$(aws cognito-idp describe-user-pool-client \
  --region "$REGION" \
  --user-pool-id "$POOL_ID" \
  --client-id "$CLIENT_ID" \
  --query 'UserPoolClient.ClientSecret' --output text)

echo "  -> Client ID: ${CLIENT_ID}"
echo ""
echo "============================================================"
echo "Staging Cognito created. Set these as GitHub Actions secrets:"
echo "  STAGING_COGNITO_USER_POOL_ID = ${POOL_ID}"
echo "  STAGING_COGNITO_CLIENT_ID    = ${CLIENT_ID}"
echo "  STAGING_COGNITO_CLIENT_SECRET = (printed below, keep private)"
echo "============================================================"
echo ""

# Push straight to GitHub if `gh` is available and authenticated.
if command -v gh >/dev/null 2>&1; then
  read -r -p "Push these to GitHub repo secrets now with gh? [y/N] " ans
  if [[ "${ans:-N}" =~ ^[Yy]$ ]]; then
    gh secret set STAGING_COGNITO_USER_POOL_ID  --body "$POOL_ID"
    gh secret set STAGING_COGNITO_CLIENT_ID     --body "$CLIENT_ID"
    gh secret set STAGING_COGNITO_CLIENT_SECRET --body "$CLIENT_SECRET"
    echo "Secrets pushed. (Client secret was not printed to the terminal.)"
    exit 0
  fi
fi

echo "STAGING_COGNITO_CLIENT_SECRET = ${CLIENT_SECRET}"
echo ""
echo "Remaining STAGING_* secrets still to add (see docs/RELEASE.md):"
echo "  STAGING_OPENAI_API_KEY, STAGING_SES_SENDER_EMAIL, STAGING_SNS_TOPIC_ARN,"
echo "  STAGING_APP_URL, STAGING_SHARE_TOKEN_SECRET (new random), STAGING_SHARE_BASE_URL,"
echo "  STAGING_APPLE_APP_STORE_{KEY_ID,ISSUER_ID,PRIVATE_KEY,APP_APPLE_ID}"
