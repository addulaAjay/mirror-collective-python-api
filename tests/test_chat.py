"""
Test MirrorGPT chat endpoints (updated after chat consolidation)
NOTE: Basic mirror chat has been replaced with MirrorGPT implementation
"""

import os
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

# Set up environment for clean testing
test_env = {
    "COGNITO_USER_POOL_ID": "testpoolid123",
    "COGNITO_CLIENT_ID": "testclientid123",
    "OPENAI_API_KEY": "test-openai-key",
    "AWS_REGION": "us-east-1",
    "LOG_LEVEL": "DEBUG",
    "ENVIRONMENT": "test",
    "NODE_ENV": "test",
    "DEBUG": "true",
    "DYNAMODB_TABLE_NAME": "test-user-profiles",
    "DISABLE_AUTH": "true",
}

for key, value in test_env.items():
    os.environ[key] = value


def get_clean_test_client():
    """Get a clean test client with proper mocking"""
    from src.app.handler import app

    # Create mock functions
    async def mock_get_current_user():
        return {
            "id": "test-user-123",
            "email": "test@example.com",
            "given_name": "Test",
            "family_name": "User",
        }

    def mock_get_mirror_orchestrator():
        mock_orchestrator = Mock()
        mock_orchestrator.process_mirror_chat = AsyncMock(
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
        return mock_orchestrator

    # Override dependencies
    from src.app.api.mirrorgpt_routes import get_mirror_orchestrator
    from src.app.core.security import get_current_user

    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = mock_get_current_user
    app.dependency_overrides[get_mirror_orchestrator] = mock_get_mirror_orchestrator

    return TestClient(app)


def test_mirrorgpt_chat_success():
    """Test successful MirrorGPT chat"""
    client = get_clean_test_client()

    # Use a proper MirrorGPT format with required fields
    mirrorgpt_data = {
        "message": "I'm seeking truth and meaning in my life. This path feels illuminating.",
        "include_archetype_analysis": True,
        "use_enhanced_response": True,
    }

    response = client.post("/api/mirrorgpt/chat", json=mirrorgpt_data)

    # Check what the actual error is if it fails
    if response.status_code != 200:
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")

    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert "data" in data
    assert "response" in data["data"]  # MirrorGPT uses 'response' instead of 'reply'
    assert "archetype_analysis" in data["data"]


def test_mirrorgpt_chat_empty_message():
    """Test MirrorGPT chat with empty message"""
    client = get_clean_test_client()
    chat_data = {"message": ""}

    response = client.post("/api/mirrorgpt/chat", json=chat_data)
    assert response.status_code == 422  # Validation error


def test_mirrorgpt_chat_no_message():
    """Test MirrorGPT chat without message field"""
    client = get_clean_test_client()
    chat_data: Dict[str, Any] = {}

    response = client.post("/api/mirrorgpt/chat", json=chat_data)
    assert response.status_code == 422  # Validation error


def test_mirrorgpt_chat_with_session_context():
    """Test MirrorGPT chat with session context"""
    client = get_clean_test_client()

    chat_data = {
        "message": "Continue our conversation about my goals",
        "session_id": "test-session-123",
        "conversation_id": "test-conversation-456",
        "include_archetype_analysis": True,
        "use_enhanced_response": True,
    }

    response = client.post("/api/mirrorgpt/chat", json=chat_data)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert "response" in data["data"]  # MirrorGPT uses 'response' instead of 'reply'


def test_mirrorgpt_chat_long_message():
    """Test MirrorGPT chat with long message"""
    client = get_clean_test_client()

    chat_data = {
        "message": "This is a longer message to test how MirrorGPT handles more complex input. "
        * 10,
        "include_archetype_analysis": True,
        "use_enhanced_response": True,
    }

    response = client.post("/api/mirrorgpt/chat", json=chat_data)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert "response" in data["data"]
    assert "archetype_analysis" in data["data"]


def test_mirrorgpt_chat_special_characters():
    """Test MirrorGPT chat with special characters and emojis"""
    client = get_clean_test_client()

    chat_data = {
        "message": "Hello! 🌟 How are you? Special chars: @#$%^&*()",
        "include_archetype_analysis": True,
        "use_enhanced_response": True,
    }

    response = client.post("/api/mirrorgpt/chat", json=chat_data)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert "response" in data["data"]
