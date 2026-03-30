"""
Test media attachment to RELEASED echoes.

This tests the special case where media can be attached to a RELEASED echo
as a first-time operation (e.g., after file upload completes).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.core.exceptions import InternalServerError
from src.app.models.echo import Echo, EchoStatus, EchoType
from src.app.services.echo_service import EchoService


@pytest.mark.asyncio
async def test_can_attach_media_to_released_echo():
    """Test that media_url can be attached to a RELEASED echo (first-time only)."""
    # Create a RELEASED echo without media
    released_echo = Echo(
        echo_id="echo-123",
        user_id="user-456",
        title="Test Echo",
        status=EchoStatus.RELEASED,
        media_url=None,  # No media attached yet
        recipient_id="recipient-789",
    )

    # Create the service
    echo_service = EchoService()

    # Mock DynamoDB table
    mock_table = AsyncMock()
    mock_table.put_item = AsyncMock()
    mock_dynamodb = AsyncMock()
    mock_dynamodb.Table = AsyncMock(return_value=mock_table)

    # Create async context manager for resource
    mock_resource_ctx = MagicMock()
    mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
    mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

    # Mock get_echo to return our released echo
    with patch.object(
        echo_service, "get_echo", new=AsyncMock(return_value=released_echo)
    ):
        with patch.object(
            echo_service.session, "resource", return_value=mock_resource_ctx
        ):
            # Should allow attaching media to RELEASED echo
            updated_echo = await echo_service.update_echo(
                echo_id="echo-123",
                user_id="user-456",
                data={"media_url": "s3://bucket/user-456/echo-123.mp4"},
            )

            # Verify media was attached
            assert updated_echo.media_url == "s3://bucket/user-456/echo-123.mp4"
            assert updated_echo.status == EchoStatus.RELEASED
            mock_table.put_item.assert_called_once()


@pytest.mark.asyncio
async def test_can_attach_media_and_echo_type_to_released_echo():
    """Test that media_url and echo_type can be updated together on RELEASED echo."""
    released_echo = Echo(
        echo_id="echo-123",
        user_id="user-456",
        title="Test Echo",
        status=EchoStatus.RELEASED,
        media_url=None,
        echo_type=EchoType.TEXT,  # Initially text
        recipient_id="recipient-789",
    )

    echo_service = EchoService()

    mock_table = AsyncMock()
    mock_table.put_item = AsyncMock()
    mock_dynamodb = AsyncMock()
    mock_dynamodb.Table = AsyncMock(return_value=mock_table)
    mock_resource_ctx = MagicMock()
    mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
    mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.object(
        echo_service, "get_echo", new=AsyncMock(return_value=released_echo)
    ):
        with patch.object(
            echo_service.session, "resource", return_value=mock_resource_ctx
        ):
            # Should allow attaching media + changing type
            updated_echo = await echo_service.update_echo(
                echo_id="echo-123",
                user_id="user-456",
                data={
                    "media_url": "s3://bucket/user-456/echo-123.mp4",
                    "echo_type": "VIDEO",
                },
            )

            assert updated_echo.media_url == "s3://bucket/user-456/echo-123.mp4"
            assert updated_echo.echo_type == EchoType.VIDEO
            mock_table.put_item.assert_called_once()


@pytest.mark.asyncio
async def test_cannot_update_other_fields_on_released_echo():
    """Test that non-media fields cannot be updated on RELEASED echo."""
    released_echo = Echo(
        echo_id="echo-123",
        user_id="user-456",
        title="Original Title",
        status=EchoStatus.RELEASED,
        media_url=None,
        recipient_id="recipient-789",
    )

    echo_service = EchoService()

    with patch.object(
        echo_service, "get_echo", new=AsyncMock(return_value=released_echo)
    ):
        # Should reject updates to title on RELEASED echo
        with pytest.raises(
            InternalServerError, match="Cannot update locked or released"
        ):
            await echo_service.update_echo(
                echo_id="echo-123",
                user_id="user-456",
                data={"title": "New Title"},
            )


@pytest.mark.asyncio
async def test_cannot_attach_media_twice():
    """Test that media cannot be replaced on a RELEASED echo that already has media."""
    released_echo = Echo(
        echo_id="echo-123",
        user_id="user-456",
        title="Test Echo",
        status=EchoStatus.RELEASED,
        media_url="s3://bucket/user-456/original.mp4",  # Already has media
        recipient_id="recipient-789",
    )

    echo_service = EchoService()

    with patch.object(
        echo_service, "get_echo", new=AsyncMock(return_value=released_echo)
    ):
        # Should reject replacing existing media
        with pytest.raises(
            InternalServerError, match="Cannot update locked or released"
        ):
            await echo_service.update_echo(
                echo_id="echo-123",
                user_id="user-456",
                data={"media_url": "s3://bucket/user-456/new.mp4"},
            )


@pytest.mark.asyncio
async def test_cannot_update_media_with_other_fields_on_released_echo():
    """Test that media + other field updates are rejected on RELEASED echo."""
    released_echo = Echo(
        echo_id="echo-123",
        user_id="user-456",
        title="Original Title",
        status=EchoStatus.RELEASED,
        media_url=None,
        recipient_id="recipient-789",
    )

    echo_service = EchoService()

    with patch.object(
        echo_service, "get_echo", new=AsyncMock(return_value=released_echo)
    ):
        # Should reject media + title update together
        with pytest.raises(
            InternalServerError, match="Cannot update locked or released"
        ):
            await echo_service.update_echo(
                echo_id="echo-123",
                user_id="user-456",
                data={
                    "media_url": "s3://bucket/user-456/echo-123.mp4",
                    "title": "New Title",
                },
            )


@pytest.mark.asyncio
async def test_draft_echo_can_be_updated_freely():
    """Test that DRAFT echoes can still be updated normally."""
    draft_echo = Echo(
        echo_id="echo-123",
        user_id="user-456",
        title="Original Title",
        status=EchoStatus.DRAFT,
        media_url=None,
    )

    echo_service = EchoService()

    mock_table = AsyncMock()
    mock_table.put_item = AsyncMock()
    mock_dynamodb = AsyncMock()
    mock_dynamodb.Table = AsyncMock(return_value=mock_table)
    mock_resource_ctx = MagicMock()
    mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
    mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.object(echo_service, "get_echo", new=AsyncMock(return_value=draft_echo)):
        with patch.object(
            echo_service.session, "resource", return_value=mock_resource_ctx
        ):
            # Should allow updating title on DRAFT echo
            updated_echo = await echo_service.update_echo(
                echo_id="echo-123",
                user_id="user-456",
                data={"title": "New Title", "content": "New content"},
            )

            assert updated_echo.title == "New Title"
            assert updated_echo.content == "New content"
            assert updated_echo.status == EchoStatus.DRAFT
            mock_table.put_item.assert_called_once()
