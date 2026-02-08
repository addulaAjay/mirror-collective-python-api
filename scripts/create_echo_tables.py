"""
Create Echo Vault DynamoDB tables
Script to create all required DynamoDB tables for Echo Vault functionality
(Echoes, Recipients, Guardians)
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


def create_echo_vault_tables():
    """Create all Echo Vault DynamoDB tables"""

    dynamodb = get_dynamodb_client()
    environment = os.getenv("ENVIRONMENT", "development")

    tables = [
        # ========================================
        # ECHOES TABLE
        # ========================================
        {
            "TableName": os.getenv("DYNAMODB_ECHOES_TABLE", "echoes"),
            "KeySchema": [
                {"AttributeName": "echo_id", "KeyType": "HASH"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "echo_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "category", "AttributeType": "S"},
                {"AttributeName": "recipient_id", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "user-echoes-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "status", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "category-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "category", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "recipient-echoes-index",
                    "KeySchema": [
                        {"AttributeName": "recipient_id", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "EchoVault"},
                {"Key": "Component", "Value": "Echoes"},
                {"Key": "Environment", "Value": environment},
            ],
        },
        # ========================================
        # RECIPIENTS TABLE
        # ========================================
        {
            "TableName": os.getenv("DYNAMODB_RECIPIENTS_TABLE", "echo_recipients"),
            "KeySchema": [
                {"AttributeName": "recipient_id", "KeyType": "HASH"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "recipient_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "user-recipients-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "email-index",
                    "KeySchema": [
                        {"AttributeName": "email", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "EchoVault"},
                {"Key": "Component", "Value": "Recipients"},
                {"Key": "Environment", "Value": environment},
            ],
        },
        # ========================================
        # GUARDIANS TABLE
        # ========================================
        {
            "TableName": os.getenv("DYNAMODB_GUARDIANS_TABLE", "echo_guardians"),
            "KeySchema": [
                {"AttributeName": "guardian_id", "KeyType": "HASH"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "guardian_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "user-guardians-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "email-index",
                    "KeySchema": [
                        {"AttributeName": "email", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "EchoVault"},
                {"Key": "Component", "Value": "Guardians"},
                {"Key": "Environment", "Value": environment},
            ],
        },
    ]

    created_tables = []
    failed_tables = []
    existing_tables = []

    for table_config in tables:
        table_name = table_config["TableName"]

        try:
            logger.info(f"Creating table: {table_name}")

            response = dynamodb.create_table(**table_config)

            logger.info(f"✅ Successfully initiated creation of table: {table_name}")
            logger.info(
                f"   Table ARN: {response['TableDescription'].get('TableArn', 'N/A')}"
            )

            created_tables.append(table_name)

        except ClientError as e:
            error_code = e.response["Error"]["Code"]

            if error_code == "ResourceInUseException":
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
    logger.info("ECHO VAULT TABLE CREATION SUMMARY")
    logger.info("=" * 60)

    if created_tables:
        logger.info(f"✅ Tables created: {len(created_tables)}")
        for table in created_tables:
            logger.info(f"   - {table}")

    if existing_tables:
        logger.info(f"⚠️  Tables already existed: {len(existing_tables)}")
        for table in existing_tables:
            logger.info(f"   - {table}")

    if failed_tables:
        logger.error(f"❌ Tables failed: {len(failed_tables)}")
        for failure in failed_tables:
            logger.error(f"   - {failure['table']}: {failure['error']}")
        return False

    logger.info(f"\n🎉 Total tables processed: {len(tables)}")
    logger.info("\nNote: Tables may take a few moments to become fully active.")

    return True


def verify_tables():
    """Verify that all Echo Vault tables exist and are active"""

    dynamodb = get_dynamodb_client()

    table_names = [
        os.getenv("DYNAMODB_ECHOES_TABLE", "echoes"),
        os.getenv("DYNAMODB_RECIPIENTS_TABLE", "echo_recipients"),
        os.getenv("DYNAMODB_GUARDIANS_TABLE", "echo_guardians"),
    ]

    logger.info("\n" + "=" * 50)
    logger.info("VERIFYING ECHO VAULT TABLES")
    logger.info("=" * 50)

    all_active = True

    for table_name in table_names:
        try:
            response = dynamodb.describe_table(TableName=table_name)
            status = response["Table"]["TableStatus"]
            item_count = response["Table"].get("ItemCount", 0)

            status_icon = "✅" if status == "ACTIVE" else "⏳"
            logger.info(f"{status_icon} {table_name}: {status} ({item_count} items)")

            if status != "ACTIVE":
                all_active = False

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.error(f"❌ {table_name}: NOT FOUND")
                all_active = False
            else:
                logger.error(f"❌ {table_name}: Error - {e}")
                all_active = False
        except Exception as e:
            logger.error(f"❌ {table_name}: Unexpected error - {e}")
            all_active = False

    if all_active:
        logger.info("\n🎉 All Echo Vault tables are active and ready!")
    else:
        logger.warning(
            "\n⚠️  Some tables are not active yet. Please wait and try again."
        )

    return all_active


def delete_echo_vault_tables():
    """Delete all Echo Vault tables (use with caution!)"""

    print("\n⚠️  WARNING: This will delete all Echo Vault tables and their data!")
    confirmation = input("Type 'DELETE' to confirm: ")

    if confirmation != "DELETE":
        print("Operation cancelled.")
        return False

    dynamodb = get_dynamodb_client()

    table_names = [
        os.getenv("DYNAMODB_ECHOES_TABLE", "echoes"),
        os.getenv("DYNAMODB_RECIPIENTS_TABLE", "echo_recipients"),
        os.getenv("DYNAMODB_GUARDIANS_TABLE", "echo_guardians"),
    ]

    for table_name in table_names:
        try:
            logger.info(f"Deleting table: {table_name}")
            dynamodb.delete_table(TableName=table_name)
            logger.info(f"✅ Deletion initiated for: {table_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.warning(f"⚠️  Table {table_name} does not exist")
            else:
                logger.error(f"❌ Error deleting {table_name}: {e}")
        except Exception as e:
            logger.error(f"❌ Unexpected error deleting {table_name}: {e}")

    logger.info("\n🗑️  Table deletion initiated. This may take a few minutes.")
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage Echo Vault DynamoDB tables")
    parser.add_argument(
        "action", choices=["create", "verify", "delete"], help="Action to perform"
    )
    parser.add_argument(
        "--force", action="store_true", help="Skip confirmation prompts"
    )

    args = parser.parse_args()

    logger.info("Echo Vault DynamoDB Table Management")
    logger.info(f"Environment: {os.getenv('ENVIRONMENT', 'development')}")
    logger.info(f"AWS Region: {AWS_REGION}")

    if os.getenv("DYNAMODB_ENDPOINT_URL"):
        logger.info(f"DynamoDB Endpoint: {os.getenv('DYNAMODB_ENDPOINT_URL')} (Local)")
    else:
        logger.info("DynamoDB Endpoint: AWS (Cloud)")

    if args.action == "create":
        success = create_echo_vault_tables()
        sys.exit(0 if success else 1)

    elif args.action == "verify":
        success = verify_tables()
        sys.exit(0 if success else 1)

    elif args.action == "delete":
        if args.force:
            success = delete_echo_vault_tables()
        else:
            print("Use --force flag to confirm deletion")
            success = False
        sys.exit(0 if success else 1)

    else:
        parser.print_help()
        sys.exit(1)
