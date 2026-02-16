#!/usr/bin/env python3
"""
Script to create DynamoDB tables for the Mirror Collective API
Run this script to set up the required DynamoDB tables in your AWS account

Tables created:
1. users - User profiles and authentication data
2. user_activity - User activity tracking and analytics
3. conversations - Conversation metadata and management
4. conversation_messages - Individual messages within conversations

Features enabled:
- User authentication and profile management
- Activity tracking and analytics
- Persistent conversation history
- Message threading and context management
- Conversation archival and deletion
"""
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv(override=True)


def create_users_table(dynamodb, table_name):
    """Create the users table with GSI on email"""
    try:
        table_config = {
            "TableName": table_name,
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"}  # Partition key
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
                {"AttributeName": "subscription_status", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "email-index",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "subscription-status-index",
                    "KeySchema": [
                        {"AttributeName": "subscription_status", "KeyType": "HASH"}
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
                {"Key": "Service", "Value": "mirror-collective-api"},
            ],
        }

        if os.getenv("DYNAMODB_ENDPOINT_URL"):
            if "Tags" in table_config:
                del table_config["Tags"]

        table = dynamodb.create_table(**table_config)

        # Wait for table to be created
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"✅ Table {table_name} created successfully!")
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"⚠️  Table {table_name} already exists")
            return True
        else:
            print(f"❌ Error creating table {table_name}: {e}")
            return False


def create_activity_table(dynamodb, table_name):
    """Create the user activity table"""
    try:
        table_config = {
            "TableName": table_name,
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},  # Partition key
                {"AttributeName": "activity_date", "KeyType": "RANGE"},  # Sort key
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "activity_date", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "activity-date-index",
                    "KeySchema": [
                        {"AttributeName": "activity_date", "KeyType": "HASH"}
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
                {"Key": "Service", "Value": "mirror-collective-api"},
            ],
        }

        if os.getenv("DYNAMODB_ENDPOINT_URL"):
            if "Tags" in table_config:
                del table_config["Tags"]

        table = dynamodb.create_table(**table_config)

        # Wait for table to be created
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"✅ Table {table_name} created successfully!")
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"⚠️  Table {table_name} already exists")
            return True
        else:
            print(f"❌ Error creating table {table_name}: {e}")
            return False


def create_conversations_table(dynamodb, table_name):
    """Create the conversations table with GSI on user_id"""
    try:
        table_config = {
            "TableName": table_name,
            "KeySchema": [
                {"AttributeName": "conversation_id", "KeyType": "HASH"}  # Partition key
            ],
            "AttributeDefinitions": [
                {"AttributeName": "conversation_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "last_message_at", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "user-conversations-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "last_message_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
                {"Key": "Service", "Value": "mirror-collective-api"},
            ],
        }

        if os.getenv("DYNAMODB_ENDPOINT_URL"):
            if "Tags" in table_config:
                del table_config["Tags"]

        table = dynamodb.create_table(**table_config)

        # Wait for table to be created
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"✅ Table {table_name} created successfully!")
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"⚠️  Table {table_name} already exists")
            return True
        else:
            print(f"❌ Error creating table {table_name}: {e}")
            return False


def create_messages_table(dynamodb, table_name):
    """Create the conversation messages table"""
    try:
        table_config = {
            "TableName": table_name,
            "KeySchema": [
                {
                    "AttributeName": "conversation_id",
                    "KeyType": "HASH",  # Partition key
                },
                {
                    "AttributeName": "timestamp",
                    "KeyType": "RANGE",  # Sort key for chronological order
                },
            ],
            "AttributeDefinitions": [
                {"AttributeName": "conversation_id", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
                {"AttributeName": "message_id", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "message-id-index",
                    "KeySchema": [{"AttributeName": "message_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
                {"Key": "Service", "Value": "mirror-collective-api"},
            ],
        }

        if os.getenv("DYNAMODB_ENDPOINT_URL"):
            if "Tags" in table_config:
                del table_config["Tags"]

        table = dynamodb.create_table(**table_config)

        # Wait for table to be created
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"✅ Table {table_name} created successfully!")
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"⚠️  Table {table_name} already exists")
            return True
        else:
            print(f"❌ Error creating table {table_name}: {e}")
            return False


def create_device_tokens_table(dynamodb, table_name):
    """Create the device tokens table"""
    try:
        table_config = {
            "TableName": table_name,
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},  # Partition key
                {"AttributeName": "device_token", "KeyType": "RANGE"},  # Sort key
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "device_token", "AttributeType": "S"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
                {"Key": "Service", "Value": "mirror-collective-api"},
            ],
        }

        # For local DynamoDB, remove features not supported
        if os.getenv("DYNAMODB_ENDPOINT_URL"):
            if "Tags" in table_config:
                del table_config["Tags"]

        table = dynamodb.create_table(**table_config)

        # Wait for table to be created
        print(f"Creating table {table_name}...")
        table.wait_until_exists()
        print(f"✅ Table {table_name} created successfully!")
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"⚠️  Table {table_name} already exists")
            return True
        else:
            print(f"❌ Error creating table {table_name}: {e}")
            return False


def main():
    """Main function to create all required tables"""
    # Configuration
    region = os.getenv("AWS_REGION", "us-east-1")
    environment = os.getenv("ENVIRONMENT", "development")
    endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")  # For local DynamoDB

    # Table names
    users_table = os.getenv("DYNAMODB_USERS_TABLE", f"users-{environment}")
    activity_table = os.getenv(
        "DYNAMODB_ACTIVITY_TABLE", f"user_activity-{environment}"
    )
    conversations_table = os.getenv(
        "DYNAMODB_CONVERSATIONS_TABLE", f"conversations-{environment}"
    )
    messages_table = os.getenv(
        "DYNAMODB_MESSAGES_TABLE", f"conversation_messages-{environment}"
    )
    tokens_table = os.getenv(
        "DYNAMODB_DEVICE_TOKENS_TABLE", f"user_device_tokens-{environment}"
    )

    # Determine if running locally or on AWS
    is_local = endpoint_url is not None
    target = "Local DynamoDB" if is_local else "AWS DynamoDB"

    print(f"🚀 Creating DynamoDB tables on: {target}")
    if is_local:
        print(f"📍 Endpoint: {endpoint_url}")
    print(f"📊 Environment: {environment}")
    print(f"🌍 Region: {region}")
    print(f"👥 Users table: {users_table}")
    print(f"📈 Activity table: {activity_table}")
    print(f"💬 Conversations table: {conversations_table}")
    print(f"📝 Messages table: {messages_table}")
    print(f"📱 Tokens table: {tokens_table}")
    print()

    try:
        # Initialize DynamoDB client (local or AWS)
        if is_local:
            print("🏠 Connecting to local DynamoDB...")
            dynamodb = boto3.resource(
                "dynamodb",
                endpoint_url=endpoint_url,
                region_name=region,
                aws_access_key_id="dummy",  # nosec
                aws_secret_access_key="dummy",  # nosec
            )
            # Test local connection
            try:
                dynamodb_client = boto3.client(
                    "dynamodb",
                    endpoint_url=endpoint_url,
                    region_name=region,
                    aws_access_key_id="dummy",  # nosec
                    aws_secret_access_key="dummy",  # nosec
                )
                dynamodb_client.list_tables()
                print("✅ Connected to local DynamoDB")
            except Exception:
                print("❌ Cannot connect to local DynamoDB. Make sure it's running:")
                print("   docker-compose -f docker-compose.local.yml up -d")
                sys.exit(1)
        else:
            print("☁️  Connecting to AWS DynamoDB...")
            dynamodb = boto3.resource("dynamodb", region_name=region)

        # Create tables
        users_success = create_users_table(dynamodb, users_table)
        activity_success = create_activity_table(dynamodb, activity_table)
        conversations_success = create_conversations_table(
            dynamodb, conversations_table
        )
        messages_success = create_messages_table(dynamodb, messages_table)
        tokens_success = create_device_tokens_table(dynamodb, tokens_table)

        all_success = all(
            [
                users_success,
                activity_success,
                conversations_success,
                messages_success,
                tokens_success,
            ]
        )
        if all_success:
            print()
            print("🎉 All tables created successfully!")
            print()
            print("📝 Add these environment variables to your .env file:")
            print(f"DYNAMODB_USERS_TABLE={users_table}")
            print(f"DYNAMODB_ACTIVITY_TABLE={activity_table}")
            print(f"DYNAMODB_CONVERSATIONS_TABLE={conversations_table}")
            print(f"DYNAMODB_MESSAGES_TABLE={messages_table}")
            print(f"AWS_REGION={region}")
            if is_local:
                print(f"DYNAMODB_ENDPOINT_URL={endpoint_url}")
                print()
                print("🏠 Local development setup:")
                print("- Tables will persist in Docker volume")
                print("- Access DynamoDB Admin UI at: http://localhost:8001")
                print("- No AWS costs for local development")
                print()
                print("💬 Conversation features enabled:")
                print("- Persistent chat history")
                print("- Message threading and context")
                print("- Conversation management (archive, delete, title)")
                print("- User conversation listing with pagination")
            else:
                print()
                print("💰 AWS Cost estimate:")
                print("- Pay-per-request billing (no fixed costs)")
                print("- ~$0.25 per million read requests")
                print("- ~$1.25 per million write requests")
                print("- Storage: $0.25 per GB per month")
                print("- Expected cost for small app: <$10/month (with conversations)")

        else:
            print("❌ Some tables failed to create")
            sys.exit(1)

    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
