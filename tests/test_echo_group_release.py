"""Releasing a multi-recipient echo.

The create flow is create -> upload attachments -> release, all against a
single echo_id. Fan-out produces N rows, but the client only ever holds the
first one's id — so without this, recipients 2..N get rows with no attachments
that are never released, and never hear about it. Plain TEXT echoes happened
to work; anything with media silently half-delivered.

Releasing the group fixes both halves at once:

* **Attachments are copied, not re-uploaded.** They are metadata rows pointing
  at S3 keys, so the siblings share the same objects. Re-uploading would cost
  N times the bytes, N times the upload time on a phone, and would break the
  shared-media property that made fan-out the right design.

* **Each sibling is released individually**, so every recipient gets their own
  email, their own push, and their own read state.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app.services.echo_service import EchoService


def an_echo(echo_id, group_id="g-1", attachments=None, **overrides):
    payload = dict(
        echo_id=echo_id,
        group_id=group_id,
        user_id="u1",
        recipient_id=f"r-{echo_id}",
        attachments=attachments if attachments is not None else [],
        media_url=None,
        poster_url=None,
    )
    payload.update(overrides)
    return SimpleNamespace(**payload)


ATTACHMENTS = [SimpleNamespace(attachment_id="a-1", s3_key="echoes/u1/a-1.mp4")]


@pytest.fixture
def service(monkeypatch):
    """EchoService with the group listing and per-echo release stubbed."""
    svc = EchoService()
    released = []
    updated = []

    async def fake_release(echo_id, user_id):
        released.append(echo_id)
        return an_echo(echo_id)

    monkeypatch.setattr(svc, "release_echo", AsyncMock(side_effect=fake_release))
    monkeypatch.setattr(
        svc,
        "update_echo",
        AsyncMock(side_effect=lambda *a, **k: updated.append((a, k))),
    )
    return svc, released, updated


class TestReleasingEveryMember:
    async def test_releases_all_rows_in_the_group(self, service, monkeypatch):
        svc, released, _ = service
        group = [an_echo("e1", attachments=ATTACHMENTS), an_echo("e2"), an_echo("e3")]
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=group))

        await svc.release_echo_group("g-1", "u1")

        assert sorted(released) == ["e1", "e2", "e3"]

    async def test_returns_one_echo_per_recipient(self, service, monkeypatch):
        svc, _, _ = service
        group = [an_echo("e1", attachments=ATTACHMENTS), an_echo("e2")]
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=group))

        result = await svc.release_echo_group("g-1", "u1")

        assert len(result) == 2

    async def test_an_unknown_group_raises(self, service, monkeypatch):
        svc, _, _ = service
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=[]))

        with pytest.raises(Exception):
            await svc.release_echo_group("nope", "u1")


class TestAttachmentPropagation:
    async def test_siblings_receive_the_source_attachments(self, service, monkeypatch):
        # The client uploaded to e1 only; e2 and e3 must end up with the same
        # attachment rows, pointing at the same S3 objects.
        svc, _, updated = service
        group = [an_echo("e1", attachments=ATTACHMENTS), an_echo("e2"), an_echo("e3")]
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=group))

        await svc.release_echo_group("g-1", "u1")

        assert group[1].attachments == ATTACHMENTS
        assert group[2].attachments == ATTACHMENTS

    async def test_the_source_echo_is_not_rewritten(self, service, monkeypatch):
        svc, _, updated = service
        group = [an_echo("e1", attachments=ATTACHMENTS), an_echo("e2")]
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=group))

        await svc.release_echo_group("g-1", "u1")

        rewritten = [a[0][0] for a, k in updated] if updated else []
        assert "e1" not in rewritten

    async def test_a_group_with_no_attachments_is_fine(self, service, monkeypatch):
        # A plain TEXT echo to several people.
        svc, released, _ = service
        group = [an_echo("e1"), an_echo("e2")]
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=group))

        await svc.release_echo_group("g-1", "u1")

        assert sorted(released) == ["e1", "e2"]

    async def test_media_urls_propagate_too(self, service, monkeypatch):
        # Older single-media rows carry media_url/poster_url alongside
        # attachments; both have to travel or the sibling renders blank.
        svc, _, _ = service
        source = an_echo("e1", media_url="s3://x.mp4", poster_url="s3://x.jpg")
        group = [source, an_echo("e2")]
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=group))

        await svc.release_echo_group("g-1", "u1")

        assert group[1].media_url == "s3://x.mp4"
        assert group[1].poster_url == "s3://x.jpg"


class TestPartialFailure:
    async def test_one_failed_release_does_not_abort_the_rest(
        self, service, monkeypatch
    ):
        # Recipient 2 failing must not deny recipients 3..N their echo.
        svc, released, _ = service
        group = [an_echo("e1"), an_echo("e2"), an_echo("e3")]
        monkeypatch.setattr(svc, "get_echo_group", AsyncMock(return_value=group))

        async def flaky(echo_id, user_id):
            if echo_id == "e2":
                raise RuntimeError("DynamoDB throttled")
            released.append(echo_id)
            return an_echo(echo_id)

        monkeypatch.setattr(svc, "release_echo", AsyncMock(side_effect=flaky))

        result = await svc.release_echo_group("g-1", "u1")

        assert sorted(released) == ["e1", "e3"]
        assert len(result) == 2


class TestGetEchoGroup:
    """Exercises the real lookup.

    The release tests above stub get_echo_group, so they never touched its
    body — and the first version was broken in a way they could not see:
    get_user_echoes returns (page, cursor), not a list, so iterating the
    return value yielded the list and the cursor string rather than echoes,
    and the filter matched nothing. It would have returned [] for every group
    in production. mypy caught it; these tests pin it.
    """

    async def test_unpacks_the_paginated_result(self, service, monkeypatch):
        svc, _, _ = service
        page = [an_echo("e1"), an_echo("e2", group_id="other"), an_echo("e3")]
        monkeypatch.setattr(
            svc, "get_user_echoes", AsyncMock(return_value=(page, None))
        )

        found = await svc.get_echo_group("g-1", "u1")

        assert [e.echo_id for e in found] == ["e1", "e3"]

    async def test_follows_the_cursor_across_pages(self, monkeypatch):
        # A group member beyond page one must still be found, or the release
        # silently skips that recipient.
        svc = EchoService()
        pages = [
            ([an_echo("e1")], "cursor-1"),
            ([an_echo("e2", group_id="other"), an_echo("e3")], None),
        ]
        calls = {"n": 0}

        async def paged(user_id, cursor=None, **kwargs):
            result = pages[calls["n"]]
            calls["n"] += 1
            return result

        monkeypatch.setattr(svc, "get_user_echoes", AsyncMock(side_effect=paged))

        found = await svc.get_echo_group("g-1", "u1")

        assert [e.echo_id for e in found] == ["e1", "e3"]
        assert calls["n"] == 2

    async def test_an_empty_group_id_short_circuits(self, service, monkeypatch):
        svc, _, _ = service
        lister = AsyncMock(return_value=([], None))
        monkeypatch.setattr(svc, "get_user_echoes", lister)

        assert await svc.get_echo_group("", "u1") == []
        assert lister.await_count == 0
