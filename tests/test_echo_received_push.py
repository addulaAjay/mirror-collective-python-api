"""A push when an echo arrives.

Until now the only signal was email. With the Echo Inbox about to become
reachable, a recipient who opens the app has no way of knowing something is
waiting — the inbox is a mailbox with no doorbell.

Push infrastructure already exists (SNS endpoints, device-token registration,
``PushNotificationService``); only the trial-expiry job used it.

Two rules shape this:

* **No push to someone without an account.** ``recipient_user_id`` is only set
  when the recipient has signed up. Everyone else already gets the email, whose
  CTA opens a server-rendered web viewer and needs no app.

* **Nothing sensitive on the lock screen.** An echo is deeply personal and the
  notification is visible to anyone holding the phone. The title and body stay
  generic; identifying detail travels in ``data`` for the app to use once
  opened. This mirrors the lock-screen rule the MirrorGPT prompt already
  applies to reflection nudges.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app.services.echo_service import EchoService


def a_recipient(**overrides):
    payload = dict(
        recipient_id="r-1",
        name="Caroline",
        email="caroline@example.com",
        recipient_user_id="cognito-sub-1",
    )
    payload.update(overrides)
    return SimpleNamespace(**payload)


def an_echo(**overrides):
    payload = dict(
        echo_id="e-1",
        title="The thing I never said",
        content="A very private message.",
        category="family",
    )
    payload.update(overrides)
    return SimpleNamespace(**payload)


@pytest.fixture
def service(monkeypatch):
    svc = EchoService()
    push = SimpleNamespace(send_notification=AsyncMock(return_value=1))
    monkeypatch.setattr(svc, "_push_service", push, raising=False)
    return svc, push


class TestWhoGetsAPush:
    async def test_a_registered_recipient_does(self, service):
        svc, push = service

        sent = await svc.push_echo_received(a_recipient(), "Ajay", an_echo())

        assert sent == 1
        assert push.send_notification.await_count == 1

    async def test_it_targets_the_recipients_own_user_id(self, service):
        svc, push = service

        await svc.push_echo_received(a_recipient(), "Ajay", an_echo())

        assert push.send_notification.await_args.kwargs["user_id"] == "cognito-sub-1"

    async def test_an_unregistered_recipient_does_not(self, service):
        # They still get the email, which opens the web viewer.
        svc, push = service

        sent = await svc.push_echo_received(
            a_recipient(recipient_user_id=None), "Ajay", an_echo()
        )

        assert sent == 0
        assert push.send_notification.await_count == 0

    async def test_a_blank_user_id_is_treated_as_unregistered(self, service):
        svc, push = service

        sent = await svc.push_echo_received(
            a_recipient(recipient_user_id=""), "Ajay", an_echo()
        )

        assert sent == 0
        assert push.send_notification.await_count == 0


class TestLockScreenPrivacy:
    """Visible to anyone holding the phone."""

    async def test_the_echo_title_never_appears(self, service):
        svc, push = service
        echo = an_echo(title="The thing I never said")

        await svc.push_echo_received(a_recipient(), "Ajay", echo)

        kwargs = push.send_notification.await_args.kwargs
        surface = f"{kwargs['title']} {kwargs['body']}"
        assert "never said" not in surface

    async def test_the_echo_content_never_appears(self, service):
        svc, push = service
        echo = an_echo(content="A very private message.")

        await svc.push_echo_received(a_recipient(), "Ajay", echo)

        kwargs = push.send_notification.await_args.kwargs
        surface = f"{kwargs['title']} {kwargs['body']}"
        assert "private message" not in surface

    async def test_the_category_never_appears(self, service):
        # Category alone can be revealing - "grief", "divorce".
        svc, push = service

        await svc.push_echo_received(a_recipient(), "Ajay", an_echo(category="grief"))

        kwargs = push.send_notification.await_args.kwargs
        surface = f"{kwargs['title']} {kwargs['body']}".lower()
        assert "grief" not in surface

    async def test_the_copy_is_still_useful(self, service):
        svc, push = service

        await svc.push_echo_received(a_recipient(), "Ajay", an_echo())

        kwargs = push.send_notification.await_args.kwargs
        assert kwargs["title"]
        assert "echo" in f"{kwargs['title']} {kwargs['body']}".lower()


class TestDataPayload:
    async def test_carries_the_echo_id_for_deep_linking(self, service):
        svc, push = service

        await svc.push_echo_received(a_recipient(), "Ajay", an_echo())

        data = push.send_notification.await_args.kwargs["data"]
        assert data["echo_id"] == "e-1"

    async def test_carries_a_type_the_client_can_route_on(self, service):
        svc, push = service

        await svc.push_echo_received(a_recipient(), "Ajay", an_echo())

        data = push.send_notification.await_args.kwargs["data"]
        assert data["type"] == "echo_received"


class TestFailureIsNonFatal:
    """Delivery already happened by email; a push failure must not undo it."""

    async def test_a_raising_push_service_does_not_propagate(self, service):
        svc, push = service
        push.send_notification = AsyncMock(side_effect=RuntimeError("SNS down"))

        sent = await svc.push_echo_received(a_recipient(), "Ajay", an_echo())

        assert sent == 0

    async def test_a_recipient_missing_fields_does_not_raise(self, service):
        svc, _ = service

        sent = await svc.push_echo_received(SimpleNamespace(), "Ajay", an_echo())

        assert sent == 0
