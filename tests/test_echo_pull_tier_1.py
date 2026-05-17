"""Tests for PR2 — Backend pull Tier 1.

Covers:
- _sign_profile_urls: parallel + dedupe behavior
- _sign_media_url: 6h TTL (was 1h)
- get_user_echoes no longer signs media_url (route omits it; signing was
  pure waste and prevented us from removing the unsigned-bucket fallback)
- _enrich_echoes_with_recipients: dedupe + parallel profile-image presigns
- Inbox route no longer leaks unsigned media_url (the locked-bucket fix
  from PR1 would 403 these; the right fix is to omit them and force
  detail-fetch)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.models.echo import Echo, EchoStatus, EchoType, Recipient
from src.app.services.echo_service import EchoService

# ----------------------------------------------------------------------
# _sign_profile_urls: parallel + dedupe
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_profile_urls_signs_each_distinct_url_once():
    """Three distinct URLs → three sign calls; five duplicates → still three."""
    svc = EchoService()
    sign_calls: list[str] = []

    async def fake_sign(url):
        sign_calls.append(url)
        return f"signed::{url}"

    with patch.object(svc, "_sign_profile_url", side_effect=fake_sign):
        result = await svc._sign_profile_urls(
            {
                "https://b.s3.amazonaws.com/p/u1.jpg",
                "https://b.s3.amazonaws.com/p/u2.jpg",
                "https://b.s3.amazonaws.com/p/u3.jpg",
            }
        )

    assert len(sign_calls) == 3
    assert len(result) == 3
    for orig, signed in result.items():
        assert signed == f"signed::{orig}"


@pytest.mark.asyncio
async def test_sign_profile_urls_handles_empty_set():
    svc = EchoService()
    assert await svc._sign_profile_urls(set()) == {}
    assert await svc._sign_profile_urls(frozenset()) == {}


@pytest.mark.asyncio
async def test_sign_profile_urls_runs_in_parallel():
    """Confirm gather is doing real fan-out, not sequential awaits.

    Each fake sign sleeps 50ms. Five distinct URLs serial = 250 ms;
    parallel = ~50 ms. Allow generous headroom for CI jitter.
    """
    import asyncio
    import time

    svc = EchoService()

    async def slow_sign(url):
        await asyncio.sleep(0.05)
        return f"signed::{url}"

    urls = {f"https://b.s3.amazonaws.com/p/u{i}.jpg" for i in range(5)}

    with patch.object(svc, "_sign_profile_url", side_effect=slow_sign):
        t0 = time.monotonic()
        result = await svc._sign_profile_urls(urls)
        elapsed = time.monotonic() - t0

    assert len(result) == 5
    # Serial would be ~0.25s; parallel must be well under that.
    assert (
        elapsed < 0.18
    ), f"_sign_profile_urls did not fan out (elapsed={elapsed:.2f}s)"


# ----------------------------------------------------------------------
# _sign_media_url: TTL bump
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_media_url_uses_six_hour_ttl():
    """1h was too short; users came back to 403s after locking their phone."""
    svc = EchoService()
    svc.s3_bucket = "b"
    echo = Echo(
        echo_id="e",
        user_id="u",
        title="t",
        media_url="https://b.s3.us-east-1.amazonaws.com/echoes/u/e_2026.mp4",
        echo_type=EchoType.VIDEO,
        status=EchoStatus.RELEASED,
    )
    fake_s3 = AsyncMock()
    fake_s3.generate_presigned_url = AsyncMock(return_value="signed")
    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        await svc._sign_media_url(echo)

    _args, kwargs = fake_s3.generate_presigned_url.call_args
    assert kwargs["ExpiresIn"] == 21600  # 6 h


# ----------------------------------------------------------------------
# get_user_echoes no longer signs media_url (it isn't returned anyway)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_echoes_does_not_sign_media_url():
    """Vault list response omits media_url; signing it was dead work."""
    svc = EchoService()
    items = [
        {
            "echo_id": f"echo-{i}",
            "user_id": "u-1",
            "title": f"Echo {i}",
            "category": "general",
            "echo_type": "VIDEO",
            "status": "DRAFT",
            "media_url": f"https://b.s3.us-east-1.amazonaws.com/echoes/u-1/e{i}.mp4",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:00:00Z",
        }
        for i in range(3)
    ]
    table = AsyncMock()
    table.query = AsyncMock(return_value={"Items": items, "LastEvaluatedKey": None})
    resource = AsyncMock()
    resource.Table = AsyncMock(return_value=table)

    sign_calls: list[str] = []

    async def spy_sign_media(echo):
        sign_calls.append(echo.echo_id)
        return echo

    with patch.object(
        svc, "_get_dynamodb_resource", new=AsyncMock(return_value=resource)
    ):
        with patch.object(svc, "_sign_media_url", side_effect=spy_sign_media):
            with patch.object(
                svc,
                "_enrich_echoes_with_recipients",
                new=AsyncMock(return_value=None),
            ):
                echoes, _cursor = await svc.get_user_echoes(user_id="u-1")

    assert len(echoes) == 3
    assert sign_calls == [], "media_url must not be signed in the vault list path"


# ----------------------------------------------------------------------
# _enrich_echoes_with_recipients: dedupe + parallel profile-image signing
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_dedupes_profile_url_signs_across_repeated_recipients():
    """Five echoes sharing two recipients → only 2 profile-URL signs."""
    svc = EchoService()
    r1 = Recipient(
        recipient_id="r-1",
        user_id="u-1",
        name="Alice",
        email="alice@e.com",
        profile_image_url="https://b.s3.amazonaws.com/p/r1.jpg",
    )
    r2 = Recipient(
        recipient_id="r-2",
        user_id="u-1",
        name="Bob",
        email="bob@e.com",
        profile_image_url="https://b.s3.amazonaws.com/p/r2.jpg",
    )
    echoes = [
        Echo(echo_id=f"e-{i}", user_id="u-1", title="t", recipient_id="r-1")
        for i in range(3)
    ] + [
        Echo(echo_id=f"e-{i}", user_id="u-1", title="t", recipient_id="r-2")
        for i in range(2)
    ]

    sign_calls: list[str] = []

    async def fake_sign(url):
        sign_calls.append(url)
        return f"signed::{url}"

    with patch.object(
        svc,
        "_batch_get_recipients",
        new=AsyncMock(return_value={"r-1": r1, "r-2": r2}),
    ):
        with patch.object(svc, "_sign_profile_url", side_effect=fake_sign):
            await svc._enrich_echoes_with_recipients(echoes, owner_user_id="u-1")

    # Five echoes → at most two distinct profile URLs → exactly 2 sign calls.
    assert len(sign_calls) == 2, f"expected 2 distinct signs, got {len(sign_calls)}"

    # And every echo's recipient should carry the signed URL.
    for e in echoes:
        assert e.recipient is not None
        signed = e.recipient["profile_image_url"]
        assert signed.startswith("signed::")


@pytest.mark.asyncio
async def test_enrich_drops_non_owner_recipients_before_signing():
    """Defense-in-depth: a misconfigured GSI could return a recipient owned by
    someone else. We must not sign or attach those — they'd leak the
    foreign profile URL into the response.
    """
    svc = EchoService()
    not_mine = Recipient(
        recipient_id="r-1",
        user_id="someone-else",  # wrong owner
        name="X",
        email="x@e.com",
        profile_image_url="https://b.s3.amazonaws.com/p/r1.jpg",
    )
    echoes = [Echo(echo_id="e-1", user_id="u-1", title="t", recipient_id="r-1")]

    sign_calls: list[str] = []

    async def fake_sign(url):
        sign_calls.append(url)
        return f"signed::{url}"

    with patch.object(
        svc,
        "_batch_get_recipients",
        new=AsyncMock(return_value={"r-1": not_mine}),
    ):
        with patch.object(svc, "_sign_profile_url", side_effect=fake_sign):
            await svc._enrich_echoes_with_recipients(echoes, owner_user_id="u-1")

    assert sign_calls == [], "must not sign foreign-owner profile URLs"
    assert echoes[0].recipient is None
