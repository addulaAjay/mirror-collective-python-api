"""Shared base for Reflection Room V1 repositories.

The four repos share the same DDB connection conventions as the existing
``DynamoDBService`` / ``EchoService`` classes:
  * a single ``aioboto3.Session`` per repo instance
  * env-driven table names (``DYNAMODB_*``)
  * ``DYNAMODB_ENDPOINT_URL`` activates DynamoDB Local with dummy creds

Repos accept an optional ``session`` argument for tests, which can pass an
in-memory shim (see ``tests/_fakes/fake_dynamodb.py``).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import aioboto3

logger = logging.getLogger(__name__)


class _RepoBase:
    """Holds the aioboto3 session + DDB connection params."""

    def __init__(self, session: Optional[Any] = None):
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")
        self.session = session if session is not None else aioboto3.Session()

    def _get_dynamodb_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"region_name": self.region}
        if self.endpoint_url:
            kwargs.update(
                {
                    "endpoint_url": self.endpoint_url,
                    "aws_access_key_id": "dummy",  # nosec
                    "aws_secret_access_key": "dummy",  # nosec
                }
            )
        return kwargs
