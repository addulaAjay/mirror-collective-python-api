"""
Test for storage quota service Decimal/float type conversion fix.

This test verifies that the storage quota service properly handles Decimal values
from DynamoDB without causing type errors when performing arithmetic operations.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_check_quota_handles_decimal_values():
    """Storage quota service should handle Decimal values from DynamoDB without type errors."""
    from src.app.models.user_profile import UserProfile
    from src.app.services.storage_quota_service import StorageQuotaService

    # Mock DynamoDB service
    mock_dynamodb = AsyncMock()

    # Create user profile with Decimal values (as DynamoDB returns them)
    # Note: UserProfile is typed with float, but DynamoDB returns Decimal via boto3
    # We intentionally use Decimal here to test the conversion logic
    user_profile = UserProfile(
        user_id="test-user-123",
        email="test@example.com",
        echo_vault_quota_gb=Decimal("50.0"),  # type: ignore[arg-type]
        echo_vault_used_gb=Decimal("25.5"),  # type: ignore[arg-type]
        subscription_tier="core",
    )

    mock_dynamodb.get_user_profile = AsyncMock(return_value=user_profile)
    mock_dynamodb.update_user_profile = AsyncMock(return_value=True)

    service = StorageQuotaService(mock_dynamodb)

    # Mock S3 client and calculate_user_storage_usage to return float
    with patch.object(
        service, "calculate_user_storage_usage", new=AsyncMock(return_value=25.5)
    ):
        # This should not raise "unsupported operand type(s) for /: 'float' and 'decimal.Decimal'"
        result = await service.check_quota_exceeded("test-user-123")

    # Verify results are correct
    assert result["exceeded"] is False
    assert result["usage_gb"] == 25.5
    assert result["quota_gb"] == 50.0
    assert result["percent_used"] == 51.0
    assert result["approaching_limit"] is False


@pytest.mark.asyncio
async def test_can_upload_handles_decimal_values():
    """can_upload should handle Decimal values without type errors."""
    from src.app.services.storage_quota_service import StorageQuotaService

    mock_dynamodb = AsyncMock()
    service = StorageQuotaService(mock_dynamodb)

    # Mock check_quota_exceeded to return status with Decimal values
    quota_status = {
        "exceeded": False,
        "usage_gb": Decimal("30.5"),  # Decimal value
        "quota_gb": 50.0,
        "percent_used": 61.0,
        "approaching_limit": False,
    }

    with patch.object(
        service, "check_quota_exceeded", new=AsyncMock(return_value=quota_status)
    ):
        # Upload a 5GB file (5 * 1024^3 bytes)
        file_size_bytes = 5 * (1024**3)

        # This should not raise type errors when adding Decimal + float
        result = await service.can_upload("test-user-123", file_size_bytes)

    # Verify upload is allowed (30.5 + 5 = 35.5 < 50)
    assert result["can_upload"] is True
    assert "quota_status" in result


@pytest.mark.asyncio
async def test_can_upload_rejects_when_quota_exceeded_with_decimals():
    """can_upload should correctly reject uploads that would exceed quota with Decimal values."""
    from src.app.services.storage_quota_service import StorageQuotaService

    mock_dynamodb = AsyncMock()
    service = StorageQuotaService(mock_dynamodb)

    # User at 48GB of 50GB quota
    quota_status = {
        "exceeded": False,
        "usage_gb": Decimal("48.0"),  # Decimal value
        "quota_gb": 50.0,
        "percent_used": 96.0,
        "approaching_limit": True,
    }

    with patch.object(
        service, "check_quota_exceeded", new=AsyncMock(return_value=quota_status)
    ):
        # Try to upload a 5GB file (would exceed quota)
        file_size_bytes = 5 * (1024**3)

        result = await service.can_upload("test-user-123", file_size_bytes)

    # Verify upload is rejected
    assert result["can_upload"] is False
    assert result["reason"] == "quota_exceeded"
    assert "quota" in result["message"].lower()


@pytest.mark.asyncio
async def test_check_quota_with_zero_quota():
    """Should handle zero quota gracefully without division by zero."""
    from src.app.models.user_profile import UserProfile
    from src.app.services.storage_quota_service import StorageQuotaService

    mock_dynamodb = AsyncMock()

    # User with no quota (no subscription)
    # Note: Testing with Decimal to simulate DynamoDB behavior
    user_profile = UserProfile(
        user_id="test-user-456",
        email="test2@example.com",
        echo_vault_quota_gb=Decimal("0"),  # type: ignore[arg-type]
        echo_vault_used_gb=Decimal("0"),  # type: ignore[arg-type]
        subscription_tier="none",
    )

    mock_dynamodb.get_user_profile = AsyncMock(return_value=user_profile)
    mock_dynamodb.update_user_profile = AsyncMock(return_value=True)

    service = StorageQuotaService(mock_dynamodb)

    with patch.object(
        service, "calculate_user_storage_usage", new=AsyncMock(return_value=0.0)
    ):
        result = await service.check_quota_exceeded("test-user-456")

    # Should not crash with division by zero
    assert result["quota_gb"] == 0.0
    assert result["percent_used"] == 0  # Should handle zero division gracefully
