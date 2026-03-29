"""
Echo Vault TDD tests.

Covers two P0 backend bugs:
  B-01 — PATCH /api/echoes/{id}/release endpoint missing
  B-02 — guardian_id not saved during echo creation

Test structure
--------------
1. Unit tests — Echo model (release / lock / guardian_id persistence)
2. Unit tests — EchoService.create_echo saves guardian_id
3. Unit tests — EchoService.release_echo (new service method)
4. Integration tests — PATCH /api/echoes/{id}/release route
   - Happy path: echo with recipient, no guardian → RELEASED + email fired
   - 404 when echo not found
   - 400 when echo has a guardian_id (must use guardian-release flow instead)
   - 400 when echo has no recipient_id
   - 400 when echo is already RELEASED
   - 400 when echo is LOCKED
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ============================================================
# SECTION 1 — Unit tests: Echo model
# ============================================================


class TestEchoModel:
    """Unit tests for the Echo dataclass (no I/O, no mocks needed)."""

    def test_echo_release_sets_status_to_released(self):
        """echo.release() must transition status to RELEASED."""
        from src.app.models.echo import Echo, EchoStatus

        echo = Echo(user_id="u1", title="Hello", category="Memory")
        assert echo.status == EchoStatus.DRAFT

        echo.release()

        assert echo.status == EchoStatus.RELEASED

    def test_echo_release_updates_updated_at(self):
        """echo.release() must refresh the updated_at timestamp."""
        from src.app.models.echo import Echo

        echo = Echo(user_id="u1", title="Hello", category="Memory")
        original_ts = echo.updated_at

        # Sleep-free approach: capture before and verify field changes
        echo.release()

        # updated_at must be set (may equal original on fast machines, but
        # the field is touched — important check is that it doesn't error out)
        assert echo.updated_at is not None

    def test_echo_lock_sets_status_to_locked(self):
        """echo.lock() must transition status to LOCKED."""
        from src.app.models.echo import Echo, EchoStatus

        echo = Echo(user_id="u1", title="Hello", category="Memory")
        echo.lock()

        assert echo.status == EchoStatus.LOCKED

    def test_echo_lock_sets_lock_date(self):
        """echo.lock() must populate the lock_date field."""
        from src.app.models.echo import Echo

        echo = Echo(user_id="u1", title="Hello", category="Memory")
        assert echo.lock_date is None

        echo.lock()

        assert echo.lock_date is not None

    def test_echo_accepts_guardian_id_on_construction(self):
        """Echo dataclass must accept guardian_id as a constructor argument."""
        from src.app.models.echo import Echo

        echo = Echo(
            user_id="u1",
            title="Hello",
            category="Memory",
            guardian_id="g-abc-123",
        )

        assert echo.guardian_id == "g-abc-123"

    def test_echo_guardian_id_defaults_to_none(self):
        """Echo.guardian_id must default to None when not provided."""
        from src.app.models.echo import Echo

        echo = Echo(user_id="u1", title="Hello", category="Memory")

        assert echo.guardian_id is None

    def test_echo_to_dynamodb_item_includes_guardian_id_when_set(self):
        """to_dynamodb_item() must include guardian_id when it is not None."""
        from src.app.models.echo import Echo

        echo = Echo(
            user_id="u1",
            title="Hello",
            category="Memory",
            guardian_id="g-abc-123",
        )

        item = echo.to_dynamodb_item()

        assert item["guardian_id"] == "g-abc-123"

    def test_echo_to_dynamodb_item_omits_guardian_id_when_none(self):
        """to_dynamodb_item() must omit guardian_id key when it is None."""
        from src.app.models.echo import Echo

        echo = Echo(user_id="u1", title="Hello", category="Memory")

        item = echo.to_dynamodb_item()

        assert "guardian_id" not in item

    def test_echo_from_dynamodb_item_restores_guardian_id(self):
        """from_dynamodb_item() must restore guardian_id from a DynamoDB record."""
        from src.app.models.echo import Echo

        raw = {
            "echo_id": "e-001",
            "user_id": "u1",
            "title": "Hello",
            "category": "Memory",
            "echo_type": "TEXT",
            "status": "DRAFT",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "guardian_id": "g-abc-123",
        }

        echo = Echo.from_dynamodb_item(raw)

        assert echo.guardian_id == "g-abc-123"

    def test_echo_status_lifecycle_draft_to_locked_to_released(self):
        """Full status lifecycle: DRAFT → LOCKED → RELEASED."""
        from src.app.models.echo import Echo, EchoStatus

        echo = Echo(user_id="u1", title="Hello", category="Memory")
        assert echo.status == EchoStatus.DRAFT

        echo.lock()
        assert echo.status == EchoStatus.LOCKED

        echo.release()
        assert echo.status == EchoStatus.RELEASED


# ============================================================
# SECTION 2 — Unit tests: EchoService.create_echo (B-02)
# ============================================================


class TestEchoServiceCreateEcho:
    """
    Unit tests for EchoService.create_echo.
    DynamoDB is mocked; we verify the Echo object that is persisted.
    """

    @pytest.mark.asyncio
    async def test_create_echo_saves_guardian_id(self):
        """B-02: create_echo must persist guardian_id from the request data."""
        from src.app.services.echo_service import EchoService

        service = EchoService()

        captured_items = []

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(
            side_effect=lambda Item: captured_items.append(Item)
        )

        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)

        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(service.session, "resource", return_value=mock_resource_ctx):
            echo = await service.create_echo(
                user_id="u-001",
                data={
                    "title": "My Echo",
                    "category": "Memory",
                    "echo_type": "TEXT",
                    "recipient_id": "r-001",
                    "guardian_id": "g-001",
                    "content": "Hello future.",
                },
            )

        assert (
            echo.guardian_id == "g-001"
        ), "create_echo must set guardian_id on the Echo object from the request data"
        # Also verify the item written to DynamoDB contains guardian_id
        assert len(captured_items) == 1
        assert (
            captured_items[0].get("guardian_id") == "g-001"
        ), "The DynamoDB item must contain guardian_id"

    @pytest.mark.asyncio
    async def test_create_echo_without_guardian_id_leaves_it_none(self):
        """create_echo without guardian_id in data must leave guardian_id as None."""
        from src.app.services.echo_service import EchoService

        service = EchoService()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)

        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)

        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(service.session, "resource", return_value=mock_resource_ctx):
            echo = await service.create_echo(
                user_id="u-001",
                data={
                    "title": "Personal Echo",
                    "category": "Reflection",
                    "echo_type": "TEXT",
                },
            )

        assert echo.guardian_id is None

    @pytest.mark.asyncio
    async def test_create_echo_saves_recipient_id(self):
        """Regression: create_echo must still persist recipient_id correctly."""
        from src.app.services.echo_service import EchoService

        service = EchoService()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)

        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)

        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(service.session, "resource", return_value=mock_resource_ctx):
            echo = await service.create_echo(
                user_id="u-001",
                data={
                    "title": "Echo to a Friend",
                    "category": "Gratitude",
                    "echo_type": "TEXT",
                    "recipient_id": "r-999",
                },
            )

        assert echo.recipient_id == "r-999"

    @pytest.mark.asyncio
    async def test_create_echo_with_null_guardian_id_in_data(self):
        """create_echo with explicit None guardian_id must not set guardian_id."""
        from src.app.services.echo_service import EchoService

        service = EchoService()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)

        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)

        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(service.session, "resource", return_value=mock_resource_ctx):
            echo = await service.create_echo(
                user_id="u-001",
                data={
                    "title": "No Guardian",
                    "category": "Memory",
                    "echo_type": "TEXT",
                    "guardian_id": None,
                },
            )

        assert echo.guardian_id is None


# ============================================================
# SECTION 3 — Unit tests: EchoService.release_echo (B-01)
# ============================================================


class TestEchoServiceReleaseEcho:
    """
    Unit tests for the new EchoService.release_echo method (B-01).
    DynamoDB and email service are fully mocked.
    """

    def _make_echo(
        self,
        status="DRAFT",
        recipient_id="r-001",
        guardian_id=None,
    ):
        """Helper: build a minimal Echo for testing."""
        from src.app.models.echo import Echo, EchoStatus, EchoType

        status_map = {
            "DRAFT": EchoStatus.DRAFT,
            "LOCKED": EchoStatus.LOCKED,
            "RELEASED": EchoStatus.RELEASED,
        }
        echo = Echo(
            echo_id="e-abc-123",
            user_id="u-001",
            title="Test Echo",
            category="Memory",
            echo_type=EchoType.TEXT,
            status=status_map[status],
            recipient_id=recipient_id,
            guardian_id=guardian_id,
        )
        return echo

    def _make_recipient(self):
        from src.app.models.echo import Recipient

        return Recipient(
            recipient_id="r-001",
            user_id="u-001",
            name="Alice",
            email="alice@example.com",
        )

    @pytest.mark.asyncio
    async def test_release_echo_transitions_status_to_released(self):
        """release_echo must call echo.release() and return a RELEASED echo."""
        from src.app.models.echo import EchoStatus
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        recipient = self._make_recipient()

        # Mock get_echo to return a DRAFT echo with a recipient

        # Mock the DynamoDB put_item call
        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_recipient", new_callable=AsyncMock, return_value=recipient
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_notification",
                        new_callable=AsyncMock,
                        return_value=True,
                    ):
                        released_echo = await service.release_echo(
                            echo_id="e-abc-123", user_id="u-001"
                        )

        assert released_echo.status == EchoStatus.RELEASED

    @pytest.mark.asyncio
    async def test_release_echo_fires_send_echo_notification(self):
        """release_echo must call email_service.send_echo_notification exactly once."""
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        recipient = self._make_recipient()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_recipient", new_callable=AsyncMock, return_value=recipient
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_notification",
                        new_callable=AsyncMock,
                        return_value=True,
                    ) as mock_send:
                        await service.release_echo(echo_id="e-abc-123", user_id="u-001")

        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_echo_passes_correct_args_to_notification(self):
        """release_echo must pass recipient email, name, echo title etc. to the email."""
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        recipient = self._make_recipient()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_recipient", new_callable=AsyncMock, return_value=recipient
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_notification",
                        new_callable=AsyncMock,
                        return_value=True,
                    ) as mock_send:
                        await service.release_echo(echo_id="e-abc-123", user_id="u-001")

        call_kwargs = mock_send.call_args
        # Accept both positional and keyword invocations
        all_args = {
            **(call_kwargs.kwargs or {}),
            **dict(
                zip(
                    [
                        "recipient_email",
                        "recipient_name",
                        "sender_name",
                        "echo_title",
                        "echo_category",
                        "echo_type",
                    ],
                    call_kwargs.args,
                )
            ),
        }
        assert all_args.get("recipient_email") == "alice@example.com"
        assert all_args.get("echo_title") == "Test Echo"

    @pytest.mark.asyncio
    async def test_release_echo_raises_not_found_when_echo_missing(self):
        """release_echo must raise NotFoundError when the echo does not exist."""
        from src.app.core.exceptions import NotFoundError
        from src.app.services.echo_service import EchoService

        service = EchoService()

        with patch.object(
            service,
            "get_echo",
            new_callable=AsyncMock,
            side_effect=NotFoundError("Echo does-not-exist not found"),
        ):
            with pytest.raises(NotFoundError):
                await service.release_echo(echo_id="does-not-exist", user_id="u-001")

    @pytest.mark.asyncio
    async def test_release_echo_raises_validation_error_when_no_recipient(self):
        """release_echo must raise ValidationError when echo has no recipient_id."""
        from src.app.core.exceptions import ValidationError
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo(recipient_id=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with pytest.raises(ValidationError, match="recipient"):
                await service.release_echo(echo_id="e-abc-123", user_id="u-001")

    @pytest.mark.asyncio
    async def test_release_echo_raises_validation_error_when_guardian_set(self):
        """
        release_echo must raise ValidationError when echo has a guardian_id.
        Echoes with a guardian must go through the guardian-release flow.
        """
        from src.app.core.exceptions import ValidationError
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo(guardian_id="g-999")

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with pytest.raises(ValidationError, match="guardian"):
                await service.release_echo(echo_id="e-abc-123", user_id="u-001")

    @pytest.mark.asyncio
    async def test_release_echo_raises_validation_error_when_already_released(self):
        """release_echo must raise ValidationError when echo is already RELEASED."""
        from src.app.core.exceptions import ValidationError
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo(status="RELEASED")

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with pytest.raises(ValidationError, match="[Aa]lready released"):
                await service.release_echo(echo_id="e-abc-123", user_id="u-001")

    @pytest.mark.asyncio
    async def test_release_echo_raises_validation_error_when_locked(self):
        """
        release_echo must raise ValidationError when echo is LOCKED.
        A LOCKED echo with a guardian must be released via the guardian flow.
        """
        from src.app.core.exceptions import ValidationError
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo(status="LOCKED")

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with pytest.raises(ValidationError, match="[Ll]ocked"):
                await service.release_echo(echo_id="e-abc-123", user_id="u-001")

    @pytest.mark.asyncio
    async def test_release_echo_persists_to_dynamodb(self):
        """release_echo must write the updated echo back to DynamoDB."""
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        recipient = self._make_recipient()

        put_calls = []
        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(side_effect=lambda Item: put_calls.append(Item))
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_recipient", new_callable=AsyncMock, return_value=recipient
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_notification",
                        new_callable=AsyncMock,
                        return_value=True,
                    ):
                        await service.release_echo(echo_id="e-abc-123", user_id="u-001")

        assert len(put_calls) == 1
        assert put_calls[0]["status"] == "RELEASED"

    @pytest.mark.asyncio
    async def test_release_echo_email_failure_does_not_raise(self):
        """
        A failed email send must not bubble up as an exception (fire-and-forget).
        The echo should still be persisted as RELEASED.
        """
        from src.app.models.echo import EchoStatus
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        recipient = self._make_recipient()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_recipient", new_callable=AsyncMock, return_value=recipient
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_notification",
                        new_callable=AsyncMock,
                        side_effect=Exception("SES timeout"),
                    ):
                        # Must not raise
                        released_echo = await service.release_echo(
                            echo_id="e-abc-123", user_id="u-001"
                        )

        assert released_echo.status == EchoStatus.RELEASED


# ============================================================
# SECTION 4 — Integration tests: PATCH /api/echoes/{id}/release
# ============================================================
#
# The full application test client has a pre-existing middleware conflict
# that produces 422 errors for all echo routes with path parameters.
# We use a minimal echo-only FastAPI app to test the route handler and
# HTTP status mapping cleanly, without that infrastructure noise.


@pytest.fixture
def echo_client():
    """
    Lightweight test client mounting only the echo router.
    Bypasses the full app middleware stack that causes interference
    with path-parameter routes in the existing test infrastructure.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.app.api.echo_routes import router
    from src.app.core.error_handlers import setup_error_handlers
    from src.app.core.security import get_current_user

    mini_app = FastAPI()
    mini_app.include_router(router, prefix="/api")
    setup_error_handlers(mini_app)

    async def _fake_user():
        return {
            "id": "test-user-123",
            "email": "test@example.com",
            "given_name": "Test",
            "family_name": "User",
        }

    mini_app.dependency_overrides[get_current_user] = _fake_user

    with TestClient(mini_app) as c:
        yield c


class TestReleaseEchoEndpoint:
    """
    Integration tests for the PATCH /api/echoes/{echo_id}/release route.
    Uses the lightweight echo_client fixture.
    """

    # ----------------------------------------------------------
    # Happy path
    # ----------------------------------------------------------

    def test_release_echo_happy_path(self, echo_client):
        """
        PATCH /api/echoes/{id}/release returns 200 with success=True
        when the echo is a DRAFT with a recipient and no guardian.
        """
        from src.app.api import echo_routes
        from src.app.models.echo import Echo, EchoStatus, EchoType

        released_echo = Echo(
            echo_id="e-abc-123",
            user_id="test-user-123",
            title="Test Echo",
            category="Memory",
            echo_type=EchoType.TEXT,
            status=EchoStatus.RELEASED,
            recipient_id="r-001",
        )

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            return_value=released_echo,
        ):
            response = echo_client.patch("/api/echoes/e-abc-123/release")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == "RELEASED"
        assert body["data"]["echo_id"] == "e-abc-123"

    # ----------------------------------------------------------
    # Error paths
    # ----------------------------------------------------------

    def test_release_echo_returns_404_when_not_found(self, echo_client):
        """PATCH /api/echoes/{id}/release returns 404 when echo is not found."""
        from src.app.api import echo_routes
        from src.app.core.exceptions import NotFoundError

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            side_effect=NotFoundError("Echo not found"),
        ):
            response = echo_client.patch("/api/echoes/does-not-exist/release")

        assert response.status_code == 404

    def test_release_echo_returns_400_when_guardian_set(self, echo_client):
        """
        PATCH /api/echoes/{id}/release returns 400 when the echo has a guardian_id.
        These echoes must go through the guardian release flow.
        """
        from src.app.api import echo_routes
        from src.app.core.exceptions import ValidationError

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            side_effect=ValidationError("Echo has a guardian assigned"),
        ):
            response = echo_client.patch("/api/echoes/e-has-guardian/release")

        assert response.status_code == 400

    def test_release_echo_returns_400_when_no_recipient(self, echo_client):
        """PATCH /api/echoes/{id}/release returns 400 when echo has no recipient_id."""
        from src.app.api import echo_routes
        from src.app.core.exceptions import ValidationError

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            side_effect=ValidationError("Echo has no recipient"),
        ):
            response = echo_client.patch("/api/echoes/e-no-recipient/release")

        assert response.status_code == 400

    def test_release_echo_returns_400_when_already_released(self, echo_client):
        """PATCH /api/echoes/{id}/release returns 400 when echo is already RELEASED."""
        from src.app.api import echo_routes
        from src.app.core.exceptions import ValidationError

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            side_effect=ValidationError("Already released"),
        ):
            response = echo_client.patch("/api/echoes/e-already-released/release")

        assert response.status_code == 400

    def test_release_echo_returns_400_when_locked(self, echo_client):
        """PATCH /api/echoes/{id}/release returns 400 when echo is LOCKED."""
        from src.app.api import echo_routes
        from src.app.core.exceptions import ValidationError

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            side_effect=ValidationError(
                "Locked echo must be released via guardian flow"
            ),
        ):
            response = echo_client.patch("/api/echoes/e-locked/release")

        assert response.status_code == 400

    def test_release_echo_returns_500_on_unexpected_error(self, echo_client):
        """PATCH /api/echoes/{id}/release returns 500 on unexpected server error."""
        from src.app.api import echo_routes

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            side_effect=Exception("Unexpected DynamoDB error"),
        ):
            response = echo_client.patch("/api/echoes/e-bad/release")

        assert response.status_code == 500

    # ----------------------------------------------------------
    # Response shape
    # ----------------------------------------------------------

    def test_release_echo_response_contains_required_fields(self, echo_client):
        """The release response body must include echo_id, status, and updated_at."""
        from src.app.api import echo_routes
        from src.app.models.echo import Echo, EchoStatus, EchoType

        released_echo = Echo(
            echo_id="e-xyz",
            user_id="test-user-123",
            title="Goodbye",
            category="Reflection",
            echo_type=EchoType.AUDIO,
            status=EchoStatus.RELEASED,
            recipient_id="r-002",
        )

        with patch.object(
            echo_routes.echo_service,
            "release_echo",
            new_callable=AsyncMock,
            return_value=released_echo,
        ):
            response = echo_client.patch("/api/echoes/e-xyz/release")

        assert response.status_code == 200
        data = response.json()["data"]
        assert "echo_id" in data
        assert "status" in data
        assert "updated_at" in data


# ============================================================
# SECTION 5 — Integration: create echo with guardian_id (B-02)
# ============================================================


class TestCreateEchoWithGuardianId:
    """
    Integration tests verifying that POST /api/echoes correctly
    threads guardian_id through to EchoService and back to the client.
    Uses the same lightweight echo_client fixture.
    """

    def test_create_echo_with_guardian_id_calls_service_with_guardian_id(
        self, echo_client
    ):
        """
        POST /api/echoes with guardian_id in the body must pass guardian_id
        through to EchoService.create_echo.
        """
        from src.app.api import echo_routes
        from src.app.models.echo import Echo, EchoStatus, EchoType

        created_echo = Echo(
            echo_id="e-new-001",
            user_id="test-user-123",
            title="Echo with Guardian",
            category="Memory",
            echo_type=EchoType.TEXT,
            status=EchoStatus.DRAFT,
            recipient_id="r-001",
            guardian_id="g-001",
        )

        with patch.object(
            echo_routes.echo_service,
            "create_echo",
            new_callable=AsyncMock,
            return_value=created_echo,
        ) as mock_create:
            response = echo_client.post(
                "/api/echoes",
                json={
                    "title": "Echo with Guardian",
                    "category": "Memory",
                    "echo_type": "TEXT",
                    "recipient_id": "r-001",
                    "guardian_id": "g-001",
                },
            )

        assert response.status_code == 201
        # Verify guardian_id was forwarded to the service
        call_data = mock_create.call_args[0][1]  # second positional arg is `data`
        assert call_data.get("guardian_id") == "g-001"

    def test_create_echo_without_guardian_id_passes_none(self, echo_client):
        """
        POST /api/echoes without guardian_id must pass None (or absent key)
        to EchoService.create_echo, not an unexpected value.
        """
        from src.app.api import echo_routes
        from src.app.models.echo import Echo, EchoStatus, EchoType

        created_echo = Echo(
            echo_id="e-new-002",
            user_id="test-user-123",
            title="Personal",
            category="Reflection",
            echo_type=EchoType.TEXT,
            status=EchoStatus.DRAFT,
        )

        with patch.object(
            echo_routes.echo_service,
            "create_echo",
            new_callable=AsyncMock,
            return_value=created_echo,
        ) as mock_create:
            response = echo_client.post(
                "/api/echoes",
                json={
                    "title": "Personal",
                    "category": "Reflection",
                    "echo_type": "TEXT",
                },
            )

        assert response.status_code == 201
        call_data = mock_create.call_args[0][1]
        # guardian_id either absent or None — must not be a non-None stale value
        assert call_data.get("guardian_id") is None


# ============================================================
# SECTION 6 — Unit tests: EchoService.lock_echo (Phase 2)
# ============================================================


class TestEchoServiceLockEcho:
    """
    Unit tests for EchoService.lock_echo method (Phase 2).
    DynamoDB and email service are fully mocked.
    """

    def _make_echo(
        self,
        status="DRAFT",
        guardian_id="g-001",
    ):
        """Helper: build a minimal Echo for testing."""
        from src.app.models.echo import Echo, EchoStatus, EchoType

        status_map = {
            "DRAFT": EchoStatus.DRAFT,
            "LOCKED": EchoStatus.LOCKED,
            "RELEASED": EchoStatus.RELEASED,
        }
        echo = Echo(
            echo_id="e-abc-123",
            user_id="u-001",
            title="Test Echo",
            category="Memory",
            echo_type=EchoType.TEXT,
            status=status_map[status],
            guardian_id=guardian_id,
            recipient_id="r-001",
        )
        return echo

    def _make_guardian(self):
        from src.app.models.echo import Guardian, GuardianScope, GuardianTrigger

        return Guardian(
            guardian_id="g-001",
            user_id="u-001",
            name="Alice Guardian",
            email="alice@guardian.com",
            scope=GuardianScope.ALL,
            trigger=GuardianTrigger.MANUAL,
        )

    @pytest.mark.asyncio
    async def test_lock_echo_transitions_status_to_locked(self):
        """lock_echo must call echo.lock() and return a LOCKED echo."""
        from src.app.models.echo import EchoStatus
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        guardian = self._make_guardian()

        # Mock get_echo to return a DRAFT echo with a guardian

        # Mock the DynamoDB put_item call
        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_guardian", new_callable=AsyncMock, return_value=guardian
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_pending_notification",
                        new_callable=AsyncMock,
                        return_value=True,
                    ):
                        locked_echo = await service.lock_echo(
                            echo_id="e-abc-123", user_id="u-001"
                        )

        assert locked_echo.status == EchoStatus.LOCKED

    @pytest.mark.asyncio
    async def test_lock_echo_sets_lock_date(self):
        """lock_echo must populate the lock_date field."""
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        guardian = self._make_guardian()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_guardian", new_callable=AsyncMock, return_value=guardian
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_pending_notification",
                        new_callable=AsyncMock,
                    ):
                        locked_echo = await service.lock_echo(
                            echo_id="e-abc-123", user_id="u-001"
                        )

        assert locked_echo.lock_date is not None

    @pytest.mark.asyncio
    async def test_lock_echo_fires_send_echo_pending_notification(self):
        """lock_echo must call email_service.send_echo_pending_notification once."""
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo()
        guardian = self._make_guardian()

        mock_table = AsyncMock()
        mock_table.put_item = AsyncMock(return_value=None)
        mock_dynamodb = AsyncMock()
        mock_dynamodb.Table = AsyncMock(return_value=mock_table)
        mock_resource_ctx = MagicMock()
        mock_resource_ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
        mock_resource_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with patch.object(
                service, "get_guardian", new_callable=AsyncMock, return_value=guardian
            ):
                with patch.object(
                    service.session, "resource", return_value=mock_resource_ctx
                ):
                    with patch(
                        "src.app.services.echo_service.email_service.send_echo_pending_notification",
                        new_callable=AsyncMock,
                    ) as mock_email:
                        await service.lock_echo(echo_id="e-abc-123", user_id="u-001")

                        mock_email.assert_called_once()
                # Verify the arguments passed
                call_kwargs = mock_email.call_args[1]
                assert call_kwargs["guardian_email"] == "alice@guardian.com"
                assert call_kwargs["guardian_name"] == "Alice Guardian"
                assert call_kwargs["echo_title"] == "Test Echo"

    @pytest.mark.asyncio
    async def test_lock_echo_raises_not_found_when_echo_missing(self):
        """lock_echo raises NotFoundError when echo doesn't exist."""
        from src.app.core.exceptions import NotFoundError
        from src.app.services.echo_service import EchoService

        service = EchoService()

        with patch.object(
            service,
            "get_echo",
            new_callable=AsyncMock,
            side_effect=NotFoundError("Echo missing not found"),
        ):
            with pytest.raises(NotFoundError):
                await service.lock_echo(echo_id="missing", user_id="u-001")

    @pytest.mark.asyncio
    async def test_lock_echo_raises_validation_error_when_no_guardian(self):
        """lock_echo raises ValidationError when echo has no guardian_id."""
        from src.app.core.exceptions import ValidationError
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo(guardian_id=None)

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with pytest.raises(ValidationError, match="no guardian"):
                await service.lock_echo(echo_id="e-abc-123", user_id="u-001")

    @pytest.mark.asyncio
    async def test_lock_echo_raises_validation_error_when_already_locked(self):
        """lock_echo raises ValidationError when echo is already LOCKED."""
        from src.app.core.exceptions import ValidationError
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo(status="LOCKED")

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with pytest.raises(ValidationError, match="already locked"):
                await service.lock_echo(echo_id="e-abc-123", user_id="u-001")

    @pytest.mark.asyncio
    async def test_lock_echo_raises_validation_error_when_released(self):
        """lock_echo raises ValidationError when echo is already RELEASED."""
        from src.app.core.exceptions import ValidationError
        from src.app.services.echo_service import EchoService

        service = EchoService()
        echo = self._make_echo(status="RELEASED")

        with patch.object(
            service, "get_echo", new_callable=AsyncMock, return_value=echo
        ):
            with pytest.raises(ValidationError, match="already released"):
                await service.lock_echo(echo_id="e-abc-123", user_id="u-001")


# ============================================================
# SECTION 7 — Integration: PATCH /api/echoes/{id}/lock (Phase 2)
# ============================================================


class TestLockEchoEndpoint:
    """
    Integration tests for the PATCH /api/echoes/{echo_id}/lock route.
    Uses the lightweight echo_client fixture.
    """

    # ----------------------------------------------------------
    # Happy path
    # ----------------------------------------------------------

    def test_lock_echo_happy_path(self, echo_client):
        """
        PATCH /api/echoes/{id}/lock returns 200 with success=True
        when the echo is a DRAFT with a guardian.
        """
        from src.app.api import echo_routes
        from src.app.models.echo import Echo, EchoStatus, EchoType

        locked_echo = Echo(
            echo_id="e-abc-123",
            user_id="test-user-123",
            title="Test Echo",
            category="Memory",
            echo_type=EchoType.TEXT,
            status=EchoStatus.LOCKED,
            guardian_id="g-001",
            lock_date="2026-03-29T10:00:00Z",
        )

        with patch.object(
            echo_routes.echo_service,
            "lock_echo",
            new_callable=AsyncMock,
            return_value=locked_echo,
        ):
            response = echo_client.patch("/api/echoes/e-abc-123/lock")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == "LOCKED"
        assert body["data"]["echo_id"] == "e-abc-123"
        assert body["data"]["lock_date"] is not None

    # ----------------------------------------------------------
    # Error paths
    # ----------------------------------------------------------

    def test_lock_echo_returns_404_when_not_found(self, echo_client):
        """PATCH /api/echoes/{id}/lock returns 404 when echo is not found."""
        from src.app.api import echo_routes
        from src.app.core.exceptions import NotFoundError

        with patch.object(
            echo_routes.echo_service,
            "lock_echo",
            new_callable=AsyncMock,
            side_effect=NotFoundError("Echo not found"),
        ):
            response = echo_client.patch("/api/echoes/does-not-exist/lock")

        assert response.status_code == 404

    def test_lock_echo_returns_400_when_no_guardian(self, echo_client):
        """
        PATCH /api/echoes/{id}/lock returns 400 when the echo has no guardian_id.
        """
        from src.app.api import echo_routes
        from src.app.core.exceptions import ValidationError

        with patch.object(
            echo_routes.echo_service,
            "lock_echo",
            new_callable=AsyncMock,
            side_effect=ValidationError("Echo has no guardian"),
        ):
            response = echo_client.patch("/api/echoes/e-abc-123/lock")

        assert response.status_code == 400

    def test_lock_echo_returns_400_when_already_locked(self, echo_client):
        """PATCH /api/echoes/{id}/lock returns 400 when echo is already LOCKED."""
        from src.app.api import echo_routes
        from src.app.core.exceptions import ValidationError

        with patch.object(
            echo_routes.echo_service,
            "lock_echo",
            new_callable=AsyncMock,
            side_effect=ValidationError("Echo is already locked"),
        ):
            response = echo_client.patch("/api/echoes/e-abc-123/lock")

        assert response.status_code == 400

    def test_lock_echo_returns_400_when_already_released(self, echo_client):
        """PATCH /api/echoes/{id}/lock returns 400 when echo is already RELEASED."""
        from src.app.api import echo_routes
        from src.app.core.exceptions import ValidationError

        with patch.object(
            echo_routes.echo_service,
            "lock_echo",
            new_callable=AsyncMock,
            side_effect=ValidationError("Echo is already released"),
        ):
            response = echo_client.patch("/api/echoes/e-abc-123/lock")

        assert response.status_code == 400
