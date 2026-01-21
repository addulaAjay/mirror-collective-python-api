"""
Test Pydantic models
"""

import pytest
from pydantic import ValidationError

from src.app.api.models import (
    EmailVerificationRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MirrorGPTChatRequest,
    ResetPasswordRequest,
    UserRegistrationRequest,
)


def test_user_registration_request_valid():
    """Test valid user registration request"""
    data = {
        "email": "test@example.com",
        "password": "ValidPass123!",
        "fullName": "John Doe",
    }

    request = UserRegistrationRequest(**data)
    assert request.email == "test@example.com"
    assert request.password == "ValidPass123!"
    assert request.fullName == "John Doe"


def test_user_registration_request_invalid_email():
    """Test user registration with invalid email"""
    data = {
        "email": "invalid-email",
        "password": "ValidPass123!",
        "fullName": "John Doe",
    }

    with pytest.raises(ValidationError) as exc_info:
        UserRegistrationRequest(**data)

    assert "email" in str(exc_info.value)


def test_user_registration_request_short_password():
    """Test user registration with short password"""
    data = {"email": "test@example.com", "password": "short", "fullName": "John Doe"}

    with pytest.raises(ValidationError) as exc_info:
        UserRegistrationRequest(**data)

    assert "password" in str(exc_info.value)


def test_user_registration_request_invalid_name():
    """Test user registration with invalid name"""
    data = {
        "email": "test@example.com",
        "password": "ValidPass123!",
        "fullName": "J",  # Too short
    }

    with pytest.raises(ValidationError) as exc_info:
        UserRegistrationRequest(**data)

    assert "fullName" in str(exc_info.value)


def test_login_request_valid():
    """Test valid login request"""
    data = {"email": "test@example.com", "password": "password123"}

    request = LoginRequest(**data)
    assert request.email == "test@example.com"
    assert request.password == "password123"


def test_mirrorgpt_chat_request_valid():
    """Test valid MirrorGPT chat request"""
    data = {
        "message": "Hello, how are you?",
        "session_id": "test-session",
        "include_archetype_analysis": True,
    }

    request = MirrorGPTChatRequest(**data)
    assert request.message == "Hello, how are you?"
    assert request.session_id == "test-session"
    assert request.include_archetype_analysis is True


def test_mirrorgpt_chat_request_minimal():
    """Test MirrorGPT chat request with only required fields"""
    data = {"message": "Hello, how are you?"}

    request = MirrorGPTChatRequest(**data)
    assert request.message == "Hello, how are you?"
    assert request.session_id is None
    assert request.include_archetype_analysis is True  # Default value


def test_mirrorgpt_chat_request_empty_message():
    """Test MirrorGPT chat request with empty message"""
    data = {"message": ""}

    with pytest.raises(ValidationError) as exc_info:
        MirrorGPTChatRequest(**data)

    assert "message" in str(exc_info.value)


def test_forgot_password_request_valid():
    """Test valid forgot password request"""
    data = {"email": "test@example.com"}

    request = ForgotPasswordRequest(**data)
    assert request.email == "test@example.com"


def test_reset_password_request_valid():
    """Test valid reset password request"""
    data = {
        "email": "test@example.com",
        "resetCode": "123456",
        "newPassword": "NewPass123!",
    }

    request = ResetPasswordRequest(**data)
    assert request.email == "test@example.com"
    assert request.resetCode == "123456"
    assert request.newPassword == "NewPass123!"


def test_email_verification_request_valid():
    """Test valid email verification request"""
    data = {"email": "test@example.com", "verificationCode": "123456"}

    request = EmailVerificationRequest(**data)
    assert request.email == "test@example.com"
    assert request.verificationCode == "123456"


def test_password_pattern_validation():
    """Test password pattern validation"""
    valid_passwords = ["ValidPass123!", "Another1@", "Test123$", "MyPassword2&"]

    invalid_passwords = [
        "short",  # Too short
        "nouppercase1!",  # No uppercase
        "NOLOWERCASE1!",  # No lowercase
        "NoDigits!",  # No digits
        "NoSpecial123",  # No special chars
    ]

    # Test valid passwords
    for password in valid_passwords:
        data = {
            "email": "test@example.com",
            "password": password,
            "fullName": "Test User",
        }
        request = UserRegistrationRequest(**data)
        assert request.password == password

    # Test invalid passwords
    for password in invalid_passwords:
        data = {
            "email": "test@example.com",
            "password": password,
            "fullName": "Test User",
        }
        with pytest.raises(ValidationError):
            UserRegistrationRequest(**data)


def test_user_profile_to_dynamodb_item_empty_email():
    """Test that UserProfile.to_dynamodb_item() properly handles empty email"""
    from src.app.models.user_profile import UserProfile, UserStatus

    # Test with empty email string
    profile = UserProfile(
        user_id="test-user-123", email="", status=UserStatus.CONFIRMED  # Empty string
    )

    item = profile.to_dynamodb_item()

    # Empty email should be filtered out to prevent DynamoDB index issues
    assert "email" not in item
    assert item["user_id"] == "test-user-123"
    assert item["status"] == "CONFIRMED"


def test_user_profile_to_dynamodb_item_valid_email():
    """Test that UserProfile.to_dynamodb_item() preserves valid email"""
    from src.app.models.user_profile import UserProfile, UserStatus

    # Test with valid email
    profile = UserProfile(
        user_id="test-user-123", email="test@example.com", status=UserStatus.CONFIRMED
    )

    item = profile.to_dynamodb_item()

    # Valid email should be preserved
    assert item["email"] == "test@example.com"
    assert item["user_id"] == "test-user-123"
    assert item["status"] == "CONFIRMED"


def test_user_profile_from_cognito_user_missing_email():
    """Test creating UserProfile from Cognito data with missing email"""
    from src.app.models.user_profile import UserProfile

    cognito_data = {
        "username": "test-user-123",
        "UserStatus": "CONFIRMED",
        "UserAttributes": [
            {"Name": "given_name", "Value": "John"},
            {"Name": "family_name", "Value": "Doe"},
            # Note: no email attribute
        ],
    }

    # Should create profile but with empty email
    profile = UserProfile.from_cognito_user(cognito_data, "test-user-123")
    assert profile.user_id == "test-user-123"
    assert profile.email == ""  # Empty string for missing email
    assert profile.first_name == "John"
    assert profile.last_name == "Doe"


def test_user_profile_from_cognito_user_transformed_format():
    """Test creating UserProfile from transformed Cognito data format"""
    from src.app.models.user_profile import UserProfile

    # This is the format returned by CognitoService.get_user_by_email()
    cognito_data = {
        "username": "test-user-123",
        "userStatus": "CONFIRMED",  # Note: lowercase 's' in userStatus
        "userAttributes": {  # Note: flat dict, not array
            "email": "test@example.com",
            "given_name": "John",
            "family_name": "Doe",
            "email_verified": "true",
        },
    }

    # Should create profile with correct email from transformed format
    profile = UserProfile.from_cognito_user(cognito_data, "test-user-123")
    assert profile.user_id == "test-user-123"
    assert profile.email == "test@example.com"  # Should find email in userAttributes
    assert profile.first_name == "John"
    assert profile.last_name == "Doe"
    assert profile.email_verified is True


def test_user_profile_cognito_format_mismatch_original_bug():
    """Test that reproduces the original bug: email empty due to format mismatch"""
    from src.app.models.user_profile import UserProfile

    # This simulates what was happening before our fix:
    # CognitoService.get_user_by_email() returns userAttributes as a flat dict,
    # but UserProfile.from_cognito_user() was only looking for UserAttributes array

    cognito_data_with_wrong_format = {
        "username": "test-user-123",
        "userStatus": "CONFIRMED",
        "userAttributes": {  # Our service returns this format
            "email": "user@example.com",
            "given_name": "John",
        },
        # But the UserProfile was only looking for "UserAttributes" (capital U)
    }

    # With our fix, this should now work correctly
    profile = UserProfile.from_cognito_user(
        cognito_data_with_wrong_format, "test-user-123"
    )

    # Before our fix, email would be empty string due to format mismatch
    # After our fix, email should be correctly extracted
    assert profile.email == "user@example.com"
    assert profile.first_name == "John"
