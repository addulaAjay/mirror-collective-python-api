"""
Test configuration and fixtures
"""
import os
import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from typing import Generator, Any

# Set test environment variables before importing app
os.environ.update({
    'COGNITO_USER_POOL_ID': 'test-pool-id',
    'COGNITO_CLIENT_ID': 'test-client-id',
    'OPENAI_API_KEY': 'test-openai-key',
    'AWS_REGION': 'us-east-1',
    'LOG_LEVEL': 'DEBUG',
    'ENVIRONMENT': 'test'
})

from src.app.handler import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Test client fixture"""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def mock_cognito_client():
    """Mock Cognito client"""
    with patch('boto3.client') as mock_client:
        mock_cognito = Mock()
        mock_client.return_value = mock_cognito
        
        # Mock successful responses
        mock_cognito.sign_up.return_value = {
            'UserSub': 'test-user-sub',
            'CodeDeliveryDetails': {
                'Destination': 'test@example.com',
                'DeliveryMedium': 'EMAIL'
            },
            'UserConfirmed': False
        }
        
        mock_cognito.admin_initiate_auth.return_value = {
            'AuthenticationResult': {
                'AccessToken': 'test-access-token',
                'RefreshToken': 'test-refresh-token',
                'IdToken': 'test-id-token'
            }
        }
        
        mock_cognito.describe_user_pool.return_value = {
            'UserPool': {
                'Name': 'Test Pool',
                'Id': 'test-pool-id'
            }
        }
        
        yield mock_cognito


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client"""
    with patch('openai.OpenAI') as mock_openai:
        mock_client = Mock()
        mock_openai.return_value = mock_client
        
        # Mock chat completion
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Test AI response"
        mock_client.chat.completions.create.return_value = mock_response
        
        # Mock models list
        mock_models = Mock()
        mock_models.data = [Mock(id='gpt-4'), Mock(id='gpt-3.5-turbo')]
        mock_client.models.list.return_value = mock_models
        
        yield mock_client


@pytest.fixture
def sample_user_data():
    """Sample user registration data"""
    return {
        "email": "test@example.com",
        "password": "TestPassword123!",
        "fullName": "Test User"
    }


@pytest.fixture
def sample_login_data():
    """Sample login data"""
    return {
        "email": "test@example.com",
        "password": "TestPassword123!"
    }


@pytest.fixture
def sample_chat_data():
    """Sample chat data"""
    return {
        "message": "Hello, this is a test message",
        "userName": "John",
        "conversationHistory": [
            {
                "role": "user",
                "content": "Previous message"
            },
            {
                "role": "assistant", 
                "content": "Previous response"
            }
        ]
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