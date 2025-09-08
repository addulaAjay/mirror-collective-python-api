"""
Enhanced API routes for conversation management
Production-ready endpoints with comprehensive error handling
"""

from fastapi import APIRouter, Depends, Query

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
enhanced_chat_router = APIRouter(prefix="/chat", tags=["Enhanced Chat"])

# Dependency injection for controllers
def get_enhanced_chat_controller():
    """Get enhanced chat controller instance (lazy initialization)"""
    from ..controllers.enhanced_chat_controller import EnhancedChatController
    return EnhancedChatController()


@enhanced_chat_router.post("/enhanced", response_model=EnhancedMirrorChatResponse)
async def enhanced_mirror_chat(
    request: EnhancedMirrorChatRequest,
    current_user=Depends(get_current_user),
    controller=Depends(get_enhanced_chat_controller)
):
    """
    Enhanced mirror chat with persistent conversation management
    
    This endpoint supports both creating new conversations and continuing existing ones.
    Conversations are automatically managed with context preservation.
    
    Args:
        request: Enhanced chat request with conversation context
        current_user: Authenticated user from JWT token
        
    Returns:
        EnhancedMirrorChatResponse: AI response with conversation metadata
    """
    return await controller.handle_enhanced_chat(request, current_user)


@enhanced_chat_router.get("/conversations", response_model=ConversationListResponse)
async def get_user_conversations(
    limit: int = Query(default=50, ge=1, le=100, description="Maximum number of conversations to return"),
    include_archived: bool = Query(default=False, description="Include archived conversations"),
    current_user=Depends(get_current_user),
    controller=Depends(get_enhanced_chat_controller)
):
    """
    Get all conversations for the authenticated user
    
    Args:
        limit: Maximum number of conversations to return (1-100)
        include_archived: Whether to include archived conversations
        current_user: Authenticated user from JWT token
        
    Returns:
        ConversationListResponse: List of user conversations with metadata
    """
    return await controller.get_user_conversations(
        user_id=current_user["sub"],
        limit=limit,
        include_archived=include_archived
    )


@enhanced_chat_router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation_detail(
    conversation_id: str,
    current_user=Depends(get_current_user),
    controller=Depends(get_enhanced_chat_controller)
):
    """
    Get detailed information about a specific conversation
    
    Args:
        conversation_id: The conversation ID
        current_user: Authenticated user from JWT token
        
    Returns:
        ConversationDetailResponse: Conversation details with recent messages
    """
    return await controller.get_conversation_detail(
        conversation_id, 
        current_user
    )


@enhanced_chat_router.post("/conversations/archive")
async def archive_conversation(
    request: ConversationManagementRequest,
    current_user=Depends(get_current_user),
    controller=Depends(get_enhanced_chat_controller)
):
    """
    Archive a conversation (soft delete)
    
    Args:
        request: Conversation management request with conversation ID
        current_user: Authenticated user from JWT token
        
    Returns:
        Dict: Success response
    """
    return await controller.archive_conversation(request, current_user)


@enhanced_chat_router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    current_user=Depends(get_current_user),
    controller=Depends(get_enhanced_chat_controller)
):
    """
    Permanently delete a conversation and all its messages
    
    Args:
        conversation_id: The conversation ID to delete
        current_user: Authenticated user from JWT token
        
    Returns:
        Dict: Success response
    """
    # Create a request object with the conversation ID
    from ..api.models import ConversationManagementRequest
    request = ConversationManagementRequest(conversationId=conversation_id)
    
    return await controller.delete_conversation(request, current_user)


@enhanced_chat_router.put("/conversations/title")
async def update_conversation_title(
    request: UpdateConversationTitleRequest,
    current_user=Depends(get_current_user),
    controller=Depends(get_enhanced_chat_controller)
):
    """
    Update the title of a conversation
    
    Args:
        request: Request with conversation ID and new title
        current_user: Authenticated user from JWT token
        
    Returns:
        Dict: Success response
    """
    return await controller.update_conversation_title(request, current_user)
