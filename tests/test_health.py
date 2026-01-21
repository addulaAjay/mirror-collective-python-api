"""
Test health endpoints
"""

from fastapi.testclient import TestClient


def test_basic_health(client: TestClient):
    """Test basic health endpoint"""
    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "Mirror Collective Python API"
    assert "timestamp" in data


def test_api_health(client: TestClient):
    """Test API health endpoint"""
    response = client.get("/api/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "Mirror Collective Python API"
    assert "timestamp" in data


def test_detailed_health(client: TestClient, mock_cognito_client, mock_openai_client):
    """Test detailed health check with dependencies"""
    response = client.get("/health/detailed")
    assert response.status_code == 200

    data = response.json()
    assert "status" in data
    assert "timestamp" in data
    assert "checks" in data
    assert "summary" in data

    # Check that we have the expected health checks
    check_names = [check["name"] for check in data["checks"]]
    assert "cognito" in check_names
    assert "openai" in check_names
    assert "database" in check_names

    # Check summary structure
    summary = data["summary"]
    assert "total_checks" in summary
    assert "healthy_checks" in summary
    assert "unhealthy_checks" in summary


def test_detailed_health_cognito_configured(client: TestClient, mock_cognito_client):
    """Test detailed health shows Cognito as configured"""
    response = client.get("/health/detailed")
    assert response.status_code == 200

    data = response.json()
    cognito_check = next(
        (check for check in data["checks"] if check["name"] == "cognito"), None
    )

    assert cognito_check is not None
    assert cognito_check["details"]["configured"] is True


def test_detailed_health_openai_configured(client: TestClient, mock_openai_client):
    """Test detailed health shows OpenAI as configured"""
    response = client.get("/health/detailed")
    assert response.status_code == 200

    data = response.json()
    openai_check = next(
        (check for check in data["checks"] if check["name"] == "openai"), None
    )

    assert openai_check is not None
    assert openai_check["details"]["configured"] is True
    # Only check api_key_present if it exists in the response
    if "api_key_present" in openai_check["details"]:
        assert openai_check["details"]["api_key_present"] is True
