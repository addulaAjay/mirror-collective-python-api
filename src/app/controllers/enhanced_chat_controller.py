"""
Enhanced chat controller with persistent conversation management
Production-ready implementation with comprehensive error handling
"""

import logging
import json
from typing import Any, Dict, Optional, AsyncGenerator
from fastapi import HTTPException

from ..core.exceptions import InternalServerError, NotFoundError, ValidationError
from ..api.models import (
    EnhancedMirrorChatRequest, 
    EnhancedMirrorChatResponse,
    ConversationListResponse,
    ConversationDetailResponse,
    ConversationManagementRequest,
    UpdateConversationTitleRequest
)
from ..services.openai_service import OpenAIService
from ..services.user_service import UserService
from ..use_cases.enhanced_mirror_chat_use_case import (
    EnhancedMirrorChatUseCase, 
    ConversationManagementUseCase,
    EnhancedMirrorChatRequest as UseCaseRequest
)

logger = logging.getLogger(__name__)


class EnhancedChatController:
    """
    Enhanced HTTP controller for chat-related endpoints with conversation management
    Handles both legacy and new conversation-aware endpoints
    """

    def __init__(self):
        """Initialize controller with injected services"""
        chat_service = OpenAIService()
        self.chat_use_case = EnhancedMirrorChatUseCase(chat_service)
        self.conversation_use_case = ConversationManagementUseCase()
        self.user_service = UserService()

    async def handle_enhanced_chat(
        self, 
        req: EnhancedMirrorChatRequest, 
        current_user: Dict[str, Any]
    ) -> EnhancedMirrorChatResponse:
        """
        Process enhanced mirror chat requests with conversation management

        Args:
            req: The validated enhanced chat request from the API layer
            current_user: The authenticated user information from JWT token

        Returns:
            EnhancedMirrorChatResponse: Structured response with AI reply and conversation metadata

        Raises:
            HTTPException: For various error conditions (400, 404, 500)
        """
        try:
            # Extract user ID from JWT token
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                logger.error("User ID not found in JWT token")
                raise HTTPException(status_code=400, detail="User ID not found in token")

            # Get user profile for personalization
            user_profile = await self.user_service.get_user_profile(user_id)
            if not user_profile:
                logger.warning(f"User profile not found for user: {user_id}")
                raise HTTPException(status_code=404, detail=f"User profile not found for user: {user_id}")

            # Resolve user name with intelligent fallbacks
            user_name = self._resolve_user_name(req.userName, user_profile, current_user)

            logger.info(
                f"Processing enhanced chat request - User: {user_id}, "
                f"ConversationId: {req.conversationId}, "
                f"CreateNew: {req.createNewConversation}, "
                f"UserName: {user_name}"
            )

            # Create use case request
            use_case_request = UseCaseRequest(
                message=req.message,
                user_id=user_id,
                conversation_id=req.conversationId,
                user_name=user_name,
                create_new_conversation=req.createNewConversation
            )

            # Execute enhanced chat use case
            result = await self.chat_use_case.execute(use_case_request)

            # Return structured API response
            return EnhancedMirrorChatResponse(
                success=True, 
                data=result.to_dict()
            )

        except ValidationError as e:
            logger.warning(f"Validation error in enhanced chat: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except NotFoundError as e:
            logger.warning(f"Not found error in enhanced chat: {e}")
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error(f"Error processing enhanced mirror chat request: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    async def handle_enhanced_chat_stream(
        self, 
        req: EnhancedMirrorChatRequest, 
        current_user: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        """
        Process enhanced mirror chat requests with REAL-TIME STREAMING
        
        Args:
            req: The validated enhanced chat request from the API layer
            current_user: The authenticated user information from JWT token
            
        Yields:
            str: Streaming AI response chunks
            
        Raises:
            HTTPException: For various error conditions (400, 404, 500)
        """
        try:
            # Extract user ID from JWT token
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                logger.error("User ID not found in JWT token")
                raise HTTPException(status_code=400, detail="User ID not found in token")

            # Get user profile for personalization
            user_profile = await self.user_service.get_user_profile(user_id)
            if not user_profile:
                logger.warning(f"User profile not found for user: {user_id}")
                raise HTTPException(status_code=404, detail=f"User profile not found for user: {user_id}")

            # Resolve user name with intelligent fallbacks
            user_name = self._resolve_user_name(req.userName, user_profile, current_user)

            logger.info(
                f"Processing STREAMING chat request - User: {user_id}, "
                f"ConversationId: {req.conversationId}, "
                f"CreateNew: {req.createNewConversation}"
            )

            # Create use case request
            from ..use_cases.enhanced_mirror_chat_use_case import EnhancedMirrorChatRequest as UseCaseRequest
            use_case_request = UseCaseRequest(
                message=req.message,
                user_id=user_id,
                conversation_id=req.conversationId,
                user_name=user_name,
                create_new_conversation=req.createNewConversation
            )

            # Stream AI response in real-time
            async for chunk in self.chat_use_case.execute_stream(use_case_request):
                yield chunk

        except ValidationError as e:
            logger.warning(f"Validation error in streaming chat: {e}")
            yield f"ERROR: {str(e)}"
        except NotFoundError as e:
            logger.warning(f"Not found error in streaming chat: {e}")
            yield f"ERROR: {str(e)}"
        except Exception as e:
            logger.error(f"Error processing streaming chat request: {str(e)}")
            yield f"ERROR: Internal server error"

    async def get_user_conversations(
        self, 
        current_user: Dict[str, Any],
        limit: int = 50,
        include_archived: bool = False
    ) -> ConversationListResponse:
        """
        Get all conversations for the authenticated user

        Args:
            current_user: The authenticated user information
            limit: Maximum number of conversations to return
            include_archived: Whether to include archived conversations

        Returns:
            ConversationListResponse: List of user's conversations

        Raises:
            HTTPException: For various error conditions
        """
        try:
            # Extract user ID
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                raise HTTPException(status_code=400, detail="User ID not found in token")

            # Get conversations
            conversations = await self.conversation_use_case.get_user_conversations(
                user_id, 
                limit, 
                include_archived
            )

            # Convert to API response format
            conversation_data = [
                {
                    "conversationId": conv.conversation_id,
                    "title": conv.title,
                    "lastMessageAt": conv.last_message_at,
                    "messageCount": conv.message_count,
                    "isArchived": conv.is_archived
                }
                for conv in conversations
            ]

            logger.info(f"Retrieved {len(conversations)} conversations for user {user_id}")

            return ConversationListResponse(
                success=True,
                data={
                    "conversations": conversation_data,
                    "totalCount": len(conversations),
                    "includeArchived": include_archived
                }
            )

        except Exception as e:
            logger.error(f"Error getting user conversations: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    async def get_conversation_detail(
        self, 
        conversation_id: str,
        current_user: Dict[str, Any]
    ) -> ConversationDetailResponse:
        """
        Get detailed information about a specific conversation

        Args:
            conversation_id: The conversation ID
            current_user: The authenticated user information

        Returns:
            ConversationDetailResponse: Conversation details with recent messages

        Raises:
            HTTPException: For various error conditions
        """
        try:
            # Extract user ID
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                raise HTTPException(status_code=400, detail="User ID not found in token")

            # Get conversation details
            detail = await self.conversation_use_case.get_conversation_detail(
                conversation_id, 
                user_id
            )

            # Convert to API response format
            conversation = detail["conversation"]
            messages = detail["recentMessages"]

            response_data = {
                "conversation": {
                    "conversationId": conversation.conversation_id,
                    "title": conversation.title,
                    "createdAt": conversation.created_at,
                    "updatedAt": conversation.updated_at,
                    "messageCount": conversation.message_count,
                    "totalTokens": conversation.total_tokens,
                    "isArchived": conversation.is_archived,
                    "lastMessageAt": conversation.last_message_at
                },
                "recentMessages": [
                    {
                        "messageId": msg.message_id,
                        "role": msg.role,
                        "content": msg.content,
                        "timestamp": msg.timestamp,
                        "tokenCount": msg.token_count
                    }
                    for msg in messages
                ]
            }

            logger.info(f"Retrieved conversation detail for {conversation_id}")

            return ConversationDetailResponse(
                success=True,
                data=response_data
            )

        except ValidationError as e:
            logger.warning(f"Validation error getting conversation detail: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except NotFoundError as e:
            logger.warning(f"Conversation not found: {e}")
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            logger.error(f"Error getting conversation detail: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    async def archive_conversation(
        self, 
        req: ConversationManagementRequest,
        current_user: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Archive a conversation

        Args:
            req: The conversation management request
            current_user: The authenticated user information

        Returns:
            Dict: Success response

        Raises:
            HTTPException: For various error conditions
        """
        try:
            # Extract user ID
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                raise HTTPException(status_code=400, detail="User ID not found in token")

            # Archive conversation
            success = await self.conversation_use_case.archive_conversation(
                req.conversationId, 
                user_id
            )

            if not success:
                raise HTTPException(status_code=500, detail="Failed to archive conversation")

            logger.info(f"Archived conversation {req.conversationId} for user {user_id}")

            return {
                "success": True,
                "message": "Conversation archived successfully"
            }

        except ValidationError as e:
            logger.warning(f"Validation error archiving conversation: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except NotFoundError as e:
            logger.warning(f"Conversation not found for archiving: {e}")
            raise HTTPException(status_code=404, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error archiving conversation: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    async def delete_conversation(
        self, 
        req: ConversationManagementRequest,
        current_user: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Delete a conversation permanently

        Args:
            req: The conversation management request
            current_user: The authenticated user information

        Returns:
            Dict: Success response

        Raises:
            HTTPException: For various error conditions
        """
        try:
            # Extract user ID
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                raise HTTPException(status_code=400, detail="User ID not found in token")

            # Delete conversation
            success = await self.conversation_use_case.delete_conversation(
                req.conversationId, 
                user_id
            )

            if not success:
                raise HTTPException(status_code=500, detail="Failed to delete conversation")

            logger.info(f"Deleted conversation {req.conversationId} for user {user_id}")

            return {
                "success": True,
                "message": "Conversation deleted successfully"
            }

        except ValidationError as e:
            logger.warning(f"Validation error deleting conversation: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except NotFoundError as e:
            logger.warning(f"Conversation not found for deletion: {e}")
            raise HTTPException(status_code=404, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting conversation: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    async def update_conversation_title(
        self, 
        req: UpdateConversationTitleRequest,
        current_user: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update a conversation's title

        Args:
            req: The title update request
            current_user: The authenticated user information

        Returns:
            Dict: Success response

        Raises:
            HTTPException: For various error conditions
        """
        try:
            # Extract user ID
            user_id = current_user.get("id") or current_user.get("sub")
            if not user_id:
                raise HTTPException(status_code=400, detail="User ID not found in token")

            # Update title
            success = await self.conversation_use_case.update_conversation_title(
                req.conversationId, 
                user_id, 
                req.title
            )

            if not success:
                raise HTTPException(status_code=500, detail="Failed to update conversation title")

            logger.info(f"Updated title for conversation {req.conversationId}")

            return {
                "success": True,
                "message": "Conversation title updated successfully"
            }

        except ValidationError as e:
            logger.warning(f"Validation error updating conversation title: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except NotFoundError as e:
            logger.warning(f"Conversation not found for title update: {e}")
            raise HTTPException(status_code=404, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating conversation title: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")

    def _resolve_user_name(
        self, 
        request_user_name: Optional[str], 
        user_profile, 
        current_user: Dict[str, Any]
    ) -> Optional[str]:
        """
        Resolve user name with intelligent fallbacks
        
        Args:
            request_user_name: User name from request
            user_profile: User profile from database
            current_user: Current user from JWT
            
        Returns:
            Optional[str]: Resolved user name
        """
        # Priority order: request -> profile -> JWT -> email username
        
        # 1. Explicit name from request
        if request_user_name and request_user_name.strip():
            return request_user_name.strip()
        
        # 2. Chat name from user profile
        if user_profile and user_profile.chat_name:
            return user_profile.chat_name
        
        # 3. Display name from user profile
        if user_profile and user_profile.display_name:
            return user_profile.display_name
        
        # 4. First name from user profile
        if user_profile and user_profile.first_name:
            return user_profile.first_name
        
        # 5. Name from JWT token
        jwt_name = (
            current_user.get("firstName") or 
            current_user.get("given_name") or 
            current_user.get("name")
        )
        if jwt_name and jwt_name.strip():
            return jwt_name.strip()
        
        # 6. Username from email as last resort
        email = user_profile.email if user_profile else current_user.get("email")
        if email:
            return email.split("@")[0]
        
        return None
