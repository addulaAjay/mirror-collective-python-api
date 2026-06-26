"""Regression tests for DynamoDBService.get_user_quiz_results.

In production, GET /api/mirrorgpt/quiz/results and the anonymous->user quiz
migration both failed with:

    ValidationException: The table does not have the specified index: user-index

because the query used IndexName="user-index" while the table provisioned by
serverless.yml exposes the GSI as "user-quiz-index" (user_id HASH +
completed_at RANGE). These tests pin the correct index name and recency order.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from tests import conftest as _conftest

EXPECTED_INDEX = "user-quiz-index"


@pytest.fixture
def ddb_module():
    """Yield the real (un-mocked) dynamodb_service module.

    conftest replaces DynamoDBService with a Mock at import time; stop that
    patcher, reload the module to get the real class, then restart it.
    """
    _conftest.dynamodb_service_patcher.stop()
    try:
        import src.app.services.dynamodb_service as module

        importlib.reload(module)
        yield module
    finally:
        _conftest.mock_dynamodb_service = _conftest.dynamodb_service_patcher.start()


def _service_with_table(module, table):
    service = module.DynamoDBService()
    resource = MagicMock(name="DynamoDBResource")
    resource.Table = AsyncMock(return_value=table)
    service._get_resource = AsyncMock(return_value=resource)
    return service


async def test_queries_the_user_quiz_index(ddb_module):
    table = MagicMock()
    table.query = AsyncMock(
        return_value={"Items": [{"quiz_id": "q1", "user_id": "u1"}]}
    )
    service = _service_with_table(ddb_module, table)

    results = await service.get_user_quiz_results("u1")

    assert results == [{"quiz_id": "q1", "user_id": "u1"}]
    kwargs = table.query.call_args.kwargs
    assert kwargs["IndexName"] == EXPECTED_INDEX
    assert kwargs["ExpressionAttributeValues"] == {":uid": "u1"}
    # GSI has completed_at as the range key; newest result must come first.
    assert kwargs["ScanIndexForward"] is False


async def test_unknown_index_validationexception_is_swallowed(ddb_module):
    """A real ValidationException (e.g. wrong index) must not crash callers."""
    table = MagicMock()
    table.query = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "ValidationException", "Message": "no such index"}},
            "Query",
        )
    )
    service = _service_with_table(ddb_module, table)

    results = await service.get_user_quiz_results("u1")

    assert results == []
