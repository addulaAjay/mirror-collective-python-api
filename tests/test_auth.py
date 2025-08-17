"""
Test authentication endpoints
"""
import pytest
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from botocore.exceptions import ClientError


def test_register_success(client: TestClient, mock_cognito_client, sample_user_data):
    """Test successful user registration"""
    response = client.post("/api/auth/register", json=sample_user_data)
    assert response.status_code == 201
    
    data = response.json()
    assert data["success"] is True
    assert "data" in data
    assert data["data"]["userSub"] == "test-user-sub"


def test_register_invalid_email(client: TestClient, sample_user_data):
    """Test registration with invalid email"""
    sample_user_data["email"] = "invalid-email"
    response = client.post("/api/auth/register", json=sample_user_data)
    assert response.status_code == 422  # Validation error


def test_register_weak_password(client: TestClient, sample_user_data):
    """Test registration with weak password"""
    sample_user_data["password"] = "weak"
    response = client.post("/api/auth/register", json=sample_user_data)
    assert response.status_code == 422  # Validation error


def test_register_cognito_error(client: TestClient, mock_cognito_client, sample_user_data):
    """Test registration with Cognito error"""
    mock_cognito_client.sign_up.side_effect = ClientError(
        error_response={'Error': {'Code': 'UsernameExistsException', 'Message': 'User already exists'}},
        operation_name='SignUp'
    )
    
    response = client.post("/api/auth/register", json=sample_user_data)
    assert response.status_code == 409  # Conflict


def test_login_success(client: TestClient, mock_cognito_client, sample_login_data):
    """Test successful login"""
    response = client.post("/api/auth/login", json=sample_login_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True
    assert "data" in data
    assert data["data"]["accessToken"] == "test-access-token"
    assert data["data"]["refreshToken"] == "test-refresh-token"


def test_login_invalid_credentials(client: TestClient, mock_cognito_client, sample_login_data):
    """Test login with invalid credentials"""
    mock_cognito_client.admin_initiate_auth.side_effect = ClientError(
        error_response={'Error': {'Code': 'NotAuthorizedException', 'Message': 'Invalid credentials'}},
        operation_name='AdminInitiateAuth'
    )
    
    response = client.post("/api/auth/login", json=sample_login_data)
    assert response.status_code == 401


def test_login_user_not_confirmed(client: TestClient, mock_cognito_client, sample_login_data):
    """Test login with unconfirmed user"""
    mock_cognito_client.admin_initiate_auth.side_effect = ClientError(
        error_response={'Error': {'Code': 'UserNotConfirmedException', 'Message': 'User not confirmed'}},
        operation_name='AdminInitiateAuth'
    )
    
    response = client.post("/api/auth/login", json=sample_login_data)
    assert response.status_code == 401


def test_verify_email_success(client: TestClient, mock_cognito_client):
    """Test successful email verification"""
    verification_data = {
        "email": "test@example.com",
        "verificationCode": "123456"
    }
    
    mock_cognito_client.confirm_sign_up.return_value = {}
    
    response = client.post("/api/auth/verify-email", json=verification_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True


def test_verify_email_invalid_code(client: TestClient, mock_cognito_client):
    """Test email verification with invalid code"""
    verification_data = {
        "email": "test@example.com",
        "verificationCode": "invalid"
    }
    
    mock_cognito_client.confirm_sign_up.side_effect = ClientError(
        error_response={'Error': {'Code': 'CodeMismatchException', 'Message': 'Invalid code'}},
        operation_name='ConfirmSignUp'
    )
    
    response = client.post("/api/auth/verify-email", json=verification_data)
    assert response.status_code == 422


def test_forgot_password_success(client: TestClient, mock_cognito_client):
    """Test successful forgot password request"""
    forgot_data = {"email": "test@example.com"}
    
    mock_cognito_client.forgot_password.return_value = {}
    
    response = client.post("/api/auth/forgot-password", json=forgot_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True


def test_reset_password_success(client: TestClient, mock_cognito_client):
    """Test successful password reset"""
    reset_data = {
        "email": "test@example.com",
        "resetCode": "123456",
        "newPassword": "NewPassword123!"
    }
    
    mock_cognito_client.confirm_forgot_password.return_value = {}
    
    response = client.post("/api/auth/reset-password", json=reset_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True


def test_refresh_token_success(client: TestClient, mock_cognito_client):
    """Test successful token refresh"""
    refresh_data = {"refreshToken": "test-refresh-token"}
    
    mock_cognito_client.initiate_auth.return_value = {
        'AuthenticationResult': {
            'AccessToken': 'new-access-token',
            'IdToken': 'new-id-token'
        }
    }
    
    response = client.post("/api/auth/refresh", json=refresh_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True
    assert data["data"]["accessToken"] == "new-access-token"


def test_resend_verification_success(client: TestClient, mock_cognito_client):
    """Test successful verification code resend"""
    resend_data = {"email": "test@example.com"}
    
    mock_cognito_client.resend_confirmation_code.return_value = {}
    
    response = client.post("/api/auth/resend-verification", json=resend_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["success"] is True