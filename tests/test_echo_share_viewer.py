"""
Tests for the public tokenized echo viewer (email-recipient playback).

Covers the share-token JWT, the shareable-echo loader (RELEASED + recipient
gating), presigned-attachment resolution (view vs download), and the viewer /
redirect route handlers.
"""

import os

os.environ.setdefault("SHARE_TOKEN_SECRET", "test-secret")
os.environ.setdefault("SHARE_BASE_URL", "https://api.test")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from src.app.api import share_routes  # noqa: E402
from src.app.core.share_token import (  # noqa: E402
    build_share_url,
    create_share_token,
    share_base_url,
    verify_share_token,
)
from src.app.models.echo import (  # noqa: E402
    Attachment,
    AttachmentType,
    Echo,
    EchoStatus,
    EchoType,
)
from src.app.services.echo_service import EchoService  # noqa: E402


def _svc_with_echo(echo_row):
    svc = EchoService()
    table = AsyncMock()
    table.get_item.return_value = {"Item": echo_row} if echo_row else {}
    res = MagicMock()
    res.Table = AsyncMock(return_value=table)
    svc._get_dynamodb_resource = AsyncMock(  # type: ignore[method-assign]
        return_value=res
    )
    return svc


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
def test_share_token_roundtrip():
    tok = create_share_token("e1", "r1")
    payload = verify_share_token(tok, "e1")
    assert payload and payload["recipient_id"] == "r1"


def test_share_token_rejects_wrong_echo_tampered_empty():
    tok = create_share_token("e1", "r1")
    assert verify_share_token(tok, "e2") is None  # bound to a different echo
    assert verify_share_token(tok[:-3] + "zzz", "e1") is None  # tampered
    assert verify_share_token("", "e1") is None


def test_build_share_url():
    # Robust against whatever SHARE_BASE_URL the env supplies (e.g. a real .env).
    assert build_share_url("e1", "tok") == f"{share_base_url()}/share/echo/e1?t=tok"


# --------------------------------------------------------------------------- #
# get_shared_echo
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_shared_echo_released_recipient_match():
    echo = Echo(
        echo_id="e1",
        user_id="u1",
        recipient_id="r1",
        status=EchoStatus.RELEASED,
        content="hi",
    )
    svc = _svc_with_echo(echo.to_dynamodb_item())
    got = await svc.get_shared_echo("e1", "r1")
    assert got and got.content == "hi"


@pytest.mark.asyncio
async def test_get_shared_echo_rejects_wrong_recipient_and_draft():
    released = Echo(echo_id="e1", recipient_id="r1", status=EchoStatus.RELEASED)
    svc = _svc_with_echo(released.to_dynamodb_item())
    assert await svc.get_shared_echo("e1", "intruder") is None

    draft = Echo(echo_id="e1", recipient_id="r1", status=EchoStatus.DRAFT)
    svc2 = _svc_with_echo(draft.to_dynamodb_item())
    assert await svc2.get_shared_echo("e1", "r1") is None


# --------------------------------------------------------------------------- #
# presign_shared_attachment
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_presign_shared_attachment_view_and_download():
    echo = Echo(
        echo_id="e1",
        recipient_id="r1",
        status=EchoStatus.RELEASED,
        attachments=[
            Attachment(
                attachment_id="a1",
                type=AttachmentType.IMAGE,
                media_url="https://b.s3.r.amazonaws.com/echoes/u/e_1.jpg",
                filename="pic.jpg",
            )
        ],
    )
    svc = _svc_with_echo(echo.to_dynamodb_item())
    s3 = AsyncMock()
    s3.generate_presigned_url.return_value = "https://signed"
    svc._get_s3_client = AsyncMock(return_value=s3)  # type: ignore[method-assign]

    url = await svc.presign_shared_attachment("e1", "r1", "a1", download=False)
    assert url == "https://signed"
    params = s3.generate_presigned_url.call_args.kwargs["Params"]
    assert "ResponseContentDisposition" not in params

    await svc.presign_shared_attachment("e1", "r1", "a1", download=True)
    params = s3.generate_presigned_url.call_args.kwargs["Params"]
    assert 'attachment; filename="pic.jpg"' in params["ResponseContentDisposition"]

    assert (
        await svc.presign_shared_attachment("e1", "r1", "nope", download=False) is None
    )


@pytest.mark.asyncio
async def test_presign_shared_attachment_primary_legacy():
    echo = Echo(
        echo_id="e1",
        recipient_id="r1",
        status=EchoStatus.RELEASED,
        echo_type=EchoType.VIDEO,
        media_url="https://b.s3.r.amazonaws.com/echoes/u/e_v.mp4",
    )
    svc = _svc_with_echo(echo.to_dynamodb_item())
    s3 = AsyncMock()
    s3.generate_presigned_url.return_value = "https://signed"
    svc._get_s3_client = AsyncMock(return_value=s3)  # type: ignore[method-assign]
    url = await svc.presign_shared_attachment("e1", "r1", "primary", download=False)
    assert url == "https://signed"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_viewer_route_renders_message_and_attachments():
    echo = Echo(
        echo_id="e1",
        recipient_id="r1",
        status=EchoStatus.RELEASED,
        content="A private message",
        attachments=[
            Attachment(
                attachment_id="a1",
                type=AttachmentType.VIDEO,
                media_url="x",
                duration="1:25",
                filename="clip.mp4",
            )
        ],
    )
    tok = create_share_token("e1", "r1")
    with patch.object(
        share_routes.echo_service,
        "get_shared_echo",
        AsyncMock(return_value=echo),
    ):
        resp = await share_routes.shared_echo_viewer("e1", t=tok)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.body.decode()
    assert "A private message" in body
    assert "Your Echo" in body
    assert "mode=download" in body  # per-attachment download link
    assert "clip.mp4" in body


def test_format_date_ordinals_and_bad_input():
    assert share_routes._format_date("2025-05-04T10:00:00Z") == "May 4th, 2025"
    assert share_routes._format_date("2025-05-01T00:00:00Z") == "May 1st, 2025"
    assert share_routes._format_date("2025-05-21T00:00:00Z") == "May 21st, 2025"
    assert share_routes._format_date(None) == ""
    assert share_routes._format_date("not-a-date") == ""


@pytest.mark.asyncio
async def test_viewer_matches_branding_and_plays_every_media_type_in_page():
    """Image, video and audio all render as inline, in-page players (not just
    links), each with a download affordance, inside the branded 7539:4157 shell."""
    echo = Echo(
        echo_id="e1",
        recipient_id="r1",
        status=EchoStatus.RELEASED,
        content="A gentle note.",
        release_date="2025-05-04T10:00:00Z",
        attachments=[
            Attachment(
                attachment_id="img1",
                type=AttachmentType.IMAGE,
                media_url="x",
                filename="photo.jpg",
            ),
            Attachment(
                attachment_id="vid1",
                type=AttachmentType.VIDEO,
                media_url="x",
                duration="1:25",
                filename="clip.mp4",
            ),
            Attachment(
                attachment_id="aud1",
                type=AttachmentType.AUDIO,
                media_url="x",
                duration="2:32",
                filename="voice.m4a",
            ),
        ],
    )
    tok = create_share_token("e1", "r1")
    with (
        patch.object(
            share_routes.echo_service, "get_shared_echo", AsyncMock(return_value=echo)
        ),
        patch.object(
            share_routes.echo_service,
            "presign_shared_attachment",
            AsyncMock(return_value="https://signed-s3/file"),
        ),
    ):
        resp = await share_routes.shared_echo_viewer("e1", t=tok)

    body = resp.body.decode()
    # In-page players for every media type (play within the page).
    assert '<img class="media"' in body
    assert '<video class="media" controls playsinline' in body
    assert '<audio class="audio" controls' in body
    # Inline players use the direct presigned S3 src (range-friendly playback).
    assert body.count("https://signed-s3/file") >= 3
    # A download option per attachment (forced Content-Disposition).
    assert body.count("mode=download") == 3
    # Subtitle + formatted date (the design's "shared with you" line).
    assert "A private message has been shared with you" in body
    assert "May 4th, 2025" in body
    # Branding from Figma 7539:4157: gold, Cormorant, blur cards, glow CTA,
    # star divider, lock footer, starfield bg.
    assert "#f2e1b0" in body and "Cormorant Garamond" in body
    assert "backdrop-filter:blur(30px)" in body
    assert "GET THE APP" in body
    assert "divider-star.png" in body
    assert "icon-lock.png" in body
    assert "email-bg-starfield.png" in body
    # The viewer MUST ship a CSP that permits its own resources. The global
    # security-headers middleware sets `default-src 'self'` via setdefault,
    # which would block the S3 images + <video>/<audio> media and the Google
    # fonts (the live "media won't play / logo broken" bug). The page sets its
    # own CSP so setdefault leaves it intact.
    csp = resp.headers["content-security-policy"]
    assert "img-src" in csp and "https://*.amazonaws.com" in csp
    assert "media-src 'self' https://*.amazonaws.com" in csp
    assert "fonts.googleapis.com" in csp and "fonts.gstatic.com" in csp


@pytest.mark.asyncio
async def test_viewer_csp_overrides_strict_global_default():
    """Sanity: the viewer response carries a permissive, media-friendly CSP
    (not the global `default-src 'self'`) so the browser loads S3 media."""
    echo = Echo(
        echo_id="e1",
        recipient_id="r1",
        status=EchoStatus.RELEASED,
        content="hi",
    )
    tok = create_share_token("e1", "r1")
    with patch.object(
        share_routes.echo_service, "get_shared_echo", AsyncMock(return_value=echo)
    ):
        resp = await share_routes.shared_echo_viewer("e1", t=tok)
    csp = resp.headers["content-security-policy"]
    # Must NOT be the bare strict policy that blocks media/images.
    assert csp != "default-src 'self'"
    assert "media-src" in csp
    # Error pages carry the CSP too (they also load fonts + logo).
    err = share_routes._error_page("nope", 404)
    assert "media-src" in err.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_viewer_route_bad_token():
    resp = await share_routes.shared_echo_viewer("e1", t="bad-token")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_attachment_redirect_view_download_and_bad_token():
    tok = create_share_token("e1", "r1")
    with patch.object(
        share_routes.echo_service,
        "presign_shared_attachment",
        AsyncMock(return_value="https://signed-s3"),
    ):
        resp = await share_routes.shared_attachment_redirect(
            "e1", "a1", t=tok, mode="view"
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://signed-s3"
    assert resp.headers["cache-control"] == "no-store"

    resp_bad = await share_routes.shared_attachment_redirect(
        "e1", "a1", t="bad", mode="view"
    )
    assert resp_bad.status_code == 403
