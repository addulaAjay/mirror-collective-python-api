import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from .exceptions import AuthenticationError, InvalidTokenError, TokenExpiredError

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"

# HTTP Bearer token scheme
security = HTTPBearer(auto_error=False)


def decode_cognito_jwt(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode and validate Cognito JWT token
    In production, this would typically be handled by API Gateway Cognito Authorizer
    This is primarily for local development and fallback scenarios
    """
    try:
        # For development/testing: decode without signature verification
        # In production, API Gateway handles JWT verification
        is_development = os.getenv("NODE_ENV") in ["development", "test"]

        if is_development:
            logger.debug("🔧 Development: Decoding JWT without signature verification")
            # Decode without verification for development
            payload = jwt.get_unverified_claims(token)
        else:
            # In production, we should trust API Gateway's validation
            # But decode payload for user info extraction
            payload = jwt.get_unverified_claims(token)
            logger.debug("✅ Production: Extracting claims from pre-validated JWT")

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
    return {
        "id": payload.get("sub"),
        "email": payload.get("email"),
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


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    """
    Dependency to get current authenticated user from JWT token
    Extracts and validates user information from Cognito JWT tokens
    """
    is_development = (
        os.getenv("NODE_ENV") in ["development", "test"]
        or os.getenv("DEBUG", "false").lower() == "true"
    )

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
            f"🔧 Development: Decoded JWT token for user: {user_profile.get('email', 'unknown')}"
        )
    else:
        logger.debug(
            f"✅ Production: User authenticated: {user_profile.get('email', 'unknown')}"
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
