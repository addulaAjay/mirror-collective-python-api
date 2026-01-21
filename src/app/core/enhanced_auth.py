"""
Enhanced user dependencies for profile data
"""

import logging
from typing import Any, Dict

from fastapi import Depends, Request

from ..services.cognito_service import CognitoService
from .security import get_current_user

logger = logging.getLogger(__name__)


async def get_user_with_profile(
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    cognito_service: CognitoService = Depends(lambda: CognitoService()),
) -> Dict[str, Any]:
    """
    Enhanced user dependency that fetches full profile data from Cognito

    This handles the case where access tokens don't contain profile info
    by using Cognito's get_user API with the same access token
    """

    try:
        # Extract the access token from the request
        access_token = None
        auth_header = request.headers.get("authorization") or request.headers.get(
            "Authorization"
        )
        if auth_header and auth_header.lower().startswith("bearer "):
            access_token = auth_header.split(" ", 1)[1]

        if not access_token:
            logger.warning("No access token found in request for profile fetch")
            return _create_fallback_user(current_user)

        # Fetch full user profile from Cognito
        cognito_user = await cognito_service.get_user(access_token)

        if cognito_user and cognito_user.get("userAttributes"):
            attrs = cognito_user["userAttributes"]

            # Extract profile information from Cognito attributes
            given_name = attrs.get("given_name", "")
            family_name = attrs.get("family_name", "")
            email = attrs.get("email", current_user.get("email", ""))

            # Create display name
            display_name = f"{given_name} {family_name}".strip()
            if not display_name:
                display_name = attrs.get("name", "")
            if not display_name:
                display_name = attrs.get("preferred_username", "")
            if not display_name:
                if email:
                    display_name = email.split("@")[0]
                else:
                    display_name = f"User-{current_user['id'][:8]}"

            # Create enhanced user profile
            enhanced_user = {
                **current_user,
                "email": email,
                "firstName": given_name,
                "lastName": family_name,
                "name": display_name,
                "emailVerified": attrs.get("email_verified", "false").lower() == "true",
                "cognitoUsername": cognito_user.get("username", ""),
                "userStatus": cognito_user.get("userStatus", "UNKNOWN"),
            }

            logger.info(
                f"Enhanced user profile created for {email or current_user['id']}"
            )
            return enhanced_user

    except Exception as e:
        logger.warning(
            f"Failed to fetch Cognito profile for user {current_user['id']}: {str(e)}"
        )

    # Fallback to basic user data
    return _create_fallback_user(current_user)


def _create_fallback_user(current_user: Dict[str, Any]) -> Dict[str, Any]:
    """Create fallback user profile when Cognito data is unavailable"""

    fallback_name = (
        current_user.get("email", "").split("@")[0]
        if current_user.get("email")
        else f"User-{current_user['id'][:8]}"
    )

    return {
        **current_user,
        "name": fallback_name,
        "firstName": "",
        "lastName": "",
        "emailVerified": False,
        "cognitoUsername": "",
        "userStatus": "UNKNOWN",
    }
