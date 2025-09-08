"""
Conversation service for managing chat history and context
Production-ready service with comprehensive error handling and optimization
"""

import logging
import os
from datetime import datetime, timezone
from typing import List, Literal, Optional, Tuple
from uuid import uuid4

from ..core.exceptions import InternalServerError, NotFoundError, ValidationError
from ..models.conversation import Conversation, ConversationMessage, ConversationSummary
from ..services.dynamodb_service import DynamoDBService
from ..services.openai_service import ChatMessage

logger = logging.getLogger(__name__)


class ConversationService:
    """
    High-level service for managing persistent conversation history
    Handles business logic, validation, and optimization
    """
    
    def __init__(self):
        self.dynamodb_service = DynamoDBService()
        
        # Configuration for context management - read from environment variables
        self.max_context_messages = int(os.getenv("MAX_CONTEXT_MESSAGES", "30"))
        self.max_tokens_per_conversation = int(os.getenv("MAX_TOKENS_PER_CONVERSATION", "4000"))
        self.conversation_title_max_length = int(os.getenv("CONVERSATION_TITLE_MAX_LENGTH", "60"))
        
        # Feature flags
        self.enable_conversation_persistence = os.getenv("ENABLE_CONVERSATION_PERSISTENCE", "true").lower() == "true"
        self.enable_conversation_search = os.getenv("ENABLE_CONVERSATION_SEARCH", "false").lower() == "true"
        self.enable_message_encryption = os.getenv("ENABLE_MESSAGE_ENCRYPTION", "false").lower() == "true"
        
        # Performance settings
        self.conversation_cache_ttl = int(os.getenv("CONVERSATION_CACHE_TTL", "3600"))
        self.message_batch_size = int(os.getenv("MESSAGE_BATCH_SIZE", "25"))
        
        logger.info(f"ConversationService initialized - Max context: {self.max_context_messages}, "
                   f"Max tokens: {self.max_tokens_per_conversation}, "
                   f"Title max length: {self.conversation_title_max_length}, "
                   f"Persistence enabled: {self.enable_conversation_persistence}")
    
    def is_persistence_enabled(self) -> bool:
        """Check if conversation persistence is enabled"""
        return self.enable_conversation_persistence
    
    def is_search_enabled(self) -> bool:
        """Check if conversation search is enabled"""
        return self.enable_conversation_search
    
    def is_encryption_enabled(self) -> bool:
        """Check if message encryption is enabled"""
        return self.enable_message_encryption
    
    async def create_conversation(
        self, 
        user_id: str, 
        title: Optional[str] = None,
        initial_message: Optional[str] = None
    ) -> Conversation:
        """
        Create a new conversation for a user
        
        Args:
            user_id: The user ID
            title: Optional conversation title
            initial_message: Optional first message to generate title from
            
        Returns:
            Conversation: The created conversation
            
        Raises:
            ValidationError: If input validation fails
            InternalServerError: If creation fails
        """
        try:
            # Validate inputs
            if not user_id or not user_id.strip():
                raise ValidationError("User ID is required")
            
            # Generate title if not provided
            if not title and initial_message:
                title = self._generate_title_from_message(initial_message)
            elif not title:
                title = f"Conversation {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
            
            # Create conversation object
            conversation = Conversation(
                conversation_id=str(uuid4()),
                user_id=user_id.strip(),
                title=title[:self.conversation_title_max_length]
            )
            
            # Save to database
            created_conversation = await self.dynamodb_service.create_conversation(conversation)
            
            logger.info(f"Created conversation {created_conversation.conversation_id} for user {user_id}")
            return created_conversation
            
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error creating conversation for user {user_id}: {e}")
            raise InternalServerError(f"Failed to create conversation: {str(e)}")
    
    async def get_conversation(self, conversation_id: str, user_id: str) -> Conversation:
        """
        Get a conversation by ID with user verification
        
        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security)
            
        Returns:
            Conversation: The conversation
            
        Raises:
            ValidationError: If inputs are invalid
            NotFoundError: If conversation not found
            InternalServerError: If retrieval fails
        """
        try:
            # Validate inputs
            if not conversation_id or not conversation_id.strip():
                raise ValidationError("Conversation ID is required")
            if not user_id or not user_id.strip():
                raise ValidationError("User ID is required")
            
            conversation = await self.dynamodb_service.get_conversation(
                conversation_id.strip(), 
                user_id.strip()
            )
            
            if not conversation:
                raise NotFoundError(f"Conversation {conversation_id} not found for user {user_id}")
            
            return conversation
            
        except (ValidationError, NotFoundError):
            raise
        except Exception as e:
            logger.error(f"Error getting conversation {conversation_id}: {e}")
            raise InternalServerError(f"Failed to get conversation: {str(e)}")
    
    async def add_message(
        self, 
        conversation_id: str, 
        user_id: str,
        role: Literal["system", "user", "assistant"], 
        content: str,
        token_count: Optional[int] = None
    ) -> ConversationMessage:
        """
        Add a message to a conversation with automatic conversation updates
        
        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security)
            role: Message role ('user', 'assistant', 'system')
            content: Message content
            token_count: Optional token count for the message
            
        Returns:
            ConversationMessage: The created message
            
        Raises:
            ValidationError: If inputs are invalid
            NotFoundError: If conversation not found
            InternalServerError: If creation fails
        """
        try:
            # Validate inputs
            if not conversation_id or not conversation_id.strip():
                raise ValidationError("Conversation ID is required")
            if not user_id or not user_id.strip():
                raise ValidationError("User ID is required")
            if role not in ["user", "assistant", "system"]:
                raise ValidationError("Role must be 'user', 'assistant', or 'system'")
            if not content or not content.strip():
                raise ValidationError("Message content is required")
            
            # Verify conversation exists and belongs to user
            conversation = await self.get_conversation(conversation_id, user_id)
            
            # Create message
            message = ConversationMessage(
                message_id=str(uuid4()),
                conversation_id=conversation_id.strip(),
                role=role,
                content=content.strip(),
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                token_count=token_count
            )
            
            # Save message
            created_message = await self.dynamodb_service.create_message(message)
            
            # Update conversation metadata
            conversation.update_activity(content, token_count or 0)
            await self.dynamodb_service.update_conversation(conversation)
            
            logger.debug(f"Added {role} message to conversation {conversation_id}")
            return created_message
            
        except (ValidationError, NotFoundError):
            raise
        except Exception as e:
            logger.error(f"Error adding message to conversation {conversation_id}: {e}")
            raise InternalServerError(f"Failed to add message: {str(e)}")
    
    async def get_conversation_history(
        self, 
        conversation_id: str, 
        user_id: str,
        limit: Optional[int] = None,
        include_system_messages: bool = True
    ) -> List[ConversationMessage]:
        """
        Get conversation history with filtering options
        
        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security)
            limit: Maximum number of messages to return
            include_system_messages: Whether to include system messages
            
        Returns:
            List[ConversationMessage]: Conversation messages in chronological order
            
        Raises:
            ValidationError: If inputs are invalid
            NotFoundError: If conversation not found
            InternalServerError: If retrieval fails
        """
        try:
            # Validate inputs
            if not conversation_id or not conversation_id.strip():
                raise ValidationError("Conversation ID is required")
            if not user_id or not user_id.strip():
                raise ValidationError("User ID is required")
            
            # Verify conversation exists and belongs to user
            await self.get_conversation(conversation_id, user_id)
            
            # Get messages (use recent messages for better performance)
            if limit and limit <= self.max_context_messages:
                messages = await self.dynamodb_service.get_recent_messages(
                    conversation_id.strip(), 
                    limit
                )
            else:
                messages, _ = await self.dynamodb_service.get_conversation_messages(
                    conversation_id.strip(),
                    limit
                )
            
            # Filter system messages if requested
            if not include_system_messages:
                messages = [msg for msg in messages if msg.role != "system"]
            
            return messages
            
        except (ValidationError, NotFoundError):
            raise
        except Exception as e:
            logger.error(f"Error getting conversation history {conversation_id}: {e}")
            raise InternalServerError(f"Failed to get conversation history: {str(e)}")
    
    async def get_user_conversations(
        self, 
        user_id: str, 
        limit: int = 50,
        include_archived: bool = False
    ) -> List[ConversationSummary]:
        """
        Get all conversations for a user as lightweight summaries
        
        Args:
            user_id: The user ID
            limit: Maximum number of conversations to return
            include_archived: Whether to include archived conversations
            
        Returns:
            List[ConversationSummary]: User's conversation summaries
            
        Raises:
            ValidationError: If user ID is invalid
            InternalServerError: If retrieval fails
        """
        try:
            # Validate inputs
            if not user_id or not user_id.strip():
                raise ValidationError("User ID is required")
            
            conversations = await self.dynamodb_service.get_user_conversations(
                user_id.strip(), 
                limit, 
                include_archived
            )
            
            # Convert to summaries for better performance
            summaries = [
                ConversationSummary.from_conversation(conv) 
                for conv in conversations
            ]
            
            logger.debug(f"Retrieved {len(summaries)} conversations for user {user_id}")
            return summaries
            
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error getting conversations for user {user_id}: {e}")
            raise InternalServerError(f"Failed to get user conversations: {str(e)}")
    
    async def archive_conversation(self, conversation_id: str, user_id: str) -> bool:
        """
        Archive a conversation (soft delete)
        
        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security)
            
        Returns:
            bool: True if archived successfully
            
        Raises:
            ValidationError: If inputs are invalid
            NotFoundError: If conversation not found
        """
        try:
            # Validate inputs and verify ownership
            await self.get_conversation(conversation_id, user_id)
            
            success = await self.dynamodb_service.archive_conversation(
                conversation_id.strip(), 
                user_id.strip()
            )
            
            if success:
                logger.info(f"Archived conversation {conversation_id} for user {user_id}")
            
            return success
            
        except (ValidationError, NotFoundError):
            raise
        except Exception as e:
            logger.error(f"Error archiving conversation {conversation_id}: {e}")
            return False
    
    async def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        """
        Delete a conversation and all its messages (hard delete)
        
        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security)
            
        Returns:
            bool: True if deleted successfully
            
        Raises:
            ValidationError: If inputs are invalid
            NotFoundError: If conversation not found
        """
        try:
            # Validate inputs and verify ownership
            await self.get_conversation(conversation_id, user_id)
            
            success = await self.dynamodb_service.delete_conversation(
                conversation_id.strip(), 
                user_id.strip()
            )
            
            if success:
                logger.info(f"Deleted conversation {conversation_id} for user {user_id}")
            
            return success
            
        except (ValidationError, NotFoundError):
            raise
        except Exception as e:
            logger.error(f"Error deleting conversation {conversation_id}: {e}")
            return False
    
    async def get_ai_context(
        self, 
        conversation_id: str, 
        user_id: str,
        system_prompt: str,
        current_message: str
    ) -> List[ChatMessage]:
        """
        Get optimized context for AI including system prompt, conversation history, and current message
        
        Args:
            conversation_id: The conversation ID
            user_id: The user ID
            system_prompt: The system prompt to include
            current_message: The current user message
            
        Returns:
            List[ChatMessage]: Optimized message list for AI context
            
        Raises:
            ValidationError: If inputs are invalid
            NotFoundError: If conversation not found
        """
        try:
            # Get recent conversation history
            messages = await self.get_conversation_history(
                conversation_id, 
                user_id, 
                limit=self.max_context_messages,
                include_system_messages=False
            )
            
            # Build AI context
            ai_messages = [ChatMessage(role="system", content=system_prompt)]
            
            # Add conversation history
            for msg in messages:
                ai_messages.append(ChatMessage(role=msg.role, content=msg.content))
            
            # Add current message
            ai_messages.append(ChatMessage(role="user", content=current_message))
            
            logger.debug(f"Built AI context with {len(ai_messages)} messages for conversation {conversation_id}")
            return ai_messages
            
        except (ValidationError, NotFoundError):
            raise
        except Exception as e:
            logger.error(f"Error building AI context for conversation {conversation_id}: {e}")
            raise InternalServerError(f"Failed to build AI context: {str(e)}")
    
    def _generate_title_from_message(self, message: str) -> str:
        """
        Generate a conversation title from a message
        
        Args:
            message: The message to generate title from
            
        Returns:
            str: Generated title
        """
        # Clean and truncate the message for title
        title = message.strip()
        # Remove line breaks and extra spaces
        title = " ".join(title.split())
        # Truncate to reasonable length
        if len(title) > self.conversation_title_max_length:
            title = title[:self.conversation_title_max_length - 3] + "..."
        
        return title if title else "New Conversation"
    
    async def update_conversation_title(
        self, 
        conversation_id: str, 
        user_id: str, 
        new_title: str
    ) -> bool:
        """
        Update a conversation's title
        
        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security)
            new_title: The new title
            
        Returns:
            bool: True if updated successfully
        """
        try:
            # Validate and get conversation
            conversation = await self.get_conversation(conversation_id, user_id)
            
            # Validate new title
            if not new_title or not new_title.strip():
                raise ValidationError("Title cannot be empty")
            
            # Update title
            conversation.title = new_title.strip()[:self.conversation_title_max_length]
            conversation.updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            
            await self.dynamodb_service.update_conversation(conversation)
            
            logger.info(f"Updated title for conversation {conversation_id}")
            return True
            
        except (ValidationError, NotFoundError):
            raise
        except Exception as e:
            logger.error(f"Error updating conversation title {conversation_id}: {e}")
            return False
