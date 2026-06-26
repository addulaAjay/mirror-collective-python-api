import logging
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional

import requests
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .exceptions import AuthenticationError, InvalidTokenError, TokenExpiredError
from .log_sanitize import mask_email
from .request_context import set_user_id

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"

# HTTP Bearer token scheme
security = HTTPBearer(auto_error=False)


def _is_dev_environment() -> bool:
    """Single source of truth for the dev/test bypass.

    Gated strictly on ENVIRONMENT to avoid auth-bypass risk: previously this
    also honored NODE_ENV/DEBUG, so a stray `DEBUG=true` env var in a
    deployed stack would grant `mock-user-123` to any tokenless request.
    The rest of the codebase already uses ENVIRONMENT (see logging_config.py,
    echo_v1_routes.py); this aligns security.py with that convention.
    """
    return os.getenv("ENVIRONMENT", "").lower() in {"development", "test"}


# --------------------------------------------------------------------------- #
# JWKS fetch + cache (defense-in-depth on top of API Gateway JWT authorizer)
# --------------------------------------------------------------------------- #
# Cognito rotates signing keys rarely. We fetch the JWKS once per warm Lambda
# container and refresh on `kid` cache miss. The lock prevents concurrent
# refresh from racing on cold-start fan-in.
_JWKS_CACHE: Optional[Dict[str, Any]] = None
_JWKS_LOCK = Lock()


def _jwks_url() -> str:
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    pool_id = os.getenv("COGNITO_USER_POOL_ID", "")
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"


def _expected_issuer() -> str:
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    pool_id = os.getenv("COGNITO_USER_POOL_ID", "")
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"


def _fetch_jwks(force: bool = False) -> Dict[str, Any]:
    global _JWKS_CACHE
    cached = _JWKS_CACHE
    if cached is not None and not force:
        return cached
    with _JWKS_LOCK:
        cached = _JWKS_CACHE
        if cached is not None and not force:
            return cached
        resp = requests.get(_jwks_url(), timeout=5)
        resp.raise_for_status()
        fresh: Dict[str, Any] = resp.json()
        _JWKS_CACHE = fresh
        return fresh


def _find_key(jwks: Dict[str, Any], kid: str) -> Optional[Dict[str, Any]]:
    for k in jwks.get("keys", []):
        if k.get("kid") == kid:
            return k
    return None


def _verify_jwt_signature(token: str) -> Dict[str, Any]:
    """Verify a Cognito JWT signature against the User Pool JWKS.

    Cognito access tokens carry `client_id` (no `aud`); ID tokens carry `aud`.
    We disable jose's `aud` check and validate the client claim explicitly so
    both token types are accepted.
    """
    expected_client_id = os.getenv("COGNITO_CLIENT_ID", "")
    if not expected_client_id:
        raise InvalidTokenError("COGNITO_CLIENT_ID is not configured")

    try:
        headers = jwt.get_unverified_header(token)
    except JWTError as e:
        raise InvalidTokenError(f"Malformed token header: {e}") from e

    kid = headers.get("kid")
    if not kid:
        raise InvalidTokenError("Token missing 'kid' header")

    jwks = _fetch_jwks()
    key = _find_key(jwks, kid)
    if key is None:
        # Signing key rotated since the last cache refresh — pull JWKS once
        # more before giving up.
        jwks = _fetch_jwks(force=True)
        key = _find_key(jwks, kid)
    if key is None:
        raise InvalidTokenError("Token signed by an unknown key")

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            issuer=_expected_issuer(),
            options={"verify_aud": False},
        )
    except JWTError as e:
        raise InvalidTokenError(f"JWT signature verification failed: {e}") from e

    token_use = payload.get("token_use")
    if token_use == "access":
        if payload.get("client_id") != expected_client_id:
            raise InvalidTokenError("Access token client_id mismatch")
    elif token_use == "id":
        if payload.get("aud") != expected_client_id:
            raise InvalidTokenError("ID token aud mismatch")
    else:
        raise InvalidTokenError(f"Unexpected token_use: {token_use!r}")

    return payload


def decode_cognito_jwt(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode and validate Cognito JWT token
    In production, this would typically be handled by API Gateway Cognito Authorizer
    This is primarily for local development and fallback scenarios
    """
    try:
        # In dev/test, tests supply synthetic tokens that aren't signed by
        # Cognito; we skip signature verification there. In every other env
        # the JWT is signature-verified via JWKS so a forged token cannot
        # reach a protected route even if the API Gateway authorizer is ever
        # misconfigured or removed.
        if _is_dev_environment():
            logger.debug("🔧 Development: Decoding JWT without signature verification")
            payload = jwt.get_unverified_claims(token)
        else:
            payload = _verify_jwt_signature(token)
            logger.debug("✅ Production: JWT signature verified")

        # Basic validation
        now = datetime.now(timezone.utc)

        # Check expiration
        exp = payload.get("exp")
        if exp and datetime.fromtimestamp(exp, tz=timezone.utc) < now:
            logger.warning("Token has expired")
            return None

        # Check not before
        nbf = payload.get("nbf")
        if nbf and datetime.fromtimestamp(nbf, tz=timezone.utc) > now:
            logger.warning("Token not yet valid")
            return None

        # Check token use (should be 'access' for access tokens)
        token_use = payload.get("token_use")
        if token_use and token_use not in ["access", "id"]:
            logger.warning(f"Invalid token use: {token_use}")
            return None

        return payload

    except JWTError as e:
        logger.warning(f"JWT decode error: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error decoding JWT: {str(e)}")
        return None


def map_claims_to_profile(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map Cognito JWT claims to user profile format"""
    groups: List[str] = payload.get("cognito:groups", []) or []

    # Email comes only from the 'email' claim. Cognito ACCESS tokens do not
    # carry it; ID tokens do. Do NOT fall back to sub/username here — that
    # produced a UUID-shaped string in profile["email"] and silently broke
    # downstream lookups (e.g. inbox match by email). Callers that need the
    # email when only an access token was sent must resolve it from the users
    # table by sub.
    email = payload.get("email")

    token_use = payload.get("token_use")
    if not email and token_use == "access":  # nosec B105 — 'access' is a token type
        logger.warning(
            "⚠️  No email claim in access token. "
            "Endpoints that need the user's email must resolve it via the "
            "users table by sub. Available claims: " + ", ".join(payload.keys())
        )

    profile = {
        "id": payload.get("sub"),
        "email": email,
        "firstName": payload.get("given_name") or "",
        "lastName": payload.get("family_name") or "",
        "provider": "cognito",
        "emailVerified": bool(payload.get("email_verified", False)),
        "createdAt": datetime.fromtimestamp(
            payload.get("auth_time", payload.get("iat", 0)), tz=timezone.utc
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "roles": groups or ["basic_user"],
        "permissions": [],
        "features": [],
    }

    # Bind the user id into the logging context so every subsequent log line in
    # this request is attributable to the user (greppable when they report an
    # issue). All auth paths return through here.
    sub = payload.get("sub")
    set_user_id(sub if isinstance(sub, str) else None)

    return profile


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    """
    Dependency to get current authenticated user from JWT token
    Extracts and validates user information from Cognito JWT tokens
    """
    is_development = _is_dev_environment()

    # Try to get token from Authorization header
    token = None
    if credentials:
        token = credentials.credentials
    else:
        # Fallback: check manual header parsing (for compatibility)
        auth = request.headers.get("authorization") or request.headers.get(
            "Authorization"
        )
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1]

    # Handle development mode with mock users
    if is_development and not token:
        logger.info("🔧 Development: Using mock user (no auth header)")
        mock_claims = {
            "sub": "mock-user-123",
            "email": "mock@localhost.dev",
            "email_verified": True,
            "given_name": "Mock",
            "family_name": "User",
            "name": "Mock User",
            "cognito:username": "mockuser",
            "cognito:groups": ["basic_user"],
            "aud": "mock-client-id",
            "token_use": "access",
            "auth_time": int(datetime.now(timezone.utc).timestamp()),
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/mock-pool",
            "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
            "iat": int(datetime.now(timezone.utc).timestamp()),
        }
        return map_claims_to_profile(mock_claims)

    if not token:
        raise AuthenticationError("Missing bearer token")

    # Try to decode JWT
    claims = None
    try:
        claims = decode_cognito_jwt(token)
    except Exception as e:
        if is_development:
            logger.warning(
                f"🔧 Development: JWT decode failed, using fallback mock user: {str(e)}"
            )
            # Fallback to mock user in development
            fallback_claims = {
                "sub": "fallback-user-456",
                "email": "fallback@localhost.dev",
                "email_verified": True,
                "given_name": "Fallback",
                "family_name": "User",
                "name": "Fallback User",
                "cognito:username": "fallbackuser",
                "cognito:groups": ["basic_user"],
                "aud": "fallback-client-id",
                "token_use": "access",
                "auth_time": int(datetime.now(timezone.utc).timestamp()),
                "iss": "https://cognito-idp.us-east-1.amazonaws.com/fallback-pool",
                "exp": int(datetime.now(timezone.utc).timestamp()) + 3600,
                "iat": int(datetime.now(timezone.utc).timestamp()),
            }
            return map_claims_to_profile(fallback_claims)
        else:
            raise InvalidTokenError("Invalid or expired token")

    if not claims:
        raise InvalidTokenError("Invalid or expired token")

    # Map claims to user profile
    user_profile = map_claims_to_profile(claims)

    if is_development:
        logger.info(
            f"🔧 Development: Decoded JWT token for user: "
            f"{mask_email(user_profile.get('email'))}"
        )
    else:
        logger.debug(
            f"✅ Production: User authenticated: "
            f"{mask_email(user_profile.get('email'))}"
        )

    return user_profile


async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[Dict[str, Any]]:
    """
    Dependency to optionally get current authenticated user (for public endpoints)
    """
    try:
        return await get_current_user(request, credentials)
    except (AuthenticationError, InvalidTokenError, TokenExpiredError):
        return None


# Alias for consistency
get_current_user_optional = get_optional_user
