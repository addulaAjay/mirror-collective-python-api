"""
Test chat endpoints
"""
import pytest
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient


def test_mirror_chat_success(client: TestClient, mock_openai_client, sample_chat_data):
    """Test successful mirror chat"""
    response = client.post("/api/chat/mirror", json=sample_chat_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True
    assert "data" in data
    assert "reply" in data["data"]
    assert "timestamp" in data["data"]


def test_mirror_chat_empty_message(client: TestClient):
    """Test mirror chat with empty message"""
    chat_data = {"message": ""}
    
    response = client.post("/api/chat/mirror", json=chat_data)
    assert response.status_code == 422  # Validation error


def test_mirror_chat_no_message(client: TestClient):
    """Test mirror chat without message field"""
    chat_data = {}
    
    response = client.post("/api/chat/mirror", json=chat_data)
    assert response.status_code == 422  # Validation error


def test_mirror_chat_with_conversation_history(client: TestClient, mock_openai_client):
    """Test mirror chat with conversation history"""
    chat_data = {
        "message": "Continue our conversation",
        "conversationHistory": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"}
        ]
    }
    
    response = client.post("/api/chat/mirror", json=chat_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True
    assert "reply" in data["data"]


def test_mirror_chat_invalid_conversation_history(client: TestClient):
    """Test mirror chat with invalid conversation history"""
    chat_data = {
        "message": "Test message",
        "conversationHistory": [
            {"role": "invalid_role", "content": "Invalid"}
        ]
    }
    
    response = client.post("/api/chat/mirror", json=chat_data)
    assert response.status_code == 422  # Validation error


def test_mirror_chat_openai_error(client: TestClient, mock_openai_client, sample_chat_data):
    """Test mirror chat with OpenAI service error"""
    mock_openai_client.chat.completions.create.side_effect = Exception("OpenAI API Error")
    
    response = client.post("/api/chat/mirror", json=sample_chat_data)
    assert response.status_code == 500  # Internal server error


def test_mirror_chat_rate_limiting(client: TestClient, mock_openai_client):
    """Test rate limiting on mirror chat endpoint"""
    chat_data = {"message": "Test rate limiting"}
    
    # Make requests up to the rate limit
    for i in range(100):  # Rate limit is 100 requests per minute
        response = client.post("/api/chat/mirror", json=chat_data)
        assert response.status_code == 200
    
    # Next request should be rate limited
    response = client.post("/api/chat/mirror", json=chat_data)
    assert response.status_code == 429  # Too many requests
    assert "Retry-After" in response.headers


def test_mirror_chat_large_message(client: TestClient, mock_openai_client):
    """Test mirror chat with large message"""
    large_message = "x" * 10000  # Very long message
    chat_data = {"message": large_message}
    
    response = client.post("/api/chat/mirror", json=chat_data)
    assert response.status_code == 200  # Should handle large messages
    
    data = response.json()
    assert data["success"] is True


def test_mirror_chat_special_characters(client: TestClient, mock_openai_client):
    """Test mirror chat with special characters"""
    chat_data = {
        "message": "Hello! 🌟 How are you? Special chars: @#$%^&*()",
        "conversationHistory": [
            {"role": "user", "content": "Previous message with émojis 🎉"}
        ]
    }
    
    response = client.post("/api/chat/mirror", json=chat_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True