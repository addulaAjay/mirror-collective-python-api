"""
Test MirrorGPT chat endpoints (updated after chat consolidation)
NOTE: Basic mirror chat has been replaced with MirrorGPT implementation
"""

import os
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

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

    async def mock_get_user_with_profile():
        """Mock the enhanced user dependency with complete profile data"""
        return {
            "id": "test-user-123",
            "email": "test@example.com",
            "firstName": "Test",
            "lastName": "User",
            "name": "Test User",  # This is what the enhanced profile provides
            "emailVerified": True,
            "cognitoUsername": "testuser",
            "userStatus": "CONFIRMED",
            "provider": "cognito",
            "roles": ["basic_user"],
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
    from src.app.api.mirrorgpt_routes import (
        get_conversation_service,
        get_mirror_orchestrator,
    )
    from src.app.core.enhanced_auth import get_user_with_profile
    from src.app.core.security import get_current_user

    app.dependency_overrides = {}
    app.dependency_overrides[get_current_user] = mock_get_current_user
    app.dependency_overrides[get_user_with_profile] = mock_get_user_with_profile
    app.dependency_overrides[get_mirror_orchestrator] = mock_get_mirror_orchestrator
    app.dependency_overrides[get_conversation_service] = (
        lambda: get_conversation_service_mock()
    )

    return TestClient(app)


def get_conversation_service_mock():
    """Helper function to create ConversationService mock"""
    mock_conversation_result = Mock()
    mock_conversation_result.conversation_id = "test-conversation-123"

    mock_conversation_service = Mock()
    mock_conversation_service.create_conversation = AsyncMock(
        return_value=mock_conversation_result
    )
    mock_conversation_service.add_message_with_mirrorgpt_analysis = AsyncMock(
        return_value={"success": True}
    )
    mock_conversation_service.add_message = AsyncMock(return_value={"success": True})

    return mock_conversation_service


def test_mirrorgpt_chat_success():
    """Test successful MirrorGPT chat"""
    client = get_clean_test_client()

    # Use a proper MirrorGPT format with required fields
    mirrorgpt_data = {
        "message": (
            "I'm seeking truth and meaning in my life. " "This path feels illuminating."
        ),
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
        "message": (
            "This is a longer message to test how MirrorGPT handles "
            "more complex input. " * 10
        ),
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


def _install_fake_life_anchors(monkeypatch):
    """Point the chat route's LifeAnchorRepo/Structurer at an in-memory table so
    the Life-Anchors flag can be exercised without touching real DynamoDB.

    Returns the FakeTable so callers can inspect persisted rows.
    """
    from unittest.mock import AsyncMock, MagicMock

    from src.app.api import mirrorgpt_routes
    from src.app.repositories.life_anchor_repo import LifeAnchorRepo
    from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

    monkeypatch.setenv("DYNAMODB_LIFE_ANCHORS_TABLE", "mc_life_anchors-test")
    table = FakeTable(
        primary_key=["user_id", "anchor_id"],
        indexes={"status-index": ["user_id", "status"]},
    )
    sess = FakeAioSession({"mc_life_anchors-test": table})
    monkeypatch.setattr(
        mirrorgpt_routes, "LifeAnchorRepo", lambda: LifeAnchorRepo(session=sess)
    )
    stub = MagicMock()
    stub.structure = AsyncMock(return_value=None)
    monkeypatch.setattr(mirrorgpt_routes, "LifeAnchorStructurer", lambda svc: stub)
    return table


def test_mirrorgpt_chat_surfaces_memory_prompt_when_enabled(monkeypatch):
    """Phase 2B: with the flag on, an anchor-worthy message yields a
    memory_prompt in the chat response (heuristic — no LLM in the path)."""
    from src.app.api import mirrorgpt_routes

    monkeypatch.setattr(mirrorgpt_routes, "_LIFE_ANCHORS_ENABLED", True)
    _install_fake_life_anchors(monkeypatch)
    client = get_clean_test_client()

    response = client.post(
        "/api/mirrorgpt/chat",
        json={
            "message": "My wife passed away last year and I still feel lost.",
            "use_enhanced_response": True,
        },
    )
    assert response.status_code == 200
    memory_prompt = response.json()["data"]["memory_prompt"]
    assert memory_prompt is not None
    assert memory_prompt["anchor_type_guess"] == "loss"
    assert memory_prompt["prompt"]


def test_mirrorgpt_chat_no_memory_prompt_when_disabled(monkeypatch):
    """Flag off (default) → no memory_prompt even for an anchor-worthy message."""
    from src.app.api import mirrorgpt_routes

    monkeypatch.setattr(mirrorgpt_routes, "_LIFE_ANCHORS_ENABLED", False)
    client = get_clean_test_client()

    response = client.post(
        "/api/mirrorgpt/chat",
        json={
            "message": "My wife passed away last year.",
            "use_enhanced_response": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["memory_prompt"] is None


def test_chat_inchat_confirm_flow(monkeypatch):
    """Phase 2D: turn 1 appends the 'remember this?' ask + stages a pending;
    turn 2's 'yes' saves the anchor and the reply acknowledges it — all in
    chat, no client involvement."""
    from src.app.api import mirrorgpt_routes

    monkeypatch.setattr(mirrorgpt_routes, "_LIFE_ANCHORS_ENABLED", True)
    _install_fake_life_anchors(monkeypatch)

    client = get_clean_test_client()

    # Turn 1 — anchor-worthy message.
    r1 = client.post(
        "/api/mirrorgpt/chat",
        json={
            "message": "My wife passed away last year and I still feel lost.",
            "conversation_id": "conv-1",
            "use_enhanced_response": True,
        },
    )
    assert r1.status_code == 200
    d1 = r1.json()["data"]
    assert "Life Anchor" in d1["response"]  # the ask was appended to the reply
    assert d1["memory_prompt"] is not None  # structured field also present

    # Turn 2 — natural-language "yes".
    r2 = client.post(
        "/api/mirrorgpt/chat",
        json={
            "message": "yes, please remember that",
            "conversation_id": "conv-1",
            "use_enhanced_response": True,
        },
    )
    assert r2.status_code == 200
    d2 = r2.json()["data"]
    assert "saved it as a Life Anchor" in d2["response"]  # inline acknowledgment
    assert d2["memory_prompt"] is None  # no new prompt on the confirm turn


def test_chat_no_ask_when_life_anchors_disabled(monkeypatch):
    """Flag off → the reply is never mutated with an ask."""
    from src.app.api import mirrorgpt_routes

    monkeypatch.setattr(mirrorgpt_routes, "_LIFE_ANCHORS_ENABLED", False)
    client = get_clean_test_client()

    r = client.post(
        "/api/mirrorgpt/chat",
        json={
            "message": "My wife passed away last year.",
            "conversation_id": "conv-1",
            "use_enhanced_response": True,
        },
    )
    assert r.status_code == 200
    assert "Life Anchor" not in r.json()["data"]["response"]
