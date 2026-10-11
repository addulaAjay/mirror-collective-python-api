"""
Per-user daily cap on vision turns (Phase 2).

There is no per-user rate limiting anywhere else in this codebase — the old
in-memory limiter was deliberately deleted (``handler.py:104-110``) because it
only counted within a single Lambda container, and what replaced it is a
*global* API Gateway throttle (1000 rps). Global throttling does nothing
against one account running up vision cost, which is why this exists.

The counter lives on the existing ``ActivityTable`` row
(``{user_id, activity_date}``), which already holds per-user-per-day counters
like ``chat_messages``. No new table, no serverless.yml change, no new IAM.

The consume is a SINGLE conditional ``ADD`` — check-then-increment in two
round-trips would let concurrent requests both pass the check.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from src.app.core.exceptions import RateLimitExceededError
from src.app.services.chat_media_quota import DAILY_VISION_LIMIT, ChatMediaQuotaService


def a_service(update_side_effect=None):
    """Service with a mocked DynamoDB table."""
    table = MagicMock()
    table.update_item = AsyncMock(side_effect=update_side_effect)

    resource = MagicMock()
    resource.Table = AsyncMock(return_value=table)

    service = ChatMediaQuotaService()
    service._get_resource = AsyncMock(return_value=resource)  # type: ignore[method-assign]
    return service, table


def conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "nope"}},
        "UpdateItem",
    )


class TestUnderTheLimit:
    async def test_allows_a_turn_when_under_the_cap(self):
        service, table = a_service()

        await service.consume_vision_turn("user-123")  # must not raise

        table.update_item.assert_awaited_once()

    async def test_increments_atomically_with_a_single_call(self):
        # Two round-trips (read count, then write) would let concurrent
        # requests both observe a count below the limit and both proceed.
        service, table = a_service()

        await service.consume_vision_turn("user-123")

        assert table.update_item.await_count == 1
        kwargs = table.update_item.await_args.kwargs
        assert "ADD" in kwargs["UpdateExpression"]

    async def test_guards_the_increment_with_a_condition_expression(self):
        service, table = a_service()

        await service.consume_vision_turn("user-123")

        kwargs = table.update_item.await_args.kwargs
        condition = kwargs["ConditionExpression"]
        # Must tolerate the attribute not existing yet (first turn of the day)
        # as well as being below the limit.
        assert "attribute_not_exists" in condition
        assert "<" in condition

    async def test_keys_the_row_by_user_and_date(self):
        service, table = a_service()

        await service.consume_vision_turn("user-123")

        key = table.update_item.await_args.kwargs["Key"]
        assert key["user_id"] == "user-123"
        # YYYY-MM-DD, matching the ActivityTable range key format used by
        # record_activity().
        assert len(key["activity_date"]) == 10
        assert key["activity_date"].count("-") == 2

    async def test_passes_the_limit_as_the_condition_value(self):
        service, table = a_service()

        await service.consume_vision_turn("user-123")

        values = table.update_item.await_args.kwargs["ExpressionAttributeValues"]
        assert DAILY_VISION_LIMIT in values.values()


class TestOverTheLimit:
    async def test_raises_rate_limit_when_the_condition_fails(self):
        service, _ = a_service(update_side_effect=conditional_failure())

        with pytest.raises(RateLimitExceededError):
            await service.consume_vision_turn("user-123")

    async def test_the_error_carries_a_429(self):
        service, _ = a_service(update_side_effect=conditional_failure())

        with pytest.raises(RateLimitExceededError) as exc:
            await service.consume_vision_turn("user-123")

        assert exc.value.status_code == 429

    async def test_the_error_message_is_user_presentable(self):
        # It reaches the chat UI, so it must read as guidance rather than as
        # an internal limit name.
        service, _ = a_service(update_side_effect=conditional_failure())

        with pytest.raises(RateLimitExceededError) as exc:
            await service.consume_vision_turn("user-123")

        message = str(exc.value).lower()
        assert "photo" in message
        assert "conditionalcheckfailed" not in message

    async def test_the_error_does_not_leak_the_raw_limit_as_a_promise(self):
        # Mentioning the number is fine; what matters is no stack detail.
        service, _ = a_service(update_side_effect=conditional_failure())

        with pytest.raises(RateLimitExceededError) as exc:
            await service.consume_vision_turn("user-123")

        assert "UpdateItem" not in str(exc.value)


class TestFailsOpen:
    async def test_a_dynamodb_outage_does_not_block_the_turn(self):
        # Matches the existing QuotaEnforcementMiddleware, which fails open
        # "to avoid blocking legitimate users". A cost control is not worth
        # taking chat down over, and the global API Gateway throttle still
        # applies underneath.
        service, _ = a_service(
            update_side_effect=ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "UpdateItem",
            )
        )

        await service.consume_vision_turn("user-123")  # must not raise

    async def test_an_unexpected_exception_does_not_block_the_turn(self):
        service, _ = a_service(update_side_effect=RuntimeError("boom"))

        await service.consume_vision_turn("user-123")  # must not raise

    async def test_a_rate_limit_error_is_never_swallowed_by_the_fail_open(self):
        # The fail-open must not accidentally catch our own 429 and let the
        # request through — that would make the cap decorative.
        service, _ = a_service(update_side_effect=conditional_failure())

        with pytest.raises(RateLimitExceededError):
            await service.consume_vision_turn("user-123")


class TestConfiguration:
    def test_the_limit_is_a_positive_integer(self):
        assert isinstance(DAILY_VISION_LIMIT, int)
        assert DAILY_VISION_LIMIT > 0

    def test_the_limit_is_generous_enough_for_normal_use(self):
        # The plan's usage model is ~2 photos/user/day. The cap exists to
        # stop runaway cost, not to ration the feature.
        assert DAILY_VISION_LIMIT >= 10


class TestTranscriptionCap:
    """Separate, tighter counter than vision — transcription costs ~10x more."""

    async def test_allows_a_transcription_under_the_cap(self):
        service, table = a_service()

        await service.consume_transcription_turn("user-123")

        table.update_item.assert_awaited_once()

    async def test_raises_rate_limit_when_over(self):
        service, _ = a_service(update_side_effect=conditional_failure())

        with pytest.raises(RateLimitExceededError):
            await service.consume_transcription_turn("user-123")

    async def test_counts_against_a_DIFFERENT_attribute_than_vision(self):
        # Sharing one counter would let photo use exhaust voice allowance and
        # vice versa, which hides which feature is actually driving spend.
        service, table = a_service()

        await service.consume_vision_turn("u")
        vision_expr = table.update_item.await_args.kwargs["UpdateExpression"]

        await service.consume_transcription_turn("u")
        transcription_expr = table.update_item.await_args.kwargs["UpdateExpression"]

        assert vision_expr != transcription_expr

    async def test_the_message_refers_to_voice_not_photos(self):
        service, _ = a_service(update_side_effect=conditional_failure())

        with pytest.raises(RateLimitExceededError) as exc:
            await service.consume_transcription_turn("user-123")

        message = str(exc.value).lower()
        assert "voice" in message or "record" in message
        assert "photo" not in message

    async def test_fails_open_on_a_dynamodb_error(self):
        service, _ = a_service(
            update_side_effect=ClientError(
                {"Error": {"Code": "ThrottlingException"}}, "UpdateItem"
            )
        )

        await service.consume_transcription_turn("user-123")  # must not raise

    def test_the_transcription_limit_is_tighter_than_the_vision_limit(self):
        # Whisper is ~10x the per-event cost of a low-detail image (plan §7),
        # so the same numeric cap would not bound spend comparably.
        from src.app.services.chat_media_quota import DAILY_TRANSCRIPTION_LIMIT

        assert DAILY_TRANSCRIPTION_LIMIT < DAILY_VISION_LIMIT
        assert DAILY_TRANSCRIPTION_LIMIT > 0
