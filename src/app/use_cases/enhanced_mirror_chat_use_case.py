"""
Enhanced mirror chat use case with persistent conversation management
Production-ready implementation with comprehensive error handling
PERFORMANCE ANALYSIS: Added timing to identify bottlenecks
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from ..core.exceptions import ValidationError
from ..models.conversation import Conversation
from ..services.conversation_service import ConversationService
from ..services.openai_service import ChatMessage, IMirrorChatRepository
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
        PERFORMANCE ANALYSIS: Added timing to identify bottlenecks

        Args:
            request: The enhanced chat request with conversation context

        Returns:
            EnhancedMirrorChatResponse: AI response with comprehensive conversation metadata

        Raises:
            ValidationError: If request validation fails
            NotFoundError: If conversation not found
            InternalServerError: If processing fails
        """
        
        # PERFORMANCE ANALYSIS: Start timing
        start_time = time.time()
        timings = {}
        
        try:
            # 1. Validate the request
            validation_start = time.time()
            request.validate()
            timings['validation'] = time.time() - validation_start
            
            logger.info(f"Processing enhanced chat request for user {request.user_id}")
            
            # 2. Check if conversation persistence is enabled
            persistence_check_start = time.time()
            if not self.conversation_service.is_persistence_enabled():
                logger.warning("Conversation persistence is disabled - falling back to stateless mode")
                # Fall back to simple chat without persistence
                return await self._handle_stateless_chat(request)
            timings['persistence_check'] = time.time() - persistence_check_start

            # 3. OPTIMIZATION: Parallel conversation setup and AI context building
            conversation_setup_start = time.time()
            import asyncio
            
            # Handle conversation creation/retrieval
            conversation = None
            is_new_conversation = False
            
            if request.create_new_conversation or not request.conversation_id:
                # Create new conversation
                create_conversation_start = time.time()
                logger.debug(f"Creating new conversation for user {request.user_id}")
                conversation = await self.conversation_service.create_conversation(
                    user_id=request.user_id,
                    initial_message=request.message
                )
                conversation_id = conversation.conversation_id
                is_new_conversation = True
                timings['create_conversation'] = time.time() - create_conversation_start
                
                # For new conversations, we can skip history lookup and start AI immediately
                build_prompt_start = time.time()
                system_content = self._build_system_prompt(request.user_name)
                ai_messages = [
                    ChatMessage(role="system", content=system_content),
                    ChatMessage(role="user", content=request.message)
                ]
                timings['build_prompt'] = time.time() - build_prompt_start
            else:
                # Use existing conversation - parallel fetch context and validate conversation
                logger.debug(f"Using existing conversation {request.conversation_id}")
                conversation_id = request.conversation_id
                
                # PARALLEL OPERATIONS: Get conversation details and AI context simultaneously
                parallel_operations_start = time.time()
                system_content = self._build_system_prompt(request.user_name)
                
                conversation_task = asyncio.create_task(
                    self.conversation_service.get_conversation(conversation_id, request.user_id)
                )
                ai_context_task = asyncio.create_task(
                    self.conversation_service.get_ai_context(
                        conversation_id=conversation_id,
                        user_id=request.user_id,
                        system_prompt=system_content,
                        current_message=request.message
                    )
                )
                
                # Wait for both operations
                gather_results = await asyncio.gather(
                    conversation_task, ai_context_task
                )
                conversation = gather_results[0]
                ai_messages = gather_results[1]
                timings['parallel_operations'] = time.time() - parallel_operations_start

            timings['conversation_setup'] = time.time() - conversation_setup_start

            logger.debug(
                f"Built AI context with {len(ai_messages)} messages for conversation {conversation_id}"
            )

            # 4. OPTIMIZATION: Start AI generation immediately while preparing to store messages
            ai_generation_start = time.time()
            ai_generation_task = asyncio.create_task(
                self._generate_ai_response_async(ai_messages)
            )
            
            # 5. OPTIMIZATION: Prepare message storage operations (don't wait for AI yet)
            message_storage_start = time.time()
            user_message_task = asyncio.create_task(
                self.conversation_service.add_message(
                    conversation_id=conversation_id,
                    user_id=request.user_id,
                    role="user",
                    content=request.message,
                    token_count=self._estimate_token_count(request.message)
                )
            )

            # 6. Wait for AI response (this is the longest operation)
            reply = await ai_generation_task
            timings['ai_generation'] = time.time() - ai_generation_start
            
            # 7. OPTIMIZATION: Parallel completion - store AI response and record activity
            completion_start = time.time()
            ai_message_task = asyncio.create_task(
                self.conversation_service.add_message(
                    conversation_id=conversation_id,
                    user_id=request.user_id,
                    role="assistant",
                    content=reply,
                    token_count=self._estimate_token_count(reply)
                )
            )
            
            activity_task = asyncio.create_task(
                self._record_user_activity_safe(request.user_id)
            )

            # Wait for user message to be stored (required for accurate count)
            await user_message_task
            timings['message_storage'] = time.time() - message_storage_start
            
            # 8. OPTIMIZATION: Calculate response metadata without fetching full conversation
            metadata_start = time.time()
            # Instead of fetching updated conversation, calculate metadata locally
            if conversation:
                estimated_message_count = conversation.message_count + 2  # user + assistant
                conversation_title = conversation.title or "New Conversation"
            else:
                estimated_message_count = 2 if is_new_conversation else 0
                conversation_title = "New Conversation"

            # 9. Build and return enhanced response (don't wait for non-critical operations)
            response = EnhancedMirrorChatResponse(
                reply=reply,
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                conversation_id=conversation_id,
                message_count=estimated_message_count,
                conversation_title=conversation_title,
                is_new_conversation=is_new_conversation
            )
            timings['metadata'] = time.time() - metadata_start
            timings['completion'] = time.time() - completion_start

            # PERFORMANCE ANALYSIS: Log detailed timing breakdown
            total_time = time.time() - start_time
            timings['total'] = total_time
            
            logger.info(
                f"🔍 PERFORMANCE ANALYSIS for user {request.user_id}:\n"
                f"Total time: {total_time:.3f}s\n"
                f"Breakdown:\n"
                f"  - Validation: {timings.get('validation', 0):.3f}s\n"
                f"  - Persistence check: {timings.get('persistence_check', 0):.3f}s\n"
                f"  - Conversation setup: {timings.get('conversation_setup', 0):.3f}s\n"
                f"    ├─ Create conversation: {timings.get('create_conversation', 0):.3f}s\n"
                f"    ├─ Build prompt: {timings.get('build_prompt', 0):.3f}s\n"
                f"    └─ Parallel operations: {timings.get('parallel_operations', 0):.3f}s\n"
                f"  - 🔥 AI generation: {timings.get('ai_generation', 0):.3f}s ({timings.get('ai_generation', 0)/total_time*100:.1f}%)\n"
                f"  - Message storage: {timings.get('message_storage', 0):.3f}s\n"
                f"  - Metadata: {timings.get('metadata', 0):.3f}s\n"
                f"  - Completion: {timings.get('completion', 0):.3f}s"
            )

            # Let remaining operations complete in background (don't block response)
            asyncio.create_task(self._complete_background_operations(ai_message_task, activity_task))

            return response

        except (ValidationError, Exception) as e:
            total_time = time.time() - start_time
            logger.error(f"Error processing enhanced chat request after {total_time:.3f}s: {str(e)}")
            raise

    async def _generate_ai_response_async(self, ai_messages) -> str:
        """
        Generate AI response asynchronously with timing analysis
        
        Args:
            ai_messages: List of messages for AI context
            
        Returns:
            str: AI generated response
        """
        ai_start_time = time.time()
        
        import asyncio
        # Convert ChatMessage objects to dict format if needed
        format_start = time.time()
        if hasattr(ai_messages[0], 'to_dict'):
            formatted_messages = [msg for msg in ai_messages]
        else:
            # Already in dict format from optimization
            from ..services.openai_service import ChatMessage
            formatted_messages = [ChatMessage(role=msg["role"], content=msg["content"]) for msg in ai_messages]
        
        format_time = time.time() - format_start
        
        # Run OpenAI call in thread pool to avoid blocking
        openai_start = time.time()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self.chat_service.send, formatted_messages)
        openai_time = time.time() - openai_start
        
        total_ai_time = time.time() - ai_start_time
        
        logger.info(
            f"🤖 AI Response Timing:\n"
            f"  - Message formatting: {format_time:.3f}s\n"
            f"  - OpenAI API call: {openai_time:.3f}s ({openai_time/total_ai_time*100:.1f}%)\n"
            f"  - Total AI time: {total_ai_time:.3f}s"
        )
        
        return result

    async def _record_user_activity_safe(self, user_id: str):
        """
        Record user activity safely without failing the main request
        
        Args:
            user_id: The user ID
        """
        try:
            await self.user_service.record_chat_activity(user_id)
        except Exception as e:
            logger.warning(f"Failed to record chat activity: {e}")
            # Don't fail the main request for analytics issues

    async def _complete_background_operations(self, *tasks):
        """
        Complete background operations without blocking the response
        
        Args:
            tasks: List of asyncio tasks to complete
        """
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.warning(f"Background operation failed: {e}")

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
