"""
Enhanced API routes for conversation management
Production-ready endpoints with comprehensive error handling
"""

from typing import Optional
from fastapi import APIRouter, Depends, Query

from ..controllers.enhanced_chat_controller import EnhancedChatController
from ..api.models import (
    EnhancedMirrorChatRequest,
    EnhancedMirrorChatResponse,
    ConversationListResponse,
    ConversationDetailResponse,
    ConversationManagementRequest,
    UpdateConversationTitleRequest
)
from ..core.security import get_current_user

# Create router for enhanced chat endpoints
enhanced_chat_router = APIRouter(prefix="/api/chat", tags=["Enhanced Chat"])

# Initialize controller
enhanced_chat_controller = EnhancedChatController()


@enhanced_chat_router.post("/enhanced", response_model=EnhancedMirrorChatResponse)
async def enhanced_mirror_chat(
    request: EnhancedMirrorChatRequest,
    current_user=Depends(get_current_user)
):
    """
    Enhanced mirror chat endpoint with conversation management
    
    Features:
    - Persistent conversation history
    - Automatic conversation creation
    - Conversation context management
    - User personalization
    
    Args:
        request: Enhanced chat request with conversation context
        current_user: Authenticated user from JWT token
        
    Returns:
        EnhancedMirrorChatResponse: AI response with conversation metadata
    """
    return await enhanced_chat_controller.handle_enhanced_chat(request, current_user)


@enhanced_chat_router.get("/conversations", response_model=ConversationListResponse)
async def get_user_conversations(
    limit: int = Query(default=50, ge=1, le=100, description="Maximum number of conversations to return"),
    include_archived: bool = Query(default=False, description="Include archived conversations"),
    current_user=Depends(get_current_user)
):
    """
    Get all conversations for the authenticated user
    
    Args:
        limit: Maximum number of conversations to return (1-100)
        include_archived: Whether to include archived conversations
        current_user: Authenticated user from JWT token
        
    Returns:
        ConversationListResponse: List of user's conversations with metadata
    """
    return await enhanced_chat_controller.get_user_conversations(
        current_user, 
        limit, 
        include_archived
    )


@enhanced_chat_router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation_detail(
    conversation_id: str,
    current_user=Depends(get_current_user)
):
    """
    Get detailed information about a specific conversation
    
    Args:
        conversation_id: The conversation ID
        current_user: Authenticated user from JWT token
        
    Returns:
        ConversationDetailResponse: Conversation details with recent messages
    """
    return await enhanced_chat_controller.get_conversation_detail(
        conversation_id, 
        current_user
    )


@enhanced_chat_router.post("/conversations/archive")
async def archive_conversation(
    request: ConversationManagementRequest,
    current_user=Depends(get_current_user)
):
    """
    Archive a conversation (soft delete)
    
    Args:
        request: Conversation management request with conversation ID
        current_user: Authenticated user from JWT token
        
    Returns:
        Dict: Success response
    """
    return await enhanced_chat_controller.archive_conversation(request, current_user)


@enhanced_chat_router.delete("/conversations")
async def delete_conversation(
    request: ConversationManagementRequest,
    current_user=Depends(get_current_user)
):
    """
    Delete a conversation permanently (hard delete)
    
    Warning: This action cannot be undone and will delete all messages in the conversation
    
    Args:
        request: Conversation management request with conversation ID
        current_user: Authenticated user from JWT token
        
    Returns:
        Dict: Success response
    """
    return await enhanced_chat_controller.delete_conversation(request, current_user)


@enhanced_chat_router.put("/conversations/title")
async def update_conversation_title(
    request: UpdateConversationTitleRequest,
    current_user=Depends(get_current_user)
):
    """
    Update a conversation's title
    
    Args:
        request: Title update request with conversation ID and new title
        current_user: Authenticated user from JWT token
        
    Returns:
        Dict: Success response
    """
    return await enhanced_chat_controller.update_conversation_title(request, current_user)
