"""
Test security features
"""
import pytest
from fastapi.testclient import TestClient


def test_security_headers(client: TestClient):
    """Test that security headers are properly set"""
    response = client.get("/health")
    
    # Check required security headers
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-XSS-Protection") == "1; mode=block"
    assert response.headers.get("Referrer-Policy") == "no-referrer"
    assert "Strict-Transport-Security" in response.headers
    assert "Content-Security-Policy" in response.headers
    assert response.headers.get("X-API-Version") == "1.0.0"


def test_cors_headers(client: TestClient):
    """Test CORS headers on preflight request"""
    response = client.options("/api/health")
    
    # Check CORS headers
    assert "Access-Control-Allow-Origin" in response.headers
    assert "Access-Control-Allow-Methods" in response.headers
    assert "Access-Control-Allow-Headers" in response.headers


def test_rate_limiting_basic(client: TestClient):
    """Test basic rate limiting functionality"""
    # Reset rate limiter
    from src.app.core.rate_limiting import rate_limiter
    rate_limiter.requests.clear()
    
    # Make requests up to the limit
    for i in range(100):
        response = client.get("/health")
        assert response.status_code == 200
    
    # Next request should be rate limited
    response = client.get("/health")
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_rate_limiting_different_ips(client: TestClient):
    """Test rate limiting with different IP addresses"""
    from src.app.core.rate_limiting import rate_limiter
    rate_limiter.requests.clear()
    
    # Simulate requests from different IPs
    for i in range(50):
        # First IP
        response = client.get("/health", headers={"X-Forwarded-For": "192.168.1.1"})
        assert response.status_code == 200
        
        # Second IP
        response = client.get("/health", headers={"X-Forwarded-For": "192.168.1.2"})
        assert response.status_code == 200


def test_sql_injection_protection(client: TestClient):
    """Test protection against SQL injection attempts"""
    malicious_payloads = [
        "'; DROP TABLE users; --",
        "1' OR '1'='1",
        "admin'/*",
        "1; DELETE FROM users WHERE 1=1 --"
    ]
    
    for payload in malicious_payloads:
        # Test in query parameters
        response = client.get(f"/health?param={payload}")
        # Should not crash the application
        assert response.status_code in [200, 422, 400]


def test_xss_protection(client: TestClient):
    """Test protection against XSS attempts"""
    xss_payloads = [
        "<script>alert('xss')</script>",
        "javascript:alert('xss')",
        "<img src=x onerror=alert('xss')>",
        "';alert('xss');//"
    ]
    
    for payload in xss_payloads:
        # Test in various endpoints
        response = client.get(f"/health?param={payload}")
        assert response.status_code in [200, 422, 400]
        
        # Ensure payload is not reflected in response
        assert payload not in response.text


def test_input_validation_edge_cases(client: TestClient):
    """Test input validation with edge cases"""
    edge_cases = [
        {"message": ""},  # Empty string
        {"message": None},  # Null value
        {"message": "x" * 100000},  # Very long string
        {"message": 123},  # Wrong type
        {"message": ["array"]},  # Array instead of string
        {"message": {"object": "value"}},  # Object instead of string
    ]
    
    for case in edge_cases:
        response = client.post("/api/chat/mirror", json=case)
        # Should return validation error, not crash
        assert response.status_code in [422, 400]


def test_no_sensitive_data_in_logs(client: TestClient, caplog):
    """Test that sensitive data doesn't appear in logs"""
    # Make a request with sensitive data
    sensitive_data = {
        "email": "test@example.com",
        "password": "secret123"
    }
    
    with caplog.at_level("DEBUG"):
        response = client.post("/api/auth/login", json=sensitive_data)
    
    # Check that password is not in logs
    for record in caplog.records:
        assert "secret123" not in record.getMessage()


def test_error_handling_no_stack_trace(client: TestClient):
    """Test that stack traces are not exposed in production"""
    # Try to trigger an error
    response = client.post("/api/chat/mirror", json={"message": "test"})
    
    # Even if there's an error, stack trace should not be exposed
    if response.status_code >= 500:
        response_text = response.text.lower()
        assert "traceback" not in response_text
        assert "stack trace" not in response_text
        assert "file \"" not in response_text