"""One echo, several recipients.

`Echo.recipient_id` is singular everywhere — the model, the routes, the release
path, the inbox query. Rather than migrate live data to a join table, a
multi-recipient send fans out: one Echo row per recipient, sharing a
``group_id`` and the same S3 media URLs.

Everything downstream then keeps working untouched — release scheduling,
per-recipient inbox, per-recipient read state, the delivery email, the
received-echo push. Per-recipient state comes free: one person opening it does
not mark it read for everyone.

Storage quota is unaffected. ``calculate_user_storage_usage`` sums S3 objects
under a prefix rather than counting echo rows, and the copies share media, so
N recipients do not cost N times the quota.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app.services.echo_service import MAX_ECHO_RECIPIENTS, EchoService


@pytest.fixture
def service(monkeypatch):
    """EchoService whose create_echo records what it was asked to build."""
    svc = EchoService()
    created = []

    async def fake_create(user_id, data):
        created.append(data)
        return SimpleNamespace(
            echo_id=f"e-{len(created)}",
            recipient_id=data.get("recipient_id"),
            group_id=data.get("group_id"),
        )

    monkeypatch.setattr(svc, "create_echo", AsyncMock(side_effect=fake_create))
    return svc, created


BASE = {"title": "For the family", "category": "family", "echo_type": "TEXT"}


class TestFanOut:
    async def test_one_row_per_recipient(self, service):
        svc, created = service

        echoes = await svc.create_echo_group(
            "u1", {**BASE, "recipient_ids": ["r1", "r2", "r3"]}
        )

        assert len(echoes) == 3
        assert len(created) == 3

    async def test_each_row_targets_its_own_recipient(self, service):
        svc, created = service

        await svc.create_echo_group("u1", {**BASE, "recipient_ids": ["r1", "r2"]})

        assert [d["recipient_id"] for d in created] == ["r1", "r2"]

    async def test_all_rows_share_one_group_id(self, service):
        # What lets the vault collapse them into a single card, and edit or
        # delete act on the whole send.
        svc, created = service

        await svc.create_echo_group("u1", {**BASE, "recipient_ids": ["r1", "r2"]})

        ids = {d["group_id"] for d in created}
        assert len(ids) == 1
        assert next(iter(ids))

    async def test_a_second_send_gets_a_different_group_id(self, service):
        svc, created = service

        await svc.create_echo_group("u1", {**BASE, "recipient_ids": ["r1", "r2"]})
        await svc.create_echo_group("u1", {**BASE, "recipient_ids": ["r3", "r4"]})

        assert created[0]["group_id"] != created[2]["group_id"]

    async def test_content_is_identical_across_the_group(self, service):
        # The copies share media URLs; only the recipient differs.
        svc, created = service

        await svc.create_echo_group(
            "u1", {**BASE, "content": "same words", "recipient_ids": ["r1", "r2"]}
        )

        assert all(d["content"] == "same words" for d in created)
        assert all(d["title"] == "For the family" for d in created)

    async def test_recipient_ids_is_not_passed_through_to_create_echo(self, service):
        # create_echo knows only about a single recipient_id.
        svc, created = service

        await svc.create_echo_group("u1", {**BASE, "recipient_ids": ["r1", "r2"]})

        assert all("recipient_ids" not in d for d in created)


class TestSingleRecipientPathUnchanged:
    async def test_a_lone_recipient_id_still_works(self, service):
        svc, created = service

        echoes = await svc.create_echo_group("u1", {**BASE, "recipient_id": "r1"})

        assert len(echoes) == 1
        assert created[0]["recipient_id"] == "r1"

    async def test_a_single_recipient_gets_no_group_id(self, service):
        # Nothing to group it with; a group of one would only confuse the
        # vault's collapsing logic.
        svc, created = service

        await svc.create_echo_group("u1", {**BASE, "recipient_ids": ["r1"]})

        assert created[0].get("group_id") is None


class TestValidation:
    async def test_no_recipient_at_all_is_still_allowed(self, service):
        # An echo with no recipient is valid - a draft, or one addressed to a
        # guardian. My first version rejected this and broke
        # test_create_echo_without_guardian_id_passes_none with a 400.
        svc, created = service

        echoes = await svc.create_echo_group("u1", {**BASE})

        assert len(echoes) == 1
        assert created[0].get("recipient_id") is None

    async def test_an_empty_recipient_list_is_rejected(self, service):
        svc, _ = service

        with pytest.raises(Exception):
            await svc.create_echo_group("u1", {**BASE, "recipient_ids": []})

    async def test_duplicates_are_collapsed(self, service):
        # Picking the same person twice should not send them two copies.
        svc, created = service

        await svc.create_echo_group("u1", {**BASE, "recipient_ids": ["r1", "r2", "r1"]})

        assert [d["recipient_id"] for d in created] == ["r1", "r2"]

    async def test_too_many_recipients_is_rejected(self, service):
        # Unbounded fan-out is a denial-of-service shape: one request writing
        # thousands of rows and sending thousands of emails.
        svc, _ = service
        too_many = [f"r{i}" for i in range(MAX_ECHO_RECIPIENTS + 1)]

        with pytest.raises(Exception):
            await svc.create_echo_group("u1", {**BASE, "recipient_ids": too_many})

    async def test_exactly_the_cap_is_allowed(self, service):
        svc, created = service
        at_cap = [f"r{i}" for i in range(MAX_ECHO_RECIPIENTS)]

        await svc.create_echo_group("u1", {**BASE, "recipient_ids": at_cap})

        assert len(created) == MAX_ECHO_RECIPIENTS
