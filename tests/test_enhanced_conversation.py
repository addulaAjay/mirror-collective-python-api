"""
Tests for enhanced conversation management system
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime, timezone

from src.app.models.conversation import Conversation, ConversationMessage
from src.app.services.conversation_service import ConversationService
from src.app.use_cases.enhanced_mirror_chat_use_case import (
    EnhancedMirrorChatUseCase,
    EnhancedMirrorChatRequest,
    ConversationManagementUseCase
)
from src.app.core.exceptions import ValidationError, NotFoundError


class TestConversationModels:
    """Test conversation model classes"""
    
    def test_conversation_creation(self):
        """Test creating a conversation object"""
        conversation = Conversation(
            conversation_id="test-conv-id",
            user_id="test-user-id",
            title="Test Conversation"
        )
        
        assert conversation.conversation_id == "test-conv-id"
        assert conversation.user_id == "test-user-id"
        assert conversation.title == "Test Conversation"
        assert conversation.message_count == 0
        assert conversation.is_archived is False
        assert conversation.created_at is not None
        assert conversation.updated_at is not None

    def test_conversation_title_generation(self):
        """Test automatic title generation"""
        conversation = Conversation(
            conversation_id="test-conv-id",
            user_id="test-user-id"
        )
        
        title = conversation.generate_title_from_content("This is a long message that should be truncated")
        assert len(title) <= 60
        assert title.startswith("This is a long message")

    def test_conversation_activity_update(self):
        """Test conversation activity updates"""
        conversation = Conversation(
            conversation_id="test-conv-id",
            user_id="test-user-id"
        )
        
        original_count = conversation.message_count
        original_tokens = conversation.total_tokens
        
        conversation.update_activity("Test message", 10)
        
        assert conversation.message_count == original_count + 1
        assert conversation.total_tokens == original_tokens + 10
        assert conversation.updated_at is not None

    def test_message_creation(self):
        """Test creating a conversation message"""
        message = ConversationMessage(
            message_id="test-msg-id",
            conversation_id="test-conv-id",
            role="user",
            content="Test message content",
            timestamp="2023-01-01T00:00:00Z"
        )
        
        assert message.message_id == "test-msg-id"
        assert message.conversation_id == "test-conv-id"
        assert message.role == "user"
        assert message.content == "Test message content"
        assert message.timestamp == "2023-01-01T00:00:00Z"

    def test_message_to_dynamodb_item(self):
        """Test converting message to DynamoDB format"""
        message = ConversationMessage(
            message_id="test-msg-id",
            conversation_id="test-conv-id",
            role="user",
            content="Test message",
            timestamp="2023-01-01T00:00:00Z",
            token_count=5
        )
        
        item = message.to_dynamodb_item()
        
        assert "message_id" in item
        assert "conversation_id" in item
        assert "role" in item
        assert "content" in item
        assert "timestamp" in item
        assert "token_count" in item
        assert "metadata" not in item  # Should be excluded since it's None


class TestConversationService:
    """Test conversation service functionality"""
    
    @pytest.fixture
    def conversation_service(self):
        """Create a conversation service with mocked dependencies"""
        service = ConversationService()
        service.dynamodb_service = Mock()
        return service

    @pytest.mark.asyncio
    async def test_create_conversation_success(self, conversation_service):
        """Test successful conversation creation"""
        # Mock the DynamoDB service
        mock_conversation = Conversation(
            conversation_id="test-conv-id",
            user_id="test-user-id",
            title="Test Title"
        )
        conversation_service.dynamodb_service.create_conversation = AsyncMock(return_value=mock_conversation)
        
        # Create conversation
        result = await conversation_service.create_conversation("test-user-id", "Test Title")
        
        assert result.user_id == "test-user-id"
        assert result.title == "Test Title"
        conversation_service.dynamodb_service.create_conversation.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_conversation_validation_error(self, conversation_service):
        """Test conversation creation with invalid user ID"""
        with pytest.raises(ValidationError, match="User ID is required"):
            await conversation_service.create_conversation("", "Test Title")

    @pytest.mark.asyncio
    async def test_get_conversation_success(self, conversation_service):
        """Test successful conversation retrieval"""
        mock_conversation = Conversation(
            conversation_id="test-conv-id",
            user_id="test-user-id"
        )
        conversation_service.dynamodb_service.get_conversation = AsyncMock(return_value=mock_conversation)
        
        result = await conversation_service.get_conversation("test-conv-id", "test-user-id")
        
        assert result.conversation_id == "test-conv-id"
        assert result.user_id == "test-user-id"

    @pytest.mark.asyncio
    async def test_get_conversation_not_found(self, conversation_service):
        """Test conversation retrieval when not found"""
        conversation_service.dynamodb_service.get_conversation = AsyncMock(return_value=None)
        
        with pytest.raises(NotFoundError, match="Conversation test-conv-id not found"):
            await conversation_service.get_conversation("test-conv-id", "test-user-id")

    @pytest.mark.asyncio
    async def test_add_message_success(self, conversation_service):
        """Test successful message addition"""
        # Mock conversation exists
        mock_conversation = Conversation(
            conversation_id="test-conv-id",
            user_id="test-user-id"
        )
        conversation_service.dynamodb_service.get_conversation = AsyncMock(return_value=mock_conversation)
        
        # Mock message creation
        mock_message = ConversationMessage(
            message_id="test-msg-id",
            conversation_id="test-conv-id",
            role="user",
            content="Test message",
            timestamp="2023-01-01T00:00:00Z"
        )
        conversation_service.dynamodb_service.create_message = AsyncMock(return_value=mock_message)
        conversation_service.dynamodb_service.update_conversation = AsyncMock(return_value=mock_conversation)
        
        result = await conversation_service.add_message(
            "test-conv-id",
            "test-user-id", 
            "user",
            "Test message"
        )
        
        assert result.content == "Test message"
        assert result.role == "user"
        conversation_service.dynamodb_service.create_message.assert_called_once()
        conversation_service.dynamodb_service.update_conversation.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_message_validation_error(self, conversation_service):
        """Test message addition with invalid role"""
        mock_conversation = Conversation(
            conversation_id="test-conv-id",
            user_id="test-user-id"
        )
        conversation_service.dynamodb_service.get_conversation = AsyncMock(return_value=mock_conversation)
        
        with pytest.raises(ValidationError, match="Role must be"):
            await conversation_service.add_message(
                "test-conv-id",
                "test-user-id",
                "invalid-role",
                "Test message"
            )


class TestEnhancedMirrorChatUseCase:
    """Test enhanced mirror chat use case"""
    
    @pytest.fixture
    def chat_use_case(self):
        """Create use case with mocked dependencies"""
        mock_chat_service = Mock()
        use_case = EnhancedMirrorChatUseCase(mock_chat_service)
        use_case.conversation_service = Mock()
        use_case.user_service = Mock()
        return use_case

    def test_request_validation_success(self):
        """Test valid request validation"""
        request = EnhancedMirrorChatRequest(
            message="Hello",
            user_id="test-user-id",
            create_new_conversation=True
        )
        
        # Should not raise any exception
        request.validate()

    def test_request_validation_missing_message(self):
        """Test request validation with missing message"""
        request = EnhancedMirrorChatRequest(
            message="",
            user_id="test-user-id",
            create_new_conversation=True
        )
        
        with pytest.raises(ValidationError, match="Message is required"):
            request.validate()

    def test_request_validation_missing_user_id(self):
        """Test request validation with missing user ID"""
        request = EnhancedMirrorChatRequest(
            message="Hello",
            user_id="",
            create_new_conversation=True
        )
        
        with pytest.raises(ValidationError, match="User ID is required"):
            request.validate()

    def test_request_validation_no_conversation_context(self):
        """Test request validation without conversation context"""
        request = EnhancedMirrorChatRequest(
            message="Hello",
            user_id="test-user-id",
            # No conversation_id and create_new_conversation=False
        )
        
        with pytest.raises(ValidationError, match="Either conversation_id must be provided"):
            request.validate()

    @pytest.mark.asyncio
    async def test_execute_new_conversation(self, chat_use_case):
        """Test executing chat with new conversation creation"""
        # Mock conversation creation
        mock_conversation = Conversation(
            conversation_id="new-conv-id",
            user_id="test-user-id",
            title="Generated Title"
        )
        chat_use_case.conversation_service.create_conversation = AsyncMock(return_value=mock_conversation)
        chat_use_case.conversation_service.get_ai_context = AsyncMock(return_value=[])
        chat_use_case.conversation_service.add_message = AsyncMock()
        chat_use_case.conversation_service.get_conversation = AsyncMock(return_value=mock_conversation)
        chat_use_case.conversation_service.is_persistence_enabled = Mock(return_value=True)
        
        # Mock chat service with async generator for streaming
        async def mock_stream(messages):
            yield "AI "
            yield "response"
        
        chat_use_case.chat_service.send_stream = mock_stream
        
        # Mock user service
        chat_use_case.user_service.record_chat_activity = AsyncMock()
        
        # Create request
        request = EnhancedMirrorChatRequest(
            message="Hello",
            user_id="test-user-id",
            create_new_conversation=True,
            user_name="Test User"
        )
        
        # Execute use case
        result = await chat_use_case.execute(request)
        
        # Verify result
        assert result.reply == "AI response"
        assert result.conversation_id == "new-conv-id"
        assert result.is_new_conversation is True
        
        # Verify service calls
        chat_use_case.conversation_service.create_conversation.assert_called_once()
        assert chat_use_case.conversation_service.add_message.call_count == 2  # User message + AI response

    def test_build_system_prompt_with_name(self, chat_use_case):
        """Test system prompt building with user name"""
        prompt = chat_use_case._build_system_prompt("John")
        
        assert "deeply empathetic" in prompt
        assert "John" in prompt
        assert "named John" in prompt

    def test_build_system_prompt_without_name(self, chat_use_case):
        """Test system prompt building without user name"""
        prompt = chat_use_case._build_system_prompt(None)
        
        assert "deeply empathetic" in prompt
        assert "named" not in prompt

    def test_estimate_token_count(self, chat_use_case):
        """Test token count estimation"""
        # Test with regular text
        count = chat_use_case._estimate_token_count("Hello world")
        assert count > 0
        
        # Test with empty text
        count = chat_use_case._estimate_token_count("")
        assert count == 0
        
        # Test with None
        count = chat_use_case._estimate_token_count(None)
        assert count == 0


class TestConversationManagementUseCase:
    """Test conversation management use case"""
    
    @pytest.fixture
    def management_use_case(self):
        """Create management use case with mocked dependencies"""
        use_case = ConversationManagementUseCase()
        use_case.conversation_service = Mock()
        return use_case

    @pytest.mark.asyncio
    async def test_get_user_conversations_success(self, management_use_case):
        """Test getting user conversations successfully"""
        # Mock conversation summaries
        mock_summaries = [
            Mock(conversation_id="conv1", title="Conv 1"),
            Mock(conversation_id="conv2", title="Conv 2")
        ]
        management_use_case.conversation_service.get_user_conversations = AsyncMock(return_value=mock_summaries)
        
        result = await management_use_case.get_user_conversations("test-user-id")
        
        assert len(result) == 2
        management_use_case.conversation_service.get_user_conversations.assert_called_once_with(
            "test-user-id", 50, False
        )

    @pytest.mark.asyncio
    async def test_get_user_conversations_validation_error(self, management_use_case):
        """Test getting conversations with invalid user ID"""
        with pytest.raises(ValidationError, match="User ID is required"):
            await management_use_case.get_user_conversations("")

    @pytest.mark.asyncio
    async def test_archive_conversation_success(self, management_use_case):
        """Test archiving conversation successfully"""
        management_use_case.conversation_service.archive_conversation = AsyncMock(return_value=True)
        
        result = await management_use_case.archive_conversation("conv-id", "user-id")
        
        assert result is True
        management_use_case.conversation_service.archive_conversation.assert_called_once_with(
            "conv-id", "user-id"
        )

    @pytest.mark.asyncio
    async def test_delete_conversation_success(self, management_use_case):
        """Test deleting conversation successfully"""
        management_use_case.conversation_service.delete_conversation = AsyncMock(return_value=True)
        
        result = await management_use_case.delete_conversation("conv-id", "user-id")
        
        assert result is True
        management_use_case.conversation_service.delete_conversation.assert_called_once_with(
            "conv-id", "user-id"
        )

    @pytest.mark.asyncio
    async def test_update_conversation_title_success(self, management_use_case):
        """Test updating conversation title successfully"""
        management_use_case.conversation_service.update_conversation_title = AsyncMock(return_value=True)
        
        result = await management_use_case.update_conversation_title("conv-id", "user-id", "New Title")
        
        assert result is True
        management_use_case.conversation_service.update_conversation_title.assert_called_once_with(
            "conv-id", "user-id", "New Title"
        )
