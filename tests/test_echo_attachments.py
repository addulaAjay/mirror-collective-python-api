"""
Tests for multi-attachment echoes.

Covers the Attachment model round-trip, EchoService.add_attachment (append +
server-side validation), URL signing, and the email media-field derivation that
feeds the rich echo-share templates.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.app.models.echo import Attachment, AttachmentType, Echo, EchoStatus, EchoType
from src.app.services.echo_service import EchoService


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _echo_row(user_id="user-1", echo_id="echo-1", **overrides):
    echo = Echo(
        echo_id=echo_id,
        user_id=user_id,
        title="t",
        category="Memory",
        echo_type=EchoType.TEXT,
        status=EchoStatus.DRAFT,
        content="hello",
    )
    for k, v in overrides.items():
        setattr(echo, k, v)
    return echo.to_dynamodb_item()


def _wire_service(echo_row, *, head=None, head_error=None):
    """EchoService with mocked DynamoDB + S3. Returns (service, table, s3)."""
    service = EchoService()

    table = AsyncMock()
    table.get_item.return_value = {"Item": echo_row} if echo_row else {}
    table.put_item.return_value = {}

    stub_resource = MagicMock()
    stub_resource.Table = AsyncMock(return_value=table)
    service._get_dynamodb_resource = AsyncMock(  # type: ignore[method-assign]
        return_value=stub_resource
    )

    s3 = AsyncMock()
    if head_error is not None:
        s3.head_object.side_effect = head_error
    else:
        s3.head_object.return_value = head or {
            "ContentType": "video/mp4",
            "ContentLength": 2048,
            "ETag": '"abc"',
        }
    s3.generate_presigned_url.return_value = (
        "https://b.s3.r.amazonaws.com/k?X-Amz-Signature=sig"
    )
    service._get_s3_client = AsyncMock(return_value=s3)  # type: ignore[method-assign]
    return service, table, s3


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def test_attachment_round_trip_and_back_compat():
    e = Echo(
        attachments=[
            Attachment(type=AttachmentType.AUDIO, media_url="u", duration="2:32")
        ]
    )
    item = e.to_dynamodb_item()
    assert item["attachments"][0]["type"] == "AUDIO"
    e2 = Echo.from_dynamodb_item(item)
    assert e2.attachments[0].type == AttachmentType.AUDIO
    # Legacy row without the attachments key -> empty list.
    legacy = Echo.from_dynamodb_item(
        {"echo_id": "x", "echo_type": "TEXT", "status": "DRAFT"}
    )
    assert legacy.attachments == []


# --------------------------------------------------------------------------- #
# add_attachment
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_add_attachment_appends_and_sets_back_compat():
    service, table, _ = _wire_service(
        _echo_row(),
        head={"ContentType": "video/mp4", "ContentLength": 4096, "ETag": '"e"'},
    )

    echo = await service.add_attachment(
        echo_id="echo-1",
        user_id="user-1",
        key="echoes/user-1/echo-1_123.mp4",
        duration="1:25",
    )

    assert len(echo.attachments) == 1
    att = echo.attachments[0]
    assert att.type == AttachmentType.VIDEO
    assert att.duration == "1:25"
    assert att.media_url.endswith("echoes/user-1/echo-1_123.mp4")
    assert att.size_bytes == 4096
    # Back-compat mirroring of the first audio/video into legacy slots.
    assert echo.media_url == att.media_url
    assert echo.echo_type == EchoType.VIDEO
    # Persisted with attachments serialized.
    persisted = table.put_item.call_args.kwargs["Item"]
    assert persisted["attachments"][0]["type"] == "VIDEO"


@pytest.mark.asyncio
async def test_add_attachment_image_sets_poster_not_media_url():
    service, table, _ = _wire_service(
        _echo_row(),
        head={"ContentType": "image/jpeg", "ContentLength": 100, "ETag": '"e"'},
    )

    echo = await service.add_attachment(
        echo_id="echo-1", user_id="user-1", key="echoes/user-1/echo-1_1.jpg"
    )

    assert echo.attachments[0].type == AttachmentType.IMAGE
    assert echo.media_url is None  # image is not primary av media
    assert echo.poster_url and echo.poster_url.endswith("echo-1_1.jpg")


@pytest.mark.asyncio
async def test_add_attachment_second_keeps_first_media_url():
    row = _echo_row(
        media_url="https://b.s3.r.amazonaws.com/echoes/user-1/echo-1_v.mp4",
        echo_type=EchoType.VIDEO,
    )
    service, _, _ = _wire_service(
        row, head={"ContentType": "image/png", "ContentLength": 1, "ETag": '"e"'}
    )

    echo = await service.add_attachment(
        echo_id="echo-1", user_id="user-1", key="echoes/user-1/echo-1_p.png"
    )

    assert len(echo.attachments) == 1  # legacy media_url is separate from list
    assert echo.media_url.endswith("echo-1_v.mp4")  # unchanged


@pytest.mark.asyncio
async def test_add_attachment_tenancy_reject():
    from src.app.core.exceptions import ValidationError

    service, _, _ = _wire_service(_echo_row())
    with pytest.raises(ValidationError):
        await service.add_attachment(
            echo_id="echo-1",
            user_id="user-1",
            key="echoes/other-user/echo-1_1.mp4",  # wrong namespace
        )


@pytest.mark.asyncio
async def test_add_attachment_owner_reject():
    from src.app.core.exceptions import NotFoundError

    service, _, _ = _wire_service(_echo_row(user_id="owner-x"))
    with pytest.raises(NotFoundError):
        await service.add_attachment(
            echo_id="echo-1",
            user_id="user-1",  # not the owner
            key="echoes/user-1/echo-1_1.mp4",
        )


@pytest.mark.asyncio
async def test_add_attachment_missing_object_raises_not_found():
    from src.app.core.exceptions import NotFoundError

    err = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    service, _, _ = _wire_service(_echo_row(), head_error=err)
    with pytest.raises(NotFoundError):
        await service.add_attachment(
            echo_id="echo-1", user_id="user-1", key="echoes/user-1/echo-1_1.mp4"
        )


@pytest.mark.asyncio
async def test_add_attachment_with_thumb_key():
    service, _, _ = _wire_service(
        _echo_row(),
        head={"ContentType": "video/mp4", "ContentLength": 9, "ETag": '"e"'},
    )
    echo = await service.add_attachment(
        echo_id="echo-1",
        user_id="user-1",
        key="echoes/user-1/echo-1_v.mp4",
        thumb_key="echoes/user-1/echo-1_v.jpg",
    )
    assert echo.attachments[0].thumb_url.endswith("echo-1_v.jpg")
    assert echo.poster_url.endswith("echo-1_v.jpg")


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sign_attachments_signs_canonical_and_skips_signed():
    service, _, _ = _wire_service(_echo_row())
    echo = Echo(
        attachments=[
            Attachment(
                type=AttachmentType.IMAGE,
                media_url="https://b.s3.r.amazonaws.com/echoes/u/e_1.jpg",
                thumb_url="https://b.s3.r.amazonaws.com/echoes/u/e_1.jpg?X-Amz-Signature=x",
            )
        ]
    )
    await service.sign_attachments(echo)
    att = echo.attachments[0]
    assert "X-Amz-Signature" in att.media_url  # canonical got signed
    # Already-signed thumb passed through (still has exactly one signature param).
    assert att.thumb_url is not None
    assert att.thumb_url.count("X-Amz-Signature") == 1


# --------------------------------------------------------------------------- #
# Email media fields
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_build_email_media_fields():
    service, _, _ = _wire_service(_echo_row())
    echo = Echo(
        attachments=[
            Attachment(type=AttachmentType.VIDEO, media_url="v", duration="1:25"),
            Attachment(
                type=AttachmentType.IMAGE,
                media_url="https://b.s3.r.amazonaws.com/echoes/u/e_2.jpg",
            ),
        ]
    )
    fields = await service.build_email_media_fields(echo)
    assert fields["attachment_count"] == 2
    assert fields["media_duration"] == "1:25"
    assert "X-Amz-Signature" in fields["hero_image_url"]
    assert "attachment_url" not in fields  # email defaults it to open-echo URL


@pytest.mark.asyncio
async def test_build_email_media_fields_empty():
    service, _, _ = _wire_service(_echo_row())
    fields = await service.build_email_media_fields(Echo())
    assert fields == {"attachment_count": 0}


# --------------------------------------------------------------------------- #
# File (pdf) attachments
# --------------------------------------------------------------------------- #
def test_pdf_allowed_and_extension_mapping():
    from src.app.services.echo_service import (
        ALLOWED_UPLOAD_MIME_TYPES,
        _upload_extension_for,
    )

    assert "application/pdf" in ALLOWED_UPLOAD_MIME_TYPES
    assert _upload_extension_for("application/pdf") == "pdf"
    # Existing mappings preserved by the refactor.
    assert _upload_extension_for("image/png") == "png"
    assert _upload_extension_for("audio/mp4") == "m4a"
    assert _upload_extension_for("video/quicktime") == "mp4"


def test_mime_alias_normalization():
    from src.app.services.echo_service import ALLOWED_UPLOAD_MIME_TYPES, _normalize_mime

    # image/jpg (what gallery pickers send) -> the allowlisted image/jpeg.
    assert _normalize_mime("image/jpg") == "image/jpeg"
    assert _normalize_mime("IMAGE/JPG") == "image/jpeg"
    assert _normalize_mime(" image/jpeg ") == "image/jpeg"
    assert _normalize_mime("video/mov") == "video/quicktime"
    assert _normalize_mime(None) == ""
    # Canonical types pass through, and normalized aliases land on the allowlist.
    assert _normalize_mime("image/png") == "image/png"
    assert _normalize_mime("image/jpg") in ALLOWED_UPLOAD_MIME_TYPES


@pytest.mark.asyncio
async def test_add_attachment_pdf_classified_as_file():
    service, _, _ = _wire_service(
        _echo_row(),
        head={"ContentType": "application/pdf", "ContentLength": 50, "ETag": '"e"'},
    )
    echo = await service.add_attachment(
        echo_id="echo-1",
        user_id="user-1",
        key="echoes/user-1/echo-1_doc.pdf",
        filename="Greeting Card.pdf",
    )
    att = echo.attachments[0]
    assert att.type == AttachmentType.FILE
    assert att.filename == "Greeting Card.pdf"
    assert att.mime_type == "application/pdf"
    # A file is not primary A/V media — legacy media_url stays empty.
    assert echo.media_url is None
    # Files still count toward the email's "See N attachments" row.
    fields = await service.build_email_media_fields(echo)
    assert fields["attachment_count"] == 1


# --------------------------------------------------------------------------- #
# remove_attachment (edit a draft)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_remove_attachment_recomputes_legacy_fields():
    atts = [
        Attachment(
            attachment_id="v1",
            type=AttachmentType.VIDEO,
            media_url="https://b/echoes/user-1/echo-1_v.mp4",
        ),
        Attachment(
            attachment_id="i1",
            type=AttachmentType.IMAGE,
            media_url="https://b/echoes/user-1/echo-1_i.jpg",
        ),
    ]
    row = _echo_row(
        attachments=atts,
        media_url="https://b/echoes/user-1/echo-1_v.mp4",
        echo_type=EchoType.VIDEO,
    )
    service, table, _ = _wire_service(row)

    echo = await service.remove_attachment("echo-1", "user-1", "v1")

    assert [a.attachment_id for a in echo.attachments] == ["i1"]
    # The only A/V was removed → media_url cleared, type back to TEXT, poster
    # recomputed from the remaining image.
    assert echo.media_url is None
    assert echo.echo_type == EchoType.TEXT
    assert echo.poster_url.endswith("echo-1_i.jpg")
    persisted = table.put_item.call_args.kwargs["Item"]
    assert len(persisted["attachments"]) == 1


@pytest.mark.asyncio
async def test_remove_attachment_draft_only():
    from src.app.core.exceptions import ValidationError

    row = _echo_row(
        status=EchoStatus.RELEASED,
        attachments=[
            Attachment(attachment_id="a1", type=AttachmentType.FILE, media_url="x")
        ],
    )
    service, _, _ = _wire_service(row)
    with pytest.raises(ValidationError):
        await service.remove_attachment("echo-1", "user-1", "a1")


@pytest.mark.asyncio
async def test_remove_attachment_owner_and_missing_rejected():
    from src.app.core.exceptions import NotFoundError

    svc1, _, _ = _wire_service(
        _echo_row(
            user_id="owner-x",
            attachments=[
                Attachment(attachment_id="a1", type=AttachmentType.FILE, media_url="x")
            ],
        )
    )
    with pytest.raises(NotFoundError):
        await svc1.remove_attachment("echo-1", "user-1", "a1")  # not owner

    svc2, _, _ = _wire_service(
        _echo_row(
            attachments=[
                Attachment(attachment_id="a1", type=AttachmentType.FILE, media_url="x")
            ]
        )
    )
    with pytest.raises(NotFoundError):
        await svc2.remove_attachment("echo-1", "user-1", "nope")  # missing id
