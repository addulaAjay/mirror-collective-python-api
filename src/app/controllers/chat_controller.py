"""
Chat controller - handles HTTP requests for mirror chat conversations
"""
from typing import Dict, Any

from ..api.models import MirrorChatRequest, MirrorChatResponse
from ..use_cases.mirror_chat_use_case import MirrorChatUseCase, MirrorChatRequest as UseCaseRequest
from ..services.openai_service import ChatMessage, OpenAIService
import logging

logger = logging.getLogger(__name__)


class ChatController:
    """
    HTTP controller for chat-related endpoints with dependency injection
    """
    
    def __init__(self):
        """Initialize controller with injected chat service and use case"""
        chat_service = OpenAIService()
        self.use_case = MirrorChatUseCase(chat_service)
    
    async def handle_chat(self, req: MirrorChatRequest, current_user: Dict[str, Any]) -> MirrorChatResponse:
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
            # Extract user name from token or request with safe fallbacks
            user_name = req.userName
            if not user_name:
                # Try firstName from the user object
                user_name = current_user.get('firstName')
                if user_name and user_name.strip():  # Make sure it's not empty string
                    user_name = user_name.strip()
                else:
                    user_name = None
            if not user_name:
                # Try standard JWT fields as fallback
                user_name = current_user.get('given_name') or current_user.get('name')
            if not user_name:
                # Use email username as last resort
                email = current_user.get('email')
                if email:
                    user_name = email.split('@')[0]
            
            # Log user name resolution for debugging
            logger.info(f"Current user object: {current_user}")
            logger.info(f"User name resolved: '{user_name}' | Request userName: '{req.userName}' | Token given_name: '{current_user.get('given_name')}' | Token email: '{current_user.get('email')}'")  
            
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
                user_name=user_name
            )
            
            # Execute business logic through use case
            result = await self.use_case.execute(use_case_request)
            
            # Return structured API response
            return MirrorChatResponse(
                success=True,
                data=result.to_dict()
            )
            
        except Exception as err:
            logger.error(f"Error processing mirror chat request: {str(err)}")
            raise