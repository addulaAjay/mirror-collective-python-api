"""
Chat controller - handles HTTP requests for mirror chat conversations
"""

import logging
from typing import Any, Dict
from fastapi import HTTPException
from ..core.exceptions import InternalServerError
from ..api.models import MirrorChatRequest, MirrorChatResponse
from ..services.openai_service import ChatMessage, OpenAIService
from ..services.user_service import UserService
from ..use_cases.mirror_chat_use_case import MirrorChatRequest as UseCaseRequest
from ..use_cases.mirror_chat_use_case import MirrorChatUseCase

logger = logging.getLogger(__name__)


class ChatController:
    """
    HTTP controller for chat-related endpoints with dependency injection
    """

    def __init__(self):
        """Initialize controller with injected services"""
        chat_service = OpenAIService()
        self.use_case = MirrorChatUseCase(chat_service)
        self.user_service = UserService()

    async def handle_chat(
        self, req: MirrorChatRequest, current_user: Dict[str, Any]
    ) -> MirrorChatResponse:
        """
        Process incoming mirror chat requests and return AI-generated responses

        Args:
            req: The validated chat request from the API layer
            current_user: The authenticated user information from JWT token

        Returns:
            MirrorChatResponse: Structured response with AI reply and metadata

        Raises:
            Exception: Re-raises any service-level errors for error middleware handling
        """
        try:
            # Log the complete incoming request for debugging
            logger.info(f"Complete request from UI: {req.model_dump()}")
            logger.info(f"Request message: '{req.message}'")
            logger.info(f"Request userName: '{req.userName}'")
            logger.info(f"Request conversationHistory: {req.conversationHistory}")

        except Exception as e:
            logger.error(f"Error logging request: {e}")

        try:
            # Get user ID from current_user (from JWT token)
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                raise ValueError("User ID not found in token")

            # Get user profile from DynamoDB (should exist from registration)
            # If not found, create with available user data (fallback for existing users)
            user_profile = await self.user_service.get_user_profile(user_id)
            if not user_profile:
                logger.warning(f"User profile not found in DynamoDB for user: {user_id}. Creating fallback profile.")
                # Create a basic profile for existing users who might not have been migrated yet
                raise InternalServerError(f"User profile not found for user: {user_id}")

            # Extract user name with intelligent fallbacks
            user_name = req.userName  # Explicit name from request
            if not user_name:
                user_name = user_profile.chat_name  # From user profile
            if not user_name:
                # Fallback to current_user data
                user_name = (
                    current_user.get("firstName")
                    or current_user.get("given_name")
                    or current_user.get("name")
                )
                if user_name and user_name.strip():
                    user_name = user_name.strip()
                else:
                    user_name = None
            if not user_name:
                # Last resort: email username
                email = user_profile.email or current_user.get("email")
                if email:
                    user_name = email.split("@")[0]

            # Record chat activity for analytics
            await self.user_service.record_chat_activity(user_id)

            # Log user name resolution for debugging
            logger.info(
                f"User ID: {user_id} | Profile: {user_profile.full_name if user_profile else 'None'}"
            )
            logger.info(
                f"User name resolved: '{user_name}' | Request userName: '{req.userName}' | Profile chat_name: '{user_profile.chat_name if user_profile else 'None'}'"
            )

            # Transform API conversation history to use case format
            conversation_history = []
            if req.conversationHistory:
                conversation_history = [
                    ChatMessage(role=turn.role, content=turn.content)
                    for turn in req.conversationHistory
                ]

            # Create use case request with all context including authenticated user
            use_case_request = UseCaseRequest(
                message=req.message,
                conversation_history=conversation_history,
                user_name=user_name,
            )

            # Execute business logic through use case
            result = await self.use_case.execute(use_case_request)

            # Return structured API response
            return MirrorChatResponse(success=True, data=result.to_dict())

        except Exception as err:
            logger.error(f"Error processing mirror chat request: {str(err)}")
            raise
