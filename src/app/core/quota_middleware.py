"""
Quota enforcement middleware for Echo Vault uploads
"""

import logging
import os

import jwt
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from ..services.dynamodb_service import DynamoDBService
from ..services.storage_quota_service import StorageQuotaService

logger = logging.getLogger(__name__)


class QuotaEnforcementMiddleware(BaseHTTPMiddleware):
    """
    Enforce storage quotas before Echo Vault uploads
    """

    def __init__(self, app):
        super().__init__(app)
        self.dynamodb_service = DynamoDBService()
        self.quota_service = StorageQuotaService(self.dynamodb_service)

    def _extract_user_id_from_token(self, request: Request) -> str | None:
        """
        Extract user ID from JWT token in Authorization header

        Args:
            request: FastAPI request object

        Returns:
            User ID (Cognito sub) if token is valid, None otherwise
        """
        try:
            # Get Authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                return None

            # Extract token
            token = auth_header.split(" ")[1]

            # Decode JWT without verification (verification happens in auth dependency)
            # We just need the user ID for quota check
            decoded = jwt.decode(token, options={"verify_signature": False})

            # Cognito tokens have 'sub' claim
            user_id = decoded.get("sub")
            return user_id

        except Exception as e:
            logger.debug(f"Could not extract user ID from token: {e}")
            return None

    async def dispatch(self, request: Request, call_next):
        """
        Check quota before allowing Echo Vault uploads
        """
        # Only check for Echo Vault upload endpoints
        if request.url.path.startswith("/api/echoes") and request.method in [
            "POST",
            "PUT",
        ]:
            # Extract user ID from JWT token
            user_id = self._extract_user_id_from_token(request)

            if user_id:
                try:
                    # Check quota status
                    quota_status = await self.quota_service.check_quota_exceeded(
                        user_id
                    )

                    # Block if quota exceeded
                    if quota_status["exceeded"]:
                        return JSONResponse(
                            status_code=402,  # Payment Required
                            content={
                                "error": "storage_quota_exceeded",
                                "message": "Storage quota exceeded. Please upgrade your plan.",
                                "usage_gb": quota_status["usage_gb"],
                                "quota_gb": quota_status["quota_gb"],
                            },
                        )

                    # Attach quota info to request for logging
                    request.state.quota_status = quota_status

                except Exception as e:
                    logger.error(f"Error checking quota for user {user_id}: {e}")
                    # Fail open to avoid blocking legitimate users
                    pass

        # Process request
        response = await call_next(request)
        return response
