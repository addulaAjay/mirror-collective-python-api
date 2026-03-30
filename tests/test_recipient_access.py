"""
Test for recipient access to echoes (fix for "Echo not found" when recipients try to view echoes).

This test ensures that:
1. Echo owners can view their own echoes (original behavior)
2. Recipients can view echoes sent to them (NEW - fixed bug)
3. Other users cannot view echoes they don't own or receive
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_recipient_can_view_echo_sent_to_them():
    """Recipients should be able to view echoes sent to them via recipient_user_id linking."""
    from src.app.models.echo import Echo, EchoStatus, Recipient
    from src.app.services.echo_service import EchoService

    service = EchoService()

    # User A owns the echo
    owner_user_id = "user-a-owner"
    # User B is the recipient
    recipient_user_id = "user-b-recipient"
    # User C is neither owner nor recipient
    other_user_id = "user-c-other"

    # Create an echo owned by user A, sent to recipient R
    echo = Echo(
        echo_id="echo-123",
        user_id=owner_user_id,
        title="Test Echo",
        category="Memory",
        status=EchoStatus.RELEASED,
        recipient_id="recipient-r",
    )

    # Create recipient R that belongs to owner but is linked to user B's account
    recipient = Recipient(
        recipient_id="recipient-r",
        user_id=owner_user_id,  # Belongs to owner
        name="User B",
        email="userb@example.com",
        recipient_user_id=recipient_user_id,  # Linked to user B's account
    )

    # Mock DynamoDB responses
    mock_echoes_table = AsyncMock()
    mock_echoes_table.get_item = AsyncMock(
        return_value={"Item": echo.to_dynamodb_item()}
    )

    mock_recipients_table = AsyncMock()
    mock_recipients_table.get_item = AsyncMock(
        return_value={"Item": recipient.to_dynamodb_item()}
    )

    mock_dynamodb = AsyncMock()

    def get_table(table_name):
        if "recipients" in table_name.lower():
            return mock_recipients_table
        elif "echoes" in table_name.lower():
            return mock_echoes_table
        return AsyncMock()

    mock_dynamodb.Table = AsyncMock(side_effect=get_table)

    mock_resource_ctx = MagicMock()
    mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
    mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

    # Mock S3 presigned URL generation
    mock_client_ctx = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.generate_presigned_url = MagicMock(
        return_value="https://signed-url.example.com/media.mp4"
    )
    mock_client_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_client_ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(service.session, "resource", return_value=mock_resource_ctx),
        patch.object(service.session, "client", return_value=mock_client_ctx),
    ):
        # Test 1: Owner can view their own echo
        echo_for_owner = await service.get_echo("echo-123", owner_user_id)
        assert echo_for_owner is not None
        assert echo_for_owner.echo_id == "echo-123"
        assert echo_for_owner.user_id == owner_user_id

        # Test 2: Recipient can view echo sent to them (THIS WAS BROKEN BEFORE FIX)
        echo_for_recipient = await service.get_echo("echo-123", recipient_user_id)
        assert echo_for_recipient is not None
        assert echo_for_recipient.echo_id == "echo-123"
        assert echo_for_recipient.user_id == owner_user_id  # Still owned by owner
        # Recipient details should be enriched
        assert echo_for_recipient.recipient is not None
        assert echo_for_recipient.recipient["name"] == "User B"

        # Test 3: Other users cannot view echo (not owner, not recipient)
        echo_for_other = await service.get_echo("echo-123", other_user_id)
        assert echo_for_other is None  # Should return None for unauthorized access


@pytest.mark.asyncio
async def test_recipient_without_user_account_cannot_view_echo():
    """Recipients without linked user accounts (recipient_user_id=None) cannot view echoes."""
    from src.app.models.echo import Echo, EchoStatus, Recipient
    from src.app.services.echo_service import EchoService

    service = EchoService()

    owner_user_id = "user-a-owner"
    some_user_id = "user-random"

    echo = Echo(
        echo_id="echo-456",
        user_id=owner_user_id,
        title="Test Echo",
        category="Memory",
        status=EchoStatus.RELEASED,
        recipient_id="recipient-unlinked",
    )

    # Recipient has NO linked user account (recipient_user_id=None)
    recipient = Recipient(
        recipient_id="recipient-unlinked",
        user_id=owner_user_id,
        name="Unlinked User",
        email="unlinked@example.com",
        recipient_user_id=None,  # Not linked to any user account
    )

    mock_echoes_table = AsyncMock()
    mock_echoes_table.get_item = AsyncMock(
        return_value={"Item": echo.to_dynamodb_item()}
    )

    mock_recipients_table = AsyncMock()
    mock_recipients_table.get_item = AsyncMock(
        return_value={"Item": recipient.to_dynamodb_item()}
    )

    mock_dynamodb = AsyncMock()

    def get_table(table_name):
        if "recipients" in table_name.lower():
            return mock_recipients_table
        elif "echoes" in table_name.lower():
            return mock_echoes_table
        return AsyncMock()

    mock_dynamodb.Table = AsyncMock(side_effect=get_table)

    mock_resource_ctx = MagicMock()
    mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
    mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_client_ctx = MagicMock()
    mock_s3_client = AsyncMock()
    mock_s3_client.generate_presigned_url = MagicMock(
        return_value="https://signed-url.example.com/media.mp4"
    )
    mock_client_ctx.__aenter__ = AsyncMock(return_value=mock_s3_client)
    mock_client_ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(service.session, "resource", return_value=mock_resource_ctx),
        patch.object(service.session, "client", return_value=mock_client_ctx),
    ):
        # Random user trying to access echo sent to unlinked recipient
        echo_result = await service.get_echo("echo-456", some_user_id)
        assert echo_result is None  # Should be denied (not owner, recipient not linked)

        # Owner can still access
        echo_for_owner = await service.get_echo("echo-456", owner_user_id)
        assert echo_for_owner is not None
