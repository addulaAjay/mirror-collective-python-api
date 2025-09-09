"""
Test configuration and fixtures
"""

import os
from typing import Any, Generator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

# Set test environment variables before importing app
os.environ.update(
    {
        "COGNITO_USER_POOL_ID": "testpoolid123",
        "COGNITO_CLIENT_ID": "testclientid123",
        "OPENAI_API_KEY": "test-openai-key",
        "AWS_REGION": "us-east-1",
        "LOG_LEVEL": "DEBUG",
        "ENVIRONMENT": "test",
        "NODE_ENV": "test",
        "DEBUG": "true",
        "DYNAMODB_TABLE_NAME": "test-user-profiles",
        "DISABLE_AUTH": "true",  # Disable auth for tests
    }
)

# Mock boto3 before any imports
mock_cognito = Mock()
mock_cognito.sign_up.return_value = {
    "UserSub": "test-user-sub",
    "CodeDeliveryDetails": {
        "Destination": "test@example.com",
        "DeliveryMedium": "EMAIL",
    },
    "UserConfirmed": False,
}

mock_cognito.admin_initiate_auth.return_value = {
    "AuthenticationResult": {
        "AccessToken": "test-access-token",
        "RefreshToken": "test-refresh-token",
        "IdToken": "test-id-token",
    }
}

mock_cognito.admin_get_user.return_value = {
    "Username": "test@example.com",
    "UserAttributes": [
        {"Name": "email", "Value": "test@example.com"},
        {"Name": "given_name", "Value": "Test"},
        {"Name": "family_name", "Value": "User"},
        {"Name": "email_verified", "Value": "true"},
    ],
    "UserStatus": "CONFIRMED",
    "Enabled": True,
}

mock_cognito.initiate_auth.return_value = {
    "AuthenticationResult": {
        "AccessToken": "new-access-token",
        "RefreshToken": "new-refresh-token",
        "IdToken": "new-id-token",
    }
}

mock_cognito.confirm_sign_up.return_value = {}
mock_cognito.forgot_password.return_value = {}
mock_cognito.confirm_forgot_password.return_value = {}
mock_cognito.resend_confirmation_code.return_value = {}

# Start global patches before any app imports
boto3_patcher = patch("boto3.client", return_value=mock_cognito)
dynamodb_service_patcher = patch("src.app.services.dynamodb_service.DynamoDBService")
user_service_patcher = patch("src.app.services.user_service.UserService")
openai_service_patcher = patch("src.app.services.openai_service.OpenAI")
# Also patch OpenAI import for health checks
openai_client_patcher = patch("src.app.core.health_checks.OpenAI")
# Mock ConversationService for MirrorGPT
conversation_service_patcher = patch(
    "src.app.services.conversation_service.ConversationService"
)
# Patch OpenAI in MirrorOrchestrator dependency
openai_orchestrator_patcher = patch("src.app.api.mirrorgpt_routes.OpenAIService")
# Patch DynamoDB in MirrorOrchestrator dependency
dynamodb_orchestrator_patcher = patch("src.app.api.mirrorgpt_routes.DynamoDBService")

# Start the patchers
boto3_patcher.start()
mock_dynamodb_service = dynamodb_service_patcher.start()
mock_user_service_class = user_service_patcher.start()
mock_openai_class = openai_service_patcher.start()
mock_openai_health_class = openai_client_patcher.start()
mock_conversation_service_class = conversation_service_patcher.start()
mock_openai_orchestrator_class = openai_orchestrator_patcher.start()
mock_dynamodb_orchestrator_class = dynamodb_orchestrator_patcher.start()


# Mock authentication to return test user
async def mock_get_current_user(*args, **kwargs):
    return {
        "sub": "test-user-123",
        "id": "test-user-123",  # Added id field for MirrorGPT routes
        "email": "test@example.com",
        "given_name": "Test",
        "family_name": "User",
    }


# Mock MirrorOrchestrator
mock_orchestrator_instance = Mock()


def mock_get_mirror_orchestrator():
    return mock_orchestrator_instance


# Configure MirrorOrchestrator mock to return successful chat response
mock_orchestrator_instance.process_mirror_chat = AsyncMock(
    return_value={
        "success": True,
        "response": "Test MirrorGPT response",
        "archetype_analysis": {
            "primary_archetype": "Seeker",
            "secondary_archetype": None,
            "confidence_score": 0.85,
            "symbolic_elements": ["light", "path"],
            "emotional_markers": {"valence": 0.6, "arousal": 0.4},
            "narrative_position": {"stage": "beginning"},
            "active_loops": [],
        },
        "change_detection": {
            "change_detected": False,
            "mirror_moment": False,
            "changes": [],
        },
        "suggested_practice": "Contemplative journaling",
        "confidence_breakdown": {
            "overall": 0.85,
            "archetype": 0.85,
            "symbol": 0.7,
            "emotion": 0.6,
        },
        "session_metadata": {
            "session_id": "test-session",
            "timestamp": "2025-01-01T00:00:00Z",
        },
    }
)

# Configure the mocked services
mock_user_service_instance = Mock()
mock_user_service_class.return_value = mock_user_service_instance

# Mock user profile
mock_profile = Mock()
mock_profile.email = "test@example.com"
mock_profile.full_name = "Test User"
mock_profile.chat_name = "Test"
mock_profile.user_id = "mock-user-123"

# Configure async methods
mock_user_service_instance.get_user_profile = AsyncMock(return_value=mock_profile)
mock_user_service_instance.create_user_profile_from_cognito = AsyncMock(
    return_value=mock_profile
)
mock_user_service_instance.record_chat_activity = AsyncMock(return_value=None)
mock_user_service_instance.record_login_activity = AsyncMock(return_value=None)
mock_user_service_instance.record_logout_activity = AsyncMock(return_value=None)
mock_user_service_instance.delete_user_account = AsyncMock(return_value=True)
mock_user_service_instance.get_user_chat_name = AsyncMock(return_value="Test")
mock_user_service_instance.increment_conversation_count = AsyncMock(return_value=None)
mock_user_service_instance.sync_user_with_cognito = AsyncMock(return_value=mock_profile)

# Mock DynamoDB service
mock_dynamodb_service_instance = Mock()
mock_dynamodb_service.return_value = mock_dynamodb_service_instance
mock_dynamodb_service_instance.get_user_profile = AsyncMock(return_value=mock_profile)
mock_dynamodb_service_instance.create_user_profile = AsyncMock(
    return_value=mock_profile
)
mock_dynamodb_service_instance.update_user_profile = AsyncMock(
    return_value=mock_profile
)
mock_dynamodb_service_instance.record_user_activity = AsyncMock(return_value=None)

# Mock MirrorGPT specific methods
mock_dynamodb_service_instance.get_user_archetype_profile = AsyncMock(return_value=None)
mock_dynamodb_service_instance.save_user_archetype_profile = AsyncMock(return_value={})
mock_dynamodb_service_instance.save_echo_signal = AsyncMock(return_value={})
mock_dynamodb_service_instance.get_user_mirror_moments = AsyncMock(return_value=[])
mock_dynamodb_service_instance.get_user_pattern_loops = AsyncMock(return_value=[])
mock_dynamodb_service_instance.save_mirror_moment = AsyncMock(return_value={})
mock_dynamodb_service_instance.acknowledge_mirror_moment = AsyncMock(return_value=True)

# Mock ConversationService for MirrorGPT
mock_conversation_service_instance = Mock()
mock_conversation_service_class.return_value = mock_conversation_service_instance
mock_conversation_service_instance.get_user_mirrorgpt_signals = AsyncMock(
    return_value=[]
)

# Mock OpenAI service
mock_openai_instance = Mock()
mock_openai_class.return_value = mock_openai_instance
mock_response = Mock()
mock_response.choices = [Mock()]
mock_response.choices[0].message.content = "Test AI response from mocked OpenAI"
mock_openai_instance.chat.completions.create.return_value = mock_response

# Add async methods for MirrorGPT
mock_openai_instance.send_async = AsyncMock(return_value="Test enhanced AI response")

# Mock OpenAI for health checks
mock_openai_health_instance = Mock()
mock_openai_health_class.return_value = mock_openai_health_instance

# Create mock models list response
mock_models_response = Mock()
mock_models_response.data = [
    Mock(id="gpt-3.5-turbo"),
    Mock(id="gpt-4"),
    Mock(id="text-davinci-003"),
]
mock_openai_health_instance.models.list.return_value = mock_models_response

from src.app.api.mirrorgpt_routes import get_mirror_orchestrator

# Override dependencies using FastAPI's built-in mechanism
from src.app.core.security import get_current_user

# Now import app after mocking
from src.app.handler import app

# Clear any existing overrides and set clean ones
app.dependency_overrides = {}
app.dependency_overrides[get_current_user] = mock_get_current_user
app.dependency_overrides[get_mirror_orchestrator] = mock_get_mirror_orchestrator

# Store references for test cleanup
GLOBAL_PATCHES = [
    boto3_patcher,
    dynamodb_service_patcher,
    user_service_patcher,
    openai_service_patcher,
    openai_client_patcher,
    conversation_service_patcher,
    openai_orchestrator_patcher,
    dynamodb_orchestrator_patcher,
]


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Test client fixture"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def mock_cognito_client():
    """Mock Cognito client - returns the module level mock"""
    return mock_cognito


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client"""
    with patch("src.app.services.openai_service.OpenAI") as mock_openai:
        mock_client = Mock()
        mock_openai.return_value = mock_client

        # Mock chat completion
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Test AI response"
        mock_client.chat.completions.create.return_value = mock_response

        # Mock models list
        mock_models = Mock()
        mock_models.data = [Mock(id="gpt-4"), Mock(id="gpt-3.5-turbo")]
        mock_client.models.list.return_value = mock_models

        yield mock_client


@pytest.fixture
def sample_user_data():
    """Sample user registration data"""
    return {
        "email": "test@example.com",
        "password": "TestPassword123!",
        "fullName": "Test User",
    }


@pytest.fixture
def sample_login_data():
    """Sample login data"""
    return {"email": "test@example.com", "password": "TestPassword123!"}


@pytest.fixture
def sample_chat_data():
    """Sample chat data"""
    return {
        "message": "Hello, this is a test message",
        "userName": "John",
        "conversationHistory": [
            {"role": "user", "content": "Previous message"},
            {"role": "assistant", "content": "Previous response"},
        ],
    }


@pytest.fixture
def mock_jwt_token():
    """Mock JWT token for authentication tests"""
    return "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXVzZXIiLCJleHAiOjk5OTk5OTk5OTl9.test-signature"


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter between tests"""
    from src.app.core.rate_limiting import rate_limiter

    rate_limiter.requests.clear()
    yield
    rate_limiter.requests.clear()


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    """Ensure dependency overrides are properly set for each test"""
    # Re-establish clean dependency overrides before each test
    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = mock_get_current_user
    app.dependency_overrides[get_mirror_orchestrator] = mock_get_mirror_orchestrator
    yield
    # Keep overrides for consistency


# Cleanup function to stop global patches
def pytest_sessionfinish(session, exitstatus):
    """Stop global patches after all tests"""
    for patcher in GLOBAL_PATCHES:
        patcher.stop()
