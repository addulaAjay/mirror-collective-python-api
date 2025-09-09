"""
Test MirrorGPT chat endpoints (updated after chat consolidation)
NOTE: Basic mirror chat has been replaced with MirrorGPT implementation
"""

from typing import Any, Dict
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient


def test_mirrorgpt_chat_success(
    client: TestClient, mock_openai_client, sample_chat_data
):
    """Test successful MirrorGPT chat"""
    # Convert old format to MirrorGPT format
    mirrorgpt_data = {
        "message": sample_chat_data["message"],
        "include_archetype_analysis": True,
        "use_enhanced_response": True,
    }

    response = client.post("/api/mirrorgpt/chat", json=mirrorgpt_data)
    assert response.status_code == 200

    data = response.json()
    assert data["success"] is True
    assert "data" in data
    assert "response" in data["data"]  # MirrorGPT uses 'response' instead of 'reply'
    assert "archetype_analysis" in data["data"]


def test_mirrorgpt_chat_empty_message(client: TestClient):
    """Test MirrorGPT chat with empty message"""
    chat_data = {"message": ""}

    response = client.post("/api/mirrorgpt/chat", json=chat_data)
    assert response.status_code == 422  # Validation error


def test_mirrorgpt_chat_no_message(client: TestClient):
    """Test MirrorGPT chat without message field"""
    chat_data: Dict[str, Any] = {}

    response = client.post("/api/mirrorgpt/chat", json=chat_data)
    assert response.status_code == 422  # Validation error


def test_mirrorgpt_chat_with_session_context(client: TestClient, mock_openai_client):
    """Test MirrorGPT chat with session context"""
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


def test_mirrorgpt_chat_long_message(client: TestClient, mock_openai_client):
    """Test MirrorGPT chat with long message"""
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


def test_mirrorgpt_chat_special_characters(client: TestClient, mock_openai_client):
    """Test MirrorGPT chat with special characters and emojis"""
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
