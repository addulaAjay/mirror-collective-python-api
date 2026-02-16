#!/usr/bin/env python3
"""
Create Subscription DynamoDB tables
Script to create tables for subscription management and billing events
"""

import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add src to path to import config
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from app.config import settings

    AWS_REGION = settings.AWS_REGION
except ImportError:
    # Fallback to environment variables if config not available
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_dynamodb_client():
    """Get DynamoDB client with appropriate configuration"""

    endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")

    if endpoint_url:
        # Local DynamoDB configuration
        logger.info(f"Using local DynamoDB endpoint: {endpoint_url}")
        return boto3.client(
            "dynamodb",
            region_name=AWS_REGION,
            endpoint_url=endpoint_url,
            aws_access_key_id="dummy",  # nosec
            aws_secret_access_key="dummy",  # nosec
        )
    else:
        # AWS DynamoDB configuration
        logger.info(f"Using AWS DynamoDB in region: {AWS_REGION}")
        return boto3.client("dynamodb", region_name=AWS_REGION)


def create_subscription_tables():
    """Create all subscription-related DynamoDB tables"""

    dynamodb = get_dynamodb_client()
    environment = os.getenv("ENVIRONMENT", "development")

    tables = [
        # ========================================
        # SUBSCRIPTIONS TABLE
        # ========================================
        {
            "TableName": os.getenv(
                "DYNAMODB_SUBSCRIPTIONS_TABLE", f"subscriptions-{environment}"
            ),
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "subscription_id", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "subscription_id", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "subscription-id-index",
                    "KeySchema": [
                        {"AttributeName": "subscription_id", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "Subscription"},
                {"Key": "Environment", "Value": environment},
            ],
        },
        # ========================================
        # SUBSCRIPTION EVENTS TABLE
        # ========================================
        {
            "TableName": os.getenv(
                "DYNAMODB_SUBSCRIPTION_EVENTS_TABLE",
                f"subscription_events-{environment}",
            ),
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "Subscription"},
                {"Key": "Component", "Value": "Events"},
                {"Key": "Environment", "Value": environment},
            ],
        },
    ]

    created_tables = []
    failed_tables = []
    existing_tables = []

    for table_config in tables:
        table_name = table_config["TableName"]

        # For local DynamoDB, remove features not supported
        if os.getenv("DYNAMODB_ENDPOINT_URL"):
            if "Tags" in table_config:
                del table_config["Tags"]

        try:
            logger.info(f"Creating table: {table_name}")
            response = dynamodb.create_table(**table_config)
            logger.info(f"✅ Successfully initiated creation of table: {table_name}")
            created_tables.append(table_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                logger.warning(f"⚠️  Table {table_name} already exists")
                existing_tables.append(table_name)
            else:
                logger.error(f"❌ Error creating table {table_name}: {e}")
                failed_tables.append({"table": table_name, "error": str(e)})
        except Exception as e:
            logger.error(f"❌ Unexpected error creating table {table_name}: {e}")
            failed_tables.append({"table": table_name, "error": str(e)})

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("SUBSCRIPTION TABLE CREATION SUMMARY")
    logger.info("=" * 60)
    for table in created_tables:
        logger.info(f"✅ Created: {table}")
    for table in existing_tables:
        logger.info(f"⚠️  Exists: {table}")
    for f in failed_tables:
        logger.error(f"❌ Failed: {f['table']} - {f['error']}")

    return len(failed_tables) == 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage Subscription DynamoDB tables")
    parser.add_argument(
        "action", choices=["create", "verify", "delete"], help="Action to perform"
    )
    parser.add_argument(
        "--force", action="store_true", help="Skip confirmation prompts"
    )
    args = parser.parse_args()

    if args.action == "create":
        success = create_subscription_tables()
        sys.exit(0 if success else 1)
    else:
        logger.error(f"Action {args.action} not fully implemented in this version")
        sys.exit(1)
