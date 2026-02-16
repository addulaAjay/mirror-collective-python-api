#!/usr/bin/env python3
"""
DynamoDB Update Script
Script to apply updates to existing DynamoDB tables (e.g., adding GSIs)
"""

import logging
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_dynamodb_client():
    endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")
    region = os.getenv("AWS_REGION", "us-east-1")
    if endpoint_url:
        return boto3.client(
            "dynamodb",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
        )
    return boto3.client("dynamodb", region_name=region)


def add_gsi_to_table(client, table_name, index_name, partition_key, sort_key=None):
    """Add a Global Secondary Index to an existing table"""
    try:
        logger.info(f"Adding index {index_name} to table {table_name}...")

        attribute_definitions = [{"AttributeName": partition_key, "AttributeType": "S"}]
        if sort_key:
            attribute_definitions.append(
                {"AttributeName": sort_key, "AttributeType": "S"}
            )

        key_schema = [{"AttributeName": partition_key, "KeyType": "HASH"}]
        if sort_key:
            key_schema.append({"AttributeName": sort_key, "KeyType": "RANGE"})

        client.update_table(
            TableName=table_name,
            AttributeDefinitions=attribute_definitions,
            GlobalSecondaryIndexUpdates=[
                {
                    "Create": {
                        "IndexName": index_name,
                        "KeySchema": key_schema,
                        "Projection": {"ProjectionType": "ALL"},
                    }
                }
            ],
        )
        logger.info(f"✅ Index {index_name} creation initiated for {table_name}")
        return True
    except ClientError as e:
        logger.error(f"❌ Error updating table {table_name}: {e}")
        return False


def verify_and_update_all_tables():
    """Verify all tables have the required GSIs"""
    client = get_dynamodb_client()
    environment = os.getenv("ENVIRONMENT", "development")

    # Define required indices per table
    required_indices = {
        os.getenv("DYNAMODB_USERS_TABLE", f"users-{environment}"): [
            {
                "IndexName": "email-index",
                "AttributeDefinitions": [
                    {"AttributeName": "email", "AttributeType": "S"}
                ],
            },
            {
                "IndexName": "subscription-status-index",
                "AttributeDefinitions": [
                    {"AttributeName": "subscription_status", "AttributeType": "S"}
                ],
            },
        ],
        os.getenv("DYNAMODB_ECHOES_TABLE", f"echoes-{environment}"): [
            {
                "IndexName": "user-echoes-index",
                "AttributeDefinitions": [
                    {"AttributeName": "user_id", "AttributeType": "S"},
                    {"AttributeName": "status", "AttributeType": "S"},
                ],
            },
            {
                "IndexName": "recipient-echoes-index",
                "AttributeDefinitions": [
                    {"AttributeName": "recipient_id", "AttributeType": "S"},
                    {"AttributeName": "status", "AttributeType": "S"},
                ],
            },
        ],
        os.getenv("DYNAMODB_ACTIVITY_TABLE", f"user_activity-{environment}"): [
            {
                "IndexName": "activity-date-index",
                "AttributeDefinitions": [
                    {"AttributeName": "activity_date", "AttributeType": "S"}
                ],
            }
        ],
        os.getenv("DYNAMODB_CONVERSATIONS_TABLE", f"mirror-conversations"): [
            {
                "IndexName": "user-conversations-index",
                "AttributeDefinitions": [
                    {"AttributeName": "user_id", "AttributeType": "S"},
                    {"AttributeName": "last_message_at", "AttributeType": "S"},
                ],
            }
        ],
        os.getenv("DYNAMODB_MESSAGES_TABLE", f"mirror-conversation-messages"): [
            {
                "IndexName": "message-id-index",
                "AttributeDefinitions": [
                    {"AttributeName": "message_id", "AttributeType": "S"}
                ],
            }
        ],
        os.getenv("DYNAMODB_SUBSCRIPTIONS_TABLE", f"subscriptions-{environment}"): [
            {
                "IndexName": "subscription-id-index",
                "AttributeDefinitions": [
                    {"AttributeName": "subscription_id", "AttributeType": "S"}
                ],
            }
        ],
    }

    logger.info("Starting schema verification and updates...")

    for table_name, indices in required_indices.items():
        try:
            logger.info(f"Checking table: {table_name}")
            description = client.describe_table(TableName=table_name)
            existing_gsi_names = [
                gsi["IndexName"]
                for gsi in description["Table"].get("GlobalSecondaryIndexes", [])
            ]

            for index in indices:
                index_name = index["IndexName"]
                if index_name not in existing_gsi_names:
                    logger.info(
                        f"🔍 Index {index_name} missing on {table_name}. Adding..."
                    )

                    # Extract PK and SK for add_gsi_to_table
                    attr_defs = index["AttributeDefinitions"]
                    partition_key = attr_defs[0]["AttributeName"]
                    sort_key = (
                        attr_defs[1]["AttributeName"] if len(attr_defs) > 1 else None
                    )

                    add_gsi_to_table(
                        client, table_name, index_name, partition_key, sort_key
                    )
                else:
                    logger.info(f"✅ Index {index_name} exists on {table_name}")

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.warning(
                    f"⚠️  Table {table_name} does not exist yet. Run create scripts first."
                )
            else:
                logger.error(f"❌ Error checking table {table_name}: {e}")


def main():
    verify_and_update_all_tables()
    logger.info("Schema verification complete.")


if __name__ == "__main__":
    main()
