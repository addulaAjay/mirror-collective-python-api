"""
Enhanced mirror chat use case with persistent conversation management
Production-ready implementation with comprehensive error handling
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from ..core.exceptions import ValidationError
from ..services.conversation_service import ConversationService
from ..services.openai_service import IMirrorChatRepository
from ..services.user_service import UserService

logger = logging.getLogger(__name__)


class EnhancedMirrorChatRequest:
    """
    Enhanced chat request with conversation management capabilities
    """

    def __init__(
        self,
        message: str,
        user_id: str,
        conversation_id: Optional[str] = None,
        user_name: Optional[str] = None,
        create_new_conversation: bool = False,
    ):
        self.message = message
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.user_name = user_name
        self.create_new_conversation = create_new_conversation

    def validate(self):
        """Validate the request parameters"""
        if not self.message or not self.message.strip():
            raise ValidationError("Message is required and cannot be empty")
        
        if not self.user_id or not self.user_id.strip():
            raise ValidationError("User ID is required")
        
        # Ensure we have either a conversation ID or request to create new one
        if not self.conversation_id and not self.create_new_conversation:
            raise ValidationError("Either conversation_id must be provided or create_new_conversation must be True")


class EnhancedMirrorChatResponse:
    """
    Enhanced response with conversation metadata and comprehensive information
    """

    def __init__(
        self, 
        reply: str, 
        timestamp: str, 
        conversation_id: str,
        message_count: int,
        conversation_title: str,
        is_new_conversation: bool = False
    ):
        self.reply = reply
        self.timestamp = timestamp
        self.conversation_id = conversation_id
        self.message_count = message_count
        self.conversation_title = conversation_title
        self.is_new_conversation = is_new_conversation

    def to_dict(self):
        """Convert response to dictionary format for API serialization"""
        return {
            "reply": self.reply,
            "timestamp": self.timestamp,
            "conversationId": self.conversation_id,
            "messageCount": self.message_count,
            "conversationTitle": self.conversation_title,
            "isNewConversation": self.is_new_conversation
        }


class EnhancedMirrorChatUseCase:
    """
    Enhanced business logic with persistent conversation management
    Handles the complete chat flow including conversation creation, message storage, and AI interaction
    """

    def __init__(self, chat_service: IMirrorChatRepository):
        self.chat_service = chat_service
        self.conversation_service = ConversationService()
        self.user_service = UserService()

    async def execute(self, request: EnhancedMirrorChatRequest) -> EnhancedMirrorChatResponse:
        """
        Process an enhanced mirror chat request with persistent conversation management

        Args:
            request: The enhanced chat request with conversation context

        Returns:
            EnhancedMirrorChatResponse: AI response with comprehensive conversation metadata

        Raises:
            ValidationError: If request validation fails
            NotFoundError: If conversation not found
            InternalServerError: If processing fails
        """
        
        try:
            # 1. Validate the request
            request.validate()
            
            logger.info(f"Processing enhanced chat request for user {request.user_id}")
            
            # 2. Check if conversation persistence is enabled
            if not self.conversation_service.is_persistence_enabled():
                logger.warning("Conversation persistence is disabled - falling back to stateless mode")
                # Fall back to simple chat without persistence
                return await self._handle_stateless_chat(request)
            
            # 3. Handle conversation creation/retrieval
            conversation = None
            is_new_conversation = False
            
            if request.create_new_conversation or not request.conversation_id:
                # Create new conversation
                logger.debug(f"Creating new conversation for user {request.user_id}")
                conversation = await self.conversation_service.create_conversation(
                    user_id=request.user_id,
                    initial_message=request.message
                )
                conversation_id = conversation.conversation_id
                is_new_conversation = True
            else:
                # Use existing conversation
                logger.debug(f"Using existing conversation {request.conversation_id}")
                conversation_id = request.conversation_id
                conversation = await self.conversation_service.get_conversation(
                    conversation_id, request.user_id
                )

            # 4. Build system prompt with personalization
            system_content = self._build_system_prompt(request.user_name)

            # 5. Get optimized AI context (system prompt + conversation history + current message)
            ai_messages = await self.conversation_service.get_ai_context(
                conversation_id=conversation_id,
                user_id=request.user_id,
                system_prompt=system_content,
                current_message=request.message
            )

            logger.debug(
                f"Built AI context with {len(ai_messages)} messages for conversation {conversation_id}"
            )

            # 5. Generate AI response
            reply = self.chat_service.send(ai_messages)

            # 6. Store the user message
            await self.conversation_service.add_message(
                conversation_id=conversation_id,
                user_id=request.user_id,
                role="user",
                content=request.message,
                token_count=self._estimate_token_count(request.message)
            )

            # 7. Store the AI response
            await self.conversation_service.add_message(
                conversation_id=conversation_id,
                user_id=request.user_id,
                role="assistant",
                content=reply,
                token_count=self._estimate_token_count(reply)
            )

            # 8. Record user activity for analytics
            try:
                await self.user_service.record_chat_activity(request.user_id)
            except Exception as e:
                logger.warning(f"Failed to record chat activity: {e}")
                # Don't fail the main request for analytics issues

            # 9. Get updated conversation for response metadata
            updated_conversation = await self.conversation_service.get_conversation(
                conversation_id, request.user_id
            )

            # 10. Build and return enhanced response
            response = EnhancedMirrorChatResponse(
                reply=reply,
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                conversation_id=conversation_id,
                message_count=updated_conversation.message_count,
                conversation_title=updated_conversation.title or "New Conversation",
                is_new_conversation=is_new_conversation
            )

            logger.info(
                f"Successfully processed chat request for user {request.user_id} "
                f"in conversation {conversation_id}"
            )

            return response

        except (ValidationError, Exception) as e:
            logger.error(f"Error processing enhanced chat request: {str(e)}")
            raise

    def _build_system_prompt(self, user_name: Optional[str] = None) -> str:
        """
        Build the system prompt with personalization
        
        Args:
            user_name: Optional user name for personalization
            
        Returns:
            str: The complete system prompt
        """
        base_prompt = (
            "You are a deeply empathetic, spiritually-aware guide. "
            "Respond with clarity, emotional resonance, and gentle encouragement for self-reflection. "
            "Your responses should be thoughtful, supportive, and help the user gain insights about themselves."
        )

        if user_name and user_name.strip():
            personalization = (
                f" The user you are speaking with is named {user_name.strip()}. "
                "Use their name naturally in your responses to create a more personal connection."
            )
            return base_prompt + personalization

        return base_prompt

    def _estimate_token_count(self, text: str) -> int:
        """
        Estimate token count for a text (rough approximation)
        
        Args:
            text: The text to estimate tokens for
            
        Returns:
            int: Estimated token count
        """
        # Rough estimation: ~4 characters per token on average
        # This is a simple approximation - for production, consider using tiktoken
        return len(text) // 4 if text else 0

    async def _handle_stateless_chat(self, request: EnhancedMirrorChatRequest) -> EnhancedMirrorChatResponse:
        """
        Handle chat in stateless mode when persistence is disabled
        Falls back to simple AI chat without conversation history
        """
        try:
            logger.info("Handling chat in stateless mode")
            
            # Build system prompt
            system_content = self._build_system_prompt(request.user_name)
            
            # Create simple message list for AI (no conversation history)
            from ..services.openai_service import ChatMessage
            ai_messages = [
                ChatMessage("system", system_content),
                ChatMessage("user", request.message)
            ]
            
            # Get AI response using the chat service
            ai_response = self.chat_service.send(ai_messages)
            
            # Update user activity (still track user engagement)
            await self.user_service.record_chat_activity(request.user_id)
            
            # Return response with minimal metadata (no conversation persistence)
            return EnhancedMirrorChatResponse(
                reply=ai_response,
                conversation_id="",  # Empty for stateless mode
                message_count=1,  # Just this exchange
                is_new_conversation=False,
                conversation_title="Stateless Chat",
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )
            
        except Exception as e:
            logger.error(f"Error in stateless chat: {e}")
            raise


class ConversationManagementUseCase:
    """
    Use case for conversation management operations (list, archive, delete, etc.)
    """

    def __init__(self):
        self.conversation_service = ConversationService()

    async def get_user_conversations(
        self, 
        user_id: str, 
        limit: int = 50,
        include_archived: bool = False
    ):
        """Get all conversations for a user"""
        try:
            if not user_id or not user_id.strip():
                raise ValidationError("User ID is required")

            summaries = await self.conversation_service.get_user_conversations(
                user_id.strip(), 
                limit, 
                include_archived
            )

            logger.info(f"Retrieved {len(summaries)} conversations for user {user_id}")
            return summaries

        except Exception as e:
            logger.error(f"Error getting conversations for user {user_id}: {e}")
            raise

    async def get_conversation_detail(self, conversation_id: str, user_id: str):
        """Get detailed conversation information with recent messages"""
        try:
            if not conversation_id or not conversation_id.strip():
                raise ValidationError("Conversation ID is required")
            if not user_id or not user_id.strip():
                raise ValidationError("User ID is required")

            # Get conversation metadata
            conversation = await self.conversation_service.get_conversation(
                conversation_id.strip(), 
                user_id.strip()
            )

            # Get recent messages (last 10 for detail view)
            messages = await self.conversation_service.get_conversation_history(
                conversation_id.strip(),
                user_id.strip(),
                limit=10,
                include_system_messages=False
            )

            return {
                "conversation": conversation,
                "recentMessages": messages
            }

        except Exception as e:
            logger.error(f"Error getting conversation detail {conversation_id}: {e}")
            raise

    async def archive_conversation(self, conversation_id: str, user_id: str) -> bool:
        """Archive a conversation"""
        try:
            success = await self.conversation_service.archive_conversation(
                conversation_id, 
                user_id
            )
            
            if success:
                logger.info(f"Archived conversation {conversation_id} for user {user_id}")
            
            return success

        except Exception as e:
            logger.error(f"Error archiving conversation {conversation_id}: {e}")
            raise

    async def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        """Delete a conversation"""
        try:
            success = await self.conversation_service.delete_conversation(
                conversation_id, 
                user_id
            )
            
            if success:
                logger.info(f"Deleted conversation {conversation_id} for user {user_id}")
            
            return success

        except Exception as e:
            logger.error(f"Error deleting conversation {conversation_id}: {e}")
            raise

    async def update_conversation_title(
        self, 
        conversation_id: str, 
        user_id: str, 
        new_title: str
    ) -> bool:
        """Update conversation title"""
        try:
            success = await self.conversation_service.update_conversation_title(
                conversation_id, 
                user_id, 
                new_title
            )
            
            if success:
                logger.info(f"Updated title for conversation {conversation_id}")
            
            return success

        except Exception as e:
            logger.error(f"Error updating conversation title {conversation_id}: {e}")
            raise
