"""Create Reflection Room V1 DynamoDB tables (spec §3 + §14).

Provisions the four tables locally (when ``DYNAMODB_ENDPOINT_URL`` is set)
or in AWS. Pattern matches ``scripts/create_echo_tables.py``.

Idempotent — tables that already exist are skipped, not recreated.

Usage:
    python scripts/create_reflection_room_tables.py
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv(override=True)

# Allow ``import src.app.*`` when run from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("create_reflection_room_tables")

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
ENV = os.getenv("ENVIRONMENT", "development")


def _client():
    endpoint = os.getenv("DYNAMODB_ENDPOINT_URL")
    if endpoint:
        logger.info(f"Using local DynamoDB endpoint: {endpoint}")
        return boto3.client(
            "dynamodb",
            region_name=AWS_REGION,
            endpoint_url=endpoint,
            aws_access_key_id="dummy",  # nosec
            aws_secret_access_key="dummy",  # nosec
        )
    logger.info(f"Using AWS DynamoDB in region: {AWS_REGION}")
    return boto3.client("dynamodb", region_name=AWS_REGION)


def _table_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "TableName": os.getenv(
                "DYNAMODB_REFLECTION_SESSIONS_TABLE",
                f"mc_reflection_sessions-{ENV}",
            ),
            "KeySchema": [{"AttributeName": "session_id", "KeyType": "HASH"}],
            "AttributeDefinitions": [
                {"AttributeName": "session_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "user_id-created_at-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "ReflectionRoom"},
                {"Key": "Component", "Value": "Sessions"},
                {"Key": "Environment", "Value": ENV},
            ],
        },
        {
            "TableName": os.getenv(
                "DYNAMODB_ECHO_LOOP_STATE_TABLE",
                f"mc_echo_loop_state-{ENV}",
            ),
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "loop_id", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "loop_id", "AttributeType": "S"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "ReflectionRoom"},
                {"Key": "Component", "Value": "EchoLoopState"},
                {"Key": "Environment", "Value": ENV},
            ],
        },
        {
            "TableName": os.getenv(
                "DYNAMODB_PRACTICE_COMPLETIONS_TABLE",
                f"mc_practice_completions-{ENV}",
            ),
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "completion_id", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "completion_id", "AttributeType": "S"},
                {"AttributeName": "practice_id", "AttributeType": "S"},
                {"AttributeName": "completed_at", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "practice_id-completed_at-index",
                    "KeySchema": [
                        {"AttributeName": "practice_id", "KeyType": "HASH"},
                        {"AttributeName": "completed_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "ReflectionRoom"},
                {"Key": "Component", "Value": "PracticeCompletions"},
                {"Key": "Environment", "Value": ENV},
            ],
        },
        {
            "TableName": os.getenv(
                "DYNAMODB_USER_PERSONALIZATION_TABLE",
                f"mc_user_personalization-{ENV}",
            ),
            "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "ReflectionRoom"},
                {"Key": "Component", "Value": "UserPersonalization"},
                {"Key": "Environment", "Value": ENV},
            ],
        },
        {
            "TableName": os.getenv(
                "DYNAMODB_LIFE_ANCHORS_TABLE",
                f"mc_life_anchors-{ENV}",
            ),
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "anchor_id", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "anchor_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "status", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "created-at-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "MirrorGPT"},
                {"Key": "Component", "Value": "LifeAnchors"},
                {"Key": "Environment", "Value": ENV},
            ],
        },
    ]


def create_tables() -> int:
    client = _client()
    is_local = bool(os.getenv("DYNAMODB_ENDPOINT_URL"))
    created: List[str] = []
    existing: List[str] = []
    failed: List[Dict[str, str]] = []

    for cfg in _table_definitions():
        # DDB Local doesn't support Tags.
        if is_local and "Tags" in cfg:
            cfg = {k: v for k, v in cfg.items() if k != "Tags"}

        name = cfg["TableName"]
        try:
            logger.info(f"Creating table: {name}")
            client.create_table(**cfg)
            created.append(name)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ResourceInUseException":
                logger.info(f"  → already exists, skipping: {name}")
                existing.append(name)
            else:
                logger.error(f"  → failed: {name}: {exc}")
                failed.append({"table": name, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.error(f"  → unexpected: {name}: {exc}")
            failed.append({"table": name, "error": str(exc)})

    logger.info("=" * 60)
    logger.info("Reflection Room table creation summary:")
    logger.info(f"  created:  {len(created)}")
    logger.info(f"  existing: {len(existing)}")
    logger.info(f"  failed:   {len(failed)}")
    if failed:
        for f in failed:
            logger.error(f"  ✗ {f['table']}: {f['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(create_tables())
